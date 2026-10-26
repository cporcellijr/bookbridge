#!/usr/bin/env bash
# Publish plugins/bridgesync.koplugin to its plugin-store repo when its version is new.
#
# KOReader plugin stores (Storefront) offer an update when a repo's release tag is
# newer than the installed _meta.lua version, so BridgeSync lives in its own repo
# tagged v<plugin version>. That repo's workflow builds the zip and the release.
set -euo pipefail

MIRROR_URL="${MIRROR_URL:-git@github.com:cporcellijr/bridgesync.koplugin.git}"
SOURCE_TAG="${SOURCE_TAG:-${GITHUB_REF_NAME:-local}}"

root="$(cd "$(dirname "$0")/.." && pwd)"
plugin="$root/plugins/bridgesync.koplugin"
version="$(sed -n 's/.*version = "\(.*\)".*/\1/p' "$plugin/_meta.lua")"
if [ -z "$version" ]; then
  echo "No version found in $plugin/_meta.lua"
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone -q "$MIRROR_URL" "$work/mirror"
cd "$work/mirror"

if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  echo "BridgeSync v$version is already published"
  exit 0
fi

rm -rf bridgesync.koplugin
cp -R "$plugin" bridgesync.koplugin
git add -A
git -c user.name="github-actions[bot]" \
    -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    commit -q --allow-empty -m "BridgeSync $version (from BookBridge $SOURCE_TAG)"
git tag "v$version"
git push -q origin HEAD "refs/tags/v$version"
echo "Published BridgeSync v$version"
