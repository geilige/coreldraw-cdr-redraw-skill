#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""位图 -> CorelDRAW 矢量 CDR：一条命令跑完全流程（本技能主入口）。

给一张位图（PNG/JPG 设计稿、扫描件、包装稿导出图），自动完成：

    标定 -> 边缘残留探测 -> 自动分区 -> 逐区阈值寻优 -> 描摹
         -> 在 CorelDRAW 中建页 / 建图层 / 导入 / 精确定位 -> 保存 CDR
         -> 配准式像素校验 -> 输出报告与对照图

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    # 最简：给图和页面宽度，产出 CDR
    python scripts/cdr_bitmap_to_cdr.py --image ref.png --page-width 210 \
        --output out/ref.cdr

    # 用标准纸型（高度按纸型取，不按图比例推导）
    python scripts/cdr_bitmap_to_cdr.py --image ref.png --page-size A4 \
        --output out/ref.cdr

    # 没有 CorelDRAW 也能跑：只描摹出 SVG + 清单
    python scripts/cdr_bitmap_to_cdr.py --image ref.png --page-width 210 \
        --output out/ref.cdr --trace-only

    # 自动分区不满意时，手工指定分区（覆盖自动结果）
    python scripts/cdr_bitmap_to_cdr.py --image ref.png --page-width 210 \
        --output out/ref.cdr \
        --region "art:6,500,1217,900" \
        --region "logo:0,901,1217,1059,invert"

--------------------------------------------------------------------------
自动分区怎么做的
--------------------------------------------------------------------------
1. 行投影切"内容带"：行内墨迹像素数 >= min_ink_px 视为有效行，
   有效行之间的空白 < min_gap_px 则合并，短于 min_run_px 的段丢弃。
2. 在每条内容带内判定"满版色带"：**用行的中位数灰度**，而不是覆盖率。
   这一点很关键——色带中间常被反白字标掏空，覆盖率会掉到 0.85 以下，
   用覆盖率判定会把色带误判成普通内容区。行中位数只看"这一行整体是深还是浅"，
   不受中间被掏空的影响（实测 vonder 图：中位数法准确圈出 y 901..1059，
   覆盖率法碎成 901..960 + 1037..1059 两段）。
   注意不要用 25 分位数：插图密集区的 25 分位也会低于 128，会误判。
3. 色带拆成两件事：垫底的原生矢量**矩形**（颜色从源图取中位数）
   + 同一框内的**反白描摹**（invert=True，描出白字标）。
4. 非色带行段再按行投影细切；最后在列方向收紧包围盒，
   列间空隙超过 col_gap_frac * 图宽时切分（--no-split 可关）。

--------------------------------------------------------------------------
依赖
--------------------------------------------------------------------------
    python -m pip install numpy opencv-python pillow potracer
    python -m pip install pywin32        # 只有要建 CDR 时才需要
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np                                        # noqa: E402
from PIL import Image                                     # noqa: E402

import cdr_image_trace as T                               # noqa: E402

PAGE_SIZES = {
    "A3": (297.0, 420.0), "A4": (210.0, 297.0), "A5": (148.0, 210.0),
    "A6": (105.0, 148.0), "B5": (176.0, 250.0),
    "LETTER": (215.9, 279.4), "LEGAL": (215.9, 355.6),
}

# 承载垫底矩形的图层名。必须同时出现在 layer_order 里，
# 否则 cdr_image_place 的矩形无处安放（会报 KeyError）。
BAND_LAYER = "BAND"


# ----------------------------------------------------------------------------
# 分区辅助
# ----------------------------------------------------------------------------


def _runs(mask):
    """把布尔序列切成连续 True 的 [start, end) 段。"""
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def _merge(segs, gap_max, run_min):
    """把间隔小于 gap_max 的相邻段合并，并丢掉短于 run_min 的段。"""
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < gap_max:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= run_min]


def _row_bands(ink, y0, y1, min_ink_px, min_gap_px, min_run_px):
    """在 [y0,y1) 内按行投影切出内容带。"""
    rows = ink[y0:y1].sum(axis=1)
    segs = _runs(rows >= min_ink_px)
    return [(y0 + a, y0 + b)
            for a, b in _merge(segs, min_gap_px, min_run_px)]


def _col_segments(ink, y0, y1, gap_min, run_min):
    """在 [y0,y1) 内按列投影找横向分段（用于收紧/切分 x 范围）。"""
    cols = ink[y0:y1].sum(axis=0)
    return _merge(_runs(cols > 0), gap_min, run_min)


def _band_color(color_img, y0, y1):
    """取色带主色：带内偏暗像素的 RGB 中位数（排除反白内容）。"""
    seg = color_img[y0:y1].reshape(-1, color_img.shape[2])
    lum = seg.mean(axis=1)
    sel = seg[lum < 128]
    if sel.size == 0:
        sel = seg
    med = np.median(sel, axis=0)
    return [int(round(v)) for v in med]


def strip_residue(ink, min_frac=0.9):
    """从墨迹图里抹掉"贯穿整幅"的边缘残留（裁切/扫描黑边条）。

    这一步不能省：残留条会让**每一行**都含有墨迹像素，行投影就永远找不到空隙，
    分区会把本应分开的区块全粘成一块。实测 vonder 图左侧 4px 黑边条
    正是这样把易碎标、插图、页脚粘成了一个 y 0..901 的大块。
    返回 (抹除后的墨迹图, 左侧列数, 右侧列数, 顶部行数, 底部行数)。
    """
    out = ink.copy()
    H, W = ink.shape

    def lead(fn, n):
        k = 0
        while k < n and fn(k):
            k += 1
        return k

    nl = lead(lambda c: ink[:, c].mean() > min_frac, W)
    nr = lead(lambda c: ink[:, W - 1 - c].mean() > min_frac, W - nl)
    nt = lead(lambda r: ink[r, :].mean() > min_frac, H)
    nb = lead(lambda r: ink[H - 1 - r, :].mean() > min_frac, H - nt)
    if nl:
        out[:, :nl] = False
    if nr:
        out[:, W - nr:] = False
    if nt:
        out[:nt, :] = False
    if nb:
        out[H - nb:, :] = False
    return out, nl, nr, nt, nb


def _tighten(ink_ext, r, pad_px=0):
    """把区域四边向外扩到**宽松**墨迹图的真实边界，遇到空隙即停。

    为什么要向外扩：细笔画的末梢（文字 descender、抗锯齿边缘）每行只有两三个
    墨迹像素，会被 min_ink_px 当成空白切掉。实测 vonder 页脚墨迹真实范围
    y 1083..1098，严格图只到 1096，切掉下缘后召回从 96.69% 掉到 86.65%。

    为什么不用无脑 padding：padding 会越过区块间的空白伸进相邻区域，
    把邻块的墨迹也描一遍，在 CDR 里产生重复几何。
    逐行/逐列检查宽松墨迹图、遇到真正的空白行/列就停，既补全外沿又不越界。
    检查时把范围限制在当前区已确定的另一轴上，避免从邻块的列里"借"到墨迹。
    """
    H, W = ink_ext.shape
    y0, y1, x0, x1 = r["y0"], r["y1"], r["x0"], r["x1"]
    while y0 > 0 and ink_ext[y0 - 1, x0:x1].any():
        y0 -= 1
    while y1 < H and ink_ext[y1, x0:x1].any():
        y1 += 1
    while x0 > 0 and ink_ext[y0:y1, x0 - 1].any():
        x0 -= 1
    while x1 < W and ink_ext[y0:y1, x1].any():
        x1 += 1
    if pad_px:
        y0, x0 = max(0, y0 - pad_px), max(0, x0 - pad_px)
        y1, x1 = min(H, y1 + pad_px), min(W, x1 + pad_px)
    return dict(r, y0=y0, y1=y1, x0=x0, x1=x1)


def _is_full_bleed(ink_raw, y0, y1):
    """候选色带是否真的"满版"：在其行范围内，**每一列**都必须有墨迹。

    为什么光有行中位数不够：行中位数只问"这一行整体偏深吗"，两个并排的实心
    内容块也能把中位数压到 128 以下（实测合成图：两个 150px 宽的实心黑块放在
    600px 宽的图里，304/600 像素为暗，中位数就是暗的），于是整条内容行带被
    误判成色带——后果是把两块内容当成"垫底矩形 + 反白描摹"，输出完全错。

    满版色带的定义特征是**横向贯通**：底色顶到左右页边，所以它的行范围内
    每一列都至少有一个墨迹像素。两块并排内容之间的空隙列则一个墨迹像素都没有。

    用未剥离的墨迹图判定，因为残留条本身也是"贯通"的（vonder 图左侧 4px），
    它不该让真色带被否掉。而真色带即使中间被反白字标掏空，字的上下仍有纯底色行，
    所以被掏空的列照样有墨迹——实测 vonder 色带 y 901..1059，字标占 y 926..1040，
    其上方 25 行与下方 19 行是纯底色，因此全列通过。
    """
    if y1 <= y0:
        return False
    return bool(ink_raw[y0:y1, :].any(axis=0).all())


def auto_partition(src, color_img, min_ink_px=4, min_gap_px=6, min_run_px=6,
                   band_med_max=128, band_min_rows=10, col_gap_frac=0.05,
                   split=True, edge_gray=200, pad_px=0):
    """自动分区。返回 (regions, residue)，坐标是源图像素。"""
    ink_raw = src.gray < 128
    ink, nl, nr, nt, nb = strip_residue(ink_raw)
    # 宽松墨迹图：只用于收紧外沿，不参与"哪里是内容块"的判定
    ink_ext, _, _, _, _ = strip_residue(src.gray < edge_gray)
    residue = {"left": nl, "right": nr, "top": nt, "bottom": nb}
    H, W = src.img_h, src.img_w
    # 行中位数：判断"这一行整体是深底还是浅底"
    row_med = np.median(src.gray, axis=1)
    raw = []

    for by0, by1 in _row_bands(ink, 0, H, min_ink_px, min_gap_px, min_run_px):
        dark = row_med[by0:by1] < band_med_max
        # 行中位数说是"深"，还得横向贯通才算色带；不贯通的一律按内容处理
        for a, b in _runs(dark):
            s0, s1 = by0 + a, by0 + b
            if b - a >= band_min_rows and _is_full_bleed(ink_raw, s0, s1):
                raw.append({"kind": "band", "y0": s0, "y1": s1, "invert": True,
                            "color": _band_color(color_img, s0, s1)})
                continue
            # 不够高、或不是满版：按内容处理，仍要按行投影细切
            if s1 - s0 < min_run_px:
                continue
            for cy0, cy1 in _row_bands(ink, s0, s1, min_ink_px, min_gap_px, min_run_px):
                raw.append({"kind": "content", "y0": cy0, "y1": cy1, "invert": False})
        # 浅色行段：按行投影细切
        for a, b in _runs(~dark):
            s0, s1 = by0 + a, by0 + b
            if s1 - s0 < min_run_px:
                continue
            for cy0, cy1 in _row_bands(ink, s0, s1, min_ink_px, min_gap_px, min_run_px):
                raw.append({"kind": "content", "y0": cy0, "y1": cy1, "invert": False})

    # 列方向收紧 + 可选切分
    out, gap_min = [], max(3, int(round(W * col_gap_frac)))
    for r in raw:
        if r["kind"] == "band":
            # 色带的横向范围用**未剥离**的墨迹算：满版底色本来就该顶到页边，
            # 残留剥离只用于"哪里是内容"的判定，不该把色带缩进去。
            segs = _col_segments(ink_raw, r["y0"], r["y1"], gap_min, min_run_px)
            if not segs:
                continue
            out.append(dict(r, x0=int(segs[0][0]), x1=int(segs[-1][1])))
            continue
        # 内容：先按严格图在列空隙处切分，再用宽松图收紧每一块的外沿
        segs = _col_segments(ink, r["y0"], r["y1"], gap_min, min_run_px)
        if not segs:
            continue
        parts = segs if (split and len(segs) > 1) else [(segs[0][0], segs[-1][1])]
        for x0, x1 in parts:
            out.append(_tighten(ink_ext,
                                dict(r, x0=int(x0), x1=int(x1)), pad_px))
    return out, residue


def _type_of(r, src):
    """按形状与尺寸给区域猜一个语义类型（仅用于命名，可被 --region 覆盖）。"""
    if r.get("kind") == "band":
        return "BAND"
    if r.get("invert"):
        return "INK"
    h_mm = (r["y1"] - r["y0"]) * src.mm_per_px
    w_mm = (r["x1"] - r["x0"]) * src.mm_per_px
    if h_mm < 6.0:
        return "TEXT"
    if w_mm < 60.0 and h_mm < 40.0:
        return "MARK"
    return "ART"


def name_regions(regions, src):
    """按 z 序命名：色带在前（垫底），其余自上而下。"""
    bands = [r for r in regions if r["kind"] == "band"]
    rest = sorted([r for r in regions if r["kind"] != "band"],
                  key=lambda r: (r["y0"], r["x0"]))
    named, counters = [], {}
    for i, r in enumerate(bands, 1):
        r = dict(r)
        r["name"] = f"band{i:02d}"
        r["type"] = "BAND"
        r["trace_name"] = f"band{i:02d}_ink"
        r["trace_type"] = "INK"      # 反白描摹，与垫底矩形分开命名
        named.append(r)
    for r in rest:
        t = _type_of(r, src)
        counters[t] = counters.get(t, 0) + 1
        r = dict(r)
        r["name"] = f"{t.lower()}{counters[t]:02d}"
        r["type"] = t
        r["trace_name"] = r["name"]
        r["trace_type"] = t
        named.append(r)
    return named


# ----------------------------------------------------------------------------
# 逐区寻优 + 描摹
# ----------------------------------------------------------------------------


def pick_thresholds(px_area, thresholds, max_px):
    """大区域降档扫描：面积越大描摹越慢，减少候选阈值数量。"""
    if px_area <= max_px or len(thresholds) <= 3:
        return thresholds
    mid = len(thresholds) // 2
    return [thresholds[0], thresholds[mid], thresholds[-1]]


def tune_and_trace(src, r, args, log):
    """对单个区域扫阈值，取 IoU 最高的组合，返回 (svg, subs, bbox, info)。"""
    y0, y1, x0, x1 = r["y0"], r["y1"], r["x0"], r["x1"]
    ts = max(1, args.turdsize * args.upscale ** 2)      # turdsize 单位是位图像素
    cands = ([args.threshold] if args.threshold
             else pick_thresholds((y1 - y0) * (x1 - x0),
                                  args.tune_list, args.max_tune_px))
    rows = []
    for thr in cands:
        m = T.evaluate(src, y0, y1, x0, x1, r["invert"], args.upscale,
                       ts, args.alphamax, args.opttolerance, threshold=thr)
        rows.append((thr, m))
        log(f"      阈值 {thr:3d}  IoU {m['iou']:6.2f}%  召回 {m['recall']:6.2f}%  "
            f"精确 {m['precision']:6.2f}%  面积比 {m['ink_ratio']:.3f}  "
            f"曲线段 {m['svg'].count('C'):5d}")
    # alphamax=0 会让 potrace 输出纯多边形（圆角变折线），IoU 反而可能最高，
    # 所以先只在"有真实曲线段"的候选里选，全都退化时才退回全体。
    curved = [t for t in rows if t[1]["svg"].count("C") > 0]
    thr, m = max(curved or rows, key=lambda t: t[1]["iou"])
    return thr, m, ts


def trace_all(src, regions, args, log):
    """逐区描摹并写 SVG，返回 (manifest, per_region_info)。"""
    os.makedirs(args.svg_dir, exist_ok=True)
    manifest = {
        "source_image": os.path.abspath(args.image_used),
        "page_mm": [src.page_w_mm, round(src.page_h_mm, 4)],
        "mm_per_px": round(src.mm_per_px, 8),
        "image_px": [src.img_w, src.img_h],
        "params": {"upscale": args.upscale, "alphamax": args.alphamax,
                   "opttolerance": args.opttolerance},
        "regions": {},
    }
    info = {}
    for r in regions:
        name = r["trace_name"]
        fg = T.region_mask(src, r["y0"], r["y1"], r["x0"], r["x1"],
                           r["invert"], args.upscale, args.threshold or 128)
        if not fg.any():
            log(f"  {name:14s} 掩膜为空，跳过")
            continue
        log(f"  {name:14s} 源框 x {r['x0']:5d}..{r['x1']:5d}  "
            f"y {r['y0']:5d}..{r['y1']:5d}  类型 {r['type']}")
        thr, m, ts = tune_and_trace(src, r, args, log)
        svg, subs, (mx0, my0, mx1, my1) = T.mask_to_svg(
            T.region_mask(src, r["y0"], r["y1"], r["x0"], r["x1"], r["invert"],
                          args.upscale, thr),
            src.mm_per_px / args.upscale, r["x0"] * args.upscale,
            r["y0"] * args.upscale, ts, args.alphamax, args.opttolerance)
        path = os.path.join(args.svg_dir, f"{name}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        manifest["regions"][name] = {
            "svg": os.path.abspath(path),
            "bbox_mm": [round(mx0, 4), round(my0, 4), round(mx1, 4), round(my1, 4)],
            "size_mm": [round(mx1 - mx0, 4), round(my1 - my0, 4)],
            "invert": r["invert"],
            "source_box_px": [r["x0"], r["y0"], r["x1"], r["y1"]],
            "subpaths": len(subs),
            "type": r["type"],
            "threshold": thr,
            # kind/color 也记下来：满版色带的垫底矩形是从这里推出来的，
            # 报告重建（--report-only）时若清单里没有 rects 就能反推回去。
            "kind": r.get("kind"),
            "color": list(r["color"]) if r.get("color") else None,
            # 把寻优指标一并写进清单，报告才可脱离描摹过程独立重建
            "iou": m["iou"],
            "recall": m["recall"],
            "precision": m["precision"],
            "ink_ratio": m["ink_ratio"],
        }
        info[name] = {"type": r["type"], "threshold": thr, "subpaths": len(subs),
                      "bbox_mm": [round(mx0, 4), round(my0, 4),
                                  round(mx1, 4), round(my1, 4)],
                      "iou": m["iou"], "recall": m["recall"],
                      "precision": m["precision"], "ink_ratio": m["ink_ratio"],
                      "source_box_px": [r["x0"], r["y0"], r["x1"], r["y1"]]}
        log(f"      -> 选中阈值 {thr}  IoU {m['iou']:.2f}%  面积比 {m['ink_ratio']:.3f}  "
            f"子路径 {len(subs)}  包围盒 {mx1-mx0:.3f} x {my1-my0:.3f} mm")
    mp = os.path.join(args.svg_dir, "manifest.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    log(f"  清单: {mp}")
    return manifest, info


# ----------------------------------------------------------------------------
# 在 CorelDRAW 中重建
# ----------------------------------------------------------------------------


def build_cdr(args, manifest, rects, layer_order, layer_map, white, log):
    """调用 cdr_image_place 建 CDR。返回 (ok, placement_path)。"""
    import cdr_image_place as P

    argv = ["--manifest", os.path.join(args.svg_dir, "manifest.json"),
            "--output", args.output,
            "--progid", args.progid,
            "--rect-layer", layer_order[0],
            "--layer-order", ",".join(layer_order)]
    if args.preview:
        argv += ["--preview", args.preview]
    for name, r in rects:
        argv += ["--rect", "%s:%.4f,%.4f,%.4f,%.4f,#%02X%02X%02X"
                 % (name, r["x"], r["y"], r["w"], r["h"],
                    r["rgb"][0], r["rgb"][1], r["rgb"][2])]
    for k, v in layer_map.items():
        argv += ["--layer", f"{k}={v}"]
    for w in sorted(white):
        argv += ["--white", w]
    if args.close_existing:
        argv += ["--close-existing", args.close_existing]
    log("  调用 cdr_image_place：")
    log("    " + " ".join(argv))
    rc = P.main(argv)
    placement = os.path.join(os.path.dirname(os.path.abspath(args.output)),
                             "placement.json")
    return rc == 0, placement


def validate(args, placement, log):
    """调用 cdr_visual_diff 做配准式像素校验。返回 metrics 字典或 None。"""
    import cdr_visual_diff as V

    argv = ["--source", args.image_used, "--render", args.preview,
            "--placement", placement, "--out", args.compare_dir]
    log("  调用 cdr_visual_diff：")
    log("    " + " ".join(argv))
    if V.main(argv) != 0:
        return None
    mp = os.path.join(args.compare_dir, "metrics.json")
    if os.path.isfile(mp):
        with open(mp, encoding="utf-8") as f:
            return json.load(f)
    return None


# ----------------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------------


def write_report(args, src, edges, info, rects, metrics, cdr_ok, placement, log,
                 page_h_explicit=False):
    """写 report.md 与 report.json，返回 markdown 文本。"""
    L = []
    A = L.append
    A("# 位图矢量化报告")
    A("")
    A(f"- 源图：`{os.path.abspath(args.image_used)}`")
    A(f"- 像素尺寸：{src.img_w} x {src.img_h}")
    A(f"- 页面：{src.page_w_mm:g} x {src.page_h_mm:g} mm")
    A(f"- 标定：1 px = {src.mm_per_px:.6f} mm（按宽度 {src.page_w_mm:g} mm 标定）")
    # 只有用户**明确指定**了页面高度时，比例不一致才算问题；
    # 没指定时高度本来就是按图比例推导的，警告会是噪音。
    # 分母都做零检查：--report-only 读的清单可能缺 image_px / page_mm。
    if page_h_explicit and src.img_h and src.page_h_mm:
        ratio_img = src.img_w / src.img_h
        ratio_pg = src.page_w_mm / src.page_h_mm
        if ratio_pg and abs(ratio_img - ratio_pg) / ratio_pg > 0.02:
            A(f"- **比例提示**：图宽高比 {ratio_img:.4f} 与页面宽高比 {ratio_pg:.4f} "
              f"不一致，纵向内容实际高度 {src.page_h_mm_derived:.3f} mm，"
              f"比页面高度少 {src.page_h_mm - src.page_h_mm_derived:.3f} mm。")
    A("")
    if edges.get("left_dark_cols", 0) >= 2:
        A(f"> **边缘残留**：源图最左侧有 {edges['left_dark_cols']} 列连续暗边"
          f"（均值 {edges['left']['mean']}），疑似裁切/扫描残留，"
          f"**未复刻为设计内容**。若属设计内容请用 `--rect` 手工补上，"
          f"或用 `--crop-left {edges['left_dark_cols']}` 裁掉后重新标定。")
        A("")
    A("## 分区与参数")
    A("")
    A("| 区域 | 类型 | 源框 px | 毫米包围盒 | 尺寸 mm | 阈值 | 子路径 | IoU% | 面积比 |")
    A("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, d in info.items():
        b = d["bbox_mm"]
        sb = d.get("source_box_px") or [0, 0, 0, 0]
        # 旧清单可能没有寻优指标（iou/ink_ratio 是后加的字段），缺就显示 "—"，
        # 不要崩，也不要假装是 0（0 会被误读成"完全不像"）。
        iou, ink = d.get("iou"), d.get("ink_ratio")
        s_iou = "—" if iou is None else f"{iou:.2f}"
        s_ink = "—" if ink is None else f"{ink:.3f}"
        A(f"| `{name}` | {d.get('type', '?')} | {sb[0]},{sb[1]}..{sb[2]},{sb[3]} "
          f"| {b[0]:.3f},{b[1]:.3f}..{b[2]:.3f},{b[3]:.3f} "
          f"| {b[2]-b[0]:.3f} x {b[3]-b[1]:.3f} | {d.get('threshold', '—')} "
          f"| {d.get('subpaths', '—')} | {s_iou} | {s_ink} |")
    A("")
    if rects:
        A("## 垫底矢量矩形（原生对象，非描摹）")
        A("")
        A("| 名称 | x mm | y mm | 宽 x 高 mm | 颜色 |")
        A("| --- | --- | --- | --- | --- |")
        for name, r in rects:
            A(f"| `{name}` | {r['x']:.3f} | {r['y']:.3f} "
              f"| {r['w']:.3f} x {r['h']:.3f} "
              f"| #{r['rgb'][0]:02X}{r['rgb'][1]:02X}{r['rgb'][2]:02X} |")
        A("")
    if metrics:
        o = metrics.get("overall", {})
        A("## 还原度（配准式像素比对）")
        A("")
        A("| 指标 | 值 |")
        A("| --- | --- |")
        A(f"| 整页 IoU | {o.get('iou', 0):.2f}% |")
        A(f"| 召回（源被覆盖） | {o.get('recall', 0):.2f}% |")
        A(f"| 精确（绘制正确） | {o.get('precision', 0):.2f}% |")
        A("")
        reg = metrics.get("regions") or {}
        if reg:
            A("| 区域 | IoU% | 召回% | 精确% |")
            A("| --- | --- | --- | --- |")
            for k, v in reg.items():
                A(f"| `{k}` | {v.get('iou', 0):.2f} | {v.get('recall', 0):.2f} "
                  f"| {v.get('precision', 0):.2f} |")
            A("")
        A("图例：深灰=一致，红=仅源图（漏画），蓝=仅重绘（多画）。"
          "对照图见 `compare/overlay_diff.png` 与 `compare/zoom/`。")
        A("")
    else:
        A("## 还原度")
        A("")
        A("未做配准校验（`--no-validate` 或 CorelDRAW 不可用）。"
          "各区域的本地 IoU 见上表。")
        A("")
    A("## 产物")
    A("")
    A(f"- CDR：`{os.path.abspath(args.output)}`"
      + ("" if cdr_ok else "  **（未生成）**"))
    A(f"- 预览：`{args.preview}`" if args.preview else "- 预览：未导出")
    A(f"- 描摹 SVG 与清单：`{args.svg_dir}`")
    if placement:
        A(f"- 定位记录：`{placement}`")
    A(f"- 对照图与指标：`{args.compare_dir}`")
    A("")
    A("## 已知偏差")
    A("")
    A("- 描摹结果是**近似矢量**，不是原始矢量数据。曲线为拟合，非设计原始路径。")
    A("- 小字（6pt 级）受源图分辨率限制，边缘有 1px 级差异，属天然失真。")
    A("- 笔画粗细以**墨迹面积比**衡量（矢量面积 ÷ 源图面积），"
      "落在 0.98~1.02 即吻合。不要用肉眼比对截图判断粗细："
      "源图与导出图密度不同，同倍数并排会产生尺度错觉。")
    A("")
    md = "\n".join(L)
    rp = os.path.join(args.out_dir, "report.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(args.out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"source_image": os.path.abspath(args.image_used),
                   "page_mm": [src.page_w_mm, src.page_h_mm],
                   "mm_per_px": src.mm_per_px,
                   "edge_probe": edges,
                   "regions": info,
                   "rects": [{"name": n, **{k: v for k, v in r.items()
                                            if k != "rgb"}} for n, r in rects],
                   "metrics": metrics, "cdr": os.path.abspath(args.output),
                   "cdr_ok": cdr_ok}, f, ensure_ascii=False, indent=2)
    log(f"  报告: {rp}")
    return md


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def parse_size(spec):
    """A4 / Letter / 210x297 -> (宽, 高) 毫米。"""
    key = spec.strip().upper()
    if key in PAGE_SIZES:
        return PAGE_SIZES[key]
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[xX*]\s*(\d+(?:\.\d+)?)$", spec.strip())
    if m:
        return float(m.group(1)), float(m.group(2))
    raise ValueError(f"无法识别的纸型 {spec!r}；可用 {sorted(PAGE_SIZES)} 或 宽x高")


def build_parser():
    ap = argparse.ArgumentParser(
        description="位图 -> CorelDRAW 矢量 CDR：一条命令跑完全流程",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default=None,
                    help="源位图路径（PNG/JPG）。--report-only 时可省略")
    ap.add_argument("--output", default=None,
                    help="输出 CDR 路径。--report-only 时可省略")
    ap.add_argument("--page-width", type=float, default=None, help="页面宽度（毫米）")
    ap.add_argument("--page-height", type=float, default=None,
                    help="页面高度（毫米）；省略则按图比例推导")
    ap.add_argument("--page-size", default=None,
                    help="标准纸型 A4/A3/A5/Letter 或 宽x高；给定时覆盖上面两项的高度")
    ap.add_argument("--region", action="append", default=[],
                    help="手工分区 name:x0,y0,x1,y1[,invert]（源图像素，左上原点），"
                         "可重复；给出后不再自动分区")
    ap.add_argument("--rect", action="append", default=[],
                    help="手工追加垫底矩形 name:x,y,w,h,#RRGGBB（毫米，y 距页顶），可重复")
    ap.add_argument("--out-dir", default=None,
                    help="中间产物与报告目录；默认 <CDR 同目录>/<文件名>_work")
    ap.add_argument("--progid", default="CorelDRAW.Application.18",
                    help="CorelDRAW ProgID（默认 CorelDRAW.Application.18）")
    ap.add_argument("--upscale", type=int, default=8,
                    help="描摹前灰度上采样倍数（默认 8）。细部保真的关键，"
                         "实测 U=4 得 95.2%% / U=8 得 98.3%%。超大图可用 4 控时")
    ap.add_argument("--turdsize", type=int, default=2,
                    help="斑点面积阈值（源图像素；内部按 upscale² 折算）")
    ap.add_argument("--alphamax", type=float, default=1.0,
                    help="拐角阈值。1.0 = 最大平滑；不要用 0（会输出纯多边形，"
                         "圆角变折线，而 IoU 反而最高，是个选参陷阱）")
    ap.add_argument("--opttolerance", type=float, default=0.1, help="曲线优化容差")
    ap.add_argument("--threshold", type=int, default=None,
                    help="固定二值化阈值；给定时不做逐区寻优")
    ap.add_argument("--tune-thresholds", default="112,118,128,138,148",
                    help="寻优候选阈值，逗号分隔（默认 112,118,128,138,148）。"
                         "阈值越低笔画越细，细笔画文字对它很敏感")
    ap.add_argument("--max-tune-px", type=int, default=200000,
                    help="源像素面积超过此值的区域只扫 3 个候选阈值以控时。"
                         "U=8 下一次大区域描摹要几十秒，5 档扫描会让整轮跑上十几分钟；"
                         "实测降到 3 档 IoU 只差约 0.1%%。整图很大时可用 --threshold "
                         "固定阈值，直接跳过寻优")
    ap.add_argument("--crop-left", type=int, default=0,
                    help="忽略源图最左侧 N 列（裁切/扫描残留），并按其后的宽度重新标定")
    ap.add_argument("--no-split", action="store_true", help="不做列方向切分")
    ap.add_argument("--trace-only", action="store_true",
                    help="只描摹出 SVG 与清单，不建 CDR（无 CorelDRAW 也能跑）")
    ap.add_argument("--no-validate", action="store_true", help="跳过配准式像素校验")
    ap.add_argument("--report-only", action="store_true",
                    help="不重跑描摹与建 CDR，仅用已有 svg/manifest.json 与 "
                         "compare/metrics.json 重建 report.md / report.json。"
                         "改一行报告措辞不该重跑十几分钟描摹")
    ap.add_argument("--close-existing", default=None,
                    help="建 CDR 前关闭名称以此前缀开头的已打开文档；"
                         "默认按输出文件名关闭同名文档，用 --keep-existing 关闭该行为")
    ap.add_argument("--keep-existing", action="store_true",
                    help="不自动关闭同名已打开文档")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.report_only:
        return _report_only(args)

    if not args.image:
        print("必须给出 --image（或用 --report-only 重建报告）。", file=sys.stderr)
        return 1
    if not args.output:
        print("必须给出 --output（或用 --report-only 重建报告）。", file=sys.stderr)
        return 1
    args.image = os.path.abspath(args.image)
    args.output = os.path.abspath(args.output)
    if not os.path.isfile(args.image):
        print(f"找不到源图: {args.image}", file=sys.stderr)
        return 1

    stem = os.path.splitext(os.path.basename(args.output))[0]
    if not args.out_dir:
        args.out_dir = os.path.join(os.path.dirname(args.output), f"{stem}_work")
    args.out_dir = os.path.abspath(args.out_dir)
    args.svg_dir = os.path.join(args.out_dir, "svg")
    args.compare_dir = os.path.join(args.out_dir, "compare")
    args.preview = os.path.join(args.out_dir, f"{stem}_preview.png")
    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    if args.close_existing is None:
        args.close_existing = "" if args.keep_existing else stem

    args.tune_list = [int(t) for t in str(args.tune_thresholds).split(",") if t.strip()]
    args.image_used = args.image
    # 页面高度是"用户明确给的"还是"按图比例推导的"——决定报告里要不要提比例偏差
    args.page_h_explicit = bool(args.page_size) or args.page_height is not None

    logfile = open(os.path.join(args.out_dir, "run.log"), "w", encoding="utf-8")

    def log(msg=""):
        print(msg)
        logfile.write(str(msg) + "\n")
        logfile.flush()

    try:
        return _run(args, log)
    finally:
        logfile.close()


# ----------------------------------------------------------------------------
# 仅重建报告
# ----------------------------------------------------------------------------


class _ShimSource:
    """从清单里的标定数据伪装的 Source，供 write_report 使用。

    write_report 只用到这几个只读属性；为了改一行报告措辞而重新打开位图、
    重新标定是不必要的（而且源图可能已经被移动或删除）。
    """

    def __init__(self, manifest, image_used):
        self.path = image_used
        self.img_w, self.img_h = (int(v) for v in manifest.get("image_px", [0, 0]))
        pm = manifest.get("page_mm") or [0.0, 0.0]
        self.page_w_mm = float(pm[0])
        self.page_h_mm = float(pm[1])
        self.mm_per_px = float(manifest.get("mm_per_px")
                               or (self.page_w_mm / self.img_w if self.img_w else 0))
        self.page_h_mm_derived = self.img_h * self.mm_per_px


_NEUTRAL_EDGES = {
    "left": {"mean": 0, "dark": False}, "right": {"mean": 0, "dark": False},
    "top": {"mean": 0, "dark": False}, "bottom": {"mean": 0, "dark": False},
    "left_dark_cols": 0,
}


def _read_json(path, default=None):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return default


def _find_placement(out_dir, output=None):
    """找定位记录 json（cdr_image_place.py 的产物）。

    它落在 **CDR 同目录**（placement.json），不是工作目录，所以两个地方都要找。
    """
    for d in (out_dir, os.path.dirname(os.path.abspath(output)) if output else None):
        if not d:
            continue
        try:
            cands = sorted(n for n in os.listdir(d)
                           if n.endswith(".json") and "place" in n.lower())
        except OSError:
            continue
        if cands:
            return os.path.join(d, cands[0])
    return None


def _report_only(args):
    """用已有清单与指标重建 report.md / report.json，不重跑描摹与建 CDR。

    为什么需要：报告里的数字来自两个阶段（逐区寻优在前，配准校验在后），
    早先的实现在校验**之前**就写了报告，于是报告里的还原度停留在有 bug 时期的
    旧值（整页 24.47%），而真实值是 93.62%。修好顺序后仍不够——改一行措辞
    也不该重跑十几分钟描摹，所以清单里补齐了寻优指标、垫底矩形与图层顺序，
    让报告可以完全脱离描摹过程独立重建。
    """
    if not args.out_dir:
        if not args.output:
            print("--report-only 需要 --out-dir，或给出 --output 以便推导工作目录",
                  file=sys.stderr)
            return 1
        stem0 = os.path.splitext(os.path.basename(args.output))[0]
        args.out_dir = os.path.join(os.path.dirname(os.path.abspath(args.output)),
                                    f"{stem0}_work")
    args.out_dir = os.path.abspath(args.out_dir)
    args.svg_dir = os.path.join(args.out_dir, "svg")
    args.compare_dir = os.path.join(args.out_dir, "compare")

    mpath = os.path.join(args.svg_dir, "manifest.json")
    manifest = _read_json(mpath)
    if not manifest:
        print(f"找不到或读不出清单 {mpath}；--report-only 需要先跑过一次描摹",
              file=sys.stderr)
        return 1

    stem = os.path.basename(args.out_dir.rstrip("\\/"))
    stem = stem[:-5] if stem.endswith("_work") else stem
    if not args.output:
        args.output = os.path.join(os.path.dirname(args.out_dir), f"{stem}.cdr")
    args.preview = os.path.join(args.out_dir, f"{stem}_preview.png")
    args.image_used = manifest.get("source_image") or (args.image or "")

    logfile = open(os.path.join(args.out_dir, "run.log"), "a", encoding="utf-8")

    def log(msg=""):
        print(msg)
        logfile.write(str(msg) + "\n")
        logfile.flush()

    try:
        src = _ShimSource(manifest, args.image_used)
        # 边缘残留探测结果优先从上一版报告里继承（重跑它要重新打开源图）
        edges = (_read_json(os.path.join(args.out_dir, "report.json"), {})
                 .get("edge_probe")) or dict(_NEUTRAL_EDGES)
        info = manifest.get("regions") or {}
        rects = []
        for r in manifest.get("rects") or []:
            d = {k: v for k, v in r.items() if k != "name"}
            if "rgb" in d:
                d["rgb"] = tuple(d["rgb"])
            rects.append((r["name"], d))
        if not rects:
            # 旧清单（在写入 rects 字段之前生成的）没有矩形记录，
            # 但满版色带的垫底矩形可以由区域本身推出来：bbox 就是矩形。
            for name, d in info.items():
                if d.get("type") != "BAND" or not d.get("color"):
                    continue
                b = d["bbox_mm"]
                rects.append((name, {"x": b[0], "y": b[1], "w": b[2] - b[0],
                                     "h": b[3] - b[1], "rgb": tuple(d["color"])}))
        metrics = _read_json(os.path.join(args.compare_dir, "metrics.json"))
        placement = _find_placement(args.out_dir, args.output)
        cdr_ok = bool(args.output) and os.path.isfile(args.output)

        log("=" * 68)
        log("仅重建报告（--report-only）")
        log("=" * 68)
        log(f"  清单      : {mpath}")
        log(f"  区域      : {len(info)} 个；垫底矩形 {len(rects)} 个")
        log(f"  配准指标  : {'有' if metrics else '无（compare/metrics.json 不存在）'}")
        log(f"  页面      : {src.page_w_mm:g} x {src.page_h_mm:g} mm，"
            f"{src.img_w} x {src.img_h} px，1 px = {src.mm_per_px:.6f} mm")
        md = write_report(args, src, edges, info, rects, metrics, cdr_ok, placement,
                          log, page_h_explicit=bool(manifest.get("page_h_explicit")))
        if metrics:
            o = metrics.get("overall", {})
            log(f"  还原度    : 整页 IoU {o.get('iou', 0):.2f}%  "
                f"召回 {o.get('recall', 0):.2f}%  精确 {o.get('precision', 0):.2f}%")
        log(f"  字数      : {len(md)} 字符")
        return 0
    finally:
        logfile.close()


def _run(args, log):
    # 1) 可选：裁掉左侧残留，并按裁后的宽度重新标定
    if args.crop_left > 0:
        im = Image.open(args.image).convert("RGB")
        w0 = im.size[0] - args.crop_left
        im.crop((args.crop_left, 0, im.size[0], im.size[1])).save(
            os.path.join(args.out_dir, "_cropped.png"))
        args.image_used = os.path.join(args.out_dir, "_cropped.png")
        log(f"已裁掉左侧 {args.crop_left} 列，标定宽度 {im.size[0]} -> {w0} px")

    # 2) 标定
    page_w, page_h = args.page_width, args.page_height
    if args.page_size:
        page_w, page_h = parse_size(args.page_size)
        log(f"纸型 {args.page_size} -> {page_w:g} x {page_h:g} mm")
    if page_w is None:
        print("必须给出 --page-width 或 --page-size 之一。", file=sys.stderr)
        return 1
    src = T.Source(args.image_used, page_w, page_h)
    log("=" * 68)
    log("步骤 1/6  标定")
    log("=" * 68)
    log(src.describe())

    # 3) 边缘残留探测
    edges = T.probe_edges(src)
    log("")
    log("步骤 2/6  边缘残留探测")
    for side in ("left", "right", "top", "bottom"):
        d = edges[side]
        log(f"  {side:6s} 均值 {d['mean']:3d}  {'暗' if d['dark'] else '亮'}")
    if edges["left_dark_cols"] >= 2:
        log(f"  [警告] 最左侧 {edges['left_dark_cols']} 列连续暗边，疑似裁切/扫描残留；"
            f"不会复刻为设计内容（如需裁掉用 --crop-left {edges['left_dark_cols']}）")

    color_img = np.array(Image.open(args.image_used).convert("RGB"))

    # 4) 分区
    log("")
    log("步骤 3/6  分区")
    if args.region:
        regions = []
        for spec in args.region:
            name, d = T.parse_region(spec)
            d["kind"] = "content"
            d["name"] = name
            d["trace_name"] = name
            d["type"] = _type_of(d, src)
            d["trace_type"] = "INK" if d["invert"] else d["type"]
            regions.append(d)
        log(f"  使用手工分区，共 {len(regions)} 个")
    else:
        raw, residue = auto_partition(src, color_img, col_gap_frac=0.05,
                                      split=not args.no_split)
        if any(residue.values()):
            log(f"  已从投影中剔除贯穿边缘的残留：左 {residue['left']} 列、"
                f"右 {residue['right']} 列、上 {residue['top']} 行、"
                f"下 {residue['bottom']} 行（仅用于分区判定，不影响描摹范围）")
        regions = name_regions(raw, src)
        log(f"  自动分区得到 {len(regions)} 个区域")
    for r in regions:
        log(f"    {r['name']:14s} {r['type']:5s} "
            f"x {r['x0']:5d}..{r['x1']:5d}  y {r['y0']:5d}..{r['y1']:5d}"
            + ("  [反白]" if r["invert"] else "")
            + (f"  垫底色 #{r['color'][0]:02X}{r['color'][1]:02X}{r['color'][2]:02X}"
               if r.get("kind") == "band" else ""))

    # 5) 描摹
    log("")
    log("步骤 4/6  逐区寻优 + 描摹")
    manifest, info = trace_all(src, regions, args, log)

    # 6) 组装矩形与图层映射
    #    一个区域一个图层：X8 的 Layer.Import 把新形状插到图层底部而不是追加到
    #    末尾，图层里已有形状时取错对象会把属性写到旧形状上。让每个区域独占
    #    一层，导入时图层为空，索引就没有歧义。
    rects, white, layer_order, layer_map = [], set(), [BAND_LAYER], {}
    for i, r in enumerate(regions, 2):        # 01 号留给垫底矩形层
        lname = f"{i:02d}_{r['trace_type']}"
        layer_map[r["trace_name"]] = lname
        layer_order.append(lname)
        if r["invert"]:
            white.add(r["trace_name"])
        if r.get("kind") == "band":
            b = [r["x0"] * src.mm_per_px, r["y0"] * src.mm_per_px,
                 (r["x1"] - r["x0"]) * src.mm_per_px,
                 (r["y1"] - r["y0"]) * src.mm_per_px]
            rects.append((r["name"], {"x": b[0], "y": b[1], "w": b[2], "h": b[3],
                                      "rgb": tuple(r["color"])}))
    for spec in args.rect:
        name, rr = _parse_rect_mm(spec)
        rects.append((name, rr))
    log("")
    log(f"  图层（自下而上）: {layer_order}")
    log(f"  垫底矩形 {len(rects)} 个，反白填充区域 {sorted(white) or '无'}")

    # 把垫底矩形也写回清单，这样 --report-only 能脱离建 CDR 过程重建报告
    manifest["rects"] = [{"name": n, **r} for n, r in rects]
    manifest["layer_order"] = layer_order
    manifest["page_h_explicit"] = bool(getattr(args, "page_h_explicit", False))
    with open(os.path.join(args.svg_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 7) 建 CDR
    cdr_ok, placement, metrics = False, None, None
    if args.trace_only:
        log("")
        log("步骤 5/6  建 CDR —— 已跳过（--trace-only）")
    else:
        log("")
        log("步骤 6/6  在 CorelDRAW 中重建")
        try:
            cdr_ok, placement = build_cdr(args, manifest, rects, layer_order,
                                          layer_map, white, log)
        except ImportError as exc:
            log(f"  [失败] 无法载入 CorelDRAW 接口: {exc}")
            log("         请先 python -m pip install pywin32，或改用 --trace-only")
        if cdr_ok and not args.no_validate:
            log("")
            log("附加  配准式像素校验")
            metrics = validate(args, placement, log)

    md = write_report(args, src, edges, info, rects, metrics, cdr_ok, placement, log,
                      page_h_explicit=getattr(args, "page_h_explicit", False))

    log("")
    log("=" * 68)
    log("完成")
    log("=" * 68)
    if metrics:
        o = metrics["overall"]
        log(f"整页 IoU {o['iou']:.2f}%  召回 {o['recall']:.2f}%  精确 {o['precision']:.2f}%")
    log(f"报告: {os.path.join(args.out_dir, 'report.md')}")
    log(f"CDR : {args.output}" if cdr_ok else "CDR : 未生成")
    return 0 if (cdr_ok or args.trace_only) else 2


def _parse_rect_mm(spec):
    """name:x,y,w,h,#RRGGBB -> (name, dict)。"""
    name, rest = spec.split(":", 1)
    p = [t.strip() for t in rest.split(",")]
    if len(p) < 5:
        raise ValueError(f"矩形格式应为 name:x,y,w,h,#RRGGBB，收到 {spec!r}")
    c = p[4].lstrip("#")
    return name, {"x": float(p[0]), "y": float(p[1]), "w": float(p[2]),
                  "h": float(p[3]),
                  "rgb": tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))}


if __name__ == "__main__":
    sys.exit(main())
