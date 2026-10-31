# Multi-User concurrent editing patch (experimental)

A test patch for the Blender Multi-User extension 0.8.2 (replication 0.10.3, Blender 5.2.2). It lets several users edit the same object at the same time. Each value goes to whoever wrote it last, and no one's edit silently overwrites someone else's. Tested 2026-10-29 and 2026-10-30 in cua Space `local:blender-agent-b`, with up to three Blender instances driven by a stress script.

To build a Space with all of this from scratch, see [SETUP.md](SETUP.md) and run `setup.sh`.

## The patched add-on

`addon/` holds the whole add-on with every patch applied: `addon/multi_user/` is the extension and `addon/replication/` is the replication library it bundles as a wheel. The history shows the change against upstream. One commit adds Multi-User 0.8.2 and replication 0.10.3 as downloaded from extensions.blender.org, and the next applies the v2 and v3 patches, so its diff is the full change.

`python3 build.py` packs `addon/` into `dist/multi_user-0.8.2+mu4.zip`, an extension Blender installs like any other: Edit > Preferences > Get Extensions > Install from Disk, or `blender -c extension install-file -r user_default -e <zip>`. Remove the upstream Multi-User first, since both use the id `multi_user`. The replication wheel inside is versioned `0.10.3+mu4` so Blender does not keep an upstream copy in its place.

`addon/` is the current version: v3 plus the mu4 changes below. `patch/` keeps the earlier versions as patches against upstream, for reference. `patch/v3/` is v2 plus merging of concurrent topology edits on one mesh, see "v3: topology merge" below. `patch/v2/` is the per-value merge without it. `patch/v1/` is the earlier three-way merge. It loses edits with three clients. `tests/test_sim.py` runs the offline simulation against `addon/replication`, with `python tests/test_sim.py`. `tests/race_harness.py` fuzzes the order of commits, network messages and applies around two edits on one region, with each client and the server running their own copy of the real merge code, and checks that everyone ends with a correct mesh.

## mu4: conflict notices, deletion, client numbers, pruning (2026-10-31)

Changes made directly in `addon/`, on top of v3.

- **Conflict notices.** When two users' topology edits clash and one is dropped, every Blender in the session shows a popup and a red box at the top of the Multi-User panel, naming the object, both users and how many elements were dropped. Each conflict is reported once. The commit that writes a resolution down (a settle commit) records what it settled, so a Blender that never saw the conflict open still reports it. Settle commits do not conflict with each other: in the first live run, two concurrent settle commits produced a second, wrong notice.
- **Deleting an object.** The deleting Blender marks the `Delete` command when the datablock is really gone (`client:deleted`). The other Blenders remove their copy too, in `ApplyTimer`. An object in Edit Mode waits until its user leaves Edit Mode. A "stop tracking" (`wm.session_datablock_ignore`) is not marked and removes nothing.
- **Client numbers.** Each mesh's table holds a register of client numbers by username. A user takes the number its username hashes to, or the next free one, and keeps it. Two usernames with the same hash no longer share a number, so their fresh vertex ids no longer collide.
- **Pruning.** A commit that every active client has seen, and that is at least `PRUNE_SETTLE` (120 s) older than the newest commit in the table, is dropped from the table along with the tombstones it left. The check uses only the table, so every replica prunes the same way, and a prune that would change the mesh is skipped. A client whose newest topology commit is `PRUNE_AGE` (600 s) older than the newest one stops holding pruning back.
- **Fixed: edits on one region less than a second apart could end mixed** (in v3 too). Two causes, both found from traces of a failing live run:
  - `repair_ids` keyed its "missing id at the same position" lookup with numpy float32 coordinates on one side and Python floats on the other. They never compare equal unless the value is exact in float32 (0.25 is, 0.2 is not), so repeating a repair gave different ids. When an update arrived before a commit's repaired ids were loaded back into Blender, the merge repaired the same mesh again, got new ids, and recorded a topology change nobody made. That commit had seen everything, so it bypassed the conflict rule.
  - Blender's inset and poke pass the integer id attribute through float32, so ids above 2**24, which fresh ids (`client << 23 | counter`) always are, lose their low bits on vertices the edit never touched (1560281096 became 1560281088). Repair now also gives such a vertex the nearest missing table id that rounds to its id in float32, which works even if the vertex moved.
- `.gitattributes` keeps every text file LF.

Results:

| Test | Result |
|---|---|
| `tests/test_sim.py` | 22 of 22 pass: the 13 v3 cases, plus number register, colliding usernames, pruning a settled commit, a conflict kept until settled, the no-change guard, concurrent settle commits, notices, a repeated repair at inexact coordinates and ids rounded by Blender. The last two failed before the fix |
| `tests/race_harness.py`, inset ratio 0.2 (coordinates not exact in float32), ids passed through float32 after each edit | Before the fix 87 of 200 runs ended with a wrong mesh. After it 0 of 300, with and without the id rounding, and 0 of 300 at ratio 0.25 |
| Live, two Spaces, one Blender each (`tests/live_session.py`), alice hosts, bob joins. At +50 s both edit the centre of the box top at once: alice pokes, bob insets. At +90 s alice deletes the object `Doomed`. At +200 s bob pokes the +X side, at +330 s alice pokes the bottom | Pass. Bob's inset won, and each Blender showed exactly one notice: "'Shared_box': alice and bob edited the same vertices at once. Kept bob's edit, dropped 19 element(s)." `Doomed` disappeared from bob's Blender within 5 s ("Removed Doomed, deleted by another user"). Both Blenders held the same mesh by id at every 5 s sample. The table went from 5 commits to 4 at +200 s (alice's settled commits pruned) and to 1 at +330 s, with tombstones from 64 to 16. No conflicts left and no tracebacks |
| GUI check, same setup, screenshots of both Blenders 2 s after the clash | The popup shows in both, titled "Multi-User: concurrent edit", with the object, both users and the count. The panel box was not seen: a script cannot switch the sidebar to the Multiuser tab |
| Same-region clash before the fix: four runs at the same moment, two with alice 0.3 s later, one of them on the plain v3 code | Wrong meshes, the same on both Blenders. v3 ended at 110 vertices and 120 faces, poke and inset both applied to the same four faces. One same-moment run ended at 106 and 108. With alice 0.3 s later she poked on top of bob's inset after it arrived (12 faces), which should give 118 and 140, and it ended at 115 and 140, in both runs that tried it |
| Same-region clash after the fix (`tests/live_trials.sh`): three runs with alice 0.3 s later, four at the same moment | Pass, all seven, both Blenders identical. The 0.3 s sequential case gave 118 and 140. The clashes kept bob's inset (106/104) three times and alice's poke (102/108) three times |

## License

GPL-3.0-or-later, the license of Multi-User and replication, which this repository modifies. See [LICENSE](LICENSE). Multi-User and replication are by Swann Martinez and their contributors.

## v3: topology merge (2026-10-31)

Design in [TOPOLOGY-DESIGN.md](TOPOLOGY-DESIGN.md). The standalone prototype it grew from is in [topology/](topology/README.md).

| File (in `patch/v3/`) | What it is |
|---|---|
| `topo3.py` | New module in `replication/`. Permanent vertex ids, the topology table, its merge, conflict view and canonical rebuild |
| `merge3.py` | v2's merge3 with the topo3 hooks: `lww` hands two meshes with tables to topo3, `stamp_commit` and `merge_fetched` attach the table to each fresh dump |
| `bl_mesh.patch` | `multi_user/bl_types/bl_mesh.py`: also send the `mu_vid` attribute. The installed file has CRLF line endings, so `install.sh` applies it with sed |
| `install.sh` | Installs v3 over v2 in a Space and backs up what it replaces to `~/own/v2-installed/` |
| `test_v3_sim.py` | Offline simulation of commits, server merges in several orders and client applies, using the real merge3 and topo3. No Blender |
| `stress.py`, `check_topo.py` | v2's harness plus the `topo` and `topoclash` scenarios, which use real Edit Mode operators |

The v2 patches (`porcelain`, `repository`, `interface`, `net`, `handlers`, `timers`) are unchanged and still needed.

How it works. Each vertex has a permanent id in the integer attribute `mu_vid`. Each mesh node carries a table of every vertex, face and loose edge that has existed, by id, with an alive flag, a stamp and the commit that last changed it. Deletions stay as tombstones. Two tables merge entry by entry, newest stamp first, so the server and every client reach the same table in any order without knowing a common base. Each commit also records which commits it had seen. Two commits that had not seen each other and that touch the same base vertices conflict, and in that region the older one is hidden when the table is turned into a mesh. The next commit from any client makes that permanent. Vertex moves still merge per vertex, newest first. When the vertex layout changes, the mesh is rebuilt in one canonical order (ids ascending) with positions, UVs, seams and sharp edges carried by id.

Blender copies an existing id onto each vertex an edit creates. Before each diff, duplicated ids are repaired: the copy nearest the id's old position keeps it, and the others take the id of a missing vertex at the same position or a fresh one (`client number << 23 | counter`, client number from the username).

Results, Space B, three clients, `A_head` (482 vertices, 512 faces, with a UV map):

| Test | Result |
|---|---|
| `test_v3_sim.py` | 13 of 13 pass |
| topo: 60 s, agent0 extrudes, agent1 subdivides, agent2 insets faces in its own third of the head, plus object moves | Pass. All 30 topology edits present (611 vertices, 608 faces as expected), identical mesh by id, UVs and transform on all three, no conflicts |
| topoclash: agent0 extrudes a +X face and a far -X face in one Edit Mode session, agent1 subdivides the same +X face 0.3 s later, agent2 insets a -Y face | Pass. One conflict, won by agent1. agent0's far extrude and agent2's inset kept (495 vertices, 523 faces, as expected). Identical on all three. The conflict was settled afterwards |
| v2 scenarios rerun on v3: disjoint, undo and reconnect (90 s each), conflict, stale | Pass, same as v2. 0 of 482 vertices wrong on all three in disjoint, undo (34 undos) and reconnect. Conflict 2.222 at the midpoint and 4.444 at the end. Stale (0.5, 0.5, 2.05). No tracebacks |

| GUI, two AI agents in Spaces A (host) and C, hand edits on `Shared_box`, a cube with a 4 by 4 grid per side (98 vertices, 96 faces). A extrudes a top center quad by 0.3. C, in one Edit Mode session, Knife-cuts a +X center quad and insets a -Y center quad by 0.1 | Pass. C was in Edit Mode while A's extrude arrived, so C's edits were made without it. Both Spaces ended at 108 vertices and 105 faces, the count with all three edits kept, with the same id hash, zmax 0.8 and no conflicts |

| ops on the real head: a copy of `pdf-sample-book-ear-rebuild-2026-10-28.blend`, object `Circle.002` (325 vertices, 318 faces, Mirror modifier with clipping, UV map). Two clients in Space A, one in the ear region, one around the eye socket. Each runs three Edit Mode sessions of 4 s that overlap the other's: loop cut (`loopcut_slide` on an edge ring inside its region), connect vertex path across a quad, merge at center of an edge | Pass. Loop cuts of 13 and 15 faces, two connects, two merges, all present on both clients: 351 vertices and 345 faces, the count with all six edits. Same id hash and UV hash, no conflicts |
| ops on `A_head` | Connect and merge synced and both clients converged. No loop cut ran: `A_head` is closed, so every edge ring wraps around the head and leaves either region |

Hosting a .blend that was saved in Edit Mode fails in upstream Multi-User: server init cannot dump a mesh in Edit Mode and the session ends. The harness switches to Object Mode before hosting. Do the same when hosting by hand.

v3 is installed in Spaces A, B and C. A three-agent GUI run on v3 was started on 2026-10-31 but stopped before it could be checked.

The first topo run failed: every client numbered the mesh on its own, and those numberings counted as conflicting commits, so about 2,000 elements were hidden for a while and one edit was lost. The first numbering is now a shared baseline built from the synced mesh, which cannot conflict.

v3 limits, in addition to v2's:

- Vertex colors and other attributes (crease, bevel weight) switch the mesh back to v2's whole-mesh rule, because a rebuild would drop them. Shape keys and vertex groups are not part of the mesh dump and are not handled.
- Two edits that share even one base vertex conflict, and the older one is dropped in that region. Since mu4 every Blender shows a notice.
- Two new users who make their first topology edit on one mesh at the same moment, with usernames that hash to the same number, both take it. The table logs a warning, and their fresh ids can collide. Users with the same username always share a number.
- A topology commit that arrives more than `PRUNE_SETTLE` after a concurrent commit it overlaps, for example from a user who stayed in Edit Mode that long, is merged without the conflict rule, entry by entry. Tombstones dropped by pruning can come back from such a client.
- Pruning drops commit records and tombstones only. Every vertex, face and loose edge of the current mesh stays in the table, and the client number register is never pruned.
- The first edit on a client before any ids were loaded maps vertices by index, which is right when Blender appended the new vertices. After a deletion it falls back to matching positions.
- Loop cut, connect vertex path, merge and Knife have each been run on v3 (loop cut, connect and merge scripted through the real operators, Knife by a GUI agent). A loop cut whose ring leaves your region will touch other users' regions and can conflict. A three-agent GUI run has not been completed on v3.

## Files (in `patch/v2/`)

| File | What it is |
|---|---|
| `merge3.py` | New module, dropped into the `replication` package. Stamps every value with its write time and merges last-writer-wins. |
| `porcelain.patch` | `commit`: send full state with stamps instead of a delta, stamp against what the datablock holds. `apply`: merge before loading, also on a forced reload. `push`: re-apply remote values a commit kept but the datablock does not have yet. |
| `repository.patch` | `do_commit`: merge an incoming update with the stored one, on clients and on the server, so a second update cannot wipe the first before it is applied. |
| `net.patch` | `replication/net/__init__.py`: read everything the socket holds per poll, not 64 KB. Needed for full-state updates (see below). `replication/net.py` exists too but is never imported, because the `net/` package shadows it. |
| `interface.patch` | `listen`: handle every complete data message per tick, not one. |
| `handlers.patch` | `multi_user/handlers.py`: also send edits to `COMMON`-owned objects. Upstream sends them only for Scene, Collection and World. |
| `timers.patch` | `multi_user/timers.py`: with `merge3.FREE_EDIT = True`, selecting an object no longer locks it. |
| `stress.py`, `run.sh`, `check.py`, `owners.py` | The stress harness. See "Running the stress test". |

Installed locations in a Space: `~/.config/blender/5.2/extensions/.local/lib/python3.13/site-packages/replication/` and `~/.config/blender/5.2/extensions/blender_org/multi_user/`. Apply the patches with `patch -p1` from the parent of both folders, and copy `merge3.py` into `replication/`.

## How it works

Every node's data carries a stamp tree under the `_mu_ts` key. It mirrors the data, with one time per scalar, one per list element, and one float64 per 4-byte word of a numpy buffer such as vertex coordinates. A commit stamps only the values that changed since the datablock's last synced state, and sends the whole node with its stamps. Every receiver, and the server, merges value by value, and the newer stamp wins. Merging full states this way gives the same result in any order, so every client ends on the same data.

If element counts differ (`vertex_count`, `poly_count` and so on), one whole side is kept, the one with the newest stamp, so a mesh never mixes two topologies.

Some tools write back a state they captured earlier. Blender's move tool (G) writes its starting position plus the move when you confirm, and undo restores a saved snapshot. If another user's change arrived in the meantime, the tool writes the old value back over it. To catch this, each client keeps the values that incoming updates replaced, the last 64 per node, each paired with what replaced it. A commit that sets a value back to one of those replaced values is treated as a stale restore. The synced value is kept, and the datablock is reloaded. Only values an incoming update actually changed count, so undoing your own edit still goes through.

After a merge, a client republishes only when the merge carried local edits that were never committed. That happens when an incoming update marks the node as fetched before the edit's commit runs. Republishing for any other reason makes every receiver echo each stale update it sees, and the slowest client falls behind.

## Results

| Test (3 clients unless noted) | Result |
|---|---|
| Disjoint: each client moves its own third of the 482 head vertices and its own location axis, about 1 edit per second each, 180 s, 675 edits total | Pass. Identical mesh hash on all three, 0 of 482 vertices wrong, the newest write to each axis on every client, merges stop when editing stops |
| Conflict (step 2): agent1 writes loc[0]=1.111 and agent2 writes 2.222 200 ms later, then agent2 writes 3.333 and agent1 writes 4.444 47 ms later | Pass. 2.222 on all three at the midpoint sample, 4.444 on all three at the end |
| Overlap: all clients move any vertex and the same location axis, 120 s | Pass. Identical on all three, newest location write on every client. Moves to the same coordinate are not summed: the later one replaces the earlier |
| Reconnect: agent1 quits at 48 s and rejoins at 54 s, 120 s | Pass. Identical on all three, 0 of 482 vertices wrong |
| Undo: agent1 also undoes 25% of the time, 44 undos, 120 s | Pass with the stale-restore fix. 0 of 482 vertices wrong on all three, agent1's own undos included. Without the fix, 289 vertices went back to old values everywhere |
| Stale move: agent0 snapshots the location, agent1 moves Y and agent2 moves Z, then agent0 writes snapshot + 0.5 on X (what the move tool does) | Pass with the fix: (0.5, 0.5, 2.05) on all three. Without it, both other moves were lost: (0.5, 0.0, 1.55) |
| 2 clients, GUI hand edits in Edit Mode (v1 patch, 2026-10-29) | Both edits kept |

The scripted results above are from one Space, all clients on one machine. Every scenario above was rerun on the final code (disjoint for 120 s and 180 s).

### Three AI agents in three Spaces, hand-editing one cube (2026-10-30)

Spaces A (host), B and C, one Blender each, joined in one session over the Docker network. Three separate agents each drove their own Blender only with screenshots, clicks and keys, and all three edited `Shared_box` at the same time. Round 1: A raised the top face 0.3, B pushed the +X face out 0.3, C pushed the -Y face out 0.3, all in Edit Mode. Round 2: each moved the object 0.5 on its own axis (A on X, B on Y, C on Z).

| Run | Mesh (round 1) | Object moves (round 2) |
|---|---|---|
| 1, before the stale-restore fix | All three faces kept in all three Spaces (hash `f3d4e7837d`, checked against every combination of the three edits) | X and Z kept, B's Y lost: this is the bug the fix addresses |
| 2, with the fix | All three faces kept in all three Spaces (`f3d4e7837d`) | Not judged. Agent A pressed X with the pointer over the outliner, which deletes the object. B and C kept their copy but it stopped syncing (see "Deleting an object"), so their Y and Z moves stayed local |
| 3, with the fix | All three faces kept in all three Spaces (`f3d4e7837d`) | **All three kept.** Location (0.5, 0.5, 1.501) in all three Spaces, no errors. The stale-restore rule fired twice in Space C. Agent B saw A's X move arrive during its own move, and X stayed 0.5 after B confirmed |

In runs 2 and 3 the agents were told to select only in the 3D viewport, to click the viewport before pressing keys (the driver's `move_cursor` does not move the real pointer, and Blender sends keys to the editor under it), and never to press X or Delete.

## Known limits

- **Deleting an object** split the session up to v3: upstream removed the node from every repository but left the object in the other Blenders, where it stopped syncing. Fixed in mu4 (see above). The mesh of a deleted object stays as orphan data in every Blender, as it does in the deleting one.
- **Stale-restore rule.** A value set back to one that an incoming update replaced, within the last 64 updates to that node, is ignored. A user who deliberately types the old value back is ignored too. An undo that reaches further back than 64 updates can still restore other users' old values.
- **CPU.** Three Blender GUIs and the server diverged on 2 CPUs: one client fell behind and was still stale at the final check. They converged on 6 CPUs. Real users run one Blender per machine, so this test is harsher than real use, but a slow client can still lag.
- **Bandwidth.** A full-state mesh update is about 210 KB for the 482-vertex head, against a few KB for a delta. The stamps take twice the space of the data they cover. Large meshes will need deltas of stamped values instead.
- **Clocks.** Stamps use `time.time()`. Across machines the clocks must be in sync (NTP) for "last" to mean anything.
- **Topology.** In v2, topology changes are not merged: one side's whole mesh wins. v3 merges them, see above.
- **New objects.** Upstream sends a new object only after its owner's first edit, and objects added in the same second can be lost. This patch does not change either.
- **Edit Mode.** Mesh edits made in Edit Mode sync when the user leaves Edit Mode. An incoming update waits while the receiver is in Edit Mode.

## Running the stress test

Copy `stress.py`, `run.sh`, `check.py` and `owners.py` to `~/stress/` in a Space, next to a `two_agent_build.blend` that holds an `A_head` mesh. Then run:

```sh
sh run.sh disjoint 3 180        # scenario, clients, seconds of editing
python3 check.py out_disjoint_3 exact
python3 owners.py out_disjoint_3
```

Scenarios are `disjoint`, `overlap`, `undo`, `reconnect`, `conflict`, `stale` and `single`. `SETTLE` sets how many seconds to wait after editing before the final dump (default 45). The `exact` check of every vertex applies only to `disjoint` and `reconnect`. Do not start `run.sh` from a shell whose command line contains `stress.py`, because `run.sh` kills every process that matches that name.
