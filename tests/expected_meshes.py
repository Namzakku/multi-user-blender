"""Print the meshes a live trial may correctly end with, for live_trials.sh.

    blender -b --python expected_meshes.py -- <host op> <client op>

Ops are mesh_ops names, "<op>" or "<op>@<region>", or "none". Builds the test
box, applies each edit alone and both in each order, and prints two lines:
  ALLOWED  "vertices/faces" for: host alone, client alone, both in each order
  GEO      the id-free geometry hash (mesh_ops.geo_hash) of the same four
A clash (one region) may end with any of the four. Edits on separate regions
must end with both kept, and then both orders give the same GEO.
"""
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mesh_ops  # noqa: E402

a, b = sys.argv[sys.argv.index("--") + 1:][:2]
counts, geos = [], []
for ops in ([a], [b], [a, b], [b, a]):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    box = mesh_ops.make_box()
    for op in ops:
        if op != "none":
            mesh_ops.edit(box, op)
    counts.append(f"{len(box.data.vertices)}/{len(box.data.polygons)}")
    geos.append(mesh_ops.geo_hash(box.data))
print("ALLOWED " + " ".join(counts))
print("GEO " + " ".join(geos))
