"""The test box and the region edits shared by live_session.py, live_mu5.py and
expected_meshes.py.

Every edit selects the faces of one region of the box and runs one Edit Mode
step on them. An edit is named "<op>" or "<op>@<region>": op is a key of OPS,
region a key of REGIONS (default "centre", the middle 2 by 2 faces of the top
and of the bottom, as before mu5).
"""
import hashlib
import json

import bmesh
import bpy


def _verts(bm):
    return [v for v in bm.verts if v.select]


def _interior_edges(bm):
    return [e for e in bm.edges if len(e.link_faces) == 2 and all(f.select for f in e.link_faces)]


def _merge_edge(bm):
    """Merge the two vertices of the lowest inner edge of the selection at their
    centre. The faces on that edge turn into triangles, so the face count stays."""
    edges = _interior_edges(bm) or [e for e in bm.edges if all(v.select for v in e.verts)]
    if not edges:
        return None
    key = lambda e: sorted(tuple(round(c, 4) for c in v.co) for v in e.verts)
    a, b = min(edges, key=key).verts
    return bmesh.ops.pointmerge(bm, verts=[a, b], merge_co=(a.co + b.co) / 2)


OPS = {
    "poke": lambda bm: bpy.ops.mesh.poke(),
    "inset": lambda bm: bpy.ops.mesh.inset(thickness=0.05),
    "bevel": lambda bm: bpy.ops.mesh.bevel(offset=0.05, segments=1, affect="EDGES"),
    "extrude": lambda bm: bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0.0, 0.0, 0.1)}),
    # delete-type steps (mu5)
    "delete": lambda bm: bpy.ops.mesh.delete(type="FACE"),
    "delete_verts": lambda bm: bpy.ops.mesh.delete(type="VERT"),
    "dissolve": lambda bm: bpy.ops.mesh.dissolve_faces(),
    "merge": lambda bm: bpy.ops.mesh.merge(type="CENTER"),
    "dissolve_edges": lambda bm: bmesh.ops.dissolve_edges(bm, edges=_interior_edges(bm), use_verts=True),
    "remove_doubles": lambda bm: bmesh.ops.remove_doubles(bm, verts=_verts(bm), dist=0.3),
    "merge_edge": _merge_edge,
}


def in_centre(face):
    """The middle 2 by 2 faces of the box top (face centres are in local space)."""
    c = face.calc_center_median()
    return c.z > 0.49 and max(abs(c.x), abs(c.y)) < 0.26


def _block(axis, sign):
    """The middle 2 by 2 faces of one side of the box, in local space."""
    def pick(face):
        c = face.calc_center_median()
        others = [abs(c[k]) for k in range(3) if k != axis]
        return c[axis] * sign > 0.49 and max(others) < 0.26
    return pick


REGIONS = {"centre": in_centre, "top": _block(2, 1), "bottom": _block(2, -1),
           "side": _block(0, 1), "back": _block(1, 1)}


def parse(spec):
    """'merge@top' -> ('merge', the top region's pick)."""
    op, _, region = spec.partition("@")
    return op, REGIONS[region or "centre"]


def make_box(name="Shared_box"):
    """A cube of size 1 at (0, 0, 1) with a 4 by 4 grid on every side."""
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 1.0))
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=3)
    bpy.ops.mesh.select_all(action="DESELECT")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.active_object.name = name
    return bpy.context.active_object


def edit(o, op, pick=in_centre):
    """Select the faces pick accepts on object o and run OPS[op]. op may also be
    "<op>@<region>". Needs a context with a 3D view (or background mode).
    Returns (faces selected, result)."""
    if "@" in op:
        op, pick = parse(op)
    for x in bpy.context.view_layer.objects:
        x.select_set(False)
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(o.data)
    for f in bm.faces:
        f.select_set(False)
    chosen = [f for f in bm.faces if pick(f)]
    for f in chosen:
        f.select_set(True)
    bmesh.update_edit_mesh(o.data)
    r = OPS[op](bm)
    bmesh.update_edit_mesh(o.data)
    bpy.ops.object.mode_set(mode="OBJECT")
    o.select_set(False)
    return len(chosen), r


def geo_hash(me):
    """The mesh without ids: vertex positions and every face as the rotated list
    of its corner positions, rounded to 1e-4. Equal on two Blenders only if each
    face sits on the same points in the same winding."""
    r = lambda p: [round(c, 4) + 0.0 for c in p]
    verts = sorted(r(v.co) for v in me.vertices)
    faces = []
    for p in me.polygons:
        pts = [r(me.vertices[i].co) for i in p.vertices]
        k = pts.index(min(pts))
        faces.append(pts[k:] + pts[:k])
    return hashlib.md5(json.dumps([verts, sorted(faces)]).encode()).hexdigest()[:10]
