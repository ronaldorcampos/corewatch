#!/usr/bin/env bash
# Build corewatch_<version>_<arch>.deb into OUT_DIR (default: dist/).
#
# Ubuntu's own PySide6 and Textual are older than corewatch needs, so the package carries
# everything: a standalone Python (uv's python-build-standalone, which runs from any path) under
# /opt/corewatch/python, with corewatch and its locked dependencies installed into it. Nothing in
# it depends on the system's python3, so a distribution upgrade can't break it. Qt's parts that
# corewatch never loads (QML, Quick, the designer tools, SQL drivers) are left out, and the build
# fails if anything left needs a library that's neither in the package nor in its Depends.
#
# Needs uv, dpkg-deb and ldd. Usage: packaging/deb/build.sh [OUT_DIR]
set -euo pipefail
export UV_LINK_MODE=copy  # the stage is on another filesystem than uv's cache, often
umask 022  # the package's directories are 755 whatever the shell's umask

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)
PYTHON_VERSION=3.13
VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/pyproject.toml")
ARCH=$(dpkg --print-architecture)
WORK=$(mktemp -d)
trap 'rm -rf "${WORK:?}"' EXIT
STAGE=$WORK/root
APP=$STAGE/opt/corewatch
PY=$APP/python/bin/python3
# Libraries Qt's X11 platform needs from the system that a build machine may not have installed
# (its docs list them), by the package that provides each: added to Depends as they are.
declare -A SYSTEM_LIBS=(
    [libxcb-cursor.so.0]=libxcb-cursor0
    [libxcb-icccm.so.4]=libxcb-icccm4
    [libxcb-image.so.0]=libxcb-image0
    [libxcb-keysyms.so.1]=libxcb-keysyms1
    [libxcb-render-util.so.0]=libxcb-render-util0
)

echo "corewatch $VERSION ($ARCH): Python $PYTHON_VERSION"
uv python install --install-dir "$WORK/python" "$PYTHON_VERSION"
mkdir -p "$APP"
cp -a "$WORK"/python/cpython-"$PYTHON_VERSION".*/ "$APP/python"
# Python's own test suite, IDLE and Tk are of no use to a Qt app.
SITE=$("$PY" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
STDLIB=$(dirname "$SITE")
rm -rf "${STDLIB:?}"/{test,idlelib,tkinter,turtledemo} "$APP"/python/lib/{tcl,tk,itcl,thread}*

echo "Dependencies from uv.lock, then corewatch"
uv export --project "$ROOT" --frozen --no-dev --no-emit-project --format requirements-txt -o "$WORK/requirements.txt" > /dev/null
uv pip install --python "$PY" --break-system-packages --require-hashes -r "$WORK/requirements.txt"
uv build --project "$ROOT" --wheel -o "$WORK/wheel" > /dev/null
uv pip install --python "$PY" --break-system-packages --no-deps "$WORK"/wheel/corewatch-*.whl

echo "Leaving out the Qt that corewatch doesn't use"
QT=$SITE/PySide6
rm -rf "${QT:?}"/{qml,include,typesystems,glue,scripts,doc,metatypes} "$QT"/Qt/{qml,metatypes,libexec}
rm -rf "$QT"/Qt/plugins/{designer,printsupport,qmltooling,sqldrivers,egldeviceintegrations,wayland-graphics-integration-server,vectorimageformats}
find "$QT" -maxdepth 1 -type f -executable ! -name '*.so*' -delete  # designer, linguist, qmlls...
find "$QT" -maxdepth 1 -name '*.pyi' -delete
for module in Designer Help Labs Lottie PrintSupport Qml Quick Sql Test UiTools WaylandCompositor WaylandEglCompositor EglFS EglFs; do
    rm -f "$QT"/Qt/lib/libQt6"$module"*.so* "$QT"/Qt"$module"*.abi3.so
done
rm -f "$QT"/libpyside6qml.* "$QT"/Qt/plugins/platforms/libqeglfs.so "$QT"/Qt/plugins/imageformats/libqpdf.so \
    "$QT"/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +  # compiled at its real path on install

echo "Checking every library left can find what it links to"
mapfile -t LIBS < <(find "$APP" -type f \( -name '*.so' -o -name '*.so.*' \) )
missing=$(for lib in "${LIBS[@]}"; do
    LD_LIBRARY_PATH=$QT/Qt/lib:$APP/python/lib ldd "$lib" 2>/dev/null | sed -n "s|^\s*\(\S*\) => not found|${lib#"$APP"/}: \1|p"
done | while IFS= read -r line; do [ -n "${SYSTEM_LIBS[${line##*: }]:-}" ] || printf '%s\n' "$line"; done)
if [ -n "$missing" ]; then
    printf 'Libraries that would be missing at run time:\n%s\n' "$missing" >&2
    exit 1
fi
# The Depends line: the system libraries they link to directly (not what those link to in turn,
# which their own packages depend on), by the packages that hold them on this machine.
declare -A BUNDLED=()
for lib in "${LIBS[@]}"; do BUNDLED[$(basename "$lib")]=1; done
mapfile -t NEEDED < <(for lib in "${LIBS[@]}"; do readelf -d "$lib" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'; done | sort -u)
PACKAGES=("${SYSTEM_LIBS[@]}")
for soname in "${NEEDED[@]}"; do
    [ -n "${BUNDLED[${soname##*/}]:-}" ] || [ -n "${SYSTEM_LIBS[$soname]:-}" ] && continue  # some name $ORIGIN/...
    path=$(ldconfig -p | awk -v name="$soname" '$1 == name { print $NF; exit }')
    if [ -z "$path" ] || ! owner=$(dpkg -S "$(readlink -f "$path")" 2> /dev/null || dpkg -S "$path" 2> /dev/null); then
        echo "No package found for $soname" >&2
        exit 1
    fi
    PACKAGES+=("${owner%%:*}")
done
DEPENDS=$(printf '%s\n' "${PACKAGES[@]}" | sort -u | paste -sd, | sed 's/,/, /g')

echo "Desktop entry, icons, command and docs"
install -Dm755 /dev/stdin "$STAGE/usr/bin/corewatch" << 'EOF'
#!/bin/sh
exec /opt/corewatch/python/bin/python3 -m corewatch "$@"
EOF
install -Dm644 "$ROOT/packaging/corewatch.desktop" "$STAGE/usr/share/applications/corewatch.desktop"
install -Dm644 "$ROOT/src/corewatch/assets/corewatch.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/corewatch.svg"
install -Dm644 "$ROOT/src/corewatch/assets/corewatch-symbolic.svg" \
    "$STAGE/usr/share/icons/hicolor/symbolic/apps/corewatch-symbolic.svg"
# Shipped off: making CPU power readable to every user is a choice (see the README's RAPL section).
install -Dm644 "$ROOT/packaging/99-corewatch-rapl.rules" "$STAGE/usr/share/corewatch/99-corewatch-rapl.rules"
install -Dm644 "$ROOT/packaging/deb/copyright" "$STAGE/usr/share/doc/corewatch/copyright"

mkdir -p "$STAGE/DEBIAN"
install -m755 "$ROOT/packaging/deb/postinst" "$ROOT/packaging/deb/prerm" "$STAGE/DEBIAN/"
cat > "$STAGE/DEBIAN/control" << EOF
Package: corewatch
Version: $VERSION
Architecture: $ARCH
Maintainer: Ronaldo Campos <ronaldo.romanello@gmail.com>
Installed-Size: $(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
Depends: $DEPENDS
Section: utils
Priority: optional
Homepage: https://github.com/ronaldorcampos/corewatch
Description: Core Temp-style hardware monitor for Linux
 Temperatures, fan speeds, voltages, clocks, load and power in real time, from
 the kernel's hwmon, RAPL and the GPU drivers, in a desktop window, a terminal
 view or as JSON. Carries its own Python and Qt under /opt/corewatch.
EOF

DEB=$OUT/corewatch_${VERSION}_${ARCH}.deb
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" > /dev/null
echo "Built $DEB ($(du -h "$DEB" | cut -f1), $(( $(du -sk --exclude=DEBIAN "$STAGE" | cut -f1) / 1024 )) MB installed)"
