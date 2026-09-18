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
    """参考图 + 页面标定。

    palette 给定时（一组 (r,g,b)），可用 ink_gray() 得到"只含某一种颜色墨迹"
    的灰度图——**多色设计稿必须走这条路**：描摹管线本身只认灰度阈值，
    一张两色图直接描会把两种颜色糊成一个色。
    """

    def __init__(self, path, page_w_mm, page_h_mm=None, palette=None):
        self.path = path
        self.gray = np.array(Image.open(path).convert("L"))
        self.img_h, self.img_w = self.gray.shape
        self.page_w_mm = float(page_w_mm)
        self.mm_per_px = self.page_w_mm / self.img_w
        self.page_h_mm = (float(page_h_mm) if page_h_mm
                          else self.img_h * self.mm_per_px)
        self.page_h_mm_derived = self.img_h * self.mm_per_px
        self.palette = [tuple(int(v) for v in c) for c in (palette or [])]
        self._rgb = None
        self._cls = None
        # "不许描摹"的整图掩膜（原生分辨率）。由 text_exclusion() 从
        # 识别结果建出来，region_mask() 会自动应用它。
        self.exclude = None

    # -- 多色分离 ----------------------------------------------------------
    def _classify(self):
        """把每个像素归到某个调色板色（或背景）。返回 (标签图, 调色板)。

        **判据是"方向角"，不是 RGB 距离。** 这点很关键：抗锯齿产生的
        中性灰（黑字边缘）在 RGB 距离下离洋红比离黑更近——实测 50% 黑
        (155,155,155) 到洋红 120、到黑 173，会被误判成洋红，于是在洋红
        图层里沿黑字长出一圈灰晕。改成比"白→该色"的方向后：
        黑字边缘的 v=255-p 是中性方向，与"白→黑"几乎平行（夹角 1.8°），
        与"白→洋红"差 25°，归黑，不再漏到洋红。

        几何含义：p 是 c 与白的混合 ⟺ v=255-p 与 d=255-c 平行。
        所以方向角判据正好就是"这个像素是不是该色与白的混合"，
        比距离判据贴合得多，而且对压缩噪声不敏感。
        """
        if self._cls is None:
            if self._rgb is None:
                self._rgb = np.array(Image.open(self.path).convert("RGB"))
            pal = self.palette + [(255, 255, 255)]
            v = 255.0 - self._rgb.astype(np.float32)      # 从白指向该像素
            nv = np.linalg.norm(v, axis=2)
            bg = len(pal) - 1
            best = np.full(v.shape[:2], bg, np.int16)
            best_cos = np.full(v.shape[:2], -1.0, np.float32)
            for i, c in enumerate(pal[:bg]):
                d = 255.0 - np.asarray(c, np.float32)     # 白 -> 该色
                nd = float(np.linalg.norm(d))
                if nd < 1e-6:
                    continue
                cos = (v * d).sum(axis=2) / np.maximum(nv * nd, 1e-6)
                cos = np.where(nv < 1e-3, -1.0, cos)      # 纯白不归任何色
                # 夹角超过 45° 就不认：给"不属于任何声明色"的杂色留个出口，
                # 否则暗杂色会被硬塞给某个方向最接近的色、在图上多出墨点。
                cos = np.where(cos < 0.707, -1.0, cos)
                take = cos > best_cos
                best = np.where(take, i, best)
                best_cos = np.where(take, cos, best_cos)
            self._cls = (best, pal)
        return self._cls

    def ink_gray(self, rgb):
        """返回"只把该颜色的像素当墨迹"的灰度图（其余像素置白）。

        归属见 _classify()。两色互斥，所以同一块区域按两种颜色各描一次
        不会重复出图形；而且沿白↔色连线的混叠像素，其归属边界恰好落在
        覆盖率 50%，与二值化阈值 128 是同一点，上采样后的边缘抗锯齿
        照样保留，不会退化成硬边。
        """
        idx, pal = self._classify()
        want = tuple(int(v) for v in rgb)
        target = None
        for i, c in enumerate(pal):
            if c == want:
                target = i
                break
        if target is None:
            raise ValueError(f"颜色 {want} 不在调色板 {pal[:-1]} 里")
        return np.where(idx == target, self.gray, 255).astype(np.uint8)

    def ink_level(self, rgb):
        """该色**完全覆盖**时的灰度值（全图 1% 分位）。

        为什么不能用单区自己的分位：像 1.3mm 高的小字，一个像素都没被完全
        覆盖，区内最暗的灰度可能是"60% 覆盖"而不是"100% 覆盖"。拿它当基准
        会把覆盖率整体高估，阈值被系统性选低（实测小字区自估 75、全图真值 22）。
        所以基准必须**按颜色在全图统计**——同一种墨色在大色块里总有完全覆盖
        的像素，那个灰度才是该色的真值。
        """
        if not hasattr(self, "_ink_lvl"):
            self._ink_lvl = {}
        key = tuple(int(v) for v in rgb)
        if key not in self._ink_lvl:
            g = self.ink_gray(key)
            ink = g[g < 250]
            self._ink_lvl[key] = (float(np.percentile(ink, 1)) if len(ink)
                                  else 0.0)
        return self._ink_lvl[key]

    def coverage(self, rgb, y0=None, y1=None, x0=None, x1=None):
        """把灰度线性映射成**墨迹覆盖率** 0..1。

        源图是抗锯齿的：`gray = ink·g_ink + (1-ink)·255`，所以
        `覆盖率 = (255-gray)/(255-g_ink)`。这条式子是后面一切的地基——
        **二值化只是覆盖率在某个阈值处切一刀**，而"哪一刀对"取决于笔画宽度：
        粗笔画怎么切都差不多，1px 的细笔画切低了就断成碎片。

        物理上正确的切线是覆盖率 50% 处，即 `(g_ink+255)/2`；对实测的
        黑墨 g_ink=22 是 139、洋红 g_ink=98 是 177。
        """
        g = self.ink_gray(rgb)
        if y0 is not None:
            g = g[y0:y1, x0:x1]
        lvl = self.ink_level(rgb)
        if lvl >= 254:
            return np.zeros(g.shape, np.float32)
        return np.clip((255.0 - g.astype(np.float32)) / (255.0 - lvl), 0, 1)

    def palette_check(self, tol=12):
        """核对给定调色板与该色**实际墨色**是否一致，返回 (ok, 报告行)。

        踩过：多色稿的调色板是从"该色像素的中位 RGB"取的，而中位被抗锯齿
        边缘拉浅——实测黑字真值 `#1A1819`，中位给出 `#383637`。两者肉眼看
        都是"黑"，于是 CDR 里整片黑都填成了偏灰的色，**一路无人报错**。
        所以调色板必须与全图众数比对，偏了就要吵。
        """
        if self._rgb is None:
            self._rgb = np.array(Image.open(self.path).convert("RGB"))
        lines, ok = [], True
        for c in self.palette:
            g = self.ink_gray(c)
            ink = self._rgb[g < 250]
            if not len(ink):
                lines.append(f"  #{c[0]:02X}{c[1]:02X}{c[2]:02X}  图里找不到该色的像素")
                ok = False
                continue
            # 取该色像素的**众数**。不能用"最暗的一撮"：两色交界处的抗锯齿
            # 混色比两色都暗（实测洋红 #C62F7C 与黑 #1A1819 的 50% 混合是
            # #6A1F46，比两者都暗），按"最暗"取样会抓到交界而不是墨色本身。
            # 完全覆盖的像素数量远多于任何单个混色，所以众数才是墨色。
            vals, cnts = np.unique(ink, axis=0, return_counts=True)
            mode = tuple(int(v) for v in vals[cnts.argmax()])
            d = max(abs(mode[i] - c[i]) for i in range(3))
            flag = "OK " if d <= tol else "!! "
            if d > tol:
                ok = False
            lines.append(f"  {flag}#{c[0]:02X}{c[1]:02X}{c[2]:02X}  "
                         f"实测墨色 #{mode[0]:02X}{mode[1]:02X}{mode[2]:02X}  "
                         f"最大分量差 {d}"
                         + ("  ← 调色板偏了，CDR 会填错色" if d > tol else ""))
        return ok, lines

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


def region_mask(src, y0, y1, x0, x1, invert=False, upscale=1, threshold=128,
                gray=None, exclude=None):
    """取区域灰度（可上采样）并二值化。返回 True = 要描摹的墨迹。

    threshold 越低，算作墨迹的像素越少 → 笔画越细；越高越粗。
    细笔画文字（6pt 级）对阈值很敏感，值得用 --probe 扫一遍。

    gray 给定时用它代替 src.gray —— 多色图按 Source.ink_gray(颜色) 传进来，
    区域里就只剩该颜色的墨迹。

    `exclude`（默认取 `src.exclude`）是**整图原生分辨率的"不许描摹"掩膜**，
    由 `text_exclusion()` 从识别结果建出来。这是"先识别、再重绘"里
    关键的一刀：文字已由活字路径负责，留在这里就会被描成线——
    那正是"文字被描线"这个问题的来源。挖空后区域可能变空，
    调用方按"掩膜为空"跳过即可，不需要额外分支。
    """
    sub = (src.gray if gray is None else gray)[y0:y1, x0:x1]
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
    ex = src.exclude if exclude is None else exclude
    if ex is not None:
        e = ex[y0:y1, x0:x1]
        if upscale > 1:
            # 最近邻重复，保证"挖掉的范围"与原生像素网格严格对齐；
            # 这里用 LANCZOS 会引入半透明过渡，反而留下毛边
            e = np.repeat(np.repeat(e, upscale, axis=0), upscale, axis=1)
        fg = fg & ~e[:fg.shape[0], :fg.shape[1]]
    return fg


def text_exclusion(texts, shape, pad=2, include_suspect=False):
    """把识别出的文字框（含抗锯齿外沿）做成"不许描摹"的掩膜。

    `pad` 外扩是必须的：文字笔画外面还有一圈覆盖率 50% 上下的抗锯齿灰，
    按紧框挖会留下 1~2px 残边，描出来就是一圈毛刺。

    `suspect` 项默认**不挖**：它是 OCR 在线稿上凑出来的假文字
    （本图那个 `ft` 就在运输图标里面），按它挖会在图标上开个洞。
    """
    h, w = shape
    m = np.zeros((h, w), bool)
    for t in texts:
        if t.get('suspect') and not include_suspect:
            continue
        x0, y0, x1, y1 = [int(round(v)) for v in t['box']]
        m[max(0, y0 - pad):min(h, y1 + pad + 1),
          max(0, x0 - pad):min(w, x1 + pad + 1)] = True
    return m


# ----------------------------------------------------------------------------
# 描摹
# ----------------------------------------------------------------------------


def _xy(p):
    try:
        return float(p.x), float(p.y)
    except AttributeError:
        return float(p[0]), float(p[1])


def _collect(curve, ox, oy, s, subs):
    """把一条 potrace 曲线写成 SVG 子路径。

    **角点段必须输出两个 L，不是一。** potrace 的曲线表示里，段 j 的
    `c[1]` 是多边形顶点 `v_j`、`c[2]` 是**下一条边的中点**
    `mid(v_j, v_{j+1})`（见 potracer `_smooth`：`c[1]=vertex`、
    `c[2]=p4=interval(1/2, vertex[k], vertex[j])`）。所以一条角点段
    是"经过顶点、到下一个中点"的折线，写成一个 `L c[2]` 就等于
    **把顶点整个切掉**。

    为什么以前没暴露：potrace 的多边形顶点通常密到 ~1px 一个，
    切掉顶点只损失不到 1px，肉眼与 IoU 都看不出来。但**顶点稀疏的
    简单形状**（实心矩形只有 4 个顶点）会被切成菱形——实测一个
    174x217 的实心矩形，只写 `L c[2]` 时召回只剩 **75%**，
    且无论怎么调阈值都救不回来（这是几何错误，不是参数问题）。
    """
    px, py = _xy(curve.start_point)
    d = [f"M{(px + ox) * s:.4f},{(py + oy) * s:.4f}"]
    for seg in curve.segments:
        ex, ey = _xy(seg.end_point)
        x, y = (ex + ox) * s, (ey + oy) * s
        if seg.is_corner:
            vx, vy = _xy(seg.c)          # c[1] = 多边形顶点
            d.append(f"L{(vx + ox) * s:.4f},{(vy + oy) * s:.4f}")
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


def mask_to_svg(fg, mm_per_px, ox_px, oy_px, turdsize, alphamax, opttolerance,
                fill="#111111"):
    """掩膜 -> SVG 文本。ox_px/oy_px 为该掩膜左上角在整页像素坐标中的原点。

    包围盒直接由前景掩膜算，精确可靠；SVG 的 width/height/viewBox 全部取自它，
    这样 CorelDRAW 导入后尺寸天然正确。

    fill 决定导入 CorelDRAW 后的填充色——多色稿一个区域一种色，
    直接写在 SVG 里，省得导入后再逐个改。
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
        f'<path fill="{fill}" fill-rule="evenodd" d="{" ".join(subs)}"/>\n'
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
             opttolerance, threshold=128, gray=None, ink_level=None):
    """描摹后光栅化回**源像素网格**，与该区域二值掩膜比对，得到保真指标。

    返回两套分数，**用途完全不同**：

    `iou` / `recall` / `precision`
        描摹结果与"源图按 **128** 二值化"的掩膜比。注意这里的参考**固定 128**，
        与候选阈值无关，所以它天然偏向 128——**不能用它选阈值**（会一路选中
        128，把细笔画切碎）。留着是为了和历次报告口径一致。

    `soft`
        描摹结果（超采样覆盖率 0..1）与**源图覆盖率**（由灰度线性反推）的
        软 IoU。这是选阈值的依据：它用上了抗锯齿携带的亚像素信息，而不是
        把信息在二值化那一步丢掉。

    为什么必须用 `soft` 选阈值（实测，`www.daiion.com` 1.36mm 高）：
    源图是 145dpi，这行字只有 7.5px 高、笔画 1px。按 `iou` 选出的阈值 128
    把 `w` 的斜画切成碎片，渲染出来是 `ʍʍʍ dai ɔn com`；按 `soft` 选出 148
    （≈ 覆盖率 50% 的等值线）则笔画连通、可读。两者分数还正好相反：
    128 的 `iou` 最高（82.6%）而 `soft` 只有 59.4%，148 的 `soft` 最高（73.0%）。
    """
    fg = region_mask(src, y0, y1, x0, x1, invert, upscale, threshold, gray)
    svg, subs, _ = mask_to_svg(fg, src.mm_per_px / upscale, x0 * upscale,
                               y0 * upscale, turdsize, alphamax, opttolerance)
    ref = region_mask(src, y0, y1, x0, x1, invert, 1, gray=gray)
    vx, vy = svg_viewbox(svg)[:2]
    cov = rasterize(svg, x1 - x0, y1 - y0, 1.0 / src.mm_per_px,
                    vx - x0 * src.mm_per_px, vy - y0 * src.mm_per_px, ss=8)
    acc = cov >= 0.5                      # 超采样覆盖率过半即算墨迹
    inter, union = (acc & ref).sum(), (acc | ref).sum()

    # -- 覆盖率软 IoU ------------------------------------------------------
    # 源覆盖率与描摹覆盖率都在**原生源像素网格**上，无需重采样即可逐像素比。
    g = gray if gray is not None else src.gray
    sub = g[y0:y1, x0:x1].astype(np.float32)
    if ink_level is None:
        ink = sub[sub < 250]
        ink_level = float(np.percentile(ink, 1)) if len(ink) else 0.0
    if ink_level >= 254:
        soft = 100.0 if not acc.any() else 0.0
    else:
        cov_src = np.clip((255.0 - sub) / (255.0 - ink_level), 0, 1)
        lo = np.minimum(cov_src, cov).sum()
        hi = np.maximum(cov_src, cov).sum()
        soft = float(lo / hi * 100) if hi else 100.0

    return {
        "iou": round(inter / union * 100, 2) if union else 100.0,
        "recall": round(inter / ref.sum() * 100, 2) if ref.sum() else 100.0,
        "precision": round(inter / acc.sum() * 100, 2) if acc.sum() else 100.0,
        "ink_ratio": round(acc.sum() / ref.sum(), 3) if ref.sum() else 1.0,
        "soft": round(soft, 2),
        "ink_level": round(float(ink_level), 1),
        "coverage50": round((float(ink_level) + 255.0) / 2, 1),
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


def hex_rgb(spec):
    """'#RRGGBB' 或 'RRGGBB' -> (r, g, b)。"""
    c = str(spec).strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        raise ValueError(f"颜色应为 #RRGGBB 或 #RGB，收到 {spec!r}")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def parse_region(spec):
    """name:x0,y0,x1,y1[,invert][,#RRGGBB]  ->  (name, dict)

    末尾可给一个颜色。多色设计稿**必须**给：该颜色既用来从彩色原图里
    挑出"本区域只描这一色"的墨迹（Source.ink_gray），也决定导入
    CorelDRAW 后的填充色。不给就退回"灰度阈值 + 默认深灰填充"的老行为。
    """
    if ":" not in spec:
        raise ValueError(f"区域格式应为 name:x0,y0,x1,y1[,invert][,#RRGGBB]，"
                         f"收到 {spec!r}")
    name, rest = spec.split(":", 1)
    parts = [p.strip() for p in rest.split(",")]
    if len(parts) < 4:
        raise ValueError(f"区域 {name} 缺少坐标")
    d = {"x0": int(parts[0]), "y0": int(parts[1]),
         "x1": int(parts[2]), "y1": int(parts[3])}
    d["invert"] = False
    d["fill"] = None
    for p in parts[4:]:
        if not p:
            continue
        # 颜色一律要带 '#'：否则 '128' 这种三位数会被当成 #112288 的简写，
        # 而它更可能是手滑写进来的数值。
        if p.startswith("#"):
            d["fill"] = hex_rgb(p)
        elif p.lower() in ("1", "true", "invert", "yes"):
            d["invert"] = True
        else:
            raise ValueError(f"区域 {name} 的第 5 段起只能是 invert 或 #RRGGBB，"
                             f"收到 {p!r}")
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
                    help="区域定义 name:x0,y0,x1,y1[,invert][,#RRGGBB]，可重复。"
                         "坐标是源图像素，左上原点。**多色稿必须给颜色**："
                         "同一块区域按两种颜色各写一条，即按色分层描摹")
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

    # 多色稿：把各区域声明的颜色汇成调色板，之后按色分离墨迹
    palette = []
    for _n, _d in regions:
        c = _d.get("fill")
        if c and tuple(c) not in palette:
            palette.append(tuple(c))
    if palette:
        src.palette = palette
        print("调色板 %d 色: %s" % (
            len(palette), ", ".join("#%02X%02X%02X" % c for c in palette)))
        print()

    def gray_of(r):
        return src.ink_gray(r["fill"]) if r.get("fill") else None

    def ink_of(r):
        return src.ink_level(r["fill"]) if r.get("fill") else None

    if args.probe:
        print("参数扫描（U=上采样，ts=turdsize 折算后，thr=二值化阈值，"
              "am=alphamax，ot=opttolerance）")
        print("注意：alphamax=0 会让 potrace 输出纯多边形（圆角变折线）。"
              "它的 IoU 可能最高但视觉最差，务必同时看「曲线段」列。")
        print("排序依据是**软IoU**（覆盖率口径）而不是二值 IoU：二值 IoU 的参考"
              "固定按 128 生成，天然偏向低阈值，会把 1px 细笔画切成碎片。")
        print(f"{'region':12s} {'U':>2s} {'ts':>5s} {'thr':>4s} {'am':>5s} "
              f"{'ot':>5s} {'软IoU%':>7s} {'IoU%':>7s} {'召回%':>7s} {'精确%':>7s} "
              f"{'子路径':>6s} {'曲线段':>6s} {'面积比':>8s}")
        best = {}
        for name, r in regions:
            rows = []
            for U in (4, 8):
                for ts0 in (2, 3):
                    for thr in (112, 128, 139, 148, 160, 172, 184):
                        for am in (0.8, 1.0):
                            for ot in (0.1,):
                                ts = max(1, int(round(ts0 * U * U)))
                                m = evaluate(src, r["y0"], r["y1"], r["x0"],
                                             r["x1"], r["invert"], U, ts, am, ot,
                                             threshold=thr, gray=gray_of(r),
                                             ink_level=ink_of(r))
                                rows.append((m["soft"], U, ts, thr, am, ot, m))
            rows.sort(key=lambda t: -t[0])
            for soft, U, ts, thr, am, ot, m in rows[:8]:
                print(f"{name:12s} {U:2d} {ts:5d} {thr:4d} {am:4.1f} {ot:5.2f} "
                      f"{m['soft']:7.2f} {m['iou']:7.2f} "
                      f"{m['recall']:7.2f} "
                      f"{m['precision']:7.2f} {m['subpaths']:6d} "
                      f"{m['svg'].count('C'):6d} {m['ink_ratio']:8.3f}")
            # 只在能产生真实曲线的候选中选最优（排除 alphamax=0 的多边形退化）
            curved = [t for t in rows if t[6]["svg"].count("C") > 0]
            best[name] = curved[0] if curved else rows[0]
            print()
        print("=== 各区域最优（已排除 alphamax=0 的多边形退化）===")
        for name, (soft, U, ts, thr, am, ot, m) in best.items():
            print(f"  {name:12s} 软IoU {soft:6.2f}%  面积比 {m['ink_ratio']:.3f}   "
                  f"--upscale {U} --turdsize {max(1, ts // (U * U))} "
                  f"--threshold {thr} --alphamax {am} --opttolerance {ot}")
        with open(os.path.join(args.out, "best_params.json"), "w",
                  encoding="utf-8") as f:
            json.dump({k: {"upscale": v[1], "turdsize_source_px": max(1, v[2] // (v[1] ** 2)),
                           "turdsize_internal": v[2], "threshold": v[3],
                           "alphamax": v[4], "opttolerance": v[5],
                           "soft": v[0], "iou": v[6]["iou"],
                           "ink_ratio": v[6]["ink_ratio"],
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
                         args.threshold, gray_of(r))
        if not fg.any():
            print(f"{name:12s} 掩膜为空，跳过")
            continue
        ts = max(1, args.turdsize * args.upscale ** 2)   # 坑 6
        fill = ("#%02X%02X%02X" % tuple(r["fill"])) if r.get("fill") else "#111111"
        svg, subs, (mx0, my0, mx1, my1) = mask_to_svg(
            fg, src.mm_per_px / args.upscale, x0 * args.upscale,
            y0 * args.upscale, ts, args.alphamax, args.opttolerance, fill=fill)
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
            "fill": list(r["fill"]) if r.get("fill") else None,
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
