from pathlib import Path
import struct


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ICON_MASTER = PROJECT_ROOT / "packaging" / "assets" / "SmartStitch.icon-master.png"


def test_macos_icon_master_is_1024_pixel_rgba_png() -> None:
    data = ICON_MASTER.read_bytes()

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"

    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height) == (1024, 1024)
    assert bit_depth == 8
    assert color_type == 6  # RGBA
