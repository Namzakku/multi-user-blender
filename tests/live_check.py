"""Judge the result files of live_loop.sh.

    python3 live_check.py <host result file> <client result file>

A trial passes when both Blenders report the same mesh by id (id_hash), no
crash, and the geometry hash expected_meshes.py gives for both edits kept
(edits on separate regions commute, so both orders give that hash).
Prints one line per trial and a total; exits 1 when any trial failed.
"""
import json
import sys


def read(path):
    expect, trials = {}, {}
    for line in open(path):
        if line.startswith("expect "):
            _, pair, geo = (line.split() + [""])[:3]
            expect[pair] = geo
        elif line.startswith("trial ") and "{" in line:
            k, pair = line.split()[1:3]
            trials[int(k)] = (pair, json.loads(line[line.index("{"):]))
        elif line.startswith("trial ") and " CRASH " in line:
            trials.setdefault(-1 - int(line.split()[1]), ("crash", {"crash": line.strip()}))
    return expect, trials


expect, host = read(sys.argv[1])
_, client = read(sys.argv[2])
bad = 0
for k in sorted(x for x in host if x >= 0):
    pair, a = host[k]
    b = client.get(k, (pair, {}))[1]
    crashed = (-1 - k) in host or (-1 - k) in client
    same = a.get("id_hash") is not None and a.get("id_hash") == b.get("id_hash")
    right = a.get("geo") == expect.get(pair) and b.get("geo") == expect.get(pair)
    ok = same and right and not crashed
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} trial {k} {pair}: host {a.get('verts')}/{a.get('faces')} {a.get('geo')}, "
          f"client {b.get('verts')}/{b.get('faces')} {b.get('geo')}, expected {expect.get(pair)}"
          + (", same ids" if same else ", DIVERGED") + (", CRASH" if crashed else ""))
total = len([x for x in host if x >= 0])
print(f"{total - bad} of {total} trials passed")
sys.exit(1 if bad else 0)
