#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""位图 -> 矢量描摹（输出毫米坐标 SVG + 清单）。

这是「图片 + 尺寸」模式下最常用的一段：把 PNG/JPG 参考图拆成若干内容区块，
逐块描摹成 SVG，坐标直接标定为页面毫米，供 CorelDRAW 导入即为目标尺寸。

依赖: numpy, opencv-python, pillow, potracer
    python -m pip install numpy opencv-python pillow potracer

--------------------------------------------------------------------------
关键坑（都在实现里体现了，改动前先读）
--------------------------------------------------------------------------
1. potracer 的 Bitmap 构造器内部会执行一次 invert()，它约定的输入是
   「True = 白色」。所以要描摹黑色前景时，必须传 ~fg（取反），
   否则描摹的是背景，成图会整体反相。
2. potracer 未实现 curves_tree，所有轮廓都是顶层，没有父子包含关系。
   因此 SVG 必须用 fill-rule="evenodd"，靠奇偶规则还原镂空；
   不要指望 nonzero。
3. 子路径之间必须用空格分隔（"Z M"）。拼成 "...ZM..." 虽然符合 SVG 规范，
   但 CorelDRAW X8 的 SVG 解析器会误解，产生桥接三角形伪影。
4. 小尺寸图形（如 8mm 宽的图标）直接在原分辨率二值化描摹会变形——
   细笔画被简化成折线、平顶被圆滑成尖拱。解决：先把**灰度**做 LANCZOS
   上采样（推荐 4x）再二值化，能显著提升细部保真。
5. alphamax 控制拐角判定：1.0 是最大平滑（会把直角磨圆），
   0.0 保留硬拐角。图标类内容用 0.0~0.5。
6. turdsize 是「丢弃面积小于该值的斑点」，单位是**位图像素**。
   一旦上采样 U 倍，面积变为 U² 倍，所以 turdsize 必须同步乘以 U²，
   否则会误删细节。
7. 反色区域（深底上的白字/白图）要剔除接触裁剪边界的白色连通域，
   否则会把区块外的白底也算成前景，描出一圈巨大轮廓。
8. 源图常见扫描/裁切残留（如最左侧 4px 纯黑边条、边缘灰线）。
   标定前先探测边缘，必要时用 --crop-left 之类裁掉，不要当成设计内容复刻。
"""

import argparse
import json
import os
import re
import sys

import cv2
import numpy as np
import potrace
from PIL import Image

# ----------------------------------------------------------------------------
# 坐标与掩膜
# ----------------------------------------------------------------------------


class Source:
    """参考图 + 页面标定。"""

    def __init__(self, path, page_w_mm, page_h_mm=None):
        self.path = path
        self.gray = np.array(Image.open(path).convert("L"))
        self.img_h, self.img_w = self.gray.shape
        self.page_w_mm = float(page_w_mm)
        self.mm_per_px = self.page_w_mm / self.img_w
        self.page_h_mm = (float(page_h_mm) if page_h_mm
                          else self.img_h * self.mm_per_px)
        self.page_h_mm_derived = self.img_h * self.mm_per_px

    def describe(self):
        lines = [
            f"参考图      : {self.path}",
            f"像素尺寸    : {self.img_w} x {self.img_h}",
            f"标定        : 1 px = {self.mm_per_px:.6f} mm（按宽度 {self.page_w_mm:g} mm 标定）",
            f"页面高度    : {self.page_h_mm:g} mm"
            + ("" if abs(self.page_h_mm - self.page_h_mm_derived) < 1e-6
               else f"（图比例推导值 {self.page_h_mm_derived:.3f} mm）"),
            f"图宽高比    : {self.img_w / self.img_h:.4f}",
        ]
        if abs(self.page_h_mm - self.page_h_mm_derived) > 0.5:
            lines.append("注意        : 图比例与给定页面尺寸不一致，"
                         "纵向内容会与页面高度产生偏差，需在报告中说明。")
        return "\n".join(lines)


def probe_edges(src):
    """探测源图四周是否有扫描/裁切残留暗边，返回报告字典。"""
    g = src.gray
    rep = {}
    for side, line in (("left", g[:, 0]), ("right", g[:, -1]),
                       ("top", g[0, :]), ("bottom", g[-1, :])):
        rep[side] = {"mean": int(line.mean()), "dark": bool(line.mean() < 128)}
    # 左侧连续暗列计数（最常见的残留形态）
    n = 0
    for c in range(g.shape[1]):
        if g[:, c].mean() < 128:
            n += 1
        else:
            break
    rep["left_dark_cols"] = n
    return rep


def region_mask(src, y0, y1, x0, x1, invert=False, upscale=1, threshold=128):
    """取区域灰度（可上采样）并二值化。返回 True = 要描摹的墨迹。

    threshold 越低，算作墨迹的像素越少 → 笔画越细；越高越粗。
    细笔画文字（6pt 级）对阈值很敏感，值得用 --probe 扫一遍。
    """
    sub = src.gray[y0:y1, x0:x1]
    if upscale > 1:
        sub = np.array(Image.fromarray(sub).resize(
            (sub.shape[1] * upscale, sub.shape[0] * upscale), Image.LANCZOS))
    fg = (sub > threshold) if invert else (sub < threshold)
    if invert:
        # 剔除接触裁剪边界的白色连通域，避免把区块外的白底算成前景
        _, labels = cv2.connectedComponents(fg.astype(np.uint8), connectivity=4)
        edge = (set(labels[0, :]) | set(labels[-1, :])
                | set(labels[:, 0]) | set(labels[:, -1]))
        for lb in edge:
            if lb != 0:
                fg[labels == lb] = False
    return fg


# ----------------------------------------------------------------------------
# 描摹
# ----------------------------------------------------------------------------


def _xy(p):
    try:
        return float(p.x), float(p.y)
    except AttributeError:
        return float(p[0]), float(p[1])


def _collect(curve, ox, oy, s, subs):
    px, py = _xy(curve.start_point)
    d = [f"M{(px + ox) * s:.4f},{(py + oy) * s:.4f}"]
    for seg in curve.segments:
        ex, ey = _xy(seg.end_point)
        x, y = (ex + ox) * s, (ey + oy) * s
        if seg.is_corner:
            d.append(f"L{x:.4f},{y:.4f}")
        else:
            c1x, c1y = _xy(seg.c1)
            c2x, c2y = _xy(seg.c2)
            d.append(f"C{(c1x + ox) * s:.4f},{(c1y + oy) * s:.4f} "
                     f"{(c2x + ox) * s:.4f},{(c2y + oy) * s:.4f} {x:.4f},{y:.4f}")
    d.append("Z")
    subs.append("".join(d))
    for child in (curve.children or []):
        _collect(child, ox, oy, s, subs)


def mask_to_svg(fg, mm_per_px, ox_px, oy_px, turdsize, alphamax, opttolerance):
    """掩膜 -> SVG 文本。ox_px/oy_px 为该掩膜左上角在整页像素坐标中的原点。

    包围盒直接由前景掩膜算，精确可靠；SVG 的 width/height/viewBox 全部取自它，
    这样 CorelDRAW 导入后尺寸天然正确。
    """
    path = potrace.Bitmap(~fg).trace(          # 坑 1：必须取反
        turdsize=turdsize, alphamax=alphamax,
        opticurve=1, opttolerance=opttolerance)
    subs = []
    for curve in path.curves:
        _collect(curve, ox_px, oy_px, mm_per_px, subs)

    ys, xs = np.where(fg)
    mx0 = (xs.min() + ox_px) * mm_per_px
    my0 = (ys.min() + oy_px) * mm_per_px
    mx1 = (xs.max() + 1 + ox_px) * mm_per_px
    my1 = (ys.max() + 1 + oy_px) * mm_per_px
    wmm, hmm = mx1 - mx0, my1 - my0

    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{wmm:.4f}mm" height="{hmm:.4f}mm" '
        f'viewBox="{mx0:.4f} {my0:.4f} {wmm:.4f} {hmm:.4f}">\n'
        # 坑 2/3：evenodd 还原镂空；子路径用空格分隔
        f'<path fill="#111111" fill-rule="evenodd" d="{" ".join(subs)}"/>\n'
        "</svg>\n"
    )
    return svg, subs, (mx0, my0, mx1, my1)


# ----------------------------------------------------------------------------
# 参数评估（本地光栅化回源网格比对，不需要 CorelDRAW）
# ----------------------------------------------------------------------------


def _flatten_cubic(p0, c1, c2, p1, steps=16):
    t = np.linspace(0.0, 1.0, steps, endpoint=False)[:, None]
    mt = 1.0 - t
    return ((mt ** 3) * p0 + 3 * (mt ** 2) * t * c1
            + 3 * mt * (t ** 2) * c2 + (t ** 3) * p1)


_TOKEN = re.compile(r"[MLCZmlcz]|-?\d*\.?\d+(?:e-?\d+)?")


def _subpaths(d):
    """把 path d 解析成折线列表（只支持本脚本生成的 M/L/C/Z）。"""
    toks = _TOKEN.findall(d)
    subs, cur, i, n = [], None, 0, len(toks)
    while i < n:
        t = toks[i]
        if t in "Mm":
            i += 1
            if cur and len(cur) >= 3:
                subs.append(np.array(cur, np.float32))
            cur = [(float(toks[i]), float(toks[i + 1]))]
            i += 2
        elif t in "Ll":
            i += 1
            cur.append((float(toks[i]), float(toks[i + 1])))
            i += 2
        elif t in "Cc":
            i += 1
            c1 = np.array((float(toks[i]), float(toks[i + 1])), np.float32)
            c2 = np.array((float(toks[i + 2]), float(toks[i + 3])), np.float32)
            e = np.array((float(toks[i + 4]), float(toks[i + 5])), np.float32)
            i += 6
            cur.extend(map(tuple, _flatten_cubic(
                np.array(cur[-1], np.float32), c1, c2, e)))
            cur.append((float(e[0]), float(e[1])))
        elif t in "Zz":
            i += 1
            if cur and len(cur) >= 3:
                subs.append(np.array(cur, np.float32))
            cur = []
        else:
            i += 1
    if cur and len(cur) >= 3:
        subs.append(np.array(cur, np.float32))
    return subs


def rasterize(svg_text, out_w, out_h, sub_px, ox, oy, ss=8):
    """把 SVG 光栅化到源像素网格。even-odd 填充 == 各子路径覆盖区异或。

    ss>1 时先按 ss 倍超采样，再盒式降采样得到**覆盖率**（0..1）而非硬边布尔。

    **必须用超采样，不要退回硬边**：`cv2.fillPoly` 是"取整后填充"，而且把
    多边形左右两个端点列**都**算作填充，于是每条扫描线多出 1 个像素——
    即渲染结果比真实几何**粗 1 个像素**（不是细）。这个恒定加粗造成的偏差方向
    是固定的：**笔画越粗，硬边 IoU 越高**（1px 宽 IoU=1/2，10px 宽 IoU=10/11，
    40px 宽 IoU=40/41），于是寻优会一路爬到最粗的阈值。实测验证：

        真实宽度   硬边渲染宽度   硬边 IoU%   超采样渲染宽度   超采样 IoU%
        1          2              50.00       1                100.00
        3          4              75.00       3                100.00
        10         11             90.91       10               100.00
        40         41             97.56       40               100.00

    真实数据上的表现（band 反白字标，U=8）：

        阈值   硬边 IoU%   超采样 IoU%   超采样面积比
        128     94.15       99.40        1.005
        138     94.29       99.51        1.000   <- 真峰
        148     94.34(最高) 99.35        0.996

    硬边把最优判成 148（过粗），超采样判成 138（正确）；而 CorelDRAW 实际导出
    比对是 99.15%，说明超采样才贴近真实。

    **残留偏差**：超采样把"每扫描线多 1 个**超采样**像素"折算成 +1/ss 个输出
    像素（ss=8 时 +0.125px，ss=1 时 +1.0px），方向仍是偏粗但已小到不影响阈值
    选择。所以本函数的输出适合用来**选阈值**，不要当成精确面积；
    要精确面积请按远高于源图密度的网格（如 60 px/mm）另行光栅化。
    """
    m = re.search(r'viewBox="([^"]+)"', svg_text)
    vx, vy = (float(v) for v in m.group(1).split()[:2])
    W, H = out_w * ss, out_h * ss
    acc = np.zeros((H, W), np.uint8)
    for poly in _subpaths(re.search(r'\sd="([^"]+)"', svg_text).group(1)):
        q = poly.copy()
        q[:, 0] = (q[:, 0] - vx + ox) * sub_px * ss
        q[:, 1] = (q[:, 1] - vy + oy) * sub_px * ss
        # 只为该子路径分配它自己的包围盒画布，再异或回总画布。
        # 这步不能省：整幅分配的话，ss=8 下一个 483 子路径的区域要
        # 483 × (5288 × 2592) ≈ 66 亿次像素操作，根本跑不完；
        # 局部化后绝大多数子路径只占几十像素见方，代价降几个数量级。
        bx0 = max(0, int(np.floor(q[:, 0].min())))
        by0 = max(0, int(np.floor(q[:, 1].min())))
        bx1 = min(W, int(np.ceil(q[:, 0].max())) + 1)
        by1 = min(H, int(np.ceil(q[:, 1].max())) + 1)
        if bx1 <= bx0 or by1 <= by0:
            continue
        lay = np.zeros((by1 - by0, bx1 - bx0), np.uint8)
        qq = np.round(q).astype(np.int32)
        qq[:, 0] -= bx0
        qq[:, 1] -= by0
        cv2.fillPoly(lay, [qq], 1)
        acc[by0:by1, bx0:bx1] ^= lay
    if ss == 1:
        return acc.astype(bool)
    return acc.reshape(out_h, ss, out_w, ss).mean(axis=(1, 3))


def svg_viewbox(svg_text):
    m = re.search(r'viewBox="([^"]+)"', svg_text)
    return [float(v) for v in m.group(1).split()]


def evaluate(src, y0, y1, x0, x1, invert, upscale, turdsize, alphamax,
             opttolerance, threshold=128):
    """描摹后光栅化回**源像素网格**，与该区域二值掩膜比对，得到保真指标。"""
    fg = region_mask(src, y0, y1, x0, x1, invert, upscale, threshold)
    svg, subs, _ = mask_to_svg(fg, src.mm_per_px / upscale, x0 * upscale,
                               y0 * upscale, turdsize, alphamax, opttolerance)
    ref = region_mask(src, y0, y1, x0, x1, invert, 1)
    vx, vy = svg_viewbox(svg)[:2]
    cov = rasterize(svg, x1 - x0, y1 - y0, 1.0 / src.mm_per_px,
                    vx - x0 * src.mm_per_px, vy - y0 * src.mm_per_px, ss=8)
    acc = cov >= 0.5                      # 超采样覆盖率过半即算墨迹
    inter, union = (acc & ref).sum(), (acc | ref).sum()
    return {
        "iou": round(inter / union * 100, 2) if union else 100.0,
        "recall": round(inter / ref.sum() * 100, 2) if ref.sum() else 100.0,
        "precision": round(inter / acc.sum() * 100, 2) if acc.sum() else 100.0,
        "ink_ratio": round(acc.sum() / ref.sum(), 3) if ref.sum() else 1.0,
        "subpaths": len(subs),
        "svg": svg,
    }


# ----------------------------------------------------------------------------
# 自动分区（按行投影找内容带，再在带内按列投影找左右边界）
# ----------------------------------------------------------------------------


def auto_regions(src, min_gap_px=12, min_ink_px=6, invert_bands=False):
    ink = (src.gray < 128)
    rows = ink.sum(axis=1)
    bands, start = [], None
    for y, v in enumerate(rows):
        if v >= min_ink_px and start is None:
            start = y
        elif v < min_ink_px and start is not None:
            if y - start >= min_gap_px:
                bands.append((start, y))
            start = None
    if start is not None and len(rows) - start >= min_gap_px:
        bands.append((start, len(rows)))

    out = []
    for y0, y1 in bands:
        cols = ink[y0:y1, :].sum(axis=0)
        xs = np.where(cols > 0)[0]
        if xs.size == 0:
            continue
        out.append((int(xs.min()), int(y0), int(xs.max()) + 1, int(y1)))
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def parse_region(spec):
    """name:x0,y0,x1,y1[,invert]  ->  (name, dict)"""
    if ":" not in spec:
        raise ValueError(f"区域格式应为 name:x0,y0,x1,y1[,invert]，收到 {spec!r}")
    name, rest = spec.split(":", 1)
    parts = [p.strip() for p in rest.split(",")]
    if len(parts) < 4:
        raise ValueError(f"区域 {name} 缺少坐标")
    d = {"x0": int(parts[0]), "y0": int(parts[1]),
         "x1": int(parts[2]), "y1": int(parts[3])}
    d["invert"] = len(parts) > 4 and parts[4].lower() in ("1", "true", "invert", "yes")
    return name, d


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把位图参考图描摹为毫米坐标 SVG（含参数自检）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True, help="参考图路径（PNG/JPG）")
    ap.add_argument("--page-width", type=float, required=True,
                    help="页面宽度（毫米），用于标定 mm/px")
    ap.add_argument("--page-height", type=float, default=None,
                    help="页面高度（毫米）；省略则按图比例推导")
    ap.add_argument("--region", action="append", default=[],
                    help="区域定义 name:x0,y0,x1,y1[,invert]，可重复。"
                         "坐标是源图像素，左上原点")
    ap.add_argument("--auto", action="store_true",
                    help="自动按投影分割区域（--region 同时给出时以 --region 为准）")
    ap.add_argument("--out", default="svg", help="输出目录")
    ap.add_argument("--upscale", type=int, default=8,
                    help="描摹前灰度上采样倍数（默认 8）。细部保真的关键："
                         "实测 U=4 得 95.2%% / U=8 得 98.3%%。"
                         "超大区域可用 4 控制耗时")
    ap.add_argument("--turdsize", type=int, default=2,
                    help="斑点面积阈值（源图像素；内部按 upscale² 折算）")
    ap.add_argument("--alphamax", type=float, default=1.0,
                    help="拐角阈值 0..1.33。0 = 纯多边形（禁用平滑），"
                         "1.0 = 最大平滑。U≥8 时用 1.0 最安全；不要用 0")
    ap.add_argument("--opttolerance", type=float, default=0.1,
                    help="曲线优化容差；越大越简并")
    ap.add_argument("--threshold", type=int, default=128,
                    help="二值化阈值，默认 128。阈值越低笔画越细；"
                         "细笔画文字（6pt 级）建议用 --probe 扫 110~150")
    ap.add_argument("--probe", action="store_true",
                    help="参数扫描：对每个区域列出候选参数的 IoU，不写 SVG")
    ap.add_argument("--crop-left", type=int, default=0,
                    help="忽略源图最左侧 N 列（裁切/扫描残留）")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.image):
        print(f"找不到参考图: {args.image}", file=sys.stderr)
        return 1

    src = Source(args.image, args.page_width, args.page_height)
    print(src.describe())
    print()

    edges = probe_edges(src)
    if edges["left_dark_cols"] >= 2:
        print(f"[警告] 源图最左侧有 {edges['left_dark_cols']} 列连续暗边"
              f"（均值 {edges['left']['mean']}），疑似裁切/扫描残留，"
              f"建议 --crop-left {edges['left_dark_cols']} 且不要复刻为设计内容。")
    print()

    regions = []
    for spec in args.region:
        regions.append(parse_region(spec))
    if not regions:
        if not args.auto:
            print("未给出 --region 也未开启 --auto，无可处理区域。", file=sys.stderr)
            return 1
        for i, (x0, y0, x1, y1) in enumerate(auto_regions(src), 1):
            regions.append((f"band{i:02d}", {"x0": x0, "y0": y0,
                                             "x1": x1, "y1": y1, "invert": False}))
        print(f"自动分区得到 {len(regions)} 个区域：")
        for n, d in regions:
            print(f"  {n:10s} x {d['x0']:5d}..{d['x1']:5d}  "
                  f"y {d['y0']:5d}..{d['y1']:5d}")
        print()

    os.makedirs(args.out, exist_ok=True)

    if args.probe:
        print("参数扫描（U=上采样，ts=turdsize 折算后，thr=二值化阈值，"
              "am=alphamax，ot=opttolerance）")
        print("注意：alphamax=0 会让 potrace 输出纯多边形（圆角变折线）。"
              "它的 IoU 可能最高但视觉最差，务必同时看「曲线段」列。")
        print(f"{'region':12s} {'U':>2s} {'ts':>5s} {'thr':>4s} {'am':>5s} "
              f"{'ot':>5s} {'IoU%':>7s} {'召回%':>7s} {'精确%':>7s} "
              f"{'子路径':>6s} {'曲线段':>6s} {'面积比':>8s}")
        best = {}
        for name, r in regions:
            rows = []
            for U in (4, 8):
                for ts0 in (2, 3):
                    for thr in (112, 118, 128, 138, 148):
                        for am in (0.8, 1.0):
                            for ot in (0.1,):
                                ts = max(1, int(round(ts0 * U * U)))
                                m = evaluate(src, r["y0"], r["y1"], r["x0"],
                                             r["x1"], r["invert"], U, ts, am, ot,
                                             threshold=thr)
                                rows.append((m["iou"], U, ts, thr, am, ot, m))
            rows.sort(key=lambda t: -t[0])
            for iou, U, ts, thr, am, ot, m in rows[:8]:
                print(f"{name:12s} {U:2d} {ts:5d} {thr:4d} {am:4.1f} {ot:5.2f} "
                      f"{m['iou']:7.2f} {m['recall']:7.2f} "
                      f"{m['precision']:7.2f} {m['subpaths']:6d} "
                      f"{m['svg'].count('C'):6d} {m['ink_ratio']:8.3f}")
            # 只在能产生真实曲线的候选中选最优（排除 alphamax=0 的多边形退化）
            curved = [t for t in rows if t[6]["svg"].count("C") > 0]
            best[name] = curved[0] if curved else rows[0]
            print()
        print("=== 各区域最优（已排除 alphamax=0 的多边形退化）===")
        for name, (iou, U, ts, thr, am, ot, m) in best.items():
            print(f"  {name:12s} IoU {iou:6.2f}%  面积比 {m['ink_ratio']:.3f}   "
                  f"--upscale {U} --turdsize {max(1, ts // (U * U))} "
                  f"--threshold {thr} --alphamax {am} --opttolerance {ot}")
        with open(os.path.join(args.out, "best_params.json"), "w",
                  encoding="utf-8") as f:
            json.dump({k: {"upscale": v[1], "turdsize_source_px": max(1, v[2] // (v[1] ** 2)),
                           "turdsize_internal": v[2], "threshold": v[3],
                           "alphamax": v[4], "opttolerance": v[5],
                           "iou": v[0], "ink_ratio": v[6]["ink_ratio"],
                           "curve_segments": v[6]["svg"].count("C")}
                       for k, v in best.items()},
                      f, ensure_ascii=False, indent=2)
        return 0

    manifest = {
        "source_image": os.path.abspath(args.image),
        "page_mm": [src.page_w_mm, round(src.page_h_mm, 4)],
        "mm_per_px": round(src.mm_per_px, 8),
        "image_px": [src.img_w, src.img_h],
        "edge_probe": edges,
        "params": {"upscale": args.upscale, "turdsize": args.turdsize,
                   "alphamax": args.alphamax, "opttolerance": args.opttolerance},
        "regions": {},
    }

    print(f"{'region':12s} {'子路径':>6s}  {'包围盒 mm  x0..x1':>26s}  "
          f"{'y0..y1':>24s}  {'尺寸 mm':>16s}")
    for name, r in regions:
        x0, y0, x1, y1 = r["x0"], r["y0"], r["x1"], r["y1"]
        if args.crop_left:
            x0 = max(x0, args.crop_left)
        fg = region_mask(src, y0, y1, x0, x1, r["invert"], args.upscale,
                         args.threshold)
        if not fg.any():
            print(f"{name:12s} 掩膜为空，跳过")
            continue
        ts = max(1, args.turdsize * args.upscale ** 2)   # 坑 6
        svg, subs, (mx0, my0, mx1, my1) = mask_to_svg(
            fg, src.mm_per_px / args.upscale, x0 * args.upscale,
            y0 * args.upscale, ts, args.alphamax, args.opttolerance)
        path = os.path.join(args.out, f"{name}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        manifest["regions"][name] = {
            "svg": os.path.abspath(path),
            "bbox_mm": [round(mx0, 4), round(my0, 4), round(mx1, 4), round(my1, 4)],
            "size_mm": [round(mx1 - mx0, 4), round(my1 - my0, 4)],
            "invert": r["invert"],
            "source_box_px": [x0, y0, x1, y1],
            "subpaths": len(subs),
        }
        print(f"{name:12s} {len(subs):6d}  {mx0:11.3f}..{mx1:11.3f}  "
              f"{my0:11.3f}..{my1:11.3f}  "
              f"{mx1-mx0:6.3f} x {my1-my0:6.3f}")

    mp = os.path.join(args.out, "manifest.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n清单: {mp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
