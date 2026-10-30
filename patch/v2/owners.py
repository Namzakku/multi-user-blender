import json, sys
d = sys.argv[1]
base = json.load(open(f"{d}/base_agent0.json")); B = base["co"]
F = {a: json.load(open(f"{d}/final_agent{a}.json")) for a in range(3)}
exp = [list(v) for v in B]
for a in range(3):
    for b in F[a]["batches"]:
        for i, dv in b:
            for k in range(3): exp[i][k] += dv[k]
for a in range(3):
    co = F[a]["co"]; row = []
    for owner in range(3):
        idx = [i for i in range(len(co)) if i % 3 == owner]
        bad = [i for i in idx if max(abs(co[i][k] - exp[i][k]) for k in range(3)) > 2e-4]
        # does the wrong value match the owner's own final copy?
        same_as_owner = sum(1 for i in bad if max(abs(co[i][k] - F[owner]["co"][i][k]) for k in range(3)) < 2e-4)
        row.append(f"owner{owner}: wrong {len(bad)} (= owner's copy {same_as_owner})")
    print(f"agent{a}:", " | ".join(row))
