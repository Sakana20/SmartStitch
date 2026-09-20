# SmartStitch third-party notices

The macOS application bundles a CPython runtime and the Python packages declared in
`pyproject.toml`/`uv.lock`. Their package metadata and license files are preserved by
the PyInstaller bundle where supplied by the package.

The application also bundles FFmpeg 7.1.5 and x264 at the exact revisions recorded in
`packaging/build_ffmpeg.sh`. FFmpeg is compiled with `--enable-gpl --enable-libx264`,
so that binary is distributed under GPL-2.0-or-later. x264 is GPL-2.0.

The DMG includes the applicable license texts, the exact source archives used to build
the binaries, and the build scripts under `Open Source Licenses`. SmartStitch invokes
`ffmpeg` and `ffprobe` as separate subprocesses; it does not link their libraries into
the Python application.
