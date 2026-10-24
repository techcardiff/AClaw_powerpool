# Handoff: AClaw Power Pool miner (`ppminer`)

For a student taking over development. Read this first, then `README.md` for
day-to-day commands. Written 24 Oct 2026.

---

## 1. What this is for

We are WorldQuant BRAIN research consultants. `ppminer` finds and prepares
**Power Pool alphas**: small, simple alphas that BRAIN accepts with a lower
bar than regular alphas.

Power Pool rules (from BRAIN's consultant documentation):

| Rule | Value |
|---|---|
| Sharpe | ≥ 1.0 |
| Operators | ≤ 8 in total, repeats count; `ts_backfill` / `group_backfill` are free |
| Data fields | ≤ 3 unique (grouping fields like `industry` don't count) |
| Power Pool correlation | < 0.5 against the account's existing Power Pool alphas (or Sharpe ≥ 10% higher than the most correlated one) |
| Turnover | must pass BRAIN's turnover tests |
| Description | ≥ 100 characters, "Idea / Rationale for data used / Rationale for operators used" |
| Theme | **not needed in the first 6 weeks** after an account's first Power Pool submission; after that each pure Power Pool alpha must match an active theme |

Quotas per account: 4 Power Pool alphas/day in weeks 1–2, 2/day in weeks 3–4,
1/day in weeks 5–6, then 1/day and 10/month, theme required. **Days are
counted in New York time.** BRAIN's `REGULAR_SUBMISSION` check shows a
combined limit of 4 submissions per day per account, and Power Pool submissions
count toward it.

There is also a separate goal: **regular alphas** (much stricter: Sharpe ≥ 1.58,
fitness ≥ 1.0, self-correlation < 0.7 against everything the account has submitted).
That is "Lane C" below.

---

## 2. How the pipeline works

```
 past research (old agent DB, Alpha Studio DB)
        │ harvest            ─ parse, dedupe, check PP shape locally
        ▼
 candidates (pp.db) ──► Lane A  ─ PP inventory: harvested, Qwen rewrites, Qwen explore ideas
                    └─► Lane C  ─ regular alphas + settings sweeps of near misses
        │ runner / resim     ─ simulate on BRAIN with an account's session
        ▼
 resims (pp.db)     ──► verdicts (verdict.py): Power Pool + regular, from BRAIN's own checks
        │ picklist           ─ pull daily PnL, fold anything correlating ≥ 0.5, one pick per cluster
        ▼
 picks + descriptions (Qwen) ──► dashboard ──► human approves ──► submit
```

**Nothing is ever submitted automatically.** A person approves each submission.

Qwen3.8-27B (on the A6000) does the language work: rewriting mixed-category alphas into
one category (`purify.py`), proposing alphas on new datasets (`explore.py`), revising
ideas after seeing their results (`revise.py`), and writing descriptions (`describe.py`).

---

## 3. Where it runs

| Part | Where | Notes |
|---|---|---|
| Miner jobs, dashboard, `data/pp.db`, session cookies | **3080 server**, `/data/aclaw_powerpool`, Linux user `alpha` | tmux session `ppminer` (windows `qwen-tunnel`, `web`), started by `scripts/start_3080.sh`. The @reboot cron entry (`scripts/install_3080_cron.sh`) is **not installed yet**, so after a 3080 restart run `scripts/start_3080.sh` by hand |
| Qwen3.8-27B (vLLM) | **A6000 server**, port 8060, reached through a tunnel on 127.0.0.1:18060 | The vLLM process belongs to **another lab user**; if they stop it, all Qwen steps fail until it's back. Simulations still work. |
| Telegram login broker | **Mac** (`scripts/start_mac_broker.sh`) | Telegram is blocked from the cluster. New session cookies are copied to the 3080. |
| Code | GitHub `techcardiff/AClaw_powerpool` | `data/` and `ref/` are git-ignored on purpose |

SSH host aliases (`qa-3080`, `qa-a6000`) come from the supervisor's `~/.ssh/config`;
ask for your own key rather than sharing one.

Dashboard from your laptop: `ssh -N -L 5070:127.0.0.1:5070 qa-3080`, then open
http://127.0.0.1:5070.

---

## 4. Code map

All code is in `ppminer/`. `python -m ppminer --help` lists the commands.

| Module | What it does | Edit here when… |
|---|---|---|
| `expr.py` | FASTEXPR parser: operator count, data fields, canonical form, skeleton, **lint** (vector fields need `vec_*`, no scientific notation, no `ts_corr(x, x)`, no quoted groups) | BRAIN rejects an expression our checks let through |
| `pp.py` | Local Power Pool checks (`check()`), `submit_expr()` (sign flip for negative Sharpe) | the Power Pool rules change |
| `catalog.py` | Field → category / dataset / type, from catalog snapshots plus per-account synced fields | |
| `fields.py` | `fields-sync`: which datasets/fields an account can use; block list of fields BRAIN rejected | accounts see different data (e.g. no `model77`) |
| `harvest.py` | Import past simulations, dedupe, classify (`pp_ready`, `pp_multi`, `purify`, `weak`, `reject`) | adding a new source of old alphas |
| `select.py` | `shortlist()`: diverse selection (one per family, caps per dataset and per operator shape, skips known collisions) | selection is too narrow or too repetitive |
| `lanes.py` | Lane A / Lane C queues, collision families, settings sweeps for near misses, sync of an account's submitted alphas | changing what each lane works on |
| `runner.py` | `run`: the continuous loop (revise → batch → simulate → recheck → pick list → explore); waits for a new login when the session expires | changing the loop or its priorities |
| `revise.py` | Qwen feedback loop for explore ideas (flip / drop / revise, up to 3 rounds) | improving how ideas get revised |
| `explore.py` | Qwen ideas on datasets we have never mined (prompt with a menu of structures) | prompt work |
| `purify.py` | Qwen rewrites of strong mixed-category alphas into one category | prompt work |
| `describe.py` | Qwen descriptions (Idea / Rationale) | description quality |
| `verdict.py` | Power Pool verdict and regular verdict, from BRAIN's `/check` output | BRAIN adds or renames checks |
| `picks.py` | PnL download, correlation clustering, pick list, approve/reject | |
| `brain.py` | BRAIN API: login with face-scan hand-off, simulate, poll, check, description, submit | BRAIN API changes |
| `cookies.py` | Session cookies: read from Chrome, save per account (mode 600) | |
| `telegram.py` | Telegram broker that sends the face-scan link to each account owner | |
| `llm.py` | Qwen client (OpenAI-compatible, JSON schema, thinking off by default) | switching models |
| `web.py`, `web.html` | Dashboard (read-only view + login and pick approval buttons) | UI work |
| `db.py` | Main schema; `lanes.migrate()` adds the newer columns/tables | schema changes |
| `__main__.py` | CLI | adding a command |

### Database (`data/pp.db`, SQLite)

| Table | Content |
|---|---|
| `candidates` | every alpha idea (harvested, rewrite, explore), local checks, status, family |
| `resims` | each simulation on BRAIN: account, expression, settings, results, raw checks JSON, lane, purpose |
| `picks` | pick list per account, status `proposed` / `approved` / `rejected` / `submitted` |
| `descriptions`, `rewrites`, `llm_jobs` | Qwen outputs |
| `pnl` | cached daily PnL per alpha |
| `account_fields`, `account_datasets`, `field_blocklist` | per-account data access |
| `account_submitted` | each account's submitted alphas (for collision checks) |
| `runner_state` | what the runner is doing (shown on the dashboard) |

---

## 5. Getting started (development)

1. Get from your supervisor: SSH access to the 3080 as `alpha`, a GitHub invite, and a
   copy of `data/catalog/` (BRAIN field catalogs, not in git).
2. Clone and set up:
   ```bash
   git clone git@github.com:techcardiff/AClaw_powerpool.git && cd AClaw_powerpool
   python3 -m venv .venv && .venv/bin/pip install requests pytest
   .venv/bin/python -m pytest -q tests
   ```
3. Most work (parser, selection, verdicts, prompts, UI) can be developed and tested
   **without a BRAIN session**: write tests against saved `/check` JSON and expressions.
4. To try changes on real data, work on the 3080 copy, or ask for a recent snapshot of `pp.db`.
5. Work on a branch and open a pull request; run the tests before every push.

---

## 6. Rules (please read)

1. **Never commit `data/`.** It holds live BRAIN session cookies, account passwords for
   the Telegram broker, and our research. `.gitignore` covers it; check `git status` before committing.
2. **Never submit or tag alphas yourself** unless the account owner asked you to. Submissions are
   permanent and stay in the account's correlation pool even if untagged later.
3. **One submitter at a time.** On 24 Oct two sessions submitted to the same account within two
   minutes of each other, each unaware of the other, and it took time to work out who submitted
   what. Agree who submits before anyone does.
4. **One writer per `pp.db`.** Run one job per account at a time; never run jobs against the old
   Mac snapshot `data/pp.db.moved-to-3080`.
5. **Keep accounts separate.** Each consultant account's alphas must be its own research. Don't
   submit the same or near-identical expressions on two accounts (the code excludes the other
   account's simulated and submitted families; keep it that way).
6. **Descriptions must be true.** Qwen sometimes describes the wrong direction, the wrong window,
   or guesses what a field is. Check each description against the expression and the field
   catalog before it goes on BRAIN.
7. **Face scans are done only by the account owner.** The Telegram bot only delivers the link.
   Never automate or work around BRAIN's identity check.

---

## 7. Lessons learned (save yourself the debugging)

- **`POWER_POOL_CORRELATION = WARNING`** means "above 0.5 against an alpha already in the Power Pool",
  not "pending".
- When **`REGULAR_SUBMISSION` fails** (daily limit reached), BRAIN **skips the correlation checks**;
  they stay `PENDING` until midnight New York time.
- Alphas that **fail `CONCENTRATED_WEIGHT`** never get correlation checks from BRAIN.
- **Theme matches can disappear** between checks; three alphas lost "D1 Power Pool Oct`26" after their
  description was set. Unexplained; worth asking a research advisor.
- After `POST /alphas/{id}/submit`, polling the same URL can return **404 once it's done**; confirm by
  reading the alpha (`status ACTIVE`, stage `OS`, tag `PowerPoolSelected`).
- BRAIN rejects **scientific notation** (`1e-8`); write `0.00000001`.
- Accounts see different data: HT52122 has no `model77`; a field BRAIN calls invalid goes on the block list.
- **The old research database is very redundant**: 11 passing alphas collapsed into 2 clusters, and many
  "different" alphas are the same 10-day reversal or the same analyst-estimate family.
- **One-shot Qwen ideas on new data mostly score Sharpe < 0.5.** The value is in the revise loop.
- Sessions last about 4 hours; the runner waits and resumes after a new login.

---

## 8. Open work, roughly in priority order

1. **Tag test:** check whether tagging an already-submitted regular alpha as `PowerPoolSelected` is
   accepted and whether it uses a daily slot (could add strong Power Pool + Regular alphas without
   using the quota). Prepared, not run.
2. **Measure the explore revise loop:** does Sharpe improve over rounds? Tune `FLIP_AT` / `DROP_BELOW`
   and the prompt in `revise.py`.
3. **Pre-simulation filtering:** learn which families fold into an account's existing Power Pool
   and stop spending simulations on them (`lanes.collide_stems` is a first version).
4. **Theme lane for after week 6:** build a stock of theme-matching alphas before quotas require it
   (early December for both accounts).
5. **Unit tests** for `verdict.py`, `select.py` and `picks.py`, using saved `/check` JSON.
6. **Dashboard:** filter by account, show Lane C near misses with their tried settings.
7. Self-hosting Qwen under our own Linux user, so we don't depend on another lab member's process.

---

## 9. Current state (24 Oct 2026)

- Accounts: **HT52122** and **YO50689**, each in week 1 of Power Pool. Both have Power Pool alphas
  submitted; see the dashboard's pick list for which.
- The miner and its database live on the 3080; the Telegram broker runs on the Mac.
- No submissions should be made until the team confirms who is the single submitter.

Questions: ask your supervisor, who has the history of this project.
