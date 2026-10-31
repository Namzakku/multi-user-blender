# Multi-User concurrent editing (experimental)

A patched build of the Blender Multi-User extension 0.8.2 (replication 0.10.3, Blender 5.2.2) that lets several users edit the same object, and the same mesh, at the same time. Values merge last-writer-wins per value, topology merges by permanent vertex id, and a dropped edit is reported to everyone.

## Install

- In a cua Space: follow [SETUP.md](SETUP.md), which runs `setup.sh`.
- Anywhere else: `python3 build.py` packs `addon/` into `dist/multi_user-0.8.2+mu4.zip`. Install it with Edit > Preferences > Get Extensions > Install from Disk, or `blender -c extension install-file -r user_default -e <zip>`. Remove the upstream Multi-User first, since both use the id `multi_user`.

## Layout

| Path | What it is |
|---|---|
| `addon/multi_user/`, `addon/replication/` | The add-on and the library it bundles, with every change applied. In the history, one commit adds upstream unmodified and the next applies the v2 and v3 patches, so its diff is the change against upstream |
| `build.py` | Packs `addon/` into an installable zip. replication goes in as a `0.10.3+mu4` wheel so Blender does not keep an upstream copy |
| `setup.sh`, `SETUP.md` | Build a test Space from scratch |
| `tests/` | Offline simulation, race fuzzer and live two-Space tests (see "Tests") |
| `patch/v1`, `v2`, `v3` | Earlier versions as patches against upstream, with the stress harness in `patch/v3/` |
| `topology/`, `TOPOLOGY-DESIGN.md` | The standalone prototype and design of the topology merge |

## How it works

**Values.** Every node's data carries a stamp tree under `_mu_ts`: one time per scalar, per list element and per 4-byte word of a buffer such as vertex coordinates. A commit stamps only what changed and sends the whole node. The server and every client merge value by value, newest stamp first, so all reach the same data in any order. Stamps use `time.time()`, so machines need synced clocks.

**Stale restores.** Blender's move tool and undo write back a state captured earlier, which can overwrite another user's newer value. Each client keeps the values incoming updates replaced (the last 64 per node). A commit that sets a value back to one of them keeps the synced value instead. Undoing your own edit still works.

**Topology.** Each vertex has a permanent id in the integer attribute `mu_vid`. Each mesh node carries a table of every vertex, face and loose edge that ever existed, with an alive flag, a stamp and the commit that last changed it. Tables merge entry by entry, newest first. Each commit records which commits it had seen. Two commits that had not seen each other and touch the same base vertices conflict: the older one is hidden in that region, and the next commit from any client (a settle commit) makes that permanent. After a topology change the mesh is rebuilt in id order, with positions, UVs, seams and sharp edges carried by id.

**Id repair.** Blender copies an existing id onto each vertex an edit creates, and its inset and poke pass the attribute through float32, so ids above 2**24 lose their low bits even on vertices the edit did not touch. Before each diff, `repair_ids` makes ids unique again and gives the same answer every time it runs on the same mesh: a vertex keeps or recovers its id by position, then by float32 rounding, and only then gets a fresh id (`client number << 23 | counter`).

**Client numbers.** Each table registers client numbers by username. A user takes the number its name hashes to, or the next free one, so two names with the same hash never share fresh ids.

**Conflict notices.** When an edit is dropped, every Blender shows a popup and a red box at the top of the Multi-User panel, naming the object, both users and the number of dropped elements. Settle commits record what they settled, so a Blender that never saw the conflict open still reports it.

**Deleting objects.** The deleting Blender marks the delete as real (`client:deleted`), and the others remove their copy too. An object in Edit Mode is removed when its user leaves Edit Mode. "Stop tracking" removes nothing.

**Pruning.** A commit every active client has seen, at least 120 s older than the newest commit, is dropped from the table with the tombstones it left, unless that would change the mesh. The decision uses only the table, so every replica prunes alike.

## Tests

| Test | Result |
|---|---|
| `python tests/test_sim.py`: offline commits, server merges in several orders, client applies, using the real merge code | 22 of 22 |
| `python tests/race_harness.py 300`: fuzzes the order of commits, messages and applies around two edits on one region, each client and the server with their own copy of the merge code | 0 of 300 wrong. Before the repair fix, 87 of 200 |
| `sh tests/live_trials.sh A B n delay hostop clientop`: two Spaces clash on the centre of the box top. `tests/expected_meshes.py` computes the correct results in headless Blender | 20 of 20 across poke, inset, bevel and extrude pairs, at the same moment and 0.3 s apart, plus 7 of 7 poke against inset. Both Blenders identical and correct every time |
| `tests/live_session.py`: notices, deletion and pruning in a 6-minute two-Space run | One correct notice on each Blender, the deleted object gone from the other Blender within 5 s, the table pruned from 5 commits to 1 and 64 tombstones to 16. Popup checked on screen |
| `patch/v3/stress.py`, three clients on the 482-vertex `A_head`: disjoint, overlap, conflict, undo, reconnect, stale move, topo, topoclash | All pass: identical meshes, every edit present, the newer edit wins a clash |
| Agents by hand: three in three Spaces on one cube; two on the real head mesh (Mirror modifier, UVs) with loop cut, connect and merge | All edits kept, identical on every Blender |

Before the repair fix, edits on one region less than a second apart could leave a mix of both edits, in v3 too. Repair compared numpy float32 coordinates with Python floats, which only match for values exact in float32, so a repeated repair gave new ids. A merge that arrived before the repaired ids were loaded back recorded a topology change nobody made, which bypassed the conflict rule.

## Known limits

- Vertex colors and other attributes (crease, bevel weight) fall back to whole-mesh merging, newest side wins. Shape keys and vertex groups are not synced.
- Two edits sharing even one base vertex conflict, and the older one is dropped in that region.
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
