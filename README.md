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

Notices (sent by `telegram run`, never during `quiet_hours`; held back until they end):

| When | Owner gets | Admins (`admin_chat_ids`) get |
|---|---|---|
| session expired / missing | 🔴 logged out (once per expiry) + face-scan link | 🔴 copy, no link |
| scan link not used | ⏳ reminder halfway, ⌛ expired; new link after `retry_after_minutes` | — |
| login succeeded | ✅ with hours left | ✅ copy |

Set `"auto_prompt": false` to get only the 🔴 notice; the owner then sends `/login` when ready.
Several accounts work the same way: one entry per account, each with its owner's `chat_id`.
An entry with `"enabled": false` is ignored, so a half-filled placeholder doesn't block the others.
