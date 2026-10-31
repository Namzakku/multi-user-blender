"""Multi-User stress test client. One Blender instance per client.

env: ROLE=host|client  IDX=0..N-1  N=<clients>  T0=<epoch start of edits>
     DUR=<seconds of editing>  SCEN=disjoint|overlap|undo|reconnect  OUT=<dir>
Host must be started with the .blend containing A_head as its file argument.
"""
import os, json, random, time, hashlib, bpy

ROLE, IDX, N = os.environ["ROLE"], int(os.environ["IDX"]), int(os.environ["N"])
T0, DUR, SCEN = float(os.environ["T0"]), float(os.environ["DUR"]), os.environ["SCEN"]
OUT = os.environ["OUT"]
PORT = int(os.environ.get("PORT", 5600))
NAME = f"agent{IDX}"
PKG = "bl_ext.blender_org.multi_user"
OBJ = os.environ.get("OBJ", "A_head")
rng = random.Random(1000 + IDX)

events = []           # what this client did
batches = []          # vertex offset batches, for undo bookkeeping
own_prop = None       # last value this client wrote to its own transform field
state = {"edits": 0, "undos": 0, "errors": 0, "connected": False}


def log(m):
    events.append([round(time.time() - T0, 2), m])


def flush(name, data):
    with open(os.path.join(OUT, f"{name}_{NAME}.json"), "w") as f:
        json.dump(data, f)


def ctx():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return bpy.context.temp_override(window=win, area=area, region=region)


def prefs():
    p = bpy.context.preferences.addons[PKG].preferences
    p.username = NAME
    p.host_port = PORT
    if len(p.server_preset) == 0:
        p.server_preset.add()
    p.server_preset[0].ip, p.server_preset[0].port = "127.0.0.1", PORT
    bpy.context.window_manager.server_index = 0


def start():
    prefs()
    with ctx():
        if ROLE == "host":
            # Multi-User cannot host a mesh in Edit Mode (server init fails), and a
            # .blend saved in Edit Mode opens in it
            if bpy.context.object and bpy.context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            for o in list(bpy.data.objects):
                if o.name != OBJ:
                    bpy.data.objects.remove(o, do_unlink=True)
            r = bpy.ops.wm.session_host()
        else:
            r = bpy.ops.wm.session_join()
    state["connected"] = True
    log(f"start {r}")
    return None


def host_touch():
    o = bpy.data.objects[OBJ]
    o.location.z += 0.0001  # first owner edit so the object is sent
    for x in bpy.context.view_layer.objects:
        x.select_set(False)
    return None


def host_base():
    o = bpy.data.objects[OBJ]
    flush("base", {"co": [list(v.co) for v in o.data.vertices],
                   "loc": list(o.location), "verts": len(o.data.vertices),
                   "faces": len(o.data.polygons)})
    return None


def mine(i):
    return i % N == IDX


def vertex_edit():
    o = bpy.data.objects[OBJ]
    vs = o.data.vertices
    pool = [i for i in range(len(vs)) if mine(i)] if SCEN != "overlap" else list(range(len(vs)))
    picks = rng.sample(pool, 5)
    batch = []
    for i in picks:
        d = [rng.uniform(-0.01, 0.01) for _ in range(3)]
        for k in range(3):
            vs[i].co[k] += d[k]
        batch.append([i, d])
    o.data.update()
    return batch


def prop_edit():
    global own_prop
    o = bpy.data.objects[OBJ]
    axis = IDX % 3 if SCEN != "overlap" else 0
    val = round(rng.uniform(-0.5, 0.5), 4)
    o.location[axis] = val
    own_prop = [axis, val, time.time()]
    log(f"prop loc[{axis}]={val}")


def undo_push(msg):
    try:
        with ctx():
            bpy.ops.ed.undo_push(message=msg)
    except Exception as e:
        log(f"undo_push failed {e!r}")


def do_undo():
    try:
        with ctx():
            r = bpy.ops.ed.undo()
        if batches:
            batches.pop()
        state["undos"] += 1
        log(f"undo {r}")
    except Exception as e:
        state["errors"] += 1
        log(f"undo error {e!r}")


def reconnect_leave():
    with ctx():
        bpy.ops.wm.session_quit()
    state["connected"] = False
    log("left session")
    return None


def reconnect_join():
    prefs()
    with ctx():
        r = bpy.ops.wm.session_join()
    state["connected"] = True
    log(f"rejoined {r}")
    return None


def _faces_in(test):
    """Indices of A_head faces whose center, relative to the mesh center, passes test."""
    import math
    me = bpy.data.objects[OBJ].data
    n = len(me.vertices)
    cx = sum(v.co.x for v in me.vertices) / n
    cy = sum(v.co.y for v in me.vertices) / n
    rmax = max(math.hypot(p.center.x - cx, p.center.y - cy) for p in me.polygons)
    out = []
    for p in me.polygons:
        x, y = p.center.x - cx, p.center.y - cy
        r = math.hypot(x, y)
        if r > 0.6 * rmax and test(math.degrees(math.atan2(y, x)) % 360, p):
            out.append(p.index)
    return out


def _counts():
    import bmesh
    o = bpy.data.objects[OBJ]
    if o.mode == "EDIT":
        bm = bmesh.from_edit_mesh(o.data)
        return len(bm.verts), len(bm.faces)
    return len(o.data.vertices), len(o.data.polygons)


def topo_ops(ops):
    """Run [(face index, op)] in one Edit Mode session with real operators, the
    way a user does it. Each op is "extrude", "subdivide" or "inset". Returns the
    vertex and face count change of each op."""
    o = bpy.data.objects[OBJ]
    for x in bpy.context.view_layer.objects:
        x.select_set(False)
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    deltas = []
    with ctx():
        normals = {fi: o.data.polygons[fi].normal.copy() for fi, _ in ops}
        centers = {fi: o.data.polygons[fi].center.copy() for fi, _ in ops}
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_mode(type="FACE")
        for fi, op in ops:
            import bmesh
            bm = bmesh.from_edit_mesh(o.data)
            for f in bm.faces:
                f.select_set(False)
            # an earlier op renumbers faces, so find the target by its center
            target = min(bm.faces, key=lambda f: (f.calc_center_median() - centers[fi]).length)
            target.select_set(True)
            bmesh.update_edit_mesh(o.data)
            before = _counts()
            if op == "extrude":
                n = normals[fi] * 0.02
                bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": tuple(n)})
            elif op == "subdivide":
                bpy.ops.mesh.subdivide(number_cuts=1)
            else:
                bpy.ops.mesh.inset(thickness=0.004)
            after = _counts()
            deltas.append([op, after[0] - before[0], after[1] - before[1]])
        bpy.ops.object.mode_set(mode="OBJECT")
    return deltas


TOPO_OP = ["extrude", "subdivide", "inset"]


def topo_tick():
    """One edit of this agent's own sector, 120 degrees each with 30 degree gaps."""
    lo = IDX * 120 + 15
    pool = _faces_in(lambda a, p: lo <= a < lo + 90)
    if not pool:
        log("topo no faces in sector")
        return
    fi = rng.choice(pool)
    d = topo_ops([(fi, TOPO_OP[IDX % 3])])
    topo_log.append(d[0])
    state["edits"] += 1
    log(f"topo {d[0]}")


topo_log = []


def schedule_topoclash():
    """agent0 and agent1 edit the same +X face 0.3 s apart without seeing each
    other. In the same Edit Mode session agent0 also extrudes a far -X face, which
    must survive. agent2 edits a -Y face, which must survive too."""
    def run():
        side = lambda a: (a < 10 or a > 350)
        same = _faces_in(lambda a, p: side(a))
        same = max(same, key=lambda i: bpy.data.objects[OBJ].data.polygons[i].center.x)
        if IDX == 0:
            far = _faces_in(lambda a, p: 170 < a < 190)[0]
            d = topo_ops([(same, "extrude"), (far, "extrude")])
        elif IDX == 1:
            d = topo_ops([(same, "subdivide")])
        else:
            d = topo_ops([(_faces_in(lambda a, p: 260 < a < 280)[0], "inset")])
        topo_log.extend(d)
        state["edits"] += 1
        log(f"topoclash {d}")
        return None
    at = {0: 5.0, 1: 5.3, 2: 5.1}[IDX]
    bpy.app.timers.register(run, first_interval=max(0.0, T0 + at - time.time()))


REGION_BOXES = {
    # normalized bounding-box ranges (x, y, z); front is -Y, the half mesh is x >= 0
    "ear": ((0.7, 1.0), (0.35, 0.8), (0.2, 0.8)),
    "eye": ((0.15, 0.75), (0.0, 0.3), (0.25, 0.6)),
}
REGIONS = os.environ.get("REGIONS", "ear,eye").split(",")
_bbox = {}


def in_region(co):
    box = REGION_BOXES[REGIONS[IDX % len(REGIONS)]]
    lo, hi = _bbox["lo"], _bbox["hi"]
    for k in range(3):
        n = (co[k] - lo[k]) / max(hi[k] - lo[k], 1e-9)
        if not box[k][0] <= n <= box[k][1]:
            return False
    return True


def _ring(bm_edge):
    """Faces a loop cut through this edge would cross: walk the quad ring both ways."""
    faces, open_ends = [], 0
    for start in bm_edge.link_loops:
        loop, seen = start, 0
        while True:
            f = loop.face
            if len(f.verts) != 4 or f in faces:
                if len(f.verts) != 4:
                    open_ends += 1
                break
            faces.append(f)
            nxt = loop.link_loop_next.link_loop_next.link_loop_radial_next
            if nxt == loop.link_loop_next.link_loop_next:  # boundary edge
                open_ends += 1
                break
            loop = nxt
            seen += 1
            if seen > 500:
                break
    return faces


def _edit_session(op):
    """Enter Edit Mode now, do the op 4 s later, leave Edit Mode, so sessions of
    the two clients overlap the way two people's do."""
    def enter():
        o = bpy.data.objects[OBJ]
        for x in bpy.context.view_layer.objects:
            x.select_set(False)
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        with ctx():
            bpy.ops.object.mode_set(mode="EDIT")
        bpy.app.timers.register(do, first_interval=4.0)
        return None

    def do():
        import bmesh
        o = bpy.data.objects[OBJ]
        before = _counts()
        try:
            with ctx():
                bm = bmesh.from_edit_mesh(o.data)
                bpy.ops.mesh.select_all(action="DESELECT")
                note = op(o, bm)
            after = _counts()
            d = [op.__name__, after[0] - before[0], after[1] - before[1], note]
        except Exception as e:
            state["errors"] += 1
            d = [op.__name__, 0, 0, f"error {e!r}"]
        with ctx():
            bpy.ops.object.mode_set(mode="OBJECT")
        topo_log.append(d)
        state["edits"] += 1
        log(f"ops {d}")
        return None
    return enter


def loopcut(o, bm):
    import bmesh
    bm.edges.ensure_lookup_table()
    for e in bm.edges:
        faces = _ring(e)
        if len(faces) >= 2 and all(in_region(v.co) for f in faces for v in f.verts):
            index = e.index
            with ctx():
                r = bpy.ops.mesh.loopcut_slide(
                    MESH_OT_loopcut={"number_cuts": 1, "edge_index": index, "object_index": 0},
                    TRANSFORM_OT_edge_slide={"value": 0.0})
            return f"edge {index} ring {len(faces)} {r}"
    return "no ring inside region"


def connect(o, bm):
    import bmesh
    for f in bm.faces:
        if len(f.verts) == 4 and all(in_region(v.co) for v in f.verts) \
                and all(abs(v.co.x) > 1e-4 for v in f.verts):
            a, b = f.verts[0], f.verts[2]
            a.select_set(True)
            b.select_set(True)
            bm.select_history.clear()
            bm.select_history.add(a)
            bm.select_history.add(b)
            bmesh.update_edit_mesh(o.data)
            with ctx():
                r = bpy.ops.mesh.vert_connect_path()
            return f"face {f.index} {r}"
    return "no quad inside region"


def merge(o, bm):
    import bmesh
    for e in bm.edges:
        a, b = e.verts
        if in_region(a.co) and in_region(b.co) and abs(a.co.x) > 1e-4 and abs(b.co.x) > 1e-4 \
                and all(in_region(v.co) for f in e.link_faces for v in f.verts):
            a.select_set(True)
            b.select_set(True)
            index = e.index  # the merge removes this edge
            bmesh.update_edit_mesh(o.data)
            with ctx():
                r = bpy.ops.mesh.merge(type="CENTER")
            return f"edge {index} {r}"
    return "no edge inside region"


def schedule_ops():
    def setup():
        o = bpy.data.objects.get(OBJ)
        if o is None or not state["connected"]:
            return 1.0
        co = [v.co for v in o.data.vertices]
        _bbox["lo"] = [min(c[k] for c in co) for k in range(3)]
        _bbox["hi"] = [max(c[k] for c in co) for k in range(3)]
        log(f"ops region {REGIONS[IDX % len(REGIONS)]} bbox {_bbox}")
        return None
    bpy.app.timers.register(setup, first_interval=max(0.5, T0 - 5 - time.time()))
    offset = 3.0 * IDX
    for k, op in enumerate((loopcut, connect, merge)):
        bpy.app.timers.register(_edit_session(op),
                                first_interval=max(1.0, T0 + offset + 12 * k - time.time()))


def tick():
    now = time.time()
    if now >= T0 + DUR:
        log("editing done")
        return None
    if not state["connected"] or bpy.data.objects.get(OBJ) is None:
        return 0.5
    if SCEN in ("conflict", "stale", "topoclash", "ops"):
        return None   # timed writes are scheduled separately
    if SCEN == "topo":
        try:
            if rng.random() < 0.25:
                prop_edit()
            else:
                topo_tick()
        except Exception as e:
            state["errors"] += 1
            log(f"tick error {e!r}")
        return rng.uniform(3.0, 6.0)
    if SCEN == "single":
        if IDX != 0:
            return 1.0
        prop_edit()
        state["edits"] += 1
        return 4.0
    try:
        roll = rng.random()
        if SCEN == "undo" and IDX == 1 and roll < 0.25 and batches:
            do_undo()
        elif roll < 0.15:
            prop_edit()
        else:
            b = vertex_edit()
            batches.append(b)
            if SCEN == "undo" and IDX == 1:
                undo_push(f"edit {len(batches)}")
            state["edits"] += 1
    except Exception as e:
        state["errors"] += 1
        log(f"tick error {e!r}")
    return rng.uniform(0.25, 1.0)


def node_report():
    try:
        from replication.interface import session
        o = bpy.data.objects.get(OBJ)
        rows = []
        for uuid in list(session.repository.graph.keys()):
            n = session.repository.graph.get(uuid)
            inst = n.instance
            if isinstance(inst, bpy.types.Mesh) or isinstance(inst, bpy.types.Object):
                rows.append(f"{uuid[:8]} {type(inst).__name__} {inst.name!r} users={inst.users} "
                            f"is_obj_data={o is not None and inst == o.data} is_obj={inst == o} state={n.state}")
        rows.append(f"obj.data={o.data.name!r} meshes={[m.name for m in bpy.data.meshes]}")
        for r in rows:
            log("node " + r)
    except Exception as e:
        log(f"node report failed {e!r}")


def final():
    node_report()
    o = bpy.data.objects.get(OBJ)
    if o is None:
        flush("final", {"missing": True, "events": events, "state": state})
        return None
    co = [[round(c, 5) for c in v.co] for v in o.data.vertices]
    flush("final", {
        "co": co, "loc": [round(v, 5) for v in o.location],
        "hash": hashlib.md5(json.dumps(co).encode()).hexdigest()[:12],
        "batches": batches, "own_prop": own_prop, "state": state,
        "users": [u.username for u in bpy.context.window_manager.online_users],
        "events": events[-40:], "stamps": mesh_stamps(), "topo": topo_report(o)})
    return None


def topo_report(o):
    """Order-independent view of the mesh by permanent id, and this agent's topology edits."""
    me = o.data
    a = me.attributes.get("mu_vid")
    out = {"verts": len(me.vertices), "faces": len(me.polygons), "edits": topo_log,
           "uv_layers": [l.name for l in me.uv_layers]}
    if a is None:
        out["ids"] = "missing"
        return out
    ids = [d.value for d in a.data]
    out["unique_ids"] = len(set(ids)) == len(ids)
    verts = sorted((ids[i], [round(c, 4) for c in v.co]) for i, v in enumerate(me.vertices))
    faces = []
    for p in me.polygons:
        f = [ids[i] for i in p.vertices]
        k = f.index(min(f))
        faces.append(f[k:] + f[:k])
    faces.sort()
    out["id_hash"] = hashlib.md5(json.dumps([verts, faces]).encode()).hexdigest()[:12]
    uv = me.uv_layers.active
    if uv:
        corner = sorted((tuple(faces_key(ids, p)), ids[me.loops[li].vertex_index],
                         [round(c, 4) for c in uv.data[li].uv])
                        for p in me.polygons for li in p.loop_indices)
        out["uv_hash"] = hashlib.md5(json.dumps(corner).encode()).hexdigest()[:12]
    try:
        from replication.interface import session
        from replication import topo3
        for uuid in list(session.repository.graph.keys()):
            n = session.repository.graph.get(uuid)
            if n.instance == me and isinstance(n.data, dict) and topo3.TOPO_KEY in n.data:
                v = topo3.view(n.data[topo3.TOPO_KEY])
                out["view_conflicts"] = v["conflicts"]
                out["commits"] = len(n.data[topo3.TOPO_KEY]["c"])
    except Exception as e:
        out["view_error"] = repr(e)
    return out


def faces_key(ids, p):
    f = [ids[i] for i in p.vertices]
    k = f.index(min(f))
    return f[k:] + f[:k]


def mesh_stamps():
    """Per-vertex x-word stamps held in node.data and in the merge base, plus node state."""
    try:
        import numpy as np
        from replication.interface import session
        from replication import merge3 as M
        o = bpy.data.objects.get(OBJ)
        for uuid in list(session.repository.graph.keys()):
            n = session.repository.graph.get(uuid)
            if n.instance == o.data:
                out = {"state": n.state}
                for tag, d in (("data", n.data), ("base", M._base.get(uuid))):
                    ts = M.stamps_of(d) if isinstance(d, dict) else None
                    if ts:
                        out[tag] = np.frombuffer(ts["vertices"]["co"], dtype=np.float64)[0::3].round(3).tolist()
                        out[tag + "_x"] = np.frombuffer(M.strip(d)["vertices"]["co"], dtype=np.float32)[0::3].round(5).tolist()
                return out
    except Exception as e:
        return {"error": repr(e)}
    return {}


def conflict_write(val):
    def run():
        global own_prop
        o = bpy.data.objects.get(OBJ)
        o.location[0] = val
        own_prop = [0, val, time.time()]
        state["edits"] += 1
        log(f"conflict write loc[0]={val} at {time.time():.3f}")
        return None
    return run


def conflict_sample(tag):
    def run():
        o = bpy.data.objects.get(OBJ)
        log(f"conflict sample {tag} loc[0]={round(o.location[0], 4)}")
        return None
    return run


def schedule_conflict():
    plan = {1: [(5.0, 1.111), (15.05, 4.444)], 2: [(5.2, 2.222), (15.0, 3.333)]}
    for at, val in plan.get(IDX, []):
        bpy.app.timers.register(conflict_write(val), first_interval=max(0.0, T0 + at - time.time()))
    bpy.app.timers.register(conflict_sample("mid"), first_interval=max(0.0, T0 + 12 - time.time()))


def schedule_stale():
    """Mimic Blender's move tool: agent0 snapshots the location, other agents
    change Y and Z meanwhile, then agent0 writes snapshot + 0.5 on X (the tool
    writes all three components from its starting state)."""
    snap = {}

    def take():
        snap["loc"] = tuple(bpy.data.objects[OBJ].location)
        log(f"stale snapshot {[round(v, 4) for v in snap['loc']]}")

    def write():
        o = bpy.data.objects[OBJ]
        x, y, z = snap["loc"]
        o.location = (x + 0.5, y, z)
        state["edits"] += 1
        log(f"stale write {[round(v, 4) for v in o.location]}")

    def move_y():
        bpy.data.objects[OBJ].location[1] += 0.5
        state["edits"] += 1
        log(f"stale move y -> {round(bpy.data.objects[OBJ].location[1], 4)}")

    def move_z():
        bpy.data.objects[OBJ].location[2] += 0.5
        state["edits"] += 1
        log(f"stale move z -> {round(bpy.data.objects[OBJ].location[2], 4)}")

    def at(t, fn):
        bpy.app.timers.register(lambda: (fn(), None)[1], first_interval=max(0.0, T0 + t - time.time()))

    if IDX == 0:
        at(3, take)
        at(9, write)
    elif IDX == 1:
        at(5, move_y)
    elif IDX == 2:
        at(6, move_z)


if SCEN == "conflict":
    schedule_conflict()
if SCEN == "stale":
    schedule_stale()
if SCEN == "topoclash":
    schedule_topoclash()
if SCEN == "ops":
    schedule_ops()

join_at = 3 if ROLE == "host" else 10 + 2 * IDX
bpy.app.timers.register(start, first_interval=join_at)
if ROLE == "host":
    bpy.app.timers.register(host_touch, first_interval=join_at + 4)
    bpy.app.timers.register(host_base, first_interval=join_at + 8)
first_edit = max(1.0, T0 - time.time())
bpy.app.timers.register(tick, first_interval=first_edit)
if SCEN == "reconnect" and IDX == 1:
    bpy.app.timers.register(reconnect_leave, first_interval=first_edit + DUR * 0.4)
    bpy.app.timers.register(reconnect_join, first_interval=first_edit + DUR * 0.4 + 6)
bpy.app.timers.register(final, first_interval=first_edit + DUR + int(os.environ.get("SETTLE", "45")))
