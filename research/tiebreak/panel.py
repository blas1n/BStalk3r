"""Build numpy panel (dates x symbols) of o,c,v from a COPY of the grouped cache -> panel.npz.

Usage: python panel.py --grouped-db /path/to/copy/of/grouped_cache.db --out panel.npz
Never point --grouped-db at the live data/grouped_cache.db; copy it first.
"""
import argparse, json, sqlite3
import numpy as np
ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--grouped-db", required=True, help="copy of data/grouped_cache.db")
ap.add_argument("--out", default="panel.npz", help="output .npz path (default: ./panel.npz)")
args = ap.parse_args()
con = sqlite3.connect(f"file:{args.grouped_db}?mode=ro", uri=True)
rows = con.execute("select date, rows_json from grouped_cache order by date").fetchall()
dates = [d for d, _ in rows]
syms = {}
data = []
for d, j in rows:
    rr = json.loads(j)
    data.append(rr)
    for r in rr:
        t = r.get("T")
        if t and t not in syms:
            syms[t] = len(syms)
O = np.full((len(dates), len(syms)), np.nan, dtype=np.float64); C = O.copy(); V = O.copy()
for i, rr in enumerate(data):
    for r in rr:
        t, c = r.get("T"), r.get("c")
        if not t or not c:
            continue
        k = syms[t]; C[i, k] = float(c); O[i, k] = float(r.get("o") or c); V[i, k] = float(r.get("v") or 0)
np.savez(args.out, O=O, C=C, V=V, dates=np.array(dates), syms=np.array(list(syms)))
print(len(dates), len(syms))
