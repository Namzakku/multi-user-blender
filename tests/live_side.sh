#!/bin/sh
# Restart this Space's Blender for one live trial, as the user cua:
#   sh ~/mu/live_side.sh <script in ~/mu> <host|client> <name> <T0> [VAR=value ...]
# Stops Blender and any old Multi-User server first, clears the old logs, then
# starts `blender --python ~/mu/<script>` on display :1 with ROLE, NAME, T0 and
# the extra variables (for example IP=172.17.0.5 HOST_OP=delete@top).
# Run it through the cua MCP tool space_bash, or docker exec -u cua.
SCRIPT=$1; ROLE=$2; NAME=$3; T0=$4; shift 4
pkill -x blender
pkill -f replication/server.py
pkill -f replication/ttl.py
sleep 1
rm -f "/tmp/live_$NAME.log" "/tmp/mu5_$NAME.log" /tmp/blender_live.log
env HOME=/home/cua DISPLAY=:1 ROLE="$ROLE" NAME="$NAME" T0="$T0" "$@" \
  nohup blender --python "/home/cua/mu/$SCRIPT" > /tmp/blender_live.log 2>&1 &
echo "started $SCRIPT as $ROLE $NAME"
