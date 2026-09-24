#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG_NAME="${1:?usage: create_dmg.sh vX.Y.Z}"
APP="${PROJECT_ROOT}/dist/SmartStitch.app"
FFMPEG_PREFIX="${SMARTSTITCH_FFMPEG_PREFIX:-${PROJECT_ROOT}/build/ffmpeg-arm64}"
DMG="${PROJECT_ROOT}/dist/SmartStitch-${TAG_NAME}-macOS-arm64.dmg"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/smartstitch-dmg.XXXXXX")"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/smartstitch-layout.XXXXXX")"
WORK_DMG="${WORK}/layout.dmg"
MOUNT="${WORK}/mount"
mkdir "${MOUNT}"
mounted=0
cleanup() {
  if [[ "${mounted}" == 1 ]]; then hdiutil detach "${MOUNT}" -quiet || true; fi
  rm -rf "${STAGING}" "${WORK}"
}
trap cleanup EXIT

test -d "${APP}"
ditto "${APP}" "${STAGING}/SmartStitch.app"
ln -s /Applications "${STAGING}/Applications"
mkdir -p "${STAGING}/Open Source Licenses/sources"
cp "${PROJECT_ROOT}/THIRD_PARTY_NOTICES.md" \
  "${STAGING}/Open Source Licenses/README.md"
cp "${PROJECT_ROOT}/packaging/licenses/Real-ESRGAN-BSD-3-Clause.txt" \
  "${STAGING}/Open Source Licenses/"
cp "${PROJECT_ROOT}/packaging/build_ffmpeg.sh" \
  "${STAGING}/Open Source Licenses/"
cp "${PROJECT_ROOT}/packaging/pkg-config-x264" \
  "${STAGING}/Open Source Licenses/"
cp "${FFMPEG_PREFIX}/share/licenses/"* \
  "${STAGING}/Open Source Licenses/"
cp "${FFMPEG_PREFIX}/sources/"* \
  "${STAGING}/Open Source Licenses/sources/"
mkdir -p "${STAGING}/.background"
cp "${PROJECT_ROOT}/packaging/assets/dmg-background.png" \
  "${STAGING}/.background/background.png"

rm -f "${DMG}"
hdiutil create \
  -volname "SmartStitch ${TAG_NAME}" \
  -srcfolder "${STAGING}" \
  -format UDRW \
  -ov \
  "${WORK_DMG}"
hdiutil attach "${WORK_DMG}" -mountpoint "${MOUNT}" -nobrowse -quiet
mounted=1
osascript - "${MOUNT}" <<'APPLESCRIPT'
on run argv
  set mountPath to item 1 of argv
  tell application "Finder"
    set installerDisk to disk (POSIX file mountPath as alias)
    open installerDisk
    delay 2
    set installerWindow to container window of installerDisk
    set current view of installerWindow to icon view
    set toolbar visible of installerWindow to false
    set statusbar visible of installerWindow to false
    set bounds of installerWindow to {160, 120, 840, 590}
    set viewOptions to icon view options of installerWindow
    set arrangement of viewOptions to not arranged
    set icon size of viewOptions to 104
    set text size of viewOptions to 12
    set background picture of viewOptions to (POSIX file (mountPath & "/.background/background.png") as alias)
    set position of item "SmartStitch.app" of installerWindow to {170, 205}
    set position of item "Applications" of installerWindow to {490, 205}
    set position of item "Open Source Licenses" of installerWindow to {575, 65}
    close installerWindow
    open installerDisk
    delay 2
  end tell
end run
APPLESCRIPT
sync
hdiutil detach "${MOUNT}" -quiet
mounted=0
hdiutil convert "${WORK_DMG}" -format UDZO -o "${DMG}" -ov
hdiutil verify "${DMG}"
echo "Created ${DMG}"
