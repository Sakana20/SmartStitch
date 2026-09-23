#!/usr/bin/env python3
"""Generate the 2× Finder background used by the drag-to-Applications DMG."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


DESTINATION = Path(__file__).parent / "assets" / "dmg-background.png"
SIZE = (1320, 800)
SCALE = 2


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = (
        "/System/Library/Fonts/STHeiti Medium.ttc"
        if bold else "/System/Library/Fonts/STHeiti Light.ttc"
    )
    return ImageFont.truetype(path, size * SCALE)


def main() -> None:
    image = Image.new("RGB", SIZE)
    pixels = image.load()
    for y in range(SIZE[1]):
        for x in range(SIZE[0]):
            shade = int(248 - 9 * x / SIZE[0] - 7 * y / SIZE[1])
            pixels[x, y] = (shade, min(255, shade + 2), shade - 3)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((42, 40, 1278, 758), radius=42, outline="#d7dfd7", width=2)
    draw.text((96, 88), "SmartStitch", font=font(30, bold=True), fill="#1d5c43")
    draw.text((98, 164), "安装到 Mac", font=font(17), fill="#58675d")
    draw.line((550, 402, 770, 402), fill="#1d5c43", width=12)
    draw.polygon([(770, 375), (816, 402), (770, 429)], fill="#1d5c43")
    instruction = "将 SmartStitch 拖到“应用程序”文件夹"
    box = draw.textbbox((0, 0), instruction, font=font(18, bold=True))
    draw.text(((SIZE[0] - (box[2] - box[0])) / 2, 610), instruction, font=font(18, bold=True), fill="#284638")
    draw.text((98, 702), "拖拽完成后，请退出旧版并从应用程序文件夹启动", font=font(11), fill="#66766b")
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    image.resize((660, 400), Image.Resampling.LANCZOS).save(DESTINATION, optimize=True)


if __name__ == "__main__":
    main()
