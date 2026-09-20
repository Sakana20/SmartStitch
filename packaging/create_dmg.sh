#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG_NAME="${1:?usage: create_dmg.sh vX.Y.Z}"
APP="${PROJECT_ROOT}/dist/SmartStitch.app"
FFMPEG_PREFIX="${SMARTSTITCH_FFMPEG_PREFIX:-${PROJECT_ROOT}/build/ffmpeg-arm64}"
DMG="${PROJECT_ROOT}/dist/SmartStitch-${TAG_NAME}-macOS-arm64.dmg"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/smartstitch-dmg.XXXXXX")"
trap 'rm -rf "${STAGING}"' EXIT

test -d "${APP}"
ditto "${APP}" "${STAGING}/SmartStitch.app"
ln -s /Applications "${STAGING}/Applications"
mkdir -p "${STAGING}/Open Source Licenses/sources"
cp "${PROJECT_ROOT}/THIRD_PARTY_NOTICES.md" \
  "${STAGING}/Open Source Licenses/README.md"
cp "${PROJECT_ROOT}/packaging/build_ffmpeg.sh" \
  "${STAGING}/Open Source Licenses/"
cp "${PROJECT_ROOT}/packaging/pkg-config-x264" \
  "${STAGING}/Open Source Licenses/"
cp "${FFMPEG_PREFIX}/share/licenses/"* \
  "${STAGING}/Open Source Licenses/"
cp "${FFMPEG_PREFIX}/sources/"* \
  "${STAGING}/Open Source Licenses/sources/"

rm -f "${DMG}"
hdiutil create \
  -volname "SmartStitch ${TAG_NAME}" \
  -srcfolder "${STAGING}" \
  -format UDZO \
  -ov \
  "${DMG}"
echo "Created ${DMG}"
