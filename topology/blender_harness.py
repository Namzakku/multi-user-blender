"""Check topomerge against real Blender edit operators.

Run: blender -b --factory-startup --python blender_harness.py
Prints one "HARNESS <scenario> PASS|FAIL ..." line per scenario, then "HARNESS DONE".
"""

import os
import sys
import traceback

import bmesh
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import topomerge as tm  # noqa: E402

ATTR = "mu_vid"


# Blender helpers

def make_base():
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8)
    ob = bpy.context.active_object
    ob.name = "Base"
    attr = ob.data.attributes.new(ATTR, 'INT', 'POINT')
    for i, d in enumerate(attr.data):
        d.value = i + 1
    return ob


def duplicate(ob, name):
    dup = ob.copy()
    dup.data = ob.data.copy()
    dup.name = name
    bpy.context.collection.objects.link(dup)
    return dup


def activate(ob):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob


def select_faces(ob, test):
    """Select faces whose center passes test, then enter Edit Mode in face select."""
    activate(ob)
    me = ob.data
    for v in me.vertices:
        v.select = False
    for e in me.edges:
        e.select = False
    n = 0
    for p in me.polygons:
        p.select = bool(test(p.center))
        n += p.select
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='FACE')
    return n


def leave_edit():
    bpy.ops.object.mode_set(mode='OBJECT')


def read_side(ob, base_verts, client):
    """Read ids and coords, repair duplicated ids, write them back, return the Mesh."""
    me = ob.data
    attr = me.attributes[ATTR]
    ids = [d.value for d in attr.data]
    coords = [tuple(v.co) for v in me.vertices]
    fixed = tm.repair_ids(ids, coords, base_verts, tm.make_allocator(client))
    for d, vid in zip(attr.data, fixed):
        d.value = vid
    return to_mesh(me, fixed), sum(1 for x, y in zip(ids, fixed) if x != y)


def to_mesh(me, ids):
    face_edges = set()
    faces = []
    for p in me.polygons:
        faces.append(tuple(ids[i] for i in p.vertices))
        face_edges.update(p.edge_keys)
    edges = [(ids[e.vertices[0]], ids[e.vertices[1]]) for e in me.edges
             if tuple(sorted(e.vertices)) not in face_edges]
    verts = {ids[i]: tuple(v.co) for i, v in enumerate(me.vertices)}
    return {"verts": verts, "faces": faces, "edges": edges}


def build(mesh, name):
    order = list(mesh["verts"])
    index = {vid: i for i, vid in enumerate(order)}
    me = bpy.data.meshes.new(name)
    me.from_pydata([mesh["verts"][v] for v in order],
                   [(index[a], index[b]) for a, b in mesh["edges"]],
                   [[index[v] for v in f] for f in mesh["faces"]])
    me.validate()
    attr = me.attributes.new(ATTR, 'INT', 'POINT')
    for d, vid in zip(attr.data, order):
        d.value = vid
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    return ob


def non_manifold(me):
    bm = bmesh.new()
    bm.from_mesh(me)
    n = sum(1 for e in bm.edges if not e.is_manifold)
    stats = (len(bm.verts), len(bm.edges), len(bm.faces))
    bm.free()
    return n, stats


# Scenarios. Each edits side A and side B with real operators.

def edit_disjoint(a, b):
    select_faces(a, lambda c: c.x > 0.6 and abs(c.z) < 0.3)
    bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0.2, 0, 0)})
    leave_edit()
    select_faces(b, lambda c: c.x < -0.6 and abs(c.z) < 0.3)
    bpy.ops.mesh.subdivide(number_cuts=1)
    leave_edit()
    # A plain move near the top pole on B.
    near = min(b.data.vertices, key=lambda v: (v.co - _vec(0.38, 0, 0.92)).length)
    near.co.z += 0.1
    return {"expect_conflicts": 0}


def edit_overlap(a, b):
    select_faces(a, lambda c: c.z > 0.85)
    bpy.ops.mesh.inset(thickness=0.05)
    leave_edit()
    select_faces(a, lambda c: c.x < -0.6 and abs(c.z) < 0.3)
    bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (-0.2, 0, 0)})
    leave_edit()
    select_faces(b, lambda c: c.z > 0.85)
    bpy.ops.mesh.subdivide(number_cuts=1)
    leave_edit()
    return {"expect_conflicts": 1, "expect_winner": "b"}


def edit_delete(a, b):
    select_faces(a, lambda c: c.y > 0.6)
    bpy.ops.mesh.delete(type='FACE')
    leave_edit()
    select_faces(b, lambda c: c.y < -0.6)
    bpy.ops.mesh.subdivide(number_cuts=1)
    leave_edit()
    return {"expect_conflicts": 0}


def _vec(x, y, z):
    from mathutils import Vector
    return Vector((x, y, z))


def run(name, edit, base_ob, stamps=(1, 2)):
    a = duplicate(base_ob, f"{name}_A")
    b = duplicate(base_ob, f"{name}_B")
    expect = edit(a, b)
    attr = base_ob.data.attributes[ATTR]
    base = to_mesh(base_ob.data, [d.value for d in attr.data])

    side_a, fixed_a = read_side(a, base["verts"], 1)
    side_b, fixed_b = read_side(b, base["verts"], 2)
    merged, report = tm.merge(base, side_a, side_b, *stamps)
    errors = [p for p in tm.validate(merged) if not p.startswith("warning:")]
    warnings = [p for p in tm.validate(merged) if p.startswith("warning:")]
    out = build(merged, f"{name}_merged")

    nm_merged, stats = non_manifold(out.data)
    nm_a, _ = non_manifold(a.data)
    nm_b, _ = non_manifold(b.data)
    da, db = tm.diff(base, side_a), tm.diff(base, side_b)
    checks = []

    checks.append(("validate", not errors))
    checks.append(("built counts match", stats[0] == len(merged["verts"]) and stats[2] == len(merged["faces"])))
    checks.append(("conflicts", len(report["conflicts"]) == expect["expect_conflicts"]))
    if expect["expect_conflicts"] == 0:
        # Every edit from both sides is present.
        want_v = len(base["verts"]) - len(da.removed_verts | db.removed_verts) + len(da.added_verts) + len(db.added_verts)
        want_f = len(base["faces"]) - len(da.removed_faces | db.removed_faces) + len(da.added_faces) + len(db.added_faces)
        checks.append(("vert count", len(merged["verts"]) == want_v))
        checks.append(("face count", len(merged["faces"]) == want_f))
        checks.append(("all a faces", all(k in set(merged["faces"]) for k in da.added_faces)))
        checks.append(("all b faces", all(k in set(merged["faces"]) for k in db.added_faces)))
        checks.append(("moves kept", all(merged["verts"].get(v) == co for v, co in {**da.moved_verts, **db.moved_verts}.items()
                                         if v in merged["verts"])))
        checks.append(("non-manifold", nm_merged == nm_a + nm_b))
    else:
        c = report["conflicts"][0]
        checks.append(("winner", c["winner"] == expect["expect_winner"]))
        checks.append(("non-manifold", nm_merged == 0))
        # A's far extrude has to survive the conflict at the pole.
        far = {k for k in da.added_faces
               if all(abs(side_a["verts"][v][2]) < 0.5 for v in k)}
        checks.append(("far a edit kept", bool(far) and far <= set(merged["faces"])))
        checks.append(("pole is b's", all(k in set(merged["faces"]) for k in db.added_faces)))

    failed = [n for n, ok in checks if not ok]
    status = "PASS" if not failed else "FAIL"
    print(f"HARNESS {name} {status} base_v={len(base['verts'])} a_v={len(side_a['verts'])} "
          f"b_v={len(side_b['verts'])} merged_v={len(merged['verts'])} merged_f={len(merged['faces'])} "
          f"repaired_a={fixed_a} repaired_b={fixed_b} conflicts={report['conflicts']} "
          f"nonmanifold a={nm_a} b={nm_b} merged={nm_merged} applied={report['applied']} "
          f"orphans={report['orphans_dropped']} warnings={len(warnings)} failed={failed} errors={errors[:3]}")


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    base_ob = make_base()
    for name, edit in (("disjoint", edit_disjoint), ("overlap", edit_overlap), ("delete", edit_delete)):
        try:
            run(name, edit, base_ob)
        except Exception:
            print(f"HARNESS {name} FAIL exception")
            traceback.print_exc()
    print("HARNESS DONE")


main()
