# Song's sandbox

A full copy of the miner for you to change freely. Nothing you do here can break the
live system or submit an alpha.

| | Live system (don't touch) | Your sandbox |
|---|---|---|
| Code | `/data/aclaw_powerpool`, branch `main` | `/data/aclaw_song`, branch `song` |
| Database | `/data/aclaw_powerpool/data/pp.db` | `/data/aclaw_song/data/pp.db` (a copy) |
| tmux session | `ppminer` | `song` |
| Web page | https://alpha.hokin.cloud/ | https://alpha.hokin.cloud/song/ (same password) |
| Submits | yes, the daily scheduler, once a day | **never** (`PP_SANDBOX=1` blocks `Brain.submit`) |

Shared with the live system: the BRAIN account **HT52122** (your simulations run on it
alongside the live ones), its session cookies (`data/secrets` is a link to the live
folder) and Qwen3.8 on `127.0.0.1:18060`. Read `docs/HANDOFF.md` first for how the
pieces fit together.

## Daily use (on the 3080, as user `alpha`)

```bash
cd /data/aclaw_song
bash scripts/sandbox.sh start          # web page only
bash scripts/sandbox.sh start daily    # web page + your own mining loop (simulates on BRAIN)
bash scripts/sandbox.sh stop           # after a code change: stop, then start again
bash scripts/sandbox.sh refresh-db     # (stopped) start over from a fresh copy of the live database
tmux attach -t song                    # watch it; detach with Ctrl-b d
tail -f data/logs/daily.log
/data/quant-agent/envs/orchestrator/bin/python -m pytest -q tests
```

You can also run single commands against your copy, e.g.
`/data/quant-agent/envs/orchestrator/bin/python -m ppminer lanes --account HT52122 -n 12`.

## Getting your work into the live system

1. Commit on branch `song` in `/data/aclaw_song` and `git push origin song`.
2. Open a pull request `song` → `main` on GitHub (techcardiff/AClaw_powerpool) and explain
   what improved, with numbers from your sandbox (pass rates, Sharpe, correlation).
3. Prof. Hokin reviews and merges; only then is it deployed to the live system.

Never push to `main`, edit files in `/data/aclaw_powerpool`, or kill windows in tmux
session `ppminer`.

## Good places to start

- `ppminer/templates.py`: the Search 1/2/3 templates; new operators, windows, groups.
- `ppminer/funnel.py`: which settings variants a promising alpha gets next.
- `ppminer/verdict.py`: the quality bar for Power Pool, regular and ATOM.
- `ppminer/select.py`, `ppminer/lanes.py`: which candidates get simulated first.
- `ppminer/search.html`: the Alpha Search page (Fields tab, Editor, Templates).
