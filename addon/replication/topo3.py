"""Topology merge by permanent vertex id, for merge3 (v3 of the Multi-User patch).

Every mesh node carries a table under TOPO_KEY with one entry per vertex, face
and loose edge that has ever existed, keyed by permanent ids:

    "v": {"<vid>": [alive, stamp, cid, x, y, z]}
    "f": {"<vid>,<vid>,...": [alive, stamp, cid, use_smooth, material_index]}
    "e": {"<vid>,<vid>": [alive, stamp, cid]}           loose edges only
    "c": {cid: [client, stamp, {"<client>": newest stamp of that client seen}]}

A deletion is an entry with alive 0 (a tombstone). Two tables merge entry by
entry, newest stamp wins, so the result is the same in any order and on the
server, which never knows a common base.

Two commits that did not see each other (by their vector clocks in "c") and
that touch the same base vertices conflict. In that region the older commit's
changes are hidden when the table is turned into a mesh (`view`), and the next
commit from any client makes that permanent.

Vertex ids live in the integer attribute ID_ATTR. Blender copies an existing id
onto every vertex an edit creates, so `repair_ids` runs before each diff.
Meshes with UVs, vertex colors or other attributes are left to the old
whole-mesh rule, because a rebuild would drop that data.
"""
import copy
import logging
import math
import zlib

import numpy as np

ENABLED = True
TOPO_KEY = "_mu_topo"
ID_ATTR = "mu_vid"
CLIENT_SHIFT = 23
GEOM_KEYS = ("vertex_count", "vertices", "egdes_count", "edges", "loop_count", "loops",
             "poly_count", "polygons", "attributes", "uv_layers", "vertex_colors")
_client = None


# ---------------------------------------------------------------- ids and keys
def client_no():
    """1..255 from the Multi-User username, the high bits of fresh ids."""
    global _client
    if _client is None:
        name = ""
        try:
            import bpy
            for key, addon in bpy.context.preferences.addons.items():
                if key.endswith("multi_user"):
                    name = addon.preferences.username
                    break
        except Exception:
            pass
        if not name:
            import os
            name = str(os.getpid())
        _client = zlib.crc32(name.encode()) % 255 + 1
        logging.info(f"topo3: client number {_client} for '{name}'")
    return _client


def face_key(face):
    face = tuple(int(v) for v in face)
    i = face.index(min(face))
    return face[i:] + face[:i]


def edge_key(a, b):
    a, b = int(a), int(b)
    return (a, b) if a <= b else (b, a)


def kstr(key):
    return ",".join(str(v) for v in key)


def kparse(s):
    return tuple(int(v) for v in s.split(","))


def repair_ids(ids, coords, base_verts, client):
    """Unique ids after a local edit. In a group of equal ids the vertex nearest
    that id's base position keeps it. Every other vertex first takes a base id
    that is missing from the mesh and sits at the same position (so repeating a
    repair gives the same ids), then a fresh id."""
    ids = [int(v) for v in ids]
    groups = {}
    for i, vid in enumerate(ids):
        groups.setdefault(vid, []).append(i)
    out = list(ids)
    needs = []
    for vid, members in groups.items():
        if vid <= 0:
            needs.extend(members)
        elif len(members) > 1:
            if vid in base_verts:
                ref = base_verts[vid]
                keeper = min(members, key=lambda i: (math.dist(coords[i], ref), i))
            else:
                keeper = members[0]
            needs.extend(i for i in members if i != keeper)
    if not needs:
        return out, 0
    present = set(ids)
    spare = {}
    for vid, pos in base_verts.items():
        if vid not in present:
            spare.setdefault(tuple(round(c, 5) for c in pos), []).append(vid)
    used = set(base_verts) | present
    top = max([v & ((1 << CLIENT_SHIFT) - 1) for v in used if v >> CLIENT_SHIFT == client] or [0])
    for i in sorted(needs):
        slot = spare.get(tuple(round(c, 5) for c in coords[i]))
        if slot:
            out[i] = slot.pop(0)
            continue
        top += 1
        out[i] = (client << CLIENT_SHIFT) | top
    return out, len(needs)


# ---------------------------------------------------------------- dumps
def is_mesh(d):
    return isinstance(d, dict) and all(k in d for k in ("vertex_count", "egdes_count", "loop_count",
                                                         "poly_count", "vertices", "loops", "polygons"))


def _attr(d, name):
    for entry in d.get("attributes") or []:
        if entry[0] == name:
            return entry
    return None


def unsupported(d):
    """Data a rebuild would lose: vertex colors, attributes other than ids."""
    if d.get("vertex_colors"):
        return True
    return any(entry[0] != ID_ATTR for entry in d.get("attributes") or [])


def _buf(section, key, dtype, width=1):
    raw = (section or {}).get(key) or b""
    arr = np.frombuffer(raw, dtype=dtype)
    return arr.reshape(-1, width) if width > 1 else arr


def get_ids(d):
    entry = _attr(d, ID_ATTR)
    if entry is None:
        return None
    return _buf(entry[3], "value", np.int32)


def decode(d, ids):
    """A dump as an id mesh: verts, faces with their flags, loose edges, edge flags."""
    co = _buf(d["vertices"], "co", np.float32, 3)
    ev = _buf(d.get("edges"), "vertices", np.int32, 2)
    seam = _buf(d.get("edges"), "use_seam", bool)
    sharp = _buf(d.get("edges"), "use_edge_sharp", bool)
    vi = _buf(d["loops"], "vertex_index", np.int32)
    ls = _buf(d["polygons"], "loop_start", np.int32)
    lt = _buf(d["polygons"], "loop_total", np.int32)
    smooth = _buf(d["polygons"], "use_smooth", bool)
    mat = _buf(d["polygons"], "material_index", np.int32)
    uv_layers = {name: _buf(layer, "data", np.float32, 2) for name, layer in (d.get("uv_layers") or {}).items()}
    verts = {int(ids[i]): tuple(float(c) for c in co[i]) for i in range(len(co))}
    faces = {}
    uvs = {name: {} for name in uv_layers}
    face_edges = set()
    for p in range(len(ls)):
        idx = vi[ls[p]:ls[p] + lt[p]]
        key = face_key(ids[j] for j in idx)
        faces[key] = (bool(smooth[p]) if p < len(smooth) else False, int(mat[p]) if p < len(mat) else 0)
        for name, data in uv_layers.items():
            if ls[p] + lt[p] <= len(data):
                # corner UVs by vertex id, so they survive the key's rotation
                uvs[name][key] = {int(ids[idx[k]]): tuple(data[ls[p] + k]) for k in range(len(idx))}
        for k in range(len(idx)):
            face_edges.add(edge_key(ids[idx[k]], ids[idx[(k + 1) % len(idx)]]))
    flags = {}
    loose = set()
    for i in range(len(ev)):
        key = edge_key(ids[ev[i][0]], ids[ev[i][1]])
        flags[key] = (bool(seam[i]) if i < len(seam) else False, bool(sharp[i]) if i < len(sharp) else False)
        if key not in face_edges:
            loose.add(key)
    return {"verts": verts, "faces": faces, "edges": loose, "flags": flags, "uvs": uvs}


def _set_ids(d, ids):
    """Write ids into the dump's attribute list, adding the attribute if needed."""
    buf = np.asarray(ids, dtype=np.int32).tobytes()
    attrs = [list(e) for e in d.get("attributes") or [] if e[0] != ID_ATTR]
    attrs.append([ID_ATTR, "INT", "POINT", {"value": buf}])
    d["attributes"] = attrs


# ---------------------------------------------------------------- table
def _ids_or_index(d):
    """A dump's ids, or 1..N by index when it has none (the shared first numbering)."""
    ids = get_ids(d)
    n = len(_buf(d["vertices"], "co", np.float32, 3))
    if ids is None or len(ids) != n:
        return np.arange(1, n + 1, dtype=np.int32)
    return ids


def _alive_verts(topo):
    return {int(k): tuple(e[3:6]) for k, e in topo.get("v", {}).items() if e[0]}


def attach(base, dump, now, client=None):
    """Return (dump with ids and an updated table, info) for a fresh local dump.

    base is the last synced data (may carry a table). info is None when the
    mesh is not handled here; otherwise it says whether ids changed in the dump,
    which then has to be loaded back into the datablock.
    """
    if not ENABLED or not is_mesh(dump):
        return dump, None
    if unsupported(dump):
        dump = dict(dump)
        dump.pop(TOPO_KEY, None)
        return dump, None
    client = client or client_no()
    base_topo = base.get(TOPO_KEY) if isinstance(base, dict) else None
    synced = base if isinstance(base, dict) and is_mesh(base) and not unsupported(base) else None
    synced_ids = _ids_or_index(synced) if synced is not None else None
    if base_topo:
        topo = copy.deepcopy(base_topo)
    else:
        # First table for this mesh, built from the last synced state, not from the
        # edited dump. Every client numbers that same mesh the same way, so the
        # entries are a shared baseline (stamp 0, commit "init", not in "c") that
        # no commit can conflict with. The local edit is then diffed against it.
        src = synced if synced is not None else dump
        m = decode(src, synced_ids if synced is not None else _ids_or_index(dump))
        topo = {"v": {}, "f": {}, "e": {}, "c": {}}
        for vid, pos in m["verts"].items():
            topo["v"][str(vid)] = [1, 0.0, "init", *pos]
        for key, (smooth, mat) in m["faces"].items():
            topo["f"][kstr(key)] = [1, 0.0, "init", smooth, mat]
        for key in m["edges"]:
            topo["e"][kstr(key)] = [1, 0.0, "init"]

    co = _buf(dump["vertices"], "co", np.float32, 3)
    ids = get_ids(dump)
    ids_changed = False
    if ids is None or len(ids) != len(co):
        ids_changed = True
        if synced is None:
            ids = np.arange(1, len(co) + 1, dtype=np.int32)
        else:
            # No ids loaded yet: Blender keeps existing vertices in place and appends
            # new ones, so map by index where the synced mesh has that vertex at
            # that index or the count is unchanged. The rest is matched by position
            # or numbered fresh in repair_ids.
            sco = _buf(synced["vertices"], "co", np.float32, 3)
            ids = np.zeros(len(co), dtype=np.int32)
            k = min(len(co), len(sco))
            same = np.all(co[:k] == sco[:k], axis=1) if k else np.zeros(0, dtype=bool)
            if len(sco) == len(co) or (k and same.mean() >= 0.9):
                ids[:k] = synced_ids[:k]          # same order, extra vertices appended
            else:
                ids[:k][same] = synced_ids[:k][same]  # e.g. after a deletion shifted indices
    fixed, n = repair_ids(ids, [tuple(c) for c in co], _alive_verts(topo), client)
    ids_changed = ids_changed or n > 0
    dump = dict(dump)
    if ids_changed:
        _set_ids(dump, fixed)
    mesh = decode(dump, fixed)

    # Last synced positions, kept in a tombstone so a hidden deletion can be undone.
    last = {}
    if synced is not None and len(synced_ids) == len(_buf(synced["vertices"], "co", np.float32, 3)):
        base_co = _buf(synced["vertices"], "co", np.float32, 3)
        last = {int(synced_ids[i]): [float(c) for c in base_co[i]] for i in range(len(base_co))}

    cid = f"{client}:{now:.6f}"
    changed = False
    tv, tf, te = topo["v"], topo["f"], topo["e"]
    for vid, pos in mesh["verts"].items():
        e = tv.get(str(vid))
        if e is None or not e[0]:
            tv[str(vid)] = [1, now, cid, *pos]
            changed = True
    for k, e in tv.items():
        if e[0] and int(k) not in mesh["verts"]:
            tv[k] = [0, now, cid, *last.get(int(k), e[3:6])]
            changed = True
    for key, (smooth, mat) in mesh["faces"].items():
        ks = kstr(key)
        e = tf.get(ks)
        if e is None or not e[0]:
            tf[ks] = [1, now, cid, smooth, mat]
            changed = True
        elif (bool(e[3]), int(e[4])) != (smooth, mat):
            tf[ks] = [1, now, e[2], smooth, mat]  # a flag change keeps the face's owner
    for ks, e in tf.items():
        if e[0] and kparse(ks) not in mesh["faces"]:
            tf[ks] = [0, now, cid, e[3], e[4]]
            changed = True
    for key in mesh["edges"]:
        ks = kstr(key)
        e = te.get(ks)
        if e is None or not e[0]:
            te[ks] = [1, now, cid]
            changed = True
    for ks, e in te.items():
        if e[0] and kparse(ks) not in mesh["edges"]:
            te[ks] = [0, now, cid]
            changed = True
    if changed:
        seen = {}
        for c in topo["c"].values():
            seen[str(c[0])] = max(seen.get(str(c[0]), 0.0), c[1])
        topo["c"][cid] = [client, now, seen]
    dump[TOPO_KEY] = topo
    return dump, {"ids_changed": ids_changed, "repaired": n, "topology_changed": changed}


def _newer(x, y):
    """Entry order: stamp, commit id, alive, then the whole entry, so every replica agrees."""
    return (x[1], str(x[2]), x[0], repr(x)) >= (y[1], str(y[2]), y[0], repr(y))


def merge_topo(ta, tb):
    out = {}
    for sec in ("v", "f", "e"):
        a, b = ta.get(sec, {}), tb.get(sec, {})
        m = dict(a)
        for k, e in b.items():
            if k not in m or not _newer(m[k], e):
                m[k] = e
        out[sec] = m
    c = dict(ta.get("c", {}))
    c.update(tb.get("c", {}))
    out["c"] = c
    return out


# ---------------------------------------------------------------- view
class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _concurrent(x, y):
    """Commits x and y ([client, stamp, seen]) did not see each other."""
    if x[0] == y[0]:
        return False
    return y[2].get(str(x[0]), -1.0) < x[1] and x[2].get(str(y[0]), -1.0) < y[1]


def _units(topo):
    """Per commit: units of owned entries, each with a footprint of base vertex ids."""
    owned = {}
    for sec in ("v", "f", "e"):
        for k, e in topo.get(sec, {}).items():
            owned.setdefault(e[2], []).append((sec, k, e))
    units = []
    for cid, entries in owned.items():
        new_verts = {int(k) for sec, k, e in entries if sec == "v" and e[0]}
        added = [(sec, k) for sec, k, e in entries if e[0] and sec != "v"]
        removed = [(sec, k) for sec, k, e in entries if not e[0]]
        for sec, k in removed:
            verts = {int(k)} if sec == "v" else set(kparse(k))
            units.append({"cid": cid, "fp": verts - new_verts, "elems": [(sec, k)]})
        uf = _UF()
        for sec, k in added:
            vs = kparse(k)
            for v in vs[1:]:
                uf.union(vs[0], v)
        groups = {}
        for sec, k in added:
            groups.setdefault(uf.find(kparse(k)[0]), []).append((sec, k))
        grouped_verts = set()
        for elems in groups.values():
            vs = set()
            for _, k in elems:
                vs.update(kparse(k))
            grouped_verts |= vs
            elems = elems + [("v", str(v)) for v in vs & new_verts]
            units.append({"cid": cid, "fp": vs - new_verts, "elems": elems})
        for v in new_verts - grouped_verts:
            units.append({"cid": cid, "fp": set(), "elems": [("v", str(v))]})
    return units


def view(topo):
    """Alive mesh of a table, with concurrent conflicts resolved. Pure function."""
    commits = topo.get("c", {})
    units = [u for u in _units(topo) if u["cid"] in commits]
    by_vert = {}
    for i, u in enumerate(units):
        for v in u["fp"]:
            by_vert.setdefault(v, []).append(i)
    uf = _UF()
    for i in range(len(units)):
        uf.find(i)
    # Join units that share a base vertex and belong to one commit or to two
    # commits that did not see each other. Ordered commits never conflict.
    for members in by_vert.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                ua, ub = units[members[x]], units[members[y]]
                if ua["cid"] == ub["cid"] or _concurrent(commits[ua["cid"]], commits[ub["cid"]]):
                    uf.union(members[x], members[y])
    clusters = {}
    for i in range(len(units)):
        clusters.setdefault(uf.find(i), []).append(i)

    hidden = set()
    conflicts = 0
    for members in clusters.values():
        cids = {units[i]["cid"] for i in members}
        if len(cids) < 2:
            continue
        win = max(cids, key=lambda c: (commits[c][1], c))
        losers = {c for c in cids if c != win and _concurrent(commits[c], commits[win])}
        if not losers:
            continue
        conflicts += 1
        for i in members:
            if units[i]["cid"] in losers:
                hidden.update(units[i]["elems"])

    def alive(sec, k, e):
        return (not e[0]) if (sec, k) in hidden else bool(e[0])

    verts = {int(k): tuple(e[3:6]) for k, e in topo.get("v", {}).items() if alive("v", k, e)}
    faces = {}
    for k, e in topo.get("f", {}).items():
        key = kparse(k)
        if alive("f", k, e) and all(v in verts for v in key):
            faces[key] = (bool(e[3]), int(e[4]))
    edges = set()
    for k, e in topo.get("e", {}).items():
        key = kparse(k)
        if alive("e", k, e) and all(v in verts for v in key):
            edges.add(key)
    return {"verts": verts, "faces": faces, "edges": edges, "conflicts": conflicts, "hidden": len(hidden)}


# ---------------------------------------------------------------- rebuild
def _vertex_stamps(d, ts):
    """Per-vertex stamp from the co word stamps (3 words per vertex)."""
    n = len(_buf(d["vertices"], "co", np.float32, 3))
    co_ts = ts.get("vertices", {}).get("co") if isinstance(ts, dict) and isinstance(ts.get("vertices"), dict) else ts
    if isinstance(co_ts, (bytes, bytearray)) and len(co_ts) == n * 3 * 8:
        return np.frombuffer(co_ts, dtype=np.float64).reshape(-1, 3).max(axis=1)
    from .merge3 import _tmax
    return np.full(n, _tmax(co_ts))


def rebuild(v, sources):
    """Mesh buffers in canonical order (ids ascending, faces by key) for view v.
    sources: [(data, stamps)] to take positions and edge flags from."""
    from .merge3 import _tmax
    pos, pos_t, flags = {}, {}, {}
    uvs, uv_t = {}, {}   # layer -> face key -> corner uvs, and the stamp they came with
    for d, ts in sources:
        ids = get_ids(d)
        if ids is None:
            continue
        co = _buf(d["vertices"], "co", np.float32, 3)
        vts = _vertex_stamps(d, ts)
        for i, vid in enumerate(ids):
            vid = int(vid)
            if vid in v["verts"] and (vid not in pos_t or vts[i] > pos_t[vid]):
                pos[vid], pos_t[vid] = co[i], float(vts[i])
        m = decode(d, ids)
        flags.update(m["flags"])
        lts = ts.get("uv_layers") if isinstance(ts, dict) else ts
        for name, faces in m["uvs"].items():
            t = _tmax(_child_ts(lts, name))
            layer, layer_t = uvs.setdefault(name, {}), uv_t.setdefault(name, {})
            for key, corners in faces.items():
                if key not in layer or (t, repr(corners)) > (layer_t[key], repr(layer[key])):
                    layer[key], layer_t[key] = corners, t
    order = sorted(v["verts"])
    index = {vid: i for i, vid in enumerate(order)}
    co = np.array([pos.get(vid, v["verts"][vid]) for vid in order], dtype=np.float32).reshape(-1, 3)
    co_ts = np.repeat(np.array([pos_t.get(vid, 0.0) for vid in order], dtype=np.float64), 3)

    faces = sorted(v["faces"])
    vi, ls, lt, smooth, mat = [], [], [], [], []
    edge_set = set()
    for key in faces:
        ls.append(len(vi))
        lt.append(len(key))
        vi.extend(index[x] for x in key)
        s, m = v["faces"][key]
        smooth.append(s)
        mat.append(m)
        for k in range(len(key)):
            edge_set.add(edge_key(key[k], key[(k + 1) % len(key)]))
    edge_set |= set(v["edges"])
    edges = sorted(edge_set)
    data = {
        "vertex_count": len(order),
        "vertices": {"co": co.tobytes()},
        "egdes_count": len(edges),
        "edges": {
            "vertices": np.array([(index[a], index[b]) for a, b in edges], dtype=np.int32).tobytes(),
            "use_seam": np.array([flags.get(e, (False, False))[0] for e in edges], dtype=bool).tobytes(),
            "use_edge_sharp": np.array([flags.get(e, (False, False))[1] for e in edges], dtype=bool).tobytes(),
        },
        "loop_count": len(vi),
        "loops": {
            "vertex_index": np.array(vi, dtype=np.int32).tobytes(),
            "normal": np.zeros(len(vi) * 3, dtype=np.float32).tobytes(),
        },
        "poly_count": len(faces),
        "polygons": {
            "loop_total": np.array(lt, dtype=np.int32).tobytes(),
            "loop_start": np.array(ls, dtype=np.int32).tobytes(),
            "use_smooth": np.array(smooth, dtype=bool).tobytes(),
            "material_index": np.array(mat, dtype=np.int32).tobytes(),
        },
        "attributes": [[ID_ATTR, "INT", "POINT", {"value": np.array(order, dtype=np.int32).tobytes()}]],
    }
    if uvs:
        data["uv_layers"] = {}
        for name, layer in uvs.items():
            out = np.zeros((len(vi), 2), dtype=np.float32)
            for p, key in enumerate(faces):
                corners = layer.get(key, {})
                for k, vid in enumerate(key):
                    if vid in corners:
                        out[ls[p] + k] = corners[vid]
            data["uv_layers"][name] = {"data": out.tobytes()}
    return data, co_ts.tobytes()


def _child_ts(ts, key):
    if isinstance(ts, dict):
        return ts.get(key)
    return ts


def fix_co_stamps(base, base_ts, new, ts, now):
    """Restamp vertex coordinates by id when the vertex layout changed.

    merge3.stamp compares buffers word by word, so after a topology edit every
    coordinate looks new and an unmoved vertex would outrank another user's
    real move. Here a vertex keeps its base stamp unless its position changed.
    """
    if not (is_mesh(new) and TOPO_KEY in new and isinstance(ts, dict)):
        return ts
    ids = get_ids(new)
    if ids is None:
        return ts
    co = _buf(new["vertices"], "co", np.float32, 3)
    out = np.full((len(co), 3), now, dtype=np.float64)
    if isinstance(base, dict) and is_mesh(base) and get_ids(base) is not None:
        bids = get_ids(base)
        bco = _buf(base["vertices"], "co", np.float32, 3)
        if _same_layout(base, new):
            return ts  # word by word stamping is already right
        bts = _vertex_stamps(base, base_ts) if base_ts is not None else np.zeros(len(bco))
        where = {int(v): i for i, v in enumerate(bids)}
        for i, vid in enumerate(ids):
            j = where.get(int(vid))
            if j is not None and j < len(bco) and np.array_equal(bco[j], co[i]):
                out[i] = bts[j]
    ts = dict(ts)
    vts = ts.get("vertices")
    vts = dict(vts) if isinstance(vts, dict) else {}
    vts["co"] = out.reshape(-1).tobytes()
    ts["vertices"] = vts
    return ts


def _same_layout(a, b):
    """Both dumps hold the same vertex ids in the same order and the same faces."""
    ia, ib = get_ids(a), get_ids(b)
    if ia is None or ib is None or ia.tobytes() != ib.tobytes():
        return False
    for sec, key in (("loops", "vertex_index"), ("polygons", "loop_start"), ("polygons", "loop_total"),
                     ("edges", "vertices")):
        if (a.get(sec) or {}).get(key) != (b.get(sec) or {}).get(key):
            return False
    return True


def matches_view(d, v):
    ids = get_ids(d)
    if ids is None:
        return False
    m = decode(d, ids)
    return set(m["verts"]) == set(v["verts"]) and set(m["faces"]) == set(v["faces"]) \
        and m["edges"] == v["edges"]


def merge_mesh(a, ats, b, bts, lww):
    """merge3.lww for two mesh dicts that both carry a table."""
    topo = merge_topo(a[TOPO_KEY], b[TOPO_KEY])
    v = view(topo)
    ra = {k: x for k, x in a.items() if k != TOPO_KEY}
    rb = {k: x for k, x in b.items() if k != TOPO_KEY}
    if _same_layout(a, b) and matches_view(a, v):
        out, ot = lww(ra, ats, rb, bts)
        out[TOPO_KEY] = topo
        return out, ot
    if v["conflicts"]:
        logging.info(f"topo3: {v['conflicts']} concurrent topology conflict(s), {v['hidden']} element(s) hidden")
    rest_a = {k: x for k, x in ra.items() if k not in GEOM_KEYS}
    rest_b = {k: x for k, x in rb.items() if k not in GEOM_KEYS}
    rest_ats = {k: x for k, x in ats.items() if k not in GEOM_KEYS} if isinstance(ats, dict) else ats
    rest_bts = {k: x for k, x in bts.items() if k not in GEOM_KEYS} if isinstance(bts, dict) else bts
    out, ot = lww(rest_a, rest_ats, rest_b, rest_bts)
    geom, co_ts = rebuild(v, [(a, ats), (b, bts)])
    from .merge3 import _tmax
    t = max(_tmax(ats), _tmax(bts))
    out.update(geom)
    ot = dict(ot) if isinstance(ot, dict) else {}
    for k in geom:
        ot[k] = t
    ot["vertices"] = {"co": co_ts}
    out[TOPO_KEY] = topo
    return out, ot


def drop(d):
    if isinstance(d, dict) and TOPO_KEY in d:
        d = dict(d)
        d.pop(TOPO_KEY)
    return d
