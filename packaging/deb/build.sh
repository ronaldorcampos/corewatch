#!/usr/bin/env bash
# Build corewatch_<version>_<arch>.deb into OUT_DIR (default: dist/).
#
# What it carries and how: see packaging/stage.sh. This adds the Debian metadata, with Depends
# naming the packages that hold the system libraries it links to.
#
# Needs uv, dpkg-deb, ldd and readelf (binutils). Usage: packaging/deb/build.sh [OUT_DIR]
set -euo pipefail

command -v dpkg-deb > /dev/null || { echo "build.sh needs dpkg-deb" >&2; exit 1; }
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)
ARCH=$(dpkg --print-architecture)
source "$ROOT/packaging/stage.sh"

# The Depends line: the system libraries they link to directly (not what those link to in turn,
# which their own packages depend on), by the packages that hold them on this machine.
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
