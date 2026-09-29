# Tie-break study (R5) — research simulator

Research scripts, not production code. `src/` does not import them, they are excluded
from ruff (`pyproject.toml` `extend-exclude`), and coverage measures only `src/`.
They are committed so that the pre-registered prospective test
(`docs/preregistration/2026-09-tiebreak-r5.md`) can be re-run by anyone.

| file | role |
|---|---|
| `panel.py` | grouped-cache SQLite copy → `panel.npz` (dates × symbols open/close/volume) |
| `core.py` | loads the panel (`$TIEBREAK_PANEL`, default `./panel.npz`), split adjustment, simple RSI-2, SMA200, dollar volume |
| `tiebreak.py` | the tie-break study (R0 random × N seeds vs R1–R5) and the prospective `--window` evaluation |
| `bt.py` | all-signals backtest + live-rules portfolio sim (paper-vs-backtest gap study) |

The live rule (`src/mr_live.py`) implements R5: candidates sort by simple RSI-2 ascending,
then by the 20-session average dollar volume of D-1..D-20 (today excluded) descending, then
by symbol. In the simulator that is `ADV[t] = sum(nan_to_num(DV[t-20..t-1])) / 20`. A session
where the symbol has no bar counts as 0, and a missing ADV sorts last.

## 1. Rebuild the panel

Never open the live cache in place. Copy it first, outside the 04:30 KST trade run that
writes to it.

```bash
WORK=$(mktemp -d)      # or any scratch dir; do not commit the outputs
cp data/grouped_cache.db "$WORK/grouped_cache.db"
cd research/tiebreak
uv run --no-project --with numpy python panel.py \
    --grouped-db "$WORK/grouped_cache.db" --out "$WORK/panel.npz"
```

numpy is not a project dependency, so `--with numpy` supplies it only for these scripts.
Outputs (`*.npz`, `*.json`, `*.db`) are gitignored.

## 2. Reproduce the study (train / test / holdout / paper buckets)

```bash
uv run --no-project --with numpy python tiebreak.py \
    --panel "$WORK/panel.npz" --seeds 300 --out "$WORK/tiebreak_out.json"
```

Buckets are computed from the panel: sessions from the first SMA200-valid day are split
60/20/20 into train/test/holdout. "paper" runs from 2026-07-01 to the end of the panel. Cost
is 10 bp per leg. On the 2026-09-29 cache (last session 2026-09-25), 300 seeds reproduce
the original study output exactly (JSON equal), which gives the numbers in the
pre-registration: train 2025-04-25..2026-03-03, test 2026-03-04..2026-06-15, holdout
2026-06-16..2026-09-25, paper 2026-07-01..2026-09-25.

## 3. Prospective evaluation (pre-registered)

After the window closes, rebuild the panel from a fresh cache copy (step 1). Then:

```bash
uv run --no-project --with numpy python tiebreak.py \
    --panel "$WORK/panel.npz" --window 2026-09-30 2026-12-31 \
    --seeds 300 --cost 0.001 --live-return <paper account return over the window>
```

This prints the R0 distribution (p5/p25/median/p75/p95), the R5 return with its percentile
rank, and, when `--live-return` is given, the live percentile rank plus the PASS/FAIL
verdict. PASS requires both the live return and R5 to be at least the R0 p75.

## Known simulator ↔ live differences

- The simulator fills at the session close. Live fills at the ~15:30 ET snapshot price
  through limit orders.
- The simulator uses split-adjusted closes for RSI/SMA. Live uses the grouped cache
  (`adjusted=true`) plus the Alpaca snapshot.
- The simulator starts at $1,000,000 and live at the paper account's equity. Returns are
  compared, not dollars; only integer-share rounding differs.
