"""Live test of conflict notices, object deletion and table pruning, two Blenders.

ROLE=host|client NAME=<user> IP=<host ip> T0=<shared epoch seconds>
    blender --python live_session.py

Both run the same schedule on the shared clock T0:
  +50   both edit the centre of the box top at once (host HOST_OP, default
        poke; client CLIENT_OP, default inset; see mesh_ops.OPS), a conflict:
        each Blender should show a notice naming the other user
  +90   the host deletes the object "Doomed"; it should vanish on the client
  +200  the client edits the +x side, after it has seen the host's edit
  +330  the host edits the bottom; the settled commits should be pruned
Every 5 s each logs to /tmp/live_<NAME>.log: objects, mesh by id, table size,
conflicts and notices. DUMP=1 also writes the topology table to
/tmp/topo_<NAME>_<t>.json whenever it changes. HOST_DELAY=<s> moves the host's
+50 edit later.
"""
import hashlib
import json
import os
import time

import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mesh_ops  # noqa: E402

ROLE, NAME = os.environ["ROLE"], os.environ["NAME"]
IP = os.environ.get("IP", "127.0.0.1")
T0 = float(os.environ["T0"])
PKG = next((a for a in bpy.context.preferences.addons.keys() if a.endswith(".multi_user")),
           "bl_ext.user_default.multi_user")
LOG = f"/tmp/live_{NAME}.log"


def note(m):
    m["t"] = round(time.time() - T0, 1)
    with open(LOG, "a") as f:
        f.write(json.dumps(m) + "\n")


def ctx():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    return bpy.context.temp_override(window=win, area=area)


def at(offset, fn):
    bpy.app.timers.register(fn, first_interval=max(0.1, T0 + offset - time.time()))


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
    try:  # show the sidebar; the Multiuser tab cannot be picked from a script
        win = bpy.context.window_manager.windows[0]
        area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
        area.spaces.active.show_region_ui = True
    except Exception as e:
        note({"sidebar": repr(e)})
    if ROLE == "host":
        at(8, make_objects)
        at(12, touch_objects)
        at(90, delete_doomed)
        at(330, lambda: edit(lambda f: f.calc_center_median().z < -0.49, "poke"))
    else:
        at(200, lambda: edit(lambda f: f.calc_center_median().x > 0.49, "poke"))
    op = os.environ.get("HOST_OP", "poke") if ROLE == "host" else os.environ.get("CLIENT_OP", "inset")
    at(50 + float(os.environ.get("HOST_DELAY", "0")) * (ROLE == "host"),
       lambda: edit(mesh_ops.in_centre, op))
    bpy.app.timers.register(status, first_interval=5)
    return None


def make_objects():
    with ctx():
        mesh_ops.make_box("Shared_box")
        bpy.ops.mesh.primitive_cube_add(size=0.5, location=(2, 0, 1.0))
        bpy.context.active_object.name = "Doomed"
    note({"made": ["Shared_box", "Doomed"]})
    return None


def touch_objects():
    for name in ("Shared_box", "Doomed"):
        bpy.data.objects[name].location.z += 0.001  # first owner edit sends the object
    for x in bpy.context.view_layer.objects:
        x.select_set(False)
    return None


def edit(pick, op):
    o = bpy.data.objects.get("Shared_box")
    if o is None:
        note({"edit_skipped": "no Shared_box"})
        return None
    with ctx():
        chosen, r = mesh_ops.edit(o, op, pick)
    note({"edit": op, "faces": chosen, "result": str(r)})
    return None


def delete_doomed():
    o = bpy.data.objects.get("Doomed")
    if o is None:
        note({"delete_skipped": "no Doomed"})
        return None
    with ctx():
        for x in bpy.context.view_layer.objects:
            x.select_set(False)
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        r = bpy.ops.object.delete()
    note({"deleted": "Doomed", "result": str(r)})
    return None


def status():
    info = {"objects": sorted(o.name for o in bpy.data.objects),
            "users": [u.username for u in bpy.context.window_manager.online_users]}
    o = bpy.data.objects.get("Shared_box")
    if o is not None and o.mode != "EDIT":
        me = o.data
        a = me.attributes.get("mu_vid")
        if a is not None:
            ids = [d.value for d in a.data]
            verts = sorted((ids[i], [round(c, 3) for c in v.co]) for i, v in enumerate(me.vertices))
            faces = []
            for p in me.polygons:
                f = [ids[i] for i in p.vertices]
                k = f.index(min(f))
                faces.append(f[k:] + f[:k])
            info["id_hash"] = hashlib.md5(json.dumps([verts, sorted(faces)]).encode()).hexdigest()[:10]
        info.update({"verts": len(me.vertices), "faces": len(me.polygons)})
        try:
            from replication.interface import session
            from replication import merge3, topo3
            for uuid in list(session.repository.graph.keys()):
                n = session.repository.graph.get(uuid)
                if n.instance == me and isinstance(n.data, dict) and topo3.TOPO_KEY in n.data:
                    t = n.data[topo3.TOPO_KEY]
                    info["commits"] = len(t["c"])
                    info["entries"] = sum(len(t[s]) for s in ("v", "f", "e"))
                    info["tombstones"] = sum(1 for s in ("v", "f", "e") for e in t[s].values() if not e[0])
                    info["numbers"] = {k: v[0] for k, v in (t.get("n") or {}).items()}
                    v = topo3.view(t)
                    info["conflicts"] = v["conflicts"]
                    info["clashes"] = v["clashes"]
                    info["log"] = {cid: [topo3.name_of(t, c[0]), round(c[1] - T0, 2), len(c) > 3]
                                   for cid, c in t["c"].items()}
                    sig = (len(t["c"]), info["entries"], info["tombstones"])
                    if os.environ.get("DUMP") and sig != getattr(status, "sig", None):
                        status.sig = sig
                        with open(f"/tmp/topo_{NAME}_{info['t'] if 't' in info else round(time.time() - T0, 1)}.json", "w") as f:
                            json.dump(t, f)
            info["notices"] = [text for _, text in merge3.NOTICES]
        except Exception as e:
            info["error"] = repr(e)
    note(info)
    return 5.0


bpy.app.timers.register(start, first_interval=4)
