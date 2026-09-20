#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "FFmpeg release binaries must be built natively on macOS arm64." >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${1:-${PROJECT_ROOT}/build/ffmpeg-arm64}"
if [[ "${PREFIX}" != /* ]]; then
  PREFIX="${PROJECT_ROOT}/${PREFIX}"
fi
case "${PREFIX}" in
  /|/Users|/Applications|/Library|/System) echo "Refusing unsafe prefix: ${PREFIX}" >&2; exit 1 ;;
esac

FFMPEG_VERSION="7.1.5"
FFMPEG_SHA256="de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f"
X264_COMMIT="b35605ace3ddf7c1a5d67a2eb553f034aef41d55"
X264_SHA256="6eeb82934e69fd51e043bd8c5b0d152839638d1ce7aa4eea65a3fedcf83ff224"

WORK_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/smartstitch-ffmpeg.XXXXXX")"
trap 'rm -rf "${WORK_DIRECTORY}"' EXIT

FFMPEG_ARCHIVE="${WORK_DIRECTORY}/ffmpeg-${FFMPEG_VERSION}.tar.xz"
X264_ARCHIVE="${WORK_DIRECTORY}/x264-${X264_COMMIT}.tar.bz2"

curl --fail --location --retry 3 --silent --show-error \
  "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
  --output "${FFMPEG_ARCHIVE}"
curl --fail --location --retry 3 --silent --show-error \
  "https://code.videolan.org/videolan/x264/-/archive/${X264_COMMIT}/x264-${X264_COMMIT}.tar.bz2" \
  --output "${X264_ARCHIVE}"

echo "${FFMPEG_SHA256}  ${FFMPEG_ARCHIVE}" | shasum -a 256 --check
echo "${X264_SHA256}  ${X264_ARCHIVE}" | shasum -a 256 --check

rm -rf "${PREFIX}"
mkdir -p "${PREFIX}" "${PREFIX}/sources" "${PREFIX}/share/licenses"
cp "${FFMPEG_ARCHIVE}" "${PREFIX}/sources/"
cp "${X264_ARCHIVE}" "${PREFIX}/sources/"

tar -xf "${X264_ARCHIVE}" -C "${WORK_DIRECTORY}"
X264_SOURCE="$(find "${WORK_DIRECTORY}" -maxdepth 1 -type d -name 'x264-*' -print -quit)"
(
  cd "${X264_SOURCE}"
  export MACOSX_DEPLOYMENT_TARGET=13.0
  ./configure \
    --prefix="${PREFIX}" \
    --host=aarch64-apple-darwin \
    --enable-static \
    --disable-cli \
    --disable-opencl
  make -j"$(sysctl -n hw.logicalcpu)"
  make install
)
cp "${X264_SOURCE}/COPYING" "${PREFIX}/share/licenses/x264-GPL-2.0.txt"

tar -xf "${FFMPEG_ARCHIVE}" -C "${WORK_DIRECTORY}"
FFMPEG_SOURCE="${WORK_DIRECTORY}/ffmpeg-${FFMPEG_VERSION}"
(
  cd "${FFMPEG_SOURCE}"
  export MACOSX_DEPLOYMENT_TARGET=13.0
  export SMARTSTITCH_X264_PREFIX="${PREFIX}"
  ./configure \
    --prefix="${PREFIX}" \
    --arch=arm64 \
    --target-os=darwin \
    --cc=clang \
    --pkg-config="${PROJECT_ROOT}/packaging/pkg-config-x264" \
    --enable-static \
    --disable-shared \
    --disable-doc \
    --disable-debug \
    --disable-ffplay \
    --disable-sdl2 \
    --enable-gpl \
    --enable-libx264 \
    --enable-videotoolbox \
    --enable-audiotoolbox \
    --extra-cflags="-I${PREFIX}/include" \
    --extra-ldflags="-L${PREFIX}/lib"
  make -j"$(sysctl -n hw.logicalcpu)"
  make install
)
cp "${FFMPEG_SOURCE}/COPYING.GPLv2" "${PREFIX}/share/licenses/FFmpeg-GPL-2.0.txt"

strip -x "${PREFIX}/bin/ffmpeg" "${PREFIX}/bin/ffprobe"
file "${PREFIX}/bin/ffmpeg" "${PREFIX}/bin/ffprobe" | grep -q "arm64"
"${PREFIX}/bin/ffmpeg" -hide_banner -encoders 2>&1 | grep -q "libx264"
"${PREFIX}/bin/ffmpeg" -hide_banner -encoders 2>&1 | grep -q "h264_videotoolbox"
"${PREFIX}/bin/ffmpeg" -hide_banner -hwaccels 2>&1 | grep -q "videotoolbox"

if otool -L "${PREFIX}/bin/ffmpeg" "${PREFIX}/bin/ffprobe" | \
  grep -E '/opt/homebrew|/usr/local'; then
  echo "FFmpeg unexpectedly depends on a Homebrew/local library." >&2
  exit 1
fi

echo "Built FFmpeg ${FFMPEG_VERSION} for arm64 in ${PREFIX}"
