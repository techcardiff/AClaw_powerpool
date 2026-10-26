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
    monkeypatch.setattr(autosubmit, "run_account", lambda conn, cat, acc, dry_run, quality=None: seen.setdefault(acc, dry_run))
    monkeypatch.setattr(autosubmit.Catalog, "add_account_fields", lambda self, conn, acc: None)
    monkeypatch.setattr(autosubmit, "single_submitter", lambda: __import__("contextlib").nullcontext())
    autosubmit.run(str(tmp_path / "pp.db"), str(tmp_path))
    assert seen == {"X": True}
