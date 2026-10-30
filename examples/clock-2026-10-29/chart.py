#!/usr/bin/env python3
"""Draw docs/clock-cap.svg from the sweep data.  python3 chart.py sweep ../../docs/clock-cap.svg"""
import json, sys, collections
src, out = sys.argv[1], sys.argv[2]
S = json.load(open(f"{src}/summary.json"))["rows"]
reps = [json.loads(l) for l in open(f"{src}/reps.jsonl") if json.loads(l)["phase"] > 0]
best = collections.defaultdict(lambda: 1e9)
for r in reps:
    best[r["level"]] = min(best[r["level"]], r["ttft_32k"])
base_pf = best["reset"]
unc = [r for r in S if r["level"] == "reset"]
base_dec = sum(r["c1_prose"] for r in unc) / len(unc)
pts = [("uncapped", sum(r["w_pair_mean"] for r in unc) / len(unc), 100.0, 100.0)]
for r in S:
    if r["level"] != "reset":
        pts.append((r["level"] + " MHz", r["w_pair_mean"], 100 * base_pf / best[r["level"]], 100 * r["c1_prose"] / base_dec))
for p in pts: print(f"{p[0]:>9}  {p[1]:6.1f} W  prefill {p[2]:5.1f}%  decode {p[3]:5.1f}%")

W, H = 720, 420
L, R, T, B = 64, 40, 80, 64
x0, x1, y0, y1 = 60, 150, 86, 101
X = lambda w: L + (w - x0) / (x1 - x0) * (W - L - R)
Y = lambda v: T + (y1 - v) / (y1 - y0) * (H - T - B)
o = []
a = o.append
a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="t d">')
a('<title id="t">GB10 clock cap: speed kept vs GPU power, two DGX Sparks</title>')
a('<desc id="d">' + "; ".join(f"{p[0]}: {p[1]:.0f} W, 32K prefill {p[2]:.1f}%, decode {p[3]:.1f}%" for p in pts) + '</desc>')
a('''<style>
svg{--bg:#f5f7f6;--ink:#0e1312;--mute:#56625f;--grid:#d5dcda;--dec:#1a62a8;--pf:#a6411a;font-family:"IBM Plex Sans","Segoe UI",Arial,sans-serif}
@media (prefers-color-scheme:dark){svg{--bg:#0e1312;--ink:#e6ebe9;--mute:#9aa8a3;--grid:#27322f;--dec:#3f98d6;--pf:#de7040}}
.bg{fill:var(--bg)}.g{stroke:var(--grid);stroke-width:1}.ax{fill:var(--mute);font-size:12px}
.h{fill:var(--ink);font-size:15px;font-weight:600}.s{fill:var(--mute);font-size:12px}
.lab{fill:var(--ink);font-size:12px;font-weight:600}.cap{fill:var(--mute);font-size:11px}
.ld{stroke:var(--dec)}.lp{stroke:var(--pf)}.dd{fill:var(--dec)}.dp{fill:var(--pf)}
.ln{fill:none;stroke-width:2;stroke-linejoin:round}.pt{stroke:var(--bg);stroke-width:2}
.k{fill:none;stroke:var(--ink);stroke-width:1}.kt{fill:var(--ink);font-size:12px}
</style>''')
a(f'<rect class="bg" width="{W}" height="{H}" rx="8"/>')
a(f'<text class="h" x="{L}" y="24">Capping the GPU clock: decode keeps up, prefill pays a little, power falls a lot</text>')
a(f'<text class="s" x="{L}" y="42">Two DGX Sparks, DeepSeek-V4.1-Flash TP2, % of uncapped speed vs GPU-rail watts for the pair</text>')
for v in range(86, 102, 2):
    a(f'<line class="g" x1="{L}" x2="{W-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>')
    a(f'<text class="ax" x="{L-8}" y="{Y(v)+4:.1f}" text-anchor="end">{v}%</text>')
for w in range(60, 151, 15):
    a(f'<text class="ax" x="{X(w):.1f}" y="{H-B+18}" text-anchor="middle">{w} W</text>')
a(f'<text class="ax" x="{(L+W-R)/2:.0f}" y="{H-B+40}" text-anchor="middle">GPU-rail power, both Sparks (not wall power) →</text>')
for cls, i, name in (("d", 3, "Decode, 1 stream"), ("p", 2, "32K cold prefill")):
    path = " ".join(f"{'M' if j==0 else 'L'}{X(p[1]):.1f},{Y(p[i]):.1f}" for j, p in enumerate(pts))
    a(f'<path class="ln l{cls}" d="{path}"/>')
    for p in pts:
        a(f'<circle class="pt d{cls}" cx="{X(p[1]):.1f}" cy="{Y(p[i]):.1f}" r="5"><title>{p[0]} · {p[1]:.0f} W · {name} {p[i]:.1f}%</title></circle>')
    a(f'<text class="lab" x="{X(120):.1f}" y="{Y(100.45 if cls=="d" else 98.2):.1f}" text-anchor="middle">{name}</text>')
# legend (kept even with direct labels) + noise note
for j, (cls, name) in enumerate((("d", "Decode, 1 stream"), ("p", "32K cold prefill (best of 4)"))):
    lx = L + j * 170
    a(f'<line class="ln l{cls}" x1="{lx}" x2="{lx+18}" y1="58" y2="58"/><circle class="pt d{cls}" cx="{lx+9}" cy="58" r="4"/>')
    a(f'<text class="s" x="{lx+24}" y="62">{name}</text>')
a(f'<text class="cap" x="{L+8}" y="{Y(100.55):.1f}">Decode moves ±1–2% rep to rep; only 1900 is a real loss</text>')
# MHz under each prefill point
for p in pts:
    a(f'<text class="cap" x="{X(p[1]):.1f}" y="{Y(p[2])+20:.1f}" text-anchor="middle">{p[0].replace(" MHz","")}</text>')
# knee bracket between uncapped and 2300
u, k = pts[0], pts[1]
yb = Y(89.6)
a(f'<path class="k" d="M{X(k[1]):.1f},{yb-5:.1f} V{yb:.1f} H{X(u[1]):.1f} V{yb-5:.1f}"/>')
a(f'<text class="kt" x="{(X(k[1])+X(u[1]))/2:.1f}" y="{yb+16:.1f}" text-anchor="middle">−{u[1]-k[1]:.0f} W for −{100-k[2]:.1f}% prefill</text>')
a(f'<text class="kt" x="{(X(k[1])+X(u[1]))/2:.1f}" y="{yb+31:.1f}" text-anchor="middle">2300 MHz, live since Oct 29</text>')
a('</svg>')
open(out, "w").write("\n".join(o) + "\n")
print("wrote", out)
