# Topology merge prototype

Step 1 of [../TOPOLOGY-DESIGN.md](../TOPOLOGY-DESIGN.md): a standalone three-way merge for two users who change the topology of the same mesh at the same time. It is not wired into Multi-User yet.

| File | What it is |
|---|---|
| `topomerge.py` | The merge. Pure Python, no bpy, no numpy |
| `test_topomerge.py` | Unit tests on hand-built cubes and grids |
| `blender_harness.py` | Runs real Blender edit operators on two copies of a sphere, merges them, and checks the result |

## How it works

Every vertex has a permanent id in the `mu_vid` integer attribute. Blender copies an existing id onto each new vertex an edit creates, so `repair_ids` runs after an edit: in a group of equal ids the vertex nearest the base position keeps the id and the others get fresh ones. A fresh id is `client_number << 24 | counter`, so two clients never collide.

`merge(base, a, b, stamp_a, stamp_b)` compares each side with the base by id. A face is keyed by its vertex ids, rotated so the smallest comes first.

- Each side's change splits into units: a removed base face, or a connected group of new faces. A unit's footprint is the base vertices it uses.
- Footprints from both sides are joined into clusters through shared base vertices.
- A cluster only one side changed gets that side's changes. A cluster both sides changed goes wholly to the larger stamp (ties go to b), and the report lists it under `conflicts`.
- New geometry that uses no base vertex is always kept.
- Vertex moves merge per vertex, larger stamp first, as in the v2 patch.

## Running

```sh
python -m unittest -v
blender -b --factory-startup --python blender_harness.py
```

## Results, 2026-10-31

Unit tests: 18 of 18 pass (Python 3.10, Windows).

Harness in Space B, Blender 5.2.2, base UV sphere with 114 vertices:

| Scenario | Edits | Result |
|---|---|---|
| disjoint | A extrudes faces at +X. B subdivides faces at -X and moves one vertex near the pole | PASS. 156 vertices, every edit from both sides present, no conflicts, 0 non-manifold edges |
| overlap | A insets the pole faces and extrudes faces at -X. B subdivides the pole faces, stamped later | PASS. One conflict at the pole, won by B. A's extrude at -X kept. 0 non-manifold edges |
| delete | A deletes faces at +Y. B subdivides faces at -Y | PASS. Hole and subdivision both present. The only 16 non-manifold edges are the hole's rim, matching A's own mesh |

## Known limits

- UVs and other per-corner data, shape keys, vertex groups and edge attributes are not carried yet.
- Stamps are one per side, not per value as in v2.
- Two edits that share even one base vertex count as one cluster, so the older one is dropped there.
- `repair_ids` picks the original by position. If an edit moves the original away and leaves the copy in place, their ids swap. The topology stays right, but a concurrent remote move of that vertex can land on the wrong one.
- A base vertex left loose on purpose, by deleting only its faces, is dropped as an orphan.
- Knife, connect-vertex-path and interactive loop cut have not been run through the harness.
