"""Generate the desktop shortcut icons (icons/tracker.ico, icons/refresh.ico)."""
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "icons")
S = 1024  # draw large, downsample for crisp edges


def gradient(c1, c2):
    v = Image.linear_gradient("L").resize((S, S))
    g = ImageChops.add(v, v.rotate(90), scale=2)  # smooth top-left -> bottom-right blend
    g = ImageChops.invert(g)
    return Image.composite(Image.new("RGBA", (S, S), c2), Image.new("RGBA", (S, S), c1), g)


def rounded_mask(inset=40, radius=230):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle((inset, inset, S - inset, S - inset), radius, fill=255)
    return m


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def tile(c1, c2):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = rounded_mask()
    # drop shadow behind the tile
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.putalpha(mask.point(lambda v: v * 0.45).filter(ImageFilter.GaussianBlur(18)))
    img.alpha_composite(shadow, (0, 14))
    grad = gradient(c1, c2)
    # soft glossy highlight in the upper half
    gloss = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gloss).ellipse((-200, -620, S + 200, 470), fill=60)
    gloss = gloss.filter(ImageFilter.GaussianBlur(40))
    grad = Image.composite(Image.new("RGBA", (S, S), (255, 255, 255, 255)), grad, gloss)
    grad.putalpha(mask)
    img.alpha_composite(grad)
    # thin inner rim
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle((44, 44, S - 44, S - 44), 226, outline=(255, 255, 255, 70), width=6)
    img.alpha_composite(rim)
    return img


def draw_box(img, accent, dx=0, dy=0, scale=1.0, speed_lines=True):
    def p(x, y):
        return (512 + (x - 512) * scale + dx, 550 + (y - 550) * scale + dy)

    T, R, C, L = p(512, 290), p(740, 415), p(512, 540), p(284, 415)
    L2, C2, R2 = p(284, 690), p(512, 815), p(740, 690)

    # ground shadow
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cx, cy = p(512, 830)
    ImageDraw.Draw(sh).ellipse((cx - 250 * scale, cy - 45 * scale, cx + 250 * scale, cy + 45 * scale), fill=(0, 0, 40, 110))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(22)))

    d = ImageDraw.Draw(img)
    if speed_lines:
        for y, x0, length in ((470, 150, 90), (560, 110, 130), (650, 165, 75)):
            a, b = p(x0, y), p(x0 + length, y)
            d.line((a, b), fill=(255, 255, 255, 170), width=int(26 * scale))
            for q in (a, b):
                r = 13 * scale
                d.ellipse((q[0] - r, q[1] - r, q[0] + r, q[1] + r), fill=(255, 255, 255, 170))

    d.polygon([L, C, C2, L2], fill=(244, 244, 252, 255))   # left face
    d.polygon([C, R, R2, C2], fill=(210, 212, 235, 255))   # right face
    d.polygon([T, R, C, L], fill=(255, 255, 255, 255))     # top face

    # packing tape across the top and down the right face
    f = 0.10
    a1, a2 = lerp(L, T, 0.5 - f), lerp(L, T, 0.5 + f)
    b1, b2 = lerp(C, R, 0.5 - f), lerp(C, R, 0.5 + f)
    c1, c2 = lerp(C2, R2, 0.5 - f), lerp(C2, R2, 0.5 + f)
    d.polygon([a1, a2, b2, b1], fill=accent)
    darker = tuple(int(v * 0.82) for v in accent[:3]) + (255,)
    d.polygon([b1, b2, c2, c1], fill=darker)

    # crisp edge lines
    edge = (255, 255, 255, 255)
    d.line([C, C2], fill=(190, 192, 220, 255), width=int(6 * scale))
    d.line([L, C, R], fill=edge, width=int(4 * scale))


def refresh_badge(img, color):
    cx, cy, r = 770, 770, 175
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((cx - r, cy - r + 14, cx + r, cy + r + 14), fill=(0, 0, 30, 120))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(16)))
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 255))
    ar, w = 95, 38
    start, end = 40, 320
    d.arc((cx - ar, cy - ar, cx + ar, cy + ar), start, end, fill=color, width=w)
    # arrowhead at the end of the arc, pointing along the direction of travel
    t = math.radians(end)
    ex, ey = cx + ar * math.cos(t), cy + ar * math.sin(t)
    tx, ty = -math.sin(t), math.cos(t)          # tangent (clockwise in screen space)
    nx, ny = math.cos(t), math.sin(t)           # outward normal
    tip = (ex + tx * 62, ey + ty * 62)
    d.polygon([tip, (ex + nx * 52, ey + ny * 52), (ex - nx * 52, ey - ny * 52)], fill=color)


def save(img, name):
    os.makedirs(OUT, exist_ok=True)
    big = img.resize((256, 256), Image.LANCZOS)
    big.save(os.path.join(OUT, name + ".ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(os.path.join(OUT, name + ".png"))


if __name__ == "__main__":
    violet = (124, 58, 237, 255)
    img = tile((79, 70, 229, 255), (192, 38, 211, 255))       # indigo -> fuchsia
    draw_box(img, violet)
    save(img, "tracker")

    teal = (13, 148, 136, 255)
    img = tile((16, 185, 129, 255), (37, 99, 235, 255))       # emerald -> blue
    draw_box(img, teal, dx=-60, dy=-50, scale=0.86, speed_lines=False)
    refresh_badge(img, (16, 140, 170, 255))
    save(img, "refresh")
    print("icons written to", OUT)
