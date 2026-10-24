# Run on the 3080 from /data/quant-agent: copies |sharpe|>=0.8 rows from the agent memory DBs into /tmp/pp_export.sqlite.
import sqlite3, os
OUT = "/tmp/pp_export.sqlite"
if os.path.exists(OUT): os.remove(OUT)
o = sqlite3.connect(OUT)
o.execute("""create table rows(src text, src_id integer, db_region text, account text, expression text,
             sharpe real, fitness real, turnover real, returns real, drawdown real, region text,
             settings text, alpha_id text, reasoning text, ts real)""")
for db, reg in [("data/alpha_memory.db", "USA"), ("data/alpha_memory_glb.db", "GLB"), ("data/alpha_memory_hkg.db", "HKG")]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    acols = {r[1] for r in c.execute("pragma table_info(attempts)")}
    reg_col = "region" if "region" in acols else "NULL"
    q1 = f"""select 'attempts', id, account_id, coalesce(wrapped_expr, expression), sharpe, fitness, turnover, returns,
             drawdown, {reg_col}, wrap_settings, alpha_id, substr(reasoning,1,1500), ts
             from attempts where sharpe is not null and abs(sharpe) >= 0.8"""
    q2 = """select 'raw_signals', id, account_id, expression, sharpe, fitness, turnover, returns, drawdown, NULL, NULL,
            alpha_id, substr(reasoning,1,1500), ts from raw_signals where sharpe is not null and abs(sharpe) >= 0.8"""
    n = 0
    for q in (q1, q2):
        for r in c.execute(q):
            o.execute("insert into rows values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (r[0], r[1], reg) + r[2:])
            n += 1
    print(db, n)
o.commit(); o.execute("vacuum"); o.close()
print(os.path.getsize(OUT) // 1024, "KB")
