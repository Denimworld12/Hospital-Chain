#!/usr/bin/env python3
"""Generate MedChain's toile/engraving line-art SVGs into assets/svg/.

Stdlib only and deterministic (seeded), so re-running produces identical files.
Every illustration is stroke-only (1-1.5px, indigo or sky-blue, no fills); tone
comes from hatching. The three accent blobs are the only filled shapes.

    python3 assets/generate_svgs.py
"""

from __future__ import annotations

import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent / "svg"

INDIGO = "#234386"
WHITE = "#ffffff"
SKY = "#6aa8dc"
GOLD = "#ffc400"
SAGE = "#a2d3a6"
ORANGE = "#ed7328"

Point = tuple[float, float]


def fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class Pen:
    """Builds slightly wobbly, hand-drawn looking SVG path data."""

    def __init__(self, seed: int, amp: float = 0.55):
        self.rng = random.Random(seed)
        self.amp = amp

    # -- smoothing ---------------------------------------------------------
    @staticmethod
    def smooth(pts: list[Point], closed: bool = False) -> str:
        """Catmull-Rom spline through pts, emitted as cubic Beziers."""
        if len(pts) < 2:
            return ""
        if closed:
            ext = [pts[-1]] + pts + [pts[0], pts[1]]
        else:
            ext = [pts[0]] + pts + [pts[-1]]
        d = [f"M{fmt(pts[0][0])} {fmt(pts[0][1])}"]
        n = len(pts) if closed else len(pts) - 1
        for i in range(1, n + 1):
            p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
            c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
            c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
            d.append(
                f"C{fmt(c1[0])} {fmt(c1[1])} {fmt(c2[0])} {fmt(c2[1])} {fmt(p2[0])} {fmt(p2[1])}"
            )
        if closed:
            d.append("Z")
        return "".join(d)

    def jitter(self, pts: list[Point], amp: float | None = None, keep_ends: bool = True) -> list[Point]:
        a = self.amp if amp is None else amp
        out = []
        for i, (x, y) in enumerate(pts):
            if keep_ends and i in (0, len(pts) - 1):
                out.append((x, y))
            else:
                out.append((x + self.rng.uniform(-a, a), y + self.rng.uniform(-a, a)))
        return out

    # -- primitives ----------------------------------------------------------
    def line(self, x1, y1, x2, y2, step: float = 14, amp: float | None = None) -> str:
        length = math.hypot(x2 - x1, y2 - y1)
        n = max(2, int(length / step) + 1)
        pts = [(x1 + (x2 - x1) * i / (n - 1), y1 + (y2 - y1) * i / (n - 1)) for i in range(n)]
        return self.smooth(self.jitter(pts, amp))

    def poly(self, pts: list[Point], step: float = 14, closed: bool = False, amp: float | None = None) -> str:
        """Hand-drawn polyline: each straight edge gets its own wobble."""
        seq = pts + ([pts[0]] if closed else [])
        return "".join(self.line(*a, *b, step=step, amp=amp) for a, b in zip(seq, seq[1:]))

    def rect(self, x, y, w, h, amp: float | None = None) -> str:
        o = 0.8  # tiny hand-drawn overshoot at the corners
        return "".join(
            [
                self.line(x - o, y, x + w + o, y, amp=amp),
                self.line(x + w, y - o, x + w, y + h + o, amp=amp),
                self.line(x + w + o, y + h, x - o, y + h, amp=amp),
                self.line(x, y + h + o, x, y - o, amp=amp),
            ]
        )

    def ellipse(self, cx, cy, rx, ry, n: int = 18, amp: float | None = None, rot: float = 0) -> str:
        pts = []
        cr, sr = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        for i in range(n):
            t = 2 * math.pi * i / n
            x, y = rx * math.cos(t), ry * math.sin(t)
            pts.append((cx + x * cr - y * sr, cy + x * sr + y * cr))
        return self.smooth(self.jitter(pts, amp, keep_ends=False), closed=True)

    def circle(self, cx, cy, r, amp: float | None = None) -> str:
        return self.ellipse(cx, cy, r, r, n=max(10, int(r * 1.2)), amp=amp)

    def bezier(self, segments: list[tuple], amp: float | None = None, samples: int = 8) -> str:
        """segments: [start, (c1, c2, end), (c1, c2, end), ...] sampled then re-smoothed."""
        start = segments[0]
        pts = [start]
        cur = start
        for c1, c2, end in segments[1:]:
            for i in range(1, samples + 1):
                t = i / samples
                mt = 1 - t
                x = mt**3 * cur[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * end[0]
                y = mt**3 * cur[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * end[1]
                pts.append((x, y))
            cur = end
        return self.smooth(self.jitter(pts, amp))

    # -- engraving tone ----------------------------------------------------
    def hatch(self, x0, y0, x1, y1, angle: float, spacing: float, amp: float = 0.35) -> str:
        """Parallel wobbly lines covering the bbox at `angle` degrees (clip afterwards)."""
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half = math.hypot(x1 - x0, y1 - y0) / 2 + spacing
        a = math.radians(angle)
        dx, dy = math.cos(a), math.sin(a)
        nx, ny = -dy, dx
        d = []
        k = -half
        while k <= half:
            px, py = cx + nx * k, cy + ny * k
            d.append(self.line(px - dx * half, py - dy * half, px + dx * half, py + dy * half, step=18, amp=amp))
            k += spacing
        return "".join(d)

    def arcs(self, cx, cy, r0, r1, spacing, a0, a1, amp: float = 0.35) -> str:
        """Concentric arc family (contour hatching that follows a curved surface)."""
        d = []
        r = r0
        while r <= r1:
            n = max(6, int(math.radians(abs(a1 - a0)) * r / 10))
            pts = []
            for i in range(n + 1):
                t = math.radians(a0 + (a1 - a0) * i / n)
                pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
            d.append(self.smooth(self.jitter(pts, amp)))
            r += spacing
        return "".join(d)

    def rays(self, cx, cy, r0, r1, a0, a1, count, amp: float = 0.3) -> str:
        d = []
        for i in range(count):
            t = math.radians(a0 + (a1 - a0) * i / max(1, count - 1))
            d.append(
                self.line(cx + r0 * math.cos(t), cy + r0 * math.sin(t), cx + r1 * math.cos(t), cy + r1 * math.sin(t), step=10, amp=amp)
            )
        return "".join(d)


def svg(viewbox: str, body: str, title: str, stroke: str = INDIGO, width: float = 1.5, extra_defs: str = "") -> str:
    defs = f"<defs>{extra_defs}</defs>" if extra_defs else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}" fill="none" stroke="{stroke}" '
        f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="{title}">'
        f"<title>{title}</title>{defs}{body}</svg>\n"
    )


def p(d: str, **attrs) -> str:
    a = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f'<path d="{d}"{a}/>'


# ---------------------------------------------------------------------------
# Hospital buildings (three size variants)
# ---------------------------------------------------------------------------


def awning(pen: Pen, x, y, w, h, stripes: int) -> str:
    """Scalloped, striped awning: top edge at y, scallops hang to y+h."""
    d = [pen.line(x + 3, y, x + w - 3, y, amp=0.3), pen.line(x + 3, y, x - 2, y + h * 0.65, amp=0.3),
         pen.line(x + w - 3, y, x + w + 2, y + h * 0.65, amp=0.3)]
    sx0, sx1 = x - 2, x + w + 2
    sw = (sx1 - sx0) / stripes
    scallop = []
    for i in range(stripes):
        a = sx0 + i * sw
        scallop.append(((a, y + h * 0.65), (a + sw / 2, y + h * 1.25), (a + sw, y + h * 0.65)))
    for a, mid, b in scallop:
        d.append(pen.bezier([a, ((a[0], mid[1]), (b[0], mid[1]), b)], amp=0.2, samples=5))
    for i in range(1, stripes):
        top_x = x + 3 + (w - 6) * i / stripes
        bot_x = sx0 + sw * i
        d.append(pen.line(top_x, y, bot_x, y + h * 0.65, amp=0.25))
    return "".join(d)


def window(pen: Pen, x, y, w, h, mullion: bool = True, arched: bool = False) -> str:
    d = []
    if arched:
        d.append(pen.line(x, y + h, x, y + w / 2))
        d.append(pen.bezier([(x, y + w / 2), ((x, y - w * 0.15), (x + w, y - w * 0.15), (x + w, y + w / 2))], samples=6))
        d.append(pen.line(x + w, y + w / 2, x + w, y + h))
        d.append(pen.line(x - 1, y + h, x + w + 1, y + h))
    else:
        d.append(pen.rect(x, y, w, h))
    if mullion:
        d.append(pen.line(x + w / 2, y + (w / 2 if arched else 1), x + w / 2, y + h - 1, amp=0.2))
        d.append(pen.line(x + 1, y + h * 0.5, x + w - 1, y + h * 0.5, amp=0.2))
    # sill
    d.append(pen.line(x - 2, y + h + 2.5, x + w + 2, y + h + 2.5, amp=0.2))
    return "".join(d)


def cross(pen: Pen, cx, cy, arm, thick) -> str:
    a, t = arm, thick / 2
    pts = [(cx - t, cy - a), (cx + t, cy - a), (cx + t, cy - t), (cx + a, cy - t), (cx + a, cy + t), (cx + t, cy + t),
           (cx + t, cy + a), (cx - t, cy + a), (cx - t, cy + t), (cx - a, cy + t), (cx - a, cy - t), (cx - t, cy - t)]
    return pen.poly(pts, step=8, closed=True, amp=0.3)


def shrub(pen: Pen, cx, base, h, lean: float = 0) -> str:
    """Toile-style botanical shrub: a stem with loopy almond leaves."""
    d = [pen.bezier([(cx, base), ((cx + lean * 0.3, base - h * 0.4), (cx + lean * 0.8, base - h * 0.75), (cx + lean, base - h))])]
    for i in range(5):
        t = 0.25 + i * 0.16
        sx, sy = cx + lean * t, base - h * t
        side = -1 if i % 2 else 1
        L = h * (0.32 - i * 0.03)
        tip = (sx + side * L, sy - L * 0.55)
        d.append(pen.bezier([(sx, sy), ((sx + side * L * 0.3, sy - L * 0.6), (tip[0] - side * L * 0.2, tip[1] - L * 0.15), tip)], amp=0.2, samples=5))
        d.append(pen.bezier([(sx, sy), ((sx + side * L * 0.5, sy + L * 0.05), (tip[0], tip[1] + L * 0.35), tip)], amp=0.2, samples=5))
        d.append(pen.line(sx, sy, tip[0] - side * L * 0.15, tip[1] + L * 0.12, amp=0.15))
    return "".join(d)


def hospital_large() -> str:
    pen = Pen(11)
    body = []
    # ground with grass tufts
    body.append(pen.line(6, 196, 234, 196, step=18))
    for gx in (14, 30, 206, 224):
        body.append(pen.poly([(gx - 3, 196), (gx - 1, 190), (gx + 1, 196), (gx + 3, 191), (gx + 4, 196)], step=4, amp=0.2))
    # wings
    body.append(pen.rect(34, 96, 172, 100))
    body.append(pen.line(30, 96, 210, 96))
    body.append(pen.line(30, 91, 210, 91))
    body.append(pen.line(30, 91, 30, 96, amp=0.1))
    body.append(pen.line(210, 91, 210, 96, amp=0.1))
    # dentil trim under cornice
    for x in range(38, 204, 8):
        body.append(pen.line(x, 99, x, 102.5, amp=0.1))
    # central tower
    body.append(pen.rect(88, 44, 64, 52))
    body.append(pen.poly([(82, 44), (120, 20), (158, 44)], step=10))
    body.append(pen.line(82, 44, 158, 44))
    # roundel with red-cross-style cross (line only)
    body.append(pen.circle(120, 66, 15))
    body.append(pen.circle(120, 66, 17.5, amp=0.4))
    body.append(cross(pen, 120, 66, 9, 6))
    # little flag pole on the pediment
    body.append(pen.line(120, 20, 120, 8, amp=0.1))
    body.append(pen.poly([(120, 8), (131, 11), (120, 14)], step=6, amp=0.2))
    # windows: two rows each wing, arched top row
    for x in (44, 62, 164, 182):
        body.append(window(pen, x, 110, 12, 18, arched=True))
        body.append(window(pen, x, 146, 12, 16))
    for x in (96, 132):
        body.append(window(pen, x, 110, 12, 16))
    # door + awning + steps
    body.append(awning(pen, 102, 132, 36, 10, stripes=6))
    body.append(pen.rect(108, 150, 24, 46))
    body.append(pen.line(120, 150, 120, 196, amp=0.2))
    body.append(pen.circle(116.5, 174, 0.9, amp=0.05))
    body.append(pen.circle(123.5, 174, 0.9, amp=0.05))
    body.append(pen.line(102, 196, 138, 196))
    body.append(pen.line(98, 200, 142, 200))
    # engraving shade: right side of tower and right wing corner
    clip = (
        '<clipPath id="hl-shade-a"><rect x="140" y="45" width="12" height="51"/></clipPath>'
        '<clipPath id="hl-shade-b"><rect x="196" y="103" width="10" height="93"/></clipPath>'
    )
    body.append(f'<g clip-path="url(#hl-shade-a)" stroke="{SKY}" stroke-width="1">{p(pen.hatch(140, 45, 152, 96, 60, 3.2))}</g>')
    body.append(f'<g clip-path="url(#hl-shade-b)" stroke="{SKY}" stroke-width="1">{p(pen.hatch(196, 103, 206, 196, 60, 3.2))}</g>')
    body.append(shrub(pen, 20, 196, 42, lean=-4))
    body.append(shrub(pen, 220, 196, 36, lean=5))
    paths = "".join(b if b.startswith("<") else p(b) for b in body)
    return svg("0 0 240 210", paths, "Hospital building (large)", extra_defs=clip)


def hospital_medium() -> str:
    pen = Pen(23)
    body = []
    body.append(pen.line(4, 150, 176, 150, step=18))
    body.append(pen.rect(24, 70, 132, 80))
    body.append(pen.line(20, 70, 160, 70))
    body.append(pen.line(20, 65, 160, 65))
    body.append(pen.rect(66, 34, 48, 36))
    body.append(pen.line(62, 34, 118, 34))
    body.append(pen.poly([(62, 34), (90, 16), (118, 34)], step=10))
    body.append(pen.circle(90, 52, 11))
    body.append(cross(pen, 90, 52, 7, 4.6))
    for x in (34, 50, 120, 136):
        body.append(window(pen, x, 82, 10, 15, arched=True))
        body.append(window(pen, x, 112, 10, 13))
    body.append(awning(pen, 76, 100, 28, 8, stripes=5))
    body.append(pen.rect(81, 114, 18, 36))
    body.append(pen.line(90, 114, 90, 150, amp=0.2))
    body.append(pen.line(76, 154, 104, 154))
    clip = '<clipPath id="hm-shade"><rect x="104" y="35" width="10" height="35"/></clipPath>'
    body.append(f'<g clip-path="url(#hm-shade)" stroke="{SKY}" stroke-width="1">{p(pen.hatch(104, 35, 114, 70, 60, 3.2))}</g>')
    body.append(shrub(pen, 12, 150, 30, lean=-3))
    paths = "".join(b if b.startswith("<") else p(b) for b in body)
    return svg("0 0 180 160", paths, "Hospital building (medium)", extra_defs=clip)


def hospital_small() -> str:
    pen = Pen(37)
    body = []
    body.append(pen.line(4, 104, 116, 104, step=16))
    body.append(pen.rect(18, 44, 84, 60))
    body.append(pen.poly([(12, 44), (60, 16), (108, 44)], step=10))
    body.append(pen.line(12, 44, 108, 44))
    body.append(cross(pen, 60, 32, 6, 4))
    for x in (26, 86):
        body.append(window(pen, x, 54, 9, 13, mullion=True))
    body.append(window(pen, 26, 80, 9, 11, mullion=False))
    body.append(window(pen, 86, 80, 9, 11, mullion=False))
    body.append(awning(pen, 46, 62, 28, 7, stripes=4))
    body.append(pen.rect(51, 76, 18, 28))
    body.append(pen.line(60, 76, 60, 104, amp=0.15))
    paths = "".join(p(b) for b in body)
    return svg("0 0 120 110", paths, "Hospital building (small)")


# ---------------------------------------------------------------------------
# ECG trace divider
# ---------------------------------------------------------------------------


def ecg_wave() -> str:
    pen = Pen(5, amp=0.25)
    w, base = 1200, 46
    pts: list[Point] = []
    x = 0.0
    beat = 0
    while x + 212 < w:
        # flowing baseline between beats (gentle respiratory sway)
        for i in range(10):
            xx = x + i * 9
            pts.append((xx, base + 3 * math.sin(xx / 95)))
        x += 90
        sway = 3 * math.sin(x / 95)
        amp = 1.0 + 0.12 * math.sin(beat * 1.7)
        # P wave
        pts += [(x + 6, base + sway - 4 * amp), (x + 14, base + sway - 6 * amp), (x + 22, base + sway - 3 * amp), (x + 28, base + sway)]
        # QRS complex
        pts += [(x + 36, base + sway), (x + 40, base + sway + 6), (x + 47, base + sway - 38 * amp), (x + 54, base + sway + 18 * amp),
                (x + 59, base + sway), (x + 70, base + sway)]
        # T wave
        pts += [(x + 82, base + sway - 7 * amp), (x + 94, base + sway - 11 * amp), (x + 106, base + sway - 6 * amp), (x + 116, base + sway)]
        x += 122
        beat += 1
    while x < w:
        pts.append((x, base + 3 * math.sin(x / 95)))
        x += 9
    pts.append((w, base + 3 * math.sin(w / 95)))
    d = pen.smooth(pen.jitter(pts, 0.25))
    # a faint sky-blue echo line underneath, like a second engraved rule
    echo = pen.line(0, base + 22, w, base + 22, step=40, amp=0.4)
    body = p(d) + p(echo, stroke=SKY, stroke_width="1", stroke_dasharray="2 6")
    return svg(f"0 0 {w} 80", body, "ECG trace divider").replace(
        "<svg ", '<svg preserveAspectRatio="none" ', 1
    ).replace('stroke-linejoin="round"', 'stroke-linejoin="round" vector-effect="non-scaling-stroke"', 1)


# ---------------------------------------------------------------------------
# Anatomical heart (hero)
# ---------------------------------------------------------------------------


def heart_anatomical() -> str:
    pen = Pen(101, amp=0.45)
    body = []
    defs = []

    ventricles = [
        (92, 196),
        ((78, 240), (98, 296), (148, 334)),
        ((172, 352), (196, 368), (214, 380)),
        ((236, 352), (266, 304), (274, 254)),
        ((280, 214), (266, 184), (244, 172)),
        ((212, 164), (178, 180), (152, 176)),
        ((128, 172), (108, 182), (92, 196)),
    ]
    vent_d = "M92 196C78 240 98 296 148 334C172 352 196 368 214 380C236 352 266 304 274 254C280 214 266 184 244 172C212 164 178 180 152 176C128 172 108 182 92 196Z"
    defs.append(f'<clipPath id="ha-vent"><path d="{vent_d}"/></clipPath>')
    defs.append('<clipPath id="ha-shadow"><path d="M214 150 L330 150 L330 400 L150 400 C200 330 222 260 214 150Z"/></clipPath>')
    body.append(pen.bezier(ventricles, amp=0.5))

    # right atrium (viewer's left) and its auricle
    body.append(pen.bezier([(92, 196), ((64, 186), (52, 154), (70, 132)), ((82, 118), (100, 118), (110, 128))]))
    body.append(pen.bezier([(70, 150), ((58, 150), (56, 166), (66, 172)), ((74, 178), (86, 174), (92, 170))], amp=0.3))
    ra_d = "M92 196C64 186 52 154 70 132C82 118 100 118 110 128C122 150 128 170 128 172C108 182 100 188 92 196Z"
    defs.append(f'<clipPath id="ha-ra"><path d="{ra_d}"/></clipPath>')
    body.append(f'<g clip-path="url(#ha-ra)" stroke="{SKY}" stroke-width="1">{p(pen.arcs(118, 160, 18, 70, 4.2, 120, 260))}</g>')

    # left auricle (viewer's right), a small lobed flap
    body.append(pen.bezier([(246, 174), ((256, 152), (282, 150), (284, 168)), ((286, 180), (276, 190), (266, 188))], amp=0.35))
    body.append(pen.bezier([(262, 166), ((268, 162), (276, 164), (278, 170))], amp=0.2))

    # superior vena cava (viewer's left), a tube with an open end
    body.append(pen.bezier([(82, 128), ((80, 100), (78, 70), (80, 44))]))
    body.append(pen.bezier([(108, 126), ((106, 100), (104, 70), (104, 46))]))
    body.append(pen.ellipse(92, 45, 12, 4.2, amp=0.25))
    svc_d = "M80 44 L104 46 L108 126 L82 128Z"
    defs.append(f'<clipPath id="ha-svc"><path d="{svc_d}"/></clipPath>')
    body.append(f'<g clip-path="url(#ha-svc)" stroke="{SKY}" stroke-width="1">{p(pen.hatch(96, 44, 110, 128, 90, 3))}</g>')

    # aorta: ascending + arch, descending behind the pulmonary trunk
    outer = [(126, 170), ((118, 112), (128, 62), (176, 56)), ((218, 52), (244, 78), (244, 116)), ((244, 130), (243, 140), (242, 148))]
    inner = [(156, 168), ((150, 118), (158, 88), (180, 86)), ((200, 85), (214, 96), (216, 118))]
    body.append(pen.bezier(outer))
    body.append(pen.bezier(inner))
    aorta_d = "M126 170C118 112 128 62 176 56C218 52 244 78 244 116L242 148L216 128L216 118C214 96 200 85 180 86C158 88 150 118 156 168Z"
    defs.append(f'<clipPath id="ha-aorta"><path d="{aorta_d}"/></clipPath>')
    body.append(f'<g clip-path="url(#ha-aorta)" stroke="{INDIGO}" stroke-width="1">{p(pen.rays(180, 118, 20, 80, -60, 40, 16))}</g>')
    body.append(f'<g clip-path="url(#ha-aorta)" stroke="{SKY}" stroke-width="1">{p(pen.rays(180, 118, 20, 80, 150, 200, 8))}</g>')

    # three arch branches with open ends
    for bx, lean, top in ((150, -8, 14), (178, 0, 8), (206, 9, 16)):
        l0, r0 = bx - 7, bx + 7
        body.append(pen.bezier([(l0, 66 if bx != 206 else 60), ((l0 + lean * 0.2, 44), (l0 + lean * 0.6, 30), (l0 + lean, top + 2))], amp=0.3))
        body.append(pen.bezier([(r0, 62 if bx != 150 else 64), ((r0 + lean * 0.2, 44), (r0 + lean * 0.6, 30), (r0 + lean, top + 2))], amp=0.3))
        body.append(pen.ellipse(bx + lean, top + 2, 7.2, 2.6, amp=0.2))

    # pulmonary trunk in front of the aorta, bifurcating to the viewer's right
    body.append(pen.bezier([(162, 178), ((160, 150), (176, 128), (206, 124)), ((230, 120), (262, 116), (292, 116))]))
    body.append(pen.bezier([(204, 184), ((208, 166), (220, 150), (240, 146)), ((258, 142), (276, 142), (294, 140))]))
    body.append(pen.ellipse(293, 128, 4.2, 12.2, amp=0.2))
    pulm_d = "M162 178C160 150 176 128 206 124C230 120 262 116 292 116L294 140C276 142 258 142 240 146C220 150 208 166 204 184Z"
    defs.append(f'<clipPath id="ha-pulm"><path d="{pulm_d}"/></clipPath>')
    body.append(f'<g clip-path="url(#ha-pulm)" stroke="{SKY}" stroke-width="1">{p(pen.hatch(160, 110, 300, 190, 72, 3.4))}</g>')

    # ventricle contour hatching (follows the surface) + cross-hatch in shadow
    body.append(f'<g clip-path="url(#ha-vent)" stroke="{INDIGO}" stroke-width="1" opacity="0.8">{p(pen.arcs(10, 120, 150, 330, 6.5, -10, 80))}</g>')
    body.append(
        f'<g clip-path="url(#ha-vent)"><g clip-path="url(#ha-shadow)" stroke="{INDIGO}" stroke-width="1" opacity="0.7">'
        f'{p(pen.hatch(150, 150, 290, 390, -35, 4.2))}</g></g>'
    )

    # coronary arteries: LAD along the interventricular groove + twigs, RCA in AV groove
    body.append(pen.bezier([(196, 178), ((186, 230), (198, 300), (214, 376))], amp=0.4))
    body.append(pen.bezier([(200, 180), ((192, 232), (204, 300), (216, 372))], amp=0.35))
    for y0, side in ((215, 1), (240, -1), (268, 1), (298, -1), (326, 1)):
        x0 = 191 + (y0 - 178) * 0.1
        body.append(pen.bezier([(x0, y0), ((x0 + side * 14, y0 + 6), (x0 + side * 22, y0 + 18), (x0 + side * 30, y0 + 22))], amp=0.3, samples=6))
    body.append(pen.bezier([(140, 176), ((118, 182), (100, 196), (98, 222)), ((96, 250), (108, 284), (124, 306))], amp=0.35))
    for y0 in (236, 268):
        body.append(pen.bezier([(99, y0), ((110, y0 + 4), (118, y0 + 12), (124, y0 + 22))], amp=0.25, samples=5))
    # a few sky-blue veins
    body.append(p(pen.bezier([(236, 190), ((250, 230), (248, 280), (232, 330))], amp=0.4), stroke=SKY, stroke_width="1"))

    paths = "".join(b if b.startswith("<") else p(b) for b in body)
    return svg("0 0 320 400", paths, "Engraving-style anatomical heart", extra_defs="".join(defs))


# ---------------------------------------------------------------------------
# Small scattered accents
# ---------------------------------------------------------------------------


def stethoscope() -> str:
    pen = Pen(59)
    b = []
    # ear tips and binaurals converging into a Y
    b.append(pen.circle(46, 16, 4))
    b.append(pen.circle(94, 16, 4))
    b.append(pen.bezier([(48, 20), ((50, 44), (58, 62), (68, 76))], amp=0.3))
    b.append(pen.bezier([(92, 20), ((90, 44), (82, 62), (72, 76))], amp=0.3))
    b.append(pen.bezier([(44, 20), ((46, 46), (56, 64), (66, 79))], amp=0.3))
    b.append(pen.bezier([(96, 20), ((94, 46), (84, 64), (74, 79))], amp=0.3))
    b.append(pen.line(56, 44, 84, 44, amp=0.2))  # tension spring
    b.append(pen.rect(66, 76, 8, 10, amp=0.15))
    # tubing looping down to the chest piece
    b.append(pen.bezier([(70, 86), ((70, 130), (30, 140), (32, 112)), ((34, 90), (74, 96), (96, 118)), ((104, 126), (110, 130), (114, 128))], amp=0.35))
    b.append(pen.bezier([(73, 86), ((74, 128), (36, 136), (36, 112)), ((38, 94), (72, 100), (94, 121))], amp=0.3))
    # chest piece
    b.append(pen.circle(126, 132, 17))
    b.append(pen.circle(126, 132, 11.5))
    b.append(pen.circle(126, 132, 2))
    defs = '<clipPath id="st-bell"><path d="M109 132a17 17 0 1 0 34 0a17 17 0 1 0 -34 0Z"/></clipPath>'
    b.append(f'<g clip-path="url(#st-bell)" stroke="{SKY}" stroke-width="1">{p(pen.arcs(126, 132, 12.5, 16.5, 1.6, 20, 160))}</g>')
    paths = "".join(x if x.startswith("<") else p(x) for x in b)
    return svg("0 0 160 160", paths, "Stethoscope", extra_defs=defs)


def pill_leaf() -> str:
    pen = Pen(71)
    b = []
    # capsule, drawn tilted
    cx, cy, L, r, rot = 58, 70, 30, 14, -28
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))

    def T(x, y):
        return (cx + x * c - y * s, cy + x * s + y * c)

    cap = [T(-L, -r), T(L, -r)]
    b.append(pen.line(*cap[0], *cap[1]))
    b.append(pen.line(*T(L, r), *T(-L, r)))
    arc_r = [T(L + r * math.cos(t), r * math.sin(t)) for t in [math.radians(a) for a in range(-90, 91, 15)]]
    arc_l = [T(-L - r * math.cos(t), -r * math.sin(t)) for t in [math.radians(a) for a in range(-90, 91, 15)]]
    b.append(pen.smooth(pen.jitter(arc_r, 0.2)))
    b.append(pen.smooth(pen.jitter(arc_l, 0.2)))
    b.append(pen.line(*T(0, -r), *T(0, r), amp=0.2))
    half = " ".join(f"{fmt(x)},{fmt(y)}" for x, y in [T(0, -r), T(L, -r), T(L + r, 0), T(L, r), T(0, r)])
    defs = f'<clipPath id="pl-half"><polygon points="{half}"/></clipPath>'
    b.append(f'<g clip-path="url(#pl-half)" stroke="{SKY}" stroke-width="1">{p(pen.hatch(40, 40, 110, 100, 62, 3))}</g>')
    b.append(pen.bezier([T(-L + 2, -r + 5), ((T(-L + 8, -r + 3)), T(-10, -r + 3), T(-4, -r + 4))], amp=0.15, samples=4))
    # leaf sprig
    stem = [(96, 104), ((110, 84), (122, 60), (126, 26))]
    b.append(pen.bezier(stem, amp=0.3))
    for (sx, sy, side, size) in ((106, 88, -1, 20), (112, 76, 1, 22), (118, 58, -1, 20), (122, 44, 1, 17), (126, 26, 0, 12)):
        ang = math.radians(-60 if side == 1 else (-120 if side == -1 else -90))
        tip = (sx + size * math.cos(ang) * (1 if side else 0.2) + (6 * side if side else 0), sy + size * math.sin(ang))
        if side == 0:
            tip = (sx + 2, sy - size)
        mx, my = (sx + tip[0]) / 2, (sy + tip[1]) / 2
        nx, ny = -(tip[1] - sy), tip[0] - sx
        n = math.hypot(nx, ny) or 1
        w = size * 0.32
        c1 = (mx + nx / n * w, my + ny / n * w)
        c2 = (mx - nx / n * w, my - ny / n * w)
        b.append(pen.bezier([(sx, sy), (c1, c1, tip)], amp=0.2, samples=6))
        b.append(pen.bezier([(sx, sy), (c2, c2, tip)], amp=0.2, samples=6))
        b.append(pen.line(sx, sy, tip[0] * 0.85 + sx * 0.15, tip[1] * 0.85 + sy * 0.15, amp=0.15))
    paths = "".join(x if x.startswith("<") else p(x) for x in b)
    return svg("0 0 160 120", paths, "Capsule with a leaf sprig", extra_defs=defs)


def microscope() -> str:
    pen = Pen(83)
    b = []
    # base
    b.append(pen.poly([(22, 160), (118, 160), (112, 148), (28, 148)], closed=True, step=12))
    b.append(pen.line(18, 164, 122, 164))
    # pillar + arm (C shaped)
    b.append(pen.bezier([(92, 148), ((106, 120), (108, 86), (90, 62))]))
    b.append(pen.bezier([(80, 148), ((94, 122), (94, 92), (80, 70))]))
    arm_d = "M92 148C106 120 108 86 90 62L80 70C94 92 94 122 80 148Z"
    defs = f'<clipPath id="mi-arm"><path d="{arm_d}"/></clipPath>'
    b.append(f'<g clip-path="url(#mi-arm)" stroke="{SKY}" stroke-width="1">{p(pen.hatch(78, 60, 110, 150, 20, 3))}</g>')
    # stage + clips
    b.append(pen.rect(34, 112, 56, 6))
    b.append(pen.line(40, 110, 54, 110, amp=0.15))
    b.append(pen.line(70, 110, 84, 110, amp=0.15))
    b.append(pen.ellipse(62, 132, 10, 3.2, amp=0.2))  # mirror
    b.append(pen.line(62, 118, 62, 128, amp=0.1))
    # tube, tilted
    tube = [(40, 30), (58, 22), (80, 78), (62, 86)]
    b.append(pen.poly(tube, closed=True, step=10))
    b.append(pen.poly([(36, 20), (54, 12), (58, 22), (40, 30)], closed=True, step=6, amp=0.25))  # eyepiece
    b.append(pen.poly([(64, 86), (76, 81), (74, 96), (68, 98)], closed=True, step=6, amp=0.2))  # objective
    b.append(pen.line(47, 42, 65, 35, amp=0.15))
    b.append(pen.line(54, 58, 72, 51, amp=0.15))
    # focus knob
    b.append(pen.circle(98, 98, 8))
    b.append(pen.circle(98, 98, 3))
    paths = "".join(x if x.startswith("<") else p(x) for x in b)
    return svg("0 0 140 180", paths, "Microscope", extra_defs=defs)


# ---------------------------------------------------------------------------
# Network chain with (b,R,q) envelopes
# ---------------------------------------------------------------------------

GLYPHS = {
    # tiny stroke glyphs on a 0..6 wide, 0..10 tall grid (baseline y=8)
    "(": [[(4, 0), (2, 3), (2, 7), (4, 10)]],
    ")": [[(1, 0), (3, 3), (3, 7), (1, 10)]],
    ",": [[(2, 8), (2, 9), (1, 10.5)]],
    "b": [[(1, 0), (1, 8)], [(1, 5), (3, 3.8), (5, 5), (5, 7), (3, 8.2), (1, 7)]],
    "R": [[(1, 8), (1, 0), (4, 0), (5, 1.5), (5, 2.8), (4, 4.2), (1, 4.2)], [(3, 4.2), (5.2, 8)]],
    "q": [[(5, 3.4), (5, 10.5)], [(5, 5), (3, 3.8), (1, 5), (1, 7), (3, 8.2), (5, 7)]],
}
ADV = {"(": 5, ")": 5, ",": 3.5, "b": 6.5, "R": 6.5, "q": 6.5}


def glyph_text(text: str, x: float, y: float, scale: float = 0.9) -> str:
    d = []
    cx = x
    for ch in text:
        for stroke in GLYPHS[ch]:
            pts = [(cx + gx * scale, y + gy * scale) for gx, gy in stroke]
            d.append(Pen.smooth(pts) if len(pts) > 2 else f"M{fmt(pts[0][0])} {fmt(pts[0][1])}L{fmt(pts[1][0])} {fmt(pts[1][1])}")
        cx += ADV[ch] * scale
    return "".join(d)


def network_chain() -> str:
    pen = Pen(97)
    b = []
    nodes = [(48, 92), (180, 52), (312, 92)]
    chain = pen.bezier([nodes[0], ((80, 60), (130, 44), nodes[1]), ((230, 44), (280, 60), nodes[2])], amp=0.3, samples=14)
    b.append(p(chain, stroke_dasharray="6 5"))
    for i, (x, y) in enumerate(nodes):
        b.append(p(pen.circle(x, y, 16)))
        b.append(p(pen.circle(x, y, 11.5, amp=0.3), stroke=SKY, stroke_width="1"))
        b.append(p(cross(pen, x, y, 5.5, 3.4), stroke_width="1.2"))
    # envelopes riding on each link, lightly rotated to follow the curve
    for (ex, ey, rot) in ((112, 58, -14), (248, 58, 14)):
        env = pen.rect(-10, -7, 20, 14, amp=0.2) + pen.poly([(-10, -7), (0, 1), (10, -7)], step=6, amp=0.15)
        label = glyph_text("(b,R,q)", -17, -22, scale=0.95)
        b.append(f'<g transform="translate({ex} {ey}) rotate({rot})">{p(env)}{p(label, stroke_width="1")}</g>')
    return svg("0 0 360 130", "".join(b), "Three nodes joined by a dashed chain carrying (b,R,q) envelopes")


# ---------------------------------------------------------------------------
# Accent blobs (the only filled shapes) and the organic divider
# ---------------------------------------------------------------------------


def blob(seed: int, color: str, title: str, lobes: int = 7, wobble: float = 0.16, stretch: tuple = (1.0, 1.0)) -> str:
    rng = random.Random(seed)
    n = 14
    phase = rng.uniform(0, math.pi * 2)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        r = 150 * (1 + wobble * math.sin(lobes * t / 2 + phase) + rng.uniform(-0.06, 0.06))
        pts.append((200 + r * math.cos(t) * stretch[0], 200 + r * math.sin(t) * stretch[1]))
    d = Pen.smooth(pts, closed=True)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" role="img" aria-label="{title}">'
        f'<title>{title}</title><path d="{d}" fill="{color}"/></svg>\n'
    )


def wave_divider_path(w: int = 1440, h: int = 200) -> str:
    """Closed organic curve: wavy top edge, filled down to the bottom edge."""
    pts = [(x, 96 + 46 * math.sin(x / 210 + 0.4) + 9 * math.sin(x / 83 + 0.8)) for x in range(0, w + 1, 24)]
    top = Pen.smooth(pts)
    return f"{top}L{w} {h}L0 {h}Z"


def wave_divider() -> str:
    """Section divider: solid fill in the colour of the section below (white by default)."""
    w, h = 1440, 200
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
        f'aria-label="Organic wave section divider"><title>Organic wave section divider</title>'
        f'<path d="{wave_divider_path(w, h)}" fill="{WHITE}"/></svg>\n'
    )


def favicon() -> str:
    heart = "M16 27C9 22 3.5 17 3.5 11.2C3.5 7.6 6.3 5 9.6 5C12.4 5 14.6 6.8 16 9.2C17.4 6.8 19.6 5 22.4 5C25.7 5 28.5 7.6 28.5 11.2C28.5 17 23 22 16 27Z"
    pulse = "M5 16H11.2L13.2 12.2L16 20.2L18.6 13.6L20.2 16H27"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none" stroke="{INDIGO}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"><title>MedChain</title><path d="{heart}"/>'
        f'<path d="{pulse}" stroke-width="1.8"/></svg>\n'
    )


FILES = {
    "hospital-building.svg": hospital_large,
    "hospital-building-md.svg": hospital_medium,
    "hospital-building-sm.svg": hospital_small,
    "ecg-wave.svg": ecg_wave,
    "heart-anatomical.svg": heart_anatomical,
    "stethoscope.svg": stethoscope,
    "pill-leaf.svg": pill_leaf,
    "microscope.svg": microscope,
    "network-chain.svg": network_chain,
    "blob-gold.svg": lambda: blob(3, GOLD, "Honey-gold accent blob", lobes=6),
    "blob-sage.svg": lambda: blob(8, SAGE, "Mint-sage accent blob", lobes=5, stretch=(0.9, 1.05)),
    "blob-orange.svg": lambda: blob(13, ORANGE, "Burnt-orange accent blob", lobes=7, wobble=0.13),
    "wave-divider.svg": wave_divider,
    "favicon.svg": favicon,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in FILES.items():
        (OUT / name).write_text(fn())
        print(f"wrote assets/svg/{name}")


if __name__ == "__main__":
    main()
