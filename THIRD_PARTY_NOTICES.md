# SmartStitch third-party notices

The macOS application bundles a CPython runtime and the Python packages declared in
`pyproject.toml`/`uv.lock`. Their package metadata and license files are preserved by
the PyInstaller bundle where supplied by the package.

The application also bundles FFmpeg 7.1.5 and x264 at the exact revisions recorded in
`packaging/build_ffmpeg.sh`. FFmpeg is compiled with `--enable-gpl --enable-libx264`,
so that binary is distributed under GPL-2.0-or-later. x264 is GPL-2.0.

The DMG includes the applicable license texts, the exact source archives used to build
the binaries, and the build scripts under `Open Source Licenses`. SmartStitch invokes
`ffmpeg` and `ffprobe` as separate subprocesses for its standard media pipeline.
The VFR-only upscaler additionally uses PyAV (BSD-3-Clause) and its bundled FFmpeg
libraries in process to encode individual frame timestamps. PyAV package metadata,
license files and dynamic libraries are included in the application bundle; these
libraries include libx264 and must be accounted for when preparing release sources
and licenses in addition to the separately built command-line binaries.

The video upscaler uses the official CoreML conversion of Real-ESRGAN x2plus from
[hanxiao/real-esrgan-coreml](https://github.com/hanxiao/real-esrgan-coreml). The
upstream README states MIT for its code and BSD-3-Clause for weights obtained from
[xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN). The build fetches
the pinned v1.0.0 model ZIPs and verifies SHA-256 before bundling both models:
`x2plus` = `4acf0afa2828e82b88e1d712b4b7604977698c99aa1505276c943922be6b3938`,
`animevideo` = `0e83446f03b4c0a60c970da02d70d10af14949c169714f9ad8e8ea3e479b145b`;
installed apps read these models from `Contents/Resources/models` without a download.
Runtime dependencies include `coremltools`, NumPy, and Pillow. The upstream
BSD-3-Clause copyright and license for the weights is reproduced in
`packaging/licenses/Real-ESRGAN-BSD-3-Clause.txt` and the DMG's
`Open Source Licenses` directory.
