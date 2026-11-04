"""Offline simulation of v3 (merge3 + topo3) with Multi-User mesh dumps. No Blender.

Run from anywhere: python tests/test_sim.py
It copies merge3.py and topo3.py into a temporary `replication` package so the
relative imports work as they do inside the add-on.
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest
import zlib

import numpy as np

# MU_SRC=<folder with merge3.py and topo3.py> runs the tests on other code, for
# example the previous release, to show which tests fail there.
HERE = os.environ.get("MU_SRC") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "replication")
PKG = tempfile.mkdtemp()
os.makedirs(os.path.join(PKG, "replication"))
open(os.path.join(PKG, "replication", "__init__.py"), "w").close()
for name in ("merge3.py", "topo3.py"):
    shutil.copy(os.path.join(HERE, name), os.path.join(PKG, "replication", name))
sys.path.insert(0, PKG)
from replication import merge3, topo3  # noqa: E402

S = merge3.STAMP_KEY


# ---------------------------------------------------------------- dumps
def dump_of(verts, faces, ids=None):
    """A Multi-User style mesh dump. verts: [(x,y,z)], faces: [[index,...]]."""
    edges = sorted({tuple(sorted((f[i], f[(i + 1) % len(f)]))) for f in faces for i in range(len(f))})
    vi, ls, lt = [], [], []
    for f in faces:
        ls.append(len(vi))
        lt.append(len(f))
        vi.extend(f)
    d = {
        "name": "Grid",
        "vertex_count": len(verts),
        "vertices": {"co": np.array(verts, dtype=np.float32).tobytes()},
        "egdes_count": len(edges),
        "edges": {"vertices": np.array(edges, dtype=np.int32).tobytes(),
                  "use_seam": np.zeros(len(edges), dtype=bool).tobytes(),
                  "use_edge_sharp": np.zeros(len(edges), dtype=bool).tobytes()},
        "loop_count": len(vi),
        "loops": {"vertex_index": np.array(vi, dtype=np.int32).tobytes(),
                  "normal": np.zeros(len(vi) * 3, dtype=np.float32).tobytes()},
        "poly_count": len(faces),
        "polygons": {"loop_total": np.array(lt, dtype=np.int32).tobytes(),
                     "loop_start": np.array(ls, dtype=np.int32).tobytes(),
                     "use_smooth": np.zeros(len(faces), dtype=bool).tobytes(),
                     "material_index": np.zeros(len(faces), dtype=np.int32).tobytes()},
        "attributes": [],
        "materials": [],
    }
    if ids is not None:
        d["attributes"] = [["mu_vid", "INT", "POINT", {"value": np.array(ids, dtype=np.int32).tobytes()}]]
    return d


def load(d):
    """What a datablock holds after loading d: (verts, faces, ids) in d's order."""
    d = topo3.drop(merge3.strip(d))
    co = np.frombuffer(d["vertices"]["co"], dtype=np.float32).reshape(-1, 3)
    vi = np.frombuffer(d["loops"]["vertex_index"], dtype=np.int32)
    ls = np.frombuffer(d["polygons"]["loop_start"], dtype=np.int32)
    lt = np.frombuffer(d["polygons"]["loop_total"], dtype=np.int32)
    faces = [list(vi[s:s + t]) for s, t in zip(ls, lt)]
    ids = topo3.get_ids(d)
    return [tuple(float(c) for c in p) for p in co], faces, (list(ids) if ids is not None else None)


def grid(n=4):
    verts = [(float(x), float(y), 0.0) for y in range(n + 1) for x in range(n + 1)]
    idx = lambda x, y: x + y * (n + 1)
    faces = [[idx(x, y), idx(x + 1, y), idx(x + 1, y + 1), idx(x, y + 1)] for y in range(n) for x in range(n)]
    return verts, faces


def find_face(verts, faces, ids, corner_ids):
    want = set(corner_ids)
    for i, f in enumerate(faces):
        if {ids[v] for v in f} == want:
            return i
    raise KeyError(corner_ids)


# Blender-like edits: new vertices copy the id of a vertex they came from.
def split_center(state, fi):
    verts, faces, ids = copy.deepcopy(state)
    f = faces.pop(fi)
    verts.append(tuple(sum(verts[v][k] for v in f) / len(f) for k in range(3)))
    ids.append(ids[f[0]])
    c = len(verts) - 1
    faces += [[f[i], f[(i + 1) % len(f)], c] for i in range(len(f))]
    return verts, faces, ids


def split_diagonal(state, fi):
    verts, faces, ids = copy.deepcopy(state)
    f = faces.pop(fi)
    faces += [[f[0], f[1], f[2]], [f[0], f[2], f[3]]]
    return verts, faces, ids


def extrude(state, fi, h=1.0):
    verts, faces, ids = copy.deepcopy(state)
    f = faces.pop(fi)
    top = []
    for v in f:
        x, y, z = verts[v]
        verts.append((x, y, z + h))
        ids.append(ids[v])
        top.append(len(verts) - 1)
    faces.append(top)
    for i in range(len(f)):
        j = (i + 1) % len(f)
        faces.append([f[i], f[j], top[j], top[i]])
    return verts, faces, ids


def move(state, vid, pos):
    verts, faces, ids = copy.deepcopy(state)
    verts[ids.index(vid)] = pos
    return verts, faces, ids


# Delete-type edits. Blender keeps the order of the elements that stay and
# renumbers them; a face that an operator creates goes last.
def _compact(verts, faces, ids, dead):
    index, nv, nids = {}, [], []
    for i, (p, vid) in enumerate(zip(verts, ids)):
        if i not in dead:
            index[i] = len(nv)
            nv.append(p)
            nids.append(vid)
    return nv, [[index[v] for v in f] for f in faces], nids


def delete_faces(state, fis):
    """Delete > Faces: the faces go, and every vertex that only they used."""
    verts, faces, ids = copy.deepcopy(state)
    gone = set(fis)
    rest = [f for i, f in enumerate(faces) if i not in gone]
    used = {v for f in rest for v in f}
    dead = {v for i in gone for v in faces[i]} - used
    return _compact(verts, rest, ids, dead)


def delete_vert(state, vi):
    """Delete > Vertices: the vertex goes with every face around it."""
    verts, faces, ids = copy.deepcopy(state)
    rest = [f for f in faces if vi not in f]
    return _compact(verts, rest, ids, {vi})


def dissolve(state, fi, fj):
    """Dissolve Edges on an edge two faces share: one polygon replaces them.
    Faces that share several edges dissolve the one with the lowest points."""
    verts, faces, ids = copy.deepcopy(state)
    f, g = faces[fi], faces[fj]
    shared = [(f[k], f[(k + 1) % len(f)]) for k in range(len(f))
              if f[k] in g and g[(g.index(f[k]) - 1) % len(g)] == f[(k + 1) % len(f)]]
    if not shared:
        raise ValueError("faces share no edge")
    a, b = min(shared, key=lambda e: sorted([tuple(verts[e[0]]), tuple(verts[e[1]])]))
    fr = f[f.index(b):] + f[:f.index(b)]          # b ... a
    gr = g[g.index(a):] + g[:g.index(a)]          # a ... b
    joined = fr + gr[1:-1]
    rest = [x for i, x in enumerate(faces) if i not in (fi, fj)]
    return verts, rest + [joined], ids


def merge_verts(state, va, vb, interp_id=False):
    """Merge > At Center: vb joins va at their midpoint. With interp_id the kept
    vertex gets the average id rounded through float32, as Blender interpolates
    an integer attribute; otherwise it keeps its own id."""
    verts, faces, ids = copy.deepcopy(state)
    verts[va] = tuple((verts[va][k] + verts[vb][k]) / 2 for k in range(3))
    if interp_id:
        ids[va] = int(round(float(np.float32(ids[va]) * np.float32(0.5) + np.float32(ids[vb]) * np.float32(0.5))))
    out = []
    for f in faces:
        g = [va if v == vb else v for v in f]
        g = [v for k, v in enumerate(g) if v != g[k - 1]]
        if len(set(g)) >= 3:
            out.append(g)
    return _compact(verts, out, ids, {vb})


# ---------------------------------------------------------------- what Blender holds
class LoadError(Exception):
    """What Blender raises (or crashes on) for a dump whose parts disagree."""


def check_dump(d):
    """Raise LoadError when bl_mesh.load would fail: counts that do not match
    the buffers ("Array length mismatch"), or indices out of range."""
    d = topo3.drop(merge3.strip(d))
    nv, ne, nl, np_ = d["vertex_count"], d["egdes_count"], d["loop_count"], d["poly_count"]
    sizes = [(d["vertices"]["co"], nv * 12), (d["edges"]["vertices"], ne * 8),
             (d["loops"]["vertex_index"], nl * 4), (d["polygons"]["loop_start"], np_ * 4),
             (d["polygons"]["loop_total"], np_ * 4)]
    for entry in d.get("attributes") or []:
        sizes.append((entry[3]["value"], nv * 4))
    for buf, want in sizes:
        if len(buf) != want:
            raise LoadError(f"Array length mismatch (expected {want // 4}, got {len(buf) // 4})")
    vi = np.frombuffer(d["loops"]["vertex_index"], dtype=np.int32)
    lt = np.frombuffer(d["polygons"]["loop_total"], dtype=np.int32)
    ls = np.frombuffer(d["polygons"]["loop_start"], dtype=np.int32)
    if int(lt.sum()) != nl or (len(ls) and (ls.min() < 0 or (ls + lt).max() > nl)):
        raise LoadError("loops do not match the polygons")
    if len(vi) and (vi.min() < 0 or vi.max() >= nv):
        raise LoadError("a loop points past the vertices")


def strict_load(d):
    check_dump(d)
    return load(d)


def geometry(state):
    """The mesh without ids: sorted vertex positions and faces as rotated
    tuples of positions, so two meshes compare equal only if every face sits on
    the same points in the same winding."""
    verts, faces, _ = state
    r = lambda p: tuple(round(float(c), 4) for c in p)
    out = []
    for f in faces:
        pts = [r(verts[v]) for v in f]
        k = pts.index(min(pts))
        out.append(tuple(pts[k:] + pts[:k]))
    return sorted(r(p) for p in verts), sorted(out)


class Clock:
    now = 0.0

    def time(self):
        return Clock.now


_peer_modules = {}


def fresh_modules(tag):
    """merge3 and topo3 keep module state, so every peer gets its own copy."""
    pkg = f"rep_{tag}"
    os.makedirs(os.path.join(PKG, pkg), exist_ok=True)
    open(os.path.join(PKG, pkg, "__init__.py"), "w").close()
    for name in ("merge3.py", "topo3.py"):
        shutil.copy(os.path.join(HERE, name), os.path.join(PKG, pkg, name))
    for mod in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        del sys.modules[mod]
    import importlib
    m3 = importlib.import_module(f"{pkg}.merge3")
    m3.time = Clock()
    return m3


class InEditMode(Exception):
    """bl_mesh.dump and bl_mesh.load raise ContextError while in Edit Mode."""


class Peer:
    """One Blender running the real hooks in the order porcelain calls them:
    stamp_commit on commit, merge_incoming on receive, merge_fetched before a
    load. The datablock is `state`; loading checks the dump as Blender would."""

    def __init__(self, name, synced, tag=None, dump=None):
        self.name = name
        self.m3 = fresh_modules(tag or name)
        self.m3.topo3._username = name
        self.node = type("Node", (), {})()
        self.node.uuid, self.node.instance, self.node.state = "mesh", object(), "UP"
        self.node.data = copy.deepcopy(synced)
        self.m3.remember(self.node.uuid, self.node.data)
        self.edit_mode = False
        self.rdp = self
        self._dump = dump or (lambda state: dump_of(*state))
        self.smooth = self.skin = None
        self._load(synced)

    def _load(self, d):
        """What bl_mesh.load leaves: the mesh, its face smooth flags and its skin rows."""
        self.state = strict_load(d)
        d = topo3.drop(merge3.strip(d))
        self.smooth = list(np.frombuffer(d["polygons"].get("use_smooth") or b"", dtype=bool))
        skin = (d.get("mu_skin") or {}).get("data")
        self.skin = np.frombuffer(skin, dtype=np.float32).reshape(-1, 4).copy() if skin else None

    def dump(self, instance=None, stamp_uuid=None):
        if self.edit_mode:
            raise InEditMode()
        d = self._dump(self.state)
        if self.smooth is not None and len(self.smooth) == len(self.state[1]):
            d["polygons"]["use_smooth"] = np.array(self.smooth, dtype=bool).tobytes()
        if self.skin is not None and len(self.skin) == len(self.state[0]):
            d["mu_skin"] = {"data": np.asarray(self.skin, dtype=np.float32).tobytes()}
        return d

    def radii(self):
        """Skin radius x by rounded vertex position, None when the skin rows do
        not match the vertices."""
        if self.skin is None or len(self.skin) != len(self.state[0]):
            return None
        return {tuple(round(c, 4) for c in p): round(float(r[0]), 4) for p, r in zip(self.state[0], self.skin)}

    def smooth_faces(self):
        """Rounded corner points of the smooth faces."""
        g = geometry((self.state[0], [f for f, s in zip(self.state[1], self.smooth) if s], None))
        return g[1]

    def commit(self):
        """porcelain.commit and push. Returns the data sent, or None."""
        if self.node.state != "UP" or self.edit_mode:
            return None
        new = self.m3.stamp_commit(self.node, self.dump())
        if self.m3._eq(new, self.node.data):
            self.m3._commit_base.pop(self.node.uuid, None)
            return None
        self.node.data = new
        self.m3.after_commit(self.node.uuid, self.node.data)
        self.node.state = self.m3.state_after_push(self.node.uuid, "UP", "FETCHED")
        return copy.deepcopy(self.node.data)

    def receive(self, data):
        self.node.data = self.m3.merge_incoming(self.node.data, data)
        self.node.state = "FETCHED"

    def apply(self):
        """porcelain.apply. Returns the data a published merge sent, or None.
        Raises InEditMode, or LoadError where Blender would fail."""
        if self.node.state != "FETCHED":
            return None
        to_load, contributed = self.m3.merge_fetched(self, self.node)
        if self.edit_mode:
            raise InEditMode()
        self._load(to_load)
        self.node.state = "UP"
        last = self.m3._last_merge.pop(self.node.uuid, None)
        if last is not None:
            self.m3.remember_merged(self.node.uuid, last[0], last[1], contributed)
        else:
            self.m3.remember(self.node.uuid, self.node.data)
        if contributed and self.m3.PUBLISH_MERGES:
            return self.commit()
        return None


# ---------------------------------------------------------------- the protocol
class Client:
    def __init__(self, number, synced):
        self.n = number
        self.base = copy.deepcopy(synced)      # merge3._base
        self.state = load(synced)               # the datablock

    def commit(self, now):
        """stamp_commit with nothing stored: attach, stamp, become the new base."""
        dump = dump_of(*self.state)
        prev = merge3.strip(self.base)
        d, info = topo3.attach(prev, dump, now, self.n, getattr(self, "name", None))
        ts = merge3.stamp(prev, merge3.stamps_of(self.base), merge3.strip(d), now)
        d[S] = topo3.fix_co_stamps(prev, merge3.stamps_of(self.base), d, ts, now)
        self.base = copy.deepcopy(d)
        if info and info["ids_changed"]:
            self.state = load(d)  # the reapply writes the repaired ids back
        return d

    def apply(self, remote, now):
        """merge_fetched + load."""
        dump = dump_of(*self.state)
        local, _ = topo3.attach(merge3.strip(self.base), dump, now, self.n, getattr(self, "name", None))
        lts = merge3.stamp(merge3.strip(self.base), merge3.stamps_of(self.base), local, now)
        lts = topo3.fix_co_stamps(merge3.strip(self.base), merge3.stamps_of(self.base), local, lts, now)
        merged, mts = merge3.lww(local, lts, merge3.strip(remote), merge3.stamps_of(remote))
        merged = dict(merged)
        merged[S] = mts
        self.base = copy.deepcopy(merged)
        self.state = load(merged)
        return merged


def host_start(state, now=1.0):
    d, _ = topo3.attach(None, dump_of(*state), now, 9)
    d[S] = merge3.stamp(merge3._MISSING, None, merge3.strip(d), now)
    return d


def server(*updates):
    stored = None
    for u in updates:
        stored = u if stored is None else merge3.merge_incoming(stored, u)
    return stored


def id_mesh(d):
    d = topo3.drop(merge3.strip(d))
    m = topo3.decode(d, topo3.get_ids(d))
    return {"verts": {k: tuple(round(c, 4) for c in v) for k, v in m["verts"].items()},
            "faces": set(m["faces"]), "edges": m["edges"]}


def corner_ids(n=4):
    vid = lambda x, y: 1 + x + y * (n + 1)
    return lambda x, y: (vid(x, y), vid(x + 1, y), vid(x + 1, y + 1), vid(x, y + 1))


Q = corner_ids()


class SimTests(unittest.TestCase):
    def setUp(self):
        merge3._superseded.clear()
        self.start = host_start((*grid(), None))
        self.a = Client(1, self.start)
        self.b = Client(2, self.start)

    def edit(self, client, fn, quad, *args):
        v, f, ids = client.state
        client.state = fn(client.state, find_face(v, f, ids, Q(*quad)), *args)

    def test_initial_ids(self):
        self.assertEqual(self.a.state[2], list(range(1, 26)))

    def test_disjoint_converges_both_orders(self):
        self.edit(self.a, split_center, (0, 0))
        self.edit(self.b, extrude, (3, 3))
        ca, cb = self.a.commit(2.0), self.b.commit(2.1)
        s1, s2 = server(self.start, ca, cb), server(self.start, cb, ca)
        self.assertEqual(id_mesh(s1), id_mesh(s2))
        m = id_mesh(s1)
        self.assertEqual(len(m["verts"]), 25 + 1 + 4)
        self.assertEqual(len(m["faces"]), 16 - 1 + 4 - 1 + 5)
        # clients apply the other's commit and end equal to the server
        self.a.apply(cb, 3.0)
        self.b.apply(ca, 3.0)
        self.assertEqual(id_mesh(self.a.base), id_mesh(s1))
        self.assertEqual(id_mesh(self.b.base), id_mesh(s1))

    def test_conflict_newer_wins_region_far_edit_kept(self):
        self.edit(self.a, split_center, (1, 1))
        self.edit(self.a, extrude, (3, 3))
        self.edit(self.b, split_diagonal, (1, 1))
        ca, cb = self.a.commit(2.0), self.b.commit(2.1)
        s = server(self.start, ca, cb)
        self.assertEqual(id_mesh(s), id_mesh(server(self.start, cb, ca)))
        m = id_mesh(s)
        q = Q(1, 1)
        self.assertIn(topo3.face_key((q[0], q[1], q[2])), m["faces"])   # b's diagonal
        self.assertEqual(sum(1 for f in m["faces"] if len(f) == 3), 2)   # a's 4 triangles gone
        self.assertEqual(len(m["verts"]), 25 + 4)                        # a's extrude kept
        self.a.apply(s, 3.0)
        self.b.apply(s, 3.0)
        self.assertEqual(id_mesh(self.a.base), m)
        self.assertEqual(id_mesh(self.b.base), m)

    def test_settle_after_conflict(self):
        self.edit(self.a, split_center, (1, 1))
        self.edit(self.b, split_diagonal, (1, 1))
        s = server(self.start, self.a.commit(2.0), self.b.commit(2.1))
        self.a.apply(s, 3.0)
        before = id_mesh(self.a.base)
        settle = self.a.commit(4.0)   # the next commit tombstones the hidden elements
        self.assertEqual(id_mesh(settle), before)
        self.assertEqual(topo3.view(settle[topo3.TOPO_KEY])["conflicts"], 0)
        self.b.apply(settle, 5.0)
        self.assertEqual(id_mesh(self.b.base), before)

    def test_sequential_edit_on_new_geometry(self):
        self.edit(self.a, extrude, (2, 2))
        ca = self.a.commit(2.0)
        self.b.apply(ca, 2.5)
        # b now splits the extruded top face, which uses a's new ids
        v, f, ids = self.b.state
        top = [i for i, face in enumerate(f) if all(ids[x] >> 23 == 1 for x in face)][0]
        self.b.state = split_center(self.b.state, top)
        cb = self.b.commit(3.0)
        s = server(self.start, ca, cb)
        self.assertEqual(topo3.view(s[topo3.TOPO_KEY])["conflicts"], 0)
        m = id_mesh(s)
        self.assertEqual(len(m["verts"]), 25 + 4 + 1)
        self.a.apply(cb, 4.0)
        self.assertEqual(id_mesh(self.a.base), m)

    def test_moves_merge_on_fast_path(self):
        self.a.state = move(self.a.state, 7, (1.0, 1.0, 0.5))
        self.b.state = move(self.b.state, 19, (3.0, 3.0, 0.7))
        s = server(self.start, self.a.commit(2.0), self.b.commit(2.1))
        m = id_mesh(s)
        self.assertEqual(m["verts"][7], (1.0, 1.0, 0.5))
        self.assertEqual(m["verts"][19], (3.0, 3.0, 0.7))
        self.assertEqual(len(m["faces"]), 16)

    def test_move_survives_concurrent_topology_edit(self):
        self.a.state = move(self.a.state, 7, (1.0, 1.0, 0.5))
        self.edit(self.b, extrude, (3, 3))
        s = server(self.start, self.a.commit(2.0), self.b.commit(2.1))
        m = id_mesh(s)
        self.assertEqual(m["verts"][7], (1.0, 1.0, 0.5))
        self.assertEqual(len(m["verts"]), 29)

    def test_repair_is_repeatable(self):
        self.edit(self.a, extrude, (1, 2))
        raw = dump_of(*self.a.state)
        d1, i1 = topo3.attach(merge3.strip(self.a.base), raw, 2.0, 1)
        d2, i2 = topo3.attach(d1, raw, 3.0, 1)
        self.assertEqual(list(topo3.get_ids(d1)), list(topo3.get_ids(d2)))
        self.assertFalse(i2["topology_changed"])

    def test_delete_vs_subdivide_elsewhere(self):
        v, f, ids = self.a.state
        fi = find_face(v, f, ids, Q(0, 3))
        self.a.state = (v, [x for i, x in enumerate(f) if i != fi], ids)
        self.edit(self.b, split_center, (3, 0))
        s = server(self.start, self.a.commit(2.0), self.b.commit(2.1))
        m = id_mesh(s)
        self.assertEqual(len(m["faces"]), 16 - 1 - 1 + 4)
        self.assertEqual(topo3.view(s[topo3.TOPO_KEY])["conflicts"], 0)

    def test_three_clients_converge(self):
        c = Client(3, self.start)
        self.edit(self.a, split_center, (0, 0))
        self.edit(self.b, extrude, (3, 3))
        self.edit(c, split_diagonal, (0, 3))
        ups = [self.a.commit(2.0), self.b.commit(2.1), c.commit(2.2)]
        results = [id_mesh(server(self.start, *order)) for order in
                   (ups, ups[::-1], [ups[1], ups[0], ups[2]])]
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(results[0]["faces"]), 16 + 3 + 4 + 1)

    def test_first_edits_before_any_table(self):
        """The synced mesh has no ids and no table yet, as when clients join a
        host that never committed the mesh. Both edit at once; nothing conflicts."""
        verts, faces = grid()
        plain = dump_of(verts, faces, None)
        plain[S] = merge3.stamp(merge3._MISSING, None, merge3.strip(plain), 1.0)
        index_ids = list(range(1, 26))
        sa = extrude((verts, faces, index_ids), find_face(verts, faces, index_ids, Q(3, 3)))
        sb = split_center((verts, faces, index_ids), find_face(verts, faces, index_ids, Q(0, 0)))

        def first_commit(state, client, now):
            prev = merge3.strip(plain)
            d, info = topo3.attach(prev, dump_of(state[0], state[1], None), now, client)
            ts = merge3.stamp(prev, merge3.stamps_of(plain), merge3.strip(d), now)
            d[S] = topo3.fix_co_stamps(prev, merge3.stamps_of(plain), d, ts, now)
            self.assertTrue(info["topology_changed"])
            return d

        ca, cb = first_commit(sa, 1, 2.0), first_commit(sb, 2, 2.1)
        s1, s2 = server(ca, cb), server(cb, ca)
        self.assertEqual(id_mesh(s1), id_mesh(s2))
        self.assertEqual(topo3.view(s1[topo3.TOPO_KEY])["conflicts"], 0)
        m = id_mesh(s1)
        self.assertEqual(len(m["verts"]), 25 + 4 + 1)
        self.assertEqual(len(m["faces"]), 16 - 1 + 5 - 1 + 4)

    def test_vertex_color_mesh_left_alone(self):
        d = dump_of(*grid(), None)
        d["vertex_colors"] = {"Col": {"data": b"\0" * 16}}
        out, info = topo3.attach(None, d, 1.0, 1)
        self.assertIsNone(info)
        self.assertNotIn(topo3.TOPO_KEY, out)

    def test_uvs_carried_through_rebuild(self):
        """UV of each corner = its vertex's x, y. Every corner must match after a merge."""
        def uvs_for(d):
            d = topo3.drop(merge3.strip(d))
            co = np.frombuffer(d["vertices"]["co"], dtype=np.float32).reshape(-1, 3)
            vi = np.frombuffer(d["loops"]["vertex_index"], dtype=np.int32)
            return np.array([co[i][:2] for i in vi], dtype=np.float32)

        def commit_uv(c, now):
            dump = dump_of(*c.state)
            dump["uv_layers"] = {"UVMap": {"data": uvs_for(dump).tobytes()}}
            prev = merge3.strip(c.base)
            x, _ = topo3.attach(prev, dump, now, c.n)
            ts = merge3.stamp(prev, merge3.stamps_of(c.base), merge3.strip(x), now)
            x[S] = topo3.fix_co_stamps(prev, merge3.stamps_of(c.base), x, ts, now)
            return x

        start = dump_of(*grid(), None)
        start["uv_layers"] = {"UVMap": {"data": uvs_for(start).tobytes()}}
        d, info = topo3.attach(None, start, 1.0, 9)
        self.assertIsNotNone(info)
        d[S] = merge3.stamp(merge3._MISSING, None, merge3.strip(d), 1.0)
        a, b = Client(1, d), Client(2, d)
        self.edit(a, split_center, (0, 0))
        self.edit(b, extrude, (3, 3))
        s = server(d, commit_uv(a, 2.0), commit_uv(b, 2.1))
        out = topo3.drop(merge3.strip(s))
        self.assertIn("uv_layers", out)
        got = np.frombuffer(out["uv_layers"]["UVMap"]["data"], dtype=np.float32).reshape(-1, 2)
        self.assertEqual(len(got), out["loop_count"])
        self.assertTrue(np.allclose(got, uvs_for(out), atol=1e-6))


def named(name, synced):
    c = Client(None, synced)
    c.name = name
    return c


def entries(d):
    t = d[topo3.TOPO_KEY]
    return sum(len(t[sec]) for sec in ("v", "f", "e")), len(t["c"])


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        merge3._superseded.clear()
        merge3.NOTICES.clear()
        merge3._noticed.clear()
        self.start = host_start((*grid(), None))

    def edit(self, client, fn, quad, *args):
        v, f, ids = client.state
        client.state = fn(client.state, find_face(v, f, ids, Q(*quad)), *args)

    @staticmethod
    def colliding_names():
        seen = {}
        for i in range(100000):
            name = f"user{i}"
            h = zlib.crc32(name.encode()) % topo3.MAX_CLIENT
            if h in seen:
                return seen[h], name
            seen[h] = name

    def test_claim_skips_a_taken_number_and_is_stable(self):
        x, y = self.colliding_names()
        topo = {}
        nx, ny = topo3.claim(topo, x, 1.0), topo3.claim(topo, y, 2.0)
        self.assertNotEqual(nx, ny)
        self.assertEqual(topo3.claim(topo, x, 3.0), nx)
        self.assertEqual(topo3.name_of(topo, ny), y)

    def test_colliding_names_create_distinct_ids(self):
        """Two usernames that hash to one number: the second to join takes the
        next number, so the vertices both create get different ids."""
        x, y = self.colliding_names()
        a, b = named(x, self.start), named(y, self.start)
        self.edit(a, extrude, (3, 3))
        ca = a.commit(2.0)
        b.apply(ca, 2.5)
        self.edit(a, split_center, (0, 3))
        self.edit(b, split_center, (0, 0))
        ca2, cb = a.commit(3.0), b.commit(3.1)
        reg = cb[topo3.TOPO_KEY]["n"]
        self.assertEqual(len({e[0] for e in reg.values()}), 2)
        s1, s2 = server(self.start, ca, ca2, cb), server(self.start, ca, cb, ca2)
        self.assertEqual(id_mesh(s1), id_mesh(s2))
        m = id_mesh(s1)
        self.assertEqual(len(m["verts"]), 25 + 4 + 1 + 1)   # extrude, then one centre each
        self.assertEqual(topo3.view(s1[topo3.TOPO_KEY])["conflicts"], 0)

    def test_prune_drops_settled_commit_and_its_tombstones(self):
        a, b = Client(1, self.start), Client(2, self.start)
        self.edit(a, split_center, (0, 0))
        ca = a.commit(10.0)
        b.apply(ca, 11.0)
        self.edit(b, extrude, (3, 3))
        cb = b.commit(200.0)
        old = topo3.PRUNE_SETTLE
        try:
            topo3.PRUNE_SETTLE = 1e9
            full = server(self.start, ca, cb)
        finally:
            topo3.PRUNE_SETTLE = old
        small = server(self.start, ca, cb)
        self.assertEqual(id_mesh(small), id_mesh(full))
        self.assertNotIn(next(iter(ca[topo3.TOPO_KEY]["c"])), small[topo3.TOPO_KEY]["c"])
        self.assertLess(entries(small)[0], entries(full)[0])     # the split face's tombstone
        self.assertEqual(entries(small)[1], 1)                   # b's commit is not settled yet
        # an unpruned replica merging the pruned one ends the same
        self.assertEqual(id_mesh(server(full, small)), id_mesh(small))

    def test_prune_keeps_unsettled_conflict(self):
        """Losers of a conflict stay in the table until a later commit settles it."""
        a, b = Client(1, self.start), Client(2, self.start)
        self.edit(a, split_center, (1, 1))
        self.edit(b, split_diagonal, (1, 1))
        s = server(self.start, a.commit(10.0), b.commit(11.0))
        before = id_mesh(s)
        self.assertEqual(len(s[topo3.TOPO_KEY]["c"]), 2)
        c = Client(3, s)
        self.edit(c, extrude, (3, 3))
        s2 = server(s, c.commit(1000.0))                         # a and b are idle now
        m = id_mesh(s2)
        self.assertEqual(len(m["verts"]), len(before["verts"]) + 4)
        self.assertTrue(before["faces"] - m["faces"] <= {topo3.face_key(Q(3, 3))})
        self.assertEqual(topo3.view(s2[topo3.TOPO_KEY])["conflicts"], 0)
        self.assertEqual(len(s2[topo3.TOPO_KEY]["c"]), 1)        # only c's commit is left

    def test_prune_never_changes_the_mesh(self):
        """With no settle time and no idle limit, b's winning commit counts as
        settled. Pruning it would bring a's dropped edit back, so it stays."""
        a, b = Client(1, self.start), Client(2, self.start)
        self.edit(a, split_center, (1, 1))
        self.edit(b, split_diagonal, (1, 1))
        full = server(self.start, a.commit(10.0), b.commit(11.0))
        expected = id_mesh(full)
        old = topo3.PRUNE_SETTLE, topo3.PRUNE_AGE
        try:
            topo3.PRUNE_SETTLE, topo3.PRUNE_AGE = 0.0, 0.0
            t = full[topo3.TOPO_KEY]
            self.assertEqual(len(topo3.prune(t)["c"]), 1)        # prune alone would drop b's
            s = server(full, full)                               # merge_mesh checks the view
        finally:
            topo3.PRUNE_SETTLE, topo3.PRUNE_AGE = old
        self.assertEqual(id_mesh(s), expected)
        self.assertEqual(len(s[topo3.TOPO_KEY]["c"]), 2)

    def test_concurrent_settles_are_not_a_new_conflict(self):
        """Both clients apply the resolved mesh and commit it at once, as in the
        live run. Those commits only write the resolution down."""
        a, b = named("alice", self.start), named("bob", self.start)
        self.edit(a, split_center, (1, 1))
        self.edit(b, split_diagonal, (1, 1))
        s = server(self.start, a.commit(2.0), b.commit(2.1))
        merge3._notice_clashes("Grid", *topo3.LAST)
        a.apply(s, 3.0)
        b.apply(s, 3.0)
        sa, sb = a.commit(4.0), b.commit(4.1)
        flags = [c[3] for d in (sa, sb) for cid, c in d[topo3.TOPO_KEY]["c"].items()
                 if c[1] >= 4.0 and len(c) > 3]
        self.assertEqual(flags, [1, 1])
        s2 = server(s, sa, sb)
        merge3._notice_clashes("Grid", *topo3.LAST)
        self.assertEqual(len(merge3.NOTICES), 1)
        self.assertEqual(id_mesh(s2), id_mesh(s))
        self.assertEqual(id_mesh(server(s, sb, sa)), id_mesh(s))
        # a client that only ever saw the settled table still hears about it
        merge3.NOTICES.clear()
        merge3._noticed.clear()
        t = s2[topo3.TOPO_KEY]
        merge3._notice_clashes("Grid", {"clashes": []}, t)
        self.assertEqual(len(merge3.NOTICES), 1)
        self.assertIn("Kept bob's edit", merge3.NOTICES[0][1])

    def test_repeated_repair_gives_the_same_ids(self):
        """An edit made new vertices that copied ids, at coordinates float32
        cannot hold exactly. The commit repaired them, and an update arrived
        before the repaired ids were loaded back. Attaching the same unrepaired
        dump again must give the same ids and no topology change."""
        state = load(self.start)
        v, f, ids = state
        inner = []
        fi = find_face(v, f, ids, Q(1, 1))
        quad = f[fi]
        verts, faces, ids = copy.deepcopy(state)
        faces.pop(fi)
        for k in quad:
            x, y, z = verts[k]
            verts.append((x + (1.5 - x) * 0.2, y + (1.5 - y) * 0.2, z))  # 1.1, 1.2 ...: inexact in float32
            ids.append(ids[k])
            inner.append(len(verts) - 1)
        faces.append(inner)
        faces += [[quad[i], quad[(i + 1) % 4], inner[(i + 1) % 4], inner[i]] for i in range(4)]
        dump = dump_of(verts, faces, ids)
        base = merge3.strip(self.start)
        committed, info = topo3.attach(base, dump, 2.0, 1)
        self.assertTrue(info["topology_changed"])
        again, info2 = topo3.attach(committed, dump, 3.0, 1)
        self.assertEqual(topo3.get_ids(again).tolist(), topo3.get_ids(committed).tolist())
        self.assertFalse(info2["topology_changed"])

    def test_repair_recovers_ids_blender_rounded(self):
        """Blender passes the id attribute through float32 in inset and poke, so
        large ids of untouched vertices lose their low bits. Repair must give
        them their ids back even where the vertex also moved."""
        big = [(1 << 23) * 186 + k for k in range(1, 26)]
        verts, faces = grid()
        dump = dump_of(verts, faces, big)
        base, _ = topo3.attach(None, dump, 1.0, 186)
        rounded = [int(np.float32(i)) for i in big]
        self.assertGreater(len(big) - len(set(rounded)), 10)
        moved = list(verts)
        moved[6] = (moved[6][0] + 0.01, moved[6][1], 0.0)
        out, info = topo3.attach(base, dump_of(moved, faces, rounded), 2.0, 186)
        self.assertEqual(topo3.get_ids(out).tolist(), big)
        self.assertFalse(info["topology_changed"])

    def test_conflict_notice_names_both_users_once(self):
        a, b = named("alice", self.start), named("bob", self.start)
        self.edit(a, split_center, (1, 1))
        self.edit(b, split_diagonal, (1, 1))
        ca, cb = a.commit(2.0), b.commit(2.1)
        server(self.start, ca, cb)
        merge3._notice_clashes("Grid", *topo3.LAST)
        merge3._notice_clashes("Grid", *topo3.LAST)
        self.assertEqual(len(merge3.NOTICES), 1)
        text = merge3.NOTICES[0][1]
        self.assertIn("alice and bob", text)
        self.assertIn("Kept bob's edit", text)


def consistent(d):
    try:
        check_dump(d)
        return True
    except LoadError:
        return False


class DeleteRaceTests(unittest.TestCase):
    """mu5: delete-type edits racing an update for another part of the mesh."""

    def setUp(self):
        Clock.now = 1.0
        self.start = host_start((*grid(), None))
        self.tags = iter(range(10 ** 6))

    def peer(self, name, synced=None):
        return Peer(name, self.start if synced is None else synced, tag=f"{name}{next(self.tags)}_{id(self)}")

    def at(self, t):
        Clock.now = t

    def face(self, p, quad):
        v, f, ids = p.state
        return find_face(v, f, ids, Q(*quad))

    def test_count_seen_before_never_reaches_a_load(self):
        """Bob splits a quad (16 to 17 faces) and Alice applies it, so her
        16-face dump is remembered as superseded. She then deletes a face far
        away (17 to 16). The stale-restore check took her new face count for a
        stale one and put 17 back over buffers of 16 faces: Blender's
        "Array length mismatch (expected 697, got 693)" and a crash."""
        alice, bob = self.peer("alice"), self.peer("bob")
        self.at(2.0)
        bob.state = split_diagonal(bob.state, self.face(bob, (3, 3)))
        ub = bob.commit()
        self.at(2.5)
        alice.receive(ub)
        alice.apply()
        self.assertEqual(len(alice.state[1]), 17)
        self.at(3.0)
        alice.state = delete_faces(alice.state, [self.face(alice, (0, 0))])
        expected = geometry(alice.state)
        ua = alice.commit()
        self.assertTrue(consistent(ua), "the commit carries counts that do not match its buffers")
        self.at(3.2)
        again = alice.apply()          # mu4: LoadError, Array length mismatch (expected 17, got 16)
        self.assertTrue(consistent(alice.m3._base["mesh"]), "the base does not match its buffers")
        self.at(3.5)
        bob.receive(ua)
        bob.apply()
        for extra in [again] if again else []:
            bob.receive(extra)
            bob.apply()
        self.assertEqual(geometry(alice.state), expected)
        self.assertEqual(geometry(bob.state), expected)

    def test_dissolve_then_merge_keeps_faces_on_their_vertices(self):
        """One editor, the other Blender idle. A dissolve puts the joined face
        last; the reapply loads the rebuild, which sorts faces by key, and
        remembers Blender's order as superseded. A merge then keeps the face
        count, so the stale-restore check compared the face buffers index by
        index and put old loop starts back: faces on the wrong vertices."""
        start = host_start((*grid(9), None))
        alice, idle = self.peer("alice", start), self.peer("idle", start)
        self.at(11.0)
        f, g = (find_face(*alice.state, corner_ids(9)(x, 1)) for x in (1, 2))
        alice.state = dissolve(alice.state, f, g)
        idle.receive(alice.commit())
        self.at(11.5)
        alice.apply()
        idle.apply()
        self.at(12.0)
        v, faces, ids = alice.state
        a, b = ids.index(1 + 2 + 2 * 10), ids.index(1 + 2 + 3 * 10)      # points (2, 2) and (2, 3)
        alice.state = merge_verts(alice.state, a, b)
        expected = geometry(alice.state)
        sent = alice.commit()
        self.assertEqual(geometry(load(sent)), expected, "the commit moved faces onto other vertices")
        self.at(12.5)
        alice.apply()
        idle.receive(sent)
        idle.apply()
        self.assertEqual(geometry(alice.state), expected)
        self.assertEqual(geometry(idle.state), expected)


def exchange(peers, server, t, rounds=3):
    """Every peer commits, the server merges, every other peer receives and
    applies, repeated. Returns the time reached."""
    for _ in range(rounds):
        for p in peers:
            Clock.now = t = t + 0.1
            sent = p.commit()
            while sent is not None:
                server.store(sent)
                for q in peers:
                    if q is not p:
                        q.receive(sent)
                Clock.now = t = t + 0.1
                sent = p.apply()
        for q in peers:
            Clock.now = t = t + 0.1
            extra = q.apply()
            if extra is not None:
                for r in peers:
                    if r is not q:
                        r.receive(extra)
                server.store(extra)
    return t


class Server:
    def __init__(self, data, tag):
        self.m3 = fresh_modules(tag)
        self.data = copy.deepcopy(data)

    def store(self, update):
        self.data = self.m3.merge_incoming(self.data, update)
        return self


class Mu5Tests(unittest.TestCase):
    """mu5: skin by vertex id, emptied lists, per-face flags."""

    def setUp(self):
        Clock.now = 1.0
        self.n = 0

    def tag(self, name):
        self.n += 1
        return f"{name}{self.n}_{id(self)}"

    def test_face_flags_set_at_once_keep_both(self):
        """Alice smooths one face and Bob the next one at the same moment. Face
        flags were merged as buffers that stamp four faces per word, so one
        side's word replaced the other's: an edit was lost, and the face flipped
        between smooth and flat as each Blender wrote back what it loaded."""
        start = host_start((*grid(), None))
        alice, bob = Peer("alice", start, self.tag("a")), Peer("bob", start, self.tag("b"))
        server = Server(start, self.tag("s"))
        for p, quad in ((alice, (0, 0)), (bob, (1, 0))):
            v, f, ids = p.state
            p.smooth[find_face(v, f, ids, Q(*quad))] = True
        Clock.now = 2.0
        ua = alice.commit()
        Clock.now = 2.05
        ub = bob.commit()
        server.store(ua).store(ub)
        alice.receive(ub)
        bob.receive(ua)
        Clock.now = 2.3
        for p in (alice, bob):
            extra = p.apply()
            if extra is not None:
                server.store(extra)
                (bob if p is alice else alice).receive(extra)
        t = exchange([alice, bob], server, 3.0)
        want = sorted(geometry((alice.state[0], [alice.state[1][find_face(*alice.state, Q(*q))]
                                                for q in ((0, 0), (1, 0))], None))[1])
        self.assertEqual(alice.smooth_faces(), want)
        self.assertEqual(bob.smooth_faces(), want)
        exchange([alice, bob], server, t)              # nothing changes on later syncs
        self.assertEqual(alice.smooth_faces(), want)
        self.assertEqual(bob.smooth_faces(), want)

    def test_skin_radii_follow_vertex_ids(self):
        """Alice and Bob each add a guide vertex at the same moment, with
        different skin radii. Index-matched skin data swapped the two radii."""
        verts, faces = grid()
        d = dump_of(verts, faces, None)
        d["mu_skin"] = {"data": np.tile(np.array([0.25, 0.25, 0, 0], dtype=np.float32), len(verts)).tobytes()}
        start, _ = topo3.attach(None, d, 1.0, 9)
        start[S] = merge3.stamp(merge3._MISSING, None, merge3.strip(start), 1.0)
        alice, bob = Peer("alice", start, self.tag("a")), Peer("bob", start, self.tag("b"))
        server = Server(start, self.tag("s"))
        for p, corner, pos, r in ((alice, 0, (0.5, -1.0, 0.0), 0.5), (bob, 3, (3.5, -1.0, 0.0), 0.9)):
            v, f, ids = p.state
            v.append(pos)
            ids.append(ids[corner])           # Blender copies the id of the vertex it came from
            f.append([corner, len(v) - 1, corner + 1])
            p.smooth.append(False)
            p.skin = np.vstack([p.skin, [r, r, 0, 0]])
        Clock.now = 2.0
        ua = alice.commit()
        Clock.now = 2.05
        ub = bob.commit()
        server.store(ua).store(ub)
        alice.receive(ub)
        bob.receive(ua)
        Clock.now = 2.3
        for p in (alice, bob):
            extra = p.apply()
            if extra is not None:
                server.store(extra)
                (bob if p is alice else alice).receive(extra)
        exchange([alice, bob], server, 3.0)
        for p in (alice, bob):
            radii = p.radii()
            self.assertIsNotNone(radii, f"{p.name}: skin rows do not match the vertices")
            self.assertEqual(radii[(0.5, -1.0, 0.0)], 0.5, p.name)
            self.assertEqual(radii[(3.5, -1.0, 0.0)], 0.9, p.name)
            self.assertEqual({r for k, r in radii.items() if k[1] >= 0}, {0.25}, p.name)

    def test_applied_modifiers_stay_gone(self):
        """X applies Mirror, Skin and Subdivision, one commit each. Y applied the
        first two updates, then moves the object. An emptied list had no stamps,
        counted as time 0, and Y's older [Subdivision] put the modifier back."""
        m = merge3
        mods = [{"name": "Mirror", "type": "MIRROR"}, {"name": "Skin", "type": "SKIN"},
                {"name": "Subdivision", "type": "SUBSURF"}]
        x = {"name": "body", "modifiers": mods, "location": [0.0, 0.0, 0.0]}
        xts = m.stamp(m._MISSING, None, x, 1.0)
        seen = None
        for k, t in ((1, 2.0), (2, 3.0), (3, 4.0)):
            new = dict(x, modifiers=mods[k:])
            xts, x = m.stamp(x, xts, new, t), new
            if k == 2:
                seen = (copy.deepcopy(x), copy.deepcopy(xts))   # what Y holds
        y, yts = seen
        moved = dict(y, location=[1.0, 0.0, 0.0])
        yts, y = m.stamp(y, yts, moved, 5.0), moved
        for a, ats, b, bts in ((x, xts, y, yts), (y, yts, x, xts)):
            out, _ = m.lww(a, ats, b, bts)
            self.assertEqual(out["modifiers"], [])
            self.assertEqual(out["location"], [1.0, 0.0, 0.0])
        stored = dict(x)
        stored[S] = xts
        update = dict(y)
        update[S] = yts
        self.assertEqual(m.merge_incoming(stored, update)["modifiers"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
