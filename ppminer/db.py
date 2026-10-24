"""SQLite store for the Power Pool miner (data/pp.db)."""
from __future__ import annotations

import sqlite3

SCHEMA = """
create table if not exists candidates (
    id              integer primary key,
    key             text unique,          -- region|delay|canonical expression
    expr            text not null,
    region          text, delay integer, universe text, neutralization text,
    decay           integer, truncation real,
    n_ops           integer, n_fields integer,
    fields          text,  categories text, datasets text,   -- JSON lists
    single_category integer, single_dataset integer,
    problems        text,                 -- JSON list from pp.check
    sharpe real, fitness real, turnover real, returns real, drawdown real,
    n_seen          integer default 1,
    sources         text,                 -- JSON list of "src:id@account"
    best_alpha_id   text,
    reasoning       text,
    skeleton        text,                 -- structure with numbers removed
    family          text,                 -- sorted field set: same data, any structure
    family_rank     integer,              -- 1 = representative of its family
    flags           text,                 -- JSON list (suspicious_sharpe, negative, ...)
    status          text,                 -- pp_ready | purify | weak | reject
    created_at      timestamp default current_timestamp
);
create index if not exists ix_cand_status on candidates(status);
create index if not exists ix_cand_family on candidates(family);

create table if not exists resims (
    id            integer primary key,
    candidate_id  integer references candidates(id),
    account       text,
    expr          text,
    settings      text,
    alpha_id      text,
    sharpe real, fitness real, turnover real, returns real, drawdown real,
    checks        text,                   -- JSON from /alphas/{id}/check
    pp_corr       real,
    status        text,                   -- queued | running | done | error
    error         text,
    created_at    timestamp default current_timestamp
);

create table if not exists llm_jobs (
    id            integer primary key,
    candidate_id  integer references candidates(id),
    kind          text,                   -- describe | purify
    model         text,
    input         text,
    output        text,                   -- JSON
    ok            integer,
    error         text,
    created_at    timestamp default current_timestamp
);

create table if not exists descriptions (
    candidate_id  integer primary key references candidates(id),
    idea          text,
    rationale_data text,
    rationale_ops text,
    text          text,
    model         text,
    created_at    timestamp default current_timestamp
);

create table if not exists rewrites (
    id            integer primary key,
    parent_id     integer references candidates(id),
    target_category text,
    expr          text,
    n_ops         integer,
    categories    text,
    valid         integer,
    problems      text,
    rationale     text,
    model         text,
    created_at    timestamp default current_timestamp,
    unique(parent_id, expr)
);
"""


def connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")
    c.executescript(SCHEMA)
    return c
