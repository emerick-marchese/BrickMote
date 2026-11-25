#!/usr/bin/env bash
# Build a Debian/Ubuntu package from the PyInstaller folder build.
#   BRICKMOTE_ONEDIR=1 pyinstaller --noconfirm packaging/brickmote.spec
#   packaging/linux/build-deb.sh 1.0.0 out/BrickMote-Linux-x64.deb
set -euo pipefail
VERSION="$1"
OUTPUT="$2"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/dist/BrickMote"
[ -x "$SRC/BrickMote" ] || { echo "missing $SRC/BrickMote (build with BRICKMOTE_ONEDIR=1)"; exit 1; }
PKG="$(mktemp -d)/brickmote_${VERSION}_amd64"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/brickmote" "$PKG/usr/bin" \
         "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/512x512/apps"
cp -a "$SRC/." "$PKG/opt/brickmote/"
ln -s /opt/brickmote/BrickMote "$PKG/usr/bin/brickmote"
cp "$ROOT/packaging/brickmote.png" "$PKG/usr/share/icons/hicolor/512x512/apps/brickmote.png"
cat > "$PKG/usr/share/applications/brickmote.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=BrickMote
GenericName=Bureau à distance
Comment=Bureau à distance chiffré de bout en bout
Exec=/opt/brickmote/BrickMote
Icon=brickmote
Terminal=false
Categories=Network;RemoteAccess;
StartupWMClass=BrickMote
DESKTOP
SIZE_KB=$(du -sk "$PKG/opt" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: brickmote
Version: ${VERSION}
Section: net
Priority: optional
Architecture: amd64
Installed-Size: ${SIZE_KB}
Depends: libegl1, libgl1, libxkbcommon-x11-0, libxcb-cursor0, libfontconfig1, libdbus-1-3
Maintainer: BrickMote <noreply@brickmote.invalid>
Homepage: https://github.com/emerick-marchese/BrickMote
Description: Bureau à distance chiffré de bout en bout
 BrickMote permet de contrôler un ordinateur à distance (écran, souris,
 clavier, presse-papiers, fichiers, chat, Wake-on-LAN). Chiffrement de bout
 en bout X25519 + Ed25519 + AES-256-GCM, mot de passe protégé par SPAKE2,
 double authentification TOTP. Ports : TCP 6568/443/80 (relais), TCP 7070
 (direct), UDP 50001 (découverte).
CONTROL
chmod 0755 "$PKG/DEBIAN"
mkdir -p "$(dirname "$OUTPUT")"
dpkg-deb --build --root-owner-group "$PKG" "$OUTPUT"
echo "built $OUTPUT"
