# Probe: what happens to integer ID attributes on verts and faces through common edit operators.
import bpy, bmesh
from collections import Counter

def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    me = ob.data
    va = me.attributes.new("mu_vid", 'INT', 'POINT')
    fa = me.attributes.new("mu_fid", 'INT', 'FACE')
    for i, d in enumerate(va.data):
        d.value = 100 + i
    for i, d in enumerate(fa.data):
        d.value = 500 + i
    return ob

def report(name, ob):
    me = ob.data
    v = [d.value for d in me.attributes["mu_vid"].data]
    f = [d.value for d in me.attributes["mu_fid"].data]
    vc, fc = Counter(v), Counter(f)
    print(f"RESULT {name}: verts={len(v)} unique={len(vc)} dup_ids={sorted(k for k,c in vc.items() if c>1)} zero={v.count(0)} "
          f"new_vals={sorted(set(v) - set(range(100,108)))} | faces={len(f)} unique={len(fc)} dup_fids={sorted(k for k,c in fc.items() if c>1)} zero={f.count(0)}")

def edit(fn):
    ob = fresh()
    bpy.ops.object.mode_set(mode='EDIT')
    fn(ob)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob

def select_top_face(ob):
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    for p in ob.data.polygons:
        p.select = p.normal.z > 0.9
    bpy.ops.object.mode_set(mode='EDIT')

def extrude(ob):
    select_top_face(ob)
    bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, 0.5)})

def subdivide(ob):
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.subdivide(number_cuts=1)

def edgering(ob):
    # loop cut uses an edge ring subdivide; run the bmesh op on 4 vertical edges
    bm = bmesh.from_edit_mesh(ob.data)
    ring = [e for e in bm.edges if abs(e.verts[0].co.z - e.verts[1].co.z) > 1]
    bmesh.ops.subdivide_edgering(bm, edges=ring, cuts=1)
    bmesh.update_edit_mesh(ob.data)

def inset(ob):
    select_top_face(ob)
    bpy.ops.mesh.inset(thickness=0.2)

def bevel(ob):
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(ob.data)
    bm.edges.ensure_lookup_table()
    bm.edges[0].select = True
    bmesh.update_edit_mesh(ob.data)
    bpy.ops.mesh.bevel(offset=0.2, segments=2)

def merge_two(ob):
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    bm.verts[0].select = True
    bm.verts[1].select = True
    bmesh.update_edit_mesh(ob.data)
    bpy.ops.mesh.merge(type='CENTER')

def dissolve(ob):
    subdivide(ob)
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    bm.verts[-1].select = True
    bmesh.update_edit_mesh(ob.data)
    bpy.ops.mesh.dissolve_verts()

def delete_vert(ob):
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    bm.verts[0].select = True
    bmesh.update_edit_mesh(ob.data)
    bpy.ops.mesh.delete(type='VERT')

def connect(ob):
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    f = bm.faces[0]
    a, b = f.verts[0], f.verts[2]
    a.select = b.select = True
    bmesh.update_edit_mesh(ob.data)
    bpy.ops.mesh.vert_connect_path()

def knife_project_like(ob):
    # bisect cuts across faces like a knife line
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.bisect(plane_co=(0, 0, 0.3), plane_no=(0, 0, 1))

for name, fn in [("extrude", extrude), ("subdivide", subdivide), ("loopcut_edgering", edgering),
                 ("inset", inset), ("bevel", bevel), ("merge_center", merge_two), ("dissolve_vert", dissolve),
                 ("delete_vert", delete_vert), ("connect_path", connect), ("bisect_cut", knife_project_like)]:
    try:
        report(name, edit(fn))
    except Exception as e:
        print(f"RESULT {name}: ERROR {e!r}")
