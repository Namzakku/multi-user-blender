"""Topology merge by permanent vertex id, for merge3 (v3 of the Multi-User patch).

Every mesh node carries a table under TOPO_KEY with one entry per vertex, face
and loose edge that has ever existed, keyed by permanent ids:

    "v": {"<vid>": [alive, stamp, cid, x, y, z]}
    "f": {"<vid>,<vid>,...": [alive, stamp, cid, use_smooth, material_index]}
    "e": {"<vid>,<vid>": [alive, stamp, cid]}           loose edges only
    "p": {"<vid>,<vid>,...": [stamp, use_smooth, material_index]}
         face flags changed after the face was made; the newer of this and
         the "f" entry wins, and a flag change never revives a deleted face
    "c": {cid: [client, stamp, {"<client>": newest stamp of that client seen}]}
         a settle commit adds 1 and [[winner cid, loser cids, hidden, winner, losers]]
    "n": {"<client>": [username, stamp]}        who holds each client number
    "k": {"<client>": {"<client>": stamp}}      newest stamp each client has seen
    "s": {"<client>": stamp}                    newest stamp of pruned commits

A deletion is an entry with alive 0 (a tombstone). Two tables merge entry by
entry, newest stamp wins, so the result is the same in any order and on the
server, which never knows a common base.

Two commits that did not see each other (by their vector clocks in "c") and
that touch the same base vertices conflict. In that region the older commit's
changes are hidden when the table is turned into a mesh (`view`), and the next
commit from any client makes that permanent.

Vertex ids live in the integer attribute ID_ATTR. Blender copies an existing id
onto every vertex an edit creates, so `repair_ids` runs before each diff.
Fresh ids carry the creating client's number in their high bits. Numbers are
claimed per table in "n", so two users never share one.
Meshes with vertex colors or other attributes are left to the old whole-mesh
rule, because a rebuild would drop that data.

A commit every active client has seen can no longer conflict. `prune` then
drops its record and the tombstones it left, when that does not change the mesh.
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
MAX_CLIENT = 255
# Pruning, in seconds of table time (the newest stamp in the table). A commit
# is pruned once every active client has seen it and it is PRUNE_SETTLE older
# than the newest commit, so a concurrent commit from a client the table does
# not know yet has time to arrive. A client whose newest topology commit is
# PRUNE_AGE older than the newest one no longer holds back pruning.
PRUNE_SETTLE = 120.0
PRUNE_AGE = 600.0
GEOM_KEYS = ("vertex_count", "vertices", "egdes_count", "edges", "loop_count", "loops",
             "poly_count", "polygons", "attributes", "uv_layers", "vertex_colors", "mu_skin")
# Skin modifier data per vertex, dumped by bl_mesh as float32 rows
# [radius x, radius y, use_loose, use_root] and carried by vertex id.
SKIN_KEY = "mu_skin"
SKIN_DEFAULT = (0.25, 0.25, 0.0, 0.0)
_username = None
# Set by merge_mesh: the view and table of the last merge, for conflict notices.
LAST = None


# ---------------------------------------------------------------- ids and keys
def username():
    """The Multi-User username, or the process id when there is none."""
    global _username
    if _username is None:
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
            name = f"pid{os.getpid()}"
        _username = name
    return _username


def claim(topo, name, now):
    """This user's client number in the table, 1..MAX_CLIENT, the high bits of
    fresh ids. A number already held by another name is skipped."""
    reg = topo.setdefault("n", {})
    for no, entry in reg.items():
        if entry[0] == name:
            return int(no)
    start = zlib.crc32(name.encode()) % MAX_CLIENT
    for k in range(MAX_CLIENT):
        no = (start + k) % MAX_CLIENT + 1
        if str(no) not in reg:
            reg[str(no)] = [name, now]
            logging.info(f"topo3: client number {no} for '{name}'")
            return no
    raise RuntimeError(f"topo3: all {MAX_CLIENT} client numbers are taken")


def name_of(topo, no):
    entry = (topo.get("n") or {}).get(str(no))
    return entry[0] if entry else f"client {no}"


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


def _pos_key(pos):
    # float() first: a numpy float32 and a Python float of the same value are
    # not equal, so a mesh coordinate would never find its table entry
    return tuple(round(float(c), 5) for c in pos)


def _f32(vid):
    return int(np.float32(vid))


def repair_ids(ids, coords, base_verts, client):
    """Unique ids after a local edit, the same ids every time it is repeated on
    the same mesh. In a group of equal ids the vertex nearest that id's base
    position keeps it. A vertex whose id is not in the base, or that lost its
    group, takes in turn: a base id missing from the mesh at its position; the
    nearest missing base id that float32 rounds to its id (Blender passes the id
    attribute through float32 in some operators, so ids above 2**24 lose their
    low bits on vertices the edit did not touch); a fresh id."""
    ids = [int(v) for v in ids]
    groups = {}
    for i, vid in enumerate(ids):
        groups.setdefault(vid, []).append(i)
    out = list(ids)
    needs = []
    for vid, members in groups.items():
        if vid <= 0 or (base_verts and vid not in base_verts):
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
    needs = sorted(needs)
    need_set = set(needs)
    present = {ids[i] for i in range(len(ids)) if i not in need_set}  # ids kept as they are
    spare, rounded = {}, {}
    for vid, pos in base_verts.items():
        if vid not in present:
            spare.setdefault(_pos_key(pos), []).append(vid)
    taken = set()
    rest = []
    for i in needs:
        slot = [v for v in spare.get(_pos_key(coords[i]), []) if v not in taken]
        if slot:
            out[i] = slot[0]
            taken.add(slot[0])
        else:
            rest.append(i)
    for vid in base_verts:
        if vid not in present and vid not in taken:
            rounded.setdefault(_f32(vid), []).append(vid)
    pairs = sorted((math.dist(coords[i], base_verts[vid]), i, vid)
                   for i in rest if ids[i] > 0 for vid in rounded.get(_f32(ids[i]), []))
    done = set()
    for _, i, vid in pairs:
        if i not in done and vid not in taken:
            out[i] = vid
            done.add(i)
            taken.add(vid)
    used = set(base_verts) | set(out)
    top = max([v & ((1 << CLIENT_SHIFT) - 1) for v in used if v >> CLIENT_SHIFT == client] or [0])
    for i in rest:
        if i not in done:
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


_DOMAIN = {"POINT": "vertex_count", "EDGE": "egdes_count", "FACE": "poly_count", "CORNER": "loop_count"}


def check(d):
    """Why bl_mesh.load would fail on this dump, or None when it is whole.

    A count that does not match its buffer makes Blender raise "Array length
    mismatch" halfway through a load, and the half-loaded mesh then crashed
    Blender (2026-11-04). So does a loop or edge that points past the vertices."""
    if not is_mesh(d):
        return None
    try:
        nv, ne, nl, nf = (int(d[k]) for k in ("vertex_count", "egdes_count", "loop_count", "poly_count"))
        need = [("vertices.co", (d["vertices"] or {}).get("co"), nv * 12),
                ("edges.vertices", (d.get("edges") or {}).get("vertices"), ne * 8),
                ("edges.use_seam", (d.get("edges") or {}).get("use_seam"), ne),
                ("edges.use_edge_sharp", (d.get("edges") or {}).get("use_edge_sharp"), ne),
                ("loops.vertex_index", (d["loops"] or {}).get("vertex_index"), nl * 4),
                ("loops.normal", (d["loops"] or {}).get("normal"), nl * 12),
                ("polygons.loop_start", (d["polygons"] or {}).get("loop_start"), nf * 4),
                ("polygons.loop_total", (d["polygons"] or {}).get("loop_total"), nf * 4),
                ("polygons.use_smooth", (d["polygons"] or {}).get("use_smooth"), nf),
                ("polygons.material_index", (d["polygons"] or {}).get("material_index"), nf * 4)]
        counts = {"vertex_count": nv, "egdes_count": ne, "poly_count": nf, "loop_count": nl}
        for name, _type, domain, values in d.get("attributes") or []:
            if domain in _DOMAIN and isinstance(values, dict):
                need.append((f"attribute {name}", values.get("value"), counts[_DOMAIN[domain]] * 4))
        for name, layer in (d.get("uv_layers") or {}).items():
            need.append((f"uv layer {name}", (layer or {}).get("data"), nl * 8))
        if SKIN_KEY in d:
            need.append(("skin", (d[SKIN_KEY] or {}).get("data"), nv * 16))
        for name, buf, size in need:
            if buf and len(buf) != size:
                return f"{name} holds {len(buf)} bytes, the counts need {size}"
        if nv and not (d["vertices"] or {}).get("co"):
            return "vertices without coordinates"
        vi = _buf(d["loops"], "vertex_index", np.int32)
        ls = _buf(d["polygons"], "loop_start", np.int32)
        lt = _buf(d["polygons"], "loop_total", np.int32)
        ev = _buf(d.get("edges"), "vertices", np.int32)
        if len(vi) != nl or len(ls) != nf or len(lt) != nf:
            return "loops or polygons missing"
        if int(lt.sum()) != nl or (nf and (ls.min() < 0 or lt.min() < 3 or int((ls + lt).max()) > nl)):
            return "polygons do not cover the loops"
        if (nl and (vi.min() < 0 or vi.max() >= nv)) or (len(ev) and (ev.min() < 0 or ev.max() >= nv)):
            return "a loop or edge points past the vertices"
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        return f"unreadable mesh dump ({e!r})"
    return None


def _ids_matching(d):
    """The dump's ids when there is one per vertex, else None."""
    ids = get_ids(d)
    if ids is None or len(ids) != len(_buf(d["vertices"], "co", np.float32, 3)):
        return None
    return ids


def _same_topology(a, b):
    for sec, key in (("loops", "vertex_index"), ("polygons", "loop_start"), ("polygons", "loop_total"),
                     ("edges", "vertices")):
        if (a.get(sec) or {}).get(key) != (b.get(sec) or {}).get(key):
            return False
    return len(_buf(a["vertices"], "co", np.float32, 3)) == len(_buf(b["vertices"], "co", np.float32, 3))


def unstale_mesh(base, new, olds, generic):
    """merge3.unstale for a mesh dump. Returns (new', changed).

    The geometry is never compared index by index: counts, faces, edges, ids,
    UVs and skin stay as the dump has them. A topology change is a real edit,
    which the table merges, and after a topology change the same index is a
    different vertex or face (2026-11-04: a reverted face count crashed the
    load, and reverted loop starts put faces on the wrong vertices). Only a
    vertex coordinate goes back to its synced value, matched by vertex id, where
    an update replaced exactly that value of that vertex. Meshes without ids are
    matched by index only while every dump involved has the same topology. The
    rest of the dump goes through `generic`, the plain merge3.unstale."""
    rest = lambda d: {k: v for k, v in d.items() if k not in GEOM_KEYS} if isinstance(d, dict) else d
    out, changed = generic(rest(base), rest(new), [(rest(b), rest(a)) for b, a in olds])
    co, co_changed = _unstale_co(base, new, olds)
    if not changed and not co_changed:
        return new, False
    result = dict(new)
    result.update(out)
    if co_changed:
        result["vertices"] = dict(new["vertices"])
        result["vertices"]["co"] = co
    return result, True


def _words_by_id(d, ids):
    """Coordinate words of d and {id: index} for the ids that occur once."""
    words = np.frombuffer((d["vertices"] or {}).get("co") or b"", dtype=np.uint32).reshape(-1, 3)
    seen = {}
    for i, v in enumerate(ids):
        seen[int(v)] = -1 if int(v) in seen else i
    return words, {v: i for v, i in seen.items() if i >= 0}


def matching_ids(d, base):
    """The ids repair_ids gives d against base's table, for matching vertices by
    id before the commit repairs them. A fresh dump still carries the ids Blender
    copied onto new vertices and rounded through float32, which name other
    vertices. Fresh ids come out unused by the base, so they match nothing."""
    ids = _ids_matching(d)
    topo = base.get(TOPO_KEY) if isinstance(base, dict) else None
    if ids is None or not topo:
        return ids
    co = _buf(d["vertices"], "co", np.float32, 3)
    fixed, n = repair_ids(ids, [tuple(c) for c in co], _alive_verts(topo), 0)
    return np.asarray(fixed, dtype=np.int64) if n else ids


def with_ids(d, ids_from):
    """d with the ids of ids_from, a dump of the same vertices in the same order."""
    ids = _ids_matching(ids_from)
    if not is_mesh(d) or ids is None or len(ids) != len(_buf(d["vertices"], "co", np.float32, 3)):
        return d
    d = dict(d)
    _set_ids(d, ids)
    return d


def _unstale_co(base, new, olds):
    if check(base) or check(new):
        return None, False
    nid, bid = matching_ids(new, base), _ids_matching(base)
    if nid is None or bid is None:
        if not _same_topology(base, new):
            return None, False
        nid = bid = np.arange(len(_buf(new["vertices"], "co", np.float32, 3)))
        by_index = True
    else:
        by_index = False
    nw, _ = _words_by_id(new, nid)
    bw, bmap = _words_by_id(base, bid)
    rows = [(i, bmap[int(v)]) for i, v in enumerate(nid) if int(v) in bmap]
    if not rows:
        return None, False
    ni = np.array([r[0] for r in rows])
    bi = np.array([r[1] for r in rows])
    differs = nw[ni] != bw[bi]
    if not differs.any():
        return None, False
    mask = np.zeros_like(differs)
    for b, a in olds or []:
        if not (is_mesh(b) and is_mesh(a)) or check(b) or check(a):
            continue
        if by_index:
            if not (_same_topology(b, new) and _same_topology(a, new)):
                continue
            ids_b = ids_a = nid
        else:
            ids_b, ids_a = _ids_matching(b), _ids_matching(a)
            if ids_b is None or ids_a is None:
                continue
        bwo, bomap = _words_by_id(b, ids_b)
        awo, aomap = _words_by_id(a, ids_a)
        sel = [(r, bomap[int(nid[i])], aomap[int(nid[i])]) for r, i in enumerate(ni)
               if int(nid[i]) in bomap and int(nid[i]) in aomap]
        if not sel:
            continue
        r = np.array([s[0] for s in sel])
        before, after = bwo[[s[1] for s in sel]], awo[[s[2] for s in sel]]
        mask[r] |= (before == nw[ni[r]]) & (before != after)
    mask &= differs
    if not mask.any():
        return None, False
    out = nw.copy()
    picked = out[ni]
    picked[mask] = bw[bi][mask]
    out[ni] = picked
    return out.tobytes(), True


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


def attach(base, dump, now, client=None, name=None):
    """Return (dump with ids and an updated table, info) for a fresh local dump.

    base is the last synced data (may carry a table). info is None when the
    mesh is not handled here; otherwise it says whether ids changed in the dump,
    which then has to be loaded back into the datablock. client forces a client
    number (tests); otherwise the number is claimed for name, by default the
    Multi-User username.
    """
    if not ENABLED or not is_mesh(dump):
        return dump, None
    if unsupported(dump):
        dump = dict(dump)
        dump.pop(TOPO_KEY, None)
        return dump, None
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
        topo = {"v": {}, "f": {}, "e": {}, "c": {}, "n": {}}
        for vid, pos in m["verts"].items():
            topo["v"][str(vid)] = [1, 0.0, "init", *pos]
        for key, (smooth, mat) in m["faces"].items():
            topo["f"][kstr(key)] = [1, 0.0, "init", smooth, mat]
        for key in m["edges"]:
            topo["e"][kstr(key)] = [1, 0.0, "init"]
    client = client or claim(topo, name or username(), now)

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

    # Last synced positions, kept in a tombstone so a hidden deletion can be undone,
    # and the synced face flags, so only a flag the user changed here is an edit.
    last = {}
    synced_faces = None
    if synced is not None and len(synced_ids) == len(_buf(synced["vertices"], "co", np.float32, 3)):
        base_co = _buf(synced["vertices"], "co", np.float32, 3)
        last = {int(synced_ids[i]): [float(c) for c in base_co[i]] for i in range(len(base_co))}
        if check(synced) is None:
            synced_faces = decode(synced, synced_ids)["faces"]

    # A commit whose mesh is the table's view, while that view hides a conflict,
    # only writes down how the conflict was resolved. Every client that applies
    # the resolution makes one at about the same time; they never conflict.
    # It records what it settled, so a client that never saw the conflict open
    # can still tell its user.
    settle = None
    if base_topo:
        bv = view(topo)
        if bv["hidden"] > 0 and set(mesh["verts"]) == set(bv["verts"]) \
                and set(mesh["faces"]) == set(bv["faces"]) and mesh["edges"] == bv["edges"]:
            cs = topo["c"]
            settle = [[cl["win"], cl["lose"], cl["hidden"], name_of(topo, cs[cl["win"]][0]),
                       [name_of(topo, cs[c][0]) for c in cl["lose"]]] for cl in bv["clashes"]]

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
        else:
            # A flag change goes to "p" with its own stamp. It is an edit only where
            # the flag differs from what this Blender last loaded: a flag the table
            # already changed, and the load has not shown yet, is not one. Writing
            # it into the face entry would also revive a face deleted at once.
            was = synced_faces.get(key) if synced_faces is not None else None
            if was is None:
                was = face_flags(topo, ks, e)
            if was != (smooth, mat):
                topo.setdefault("p", {})[ks] = [now, smooth, mat]
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
        seen = dict(topo.get("s") or {})
        for c in topo["c"].values():
            seen[str(c[0])] = max(seen.get(str(c[0]), 0.0), c[1])
        topo["c"][cid] = [client, now, seen, 1, settle] if settle is not None else [client, now, seen]
        acks = topo.setdefault("k", {})
        mine = dict(acks.get(str(client)) or {})
        for k, t in list(seen.items()) + [(str(client), now)]:
            mine[k] = max(mine.get(k, -1.0), t)
        acks[str(client)] = mine
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
    flags = dict(ta.get("p") or {})
    for k, e in (tb.get("p") or {}).items():
        if k not in flags or (e[0], repr(e)) > (flags[k][0], repr(flags[k])):
            flags[k] = e
    if flags:
        out["p"] = flags
    reg = dict(ta.get("n") or {})
    for no, entry in (tb.get("n") or {}).items():
        mine = reg.get(no)
        if mine is not None and mine[0] != entry[0]:
            logging.warning(f"topo3: '{mine[0]}' and '{entry[0]}' claimed client number {no} at once")
        if mine is None or (entry[1], entry[0]) < (mine[1], mine[0]):
            reg[no] = entry
    out["n"] = reg
    acks = {x: dict(v) for x, v in (ta.get("k") or {}).items()}
    for x, v in (tb.get("k") or {}).items():
        mine = acks.setdefault(x, {})
        for k, t in v.items():
            mine[k] = max(mine.get(k, -1.0), t)
    out["k"] = acks
    pruned = dict(ta.get("s") or {})
    for k, t in (tb.get("s") or {}).items():
        pruned[k] = max(pruned.get(k, -1.0), t)
    out["s"] = pruned
    return out


def prune(topo):
    """The table without commits every active client has seen, and without the
    tombstones they left. Returns topo itself when there is nothing to drop.
    Decided from the table alone, so every replica prunes the same way."""
    commits = topo.get("c") or {}
    acks = topo.get("k") or {}
    if not commits:
        return topo
    last = {x: v.get(x, -1.0) for x, v in acks.items()}
    newest = max(last.values(), default=0.0)
    active = [x for x, t in last.items() if t >= newest - PRUNE_AGE]
    newest = max([newest] + [c[1] for c in commits.values()])
    stable = {cid for cid, (author, t, *_) in commits.items()
              if t <= newest - PRUNE_SETTLE
              and all(x == str(author) or acks[x].get(str(author), -1.0) >= t for x in active)}
    if not stable:
        return topo
    out = dict(topo)
    out["c"] = {cid: c for cid, c in commits.items() if cid not in stable}
    pruned = dict(topo.get("s") or {})
    for cid in stable:
        author, t = str(commits[cid][0]), commits[cid][1]
        pruned[author] = max(pruned.get(author, -1.0), t)
    out["s"] = pruned
    for sec in ("v", "f", "e"):
        out[sec] = {k: e for k, e in topo.get(sec, {}).items() if e[0] or e[2] in out["c"]}
    if topo.get("p"):
        # a flag entry outlives neither its face nor a newer face entry
        out["p"] = {k: e for k, e in topo["p"].items() if k in out["f"] and e[0] >= out["f"][k][1]}
    return out


def face_flags(topo, ks, e):
    """(use_smooth, material_index) of face entry e: from "p" when that is newer."""
    p = (topo.get("p") or {}).get(ks)
    if p is not None and p[0] >= e[1]:
        return bool(p[1]), int(p[2])
    return bool(e[3]), int(e[4])


def same_mesh(x, y):
    return x["verts"] == y["verts"] and x["faces"] == y["faces"] and x["edges"] == y["edges"]


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
    units = [u for u in _units(topo) if u["cid"] in commits and len(commits[u["cid"]]) < 4]
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
    clashes = []
    for members in clusters.values():
        cids = {units[i]["cid"] for i in members}
        if len(cids) < 2:
            continue
        win = max(cids, key=lambda c: (commits[c][1], c))
        losers = {c for c in cids if c != win and _concurrent(commits[c], commits[win])}
        if not losers:
            continue
        conflicts += 1
        dropped = set()
        for i in members:
            if units[i]["cid"] in losers:
                dropped.update(units[i]["elems"])
        hidden |= dropped
        clashes.append({"win": win, "lose": sorted(losers), "hidden": len(dropped)})

    def alive(sec, k, e):
        return (not e[0]) if (sec, k) in hidden else bool(e[0])

    verts = {int(k): tuple(e[3:6]) for k, e in topo.get("v", {}).items() if alive("v", k, e)}
    faces = {}
    for k, e in topo.get("f", {}).items():
        key = kparse(k)
        if alive("f", k, e) and all(v in verts for v in key):
            faces[key] = face_flags(topo, k, e)
    edges = set()
    for k, e in topo.get("e", {}).items():
        key = kparse(k)
        if alive("e", k, e) and all(v in verts for v in key):
            edges.add(key)
    return {"verts": verts, "faces": faces, "edges": edges, "conflicts": conflicts, "hidden": len(hidden),
            "clashes": clashes}


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
    skin, skin_t, has_skin = {}, {}, False
    for d, ts in sources:
        ids = _ids_matching(d)
        if ids is None or check(d) is not None:
            continue  # a dump whose parts disagree would put values on the wrong ids
        co = _buf(d["vertices"], "co", np.float32, 3)
        vts = _vertex_stamps(d, ts)
        for i, vid in enumerate(ids):
            vid = int(vid)
            if vid in v["verts"] and (vid not in pos_t or vts[i] > pos_t[vid]):
                pos[vid], pos_t[vid] = co[i], float(vts[i])
        if d.get(SKIN_KEY):
            has_skin = True
            rows = _buf(d[SKIN_KEY], "data", np.float32, 4)
            sts = _row_stamps(_child_ts(ts, SKIN_KEY), "data", len(rows), 4)
            for i, vid in enumerate(ids):
                vid = int(vid)
                if vid in v["verts"] and (vid not in skin_t or sts[i] > skin_t[vid]):
                    skin[vid], skin_t[vid] = rows[i], float(sts[i])
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
    if has_skin:
        data[SKIN_KEY] = {"data": np.array([skin.get(vid, SKIN_DEFAULT) for vid in order],
                                           dtype=np.float32).reshape(-1, 4).tobytes()}
    return data, co_ts.tobytes()


def _child_ts(ts, key):
    if isinstance(ts, dict):
        return ts.get(key)
    return ts


def _row_stamps(ts, key, n, width):
    """Per-row stamp of a buffer of n rows of `width` words: the newest word."""
    t = _child_ts(ts, key)
    if isinstance(t, (bytes, bytearray)) and len(t) == n * width * 8:
        return np.frombuffer(t, dtype=np.float64).reshape(-1, width).max(axis=1)
    from .merge3 import _tmax
    return np.full(n, _tmax(t))


def _flags_from_view(d, v):
    """d with each face's use_smooth and material_index taken from view v, so a
    merge that kept one side's face buffers still shows the table's flags. The
    buffers stamp four faces per word; per-face flags live in the table."""
    ids = _ids_matching(d)
    if ids is None or check(d) is not None:
        return d
    vi = _buf(d["loops"], "vertex_index", np.int32)
    ls = _buf(d["polygons"], "loop_start", np.int32)
    lt = _buf(d["polygons"], "loop_total", np.int32)
    smooth = _buf(d["polygons"], "use_smooth", bool).copy()
    mat = _buf(d["polygons"], "material_index", np.int32).copy()
    if len(smooth) != len(ls):
        smooth = np.zeros(len(ls), dtype=bool)
    if len(mat) != len(ls):
        mat = np.zeros(len(ls), dtype=np.int32)
    changed = False
    for p in range(len(ls)):
        want = v["faces"].get(face_key(ids[j] for j in vi[ls[p]:ls[p] + lt[p]]))
        if want is not None and (bool(smooth[p]), int(mat[p])) != want:
            smooth[p], mat[p] = want
            changed = True
    if not changed:
        return d
    d = dict(d)
    d["polygons"] = dict(d["polygons"])
    d["polygons"]["use_smooth"] = smooth.tobytes()
    d["polygons"]["material_index"] = mat.tobytes()
    return d


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
    global LAST
    topo = merge_topo(a[TOPO_KEY], b[TOPO_KEY])
    v = view(topo)
    small = prune(topo)
    if small is not topo and same_mesh(view(small), v):
        topo = small
    LAST = (v, topo)
    ra = {k: x for k, x in a.items() if k != TOPO_KEY}
    rb = {k: x for k, x in b.items() if k != TOPO_KEY}
    bad_a, bad_b = check(ra), check(rb)
    if bad_a or bad_b:
        logging.warning(f"topo3: rebuilding a mesh from its table, a dump does not match its counts: "
                        f"{bad_a or bad_b}")
    if not bad_a and not bad_b and _same_layout(a, b) and matches_view(a, v):
        out, ot = lww(ra, ats, rb, bts)
        out = _flags_from_view(out, v)
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
