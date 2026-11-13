#!/usr/bin/env python3
"""名片 · 图标生成器（纯标准库，无第三方依赖）

三种方向都能出图，改 CONCEPT 即可：
    stack  一叠    两张错位卡片，递一张给你
    tag    挂牌    竖牌带顶孔，挂在门口那块
    fold   折角    一张手写的小卡

设计空间固定为 100x100，再按目标尺寸缩放，4x4 超采样抗锯齿。
输出：android/res/mipmap-*/ic_launcher.png 与 icon-512.png
"""

import math
import os
import struct
import sys
import zlib

DESIGN = 100.0
SS = 4  # 每像素 4x4 子采样

# ---------------------------------------------------------------- 配色
INK = "#1C1B19"     # 墨黑
TEAL = "#0F6E56"    # 应用主色（深青）
RED = "#A32D2D"     # 印章红
WHITE = "#FFFFFF"
TILE = TEAL
CONCEPT = "cardline"

# ---------------------------------------------------------------- 形状


def _hex(c):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _round_rect(x, y, rx, ry, w, h, r):
    if x < rx or x > rx + w or y < ry or y > ry + h:
        return False
    cx = min(max(x, rx + r), rx + w - r)
    cy = min(max(y, ry + r), ry + h - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _circle(x, y, cx, cy, r):
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _tri(x, y, p1, p2, p3):
    def sign(a, b, c):
        return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])
    d1 = sign((x, y), p1, p2)
    d2 = sign((x, y), p2, p3)
    d3 = sign((x, y), p3, p1)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _poly(x, y, pts):
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def _rounded_poly(x, y, pts, r):
    """多边形 + 各顶点补圆 = 圆角多边形，省得手写贝塞尔。"""
    if _poly(x, y, pts):
        return True
    for px, py in pts:
        if _circle(x, y, px, py, r):
            return True
    return False


class Shape:
    def __init__(self, color, test, alpha=1.0, rot=0.0, pivot=(50.0, 50.0)):
        self.rgb = _hex(color)
        self.test = test
        self.alpha = alpha
        self.rot = rot
        self.pivot = pivot

    def hit(self, x, y):
        if self.rot:
            a = math.radians(-self.rot)
            px, py = self.pivot
            dx, dy = x - px, y - py
            x = px + dx * math.cos(a) - dy * math.sin(a)
            y = py + dx * math.sin(a) + dy * math.cos(a)
        return self.test(x, y)


def build(concept, tile):
    """返回按绘制顺序排列的形状列表。"""
    s = [Shape(tile, lambda x, y: _round_rect(x, y, 0, 0, 100, 100, 22.5))]

    if concept == "stack":
        # 后面那张：错位 + 微旋，压暗当阴影卡
        s.append(Shape(WHITE, lambda x, y: _round_rect(x, y, 17.75, 26.5, 52.5, 35, 6.25),
                       alpha=0.5, rot=-13))
        # 前面那张：正对
        s.append(Shape(WHITE, lambda x, y: _round_rect(x, y, 23.75, 32.5, 52.5, 35, 6.25)))

    elif concept == "tag":
        # 经典吊牌：圆角矩形，再用两个斜切三角把上方两角抹掉，靠近顶边打穿绳孔
        # 斜切角要够深、孔要够大，否则到 32px 就退化成一块白方块
        s.append(Shape(WHITE, lambda x, y: _round_rect(x, y, 27, 21, 46, 57, 4.5), rot=-5))
        s.append(Shape(tile, lambda x, y: _tri(x, y, (27, 21), (39, 21), (27, 33)), rot=-5))
        s.append(Shape(tile, lambda x, y: _tri(x, y, (73, 21), (61, 21), (73, 33)), rot=-5))
        s.append(Shape(tile, lambda x, y: _circle(x, y, 50, 30.5, 4.6), rot=-5))

    elif concept == "card":
        # 纯白横卡：名片比例 74:44 ≈ 1.68，撑满画面
        s.append(Shape(WHITE, lambda x, y: _round_rect(x, y, 13, 28, 74, 44, 5.5), rot=-6))

    elif concept == "carddot":
        # 横卡 + 左侧头像位圆孔
        s.append(Shape(WHITE, lambda x, y: _round_rect(x, y, 13, 28, 74, 44, 5.5), rot=-6))
        s.append(Shape(tile, lambda x, y: _circle(x, y, 25, 50, 7.2), rot=-6))

    elif concept == "cardline":
        # 横卡 + 两条"姓名 / 信息"横杠
        s.append(Shape(WHITE, lambda x, y: _round_rect(x, y, 13, 28, 74, 44, 5.5), rot=-6))
        s.append(Shape(tile, lambda x, y: _round_rect(x, y, 25, 39, 34, 6.5, 3.2), rot=-6))
        s.append(Shape(tile, lambda x, y: _round_rect(x, y, 25, 52, 22, 5, 2.5), rot=-6))

    elif concept == "fold":
        s.append(Shape(WHITE, lambda x, y: _round_rect(x, y, 27.5, 27.5, 45, 45, 10.6)))
        s.append(Shape(tile, lambda x, y: _tri(x, y, (72.5, 27.5), (72.5, 45), (55, 27.5))))

    else:
        raise SystemExit("未知方向: " + concept)
    return s


# ---------------------------------------------------------------- 渲染


def render(size, concept, tile):
    shapes = build(concept, tile)
    k = DESIGN / size
    rows = []
    inv = 1.0 / (SS * SS)
    for py in range(size):
        row = bytearray()
        for px in range(size):
            ar = ag = ab = aa = 0.0
            for sy in range(SS):
                y = (py + (sy + 0.5) / SS) * k
                for sx in range(SS):
                    x = (px + (sx + 0.5) / SS) * k
                    cr = cg = cb = ca = 0.0  # 预乘
                    for sh in shapes:
                        if sh.hit(x, y):
                            sr, sg, sb = sh.rgb[0] / 255.0, sh.rgb[1] / 255.0, sh.rgb[2] / 255.0
                            sa = sh.alpha
                            cr = sr * sa + cr * (1 - sa)
                            cg = sg * sa + cg * (1 - sa)
                            cb = sb * sa + cb * (1 - sa)
                            ca = sa + ca * (1 - sa)
                    ar += cr
                    ag += cg
                    ab += cb
                    aa += ca
            ar *= inv
            ag *= inv
            ab *= inv
            aa *= inv
            if aa <= 0:
                row += b"\x00\x00\x00\x00"
            else:
                row += bytes((int(round(ar / aa * 255)), int(round(ag / aa * 255)),
                              int(round(ab / aa * 255)), int(round(aa * 255))))
        rows.append(bytes(row))
    return rows


def write_png(path, rows, size):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)


DENSITIES = [("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)]


def main():
    concept = sys.argv[1] if len(sys.argv) > 1 else CONCEPT
    tile = sys.argv[2] if len(sys.argv) > 2 else TILE
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for name, px in DENSITIES:
        rows = render(px, concept, tile)
        for fn in ("ic_launcher.png", "ic_launcher_round.png"):
            p = os.path.join(here, "android", "res", "mipmap-" + name, fn)
            write_png(p, rows, px)
        print("  mipmap-%-8s %3dpx" % (name, px))

    rows = render(512, concept, tile)
    write_png(os.path.join(here, "icon-512.png"), rows, 512)
    print("  icon-512.png 512px")
    print("方向=%s  底色=%s" % (concept, tile))


if __name__ == "__main__":
    main()
