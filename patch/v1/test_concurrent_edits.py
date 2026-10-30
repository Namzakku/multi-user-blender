"""Multi-User ownership test 2. ROLE=host|client NAME OUT MODE=select|common."""
import os, json, bpy

ROLE, NAME, OUT = os.environ["ROLE"], os.environ["NAME"], os.environ["OUT"]
MODE = os.environ.get("MODE", "select")
PKG = "bl_ext.blender_org.multi_user"
log = []
HOST = ROLE == "host"


def note(m):
    log.append(m)
    with open(OUT, "w") as f:
        json.dump(log, f, indent=1)


def ctx():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    return bpy.context.temp_override(window=win, area=area)


def owner(o):
    from replication.interface import session
    n = session.repository.graph.get(getattr(o, "uuid", ""))
    return n.owner if n else None


def state():
    out = {}
    for n in ("A_obj", "B_obj"):
        o = bpy.data.objects.get(n)
        if o:
            xs = [v.co.x for v in o.data.vertices]
            zs = [v.co.z for v in o.data.vertices]
            out[n] = [[round(v, 3) for v in o.location], round(o.scale.x, 3),
                      round(max(xs) - min(xs), 3), round(min(zs), 3), owner(o)]
    return out


def step(label, fn, who):
    def run():
        if who == "both" or (who == "host") == HOST:
            try:
                note({label: fn() or "ok", "state": state()})
            except Exception as e:
                note({label: f"error {e!r}", "state": state()})
        else:
            note({label: "(other agent)", "state": state()})
        return None
    return run


def start():
    p = bpy.context.preferences.addons[PKG].preferences
    p.username = NAME
    p.host_port = 5555
    if len(p.server_preset) == 0:
        p.server_preset.add()
    p.server_preset[0].ip, p.server_preset[0].port = "127.0.0.1", 5555
    bpy.context.window_manager.server_index = 0
    with ctx():
        return str(bpy.ops.wm.session_host() if HOST else bpy.ops.wm.session_join())


def mine():
    return bpy.data.objects["A_obj" if HOST else "B_obj"]


def add_obj():
    with ctx():
        bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0 if HOST else 2, 0, 0))
    bpy.context.active_object.name = "A_obj" if HOST else "B_obj"


def touch():
    mine().location.z += 0.01


def deselect():
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    return "deselected all"


def select_a_obj():
    o = bpy.data.objects["A_obj"]
    for x in bpy.context.view_layer.objects:
        x.select_set(False)
    o.hide_select = False
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    return f"selected A_obj (selectable before: {not o.hide_select})"


def move_x():
    bpy.data.objects["A_obj"].location.x += 1.0
    return "A_obj x+1"


def move_y():
    bpy.data.objects["A_obj"].location.y += 1.0
    return "A_obj y+1"


def diff_props():
    o = bpy.data.objects["A_obj"]
    if HOST:
        o.location.z = 2.0
        return "A set loc.z=2"
    o.scale.x = 3.0
    return "B set scale.x=3"


def same_prop():
    o = bpy.data.objects["A_obj"]
    o.location.z = 5.0 if HOST else 7.0
    return f"{NAME} set loc.z={o.location.z}"


def mesh_edit():
    o = bpy.data.objects["A_obj"]
    for v in o.data.vertices:
        if HOST:
            v.co.x *= 2.0
        else:
            v.co.z -= 0.5
    o.data.update()
    return "A doubled mesh width" if HOST else "B moved mesh down 0.5"


def topo_edit():
    o = bpy.data.objects["A_obj"]
    if HOST:
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
        bm.to_mesh(o.data)
        bm.free()
        o.data.update()
        return f"A subdivided mesh -> {len(o.data.vertices)} verts"
    for v in o.data.vertices:
        v.co.z -= 0.5
    o.data.update()
    return "B moved mesh down 0.5"


def mesh_info():
    o = bpy.data.objects["A_obj"]
    m = o.data
    ok = all(i < len(m.vertices) for p in m.polygons for i in p.vertices)
    return {"verts": len(m.vertices), "edges": len(m.edges), "faces": len(m.polygons),
            "faces_valid": ok, "validate_errors": m.validate(verbose=False)}


def report():
    return "report"


common = [("start", start, "both", 3, 12), ("add", add_obj, "both", 17, 24),
          ("touch", touch, "both", 21, 27), ("deselect_all", deselect, "both", 31, 31)]
if MODE == "select":
    plan = common + [
        ("B_selects_A_obj", select_a_obj, "client", 0, 36), ("B_moves_x", move_x, "client", 0, 41),
        ("r1", report, "both", 49, 49),
        ("B_releases", deselect, "client", 0, 52), ("A_selects_A_obj", select_a_obj, "host", 56, 0),
        ("A_moves_y", move_y, "host", 61, 0), ("r2", report, "both", 70, 70)]
elif MODE == "topo":
    plan = common + [
        ("topo_edit", topo_edit, "both", 38, 38), ("r1", mesh_info, "both", 52, 52)]
else:
    plan = common + [
        ("diff_props", diff_props, "both", 38, 38), ("r1", report, "both", 48, 48),
        ("same_prop", same_prop, "both", 52, 52), ("r2", report, "both", 62, 62),
        ("mesh_edit", mesh_edit, "both", 66, 66), ("r3", report, "both", 78, 78)]
for label, fn, who, th, tc in plan:
    t = th if HOST else tc
    if t:
        bpy.app.timers.register(step(label, fn, who), first_interval=t)
    else:
        bpy.app.timers.register(step(label, fn, "nobody"), first_interval=(tc or th))
