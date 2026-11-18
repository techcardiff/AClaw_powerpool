import pytest

from ppminer import autosubmit, sandbox
from ppminer.brain import Brain


def test_sandbox_never_submits(monkeypatch):
    monkeypatch.setattr(sandbox, "ON", True)
    b = Brain.__new__(Brain)          # no session: the guard must fire before any request
    with pytest.raises(RuntimeError, match="sandbox"):
        b.submit("abc123")


def test_sandbox_forces_dry_run(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "ON", True)
    seen = {}
    monkeypatch.setattr(autosubmit, "load_config", lambda: {"accounts": {"X": {"enabled": True, "dry_run": False}}})
    monkeypatch.setattr(autosubmit, "run_account", lambda conn, cat, acc, dry_run, quality=None, scopes=None: seen.setdefault(acc, dry_run))
    monkeypatch.setattr(autosubmit.Catalog, "add_account_fields", lambda self, conn, acc: None)
    monkeypatch.setattr(autosubmit, "single_submitter", lambda: __import__("contextlib").nullcontext())
    autosubmit.run(str(tmp_path / "pp.db"), str(tmp_path))
    assert seen == {"X": True}


def test_power_pool_error_does_not_stop_regular_and_super(monkeypatch, tmp_path):
    from ppminer import autosubmit
    calls = []
    monkeypatch.setattr(autosubmit, "load_config", lambda: {"accounts": {"X": {
        "enabled": True, "dry_run": False, "regular": {"enabled": True, "dry_run": False},
        "super": {"enabled": True, "dry_run": False}}}})

    def boom(*a, **k):
        raise RuntimeError("rate limited too long on https://api.worldquantbrain.com/alphas/A/check")

    monkeypatch.setattr(autosubmit, "run_account", boom)
    monkeypatch.setattr(autosubmit.regular, "run_account", lambda *a, **k: calls.append("regular") or {"status": "done"})
    monkeypatch.setattr(autosubmit.superalpha, "run_account", lambda *a, **k: calls.append("super") or {"status": "done"})
    monkeypatch.setattr(autosubmit.Catalog, "add_account_fields", lambda self, conn, acc: None)
    monkeypatch.setattr(autosubmit, "single_submitter", lambda: __import__("contextlib").nullcontext())
    out = autosubmit.run(str(tmp_path / "pp.db"), str(tmp_path))
    assert out["X"]["status"] == "error" and calls == ["regular", "super"]
