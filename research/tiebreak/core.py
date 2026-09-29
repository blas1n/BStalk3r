"""Shared panel + indicators for the paper-vs-backtest gap study (read-only).

The panel path comes from $TIEBREAK_PANEL (default: panel.npz next to this file);
the entry scripts set it from their --panel argument before importing this module.
"""
import os
from pathlib import Path

import numpy as np

PANEL = os.environ.get("TIEBREAK_PANEL", str(Path(__file__).with_name("panel.npz")))
z = np.load(PANEL)
O, C_raw, V = z["O"], z["C"], z["V"]
DATES = [str(d) for d in z["dates"]]
SYMS = [str(s) for s in z["syms"]]
SIDX = {s: i for i, s in enumerate(SYMS)}
DIDX = {d: i for i, d in enumerate(DATES)}
T, N = C_raw.shape
COST = 0.001


def ffill(a):
    a = a.copy()
    for i in range(1, a.shape[0]):
        m = np.isnan(a[i])
        a[i, m] = a[i - 1, m]
    return a


SPLIT_K = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 35, 40, 50, 60, 75, 80, 100, 150, 200, 250]
SPLIT_R = sorted({k for k in SPLIT_K} | {1 / k for k in SPLIT_K} | {1.5, 2 / 3, 2.5, 0.4, 4 / 3, 0.75})


def split_adjust(C):
    """Back-adjust bars whose close/prev ratio is within 3% of a standard split
    ratio and |move|>40%. Returns adjusted closes, number of events, event list."""
    Cf = ffill(C)
    A = Cf.copy()
    ev = []
    r = Cf[1:] / Cf[:-1]
    idx = np.argwhere(np.isfinite(r) & ((r > 1.4) | (r < 0.6)))
    for t, k in idx:
        x = r[t, k]
        best = min(SPLIT_R, key=lambda s: abs(x / s - 1))
        if abs(x / best - 1) < 0.03:
            ev.append((DATES[t + 1], SYMS[k], x, best))
            A[: t + 1, k] *= best
    A[np.isnan(C)] = np.nan
    return A, ev


def rsi2(Cf):
    ch = np.diff(Cf, axis=0, prepend=np.nan)
    g = np.where(ch > 0, ch, 0.0); l = np.where(ch < 0, -ch, 0.0)
    g[np.isnan(ch)] = np.nan; l[np.isnan(ch)] = np.nan
    G = (g + np.roll(g, 1, axis=0)) / 2; L = (l + np.roll(l, 1, axis=0)) / 2
    G[:2] = np.nan; L[:2] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        R = np.where(L == 0, 100.0, 100 - 100 / (1 + G / L))
    R[np.isnan(G) | np.isnan(L)] = np.nan
    return R


def sma(Cf, n=200):
    cs = np.nancumsum(np.nan_to_num(Cf), axis=0)
    cnt = np.cumsum(~np.isnan(Cf), axis=0)
    out = np.full_like(Cf, np.nan)
    out[n:] = (cs[n:] - cs[:-n]) / n
    out[n - 1] = cs[n - 1] / n
    ok = np.zeros_like(cnt, dtype=bool); ok[n - 1:] = (cnt[n - 1:] - np.vstack([np.zeros((1, Cf.shape[1])), cnt[:-n]])) >= n
    out[~ok] = np.nan
    return out


def build(adjust):
    C = split_adjust(C_raw)[0] if adjust else C_raw
    Cf = ffill(C)
    R = rsi2(Cf)
    M = sma(Cf)
    DV = C_raw * V  # dollar volume uses raw traded price
    return dict(C=C, Cf=Cf, R=R, M=M, DV=DV)


def ew_index(Cf, DV):
    r = Cf[1:] / Cf[:-1] - 1
    liq = (DV[:-1] >= 10e6) & (Cf[:-1] >= 5) & (Cf[:-1] <= 1000)
    ok = liq & np.isfinite(r) & (np.abs(r) < 0.5)
    ew = np.where(ok, r, 0).sum(1) / np.maximum(ok.sum(1), 1)
    return np.concatenate([[0.0], ew])  # ew[t] = return from t-1 to t
