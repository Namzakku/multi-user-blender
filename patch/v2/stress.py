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
OBJ = "A_head"
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
    return bpy.context.temp_override(window=win, area=area)


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
                   "loc": list(o.location)})
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


def tick():
    now = time.time()
    if now >= T0 + DUR:
        log("editing done")
        return None
    if not state["connected"] or bpy.data.objects.get(OBJ) is None:
        return 0.5
    if SCEN in ("conflict", "stale"):
        return None   # timed writes are scheduled separately
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
        "events": events[-40:], "stamps": mesh_stamps()})
    return None


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
