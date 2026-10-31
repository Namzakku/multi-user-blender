"""Fuzz the order of events around two concurrent edits on one region.

    python tests/race_harness.py [runs] [first seed]

Each client and the server get their own copy of merge3 and topo3 (they keep
module state), a fake clock, and the call sequence porcelain uses:
  commit  stamp_commit, store if changed, after_commit, push, state_after_push
          (skipped while the node waits to be applied, as upstream does)
  receive merge_incoming into the stored node, node becomes FETCHED
  apply   merge_fetched, load, remember_merged, publish if it contributed
The server merges each update and forwards the original to the others.

Alice and Bob change the same quad at nearly the same moment. A run passes when
everyone ends with the same mesh and that mesh holds exactly one of the two
edits. Prints each failing seed.
"""
import copy
import heapq
import importlib
import os
import random
import shutil
import sys
import tempfile

import numpy as np
import logging

logging.disable(logging.WARNING)

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "addon", "replication")
sys.argv, _argv = sys.argv[:1], sys.argv
import test_sim as ts  # noqa: E402  (mesh helpers; its own merge3 copy is unused)
sys.argv = _argv

ROOT = tempfile.mkdtemp()
REAL_NORMALS = os.environ.get("NORMALS", "1") == "1"
FLOAT_IDS = os.environ.get("FLOAT_IDS", "1") == "1"
INSET = float(os.environ.get("INSET", "0.2"))  # 0.2 gives coordinates float32 cannot hold exactly
sys.path.insert(0, ROOT)


BOB_EDIT = None  # set below


class Clock:
    now = 0.0

    def time(self):
        return Clock.now


def fresh_modules(tag):
    pkg = f"rep_{tag}"
    os.makedirs(os.path.join(ROOT, pkg), exist_ok=True)
    open(os.path.join(ROOT, pkg, "__init__.py"), "w").close()
    for name in ("merge3.py", "topo3.py"):
        shutil.copy(os.path.join(SRC, name), os.path.join(ROOT, pkg, name))
    for mod in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        del sys.modules[mod]
    m3 = importlib.import_module(f"{pkg}.merge3")
    m3.time = Clock()
    return m3


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


class Node:
    def __init__(self, data):
        self.uuid = "mesh"
        self.data = data
        self.instance = object()
        self.state = "UP"


class Client:
    def __init__(self, name, number, synced, sim):
        self.name, self.sim = name, sim
        self.m3 = fresh_modules(name)
        self.m3.topo3._username = name
        self.number = number
        self.node = Node(copy.deepcopy(synced))
        self.m3.remember(self.node.uuid, self.node.data)
        self.state = ts.load(synced)        # the datablock
        self.repo = self
        self.rdp = self

    def dump(self, instance, stamp_uuid=None):
        return blender_dump(self.state)

    def commit(self):
        if self.node.state != "UP":
            return  # porcelain.commit skips a node in any other state
        new = self.m3.stamp_commit(self.node, blender_dump(self.state))
        if self.m3._eq(new, self.node.data):
            self.m3._commit_base.pop(self.node.uuid, None)
            return
        self.node.data = new
        self.m3.after_commit(self.node.uuid, self.node.data)
        self.sim.send(self.name, copy.deepcopy(self.node.data))
        self.node.state = self.m3.state_after_push(self.node.uuid, "UP", "FETCHED")
        if self.node.state == "FETCHED":
            self.sim.later(self.sim.apply_delay(), self.name, "apply")

    def receive(self, data):
        self.node.data = self.m3.merge_incoming(self.node.data, data)
        self.node.state = "FETCHED"
        self.sim.later(self.sim.apply_delay(), self.name, "apply")

    def apply(self):
        if self.node.state != "FETCHED":
            return
        to_load, contributed = self.m3.merge_fetched(self, self.node)
        self.state = ts.load(to_load)
        self.node.state = "UP"
        last = self.m3._last_merge.pop(self.node.uuid, None)
        if last is not None:
            self.m3.remember_merged(self.node.uuid, last[0], last[1], contributed)
        else:
            self.m3.remember(self.node.uuid, self.node.data)
        if contributed and self.m3.PUBLISH_MERGES:
            self.commit()
        self.sim.later(self.sim.rng.uniform(0.0, 0.05), self.name, "commit")  # depsgraph after load


class Sim:
    def __init__(self, seed, gap):
        self.rng = random.Random(seed)
        self.q, self.seq = [], 0
        self.edited, self.saw = [], {}
        Clock.now = 1.0
        start = ts.host_start((*ts.grid(), None))
        self.server_m3 = fresh_modules("server")
        self.stored = copy.deepcopy(start)
        self.clients = {"alice": Client("alice", 1, start, self), "bob": Client("bob", 2, start, self)}
        t0 = 10.0
        self.later_at(t0, "alice", "edit", ts.split_center)
        self.later_at(t0 + gap, "bob", "edit", BOB_EDIT)

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
        while self.q and steps < 2000:
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
            elif what == "commit":
                c.commit()
            elif what == "receive":
                c.receive(arg)
            elif what == "apply":
                c.apply()
        return steps


LO, HI = 1.0, 3.0  # the 2x2 block of quads in the middle of the 4x4 grid


def region_edit(state, fn):
    """Apply fn to every face whose centre lies in the block, as selecting the
    block and running the operator does."""
    def centre(st, f):
        return [sum(st[0][v][i] for v in f) / len(f) for i in range(2)]
    todo = [tuple(sorted(state[2][v] for v in f)) for f in state[1]
            if all(LO < c < HI for c in centre(state, f))]
    for key in todo:
        fi = next(i for i, f in enumerate(state[1]) if tuple(sorted(state[2][v] for v in f)) == key)
        state = fn(state, fi)
    return state


def signature(state):
    verts, faces, _ = state
    return len(verts), len(faces)


EDITS = {"alice": ts.split_center, "bob": None}


def expected():
    EDITS["bob"] = BOB_EDIT
    start = ts.load(ts.host_start((*ts.grid(), None)))
    out = {n: signature(region_edit(start, fn)) for n, fn in EDITS.items()}
    out["alice,bob"] = signature(region_edit(region_edit(start, EDITS["alice"]), EDITS["bob"]))
    out["bob,alice"] = signature(region_edit(region_edit(start, EDITS["bob"]), EDITS["alice"]))
    return out


def run_one(seed, gap):
    sim = Sim(seed, gap)
    sim.run()
    meshes = {n: ts.id_mesh(ts.dump_of(*c.state)) for n, c in sim.clients.items()}
    server = ts.id_mesh(sim.stored)
    same = all(m == server for m in meshes.values())
    sig = (len(server["verts"]), len(server["faces"]))
    exp = expected()
    first, second = sim.edited
    if sim.saw[second] == exp[first] or sim.saw[second] == exp[",".join(sim.edited)]:
        # the second edit was made on top of the first: both must be kept, in order
        allowed = [exp[",".join(sim.edited)]]
        kind = "sequential"
    else:
        allowed = [exp["alice"], exp["bob"]]
        kind = "clash"
    sim.kind = kind
    ok = same and sig in allowed
    return ok, same, sig, sim.kind


BOB_EDIT = inset if os.environ.get("BOB", "inset") == "inset" else ts.split_diagonal

if __name__ == "__main__":
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    first = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    print("expected", expected())
    bad = 0
    for seed in range(first, first + runs):
        gap = random.Random(seed * 7 + 1).uniform(-0.6, 0.6)
        ok, same, sig, edited = run_one(seed, gap)
        if not ok:
            bad += 1
            print(f"FAIL seed {seed} gap {gap:+.3f} edited {edited}: converged={same} server mesh {sig}")
    print(f"{bad} of {runs} runs failed")
    sys.exit(1 if bad else 0)
