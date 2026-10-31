"""Plumbing for the GUI concurrent-edit test: host or join the session, the host
creates one shared cube, and both log its state. No modeling of the test edits.
ROLE=host|client NAME=<user> IP=<host ip>."""
import os, hashlib, json, bpy

ROLE, NAME = os.environ["ROLE"], os.environ["NAME"]
IP = os.environ.get("IP", "127.0.0.1")
PKG = "bl_ext.blender_org.multi_user"
LOG = f"/tmp/gui_{NAME}.log"


def note(m):
    with open(LOG, "a") as f:
        f.write(json.dumps(m) + "\n")


def ctx():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    return bpy.context.temp_override(window=win, area=area)


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
    note({"start": str(r)})
    if ROLE == "host":
        bpy.app.timers.register(make_box, first_interval=3)
        bpy.app.timers.register(touch_box, first_interval=6)
    bpy.app.timers.register(status, first_interval=10)
    return None


def make_box():
    with ctx():
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 1.0))
        # v3: a 4x4 grid on every side, so edits on different sides share no vertex
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.subdivide(number_cuts=3)
        bpy.ops.mesh.select_all(action="DESELECT")
        bpy.ops.object.mode_set(mode="OBJECT")
    o = bpy.context.active_object
    o.name = "Shared_box"
    note({"made": o.name})
    return None


def touch_box():
    o = bpy.data.objects["Shared_box"]
    o.location.z += 0.001  # first owner edit, so the new object is sent
    for x in bpy.context.view_layer.objects:
        x.select_set(False)
    note({"touched_and_deselected": True})
    return None


def status():
    o = bpy.data.objects.get("Shared_box")
    if o and o.type == "MESH":
        me = o.data
        if o.mode == "EDIT":
            info = {"mode": "EDIT (mesh not readable)"}
        else:
            co = [round(c, 3) for v in me.vertices for c in v.co]
            info = {"verts": len(me.vertices), "faces": len(me.polygons),
                    "zmax": max(co[2::3]), "zmin": min(co[2::3]),
                    "xmax": max(co[0::3]), "xmin": min(co[0::3]),
                    "hash": hashlib.md5(str(co).encode()).hexdigest()[:10]}
            info.update(id_info(me))
        info.update({"loc": [round(v, 3) for v in o.location], "mode": o.mode,
                     "users": [u.username for u in bpy.context.window_manager.online_users]})
        note(info)
    return 5.0


def id_info(me):
    """v3: a hash of the mesh by permanent vertex id, which does not depend on
    vertex order, and the conflict count of the synced topology table."""
    a = me.attributes.get("mu_vid")
    if a is None:
        return {"id_hash": None}
    ids = [d.value for d in a.data]
    verts = sorted((ids[i], [round(c, 3) for c in v.co]) for i, v in enumerate(me.vertices))
    faces = []
    for p in me.polygons:
        f = [ids[i] for i in p.vertices]
        k = f.index(min(f))
        faces.append(f[k:] + f[:k])
    out = {"id_hash": hashlib.md5(json.dumps([verts, sorted(faces)]).encode()).hexdigest()[:10],
           "unique_ids": len(set(ids)) == len(ids)}
    try:
        from replication.interface import session
        from replication import topo3
        for uuid in list(session.repository.graph.keys()):
            n = session.repository.graph.get(uuid)
            if n.instance == me and isinstance(n.data, dict) and topo3.TOPO_KEY in n.data:
                out["conflicts"] = topo3.view(n.data[topo3.TOPO_KEY])["conflicts"]
    except Exception as e:
        out["view_error"] = repr(e)
    return out


bpy.app.timers.register(start, first_interval=4)
