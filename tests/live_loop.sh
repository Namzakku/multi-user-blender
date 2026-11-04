#!/bin/sh
# Run live trials back to back in two Spaces, synchronised by the clock. Start
# it in both Spaces (through cua space_bash, or docker exec -u cua) with the
# same base time, in the background:
#   nohup sh ~/mu/live_loop.sh <host|client> <base epoch> <trials> <result file> [VAR=value ...] &
# Trial k uses T0 = base + 100*k + 25: the host's Blender starts at T0-25, the
# client's at T0-13 (live_side.sh), both run live_session.py, and at T0+75 each
# appends its last status line to the result file. Variables:
#   PAIRS="hostop:clientop ..."   op pairs used in turn (mesh_ops names, op@region)
#   DELAYS="0 0.3 ..."            host delays used in turn (HOST_DELAY)
#   IP=<host address>             needed by the client
# The host also writes "expect <pair> <geo hash>" lines from expected_meshes.py:
# a trial on separate regions passes when both Blenders report the same
# id_hash and that geo hash (compare the two result files line by line).
ROLE=$1; BASE=$2; N=$3; OUT=$4; shift 4
for kv in "$@"; do export "$kv"; done
NAME=alice; LEAD=25
[ "$ROLE" = client ] && NAME=bob && LEAD=13
DELAYS=${DELAYS:-0}
set -- $PAIRS; NP=$#
set -- $DELAYS; ND=$#
pick() { n=$1; shift; shift "$n"; echo "$1"; }
if [ "$ROLE" = host ]; then
  for pair in $PAIRS; do
    a=${pair%%:*}; b=${pair#*:}
    echo "expect $pair $(cd /home/cua/mu && blender -b --python expected_meshes.py -- "$a" "$b" 2>/dev/null | grep ^GEO | cut -d' ' -f4)" >> "$OUT"
  done
fi
k=0
while [ "$k" -lt "$N" ]; do
  T0=$((BASE + 100 * k + 25))
  pair=$(pick $((k % NP)) $PAIRS)
  delay=$(pick $((k % ND)) $DELAYS)
  while [ "$(date +%s)" -lt $((T0 - LEAD)) ]; do sleep 1; done
  sh /home/cua/mu/live_side.sh live_session.py "$ROLE" "$NAME" "$T0" \
    HOST_OP="${pair%%:*}" CLIENT_OP="${pair#*:}" HOST_DELAY="$delay" IP="${IP:-127.0.0.1}" >/dev/null
  while [ "$(date +%s)" -lt $((T0 + 75)) ]; do sleep 1; done
  echo "trial $k $pair delay $delay $(tail -n 1 /tmp/live_$NAME.log)" >> "$OUT"
  if grep -qE "Array length mismatch|Segmentation|Writing: /tmp/.*crash" /tmp/blender_live.log; then
    echo "trial $k CRASH $(grep -m1 -E 'Array length mismatch|Segmentation' /tmp/blender_live.log)" >> "$OUT"
  fi
  k=$((k + 1))
done
echo "done" >> "$OUT"
