"""Check a stress run: convergence across clients and, for disjoint vertex
ownership, that every logged edit is present."""
import glob, json, os, sys

d = sys.argv[1]
exact = sys.argv[2] == "exact" if len(sys.argv) > 2 else True
base = json.load(open(os.path.join(d, "base_agent0.json")))
finals = {os.path.basename(p)[6:-5]: json.load(open(p)) for p in sorted(glob.glob(os.path.join(d, "final_*.json")))}

print("clients:", list(finals))
for n, f in finals.items():
    if f.get("missing"):
        print(f"  {n}: OBJECT MISSING", f.get("state"))
        continue
    print(f"  {n}: hash={f['hash']} loc={f['loc']} state={f['state']} users={f['users']}")

hashes = {f.get("hash") for f in finals.values()}
locs = {tuple(f.get("loc", [])) for f in finals.values()}
print("CONVERGED mesh:", len(hashes) == 1, "| CONVERGED transform:", len(locs) == 1)

if exact:
    expected = [list(c) for c in base["co"]]
    for f in finals.values():
        for batch in f.get("batches", []):
            for i, dv in batch:
                for k in range(3):
                    expected[i][k] += dv[k]
    for n, f in finals.items():
        if f.get("missing"):
            continue
        bad = [i for i, (e, a) in enumerate(zip(expected, f["co"]))
               if max(abs(e[k] - a[k]) for k in range(3)) > 2e-4]
        print(f"  {n}: vertices differing from expected = {len(bad)} / {len(expected)}"
              + (f" e.g. v{bad[0]} exp={[round(x, 4) for x in expected[bad[0]]]} got={f['co'][bad[0]]}" if bad else ""))
    for n, f in finals.items():
        if f.get("own_prop"):
            axis, val, _ = f["own_prop"]
            seen = {m: g["loc"][axis] for m, g in finals.items() if g.get("loc")}
            ok = all(abs(v - val) < 1e-4 for v in seen.values())
            print(f"  {n} last wrote loc[{axis}]={val}: present everywhere = {ok} {seen if not ok else ''}")

# last writer wins: the newest write to each property must be on every client
props = [f["own_prop"] for f in finals.values() if f.get("own_prop")]
for axis in sorted({p[0] for p in props}):
    newest = max((p for p in props if p[0] == axis), key=lambda p: p[2])
    seen = {m: g["loc"][axis] for m, g in finals.items() if g.get("loc")}
    ok = all(abs(v - newest[1]) < 1e-4 for v in seen.values())
    print(f"LWW loc[{axis}]: newest write {newest[1]} at {newest[2]:.3f} on every client = {ok} {seen if not ok else ''}")
for n, f in finals.items():
    for t, m in f.get("events", []):
        if str(m).startswith("conflict"):
            print(f"  {n} t+{t}: {m}")
