#!/bin/sh
# Builds dist/markdown-editor-<version>-x86_64.AppImage.
# appimagetool and the AppImage runtime are pinned releases, checked by sha256.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP_ID=io.github._24icewolf42.markdown-editor
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$ROOT/markdown_editor/__init__.py")"
ARCH=x86_64
BUILD="$ROOT/build/appimage"
APPDIR="$BUILD/AppDir"
OUT="$ROOT/dist/markdown-editor-$VERSION-$ARCH.AppImage"

TOOL_URL=https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
TOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
RUNTIME_URL=https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64
RUNTIME_SHA256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d

fetch() { # url sha256 destination
    if [ ! -f "$3" ] || ! echo "$2  $3" | sha256sum -c --status; then
        curl -fsSL -o "$3.part" "$1"
        echo "$2  $3.part" | sha256sum -c --quiet
        mv "$3.part" "$3"
    fi
}

mkdir -p "$BUILD" "$ROOT/dist"
fetch "$TOOL_URL" "$TOOL_SHA256" "$BUILD/appimagetool"
fetch "$RUNTIME_URL" "$RUNTIME_SHA256" "$BUILD/runtime"
chmod +x "$BUILD/appimagetool"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/markdown-editor" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo"
(cd "$ROOT" && git ls-files -z markdown_editor | xargs -0 cp --parents -t "$APPDIR/usr/share/markdown-editor")
cp -r "$ROOT/markdown_editor/data/icons" "$APPDIR/usr/share/"
cp "$ROOT/packaging/appimage/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$ROOT/data/$APP_ID.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/data/$APP_ID.desktop" "$APPDIR/"
cp "$ROOT/data/$APP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/$APP_ID.appdata.xml"
cp "$ROOT/markdown_editor/data/icons/hicolor/256x256/apps/$APP_ID.png" "$APPDIR/$APP_ID.png"
cp "$APPDIR/$APP_ID.png" "$APPDIR/.DirIcon"

# appimagetool is itself an AppImage; extract-and-run avoids needing FUSE.
# Update information lets AppImageUpdate and Gear Lever fetch new releases
# (delta downloads via the .zsync file, which needs zsyncmake at build time).
UPDATE_INFO="gh-releases-zsync|24icewolf42|markdown-editor|latest|markdown-editor-*-$ARCH.AppImage.zsync"
command -v zsyncmake >/dev/null || echo "zsyncmake not found: no .zsync file is generated" >&2
(cd "$ROOT/dist" && APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$BUILD/appimagetool" --no-appstream \
    --runtime-file "$BUILD/runtime" --updateinformation "$UPDATE_INFO" "$APPDIR" "$OUT")
echo "$OUT"
