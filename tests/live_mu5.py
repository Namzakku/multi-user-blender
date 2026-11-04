"""Live checks for the mu5 fixes, two Blenders, each started with

    ROLE=host|client NAME=<user> IP=<host ip> T0=<shared epoch seconds> MODE=extras|splitjoin
    blender --python live_mu5.py

Both log to /tmp/mu5_<NAME>.log every 5 s (one JSON object per line) and quit
at the end. MODE=extras, on the shared clock T0:
  +8    the host makes Shared_box, Guides (one vertex, Skin modifier added in
        Object Mode), Mods (cube with Mirror, Skin, Subdivision) and Doomed2
  +50   both add a guide vertex to Guides at once: host at (1, 0, 0) with
        skin radius 0.5, client at (0, 1, 0) with radius 0.9
  +60   the host applies Mods' modifiers one by one, 2 s apart
  +72   the client moves Mods, a later sync from the other user
  +76   the client nudges Shared_box every 0.2 s for 10 s, so the host is
        applying incoming updates while it removes objects
  +80   the host removes Doomed2 (scene collection) and Doomed3 (child
        collection Props) with bpy.data.objects.remove from a timer
  +90   both smooth one face of Shared_box's -x side at once, neighbours
  +125  last status line, then Blender quits
  Expected on both: Guides has a skin layer, radius 0.5 at (1, 0, 0) and 0.9
  at (0, 1, 1); Mods has no modifiers; Doomed2 and Doomed3 are gone; both
  faces smooth.
  NO_SKIN=1 leaves Guides out (on mu4 its skin load ended the client's session
  at +60, which hid the other checks). DEBUG=1 logs the skin rows Multi-User
  holds for Guides.
MODE=splitjoin, the planner's one-editor sequence of 2026-11-03:
  +50   the host separates the top centre faces of Shared_box into "part"
  +70   the host joins "part" back and merges the doubled seam vertices
  CLIENT_OP (optional, mesh_ops name such as poke@side) at +60 on the client
  +100  last status line, then Blender quits
  Expected on both: Shared_box's geometry hash equals the untouched box with
  CLIENT_OP applied (expected_meshes.py none <CLIENT_OP>).
MODE=sequence: the host runs the mesh_ops edits in SEQ (comma separated) on
  Shared_box at +50, +65, ...; CLIENT_OP (optional) runs at +57 on the client.
  With SEQ=dissolve@top,merge_edge@side the merge keeps the face count after a
  dissolve, which is what made mu4 put faces on the wrong vertices with one
  editor. Expected: the third GEO of expected_meshes.py <SEQ op 1> <SEQ op 2>.
"""
import json
import os
import sys
import time

import bmesh
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mesh_ops  # noqa: E402

ROLE, NAME, MODE = os.environ["ROLE"], os.environ["NAME"], os.environ.get("MODE", "extras")
IP = os.environ.get("IP", "127.0.0.1")
T0 = float(os.environ["T0"])
END = 125 if MODE == "extras" else 100
NO_SKIN = os.environ.get("NO_SKIN") == "1"  # leave out Guides (on mu4 its skin load ends the session)
PKG = next((a for a in bpy.context.preferences.addons.keys() if a.endswith(".multi_user")),
           "bl_ext.user_default.multi_user")
LOG = f"/tmp/mu5_{NAME}.log"


def note(m):
    m["t"] = round(time.time() - T0, 1)
    with open(LOG, "a") as f:
        f.write(json.dumps(m) + "\n")


def ctx(**kw):
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    return bpy.context.temp_override(window=win, area=area, **kw)


def at(offset, fn):
    def run():
        try:
            fn()
        except Exception as e:
            note({"error": f"{fn.__name__}: {e!r}"})
        return None
    bpy.app.timers.register(run, first_interval=max(0.1, T0 + offset - time.time()))


def only(o):
    for x in bpy.context.view_layer.objects:
        x.select_set(False)
    o.select_set(True)
    bpy.context.view_layer.objects.active = o


def start():
    p = bpy.context.preferences.addons[PKG].preferences
    p.username = NAME
    if len(p.server_preset) == 0:
        p.server_preset.add()
    p.server_preset[0].ip, p.server_preset[0].port = IP, 5555
    bpy.context.window_manager.server_index = 0
    with ctx():
        if ROLE == "host":
            for o in list(bpy.data.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            r = bpy.ops.wm.session_host()
        else:
            r = bpy.ops.wm.session_join()
    note({"start": str(r), "mode": MODE})
    if ROLE == "host":
        at(8, make_objects)
        at(12, touch_objects)
    if MODE == "extras":
        if not NO_SKIN:
            at(50, add_guide)
        if ROLE == "host":
            for k in range(3):
                at(60 + 2 * k, apply_first_modifier)
            at(80, remove_by_script)
        else:
            at(72, move_mods)
            for k in range(50):  # incoming updates on the host while it removes objects
                at(76 + 0.2 * k, nudge_box)
        at(90, smooth_one)
    elif MODE == "sequence":
        if ROLE == "host":
            for i, op in enumerate(os.environ.get("SEQ", "").split(",")):
                at(50 + 15 * i, lambda op=op: host_edit(op))
        elif os.environ.get("CLIENT_OP", "none") != "none":
            at(57, client_edit)
    else:
        if ROLE == "host":
            at(50, split_part)
            at(70, join_part)
        elif os.environ.get("CLIENT_OP", "none") != "none":
            at(60, client_edit)
    bpy.app.timers.register(status, first_interval=5)
    return None


def make_objects():
    with ctx():
        mesh_ops.make_box("Shared_box")
        if MODE == "extras" and not NO_SKIN:
            me = bpy.data.meshes.new("Guides")
            me.from_pydata([(0.0, 0.0, 0.0), (0.0, 0.0, 1.0)], [(0, 1)], [])
            o = bpy.data.objects.new("Guides", me)
            bpy.context.scene.collection.objects.link(o)
            o.location = (3.0, 0.0, 0.0)
            only(o)
            bpy.ops.object.modifier_add(type="SKIN")           # makes the skin layer, Object Mode
        if MODE == "extras":
            bpy.ops.mesh.primitive_cube_add(size=0.5, location=(-3, 0, 1.0))
            mods = bpy.context.active_object
            mods.name = "Mods"
            for kind in ("MIRROR", "SKIN", "SUBSURF"):
                mods.modifiers.new(kind.title(), kind)
            bpy.ops.mesh.primitive_cube_add(size=0.5, location=(2, 2, 1.0))
            bpy.context.active_object.name = "Doomed2"
            props = bpy.data.collections.new("Props")
            bpy.context.scene.collection.children.link(props)
            me3 = bpy.data.meshes.new("Doomed3")
            me3.from_pydata([(0, 0, 0), (0.3, 0, 0), (0, 0.3, 0)], [], [(0, 1, 2)])
            props.objects.link(bpy.data.objects.new("Doomed3", me3))
    note({"made": sorted(o.name for o in bpy.data.objects)})


def touch_objects():
    for o in bpy.data.objects:
        o.location.z += 0.001  # first owner edit sends the object
    for x in bpy.context.view_layer.objects:
        x.select_set(False)


def add_guide():
    o = bpy.data.objects.get("Guides")
    if o is None:
        note({"skipped": "no Guides"})
        return
    # from different base vertices: one shared vertex would be a conflict by design
    pos, r, src = ((1.0, 0.0, 0.0), 0.5, 0) if ROLE == "host" else ((0.0, 1.0, 1.0), 0.9, 1)
    with ctx():
        only(o)
        bpy.ops.object.mode_set(mode="EDIT")
        bm = bmesh.from_edit_mesh(o.data)
        bm.verts.ensure_lookup_table()
        v0 = next(v for v in bm.verts if abs(v.co.z - src) < 1e-4 and v.co.length < 1.01)
        nv = bm.verts.new(pos, v0)          # copies v0's data, as extrude does
        bm.edges.new((v0, nv))
        layer = bm.verts.layers.skin.verify()
        nv[layer].radius = (r, r)
        bmesh.update_edit_mesh(o.data)
        bpy.ops.object.mode_set(mode="OBJECT")
    note({"guide": pos, "radius": r})


def apply_first_modifier():
    o = bpy.data.objects.get("Mods")
    if o is None or not o.modifiers:
        note({"apply_skipped": [m.name for m in o.modifiers] if o else "no Mods"})
        return
    name = o.modifiers[0].name
    with ctx(object=o, active_object=o):
        only(o)
        r = bpy.ops.object.modifier_apply(modifier=name)
    note({"applied": name, "result": str(r), "left": [m.name for m in o.modifiers]})


def move_mods():
    o = bpy.data.objects.get("Mods")
    if o is not None:
        o.location.x += 0.5
        note({"moved": "Mods", "modifiers": [m.name for m in o.modifiers]})


def nudge_box():
    o = bpy.data.objects.get("Shared_box")
    if o is not None:
        o.location.y += 0.001


def remove_by_script():
    gone = []
    for name in ("Doomed2", "Doomed3"):  # in the scene collection, and in a child collection
        o = bpy.data.objects.get(name)
        if o is not None:
            bpy.data.objects.remove(o)
            gone.append(name)
    note({"removed": gone})


def side_faces(me):
    out = []
    for p in me.polygons:
        c = p.center
        if c.x < -0.49:
            out.append(((round(c.z, 3), round(c.y, 3)), p.index))
    return [i for _, i in sorted(out)]


def smooth_one():
    o = bpy.data.objects.get("Shared_box")
    i = side_faces(o.data)[0 if ROLE == "host" else 1]
    o.data.polygons[i].use_smooth = True
    o.data.update()
    note({"smooth": [round(c, 3) for c in o.data.polygons[i].center]})


def split_part():
    o = bpy.data.objects.get("Shared_box")
    with ctx():
        only(o)
        bpy.ops.object.mode_set(mode="EDIT")
        bm = bmesh.from_edit_mesh(o.data)
        for f in bm.faces:
            f.select_set(mesh_ops.REGIONS["top"](f))
        bmesh.update_edit_mesh(o.data)
        bpy.ops.mesh.separate(type="SELECTED")
        bpy.ops.object.mode_set(mode="OBJECT")
    part = next(x for x in bpy.data.objects if x.name.startswith("Shared_box") and x is not o)
    part.name = part.data.name = "part"
    note({"split": len(part.data.polygons)})


def join_part():
    o, part = bpy.data.objects.get("Shared_box"), bpy.data.objects.get("part")
    with ctx(active_object=o, selected_editable_objects=[o, part]):
        for x in bpy.context.view_layer.objects:
            x.select_set(x in (o, part))
        bpy.context.view_layer.objects.active = o
        r = bpy.ops.object.join()
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-4)
        bm.to_mesh(o.data)
        bm.free()
        o.data.update()
    note({"joined": str(r), "verts": len(o.data.vertices), "faces": len(o.data.polygons)})


def client_edit():
    host_edit(os.environ["CLIENT_OP"])


def host_edit(op):
    o = bpy.data.objects.get("Shared_box")
    with ctx():
        chosen, r = mesh_ops.edit(o, op)
    note({"edit": op, "faces": chosen, "result": str(r)})


def skin_debug(me):
    """DEBUG=1: the skin rows Multi-User holds for this mesh: the stored node
    data and the merge base, as (vertex count, radius x per vertex)."""
    import numpy as np
    from replication import merge3
    from replication.interface import session
    out = {}
    for uuid in list(session.repository.graph.keys()):
        n = session.repository.graph.get(uuid)
        if n.instance != me:
            continue
        for label, d in (("node", n.data), ("base", merge3._base.get(uuid))):
            if isinstance(d, dict):
                rows = np.frombuffer((d.get("mu_skin") or {}).get("data") or b"", dtype=np.float32).reshape(-1, 4)
                out[label] = [d.get("vertex_count"), [round(float(r[0]), 3) for r in rows]]
        out["state"] = str(n.state)
    return out


def status():
    info = {"objects": sorted(o.name for o in bpy.data.objects)}
    box = bpy.data.objects.get("Shared_box")
    if box is not None and box.mode != "EDIT":
        me = box.data
        info["box"] = [len(me.vertices), len(me.polygons), mesh_ops.geo_hash(me)]
        info["smooth"] = sorted([round(c, 3) for c in p.center] for p in me.polygons if p.use_smooth)
    g = bpy.data.objects.get("Guides")
    if g is not None and g.mode != "EDIT":
        me = g.data
        info["skin_layers"] = len(me.skin_vertices)
        if me.skin_vertices:
            info["radii"] = sorted([[round(c, 3) for c in v.co], round(s.radius[0], 3)]
                                   for v, s in zip(me.vertices, me.skin_vertices[0].data))
        if os.environ.get("DEBUG"):
            info["debug"] = skin_debug(me)
    m = bpy.data.objects.get("Mods")
    if m is not None:
        info["mods"] = [x.name for x in m.modifiers]
    note(info)
    if time.time() - T0 > END:
        bpy.ops.wm.quit_blender()
        return None
    return 5.0


bpy.app.timers.register(start, first_interval=4)
