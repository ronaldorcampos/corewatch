#!/usr/bin/env bash
# Build a signed apt repository holding the .debs in DEBS_DIR, into OUT_DIR, for GitHub Pages:
# dists/stable/{InRelease,Release,Release.gpg}, the Packages index for each architecture, the .debs
# under pool/, and corewatch.gpg, the public key apt checks it with. It holds only the .debs it's
# given, the latest release's, so the site stays well under Pages' 1 GB.
#
# Signs with, and publishes, the first secret key in gpg's keyring (CI imports only the one). Needs apt-ftparchive (apt-utils) and gpg.
# Usage: packaging/deb/apt-repo.sh DEBS_DIR OUT_DIR
set -euo pipefail

for tool in apt-ftparchive gpg; do
    command -v "$tool" > /dev/null || { echo "apt-repo.sh needs $tool" >&2; exit 1; }
done
DEBS=$1
OUT=$2
ARCHES=(amd64 arm64)
shopt -s nullglob
debs=("$DEBS"/corewatch_*.deb)
[ "${#debs[@]}" -gt 0 ] || { echo "No corewatch .debs in $DEBS" >&2; exit 1; }

KEY=$(gpg --batch --list-secret-keys --with-colons | awk -F: '$1 == "fpr" { print $10; exit }')
[ -n "$KEY" ] || { echo "gpg has no secret key to sign with" >&2; exit 1; }

rm -rf "${OUT:?}" && mkdir -p "$OUT/pool/main"
cp "${debs[@]}" "$OUT/pool/main/"
cd "$OUT"
for arch in "${ARCHES[@]}"; do
    mkdir -p "dists/stable/main/binary-$arch"
    apt-ftparchive --arch "$arch" packages pool > "dists/stable/main/binary-$arch/Packages"
    gzip -9kn "dists/stable/main/binary-$arch/Packages"
done
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=corewatch -o APT::FTPArchive::Release::Label=corewatch \
    -o APT::FTPArchive::Release::Suite=stable -o APT::FTPArchive::Release::Codename=stable \
    -o APT::FTPArchive::Release::Architectures="${ARCHES[*]}" -o APT::FTPArchive::Release::Components=main \
    release dists/stable > Release.tmp  # outside dists/stable, so it doesn't list itself
mv Release.tmp dists/stable/Release
gpg --batch --yes --local-user "$KEY" --clearsign -o dists/stable/InRelease dists/stable/Release
gpg --batch --yes --local-user "$KEY" --armor --detach-sign -o dists/stable/Release.gpg dists/stable/Release
gpg --batch --yes --export -o corewatch.gpg "$KEY"
echo "apt repository in $OUT: $(printf '%s ' pool/main/*.deb)"
