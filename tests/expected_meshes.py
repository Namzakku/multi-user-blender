"""Print the meshes a live clash may correctly end with, for live_trials.sh.

    blender -b --python expected_meshes.py -- <host op> <client op>

Builds the test box, applies each edit alone and both in each order, and prints
one line: ALLOWED followed by "vertices/faces" for each, space separated.
"""
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mesh_ops  # noqa: E402

a, b = sys.argv[sys.argv.index("--") + 1:][:2]
out = []
for ops in ([a], [b], [a, b], [b, a]):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    box = mesh_ops.make_box()
    for op in ops:
        mesh_ops.edit(box, op)
    out.append(f"{len(box.data.vertices)}/{len(box.data.polygons)}")
print("ALLOWED " + " ".join(out))
