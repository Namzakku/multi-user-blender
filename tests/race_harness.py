"""Fuzz the order of events around concurrent edits on one mesh.

    python tests/race_harness.py [runs] [first seed] [scenario]

Each client and the server get their own copy of merge3 and topo3 (they keep
module state), a fake clock, and the call sequence porcelain uses:
  commit  stamp_commit, store if changed, after_commit, push, state_after_push
          (skipped while the node waits to be applied, as upstream does)
  receive merge_incoming into the stored node, node becomes FETCHED
  apply   merge_fetched, load, remember_merged, publish if it contributed
The server merges each update and forwards the original to the others. A load
checks the dump the way Blender does: a count that does not match its buffer is
"Array length mismatch", which crashed Blender in the 2026-11-04 run.

Scenarios (default: both, `runs` seeds each):
  clash   Alice and Bob change the same quad at nearly the same moment. A run
          passes when everyone ends with the same mesh and that mesh holds
          exactly one of the two edits.
  delete  Alice runs one to three delete-type steps (delete faces, delete a
          vertex, dissolve an edge, merge two vertices at centre) in one corner
          of a 9 by 9 grid while Bob runs one to three edits of any kind in the
          far corner. Each step happens in an Edit Mode window of up to 1 s,
          during which loads fail and commits wait, as in Blender. A run passes
          when nothing failed to load, everyone ends with the same mesh, and
          every face sits on the points it would after both users' steps run
          one after the other.
Prints each failing seed.
"""
import copy
import heapq
import logging
import os
import random
import sys

import numpy as np

logging.disable(logging.WARNING)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv, _argv = sys.argv[:1], sys.argv
import test_sim as ts  # noqa: E402  (mesh helpers, Peer and the strict load)
sys.argv = _argv

REAL_NORMALS = os.environ.get("NORMALS", "1") == "1"
FLOAT_IDS = os.environ.get("FLOAT_IDS", "1") == "1"
INSET = float(os.environ.get("INSET", "0.2"))  # 0.2 gives coordinates float32 cannot hold exactly
Clock = ts.Clock


def blender_dump(state):
    """dump_of with the loop normals Blender computes (the grid is flat), where a
    topo3 rebuild writes zeros."""
    d = ts.dump_of(*state)
    if REAL_NORMALS:
        n = len(d["loops"]["vertex_index"]) // 4
        d["loops"]["normal"] = np.tile(np.array([0, 0, 1], dtype=np.float32), n).tobytes()
    return d


def blender_ids(state):
    """Blender's inset and poke pass the integer id attribute through float32,
    so ids above 2**24 lose their low bits (seen live: 1560281096 became
    1560281088 on untouched vertices)."""
    if not FLOAT_IDS:
        return state
    verts, faces, ids = state
    return verts, faces, [int(np.float32(i)) for i in ids]


def inset(state, fi, k=INSET):
    """Blender's inset: an inner ring copying the corner ids, an inner face and a
    ring of quads."""
    verts, faces, ids = copy.deepcopy(state)
    f = faces.pop(fi)
    cx = [sum(verts[v][i] for v in f) / len(f) for i in range(3)]
    inner = []
    for v in f:
        verts.append(tuple(verts[v][i] + (cx[i] - verts[v][i]) * k for i in range(3)))
        ids.append(ids[v])
        inner.append(len(verts) - 1)
    faces.append(inner)
    for i in range(len(f)):
        j = (i + 1) % len(f)
        faces.append([f[i], f[j], inner[j], inner[i]])
    return verts, faces, ids


class Client:
    def __init__(self, name, synced, sim):
        self.name, self.sim = name, sim
        self.peer = ts.Peer(name, synced, dump=blender_dump)
        self.pending = None

    @property
    def state(self):
        return self.peer.state

    @state.setter
    def state(self, value):
        self.peer.state = value

    def send(self, sent):
        if sent is None:
            return
        self.sim.send(self.name, sent)
        if self.peer.node.state == "FETCHED":
            self.sim.later(self.sim.apply_delay(), self.name, "apply")

    def commit(self):
        self.send(self.peer.commit())

    def receive(self, data):
        self.peer.receive(data)
        self.sim.later(self.sim.apply_delay(), self.name, "apply")

    def apply(self):
        if self.peer.node.state != "FETCHED" or self.sim.crashed:
            return
        try:
            sent = self.peer.apply()
        except ts.InEditMode:
            self.sim.later(0.2, self.name, "apply")  # the apply timer tries again
            return
        except ts.LoadError as e:
            self.sim.crashed.append(f"{self.name} at {Clock.now:.2f}: {e}")
            return
        self.send(sent)
        self.sim.later(self.sim.rng.uniform(0.0, 0.05), self.name, "commit")  # depsgraph after load


class Sim:
    def __init__(self, seed, start):
        self.seed = seed
        self.rng = random.Random(seed)
        self.q, self.seq = [], 0
        self.edited, self.saw = [], {}
        self.crashed = []
        Clock.now = 1.0
        self.server_m3 = ts.fresh_modules("server")
        self.stored = copy.deepcopy(start)
        self.clients = {"alice": Client("alice", start, self), "bob": Client("bob", start, self)}

    def apply_delay(self):
        return self.rng.uniform(0.0, 0.5)

    def later(self, delay, who, what, arg=None):
        self.later_at(Clock.now + delay, who, what, arg)

    def later_at(self, t, who, what, arg=None):
        self.seq += 1
        heapq.heappush(self.q, (t, self.seq, who, what, arg))

    def send(self, sender, data):
        self.later(self.rng.uniform(0.005, 0.2), "server", "receive", (sender, data))

    def run(self):
        steps = 0
        while self.q and steps < 4000:
            steps += 1
            t, _, who, what, arg = heapq.heappop(self.q)
            Clock.now = t
            if who == "server":
                sender, data = arg
                self.stored = self.server_m3.merge_incoming(self.stored, data)
                for name in self.clients:
                    if name != sender:
                        self.later(self.rng.uniform(0.005, 0.2), name, "receive", copy.deepcopy(data))
                continue
            c = self.clients[who]
            if what == "edit":
                c.state = blender_ids(region_edit(c.state, arg))
                self.edited.append(who)
                self.saw[who] = signature(c.state)
                self.later(self.rng.uniform(0.0, 0.3), who, "commit")
            elif what == "enter":          # Edit Mode: Blender edits a copy of the mesh it held now
                op, window = arg
                c.pending = op(c.state)
                c.peer.edit_mode = True
                self.later(window, who, "leave")
            elif what == "leave":
                c.state, c.pending = c.pending, None
                c.peer.edit_mode = False
                self.later(self.rng.uniform(0.0, 0.3), who, "commit")
            elif what == "commit":
                c.commit()
            elif what == "receive":
                c.receive(arg)
            elif what == "apply":
                c.apply()
        return steps


# ---------------------------------------------------------------- clash scenario
LO, HI = 1.0, 3.0  # the 2x2 block of quads in the middle of the 4x4 grid


def region_edit(state, fn, box=(LO, HI, LO, HI), by_position=False):
    """Apply fn to every face whose centre lies in the box, as selecting the
    block and running the operator does. Faces are found again by their sorted
    ids, or with by_position by their sorted corner points, which do not depend
    on how ids were copied or repaired."""
    x0, x1, y0, y1 = box

    def centre(st, f):
        return [sum(st[0][v][i] for v in f) / len(f) for i in range(2)]

    def key(st, f):
        if by_position:
            return tuple(sorted(tuple(round(float(c), 5) for c in st[0][v]) for v in f))
        return tuple(sorted(st[2][v] for v in f))
    todo = sorted(key(state, f) for f in state[1]
                  if x0 < centre(state, f)[0] < x1 and y0 < centre(state, f)[1] < y1) if by_position else \
        [key(state, f) for f in state[1] if x0 < centre(state, f)[0] < x1 and y0 < centre(state, f)[1] < y1]
    for k in todo:
        fi = next(i for i, f in enumerate(state[1]) if key(state, f) == k)
        state = fn(state, fi)
    return state


def signature(state):
    verts, faces, _ = state
    return len(verts), len(faces)


BOB_EDIT = inset if os.environ.get("BOB", "inset") == "inset" else ts.split_diagonal
EDITS = {"alice": ts.split_center, "bob": BOB_EDIT}


def expected():
    start = ts.load(ts.host_start((*ts.grid(), None)))
    out = {n: signature(region_edit(start, fn)) for n, fn in EDITS.items()}
    out["alice,bob"] = signature(region_edit(region_edit(start, EDITS["alice"]), EDITS["bob"]))
    out["bob,alice"] = signature(region_edit(region_edit(start, EDITS["bob"]), EDITS["alice"]))
    return out


def run_clash(seed):
    gap = random.Random(seed * 7 + 1).uniform(-0.6, 0.6)
    sim = Sim(seed, ts.host_start((*ts.grid(), None)))
    t0 = 10.0
    sim.later_at(t0, "alice", "edit", EDITS["alice"])
    sim.later_at(t0 + gap, "bob", "edit", EDITS["bob"])
    sim.run()
    meshes = {n: ts.id_mesh(ts.dump_of(*c.state)) for n, c in sim.clients.items()}
    server = ts.id_mesh(sim.stored)
    same = all(m == server for m in meshes.values())
    sig = (len(server["verts"]), len(server["faces"]))
    exp = expected()
    first, second = sim.edited
    if sim.saw[second] == exp[first] or sim.saw[second] == exp[",".join(sim.edited)]:
        allowed = [exp[",".join(sim.edited)]]   # made on top of the first: both kept
        kind = "sequential"
    else:
        allowed = [exp["alice"], exp["bob"]]
        kind = "clash"
    ok = same and sig in allowed and not sim.crashed
    return ok, f"gap {gap:+.3f} {kind}: converged={same} server mesh {sig} {sim.crashed}"


# ---------------------------------------------------------------- delete scenario
N = 9                                   # 9 by 9 quads, points 0..9
ALICE_BOX = (1.0, 3.0, 1.0, 3.0)        # 2 by 2 quads; with their ring they stay below x, y = 4
BOB_BOX = (6.0, 8.0, 6.0, 8.0)          # and Bob's ring stays above 5, so no base vertex is shared


def _centre(state, f):
    return tuple(sum(state[0][v][i] for v in f) / len(f) for i in range(3))


def _inside(p, box, closed=False):
    x0, x1, y0, y1 = box
    if closed:
        return x0 - 1e-6 <= p[0] <= x1 + 1e-6 and y0 - 1e-6 <= p[1] <= y1 + 1e-6
    return x0 < p[0] < x1 and y0 < p[1] < y1


def _key(p):
    return tuple(round(float(c), 4) for c in p)


def region_faces(state, box):
    return sorted((i for i, f in enumerate(state[1]) if _inside(_centre(state, f), box)),
                  key=lambda i: _key(_centre(state, state[1][i])))


def op_delete(box):
    def run(state):
        fs = region_faces(state, box)
        return ts.delete_faces(state, fs[:1]) if fs else state
    return run


def op_delete_vert(box):
    def run(state):
        used = {v for i in region_faces(state, box) for v in state[1][i]}
        vs = sorted((v for v in used if _inside(state[0][v], box, closed=True)), key=lambda v: _key(state[0][v]))
        return ts.delete_vert(state, vs[0]) if vs else state
    return run


def op_dissolve(box):
    def run(state):
        fs = region_faces(state, box)
        best = None
        for x in range(len(fs)):
            for y in range(x + 1, len(fs)):
                f, g = state[1][fs[x]], state[1][fs[y]]
                if any(g[(g.index(f[k]) - 1) % len(g)] == f[(k + 1) % len(f)]
                       for k in range(len(f)) if f[k] in g):
                    key = sorted([_key(_centre(state, f)), _key(_centre(state, g))])
                    if best is None or key < best[0]:
                        best = (key, fs[x], fs[y])
        return ts.dissolve(state, best[1], best[2]) if best else state
    return run


def op_merge(box, interp_id):
    def run(state):
        edges = set()
        for i in region_faces(state, box):
            f = state[1][i]
            for k in range(len(f)):
                a, b = f[k], f[(k + 1) % len(f)]
                if _inside(state[0][a], box, True) and _inside(state[0][b], box, True):
                    edges.add((min(a, b), max(a, b)))
        if not edges:
            return state
        a, b = min(edges, key=lambda e: sorted([_key(state[0][e[0]]), _key(state[0][e[1]])]))
        if _key(state[0][b]) < _key(state[0][a]):
            a, b = b, a
        return ts.merge_verts(state, a, b, interp_id=interp_id)
    return run


def op_extrude(box, h=0.5):
    """Extrude Region on the faces in the box: one shared top, side quads along
    the outline, the inner points of the old region removed. New vertices copy
    the ids of the vertices they came from, as in Blender."""
    def run(state):
        fs = region_faces(state, box)
        if not fs:
            return state
        verts, faces, ids = copy.deepcopy(state)
        region = [faces[i] for i in fs]
        top = {}
        for f in region:
            for v in f:
                if v not in top:
                    x, y, z = verts[v]
                    verts.append((x, y, z + h))
                    ids.append(ids[v])
                    top[v] = len(verts) - 1
        count = {}
        for f in region:
            for k in range(len(f)):
                e = (f[k], f[(k + 1) % len(f)])
                count[tuple(sorted(e))] = count.get(tuple(sorted(e)), 0) + 1
        sides = []
        for f in region:
            for k in range(len(f)):
                a, b = f[k], f[(k + 1) % len(f)]
                if count[tuple(sorted((a, b)))] == 1:
                    sides.append([a, b, top[b], top[a]])
        rest = [f for i, f in enumerate(faces) if i not in set(fs)]
        used = {v for f in rest + sides for v in f}
        dead = {v for f in region for v in f} - used
        out = rest + [[top[v] for v in f] for f in region] + sides
        return ts._compact(verts, out, ids, dead)
    return run


def op_region(fn, box, float_ids=True):
    def run(state):
        out = region_edit(state, fn, box, by_position=True)
        return blender_ids(out) if float_ids else out
    return run


def delete_ops(box):
    return {"delete": op_delete(box), "delete_vert": op_delete_vert(box), "dissolve": op_dissolve(box),
            "merge": op_merge(box, False), "merge_interp": op_merge(box, True)}


def any_ops(box):
    ops = delete_ops(box)
    ops.update({"poke": op_region(ts.split_center, box), "inset": op_region(inset, box),
                "extrude": op_extrude(box)})
    return ops


def run_delete(seed):
    rng = random.Random(seed * 13 + 5)
    start = ts.host_start((*ts.grid(N), None))
    sim = Sim(seed, start)
    plan = {"alice": [], "bob": []}
    for who, ops in (("alice", delete_ops(ALICE_BOX)), ("bob", any_ops(BOB_BOX))):
        t = 10.0 + (rng.uniform(-0.6, 0.6) if who == "bob" else 0.0)
        for _ in range(rng.randint(1, 3)):
            name = rng.choice(sorted(ops))
            window = rng.choice([0.0, rng.uniform(0.0, 1.0)])
            plan[who].append(name)
            sim.later_at(t, who, "enter", (ops[name], window))
            t += window + rng.uniform(0.05, 1.5)
    sim.run()
    exp = ts.load(start)
    for who, ops in (("alice", delete_ops(ALICE_BOX)), ("bob", any_ops(BOB_BOX))):
        for name in plan[who]:
            exp = ops[name](exp)
    want = ts.geometry(exp)
    meshes = {n: ts.id_mesh(ts.dump_of(*c.state)) for n, c in sim.clients.items()}
    server = ts.id_mesh(sim.stored)
    same = all(m == server for m in meshes.values())
    right = {n: ts.geometry(c.state) == want for n, c in sim.clients.items()}
    ok = same and all(right.values()) and not sim.crashed
    detail = (f"alice {plan['alice']} bob {plan['bob']}: converged={same} right={right} "
              f"faces {[len(c.state[1]) for c in sim.clients.values()]} want {len(exp[1])}"
              + (f" LOAD FAILED {sim.crashed}" if sim.crashed else ""))
    return ok, detail


SCENARIOS = {"clash": run_clash, "delete": run_delete}

if __name__ == "__main__":
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    first = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    names = [sys.argv[3]] if len(sys.argv) > 3 else list(SCENARIOS)
    total = 0
    for name in names:
        if name == "clash":
            print("clash expected", expected())
        bad = 0
        for seed in range(first, first + runs):
            ok, detail = SCENARIOS[name](seed)
            if not ok:
                bad += 1
                print(f"FAIL {name} seed {seed} {detail}")
        print(f"{name}: {bad} of {runs} runs failed")
        total += bad
    sys.exit(1 if total else 0)
