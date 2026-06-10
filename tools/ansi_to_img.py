#!/usr/bin/env python3
"""Render captured ANSI terminal frames to PNG / animated GIF.

Used to make the README screenshot+gif. Reads frames separated by the
form-feed byte (\\x0c) on stdin, or a single frame for a still.
"""
import re
import sys
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/TTF/JetBrainsMonoNerdFontMono-Regular.ttf"
FS = 22
CW, CH = 13, 26          # cell metrics tuned to the font at FS
PAD = 24
BG = (16, 16, 22)

SGR = re.compile(r"\x1b\[([0-9;]*)m")
CUP = re.compile(r"\x1b\[(\d*)(?:;(\d*))?H")       # cursor position (1-based)
CLR = re.compile(r"\x1b\[[0-9;]*[JKG]")            # clear-to-eol etc.
PRIV = re.compile(r"\x1b\[\?[0-9]+[hl]")           # ?1049h etc.


def parse(frame, cols, rows):
    """→ grid[row][col] = (char, fg, bg). Honors absolute cursor moves."""
    grid = [[(" ", (220, 220, 220), None) for _ in range(cols)]
            for _ in range(rows)]
    fg, bg, bold = (220, 220, 220), None, False
    r = c = 0
    i = 0
    while i < len(frame):
        m = SGR.match(frame, i)
        if m:
            fg, bg, bold = apply_sgr(m.group(1), fg, bg, bold)
            i = m.end()
            continue
        m = CUP.match(frame, i)
        if m:
            r = (int(m.group(1) or 1)) - 1
            c = (int(m.group(2) or 1)) - 1
            i = m.end()
            continue
        m = CLR.match(frame, i) or PRIV.match(frame, i)
        if m:
            i = m.end()
            continue
        ch = frame[i]
        i += 1
        if ch == "\n":
            r += 1
            c = 0
            continue
        if ch == "\r":
            c = 0
            continue
        if ch == "\x1b":          # unknown escape — skip the intro byte
            continue
        if 0 <= r < rows and 0 <= c < cols:
            grid[r][c] = (ch, fg, bg)
        c += 1
    return grid


def apply_sgr(params, fg, bg, bold):
    codes = [int(x) for x in params.split(";") if x != ""] or [0]
    i = 0
    while i < len(codes):
        co = codes[i]
        if co == 0:
            fg, bg, bold = (220, 220, 220), None, False
        elif co == 1:
            bold = True
        elif co == 22:
            bold = False
        elif co == 2:
            fg = tuple(int(x * 0.6) for x in fg)
        elif co == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
            fg = (codes[i + 2], codes[i + 3], codes[i + 4])
            i += 4
        elif co == 48 and i + 4 < len(codes) and codes[i + 1] == 2:
            bg = (codes[i + 2], codes[i + 3], codes[i + 4])
            i += 4
        elif co == 39:
            fg = (220, 220, 220)
        elif co == 49:
            bg = None
        i += 1
    return fg, bg, bold


def render(frame, cols, rows):
    grid = parse(frame, cols, rows)
    W = cols * CW + PAD * 2
    H = rows * CH + PAD * 2
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, FS)
    for r in range(rows):
        for c in range(cols):
            ch, fg, bg = grid[r][c]
            x, y = PAD + c * CW, PAD + r * CH
            if bg:
                d.rectangle([x, y, x + CW, y + CH], fill=bg)
            if ch != " ":
                d.text((x, y - 2), ch, font=font, fill=fg)
    return img


def main():
    cols = int(sys.argv[1])
    rows = int(sys.argv[2])
    out = sys.argv[3]
    data = sys.stdin.buffer.read().decode("utf-8", "ignore")
    frames = [f for f in data.split("\x0c") if f.strip()]
    imgs = [render(f, cols, rows) for f in frames]
    if out.endswith(".gif") and len(imgs) > 1:
        imgs[0].save(out, save_all=True, append_images=imgs[1:],
                     duration=90, loop=0, optimize=True)
    else:
        imgs[-1].save(out)
    print(f"wrote {out} ({len(imgs)} frame(s), {imgs[0].size})")


if __name__ == "__main__":
    main()
