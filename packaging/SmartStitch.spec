from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


project_root = Path(SPECPATH).parent
ffmpeg_prefix = Path(
    os.environ.get(
        "SMARTSTITCH_FFMPEG_PREFIX",
        project_root / "build" / "ffmpeg-arm64",
    )
)
ffmpeg = ffmpeg_prefix / "bin" / "ffmpeg"
ffprobe = ffmpeg_prefix / "bin" / "ffprobe"
if not ffmpeg.is_file() or not ffprobe.is_file():
    raise SystemExit(
        "Missing bundled FFmpeg. Run packaging/build_ffmpeg.sh before PyInstaller."
    )

version = os.environ.get("SMARTSTITCH_BUILD_VERSION", "0.1.0").removeprefix("v")
signing_identity = "-"

def data_tree(source, destination):
    source = Path(source)
    return [
        (str(path), str(Path(destination) / path.relative_to(source).parent))
        for path in source.rglob("*")
        if path.is_file()
    ]


datas = [
    *data_tree(project_root / "frontend", "frontend"),
    *data_tree(project_root / "config", "config"),
    *copy_metadata("fastapi"),
    *copy_metadata("pydantic"),
    *copy_metadata("truststore"),
    *copy_metadata("uvicorn"),
    *copy_metadata("pywebview"),
]
hiddenimports = collect_submodules("uvicorn") + ["webview.platforms.cocoa"]

a = Analysis(
    [str(project_root / "packaging" / "launcher.py")],
    pathex=[str(project_root)],
    binaries=[(str(ffmpeg), "bin"), (str(ffprobe), "bin")],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "httpx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SmartStitch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
    codesign_identity=signing_identity,
    entitlements_file=str(project_root / "packaging" / "entitlements.plist"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SmartStitch",
)
app = BUNDLE(
    coll,
    name="SmartStitch.app",
    # PyInstaller uses Pillow to turn this 1024 px source artwork into the
    # multi-resolution .icns embedded in the application bundle.
    icon=str(project_root / "packaging" / "assets" / "SmartStitch.icon-master.png"),
    bundle_identifier="com.sakana.smartstitch",
    version=version,
    target_arch="arm64",
    codesign_identity=signing_identity,
    entitlements_file=str(project_root / "packaging" / "entitlements.plist"),
    info_plist={
        "CFBundleDisplayName": "SmartStitch",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSLocalNetworkUsageDescription": "发现并连接局域网中的 SmartStitch 渲染工作机。",
        "NSBonjourServices": ["_smartstitch._tcp"],
    },
)
