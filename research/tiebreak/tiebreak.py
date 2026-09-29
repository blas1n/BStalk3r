"""Pre-registered tie-break study (measurement only). Entry/exit identical to bt.portfolio;
only the secondary sort key among eligible candidates changes (primary = simple RSI-2 asc).
Rules: R0 random (300 seeds) | R1 symbol alpha | R2 Wilder RSI2 asc | R3 3d decline (most neg first)
       | R4 dist above SMA200 (largest first) | R5 20d avg $vol of D-1..D-20 (largest first)."""
import argparse, json, os
import numpy as np

ap = argparse.ArgumentParser(description="R5 tie-break study / prospective evaluation")
ap.add_argument("--panel", default=None, help="panel.npz built by panel.py (default: ./panel.npz)")
ap.add_argument("--seeds", type=int, default=300, help="random tie-break (R0) seeds")
ap.add_argument("--out", default="tiebreak_out.json", help="study-mode JSON output path")
ap.add_argument("--window", nargs=2, metavar=("START", "END"),
                help="prospective mode: evaluate only this date window (YYYY-MM-DD, inclusive)")
ap.add_argument("--cost", type=float, default=0.001, help="cost per leg (0.001 = 10 bp)")
ap.add_argument("--live-return", type=float, default=None,
                help="prospective mode: live paper return over the window (0.05 = +5%%)")
ARGS = ap.parse_args() if __name__ == "__main__" else ap.parse_args([])
if ARGS.panel:
    os.environ["TIEBREAK_PANEL"] = ARGS.panel
from core import *  # noqa  (run from this directory: `cd research/tiebreak`)

ENT, EXT, HOLD_LIVE, SLOTS = 15.0, 70.0, 11, 20  # HOLD_LIVE=11 as in bt.portfolio (live max_hold 10)
PA = build(True)
C, Cf, R, M, DV = PA["C"], PA["Cf"], PA["R"], PA["M"], PA["DV"]


def wilder_rsi2(Cf):
    ch = np.diff(Cf, axis=0, prepend=np.nan)
    g = np.where(ch > 0, ch, 0.0); l = np.where(ch < 0, -ch, 0.0)
    ag = np.full_like(Cf, np.nan); al = ag.copy()
    pg = np.full(Cf.shape[1], np.nan); pl = pg.copy(); cnt = np.zeros(Cf.shape[1])
    for t in range(1, Cf.shape[0]):
        ok = ~np.isnan(ch[t])
        cnt[ok] += 1
        seed = ok & (cnt == 2)  # seed with simple avg of first 2 changes
        pg[seed] = (g[t - 1, seed] + g[t, seed]) / 2; pl[seed] = (l[t - 1, seed] + l[t, seed]) / 2
        up = ok & (cnt > 2)
        pg[up] = (pg[up] + g[t, up]) / 2; pl[up] = (pl[up] + l[t, up]) / 2  # alpha = 1/period = 1/2
        ag[t] = pg; al[t] = pl
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(al == 0, 100.0, 100 - 100 / (1 + ag / al))
    out[np.isnan(ag)] = np.nan
    return out


WR = wilder_rsi2(Cf)
D3 = np.full_like(Cf, np.nan); D3[3:] = Cf[3:] / Cf[:-3] - 1
DIST = Cf / M - 1
DVf = np.nan_to_num(DV)
cs = np.cumsum(DVf, axis=0); ADV = np.full_like(Cf, np.nan)
ADV[21:] = (cs[20:-1] - cs[:-21]) / 20  # mean DV over t-20..t-1 (known at decision time)
alpha_rank = np.empty(N); alpha_rank[np.argsort(np.array(SYMS))] = np.arange(N)
SEC = {  # secondary key, ascending = preferred first; nan -> last
    "R1": lambda t: alpha_rank, "R2": lambda t: WR[t], "R3": lambda t: D3[t],
    "R4": lambda t: -DIST[t], "R5": lambda t: -ADV[t],
}

ELIG = {}
for t in range(1, T):
    px = Cf[t]
    ELIG[t] = np.where((R[t] <= ENT) & (px > M[t]) & (px >= 5) & (px <= 1000) & (DV[t - 1] >= 10e6) & ~np.isnan(C[t]))[0]


def run(t0, t1, rule, seed=0, cost=0.001):
    rng = np.random.default_rng(seed)
    cash, pos, rets, eq = 1_000_000.0, {}, [], []
    for t in range(t0, t1 + 1):
        px = Cf[t]
        equity = cash + sum(q * px[k] for k, (q, e, ep) in pos.items())
        eq.append(equity)
        cash_pre = cash
        for k in [k for k, (q, e, ep) in pos.items() if R[t, k] >= EXT or (t - e) >= HOLD_LIVE]:
            q, e, ep = pos.pop(k)
            cash += q * px[k] * (1 - cost)
            rets.append(px[k] * (1 - cost) / ep - 1)
        free = SLOTS - len(pos)
        if free <= 0 or t == t1:
            continue
        cand = np.array([k for k in ELIG[t] if k not in pos], dtype=int)
        if len(cand) == 0:
            continue
        sec = rng.random(len(cand)) if rule == "R0" else np.nan_to_num(SEC[rule](t)[cand], nan=np.inf)
        order = cand[np.lexsort((sec, R[t, cand]))]
        notional = equity / SLOTS
        afford = int(max(0, cash_pre) // notional)
        n = 0
        for k in order[:free]:
            if n >= afford:
                break
            q = int(notional // px[k])
            if q < 1:
                continue
            fill = px[k] * (1 + cost)
            cash -= q * fill; pos[k] = (q, t, fill); n += 1
    rets += [Cf[t1, k] * (1 - cost) / ep - 1 for k, (q, e, ep) in pos.items()]  # open marked at end
    return np.array(eq), np.array(rets)


def metrics(eq, rets):
    d = eq[1:] / eq[:-1] - 1
    tot = eq[-1] / eq[0] - 1
    return dict(tot=tot, ann=(1 + tot) ** (252 / max(len(d), 1)) - 1,
                sh=d.mean() / d.std() * np.sqrt(252) if d.std() > 0 else 0.0,
                mdd=(eq / np.maximum.accumulate(eq) - 1).min(), n=len(rets),
                ptm=rets.mean() if len(rets) else np.nan, d=d)


def pct_rank(tots, x):
    """Percentile rank of x within the R0 distribution (ties count half) — as in the study."""
    return float((tots < x).mean() * 100 + (tots == x).mean() * 50)


def prospective(start, end, seeds, cost, live_return=None):
    """Pre-registered evaluation (docs/preregistration/2026-09-tiebreak-r5.md):
    R0 x seeds and R5 over [start, end] at `cost`/leg; optional live return."""
    idx = [i for i, d in enumerate(DATES) if start <= d <= end]
    if not idx:
        raise SystemExit(f"no panel sessions in {start}..{end}")
    x, y = idx[0], idx[-1]
    tots = np.array([metrics(*run(x, y, "R0", seed=s, cost=cost))["tot"] for s in range(seeds)])
    p75 = float(np.percentile(tots, 75))
    r5 = metrics(*run(x, y, "R5", cost=cost))["tot"]
    out = {"window": [DATES[x], DATES[y]], "sessions": y - x + 1, "seeds": seeds, "cost": cost,
           "R0": {q: float(np.percentile(tots, p)) for q, p in
                  (("p5", 5), ("p25", 25), ("med", 50), ("p75", 75), ("p95", 95))},
           "R5": {"tot": float(r5), "pct": pct_rank(tots, r5), "pass_b": bool(r5 >= p75)}}
    if live_return is not None:
        out["live"] = {"tot": live_return, "pct": pct_rank(tots, live_return),
                       "pass_a": bool(live_return >= p75)}
        out["verdict"] = "PASS" if out["live"]["pass_a"] and out["R5"]["pass_b"] else "FAIL"
    return out


t_first = int(np.argmax(np.isfinite(M).any(1))) + 1  # first session with SMA200 (+1 for DV[t-1])
n = T - t_first
a, b = t_first + int(round(0.6 * n)), t_first + int(round(0.8 * n))
BUCKETS = {"train": (t_first, a - 1), "test": (a, b - 1), "holdout": (b, T - 1)}
if "2026-07-01" in DIDX:
    BUCKETS["paper"] = (DIDX["2026-07-01"], T - 1)
if __name__ == "__main__" and ARGS.window:
    print(json.dumps(prospective(*ARGS.window, ARGS.seeds, ARGS.cost, ARGS.live_return), indent=1))
elif __name__ == "__main__":
    for k, (x, y) in BUCKETS.items():
        print(f"{k:8s} {DATES[x]}..{DATES[y]} sessions={y-x+1}")
    NS = ARGS.seeds
    res = {}
    for bk, (x, y) in BUCKETS.items():
        r0 = [metrics(*run(x, y, "R0", seed=s)) for s in range(NS)]
        res[(bk, "R0")] = r0
        for rl in SEC:
            res[(bk, rl)] = metrics(*run(x, y, rl))
        print(bk, "done", flush=True)
    out = {}
    for bk in BUCKETS:
        r0 = res[(bk, "R0")]; tots = np.array([m["tot"] for m in r0])
        out[bk] = {"R0": {q: {k: float(np.percentile([m[k] for m in r0], p)) for k in ("tot", "ann", "sh", "mdd", "n", "ptm")}
                          for q, p in (("p5", 5), ("med", 50), ("p95", 95))}}
        for rl in SEC:
            m = res[(bk, rl)]
            out[bk][rl] = {k: float(m[k]) for k in ("tot", "ann", "sh", "mdd", "n", "ptm")}
            out[bk][rl]["pct"] = float((tots < m["tot"]).mean() * 100 + (tots == m["tot"]).mean() * 50)
    # selection (train+test only)
    qual = [rl for rl in SEC if out["train"][rl]["tot"] > out["train"]["R0"]["med"]["tot"]
            and out["test"][rl]["tot"] > out["test"]["R0"]["med"]["tot"]]
    comb = {}
    for rl in qual:
        d = np.concatenate([res[("train", rl)]["d"], res[("test", rl)]["d"]])
        comb[rl] = float(d.mean() / d.std() * np.sqrt(252))
    sel = max(comb, key=comb.get) if comb else None
    out["selection"] = {"qualifiers": qual, "combined_sharpe": comb, "selected": sel}
    if sel:
        cs_ = {}
        for c in (0.0005, 0.001, 0.002):
            for bk in ("holdout", "paper"):
                cs_[f"{bk}@{c*1e4:.0f}bp"] = {k: float(v) for k, v in metrics(*run(*BUCKETS[bk], sel, cost=c)).items() if k != "d"}
        out["cost_sens"] = cs_
    # R0 median at 20bp in holdout (reference, 100 seeds)
    x, y = BUCKETS["holdout"]
    out["R0_holdout_20bp_med"] = float(np.median([metrics(*run(x, y, "R0", seed=s, cost=0.002))["tot"] for s in range(100)]))
    json.dump(out, open(ARGS.out, "w"), indent=1)
    print(json.dumps(out, indent=1))
