"""Offline simulation of v3 (merge3 + topo3) with Multi-User mesh dumps. No Blender.

Run from this folder: python test_v3_sim.py
It copies merge3.py and topo3.py into a temporary `replication` package so the
relative imports work as they do inside the add-on.
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
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
        d, info = topo3.attach(prev, dump, now, self.n)
        ts = merge3.stamp(prev, merge3.stamps_of(self.base), merge3.strip(d), now)
        d[S] = topo3.fix_co_stamps(prev, merge3.stamps_of(self.base), d, ts, now)
        self.base = copy.deepcopy(d)
        if info and info["ids_changed"]:
            self.state = load(d)  # the reapply writes the repaired ids back
        return d

    def apply(self, remote, now):
        """merge_fetched + load."""
        dump = dump_of(*self.state)
        local, _ = topo3.attach(merge3.strip(self.base), dump, now, self.n)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
