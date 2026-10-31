"""The test box and the region edits shared by live_session.py and expected_meshes.py.

Every edit selects the faces whose centre lies in the middle of the box top
(the 2 by 2 block of the 4 by 4 grid, or whatever replaced it) and runs one
Edit Mode operator on them.
"""
import bmesh
import bpy

OPS = {
    "poke": lambda: bpy.ops.mesh.poke(),
    "inset": lambda: bpy.ops.mesh.inset(thickness=0.05),
    "bevel": lambda: bpy.ops.mesh.bevel(offset=0.05, segments=1, affect="EDGES"),
    "extrude": lambda: bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0.0, 0.0, 0.1)}),
}


def in_centre(face):
    c = face.calc_center_median()
    return c.z > 0.49 and max(abs(c.x), abs(c.y)) < 0.26


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
    """Select the faces pick accepts on object o and run OPS[op]. Needs a context
    with a 3D view (or background mode). Returns (faces selected, result)."""
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
    r = OPS[op]()
    bpy.ops.object.mode_set(mode="OBJECT")
    o.select_set(False)
    return len(chosen), r
