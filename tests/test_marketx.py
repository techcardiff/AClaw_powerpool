import sqlite3

from ppminer import marketx


def test_dataset_claim_is_exclusive(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""create table account_datasets (account, region, dataset, value_score, alpha_count);
                          create table account_fields (account, region, dataset, field);""")
    for acct in ("A", "B"):
        conn.execute("insert into account_datasets values (?, 'EUR', 'ds1', 1, 0)", (acct,))
        conn.execute("insert into account_fields values (?, 'EUR', 'ds1', 'f')", (acct,))
    monkeypatch.setattr(marketx, "_pending_templates", lambda *a, **k: [{"expr": "rank(f)"}])
    mx = marketx.connect(str(tmp_path / "m.db"))
    assert marketx._dataset(conn, mx, "A", "EUR") == "ds1"
    assert marketx._dataset(conn, mx, "B", "EUR") is None          # already claimed by A
    assert marketx._dataset(conn, mx, "A", "EUR") == "ds1"          # A keeps working on its own dataset
