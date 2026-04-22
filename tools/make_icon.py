"""產生 判決檢索 Monitor.app 的 icon（.icns）。

設計：
  · 暖銅 seal (#6D5A41) 圓角方形背景（跟 app 主題色一致）
  · 大「判」字、米紙色 (#F7F7F5)
  · macOS 標準 superellipse rounded corners（border-radius ~22% of side length）
  · 8 段 PNG（16/32/128/256/512 + 2x）→ iconutil 合成 icon.icns

執行：.venv/bin/python tools/make_icon.py
產出：tools/AppIcon.icns
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]

OUT_DIR = Path(__file__).parent
ICONSET = OUT_DIR / "AppIcon.iconset"
ICNS = OUT_DIR / "AppIcon.icns"

# 主尺寸 1024：渲染一次、rescale 其他尺寸
BASE_SIZE = 1024
# macOS 標準圓角 ≈ 22.5% 邊長（superellipse squircle、但矩形 rounded 已接近視覺）
CORNER_RADIUS = int(BASE_SIZE * 0.225)

# 配色：暖銅 seal / 米紙 parchment（專案 accent 色）
BG_COLOR = (109, 90, 65, 255)      # #6D5A41 seal
FG_COLOR = (247, 247, 245, 255)    # #F7F7F5 parchment

# 文字
TEXT = "判"
# 試 PingFang（Apple 預設）→ Songti fallback。用 Bold weight 讓小 size 時更清楚
FONT_CANDIDATES = [
    ("/System/Library/Fonts/PingFang.ttc", 8),  # PingFang SC Semibold
    ("/System/Library/Fonts/PingFang.ttc", 7),  # PingFang SC Medium
    ("/System/Library/Fonts/PingFang.ttc", 0),  # PingFang SC Regular
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 1),  # Songti Bold
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),  # Songti Regular
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path, idx in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size, index=idx)
            except (OSError, IndexError):
                continue
    raise RuntimeError("No CJK font found")


def render_base() -> Image.Image:
    """渲染 1024x1024 基底、之後 rescale 各 size"""
    img = Image.new("RGBA", (BASE_SIZE, BASE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded rectangle 背景
    draw.rounded_rectangle(
        [(0, 0), (BASE_SIZE, BASE_SIZE)],
        radius=CORNER_RADIUS,
        fill=BG_COLOR,
    )

    # 「判」字 — 佔 ~70% 寬度、置中
    # PingFang Semibold 在 720px font size 大約佔 720px 寬（CJK 字元接近方形）
    font_size = int(BASE_SIZE * 0.72)
    font = _load_font(font_size)

    # 量字元實際 bbox、做精準置中
    bbox = draw.textbbox((0, 0), TEXT, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    # 扣掉 bbox 的內部 offset（CJK 字體頂部常有空白）
    tx = (BASE_SIZE - text_w) // 2 - bbox[0]
    ty = (BASE_SIZE - text_h) // 2 - bbox[1]
    # 視覺微調往上移（中文字的視覺中心通常偏下）
    ty -= int(BASE_SIZE * 0.02)

    draw.text((tx, ty), TEXT, fill=FG_COLOR, font=font)
    return img


def build_iconset() -> None:
    base = render_base()

    # 清舊
    if ICONSET.exists():
        for f in ICONSET.iterdir():
            f.unlink()
        ICONSET.rmdir()
    ICONSET.mkdir()

    # 需要的尺寸（macOS iconutil 規範）
    specs = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in specs:
        scaled = base.resize((size, size), Image.LANCZOS)
        scaled.save(ICONSET / name, "PNG")
        print(f"  ↳ {name} ({size}x{size})")


def make_icns() -> None:
    if ICNS.exists():
        ICNS.unlink()
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)],
        check=True,
    )
    # 清 iconset（只保留最終的 .icns）
    for f in ICONSET.iterdir():
        f.unlink()
    ICONSET.rmdir()
    size_kb = ICNS.stat().st_size // 1024
    print(f"\n✅ 產出 {ICNS} ({size_kb} KB)")


if __name__ == "__main__":
    print(f"渲染 iconset（TEXT={TEXT!r}、BG={BG_COLOR}、FG={FG_COLOR}）：")
    build_iconset()
    make_icns()
