# Design: merging concurrent topology edits on one mesh

Status: step 1 prototyped in [topology/](topology/README.md), and steps 2 and 3 (scripted) done in `patch/v3/` on 2026-10-31. The wired-in version replaced the three-way merge with an order-independent table, because the server never knows the common base; see the v3 section of [README.md](README.md). Written 2026-10-31.

## Problem

The v2 patch merges vertex moves by index. When two users change the vertex count of the same mesh at the same time, it keeps one whole mesh and drops the other user's edits without warning. Example: one agent rebuilds the ear with Knife cuts while another adds loops around the eye socket of the same head mesh.

## Existing tools

None found that merges concurrent topology edits on one mesh. Blend Buddies syncs Edit Mode geometry but locks an object while someone edits it. Multi-User and Mixer send whole meshes. cSculpt merges concurrent sculpting, which changes vertex properties, not connectivity. Searched 2026-10-31.

## Idea

Give every vertex a permanent ID, stored as an integer attribute `mu_vid` on the point domain. Merge meshes by ID instead of by index. A face is identified by the cyclic list of its vertex IDs. Each user's edit becomes a set of changes against the shared base. Edits merge when they touch different regions, and the newer one wins only inside a region both users changed.

## Measured: what Blender does with the ID attribute

Probe run in Space B, Blender 5.2.2 background, on a cube with IDs 100 to 107 (`patch/v2/id_probe.py`):

| Operation | Verts after | Result for `mu_vid` |
|---|---|---|
| Extrude top face | 12 | New verts copy the IDs of their source verts (4 duplicates) |
| Subdivide | 26 | New verts copy a neighbour's ID (7 IDs duplicated) |
| Edge-ring subdivide (what loop cut uses) | 12 | 4 duplicates |
| Inset, bevel, bisect | 12 | Duplicates, no zeros |
| Merge at center, delete vertex | 7 | IDs stay unique, removed IDs disappear |
| Dissolve vertex | 25 | Same as subdivide minus one |

New vertices never get 0. They always inherit an existing ID. Deleting and merging behave cleanly. So a new vertex shows up as a duplicated ID, and the sync code must give it a fresh one. Knife, connect path and interactive loop cut were not probed: Knife needs a viewport, and the connect probe hit a script error.

## Algorithm

1. **IDs.** When a mesh first joins the session, number its vertices. A fresh ID is `client_number << 24 | counter`, so two clients never hand out the same ID.
2. **Repair after each local edit.** At commit time (the user leaves Edit Mode), find IDs used more than once. In each group, keep the ID on the vertex closest to that ID's base position and give the others fresh IDs. Write the fresh IDs back into the local mesh so they stay stable.
3. **Diff against base.** Each side's change is a set of added vertices, removed vertex IDs, moved vertices, added faces and removed faces, all keyed by ID.
4. **Regions.** A side's touched region is the set of base faces it removed or changed, plus their vertices. Two edits conflict only where those regions overlap.
5. **Merge.** Outside overlaps, apply both sides: union of added faces and vertices, remove what either side removed, and keep last-writer-wins per value for positions, as v2 does now. Inside an overlapping cluster of faces, keep the newer side's faces for that cluster only, and log the dropped side so it can be shown to the user.
6. **Rebuild.** Build the mesh from the merged vertex and face sets, carrying per-vertex and per-face attributes, vertex groups and UVs by ID.

For the eye and ear case, the two edits touch separate faces, so both are kept.

## Open problems

- Loop data (UVs, per-corner normals) must be carried by face key and corner. Not designed in detail.
- Shape keys are stored by vertex index and need remapping by ID.
- A loop cut that runs all the way around the head crosses other users' regions and will conflict with them.
- Repair in step 2 picks the original by position. An edit that moves the original and leaves the copy at the old place gets the IDs swapped. That keeps the topology right but can misplace a concurrent remote move of that vertex.
- Edits sync only on leaving Edit Mode. A long Edit Mode session means a large diff and more overlap.
- Bandwidth is still a full mesh per update.

## Plan

1. Prototype the merge as a pure Python module that takes base, side A and side B meshes as lists and returns the merged mesh. Unit-test it with scripted extrudes, cuts and deletes in background Blender.
2. Wire it into `merge3.py` in place of the whole-mesh rule when vertex counts differ.
3. Rerun the stress scenarios, then a two-agent GUI run: one agent cuts the ear while the other adds loops around the eye of one test head.
