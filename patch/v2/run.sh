#!/bin/sh
# usage: run.sh <scenario> <clients> <duration-seconds>
SCEN=$1; N=$2; DUR=$3
OUT=/home/cua/stress/out_${SCEN}_${N}
rm -rf "$OUT"; mkdir -p "$OUT"
export DISPLAY=:1 SCEN N DUR OUT PORT=5600
# stop earlier stress instances and their 5600 server
for p in $(pgrep -f "stress.py"); do [ "$p" != "$$" ] && kill "$p" 2>/dev/null; done
for p in $(pgrep -f "replication/"); do
  c=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
  case "$c" in *"-p 5600"*) kill "$p";; esac
done
sleep 2
T0=$(( $(date +%s) + 30 )); export T0
ROLE=host IDX=0 nohup blender /home/cua/stress/two_agent_build.blend --python /home/cua/stress/stress.py > "$OUT/log_0.txt" 2>&1 &
i=1
while [ $i -lt $N ]; do
  ROLE=client IDX=$i nohup blender --python /home/cua/stress/stress.py > "$OUT/log_$i.txt" 2>&1 &
  i=$((i+1))
done
echo "started $N clients, edits from $(date -d @$T0 +%H:%M:%S) for ${DUR}s"
