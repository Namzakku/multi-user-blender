#!/bin/sh
# Install v3 over an installed v2 in a Space. Run as the cua user with merge3.py
# and topo3.py next to this script. Backs up what it replaces to ~/own/v2-installed/.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
EXT=/home/cua/.config/blender/5.2/extensions
REP=$EXT/.local/lib/python3.13/site-packages/replication
MESH=$EXT/blender_org/multi_user/bl_types/bl_mesh.py
BAK=/home/cua/own/v2-installed
mkdir -p "$BAK"
[ -f "$BAK/merge3.py" ] || cp "$REP/merge3.py" "$BAK/merge3.py"
[ -f "$BAK/bl_mesh.py" ] || cp "$MESH" "$BAK/bl_mesh.py"
cp "$HERE/merge3.py" "$HERE/topo3.py" "$REP/"
rm -rf "$REP/__pycache__" "$EXT/blender_org/multi_user/bl_types/__pycache__"
# bl_mesh.py has CRLF line endings, so edit it with sed rather than patch.
if ! grep -q "'mu_vid'" "$MESH"; then
  sed -i "s/^    'bevel_weight_edge'\r\?$/    'bevel_weight_edge',\r\n    'mu_vid',  # patched: permanent vertex ids (replication\/topo3.py)\r/" "$MESH"
fi
grep -n "mu_vid" "$MESH"
md5sum "$REP/merge3.py" "$REP/topo3.py"
