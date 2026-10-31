"""ID-based three-way merge for concurrent topology edits on one mesh.

Prototype for the Multi-User patch, see ../TOPOLOGY-DESIGN.md. Pure Python, no bpy.

A mesh is a dict:
    "verts": {vertex id: (x, y, z)}
    "faces": [tuple of vertex ids in winding order]
    "edges": [(id, id)]   loose edges only, edges used by no face

Vertex ids are permanent and come from the `mu_vid` attribute. Blender copies an
existing id onto every new vertex an edit creates, so `repair_ids` hands those
copies fresh ids before a mesh is diffed.
"""

import math
from dataclasses import dataclass, field

MOVE_EPS = 1e-9
CLIENT_SHIFT = 24


def face_key(face):
    """Rotate a face so its smallest id comes first. Winding is kept."""
    face = tuple(face)
    i = face.index(min(face))
    return face[i:] + face[:i]


def edge_key(edge):
    a, b = edge
    return (a, b) if a <= b else (b, a)


def make_allocator(client_number, start=1):
    """Fresh ids for one client: client number in the high bits, a counter below."""
    if not 1 <= client_number <= 127:
        raise ValueError("client_number must be in 1..127")
    state = {"n": start}

    def alloc():
        value = (client_number << CLIENT_SHIFT) | state["n"]
        state["n"] += 1
        return value
    return alloc


def repair_ids(ids, coords, base_verts, alloc):
    """Give every vertex a unique id after a local edit.

    In a group of equal ids, the vertex closest to that id's base position keeps
    it (the first one if the id is not in base). The others, and any id <= 0,
    get alloc(). Fresh ids that are already in use are skipped.
    """
    groups = {}
    for i, vid in enumerate(ids):
        groups.setdefault(vid, []).append(i)

    out = list(ids)
    needs = []
    for vid, members in groups.items():
        if vid <= 0:
            needs.extend(members)
            continue
        if len(members) == 1:
            continue
        if vid in base_verts:
            ref = base_verts[vid]
            keeper = min(members, key=lambda i: (math.dist(coords[i], ref), i))
        else:
            keeper = members[0]
        needs.extend(i for i in members if i != keeper)

    used = set(base_verts) | {v for v in ids if v > 0}
    for i in sorted(needs):
        vid = alloc()
        while vid in used:
            vid = alloc()
        used.add(vid)
        out[i] = vid
    return out


def _elements(mesh):
    """Faces and loose edges as one set of tagged keys."""
    elems = {("f", face_key(f)) for f in mesh["faces"]}
    elems |= {("e", edge_key(e)) for e in mesh["edges"]}
    return elems


def _used_verts(elems):
    used = set()
    for _, key in elems:
        used.update(key)
    return used


@dataclass
class Change:
    added_verts: dict = field(default_factory=dict)
    removed_verts: set = field(default_factory=set)
    moved_verts: dict = field(default_factory=dict)
    added_faces: set = field(default_factory=set)
    removed_faces: set = field(default_factory=set)
    added_edges: set = field(default_factory=set)
    removed_edges: set = field(default_factory=set)


def diff(base, side):
    """What one side changed against the shared base, keyed by id."""
    bv, sv = base["verts"], side["verts"]
    bf = {face_key(f) for f in base["faces"]}
    sf = {face_key(f) for f in side["faces"]}
    be = {edge_key(e) for e in base["edges"]}
    se = {edge_key(e) for e in side["edges"]}
    moved = {}
    for vid in bv.keys() & sv.keys():
        if math.dist(bv[vid], sv[vid]) > MOVE_EPS:
            moved[vid] = tuple(sv[vid])
    return Change(
        added_verts={v: tuple(sv[v]) for v in sv.keys() - bv.keys()},
        removed_verts=set(bv.keys() - sv.keys()),
        moved_verts=moved,
        added_faces=sf - bf,
        removed_faces=bf - sf,
        added_edges=se - be,
        removed_edges=be - se,
    )


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _units(base_elems, base_ids, change):
    """Split one side's change into units, each with a footprint of base vertex ids.

    A unit is either one touched base element (removed, or using a removed vertex)
    or one connected group of added elements. Units with an empty footprint are
    floating new geometry.
    """
    added = {("f", k) for k in change.added_faces} | {("e", k) for k in change.added_edges}
    removed = {("f", k) for k in change.removed_faces} | {("e", k) for k in change.removed_edges}
    removed |= {el for el in base_elems if change.removed_verts.intersection(el[1])}

    units = []
    for el in sorted(removed):
        units.append({"footprint": set(el[1]), "removed": {el}, "added": set(), "verts": set()})

    # A removed vertex no base element used still needs a unit to carry the removal.
    in_removed = _used_verts(removed)
    for vid in sorted(change.removed_verts - in_removed):
        units.append({"footprint": {vid}, "removed": set(), "added": set(), "verts": set()})

    uf = _UnionFind()
    for el in added:
        first = el[1][0]
        for vid in el[1][1:]:
            uf.union(first, vid)
    groups = {}
    for el in sorted(added):
        groups.setdefault(uf.find(el[1][0]), set()).add(el)
    for elems in groups.values():
        used = _used_verts(elems)
        units.append({"footprint": used & base_ids, "removed": set(), "added": elems,
                      "verts": used & change.added_verts.keys()})

    # New vertices no added element uses are loose new geometry.
    loose_new = change.added_verts.keys() - _used_verts(added)
    for vid in sorted(loose_new):
        units.append({"footprint": set(), "removed": set(), "added": set(), "verts": {vid}})
    return units


def merge(base, a, b, stamp_a, stamp_b):
    """Three-way merge of two edited copies of base.

    Edits merge region by region. A cluster of base vertices that both sides
    changed goes wholly to the side with the larger stamp (ties go to b).
    Returns (mesh, report).
    """
    base_ids = set(base["verts"])
    base_elems = _elements(base)
    base_loose = base_ids - _used_verts(base_elems)
    ch = {"a": diff(base, a), "b": diff(base, b)}
    stamps = {"a": stamp_a, "b": stamp_b}

    clash = ch["a"].added_verts.keys() & ch["b"].added_verts.keys()
    if clash:
        raise ValueError(f"both sides added the same new vertex ids: {sorted(clash)[:5]}")

    units = {s: _units(base_elems, base_ids, ch[s]) for s in ("a", "b")}

    # Clusters: base vertices joined through every unit footprint from both sides.
    uf = _UnionFind()
    for s in ("a", "b"):
        for u in units[s]:
            fp = sorted(u["footprint"])
            for vid in fp:
                uf.find(vid)
            for vid in fp[1:]:
                uf.union(fp[0], vid)

    sides_in = {}
    for s in ("a", "b"):
        for u in units[s]:
            if u["footprint"]:
                u["cluster"] = uf.find(min(u["footprint"]))
                sides_in.setdefault(u["cluster"], set()).add(s)
            else:
                u["cluster"] = None

    newer = "b" if stamp_b >= stamp_a else "a"
    winner = {}
    conflicts = []
    for root, sides in sorted(sides_in.items()):
        if len(sides) == 1:
            winner[root] = next(iter(sides))
            continue
        winner[root] = newer
        loser = "a" if newer == "b" else "b"
        lost = [u for u in units[loser] if u["cluster"] == root]
        won = [u for u in units[newer] if u["cluster"] == root]
        conflicts.append({
            "winner": newer,
            "base_faces": len({el for u in won + lost for el in u["removed"] if el[0] == "f"}),
            "dropped_added_faces": sum(1 for u in lost for el in u["added"] if el[0] == "f"),
            "dropped_removed_faces": sum(1 for u in lost for el in u["removed"] if el[0] == "f"),
        })

    elems = set(base_elems)
    verts = dict(base["verts"])
    removed_verts = set()
    applied = {}
    for s in ("a", "b"):
        counts = {"added_faces": 0, "removed_faces": 0, "added_edges": 0, "removed_edges": 0,
                  "added_verts": 0, "removed_verts": 0, "moved_verts": 0}
        for u in units[s]:
            if u["cluster"] is not None and winner[u["cluster"]] != s:
                continue
            for el in u["removed"]:
                if el in elems:
                    elems.discard(el)
                    counts["removed_faces" if el[0] == "f" else "removed_edges"] += 1
            for el in u["added"]:
                if el not in elems:
                    elems.add(el)
                    counts["added_faces" if el[0] == "f" else "added_edges"] += 1
            for vid in u["verts"]:
                verts[vid] = ch[s].added_verts[vid]
                counts["added_verts"] += 1
        for vid in ch[s].removed_verts:
            if winner.get(uf.find(vid)) == s:
                removed_verts.add(vid)
                counts["removed_verts"] += 1
        applied[s] = counts

    for vid in removed_verts:
        verts.pop(vid, None)

    # Moves merge per vertex, newer stamp first, wherever the vertex still exists.
    for vid in set(ch["a"].moved_verts) | set(ch["b"].moved_verts):
        if vid not in verts:
            continue
        sides = [s for s in ("a", "b") if vid in ch[s].moved_verts]
        pick = sides[0] if len(sides) == 1 else newer
        verts[vid] = ch[pick].moved_verts[vid]
        applied[pick]["moved_verts"] += 1

    used = _used_verts(elems)
    loose_added = set()
    for s in ("a", "b"):
        loose_added |= ch[s].added_verts.keys() - _used_verts(
            {("f", k) for k in ch[s].added_faces} | {("e", k) for k in ch[s].added_edges})
    orphans = [v for v in verts if v not in used and v not in base_loose and v not in loose_added]
    for vid in orphans:
        del verts[vid]

    mesh = {
        "verts": {v: verts[v] for v in sorted(verts)},
        "faces": sorted(k for t, k in elems if t == "f"),
        "edges": sorted(k for t, k in elems if t == "e"),
    }
    report = {"conflicts": conflicts, "applied": applied, "orphans_dropped": len(orphans)}
    return mesh, report


def validate(mesh):
    """Structural problems in a merged mesh. Strings starting 'warning:' are not errors."""
    problems = []
    verts = mesh["verts"]
    seen = set()
    edge_use = {}
    for f in mesh["faces"]:
        key = face_key(f)
        if len(f) < 3:
            problems.append(f"face {key} has fewer than 3 vertices")
        if len(set(f)) != len(f):
            problems.append(f"face {key} repeats a vertex")
        if key in seen:
            problems.append(f"duplicate face {key}")
        seen.add(key)
        missing = [v for v in f if v not in verts]
        if missing:
            problems.append(f"face {key} uses missing vertices {missing}")
        for i in range(len(f)):
            ek = edge_key((f[i], f[(i + 1) % len(f)]))
            edge_use[ek] = edge_use.get(ek, 0) + 1
    for e in mesh["edges"]:
        missing = [v for v in e if v not in verts]
        if missing:
            problems.append(f"edge {edge_key(e)} uses missing vertices {missing}")
    for ek, n in sorted(edge_use.items()):
        if n > 2:
            problems.append(f"warning: edge {ek} is used by {n} faces")
    return problems
