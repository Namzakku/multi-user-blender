"""Checks of the installed add-on's Blender side, in background Blender.

    blender -b --python tests/blender_units.py

Needs the patched Multi-User installed and enabled for this user. Do not pass
--factory-startup: the add-on's preferences are then missing. Prints one line
per check, PASS or FAIL, then "BLENDER_UNITS <passed>/<total>".
"""
import importlib
import os
import tempfile
import traceback
from pathlib import Path

import bpy
import numpy as np

PKG = next((a for a in bpy.context.preferences.addons.keys() if a.endswith(".multi_user")),
           "bl_ext.user_default.multi_user")
bl_mesh = importlib.import_module(f"{PKG}.bl_types.bl_mesh")
bl_file = importlib.import_module(f"{PKG}.bl_types.bl_file")
bl_image = importlib.import_module(f"{PKG}.bl_types.bl_image")
BlMesh = bl_mesh.BlMesh
results = []


def check(name):
    def wrap(fn):
        try:
            fn()
            results.append((name, True, ""))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))
            traceback.print_exc()
        return fn
    return wrap


def fresh_grid(name="Grid"):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4, size=2)
    o = bpy.context.active_object
    o.name = name
    a = o.data.attributes.new("mu_vid", "INT", "POINT")
    a.data.foreach_set("value", np.arange(1, len(o.data.vertices) + 1, dtype=np.int32))
    return o


def shape(me):
    co = np.zeros(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    return len(me.vertices), len(me.edges), len(me.polygons), co.round(5).tobytes(), \
        [tuple(p.vertices) for p in me.polygons]


@check("a mesh update whose face count does not match its buffers is refused and the mesh is kept")
def _():
    o = fresh_grid()
    before = shape(o.data)
    data = BlMesh.dump(o.data)
    data["poly_count"] += 4            # what the stale-restore check produced on 2026-11-04
    try:
        BlMesh.load(data, o.data)
    except Exception as e:
        print("load raised", type(e).__name__, e)
    assert shape(o.data) == before, "the mesh changed"
    assert not o.data.validate(), "the mesh needed repairs"


@check("a load that fails halfway puts the mesh back as it was")
def _():
    o = fresh_grid()
    before = shape(o.data)
    data = BlMesh.dump(o.data)
    data["attributes"] = [("broken", "NOT_A_TYPE", "POINT", {"value": b""})] + list(data["attributes"])
    try:
        BlMesh.load(data, o.data)
    except Exception as e:
        print("load raised", type(e).__name__, e)
    assert shape(o.data) == before, "the mesh changed"
    ids = np.zeros(len(o.data.vertices), dtype=np.int32)
    o.data.attributes["mu_vid"].data.foreach_get("value", ids)
    assert ids.tolist() == list(range(1, len(ids) + 1)), "ids were lost"


@check("the Skin layer and its radii survive loading the mesh's own dump")
def _():
    o = fresh_grid()
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.modifier_add(type="SKIN")
    o.data.skin_vertices[0].data[3].radius = (0.7, 0.9)
    data = BlMesh.dump(o.data)
    BlMesh.load(data, o.data)
    assert len(o.data.skin_vertices) == 1, "the skin layer is gone"
    r = tuple(round(x, 4) for x in o.data.skin_vertices[0].data[3].radius)
    assert r == (0.7, 0.9), f"radius {r}"


@check("skin radii survive reloading the object, which remakes its modifier stack")
def _():
    o = fresh_grid()
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.modifier_add(type="SKIN")
    o.data.skin_vertices[0].data[3].radius = (0.7, 0.9)
    BlObject = importlib.import_module(f"{PKG}.bl_types.bl_object").BlObject
    data = BlObject.dump(o)
    data.pop("skin_vertices", None)    # the radii must come from the mesh, not the object dump
    BlObject.load(data, o)
    assert len(o.data.skin_vertices) == 1, "the skin layer is gone"
    r = tuple(round(x, 4) for x in o.data.skin_vertices[0].data[3].radius)
    assert r == (0.7, 0.9), f"radius {r}"


@check("a file that does not exist is dumped without content and never raises")
def _():
    missing = Path(tempfile.gettempdir()) / "mu_missing_dir" / "character_front.png"
    assert not missing.exists()
    data = bl_file.BlFile.dump(missing)
    assert "file" not in data
    bl_file.BlFile.needs_update(missing, {"name": missing.name})
    bl_file.BlFile.load({"name": missing.name}, Path(tempfile.gettempdir()) / "mu_never_written.png")


@check("an image whose file is missing does not depend on that file")
def _():
    img = bpy.data.images.new("ref", 4, 4)
    img.source = "FILE"
    img.filepath = "//../.config/blender/5.2/extensions/.user/user_default/multi_user/cache/character_front.png"
    deps = bl_image.BlImage.resolve_deps(img)
    assert not [d for d in deps if isinstance(d, Path)], f"depends on {deps}"


for name, ok, why in results:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {why}" if why else ""))
print(f"BLENDER_UNITS {sum(ok for _, ok, _ in results)}/{len(results)}")
