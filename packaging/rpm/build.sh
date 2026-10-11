#!/usr/bin/env bash
# Build corewatch-<version>-1.<arch>.rpm into OUT_DIR (default: dist/).
#
# What it carries and how: see packaging/stage.sh. This adds the RPM metadata, with Requires
# naming the system libraries it links to by soname, as rpm's own dependency generator would, so
# any distribution's packages can satisfy them. That generator is off: it would also advertise
# the bundled Qt and Python to the whole system. The Python is compiled here, with the paths it
# will have, so rpm owns the bytecode and the package needs no scripts.
#
# Needs uv, rpmbuild, ldd and readelf (binutils). Usage: packaging/rpm/build.sh [OUT_DIR]
set -euo pipefail

command -v rpmbuild > /dev/null || { echo "build.sh needs rpmbuild" >&2; exit 1; }
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)
ARCH=$(uname -m)
source "$ROOT/packaging/stage.sh"

echo "Compiling the Python as /opt/corewatch"
# -B: Python writes none for its own imports, with the build's paths, before compileall reaches them.
# checked-hash: valid as long as the source is unchanged, whatever mtimes the install gives it.
"$PY" -I -B -m compileall -q -j 0 --invalidation-mode checked-hash -s "$STAGE" -p / "$APP/python/lib"
if leftover=$(grep -rl "$WORK" "$APP"); then
    printf 'Files still naming the build directory:\n%s\n' "$leftover" >&2
    exit 1
fi

# The system libraries they link to directly, as rpm names what a library provides.
BITS=$([ "$(getconf LONG_BIT)" = 64 ] && echo '(64bit)' || true)
REQUIRES=()
for soname in "${NEEDED[@]}"; do
    [ -n "${BUNDLED[${soname##*/}]:-}" ] && continue  # bundled; some are named $ORIGIN/...
    REQUIRES+=("Requires: $soname()$BITS")
done

install -Dm644 "$ROOT/packaging/deb/copyright" "$STAGE/usr/share/licenses/corewatch/copyright"

cat > "$WORK/corewatch.spec" << EOF
Name: corewatch
Version: $VERSION
Release: 1
Summary: Core Temp-style hardware monitor for Linux
License: MIT AND PSF-2.0 AND BSD-2-Clause AND BSD-3-Clause AND LGPL-3.0-only AND OFL-1.1
URL: https://github.com/ronaldorcampos/corewatch
Packager: Ronaldo Campos <ronaldo.romanello@gmail.com>
AutoReqProv: no
Requires: /bin/sh
$(printf '%s\n' "${REQUIRES[@]}")
Recommends: hwdata

# Ships as staged: no stripping, bytecode compiling with the system's Python, debuginfo or build-id links.
%global __os_install_post %{nil}
%global debug_package %{nil}
%define _build_id_links none
%define _binary_payload w6.xzdio
%define source_date_epoch_from_changelog 0

%description
Temperatures, fan speeds, voltages, clocks, load and power in real time, from
the kernel's hwmon, RAPL and the GPU drivers, in a desktop window, a terminal
view or as JSON. Carries its own Python and Qt under /opt/corewatch. With
hwdata installed, AMD and Intel graphics show their model names.

%install
cp -a "$STAGE/." %{buildroot}/

%files
/opt/corewatch
/usr/bin/corewatch
/usr/share/applications/corewatch.desktop
/usr/share/icons/hicolor/scalable/apps/corewatch.svg
/usr/share/icons/hicolor/symbolic/apps/corewatch-symbolic.svg
/usr/share/corewatch
%dir /usr/share/licenses/corewatch
%license /usr/share/licenses/corewatch/copyright
EOF

rpmbuild -bb --quiet --target "$ARCH" --define "_topdir $WORK/rpmbuild" --define "_rpmdir $OUT" \
    --define '_build_name_fmt %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm' "$WORK/corewatch.spec"
RPM=$OUT/corewatch-$VERSION-1.$ARCH.rpm
echo "Built $RPM ($(du -h "$RPM" | cut -f1), $(( $(du -sk "$STAGE" | cut -f1) / 1024 )) MB installed)"
