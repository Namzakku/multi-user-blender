# Setting up a Space for concurrent Blender editing

How to build a cua Space that runs Blender with the patched Multi-User add-on (v3), and how to join several Spaces in one session. Everything here comes from this folder, so an agent with this repo can do it without any earlier conversation.

## What you get

- Blender 5.2.2 in `/opt/blender`, on the path as `blender`
- The patched Multi-User from `addon/` (upstream 0.8.2 with the v2 and v3 patches applied), built by `build.py` and installed for the user `cua` as `multi_user 0.8.2+mu4`: per-value merging, and topology merging by permanent vertex id (see [README.md](README.md))
- `~/mu/gui_session.py` to host or join a session, and `~/stress/` with the stress harness

## 1. Create the Space

With the cua MCP server:

```
create_space(name="blender-agent-x", image="ghcr.io/trycua/linux:24.04", on="local", kind="container", spacesd=true)
```

`spacesd=true` matters. Without it the Space has no driver, so agents cannot take screenshots or click.

## 2. Install

Copy this folder into the Space and run `setup.sh` as root. From the Windows host with Docker, in Git Bash:

```sh
export MSYS_NO_PATHCONV=1
docker cp "<path to this repository>" blender-agent-x:/tmp/mu-patch
docker exec -u root blender-agent-x sh /tmp/mu-patch/setup.sh
```

It takes a few minutes, mostly the Blender download. It ends by printing two `MU_CHECK` lines: Multi-User enabled, and `merge3 True topo3 True`. Nothing is downloaded from extensions.blender.org, so a newer upstream release does not affect it.

The script stops if the upstream Multi-User is already installed in the Space, and prints the command that removes it.

## 3. Start a session

One Space hosts, the others join it over the Docker network. Find the host's address with `docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <host container>`.

```sh
# host
docker exec -d -u cua -e HOME=/home/cua -e DISPLAY=:1 -e ROLE=host -e NAME=agentA <host container> \
  sh -c 'blender --python /home/cua/mu/gui_session.py > /tmp/blender_gui.log 2>&1'
# each client, about 20 s later
docker exec -d -u cua -e HOME=/home/cua -e DISPLAY=:1 -e ROLE=client -e NAME=agentB -e IP=<host ip> <client container> \
  sh -c 'blender --python /home/cua/mu/gui_session.py > /tmp/blender_gui.log 2>&1'
```

The host clears its scene and adds `Shared_box`, a cube with a 4 by 4 grid per side. Every Blender logs the mesh to `/tmp/gui_<NAME>.log` every 5 s. Equal `id_hash` values in all logs mean the Spaces hold the same mesh.

To work on a real file instead, open it in the host's Blender, switch to Object Mode, and host from the Multi-User panel. Multi-User cannot host a mesh that is in Edit Mode, and a .blend saved in Edit Mode opens in it.

## 4. Things that have bitten before

- `docker exec -u cua` sets HOME to `/root`. Pass `-e HOME=/home/cua`, or Blender looks for its config in the wrong place.
- In Git Bash, `MSYS_NO_PATHCONV=1` stops container paths from being rewritten, but then a host path must be written `E:/...`, not `/e/...`.
- `pkill -f <pattern>` also kills the shell running it when the pattern appears in its own command line. `run.sh` kills everything matching `stress.py`, so never start it from a command that contains that text.
- Only one Multi-User server can hold a port. Stop old `replication/server.py` and `replication/ttl.py` processes before hosting again, or the new host fails without a clear error.
- cua suspends idle Spaces. A stopped container needs `docker start` before `docker exec`.
- Agents driving Blender through the cua driver: keys go to the editor under the real pointer, and `move_cursor` moves only a synthetic one, so click in the viewport before pressing keys. `type_text` does not work in Blender; press keys one at a time. Numpad keys are not accepted. Never press X or Delete over the Outliner, which deletes the object and splits the session.

## 5. Check it works

```sh
docker exec -u cua -e HOME=/home/cua -e SETTLE=40 <container> sh /home/cua/stress/run.sh topo 3 60
# about 2.5 minutes later
docker exec -u cua <container> sh -c 'cd /home/cua/stress && python3 check_topo.py out_topo_3'
```

Expect `CONVERGED mesh by id: True` and `ALL EDITS PRESENT: True`. The topo scenario needs `two_agent_build.blend` with an `A_head` mesh in `~/stress/`, which is not in the repo. Without it, use `BLEND=<file> OBJ=<mesh object>` with the `ops` scenario.

For same-region clashes between two Spaces, run from the Docker host (Git Bash):

```sh
sh tests/live_trials.sh <host space> <client space> 5 0 bevel extrude
```

It restarts both Blenders per trial and stops at the first wrong or diverged mesh. For conflict notices, deletion and pruning, run `~/mu/live_session.py` in two Spaces with the same `T0` (a Unix time about 20 s ahead), `ROLE=host NAME=alice` in one and `ROLE=client NAME=bob IP=<host ip>` in the other, the same way as `gui_session.py` above. It takes about 6 minutes and logs to `/tmp/live_<NAME>.log`. Its docstring lists what to expect at each step.
