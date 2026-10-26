# Handoff: AClaw Power Pool miner (`ppminer`)

For a student taking over development. Read this first, then `README.md` for
day-to-day commands. Written 24 Oct 2026, updated 26 Oct 2026.

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
 SOURCES OF IDEAS                                   (all land in pp.db: candidates)
   past research        harvest.py   old agent DB + Alpha Studio DB, deduped, checked locally
   Qwen rewrites        purify.py    strong mixed-category alphas rewritten into one category
   Qwen explore         explore.py   new ideas on datasets we never mined (USA, EUR, ASI)
   Qwen revisions       revise.py    explore ideas revised after seeing their results
   templates            templates.py Alpha Studio Search 1/2/3 on any synced region
   manual               Search page  an expression typed by a person (lane M)
        │
        ▼
 SCHEDULER (scheduler.py, `ppminer daily`, 3080)  one cycle per account, all day:
   field sync           a few datasets per region per pass (fields.sync_step)
   batch                lane M (manual) first, then Lane A (Power Pool inventory, fresh ideas
                        round-robin across regions), then Lane C (regular/ATOM: funnel.py fixes
                        the failing check of the best near miss, plus regular-grade harvest)
   simulate             runner.run_jobs on BRAIN with the account's session (resims table)
   verdicts             verdict.py: Power Pool / regular / ATOM, from BRAIN's own checks
   pick list            picks.py: daily PnL, fold anything correlating >= 0.5, one per cluster
        │
        ▼
 AUTO-SUBMIT (autosubmit.py, once the New York day starts, retried every 3 h)
   quota (quota.py) -> fresh BRAIN check -> quality bar -> verified description (descheck.py)
   -> submit -> confirm ACTIVE + PowerPoolSelected -> autosubmit_log
```

**Only the scheduler submits.** It respects each account's Power Pool quota calendar, requires
Power Pool correlation PASS, applies the quality bar in `data/autosubmit.json` (Sharpe >= 1.5,
fitness >= 1.0, turnover <= 0.4, last-2y Sharpe >= 1.0, sub-universe >= 0.7) and leaves slots
empty rather than submit weak alphas. Dry run is per account in the same file.

Qwen3.8-27B (on the A6000) does all the language work: rewrites, explore ideas, revisions,
descriptions, and the second-pass audit of every description before it goes on BRAIN.

---

## 3. Where it runs (the structure)

```
                         you / the student (browser)
                                   │ https + password
                                   ▼
  ┌──────────── VPS (veghere-vps) ────────────┐
  │ nginx: alpha.hokin.cloud                  │   HTTPS (certbot, renews itself)
  │   basic auth (/etc/nginx/.htpasswd-alpha) │   other sites on this VPS: eazyteach, mailcow, ...
  │   proxy -> 127.0.0.1:15070                │   (don't touch them)
  └───────────────────▲───────────────────────┘
                      │ reverse SSH tunnel (key may only listen on 127.0.0.1:15070)
  ┌───────────────────┴──── 3080 (qa-3080), /data/aclaw_powerpool ─────────────────┐
  │ tmux session "ppminer", started by scripts/start_3080.sh:                      │
  │   qwen-tunnel   127.0.0.1:18060 -> A6000 Qwen3.8                               │
  │   web           dashboard + Search page on 127.0.0.1:5070                      │
  │   daily         the scheduler (mining + the only submitter)                    │
  │   tunnel        reverse tunnel to the VPS (the public website)                 │
  │ data/pp.db (the live database), data/secrets/*.cookie, data/autosubmit.json    │
  └───────▲──────────────────────────────────────────────▲─────────────────────────┘
          │ ssh tunnel                                    │ fresh session cookies (restricted key)
  ┌───────┴──────── A6000 (qa-a6000) ──┐      ┌──────────┴──── lab workstation ──────────────┐
  │ Qwen3.8-27B vLLM, port 8060        │      │ Telegram login broker (start_ws_broker.sh)    │
  │ (process owned by another lab user)│      │ internet via a Tailscale exit node            │
  └────────────────────────────────────┘      └──────────────▲────────────────────────────────┘
                                                             │ face-scan link
                                                       account owners' phones (Telegram)
```

| Part | Where | Notes |
|---|---|---|
| Website | **https://alpha.hokin.cloud** | nginx on the VPS, HTTPS + password; ask the supervisor for the login. Everyone with the password can queue simulations and approve picks, so don't share it further. |
| Miner, scheduler, dashboard, `data/pp.db`, cookies | **3080**, `/data/aclaw_powerpool`, Linux user `alpha` | tmux `ppminer` (windows above). `scripts/start_3080.sh` is safe to re-run and re-adds any missing window. Restart one service: `tmux kill-window -t ppminer:<name>`, then the script; never `pkill -f`. **No @reboot entry is installed**: after a 3080 restart, run the script by hand. Logs: `data/logs/{daily,web,web_tunnel,qwen_tunnel}.log`. |
| Qwen3.8-27B (vLLM) | **A6000**, port 8060 | Owned by **another lab user**; if it stops, Qwen steps fail until it's back. Simulations keep working. |
| Telegram login broker | **Lab workstation** (`~/aclaw_powerpool`, `scripts/start_ws_broker.sh`), since 26 Oct | Telegram is blocked on the cluster and the workstation's network, so the workstation goes out through a **Tailscale exit node**. Logs every message it sends (`data/logs/telegram.log` there). After a reboot, start it by hand. **Run only one broker** (`start_mac_broker.sh` is a fallback). |
| Code | GitHub `techcardiff/AClaw_powerpool` | `data/` and `ref/` are git-ignored on purpose |

**Daily checks (5 minutes):** open the website; the tiles show the scheduler status and session
state; the **auto-submit log** shows what was submitted or skipped and why; the **Search** page's
Funnel tab shows where ideas drop out and which BRAIN checks block regular/ATOM alphas.

**Sessions:** BRAIN sessions last about 4 hours. The broker sends each owner a face-scan link
before expiry; while a session is expired, that account's mining and submissions wait.

SSH host aliases (`qa-3080`, `qa-a6000`, `veghere-vps`) come from the supervisor's `~/.ssh/config`;
ask for your own key rather than sharing one.

---

## 4. Code map

All code is in `ppminer/`. `python -m ppminer --help` lists the commands.

| Module | What it does | Edit here when… |
|---|---|---|
| `expr.py` | FASTEXPR parser: operator count, data fields, canonical form, skeleton, **lint** (vector fields need `vec_*`, no scientific notation, no `ts_corr(x, x)`, no quoted groups) | BRAIN rejects an expression our checks let through |
| `pp.py` | Local Power Pool checks (`check()`), `submit_expr()` (sign flip for negative Sharpe) | the Power Pool rules change |
| `catalog.py` | Field → category / dataset / type, from catalog snapshots plus per-account synced fields | |
| `fields.py` | which datasets/fields an account can use per region; `sync_step()` runs inside the scheduler (resumable, never deletes); block list of fields BRAIN rejected | accounts see different data (e.g. no `model77`) |
| `harvest.py` | Import past simulations, dedupe, classify (`pp_ready`, `pp_multi`, `purify`, `weak`, `reject`) | adding a new source of old alphas |
| `select.py` | `shortlist()`: diverse selection (one per family, caps per dataset and per operator shape, skips known collisions) | selection is too narrow or too repetitive |
| `lanes.py` | Lane A / Lane C queues, collision families, other-account exclusion, sync of an account's submitted alphas | changing what each lane works on |
| `funnel.py` | Lane C tiered funnel: fixes the failing regular check by expression (size buckets / liquid names for sub-universe Sharpe, `rank()` for concentration, smoothing for turnover); stops on self-correlation | regular/ATOM strategy |
| `templates.py` | Alpha Studio Search 1 (`ts_op`, `group_rank`), 2 (same-dataset A/B, `ts_corr`), 3 (`trade_when` conditions) for any region | adding templates |
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
| `quota.py` | Power Pool quota calendar per account, in New York days | BRAIN changes the quota rules |
| `descheck.py` | Description verifier: window/field rules + Qwen audit; one rewrite, else skip | descriptions get through that shouldn't |
| `autosubmit.py` | **The only submitter**: quota, fresh check, `min_sharpe`, verified description, submit, log | submission policy |
| `scheduler.py` | `ppminer daily`: auto-submit after midnight New York, mining cycles for every account in between | scheduling |
| `markets.py` | Per-region universe and neutralization (EUR, ASI, …) | adding a market |
| `web.py`, `web.html` | Dashboard: tiles, accounts/login, pick list, auto-submit log, Lane C table, re-simulations | UI work |
| `search.py`, `search.html` | **Search page** (`/search`): Funnel, Datasets map, Editor (check + queue a simulation, lane M), Templates (preview + queue) | search UI |
| `db.py` | Main schema; `lanes.migrate()` adds the newer columns/tables | schema changes |
| `__main__.py` | CLI | adding a command |

### Database (`data/pp.db`, SQLite)

| Table | Content |
|---|---|
| `candidates` | every alpha idea (status `pp_ready`, `pp_multi`, `purify`, `rewrite`, `explore`, `template`, `funnel`, `manual`, …), local checks, family |
| `resims` | each simulation on BRAIN: account, expression, settings, results, raw checks JSON, lane (`A`, `C`, `M`), purpose |
| `picks` | pick list per account, status `proposed` / `approved` / `rejected` / `submitted` |
| `descriptions`, `rewrites`, `llm_jobs` | Qwen outputs |
| `pnl` | cached daily PnL per alpha |
| `account_fields`, `account_datasets`, `field_blocklist` | per-account data access |
| `account_submitted` | each account's submitted alphas (for collision checks) |
| `runner_state` | what the scheduler is doing (shown on the dashboard) |
| `autosubmit_log` | every auto-submit decision: quota, skip (with reason), dry run, submitted |
| `field_sync_progress` | which datasets of which region are synced for each account |

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
2. **Never submit or tag alphas yourself.** Only the scheduler submits; change its behaviour in code,
   reviewed, not by hand. Submissions are permanent and stay in the account's correlation pool even if
   untagged later. Switching an account from dry run to real submissions is the account owner's decision.
3. **One submitter at a time.** On 24 Oct two sessions submitted to the same account within two
   minutes of each other, each unaware of the other, and it took time to work out who submitted
   what. That is why the scheduler is now the single submitter (with a file lock).
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
- **Regular alphas here are blocked by two checks:** `LOW_SUB_UNIVERSE_SHARPE` (the signal earns in
  small, illiquid names: switching to TOP1000 cut Sharpe from ~2.4 to ~1.2) and `SELF_CORRELATION`
  against the account's many submitted alphas (HT52122 has 137). Settings sweeps don't fix either;
  `funnel.py` changes the expression for sub-universe and stops on self-correlation. New datasets and
  regions (EUR/ASI templates) are the better route.
- **Never tune a tuned result:** an early version re-tuned its own results and looped through
  neutralizations; 235 of 276 Lane C simulations were repeats of 41 alphas. The funnel now skips any
  expression + settings already tried.
- The **untested-idea slots** are shared round-robin across regions; before that, the big USA backlog
  starved EUR/ASI completely.
- Several processes write `pp.db` (scheduler, dashboard, syncs): connections wait up to 120 s for the
  lock. Long one-off jobs belong inside the scheduler (like the field sync), not beside it.

---

## 8. Open work, roughly in priority order

1. **Notifications:** relay the auto-submit log through the Telegram broker on the workstation.
2. **Tag test:** check whether tagging an already-submitted regular alpha as `PowerPoolSelected` is
   accepted and whether it uses a daily slot (could add strong Power Pool + Regular alphas without
   using the quota). Prepared, not run.
3. **Measure the explore revise loop:** does Sharpe improve over rounds? Tune `FLIP_AT` / `DROP_BELOW`
   and the prompt in `revise.py`.
4. **Pre-simulation filtering:** learn which families fold into an account's existing Power Pool
   and stop spending simulations on them (`lanes.collide_stems` is a first version).
5. **Theme lane for after week 6** (the scheduler already requires a theme match then, so it needs supply): build a stock of theme-matching alphas before quotas require it
   (early December for both accounts).
6. **Unit tests** for `verdict.py`, `select.py` and `picks.py`, using saved `/check` JSON.
7. **Dashboard:** filter by account, show Lane C near misses with their tried settings.
8. Self-hosting Qwen under our own Linux user, so we don't depend on another lab member's process.

---

## 9. Current state (26 Oct 2026)

- **Accounts:** HT52122 and YO50689, both in weeks 1-2 of Power Pool (first submissions 23 Oct, so
  4 per day until about 5 Nov, then 2, then 1; theme required from about 4 Dec). HT52122 has 7
  Power Pool alphas, YO50689 has 11. Weak early ones stay tagged on purpose (decision of 26 Oct).
- **The scheduler submits for real** (dry run off since 24 Oct) with the quality bar of 26 Oct, so
  expect a few submissions a week rather than 4 a day.
- **Regular / ATOM:** none passed yet; the funnel and EUR/ASI templates were deployed on 26 Oct.
- **Field sync:** USA complete; EUR and ASI syncing a few datasets per pass inside the scheduler.
- **Website:** https://alpha.hokin.cloud since 26 Oct (VPS nginx + password + reverse tunnel).
- **Broker:** on the lab workstation (moved from the Mac on 26 Oct).
- No @reboot entries anywhere: after a restart of the 3080 run `scripts/start_3080.sh`, after a
  restart of the workstation run `scripts/start_ws_broker.sh`.
- No notifications from the 3080 (Telegram is blocked there): check the website's auto-submit log.

Questions: ask your supervisor, who has the history of this project.
