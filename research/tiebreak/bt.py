"""All-signals backtest (no slot cap) + live-rules portfolio sim, raw vs split-adjusted."""
import os
import sys
import numpy as np
if __name__ == "__main__" and len(sys.argv) > 1:  # usage: python bt.py [path/to/panel.npz]
    os.environ["TIEBREAK_PANEL"] = sys.argv[1]
from core import *  # noqa  (run from this directory: `cd research/tiebreak`)

ENT, EXT, HOLD = 15.0, 70.0, 10


def all_signals(P, t0, t1, hold=HOLD, entry_shift=0):
    """Per-symbol non-overlapping trades, entry at close of signal day i (+shift)."""
    C, Cf, R, M, DV = P["C"], P["Cf"], P["R"], P["M"], P["DV"]
    sig = (R <= ENT) & (Cf > M) & (Cf >= 5) & (Cf <= 1000) & (DV >= 10e6) & ~np.isnan(C)
    out = []
    for k in np.where(sig.any(0))[0]:
        i = 0
        col = sig[:, k]
        while i < T:
            if col[i]:
                e = i + entry_shift
                x = None
                for j in range(e + 1, T):
                    if R[j, k] >= EXT or (j - e) >= hold:
                        x = j; break
                if x is None:
                    break
                if t0 <= DATES[i] <= t1:
                    out.append((DATES[i], SYMS[k], Cf[x, k] / Cf[e, k] - 1 - 2 * COST, i, x, k, R[i, k]))
                i = x + 1
            else:
                i += 1
    return out


def stats(rets):
    r = np.array(rets)
    if len(r) == 0:
        return "n=0"
    k = len(r) // 100
    tr = np.sort(r)[k:len(r) - k]
    return (f"n={len(r):5d} mean={r.mean()*100:+.2f}% trim1={tr.mean()*100:+.2f}% "
            f"med={np.median(r)*100:+.2f}% win={(r>0).mean()*100:.1f}% n|r|>50%={(np.abs(r)>0.5).sum()}")


def portfolio(P, start, end, slots=20, hold_live=HOLD + 1, seed=None, cash_cap=True, tie="random"):
    """Live mr-trade rules: decide & fill at close D; exits RSI>=70 or held>=hold_live
    sessions; entries most-oversold first (ties random), capped by free slots and
    by pre-exit cash / (equity/slots). Equal weight = equity/slots."""
    C, Cf, R, M, DV = P["C"], P["Cf"], P["R"], P["M"], P["DV"]
    rng = np.random.default_rng(seed)
    t0, t1 = DIDX[start], DIDX[end]
    cash, pos, trades, eq = 1_000_000.0, {}, [], []
    for t in range(t0, t1 + 1):
        px = Cf[t]
        equity = cash + sum(q * px[k] for k, (q, e, ep) in pos.items())
        eq.append(equity)
        cash_pre = cash
        exits = [k for k, (q, e, ep) in pos.items() if R[t, k] >= EXT or (t - e) >= hold_live]
        for k in exits:
            q, e, ep = pos.pop(k)
            cash += q * px[k] * (1 - COST)
            trades.append((DATES[e], SYMS[k], px[k] * (1 - COST) / (ep) - 1, e, t, k))
        free = slots - len(pos)
        if free <= 0 or t == t1:
            continue
        # universe: liquid on the previous session (live uses latest grouped = D-1)
        cand = np.where((R[t] <= ENT) & (px > M[t]) & (px >= 5) & (px <= 1000) & (DV[t - 1] >= 10e6)
                        & ~np.isnan(C[t]))[0]
        cand = [k for k in cand if k not in pos]
        key = R[t, cand] + (rng.random(len(cand)) * 1e-6 if tie == "random" else 0)
        order = [cand[i] for i in np.argsort(key, kind="stable")]
        notional = equity / slots
        afford = int(max(0, cash_pre) // notional) if cash_cap else 10**9
        n = 0
        for k in order[:free]:
            if n >= afford:
                break
            q = int(notional // px[k])
            if q < 1:
                continue
            fill = px[k] * (1 + COST)
            cash -= q * fill
            pos[k] = (q, t, fill)
            n += 1
    # open positions marked at end
    opn = [(DATES[e], SYMS[k], Cf[t1, k] * (1 - COST) / ep - 1, e, t1, k) for k, (q, e, ep) in pos.items()]
    return np.array(eq), trades, opn


if __name__ == "__main__":
    PR, PA = build(False), build(True)
    _, ev = split_adjust(C_raw)
    print("split-like events detected:", len(ev), " in paper window:", sum(d >= "2026-07-01" for d, *_ in ev))
    print("  e.g.", [(d, s, round(x, 3)) for d, s, x, b in ev if d >= "2026-07-01"][:12])
    W = [("paper 07-01..09-25", "2026-07-01", "2026-09-25"), ("2y hist ..06-30", "2024-07-09", "2026-06-30")]
    for lab, a, b in W:
        for nm, P in [("raw", PR), ("splitadj", PA)]:
            tr = all_signals(P, a, b)
            print(f"[ALL-SIG {nm:8s}] {lab}: {stats([t[2] for t in tr])}")
            if nm == "raw":
                cl = [t[2] for t in tr if abs(t[2]) <= 0.5]
                print(f"[ALL-SIG raw ex|r|>50%] {lab}: {stats(cl)}")
