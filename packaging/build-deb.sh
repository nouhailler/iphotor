#!/bin/sh
# Construit le paquet Debian d'iphotor : ./packaging/build-deb.sh
# Résultat : dist/iphotor_<version>_all.deb
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/iphotor.py")
[ -n "$VERSION" ] || { echo "Version introuvable dans iphotor.py" >&2; exit 1; }

PKG=iphotor_${VERSION}_all
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
D="$BUILD/$PKG"

install -Dm755 "$ROOT/iphotor.py"          "$D/usr/bin/iphotor"
install -Dm644 "$HERE/iphotor.desktop"     "$D/usr/share/applications/iphotor.desktop"
install -Dm644 "$HERE/iphotor.svg"         "$D/usr/share/icons/hicolor/scalable/apps/iphotor.svg"
install -Dm644 "$ROOT/README.md"           "$D/usr/share/doc/iphotor/README.md"
install -Dm644 "$ROOT/CONTEXT.md"          "$D/usr/share/doc/iphotor/CONTEXT.md"
gzip -9n -c "$ROOT/CHANGELOG.md" > "$BUILD/changelog.gz"
install -Dm644 "$BUILD/changelog.gz"       "$D/usr/share/doc/iphotor/changelog.gz"
cat > "$BUILD/copyright" <<COPY
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: iphotor

Files: *
Copyright: 2026 Contributeurs d'iphotor
License: none
 Aucune licence n'est encore précisée par l'auteur.
COPY
install -Dm644 "$BUILD/copyright"          "$D/usr/share/doc/iphotor/copyright"

SIZE=$(du -sk "$D/usr" | cut -f1)
mkdir -p "$D/DEBIAN"
cat > "$D/DEBIAN/control" <<CTRL
Package: iphotor
Version: $VERSION
Section: graphics
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-tk, python3-pil, python3-pil.imagetk
Recommends: xdg-utils, python3-gi, gir1.2-gdkpixbuf-2.0, heif-gdk-pixbuf
Installed-Size: $SIZE
Maintainer: Mainteneurs d'iphotor <iphotor@users.noreply.github.com>
Description: explorateur en lecture seule de bibliothèques iPhoto et Photos
 iphotor permet de parcourir sous Linux une ancienne bibliothèque iPhoto
 ou Photos 1 à 4 (macOS) en conservant son organisation : albums, dossiers,
 événements, légendes, commentaires et notes. Les photos HEIC et l'aperçu
 intégré des fichiers RAW (CR2, NEF, DNG…) sont affichés.
 .
 Le mode « Explorer un dossier » affiche aussi les images (JPG, PNG, GIF,
 BMP, TIFF) de n'importe quel dossier, sous-dossiers compris.
 .
 La bibliothèque n'est jamais modifiée : l'index et les miniatures sont
 stockés dans ~/.cache/photoexplorer_iphoto/.
CTRL
cat > "$D/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
fi
exit 0
POST
chmod 755 "$D/DEBIAN/postinst"
cp "$D/DEBIAN/postinst" "$D/DEBIAN/postrm"

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group -Zxz --build "$D" "$ROOT/dist/$PKG.deb"
echo "Paquet créé : dist/$PKG.deb"
