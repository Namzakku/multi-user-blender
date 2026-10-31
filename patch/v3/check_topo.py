"""Check a topo or topoclash stress run.

python3 check_topo.py out_topo_3      every agent's edits present, all clients equal
python3 check_topo.py out_topoclash_3 the conflict resolved one way, far edits kept
"""
import glob, json, os, sys

d = sys.argv[1]
base = json.load(open(os.path.join(d, "base_agent0.json")))
finals = {os.path.basename(p)[6:-5]: json.load(open(p)) for p in sorted(glob.glob(os.path.join(d, "final_*.json")))}
print("clients:", list(finals), "base verts", base["verts"], "faces", base["faces"])
for n, f in finals.items():
    t = f.get("topo", {})
    print(f"  {n}: verts={t.get('verts')} faces={t.get('faces')} id_hash={t.get('id_hash')} uv_hash={t.get('uv_hash')} "
          f"unique_ids={t.get('unique_ids')} conflicts={t.get('view_conflicts')} commits={t.get('commits')} "
          f"loc={f.get('loc')} state={f.get('state')} edits={t.get('edits')}")

ids = {f.get("topo", {}).get("id_hash") for f in finals.values()}
uvs = {f.get("topo", {}).get("uv_hash") for f in finals.values()}
print("CONVERGED mesh by id:", len(ids) == 1 and None not in ids, "| UVs:", len(uvs) == 1,
      "| transform:", len({tuple(f.get("loc", [])) for f in finals.values()}) == 1)

edits = {n: f.get("topo", {}).get("edits", []) for n, f in finals.items()}
dv = sum(e[1] for es in edits.values() for e in es)
df = sum(e[2] for es in edits.values() for e in es)
got = next(iter(finals.values())).get("topo", {})
if "topoclash" in d:
    # agent0's +X extrude loses to agent1's later subdivide of the same face
    lost = edits.get("agent0", [[0, 0, 0]])[0]
    print(f"expected if agent0's clashing extrude is dropped: verts {base['verts'] + dv - lost[1]} "
          f"faces {base['faces'] + df - lost[2]}")
    print(f"expected if every edit were kept (impossible here): verts {base['verts'] + dv} faces {base['faces'] + df}")
    ok = got.get("verts") == base["verts"] + dv - lost[1] and got.get("faces") == base["faces"] + df - lost[2]
    print("CLASH RESOLVED with far edits kept:", ok)
else:
    print(f"expected with every edit kept: verts {base['verts'] + dv} faces {base['faces'] + df}")
    print("ALL EDITS PRESENT:", got.get("verts") == base["verts"] + dv and got.get("faces") == base["faces"] + df)
props = [f["own_prop"] for f in finals.values() if f.get("own_prop")]
for axis in sorted({p[0] for p in props}):
    newest = max((p for p in props if p[0] == axis), key=lambda p: p[2])
    seen = {m: g["loc"][axis] for m, g in finals.items() if g.get("loc")}
    print(f"LWW loc[{axis}]: newest {newest[1]} on every client =", all(abs(v - newest[1]) < 1e-4 for v in seen.values()))
