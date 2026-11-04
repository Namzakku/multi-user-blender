#!/bin/sh
# Repeat the live same-region clash in two Spaces until the final mesh is wrong.
# Run from Git Bash on the Docker host:
#   sh tests/live_trials.sh <host space> <client space> <trials> [host delay] [host op] [client op]
# Ops are mesh_ops names: a key of mesh_ops.OPS (poke, inset, bevel, extrude,
# delete, dissolve, merge, ...), optionally with a region (delete@top,
# poke@side); defaults poke and inset. Without Docker access use live_loop.sh. Each trial restarts both Blenders, runs live_session.py to +70 s
# and prints both final meshes. A trial passes when both Blenders hold the same
# mesh by id and it has the size of one of the correct results that
# expected_meshes.py computes: the host's edit alone, the client's alone, or
# both in either order. It stops at the first failing trial. Before the
# repair_ids fix, poke against inset with a host delay of 0.3 gave 115/140 every
# time.
export MSYS_NO_PATHCONV=1
H=$1; C=$2; N=${3:-5}; DELAY=${4:-0}; HOP=${5:-poke}; COP=${6:-inset}
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$H")
ALLOWED=$(docker exec -u cua -e HOME=/home/cua "$H" sh -c \
  "blender -b --python /home/cua/mu/expected_meshes.py -- $HOP $COP 2>/dev/null | grep ^ALLOWED | cut -d' ' -f2-")
echo "$HOP vs $COP, host delay $DELAY: allowed $ALLOWED"
i=0
while [ $i -lt "$N" ]; do
  i=$((i + 1))
  for X in "$H" "$C"; do
    docker exec "$X" pkill -x blender
    docker exec "$X" pkill -f replication/server.py
    docker exec "$X" pkill -f replication/ttl.py
    docker exec -u root "$X" sh -c 'rm -f /tmp/live_*.log /tmp/blender_live.log /tmp/topo_*.json'
  done
  T0=$(( $(date +%s) + 20 ))
  docker exec -d -u cua -e HOME=/home/cua -e DISPLAY=:1 -e ROLE=host -e NAME=alice -e T0=$T0 \
    -e HOST_DELAY="$DELAY" -e HOST_OP="$HOP" "$H" \
    sh -c 'blender --python /home/cua/mu/live_session.py > /tmp/blender_live.log 2>&1'
  sleep 12
  docker exec -d -u cua -e HOME=/home/cua -e DISPLAY=:1 -e ROLE=client -e NAME=bob -e IP="$IP" -e T0=$T0 \
    -e CLIENT_OP="$COP" "$C" \
    sh -c 'blender --python /home/cua/mu/live_session.py > /tmp/blender_live.log 2>&1'
  until docker exec "$H" sh -c 'grep -q "\"t\": [6-9][5-9]" /tmp/live_alice.log' 2>/dev/null; do sleep 3; done
  A=$(docker exec "$H" sh -c 'tail -n1 /tmp/live_alice.log' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("%s/%s %s" % (d.get("verts"), d.get("faces"), d.get("id_hash")))')
  B=$(docker exec "$C" sh -c 'tail -n1 /tmp/live_bob.log' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("%s/%s %s" % (d.get("verts"), d.get("faces"), d.get("id_hash")))')
  E=$(docker exec "$H" sh -c 'grep "\"edit\"" /tmp/live_alice.log' | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline())["faces"])')
  echo "trial $i: alice edited $E faces; alice $A; bob $B"
  SIZE=${A%% *}
  if [ "$A" != "$B" ]; then echo "DIVERGED in trial $i"; exit 1; fi
  case " $ALLOWED " in
    *" $SIZE "*) ;;
    *) echo "WRONG MESH in trial $i"; exit 1 ;;
  esac
done
echo "no wrong mesh in $N trials"
