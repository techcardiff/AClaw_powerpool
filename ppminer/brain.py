"""Minimal WorldQuant BRAIN API client: login (with face-scan hand-off),
simulate, poll, alpha stats and the submission check.

The face scan itself is always done by the account owner. `on_biometric(url)`
is the hook where the Telegram broker will send that link to the owner.
"""
from __future__ import annotations

import time
from typing import Callable
from urllib.parse import urljoin

import requests

from . import markets, sandbox

BASE = "https://api.worldquantbrain.com"


class AuthRequired(RuntimeError):
    pass


class Brain:
    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()

    # ── auth ────────────────────────────────────────────────────────────────
    @classmethod
    def from_cookie(cls, cookie_header: str) -> "Brain":
        from .cookies import only_session
        s = requests.Session()
        s.headers["Cookie"] = only_session(cookie_header)
        return cls(s)

    def login(self, user: str, password: str, on_biometric: Callable[[str], None],
              wait_seconds: int = 600, poll: int = 10) -> None:
        self.s.auth = (user, password)
        r = self.s.post(f"{BASE}/authentication")
        if r.status_code == 201:
            return
        if r.status_code == 401 and r.headers.get("WWW-Authenticate") == "persona":
            link = urljoin(r.url, r.headers["Location"])
            on_biometric(link)
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                time.sleep(poll)
                if self.s.post(link).status_code == 201:
                    return
            raise AuthRequired("face scan not completed in time")
        raise AuthRequired(f"login failed: HTTP {r.status_code} {r.text[:200]}")

    def is_authenticated(self) -> bool:
        r = self.s.get(f"{BASE}/authentication")
        return r.status_code == 200

    # ── http helpers ────────────────────────────────────────────────────────
    def _req(self, method: str, url: str, **kw) -> requests.Response:
        for _ in range(30):
            r = self.s.request(method, url, timeout=60, **kw)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 5)))
                continue
            if r.status_code == 401:
                raise AuthRequired("session expired")
            r.raise_for_status()
            return r
        raise RuntimeError(f"rate limited too long on {url}")

    def _poll(self, url: str, max_wait: int = 1800) -> dict:
        """BRAIN async protocol: 200 + Retry-After (often empty body) = still running."""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            r = self._req("GET", url)
            ra = float(r.headers.get("Retry-After", 0) or 0)
            if ra == 0 and r.text.strip():
                return r.json()
            time.sleep(max(ra, 5))
        raise TimeoutError(url)

    # ── research calls ──────────────────────────────────────────────────────
    def simulate(self, expr: str, settings: dict) -> dict:
        r = self._req("POST", f"{BASE}/simulations", json={"type": "REGULAR", "settings": settings, "regular": expr})
        sim = self._poll(r.headers["Location"])
        if "alpha" not in sim:       # a WARNING status (e.g. unit mismatch) still produces an alpha
            raise RuntimeError(f"simulation failed: {sim.get('status')} {sim.get('message', '')}")
        return self.alpha(sim["alpha"])

    def multi_simulate(self, items: list[tuple[str, dict]], max_wait: int = 3600) -> list:
        """Up to 10 alphas in one BRAIN multi-simulation. Returns one entry per item, in
        order: the alpha dict, or an Exception with BRAIN's message for that child."""
        if len(items) == 1:
            try:
                return [self.simulate(*items[0])]
            except AuthRequired:
                raise
            except Exception as e:
                return [e]
        body = [{"type": "REGULAR", "settings": st, "regular": ex} for ex, st in items]
        r = self._req("POST", f"{BASE}/simulations", json=body)
        parent = self._poll(r.headers["Location"], max_wait=max_wait)
        children = parent.get("children") or []
        if not children:
            raise RuntimeError(f"multi-simulation failed: {parent.get('status')} {parent.get('message', '')}")
        out = []
        for child in children:
            try:
                sim = self._poll(f"{BASE}/simulations/{child}", max_wait=600)
                if "alpha" not in sim:
                    raise RuntimeError(f"simulation failed: {sim.get('status')} {sim.get('message', '')}")
                out.append(self.alpha(sim["alpha"]))
            except AuthRequired:
                raise
            except Exception as e:
                out.append(e)
        return out

    def alpha(self, alpha_id: str) -> dict:
        return self._req("GET", f"{BASE}/alphas/{alpha_id}").json()

    def check(self, alpha_id: str, max_wait: int = 900) -> dict:
        return self._poll(f"{BASE}/alphas/{alpha_id}/check", max_wait=max_wait)

    def set_description(self, alpha_id: str, text: str) -> dict:
        """Power Pool needs >= 100 chars in the alpha's regular description (Idea / Rationale)."""
        return self._req("PATCH", f"{BASE}/alphas/{alpha_id}", json={"regular": {"description": text}}).json()

    def submit(self, alpha_id: str, max_wait: int = 900) -> dict:
        """POST /submit, then poll the same URL until BRAIN finishes its submission checks.
        Returns the final body (on refusal it lists the failing checks) plus the alpha's
        status afterwards: the poll URL can 404 once the submission is done, so the
        alpha itself (status ACTIVE, stage OS) is the real confirmation."""
        if sandbox.ON:
            raise RuntimeError("submitting is disabled in a sandbox (PP_SANDBOX); only the live scheduler submits")
        r = self.s.post(f"{BASE}/alphas/{alpha_id}/submit", timeout=60)
        if r.status_code == 401:
            raise AuthRequired("session expired")
        if r.status_code == 403 and "ALREADY_SUBMITTED" in r.text:
            # an earlier attempt got through (the alpha can take a moment to show ACTIVE)
            for _ in range(12):
                a = self.alpha(alpha_id)
                if a.get("status") == "ACTIVE":
                    break
                time.sleep(10)
            return {"http": r.status_code, "body": {}, "submitted": a.get("status") == "ACTIVE", "status": a.get("status"),
                    "stage": a.get("stage"), "tags": a.get("tags"), "dateSubmitted": a.get("dateSubmitted")}
        if r.status_code >= 400:
            raise RuntimeError(f"submit refused: HTTP {r.status_code} {r.text[:300]}")
        deadline = time.time() + max_wait
        while time.time() < deadline:
            g = self.s.get(f"{BASE}/alphas/{alpha_id}/submit", timeout=60)
            ra = float(g.headers.get("Retry-After", 0) or 0)
            if ra == 0:
                a = self.alpha(alpha_id)
                return {"http": g.status_code, "body": g.json() if g.text.strip() else {},
                        "submitted": a.get("status") == "ACTIVE", "status": a.get("status"),
                        "stage": a.get("stage"), "tags": a.get("tags"), "dateSubmitted": a.get("dateSubmitted")}
            time.sleep(max(ra, 5))
        raise TimeoutError(f"submit of {alpha_id} still running after {max_wait}s")


def settings_for(row, *, universe: str | None = None) -> dict:
    return {
        "instrumentType": "EQUITY",
        "region": row["region"] or "USA",
        "universe": universe or row["universe"] or markets.universe(row["region"]),
        "delay": row["delay"] if row["delay"] is not None else 1,
        "decay": row["decay"] if row["decay"] is not None else 6,
        "neutralization": row["neutralization"] or "SUBINDUSTRY",
        "truncation": row["truncation"] if row["truncation"] is not None else 0.08,
        "pasteurization": "ON",
        "unitHandling": "VERIFY",
        "nanHandling": "OFF",
        "language": "FASTEXPR",
        "visualization": False,
    }


def summarize(alpha: dict, checks: dict | None = None) -> dict:
    is_ = alpha.get("is") or {}
    out = {k: is_.get(k) for k in ("sharpe", "fitness", "turnover", "returns", "drawdown")}
    out["alpha_id"] = alpha.get("id")
    if checks:
        items = (checks.get("is") or {}).get("checks", [])
        out["checks"] = items
        for c in items:
            # SELF_CORRELATION is the regular-alpha test; Power Pool only uses its own correlation check
            if c.get("name") == "POWER_POOL_CORRELATION" and c.get("value") is not None:
                out["pp_corr"] = c["value"]
    return out
