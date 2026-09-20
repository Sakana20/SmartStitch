#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG_NAME="${1:?usage: build_app.sh vX.Y.Z}"
VERSION="${TAG_NAME#v}"
PROJECT_VERSION="$(python -c 'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
PACKAGE_VERSION="$(python -c 'from smartstitch import __version__; print(__version__)')"

if [[ "${TAG_NAME}" != "v${PROJECT_VERSION}" ]]; then
  echo "Tag ${TAG_NAME} does not match pyproject.toml version ${PROJECT_VERSION}." >&2
  exit 1
fi
if [[ "${PROJECT_VERSION}" != "${PACKAGE_VERSION}" ]]; then
  echo "pyproject.toml (${PROJECT_VERSION}) and smartstitch.__version__ (${PACKAGE_VERSION}) differ." >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "SmartStitch.app must be built on an arm64 runner." >&2
  exit 1
fi

cd "${PROJECT_ROOT}"
export SMARTSTITCH_BUILD_VERSION="${VERSION}"
export SMARTSTITCH_FFMPEG_PREFIX="${SMARTSTITCH_FFMPEG_PREFIX:-${PROJECT_ROOT}/build/ffmpeg-arm64}"
export PYINSTALLER_CONFIG_DIR="${PROJECT_ROOT}/build/pyinstaller-cache"
python -m PyInstaller --clean --noconfirm packaging/SmartStitch.spec

APP="${PROJECT_ROOT}/dist/SmartStitch.app"
test -d "${APP}"
file "${APP}/Contents/MacOS/SmartStitch" | grep -q "arm64"
test -x "${APP}/Contents/Resources/bin/ffmpeg"
test -x "${APP}/Contents/Resources/bin/ffprobe"
ICON_FILE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "${APP}/Contents/Info.plist")"
test -n "${ICON_FILE}"
test -f "${APP}/Contents/Resources/${ICON_FILE}"
file "${APP}/Contents/Resources/${ICON_FILE}" | grep -q 'Mac OS X icon'

dependency_report="$(mktemp "${TMPDIR:-/tmp}/smartstitch-otool.XXXXXX")"
trap 'rm -f "${dependency_report}"' EXIT
while IFS= read -r -d '' bundled_file; do
  if file "${bundled_file}" | grep -q 'Mach-O'; then
    architectures="$(lipo -archs "${bundled_file}")"
    if [[ "${architectures}" != "arm64" ]]; then
      echo "Non-arm64 Mach-O in application: ${bundled_file} (${architectures})" >&2
      exit 1
    fi
    otool -L "${bundled_file}" >> "${dependency_report}"
  fi
done < <(find "${APP}" -type f -print0)
if grep -E '/opt/homebrew|/usr/local' "${dependency_report}"; then
  echo "Application unexpectedly depends on a Homebrew/local library." >&2
  exit 1
fi

while IFS= read -r -d '' bundled_file; do
  if file "${bundled_file}" | grep -q 'Mach-O'; then
    codesign --force --sign - --timestamp=none "${bundled_file}"
  fi
done < <(find "${APP}/Contents" -type f -print0)

# Sign nested code containers from the inside out before sealing the top-level app.
while IFS= read -r -d '' code_container; do
  codesign --force --sign - --timestamp=none "${code_container}"
done < <(
  find -d "${APP}/Contents" -type d \
    \( -name '*.framework' -o -name '*.bundle' -o -name '*.app' -o -name '*.xpc' \) \
    -print0
)

codesign --force \
  --sign - \
  --timestamp=none \
  --entitlements "${PROJECT_ROOT}/packaging/entitlements.plist" \
  "${APP}"

codesign --verify --strict --verbose=2 "${APP}/Contents/Resources/bin/ffmpeg"
codesign --verify --strict --verbose=2 "${APP}/Contents/Resources/bin/ffprobe"
codesign --verify --deep --strict --verbose=2 "${APP}"
for signed_target in \
  "${APP}" \
  "${APP}/Contents/Resources/bin/ffmpeg" \
  "${APP}/Contents/Resources/bin/ffprobe"; do
  signature_details="$(codesign -d --verbose=4 "${signed_target}" 2>&1)"
  grep -q '^Signature=adhoc$' <<< "${signature_details}" || {
    echo "Expected ad-hoc signature: ${signed_target}" >&2
    exit 1
  }
done
echo "Built ${APP}"
