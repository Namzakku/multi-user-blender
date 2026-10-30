# Multi-User concurrent editing patch (experimental)

A test patch for the Blender Multi-User extension 0.8.2 (replication 0.10.3, Blender 5.2.2). It lets several users edit the same object at the same time. Each value goes to whoever wrote it last, and no one's edit silently overwrites someone else's. Tested 2026-10-29 and 2026-10-30 in cua Space `local:blender-agent-b`, with up to three Blender instances driven by a stress script.

`patch/v2/` is the current version. `patch/v1/` is the earlier three-way merge, kept for reference. It loses edits with three clients.

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

- **Deleting an object splits the session.** Upstream sends a `Delete` command that removes the node from every client's repository, but nothing removes the object itself from the other Blenders. They keep a copy that no longer syncs, and their later edits to it stay local, without any warning. Found in the three-agent run. Not fixed.
- **Stale-restore rule.** A value set back to one that an incoming update replaced, within the last 64 updates to that node, is ignored. A user who deliberately types the old value back is ignored too. An undo that reaches further back than 64 updates can still restore other users' old values.
- **CPU.** Three Blender GUIs and the server diverged on 2 CPUs: one client fell behind and was still stale at the final check. They converged on 6 CPUs. Real users run one Blender per machine, so this test is harsher than real use, but a slow client can still lag.
- **Bandwidth.** A full-state mesh update is about 210 KB for the 482-vertex head, against a few KB for a delta. The stamps take twice the space of the data they cover. Large meshes will need deltas of stamped values instead.
- **Clocks.** Stamps use `time.time()`. Across machines the clocks must be in sync (NTP) for "last" to mean anything.
- **Topology.** Topology changes are not merged: one side's whole mesh wins.
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
