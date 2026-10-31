#!/bin/sh
# Build a Multi-User v3 Space from a fresh cua Space (image ghcr.io/trycua/linux:24.04).
# Copy this whole folder into the Space, then run as root:
#   sh /path/to/multiuser-merge-patch/setup.sh
# It installs Blender 5.2.2 and the patched Multi-User in addon/ (upstream 0.8.2 with the
# v2 and v3 merge patches) for the user `cua`. Running it again reinstalls the add-on.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
U=cua
UHOME=/home/$U
BLENDER_URL=https://download.blender.org/release/Blender5.2/blender-5.2.2-linux-x64.tar.xz
EXT=$UHOME/.config/blender/5.2/extensions
SITE=$EXT/.local/lib/python3.13/site-packages
MU=$EXT/user_default/multi_user

as_user() { runuser -u "$U" -- env HOME="$UHOME" DISPLAY=:1 "$@"; }

echo "== system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl xz-utils libxi6 libxxf86vm1 libxfixes3 libxrender1 \
  libxkbcommon0 libxkbcommon-x11-0 libsm6 libice6 libgl1 libegl1 libglu1-mesa libxrandr2 \
  libxinerama1 libxcursor1 libwayland-client0 libdbus-1-3 >/dev/null

echo "== Blender 5.2.2"
if [ ! -x /opt/blender/blender ]; then
  mkdir -p /opt/blender
  curl -fsSL "$BLENDER_URL" | tar -xJ --strip-components=1 -C /opt/blender
fi
ln -sf /opt/blender/blender /usr/local/bin/blender
blender --version | head -1

echo "== Multi-User 0.8.2+mu4 from addon/"
if [ -d "$EXT/blender_org/multi_user" ]; then
  echo "The upstream Multi-User is installed in $EXT/blender_org. Remove it first:" >&2
  echo "  rm -rf $EXT/blender_org/multi_user $SITE/replication*" >&2
  exit 1
fi
PY=$(ls /opt/blender/5.2/python/bin/python3.* | head -1)
ZIP=$("$PY" "$HERE/build.py")
cp "$ZIP" /tmp/multi_user.zip && chmod 644 /tmp/multi_user.zip
as_user blender -b -c extension install-file -r user_default -e /tmp/multi_user.zip
test -f "$SITE/replication/topo3.py" || { echo "patched replication missing in $SITE" >&2; exit 1; }

echo "== test scripts"
mkdir -p "$UHOME/mu" "$UHOME/stress"
cp "$HERE/patch/v3/gui_session.py" "$HERE/tests/live_session.py" "$UHOME/mu/"
cp "$HERE/patch/v3/stress.py" "$HERE/patch/v3/run.sh" "$HERE/patch/v3/check.py" \
   "$HERE/patch/v3/check_topo.py" "$HERE/patch/v3/owners.py" "$UHOME/stress/"
chown -R "$U:$U" "$UHOME/mu" "$UHOME/stress"

echo "== check"
grep -q "'mu_vid'" "$MU/bl_types/bl_mesh.py"
as_user blender -b --python-expr "import addon_utils; m = [x for x in addon_utils.modules() if x.__name__.endswith('multi_user')]; print('MU_CHECK', [(x.__name__, addon_utils.check(x.__name__)) for x in m])
from replication import merge3, topo3; print('MU_CHECK merge3', merge3.ENABLED, 'topo3', topo3.ENABLED)" 2>&1 | grep MU_CHECK
echo "Done. Multi-User 0.8.2+mu4 is installed for $U."
