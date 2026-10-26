import datetime as dt

import pytest

from ppminer import telegram as tgm
from ppminer.brain import BASE


class Resp:
    def __init__(self, code, headers=None, url=f"{BASE}/authentication"):
        self.status_code, self.headers, self.url = code, headers or {}, url


class FakeSession:
    """BRAIN: first POST asks for a face scan; the link returns 201 after `scan_after` polls."""
    def __init__(self, scan_after=2, persona=True):
        self.auth, self.cookies, self.calls = None, {}, []
        self.scan_after, self.persona = scan_after, persona

    def post(self, url, timeout=None):
        self.calls.append(url)
        if url.endswith("/authentication"):
            if not self.persona:
                self.cookies["t"] = "tok"
                return Resp(201)
            return Resp(401, {"WWW-Authenticate": "persona", "Location": "/authentication/persona?inquiry=abc"})
        if len(self.calls) - 1 >= self.scan_after:
            self.cookies["t"] = "tok"
            return Resp(201)
        return Resp(401)


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat, text, button=None):
        self.sent.append((chat, text, button))


class Clock:
    def __init__(self, t=dt.datetime(2026, 10, 23, 12, 0).timestamp()):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


CFG = {"bot_token": "x", "renew_before_minutes": 30, "quiet_hours": [0, 7], "scan_wait_minutes": 10,
       "retry_after_minutes": 20, "admin_chat_ids": [],
       "accounts": {"acc1": {"chat_id": 1, "email": "a@x", "password": "p"}}}


@pytest.fixture
def saved(monkeypatch):
    store = {}
    monkeypatch.setattr(tgm.ck, "save_cookie", lambda a, c: store.__setitem__(a, c))
    monkeypatch.setattr(tgm.ck, "whoami", lambda c: {"user_id": "U1", "expiry_seconds": 14400} if "tok" in c else None)
    return store


def broker(session, clock=None):
    clock = clock or Clock()
    return tgm.Broker(CFG, tg=FakeTG(), http_factory=lambda: session, now=clock.now, sleep=clock.sleep)


def test_face_scan_link_goes_to_owner_and_session_is_saved(saved):
    s = FakeSession(scan_after=2)
    b = broker(s)
    assert b.login("acc1")
    assert s.auth == ("a@x", "p")
    chat, text, button = b.tg.sent[0]
    assert chat == 1 and button == ("Verify face", f"{BASE}/authentication/persona?inquiry=abc")
    assert saved == {"acc1": "t=tok"}
    assert "✅" in b.tg.sent[-1][1]


def test_no_face_scan_needed(saved):
    b = broker(FakeSession(persona=False))
    assert b.login("acc1")
    assert len(b.tg.sent) == 1 and "no face scan" in b.tg.sent[0][1]


def test_timeout_reminds_once_then_expires(saved):
    b = broker(FakeSession(scan_after=10_000))
    assert not b.login("acc1")
    texts = [t for _, t, _ in b.tg.sent]
    assert sum("still waiting" in t for t in texts) == 1
    assert "expired" in texts[-1] and saved == {}


def test_tick_respects_quiet_hours_and_retry_gap(monkeypatch, saved):
    monkeypatch.setattr(tgm.ck, "saved_accounts", lambda: [])
    started = []
    monkeypatch.setattr(tgm.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(args[0])})())
    night = Clock(dt.datetime(2026, 10, 23, 3, 0).timestamp())
    assert broker(FakeSession(), night).tick() == []
    b = broker(FakeSession())
    assert b.tick() == ["acc1"]
    b.last_prompt["acc1"] = b.now()
    assert b.tick() == []                       # prompted recently; don't spam the owner


def test_strangers_are_ignored_except_id(saved):
    b = broker(FakeSession())
    b.handle({"chat": {"id": 999}, "text": "/login acc1"})
    assert b.tg.sent == []
    b.handle({"chat": {"id": 999}, "text": "/id"})
    assert "999" in b.tg.sent[0][1]


TWO = dict(CFG, admin_chat_ids=[1], accounts={
    "me": {"chat_id": 1, "email": "me@x", "password": "p"},
    "her": {"chat_id": 2, "email": "her@x", "password": "q"}})


def _no_threads(monkeypatch):
    started = []
    monkeypatch.setattr(tgm.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(args[0])})())
    return started


def test_logout_notice_once_to_owner_and_admin(monkeypatch, saved):
    monkeypatch.setattr(tgm.ck, "saved_accounts", lambda: ["me", "her"])
    monkeypatch.setattr(tgm.ck, "load_cookie", lambda a: "t=live" if a == "me" else "t=old")
    monkeypatch.setattr(tgm.ck, "whoami", lambda c: {"user_id": "U", "expiry_seconds": 9000} if c == "t=live" else None)
    started = _no_threads(monkeypatch)
    clock = Clock()
    b = tgm.Broker(TWO, tg=FakeTG(), http_factory=FakeSession, now=clock.now, sleep=clock.sleep)
    assert b.tick() == ["her"]
    to_her = [(t, btn) for c, t, btn in b.tg.sent if c == 2]
    to_me = [(t, btn) for c, t, btn in b.tg.sent if c == 1]
    assert len(to_her) == 1 and "🔴 her: logged out" in to_her[0][0]
    assert len(to_me) == 1 and "🔴 her" in to_me[0][0] and to_me[0][1] is None   # admin copy, no scan link
    clock.sleep(30 * 60)
    b.last_prompt["her"] = clock.now()
    b.tick()
    assert sum("logged out" in t for _, t, _ in b.tg.sent) == 2                   # not repeated


def test_logout_notice_waits_for_end_of_quiet_hours(monkeypatch, saved):
    monkeypatch.setattr(tgm.ck, "saved_accounts", lambda: [])
    _no_threads(monkeypatch)
    clock = Clock(dt.datetime(2026, 10, 24, 3, 0).timestamp())
    b = tgm.Broker(TWO, tg=FakeTG(), http_factory=FakeSession, now=clock.now, sleep=clock.sleep)
    b.tick()
    assert b.tg.sent == []
    clock.t = dt.datetime(2026, 10, 24, 7, 5).timestamp()
    b.tick()
    assert sum("logged out" in t for _, t, _ in b.tg.sent) == 3   # me, her, admin copy of her


def test_admin_gets_her_success_but_not_her_link(saved):
    s = FakeSession(scan_after=1)
    b = tgm.Broker(TWO, tg=FakeTG(), http_factory=lambda: s, now=Clock().now, sleep=Clock().sleep)
    assert b.login("her")
    to_me = [(t, btn) for c, t, btn in b.tg.sent if c == 1]
    assert [t[:6] for t, _ in to_me] == ["✅ her:"] and to_me[0][1] is None
    assert any(btn for c, _, btn in b.tg.sent if c == 2)


def test_auto_prompt_off_only_notifies(monkeypatch, saved):
    monkeypatch.setattr(tgm.ck, "saved_accounts", lambda: [])
    started = _no_threads(monkeypatch)
    b = tgm.Broker(dict(TWO, auto_prompt=False), tg=FakeTG(), http_factory=FakeSession,
                   now=Clock().now, sleep=Clock().sleep)
    assert b.tick() == [] and started == []
    assert any("/login her" in t for c, t, _ in b.tg.sent if c == 2)


def test_disabled_placeholder_account_is_skipped(tmp_path):
    import json, os
    p = tmp_path / "telegram.json"
    p.write_text(json.dumps({"bot_token": "x", "admin_chat_ids": [5], "accounts": {
        "me": {"chat_id": 5, "email": "me@real.org", "password": "p"},
        "account2": {"enabled": False, "chat_id": 0, "email": "", "password": ""}}}))
    os.chmod(p, 0o600)
    assert list(tgm.load_config(str(p))["accounts"]) == ["me"]


def test_stuck_login_expires_and_duplicate_gets_reply():
    import ppminer.telegram as T
    sent = []

    class TG:
        def send(self, chat, text, button=None):
            sent.append(text)

    b = T.Broker.__new__(T.Broker)
    import threading
    b.cfg = {"scan_wait_minutes": 10, "accounts": {"X": {"chat_id": 1}}}
    b.lock, b.tg = threading.Lock(), TG()
    clock = [1000.0]
    b.now = lambda: clock[0]
    b._login = lambda account, reason: True
    b.running = {"X": 1000.0}
    assert b.login("X", "requested") is False                 # fresh attempt in progress: reply, don't start
    assert any("already in progress" in t for t in sent)
    clock[0] += 16 * 60                                       # older than scan window + 5 min: treat as stuck
    assert b.login("X", "requested") is True
    assert "X" not in b.running
