# AClaw Power Pool miner

Harvests Power Pool candidates from our past research, checks them against the
Power Pool rules locally, picks a diverse shortlist per weekly theme, and uses
Qwen3.8-27B (A6000) to write descriptions and rewrite mixed-category signals.

## Layout

```
ppminer/
  expr.py       FASTEXPR parser: operator count, data fields, canonical form, skeleton
  catalog.py    field -> category/dataset/type from data/catalog/brain_meta*.sqlite
  pp.py         local Power Pool checks (<=8 ops, <=3 fields, single category)
  theme.py      parse theme rules, e.g. "delay=1 & datacategory in [...] & single datacategory"
  harvest.py    merge Alpha Studio + agent memory into data/pp.db, dedupe, classify, rank families
  select.py     diverse shortlist for a theme (one per family, capped per dataset/shape)
  llm.py        Qwen3.8 client (vLLM, JSON schema, thinking off by default)
  describe.py   Idea / Rationale description (>=100 chars) for each shortlisted alpha
  purify.py     single-category, <=8-operator rewrites of strong mixed-category alphas
  brain.py      BRAIN API: login with face-scan hand-off, simulate, poll, check
themes.json     theme calendar (add each month's themes here)
scripts/export_agent_memory.py   run on the 3080 to export alpha_memory*.db
```

## Candidate status

| status | meaning |
|---|---|
| `pp_ready` | parses, <=8 ops (incl. sign flip), <=3 fields, single category, \|sharpe\|>=1, turnover 1-70%, sharpe < 5 |
| `purify` | \|sharpe\|>=1 but mixed categories / too complex / flagged; input for Qwen rewrites |
| `rewrite` | valid Qwen rewrite, not yet simulated |
| `weak` | \|sharpe\| < 1 |
| `reject` | parse error or fields not in the catalog |

Sharpe values are from the original simulations (older settings/periods); every
candidate must be re-simulated before submission.

## Where it runs (since 2026-10-24)

| Part | Where |
|---|---|
| Dashboard, re-sim / explore / rewrite jobs, `data/pp.db`, session cookies | **3080**, `/data/aclaw_powerpool` (Python: `/data/quant-agent/envs/orchestrator/bin/python`) |
| Qwen3.8 | A6000; the 3080 reaches it through a tunnel on 127.0.0.1:18060 |
| Telegram login broker | **Workstation** (since 2026-10-26; `~/aclaw_powerpool`, started by `scripts/start_ws_broker.sh`). Telegram is blocked from the cluster and the workstation's own network, so the workstation routes through a Tailscale exit node. Each new cookie is copied to the 3080 (`PP_COOKIE_SYNC`) with its own restricted SSH key. |

- Broker log: `~/aclaw_powerpool/data/logs/telegram.log` on the workstation. It runs in a restart loop;
  after a workstation reboot start it with `scripts/start_ws_broker.sh` (no @reboot entry is installed).
- The workstation's internet goes through the exit node, so the broker needs that node online.
- Run **only one broker**: two brokers polling the same bot interfere. `scripts/start_mac_broker.sh`
  is kept only as a fallback if the workstation is down; stop the workstation broker first.

- 3080 services run in tmux session `ppminer` (windows `qwen-tunnel`, `web`), started by
  `scripts/start_3080.sh`; each restarts itself if it exits. Attach: `ssh qa-3080 -t tmux attach -t ppminer`.
- Dashboard: **https://alpha.hokin.cloud** (VPS nginx with HTTPS + password, reached through the 3080's
  `tunnel` window; credentials in `/etc/nginx/.htpasswd-alpha` on the VPS, ask the owner).
  From the Mac without the VPS: `ssh -N -L 5070:127.0.0.1:5070 qa-3080`, then http://127.0.0.1:5070
  (or the `ppminer-dashboard` entry in `.claude/launch.json`).
- Run jobs on the 3080, e.g.
  `ssh qa-3080 'cd /data/aclaw_powerpool && nohup /data/quant-agent/envs/orchestrator/bin/python -m ppminer resim --lane A --account YO50689 --top 15 > data/logs/resim.log 2>&1 &'`
- One job per account at a time: several writers on one `pp.db` lock each other out.
- The Mac copy `data/pp.db.moved-to-3080` is the pre-move snapshot; don't run jobs against it.

### Stand-in submitter on the workstation (temporary, while the cluster is down)

When the 3080 can't be reached, the lab workstation (`amaz@100.119.181.15` over Tailscale,
`~/aclaw_powerpool`) can run the same scheduler without Qwen:
- `scripts/start_ws_standby.sh` refuses to start while the 3080's SSH port answers. It imports the
  strongest unsubmitted alphas of each account from BRAIN (`python -m ppminer import-brain`, at most
  40 each), then starts `ppminer daily` and a guard. Its own `data/pp.db` starts empty; the 3080's is untouched.
- Log in with `/login` in Telegram as usual: the broker saves the cookie on the workstation.
- **Switching back:** the guard checks every 5 minutes and runs `scripts/stop_ws_standby.sh` as soon
  as the 3080 answers (it starts ppminer at boot). You can also run that script by hand. Then log in
  again from Telegram so the broker copies a fresh cookie to the 3080. What the stand-in submitted is
  on BRAIN, and the 3080 reads it back before its own next submission (quota, correlation).

### Osmosis scopes (temporary steering)

Osmosis counts a scope (region + delay) only once an account has 10 submitted alphas in it. To fill
the nearest scopes before a cutoff, per account in `data/autosubmit.json`:
- `"scope_targets": {"scopes": ["EUR/1", "USA/0"], "until": "2026-11-22"}`: auto-submit tries picks in
  those scopes first (most needed first), until that New York date.
- `"regions": ["EUR", "GLB"]`: that account mines only there (the theme's regions stay in).
- `python -m ppminer import-brain --account HT52122 --scope EUR/1 --pp-bar --regular`: pulls the
  account's own unsubmitted alphas of a scope in as candidates, and queues those that pass the regular
  bar but aren't Power Pool shapes for the regular lane.

### One a day, least correlated (since 20 Nov)

With a low value factor, extra submissions add little and low production correlation is what helps.
`accounts.<id>.daily_total = 1` makes autosubmit submit at most one regular-or-Power-Pool alpha a day:
both lanes only collect the candidates that pass everything (fresh check included), the one with the
lowest production correlation is submitted, and the rest stay queued for later days
(`autosubmit.daily_one`). The super alpha keeps its own one a day.

Checks are done in two sweeps (21 Nov): every candidate's BRAIN check gets 45 s, then the ones BRAIN
hadn't finished get 150 s more at the end of the pass, within 20 minutes in all. Before, each pending
check held the pass for 3 minutes in turn, and on 20 Nov HT52122's session expired before anything
was submitted.

### Super alphas (HT52122)

`ppminer/superalpha.py`: one super alpha a day (BRAIN's SUPER_SUBMISSION limit), combining the
account's own submitted alphas. It runs inside autosubmit after Power Pool and regular, with
`accounts.<id>.super = {"enabled": true, "dry_run": true}`. It simulates a small grid of selection
(low self-correlation, turnover cap) x combo (`combo_a`) variants in five region/delay scopes,
writes both descriptions from the expressions, and submits the best one whose fresh check passes,
correlation included. Grid variants not simulated in the last 5 days are simulated each pass, whether
or not BRAIN has answered for the older ones; a variant whose self or production correlation comes
back at 0.7 or more is dropped. Checks and correlation requests wait 60 s each; when /check is still
running but both correlations are under 0.7, it tries the submission (BRAIN refuses it if a check fails).
`python -m ppminer super sim --account HT52122` simulates the grid by hand; `super list` shows them.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install requests pytest
ssh -f -N -L 18060:127.0.0.1:8060 qa-a6000      # Qwen3.8 tunnel
```

## Daily use

```bash
.venv/bin/python -m ppminer harvest
.venv/bin/python -m ppminer shortlist -n 40
.venv/bin/python -m ppminer describe --top 20
.venv/bin/python -m ppminer purify --limit 50
.venv/bin/python -m ppminer resim --account acc1 --top 10
```

## Logging in (cookie flow, same as the old Alpha Studio)

1. In Chrome, log in to platform.worldquantbrain.com and do the face scan.
   Use one Chrome profile per BRAIN account so all three stay logged in.
2. `.venv/bin/python -m ppminer cookie grab` reads each profile's session
   (macOS asks once for access to "Chrome Safe Storage"), checks which BRAIN
   user it is, and saves `data/secrets/<name>.cookie` (mode 600).
   Name accounts with `data/secrets/accounts.json`: `{"acc1": "<BRAIN user id>"}`;
   otherwise files are named by user id.
3. `.venv/bin/python -m ppminer login-status` shows which sessions are live.

Other options: `cookie set --account acc1` (paste a cookie, input hidden) and
`cookie import-old` (reuse quant/brain_cookies*.txt if still live).
## Logging in with Telegram (face-scan link on your phone)

The broker does BRAIN's own login: it posts the account's email/password, and
when BRAIN asks for a face scan it sends the scan link to the **account
owner's** Telegram as a "Verify face" button. After the owner scans, the session
is saved to `data/secrets/<account>.cookie` like the Chrome flow. The scan is
always done by the account's owner; the bot only delivers the link.

1. Create a bot with @BotFather and copy its token.
2. Each account owner opens the bot and sends `/id`; the bot replies with their chat id.
3. `cp telegram.example.json data/secrets/telegram.json && chmod 600 data/secrets/telegram.json`,
   then fill in `bot_token` and, per account, `chat_id`, `email`, `password`
   (each owner enters their own). The broker refuses the file unless it is mode 600.
4. `.venv/bin/python -m ppminer telegram check` sends a test message to every owner.
5. `.venv/bin/python -m ppminer telegram run` keeps running: it renews sessions
   `renew_before_minutes` before they expire (not during `quiet_hours`), and answers
   `/status` and `/login [account]` from the owners' chats. Other chats are ignored.

One-off: `telegram login --account HT52122`, or the "Send Telegram login" button in the dashboard.

Daily report (`ppminer/report.py`, since 23 Nov): just after BRAIN's day ends (00:05 New York, 13:05 CST in
winter) the broker sends each account's submissions for that day (lane, scope, Sharpe, production
correlation), its simulations (done, errors, best Sharpe), what's queued, new super variants, and in how many
hours it simulated. It goes to `"report": {"chat_ids": [...]}` in telegram.json, by default the
`admin_chat_ids`; for a channel, add the bot to it as an admin and put the channel's id (-100…) there.
`/report` sends it on demand (admins only), `/report today` for the day so far. It reads `data/pp.db` on the
broker's machine, which is the submitter's while the workstation submits.

Control panel (`ppminer/control.py`): owners steer their own account from the bot, admins any account.
`/status`, `/queue`, `/pause [h]`, `/resume`, `/regions EUR GLB|all`, `/focus EUR/1 GLB/1 [days]|off`, `/pass`
(run the submit pass now). They change only what the `daily` scheduler already re-reads every 15 s
(`data/autosubmit.json` regions / scope_targets, runner_state pause and last-pass keys), so no restart is
needed; the scheduler stays the only submitter, and submission counts, dry run and the quality bar are not
changeable from Telegram. Admins get a copy of every change an owner makes.

`/advise [account]` (`ppminer/advise.py`): a checklist built from the data (session, Power Pool phase and
theme, Osmosis and super-alpha scopes from BRAIN's list of submitted alphas, queue and simulations per
scope/region), each item with the command that acts on it, then Qwen3.8's top 1-3 priorities. Qwen only
ranks and explains the checklist; lines naming a command that isn't in it are dropped. The workstation
reaches Qwen through `~/.ssh/id_qwen_tunnel`, a key the 3080 allows only to forward to 127.0.0.1:18060
(started by `scripts/start_ws_broker.sh`, log `data/logs/qwen_tunnel.log`); the miner's Qwen ideas use it too.

Notices (sent by `telegram run`, never during `quiet_hours`; held back until they end):

| When | Owner gets | Admins (`admin_chat_ids`) get |
|---|---|---|
| session expired / missing | 🔴 logged out (once per expiry) + face-scan link | 🔴 copy, no link |
| scan link not used | ⏳ reminder halfway, ⌛ expired; new link after `retry_after_minutes` | — |
| login succeeded | ✅ with hours left | ✅ copy |

Set `"auto_prompt": false` to get only the 🔴 notice; the owner then sends `/login` when ready.
Several accounts work the same way: one entry per account, each with its owner's `chat_id`.
An entry with `"enabled": false` is ignored, so a half-filled placeholder doesn't block the others.
