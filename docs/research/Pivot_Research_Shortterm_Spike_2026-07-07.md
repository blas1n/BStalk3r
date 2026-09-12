# BStalk3r Pivot Research — Short-term Spike/Day-trade Edges (2026-07-07)

Scope: **strictly short-term day-trading, spike/급등 focused.** Long-term &
blue-chip/S&P portfolios are handled separately and are OUT of scope here.

## The one-line finding
Our own failure was predicted by the literature: **naive momentum-chasing of
intraday spikes loses (−4.2% net).** Every documented edge in this space is on
the **REVERSAL / short / fade** side, not the chase. We were on the wrong side.

## Documented anomalies (all consistent with our data)

1. **Short-term reversal** — Jegadeesh (1990), Lehmann (1990). Prior-week winners
   → next-week losers (−0.35…−0.55%/wk); losers → winners (+0.86…+1.24%/wk);
   ~2%/month contrarian edge. Cause: overreaction + liquidity/price-pressure.
   → Our intraday crossers (+5% → −4.2%) are the intraday version of this.
2. **MAX / lottery effect** — Bali, Cakici, Whitelaw (2011, JFE). Highest
   single-day-return (lottery-like) stocks UNDERPERFORM >1%/month. Recent work:
   it's overreaction, and **concentrated in stocks with poor recent past
   returns** (a usable conditioning variable). → "the big spikers underperform."
3. **Market intraday momentum** — Gao, Han, Li, Zhou (2018, JFE). First half-hour
   return predicts last half-hour (S&P ETF, R²≈1.6%), stronger on high-vol,
   high-volume, news days. → a real intraday timing signal (market-level).
4. **Overnight vs intraday** — overnight edge is a large-cap phenomenon, **weak/
   absent in small caps**; "intraday trading in small-cap growth destroyed
   value"; and **trading costs wipe out the overnight anomaly**. Short-horizon
   (45–60 min) small-cap anomalies do persist. → confirms small-cap intraday is
   a cost-dominated, reversal-prone regime.

## Practitioner consensus (short side)
5. **Pump-and-dump / dilution** — manipulation concentrates in low-float (<10M
   shares), nano-cap (<$50M). "Parabolic moves have a high propensity to reverse
   quickly." The documented practitioner edge is **short-side**.
6. **First Red Day / Parabolic Short** — Alex Temiz, Kyle Williams, Kris Verma
   (well-known small-cap short traders): short a stock that ran up parabolically
   for 3+ days on the **first day it closes red / breaks below the prior close**;
   stop above that level; cover into the flush. Rules-based, widely traded.

## What this means → testable hypotheses (all FREE with our infra)
Our pipeline (grouped crossers + minute bars + sim + survivorship/feature checks)
tests any of these in days, no paid data:

- **H-A · Fade/short the crosser** (direct inverse of our −4.2% long). Short at
  the +X% cross with a stop; measure net INCLUDING the squeeze tail (the 35% that
  run +50–270% are the killer). Question: does a stop-managed short beat costs +
  borrow, and is the tail survivable?
- **H-B · First-red-day / multi-day exhaustion** (practitioner #1). Use multi-
  session grouped data to find 3+ day parabolic runners, short on the first red
  day / prior-close break. Likely the highest-quality short setup.
- **H-C · MAX-extreme cross-sectional short**, conditioned on poor recent returns
  (Bali + the recent refinement). Grouped-daily only.
- **H-D · Intraday-momentum timing** overlay (Gao et al.) — first-30m → decision.

## Honest caveats (short side)
- **Borrow/locate + squeeze tail** is the real risk. Low-float shorts have
  unbounded loss; hard-to-borrow fees; forced buy-ins. Our data can MEASURE the
  tail (how often/how far crossers run) to size stops, but paper fills (Alpaca)
  won't simulate borrow availability or squeeze slippage — that's a real-money
  frontier even if the backtest is positive.
- Costs dominate small-cap intraday; keep the 2%+cheap round-trip model on.

## Recommended next build
**H-A first** (fade/short crosser backtest): it's the direct inverse of the −4.2%
result we already have, reuses the crosser+minute infra, and immediately tells us
whether the reversal edge is real net of costs+stops and whether the tail is
survivable. If yes → refine toward H-B (first-red-day, higher quality). If the
tail eats it → the space may be short-in-theory / untradeable-in-practice.

## Sources
- Short-term reversal: Jegadeesh 1990; Lehmann 1990. Overview:
  https://alphaarchitect.com/quantitative-momentum-research-short-term-return-reversal/ ·
  https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr513.pdf
- MAX effect: Bali, Cakici, Whitelaw (2011) https://pages.stern.nyu.edu/~rwhitela/papers/max%20jfe11.pdf ·
  overreaction refinement https://www.nowpublishers.com/article/Details/CFR-0123
- Intraday momentum: Gao, Han, Li, Zhou https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866
- Overnight anomaly / costs: https://alphaarchitect.com/trading-costs-wipe-out-the-overnight-return-anomaly/ ·
  https://www.sciencedirect.com/science/article/abs/pii/S1544612325018926
- Pump/dump, dilution, squeezes: https://knowledge.dilutiontracker.com/en/articles/5611407-characteristics-of-mega-squeezes-and-how-to-anticipate-them ·
  https://www.forbes.com/councils/forbesfinancecouncil/2026/07/06/deep-dive-short-selling-recognizing-dilution-the-fundamentals-of-uncovering-a-pump-and-dump-scheme/
- First Red Day / Parabolic Short: https://www.tradezella.com/strategies/first-red-day ·
  https://www.tradezella.com/strategies/parabolic-short-strategy
