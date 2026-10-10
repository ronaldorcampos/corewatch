#!/usr/bin/env bash
# Build corewatch_<version>_<arch>.deb into OUT_DIR (default: dist/).
#
# Ubuntu's own PySide6 and Textual are older than corewatch needs, so the package carries
# everything: a standalone Python (uv's python-build-standalone, which runs from any path) under
# /opt/corewatch/python, with corewatch and its locked dependencies installed into it. It runs
# isolated (python3 -I), so nothing of the system's Python, the user's or the current directory's
# can stand in for what it carries. Qt's parts that corewatch never loads (QML, Quick, printing,
# the designer tools, SQL drivers) are left out, and the build fails if anything left needs a
# library that's neither in the package nor in its Depends.
#
# Needs uv, dpkg-deb, ldd and readelf (binutils). Usage: packaging/deb/build.sh [OUT_DIR]
set -euo pipefail
export UV_LINK_MODE=copy  # the stage is on another filesystem than uv's cache, often
umask 022  # the package's directories are 755 whatever the shell's umask

for tool in uv dpkg-deb ldd readelf; do
    command -v "$tool" > /dev/null || { echo "build.sh needs $tool" >&2; exit 1; }
done
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)
PYTHON_VERSION=3.13
# A PEP 440 pre-release (0.2.0rc1) becomes 0.2.0~rc1, which dpkg sorts before 0.2.0.
VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/pyproject.toml" | sed -E 's/([0-9])(a|b|rc)([0-9])/\1~\2\3/')
ARCH=$(dpkg --print-architecture)
WORK=$(mktemp -d)
trap 'rm -rf "${WORK:?}"' EXIT
STAGE=$WORK/root
APP=$STAGE/opt/corewatch
PY=$APP/python/bin/python3

echo "corewatch $VERSION ($ARCH): Python $PYTHON_VERSION"
# --no-bin: no python3.13 link in ~/.local/bin pointing into this soon-deleted directory.
uv python install --no-bin --install-dir "$WORK/python" "$PYTHON_VERSION"
mkdir -p "$APP"
INSTALLED=$(cd "$WORK"/python/cpython-"$PYTHON_VERSION".*/ && pwd)
cp -a "$INSTALLED/" "$APP/python"
SITE=$("$PY" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
STDLIB=$(dirname "$SITE")
# uv points Python's record of its own build at where it installed it; point it where it goes.
sed -i "s|$INSTALLED|/opt/corewatch/python|g" "$STDLIB"/_sysconfigdata_*.py
# Of no use to a Qt app: Python's test suite, IDLE, Tk, the headers for building extensions, and
# the shared libpython (python3 itself doesn't link to it; only libpython3.so, its stub, does).
rm -rf "${STDLIB:?}"/{test,idlelib,tkinter,turtledemo} "$STDLIB"/lib-dynload/_tkinter*.so \
    "$APP"/python/lib/{tcl,tk,itcl,thread}* "$APP"/python/lib/lib{tcl,tk}* \
    "$APP"/python/lib/libpython3*.so* "$APP"/python/include

echo "Dependencies from uv.lock, then corewatch"
uv export --project "$ROOT" --locked --no-dev --no-emit-project --format requirements-txt -o "$WORK/requirements.txt" > /dev/null
uv pip install --python "$PY" --break-system-packages --require-hashes -r "$WORK/requirements.txt"
uv build --project "$ROOT" --wheel -o "$WORK/wheel" > /dev/null
uv pip install --python "$PY" --break-system-packages --no-deps "$WORK"/wheel/corewatch-*.whl
# The console scripts those installs wrote start with this build's temporary path, and pip
# would only offer to change what the package carries: /usr/bin/corewatch is the way in.
find "$APP/python/bin" \( -type f -o -type l \) ! -name 'python3' ! -name 'python3.[0-9]*' -delete
rm -f "$APP"/python/bin/python3*-config  # for building extensions, whose headers are gone
rm -rf "${SITE:?}"/pip "$SITE"/pip-*.dist-info
rm -f "$SITE"/*.dist-info/direct_url.json  # the build's path again

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
if leftover=$(grep -rIl "$WORK" "$APP"); then
    printf 'Files still naming the build directory:\n%s\n' "$leftover" >&2
    exit 1
fi

echo "Checking every library left can find what it links to"
mapfile -t LIBS < <(find "$APP" -type f \( -name '*.so' -o -name '*.so.*' \) )
# Each as it will be loaded: by its own RPATH and the system's paths, nothing added.
missing=$(for lib in "${LIBS[@]}"; do
    if ! out=$(ldd "$lib" 2>&1); then
        printf '%s: ldd failed: %s\n' "${lib#"$APP"/}" "$out"
        continue
    fi
    printf '%s\n' "$out" | sed -n "s|^\s*\(\S*\) => not found|${lib#"$APP"/}: \1|p"
done)
if [ -n "$missing" ]; then
    printf 'Libraries that would be missing at run time:\n%s\n' "$missing" >&2
    exit 1
fi
# The Depends line: the system libraries they link to directly (not what those link to in turn,
# which their own packages depend on), by the packages that hold them on this machine.
declare -A BUNDLED=()
for lib in "${LIBS[@]}"; do BUNDLED[$(basename "$lib")]=1; done
NEEDED=()
for lib in "${LIBS[@]}"; do
    dynamic=$(readelf -d "$lib")
    mapfile -t -O "${#NEEDED[@]}" NEEDED < <(printf '%s\n' "$dynamic" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p')
done
mapfile -t NEEDED < <(printf '%s\n' "${NEEDED[@]}" | sort -u)
[ "${#NEEDED[@]}" -gt 0 ] || { echo "readelf found no linked libraries at all" >&2; exit 1; }
PACKAGES=()
for soname in "${NEEDED[@]}"; do
    [ -n "${BUNDLED[${soname##*/}]:-}" ] && continue  # bundled; some are named $ORIGIN/...
    path=$(ldconfig -p | awk -v name="$soname" '$1 == name { print $NF; exit }')
    if [ -z "$path" ] || ! owner=$(dpkg -S "$(readlink -f "$path")" 2> /dev/null || dpkg -S "$path" 2> /dev/null); then
        echo "No package found for $soname" >&2
        exit 1
    fi
    PACKAGES+=("${owner%%:*}")
done
DEPENDS=$(printf '%s\n' "${PACKAGES[@]}" | sort -u | paste -sd, | sed 's/,/, /g')

echo "Desktop entry, icons, command and docs"
# -I: isolated, so the current directory, PYTHONPATH and the user's site-packages can't put
# their own modules ahead of the package's.
install -Dm755 /dev/stdin "$STAGE/usr/bin/corewatch" << 'EOF'
#!/bin/sh
exec /opt/corewatch/python/bin/python3 -I -m corewatch "$@"
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
Recommends: pci.ids | hwdata
Section: utils
Priority: optional
Homepage: https://github.com/ronaldorcampos/corewatch
Description: Core Temp-style hardware monitor for Linux
 Temperatures, fan speeds, voltages, clocks, load and power in real time, from
 the kernel's hwmon, RAPL and the GPU drivers, in a desktop window, a terminal
 view or as JSON. Carries its own Python and Qt under /opt/corewatch. With
 pci.ids or hwdata installed, AMD and Intel graphics show their model names.
EOF

DEB=$OUT/corewatch_${VERSION}_${ARCH}.deb
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" > /dev/null
echo "Built $DEB ($(du -h "$DEB" | cut -f1), $(( $(du -sk --exclude=DEBIAN "$STAGE" | cut -f1) / 1024 )) MB installed)"
