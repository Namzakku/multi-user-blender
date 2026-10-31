#!/bin/sh
# Build a Multi-User v3 Space from a fresh cua Space (image ghcr.io/trycua/linux:24.04).
# Copy this whole folder into the Space, then run as root:
#   sh /path/to/multiuser-merge-patch/setup.sh
# It installs Blender 5.2.2, the Multi-User 0.8.2 extension, the v2 patches and v3
# on top, for the user `cua`. Running it again skips what is already done.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
U=cua
UHOME=/home/$U
BLENDER_URL=https://download.blender.org/release/Blender5.2/blender-5.2.2-linux-x64.tar.xz
EXT=$UHOME/.config/blender/5.2/extensions
SITE=$EXT/.local/lib/python3.13/site-packages
MU=$EXT/blender_org/multi_user

as_user() { runuser -u "$U" -- env HOME="$UHOME" DISPLAY=:1 "$@"; }

echo "== system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl xz-utils patch libxi6 libxxf86vm1 libxfixes3 libxrender1 \
  libxkbcommon0 libxkbcommon-x11-0 libsm6 libice6 libgl1 libegl1 libglu1-mesa libxrandr2 \
  libxinerama1 libxcursor1 libwayland-client0 libdbus-1-3 >/dev/null

echo "== Blender 5.2.2"
if [ ! -x /opt/blender/blender ]; then
  mkdir -p /opt/blender
  curl -fsSL "$BLENDER_URL" | tar -xJ --strip-components=1 -C /opt/blender
fi
ln -sf /opt/blender/blender /usr/local/bin/blender
blender --version | head -1

echo "== Multi-User 0.8.2 from extensions.blender.org"
if [ ! -f "$MU/blender_manifest.toml" ]; then
  as_user blender --online-mode -b -c extension install -s -e multi_user
fi
VERSION=$(tr -d '\r' < "$MU/blender_manifest.toml" | sed -n 's/^version = "\(.*\)"/\1/p')
if [ "$VERSION" != "0.8.2" ]; then
  echo "Multi-User $VERSION is installed, but the patches target 0.8.2. Stopping." >&2
  exit 1
fi
test -f "$SITE/replication/porcelain.py" || { echo "replication wheel missing in $SITE" >&2; exit 1; }

echo "== patches (v2, then v3)"
if ! grep -q "_merge3" "$SITE/replication/porcelain.py"; then
  # The add-on and the replication wheel ship CRLF line endings; the patches are LF.
  for f in handlers.py timers.py bl_types/bl_mesh.py; do sed -i 's/\r$//' "$MU/$f"; done
  for f in porcelain.py repository.py interface.py net/__init__.py; do
    sed -i 's/\r$//' "$SITE/replication/$f"
  done
  find "$SITE/replication" "$MU" -name "*.rej" -delete
  for p in porcelain repository interface net; do
    patch -d "$SITE" -p1 --forward < "$HERE/patch/v2/$p.patch"
  done
  for p in handlers timers; do
    patch -d "$EXT/blender_org" -p1 --forward < "$HERE/patch/v2/$p.patch"
  done
  patch -d "$EXT/blender_org" -p1 --forward < "$HERE/patch/v3/bl_mesh.patch"
fi
cp "$HERE/patch/v3/merge3.py" "$HERE/patch/v3/topo3.py" "$SITE/replication/"
rm -rf "$SITE/replication/__pycache__" "$MU/__pycache__" "$MU/bl_types/__pycache__"

echo "== test scripts"
mkdir -p "$UHOME/mu" "$UHOME/stress"
cp "$HERE/patch/v3/gui_session.py" "$UHOME/mu/"
cp "$HERE/patch/v3/stress.py" "$HERE/patch/v3/run.sh" "$HERE/patch/v3/check.py" \
   "$HERE/patch/v3/check_topo.py" "$HERE/patch/v3/owners.py" "$UHOME/stress/"
chown -R "$U:$U" "$EXT" "$UHOME/mu" "$UHOME/stress"

echo "== check"
grep -q "'mu_vid'" "$MU/bl_types/bl_mesh.py"
as_user blender -b --python-expr "import addon_utils; m = [x for x in addon_utils.modules() if x.__name__.endswith('multi_user')]; print('MU_CHECK', [(x.__name__, addon_utils.check(x.__name__)) for x in m])
from replication import merge3, topo3; print('MU_CHECK merge3', merge3.ENABLED, 'topo3', topo3.ENABLED)" 2>&1 | grep MU_CHECK
echo "Done. Multi-User 0.8.2 with the v3 patch is installed for $U."
