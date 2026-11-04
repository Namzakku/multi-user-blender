# Multi-User concurrent editing (experimental)

A patched build of the Blender Multi-User extension 0.8.2 (replication 0.10.3, Blender 5.2.2) that lets several users edit the same object, and the same mesh, at the same time. Values merge last-writer-wins per value, topology merges by permanent vertex id, and a dropped edit is reported to everyone.

## Install

- In a cua Space: follow [SETUP.md](SETUP.md), which runs `setup.sh`.
- Anywhere else: `python3 build.py` packs `addon/` into `dist/multi_user-0.8.2+mu5.zip`. Install it with Edit > Preferences > Get Extensions > Install from Disk, or `blender -c extension install-file -r user_default -e <zip>`. Remove the upstream Multi-User first, since both use the id `multi_user`.
- Upgrading from mu4: mu5 sends skin data with the mesh and keeps face flags in the topology table, so every Blender in a session, and the server the host runs, must run the same version. Save the host's file in Object Mode, stop the session, install the zip in every Blender, restart Blender (an enabled extension keeps its old modules loaded) and host the saved file again.

## Layout

| Path | What it is |
|---|---|
| `addon/multi_user/`, `addon/replication/` | The add-on and the library it bundles, with every change applied. In the history, one commit adds upstream unmodified and the next applies the v2 and v3 patches, so its diff is the change against upstream |
| `build.py` | Packs `addon/` into an installable zip. replication goes in as a `0.10.3+mu5` wheel so Blender does not keep an upstream copy |
| `setup.sh`, `SETUP.md` | Build a test Space from scratch |
| `tests/` | Offline simulation, race fuzzer and live two-Space tests (see "Tests") |
| `patch/v1`, `v2`, `v3` | Earlier versions as patches against upstream, with the stress harness in `patch/v3/` |
| `topology/`, `TOPOLOGY-DESIGN.md` | The standalone prototype and design of the topology merge |

## How it works

**Values.** Every node's data carries a stamp tree under `_mu_ts`: one time per scalar, per list element and per 4-byte word of a buffer such as vertex coordinates. A commit stamps only what changed and sends the whole node. The server and every client merge value by value, newest stamp first, so all reach the same data in any order. An emptied list or buffer gets one stamp of its own, the time it became empty, so applying the last modifier wins over an older copy that still has it. Stamps use `time.time()`, so machines need synced clocks.

**Stale restores.** Blender's move tool and undo write back a state captured earlier, which can overwrite another user's newer value. Each client keeps the values incoming updates replaced (the last 64 per node). A commit that sets a value back to one of them keeps the synced value instead. Undoing your own edit still works. In a mesh only vertex positions are checked, matched by vertex id after id repair. Counts, faces, edges, ids, UVs and skin data are never put back: after a topology change the same index is another element.

**Topology.** Each vertex has a permanent id in the integer attribute `mu_vid`. Each mesh node carries a table of every vertex, face and loose edge that ever existed, with an alive flag, a stamp and the commit that last changed it. Tables merge entry by entry, newest first. Each commit records which commits it had seen. Two commits that had not seen each other and touch the same base vertices conflict: the older one is hidden in that region, and the next commit from any client (a settle commit) makes that permanent. After a topology change the mesh is rebuilt in id order, with positions, UVs, Skin modifier data, seams and sharp edges carried by id.

**Face flags.** A change of smooth shading or material index on an existing face is a table entry of its own (section `p`) with its own stamp. It is an edit only where the flag differs from what that Blender last loaded, it never brings back a deleted face, and every merge takes the flags from the table, also when it keeps one side's face buffers whole.

**Whole loads.** A mesh update whose counts do not match its buffers is never loaded. merge3 rebuilds it from the topology table, or keeps what Blender holds. `bl_mesh.load` checks again and refuses such an update before touching the mesh, and a load that fails partway puts the mesh back as it was.

**Id repair.** Blender copies an existing id onto each vertex an edit creates, and its inset and poke pass the attribute through float32, so ids above 2**24 lose their low bits even on vertices the edit did not touch. Before each diff, `repair_ids` makes ids unique again and gives the same answer every time it runs on the same mesh: a vertex keeps or recovers its id by position, then by float32 rounding, and only then gets a fresh id (`client number << 23 | counter`).

**Client numbers.** Each table registers client numbers by username. A user takes the number its name hashes to, or the next free one, so two names with the same hash never share fresh ids.

**Conflict notices.** When an edit is dropped, every Blender shows a popup and a red box at the top of the Multi-User panel, naming the object, both users and the number of dropped elements. Settle commits record what they settled, so a Blender that never saw the conflict open still reports it.

**Deleting objects.** The deleting Blender marks the delete as real (`client:deleted`), and the others remove their copy too. An object in Edit Mode is removed when its user leaves Edit Mode. "Stop tracking" removes nothing. An object removed by a script (`bpy.data.objects.remove`) tags no scene update, so the apply timer also looks for object nodes whose object is gone and deletes them the same way.

**Missing files.** An image whose file is missing (for example a path into another machine's Multi-User cache) syncs without its pixels. Before mu5 reading the file raised `FileNotFoundError` and ended the session.

**Pruning.** A commit every active client has seen, at least 120 s older than the newest commit, is dropped from the table with the tombstones it left, unless that would change the mesh. The decision uses only the table, so every replica prunes alike.

## Tests

| Test | Result |
|---|---|
| `python tests/test_sim.py`: offline commits, server merges in several orders, client applies, using the real merge code. `MU_SRC=<folder>` runs it on other merge3.py and topo3.py | 27 of 27. On the mu4 code 22 of 27: the five mu5 tests fail (a reverted face count fails the load, faces on the wrong vertices after dissolve then merge, skin radii, applied modifiers, face flags) |
| `python tests/race_harness.py 300`: fuzzes the order of commits, messages and applies, each client and the server with their own copy of the merge code. Scenario `clash`: two edits on one region. Scenario `delete`: one user runs 1 to 3 delete, delete-vertex, dissolve or merge steps in one corner of a 9 by 9 grid while the other runs any 1 to 3 edits in the far corner, each step inside an Edit Mode window during which loads fail | clash 0 of 300 wrong (before the repair fix, 87 of 200). delete 0 of 300 wrong, and 0 of 1000 more seeds. On the mu4 code delete fails 16 of 300, 2 of them with a load failure (Array length mismatch) |
| `blender -b --python tests/blender_units.py`: the Blender side in Blender 5.2.2 | 6 of 6: inconsistent update refused with the mesh unchanged, a load failing partway restores the mesh, skin layer and radii survive a mesh load and an object reload, a missing image file neither raises nor becomes a dependency. On mu4 0 of 6 |
| `tests/live_loop.sh` and `tests/live_check.py`, two Spaces driven with cua `space_bash`: the host runs a delete-type step on the box top while the client edits the +x side, host 0 to 0.3 s early or late | 18 of 18 on mu5: delete faces, delete vertices, dissolve faces, dissolve edges, merge at centre and merge by distance against poke, inset, extrude and bevel. Both Blenders identical by vertex id and equal to the geometry `expected_meshes.py` gives for both edits. Not run on mu4 |
| The same loop with `PAIRS=poke:inset`, both on the top centre (the mu4 clash, as a regression check) | 3 of 3 on mu5, host 0, 0.3 s late and 0.3 s early: both Blenders identical, ending with the inset alone (with a notice), the poke alone, and poke then inset, all allowed results |
| `tests/live_mu5.py MODE=extras`: skin guides, applied modifiers, script deletion, face flags | mu5: both Blenders end with skin radii 0.5 and 0.9 on the right new vertices and the layer kept, no modifiers left on Mods after the other user's later move, both script-removed objects (scene and child collection) gone from the client within 5 s while it sent updates, both faces smooth. mu4: every radius back at 0.25, then `Array length mismatch (expected 8, got 6)` in the object reload stopped the client's apply timer. Without the skin part (`NO_SKIN=1`), Subdivision came back on the host after the client's move. Script deletion and face flags came out right on mu4 too |
| `tests/live_mu5.py MODE=splitjoin` and `MODE=sequence`: one editor separates the top and joins it back with a merge of the doubled seam (the planner's 2026-11-03 sequence) while the client pokes the side; one editor dissolves, then merges an edge | Both Blenders equal to the expected geometry, on mu5 and on mu4: these runs did not reproduce the one-editor remap, which the offline tests do |
| `sh tests/live_trials.sh A B n delay hostop clientop` (mu4, Docker): two Spaces clash on the centre of the box top. `tests/expected_meshes.py` computes the correct results in headless Blender | 20 of 20 across poke, inset, bevel and extrude pairs, at the same moment and 0.3 s apart, plus 7 of 7 poke against inset. Both Blenders identical and correct every time |
| `tests/live_session.py` (mu4): notices, deletion and pruning in a 6-minute two-Space run | One correct notice on each Blender, the deleted object gone from the other Blender within 5 s, the table pruned from 5 commits to 1 and 64 tombstones to 16. Popup checked on screen |
| `patch/v3/stress.py`, three clients on the 482-vertex `A_head`: disjoint, overlap, conflict, undo, reconnect, stale move, topo, topoclash | All pass: identical meshes, every edit present, the newer edit wins a clash |
| Agents by hand: three in three Spaces on one cube; two on the real head mesh (Mirror modifier, UVs) with loop cut, connect and merge | All edits kept, identical on every Blender |

Before the repair fix, edits on one region less than a second apart could leave a mix of both edits, in v3 too. Repair compared numpy float32 coordinates with Python floats, which only match for values exact in float32, so a repeated repair gave new ids. A merge that arrived before the repaired ids were loaded back recorded a topology change nobody made, which bypassed the conflict rule.

The mu5 fixes, from the 2026-11-03 and 11-04 runs. The stale-restore check compared a whole mesh index by index. When a count came back to a value an earlier update had replaced, it put the old count back over buffers of the new size: the next load failed with "Array length mismatch" (697 faces against 693, 2017 edges against 2015) and Blender crashed on the half-loaded mesh. After a merge reordered the faces, it put old loop starts back over a face list of the same length, so faces landed on other vertices, with one editor too. Skin data went by vertex index in the object node, and Blender deletes the skin layer whenever a load remakes the modifier stack. A list emptied by applying its last modifier carried no stamp and lost to any older copy. Face flags were merged as buffers that stamp four faces per word.

## Known limits

- Vertex colors and other attributes (crease, bevel weight) fall back to whole-mesh merging, newest side wins. Shape keys and vertex groups go in the object dump by vertex index, as upstream does, so after another user's topology edit they can sit on the wrong vertices (read from the code, not tested).
- mu4 and mu5 cannot share a session: skin data moved into the mesh dump and face flags into the table. Restart the session after upgrading every Blender.
- Two edits sharing even one base vertex conflict, and the older one is dropped in that region. Two users extruding a new vertex from the same vertex at once is such a conflict.
- Seams and sharp edges are not in the table. After a topology merge they come from the other user's dump where both have the edge, so a concurrent seam change can be lost.
- A dict key removed on one side, for example the parent of an unparented object, comes back from an older copy. mu5 fixes this only for lists and buffers that become empty.
- Script deletion: the apply timer now deletes for everyone an object whose datablock is gone, but three live variants on mu4 (scene collection, child collection, with incoming updates) all propagated, so the cause of the 2026-11-03 miss is not confirmed.
- The live runs on the test box did not reproduce the one-editor face remap or the face-flag flip on mu4. Those are shown by the offline tests, which run the real merge code.
- Two new users whose names hash to the same number, making their first topology edit at the same moment, can share a number. Users with the same username always do.
- A commit that arrives more than 120 s after a concurrent one it overlaps (for example from a long Edit Mode session) merges without the conflict rule.
- Edit Mode edits sync when the user leaves Edit Mode, and incoming updates wait meanwhile. Hosting a file saved in Edit Mode fails upstream: switch to Object Mode first.
- Full-state updates are large (about 210 KB for a 482-vertex mesh). A slow machine can lag: three GUIs and the server diverged on 2 CPUs and converged on 6.
- Upstream sends a new object only after its owner's first edit. The mesh of a deleted object stays as orphan data.
- Loop cut, connect, merge and Knife have only been tested away from clashes, and a three-agent run on the topology merge was never completed.

## Stress test

Copy `patch/v3/stress.py`, `run.sh`, `check.py`, `check_topo.py` and `owners.py` to `~/stress/` next to a `.blend` with the mesh to edit (`setup.sh` does this). Then:

```sh
sh run.sh disjoint 3 180        # scenario, clients, seconds of editing
python3 check.py out_disjoint_3 exact
```

Scenarios: `disjoint`, `overlap`, `undo`, `reconnect`, `conflict`, `stale`, `single`, `topo`, `topoclash`, `ops` (`BLEND=<file> OBJ=<mesh>`). Never start `run.sh` from a command line containing `stress.py`: it kills every process matching that name.

## License

GPL-3.0-or-later, the license of Multi-User and replication, which this modifies. See [LICENSE](LICENSE). Multi-User and replication are by Swann Martinez and contributors.
