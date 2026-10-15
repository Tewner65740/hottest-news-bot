"""Собственные картинки для постов и аватарка: без чужих водяных знаков."""
import hashlib
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

def _font(*candidates):
    for p in candidates:
        if Path(p).exists():
            return p
    return candidates[-1]


FONT_BOLD = _font(
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
FONT_IMPACT = _font(
    "C:/Windows/Fonts/impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
BRAND = "THE HOTTEST NEWS"

PALETTES = [
    ((190, 20, 40), (30, 0, 10)),
    ((20, 80, 190), (0, 10, 40)),
    ((220, 100, 10), (50, 10, 0)),
    ((120, 30, 160), (15, 0, 30)),
    ((10, 130, 110), (0, 25, 25)),
]


def _gradient(size, c1, c2):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
    return img


def _no_emoji(text):
    return re.sub(r"[^\w\s.,:;!?«»\"'()\-—–%+/№]", "", text).strip()


def _wrap(draw, text, font, width):
    lines, cur = [], ""
    for w in text.split():
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def news_card(title, path, size=1080):
    c1, c2 = PALETTES[int(hashlib.md5(title.encode()).hexdigest(), 16) % len(PALETTES)]
    img = _gradient(size, c1, c2)
    d = ImageDraw.Draw(img)
    margin = 80
    text = _no_emoji(title).upper()[:140]
    for fs in range(88, 40, -4):
        font = ImageFont.truetype(FONT_IMPACT, fs)
        lines = _wrap(d, text, font, size - 2 * margin)
        if len(lines) * fs * 1.15 <= size - 420:
            break
    d.rectangle([margin, 90, margin + 150, 102], fill="white")
    y = 150
    for ln in lines:
        d.text((margin, y), ln, font=font, fill="white")
        y += int(fs * 1.15)
    brand_font = ImageFont.truetype(FONT_BOLD, 36)
    d.text((margin, size - 130), BRAND, font=brand_font, fill=(255, 255, 255))
    d.text((margin, size - 80), "новости каждый день", font=ImageFont.truetype(FONT_BOLD, 28), fill=(255, 255, 255, 200))
    img.save(path, "PNG")
    return path


def avatar(path, size=512):
    img = _gradient(size, (200, 20, 40), (30, 0, 10))
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype(FONT_IMPACT, 190)
    small = ImageFont.truetype(FONT_IMPACT, 70)
    d.text((size / 2, 210), "HOT", font=big, fill="white", anchor="mm")
    d.text((size / 2, 360), "NEWS", font=small, fill="white", anchor="mm")
    d.rectangle([size / 2 - 90, 410, size / 2 + 90, 420], fill="white")
    img.save(path, "PNG")
    return path


if __name__ == "__main__":
    base = Path(__file__).parent
    avatar(base / "avatar.png")
    news_card("Ракетную опасность объявили сразу в нескольких регионах России", base / "sample_card.png")
