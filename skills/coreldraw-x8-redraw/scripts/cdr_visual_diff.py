#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配准式像素校验：把 CDR 渲染图贴回页面坐标系，与源图逐像素比对。

关键点：CorelDRAW 导出的位图通常是**内容包围盒**（例如 210 x 130.065 mm），
不是整页 A4。直接拿它和整页源图比会得到毫无意义的低分。
必须按 placement.json 记录的内容包围盒，把渲染图贴回整页白底，
与源图在**同一页面坐标系**下比对，指标才有意义。

依赖: numpy, pillow
    python -m pip install numpy pillow

输出（--out 目录）：
  page_source.png   源图贴到整页白底
  page_render.png   渲染图贴到整页白底
  side_by_side.png  两者并排
  overlay_diff.png  差异叠加（深灰=一致 红=仅源图漏画 蓝=仅渲染多画）
  zoom/*.png        各区域放大对照
"""

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image


def load_gray(path, target_w=None):
    im = Image.open(path).convert("L")
    if target_w and im.size[0] != target_w:
        im = im.resize((target_w, int(im.size[1] * target_w / im.size[0])),
                       Image.LANCZOS)
    return im


def metrics(a, b, box=None):
    if box:
        x0, y0, x1, y1 = box
        a, b = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    inter, union = (a & b).sum(), (a | b).sum()
    return {
        "source_ink": int(a.sum()),
        "render_ink": int(b.sum()),
        "iou": round(inter / union * 100, 2) if union else 100.0,
        "recall": round(inter / a.sum() * 100, 2) if a.sum() else 100.0,
        "precision": round(inter / b.sum() * 100, 2) if b.sum() else 100.0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把 CDR 渲染图贴回页面坐标系后与源图逐像素比对",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="源参考图路径")
    ap.add_argument("--render", required=True, help="CDR 导出的预览图路径")
    ap.add_argument("--placement", required=True,
                    help="cdr_image_place.py 写出的 placement.json")
    ap.add_argument("--out", default="compare", help="输出目录")
    ap.add_argument("--width", type=int, default=2480,
                    help="比对用的整页位图宽度（默认 2480，约 300dpi A4）")
    ap.add_argument("--threshold", type=int, default=128,
                    help="二值化阈值（默认 128）")
    ap.add_argument("--zoom", action="store_true", default=True,
                    help="额外输出各区域放大对照图（默认开启）")
    args = ap.parse_args(argv)

    for p, label in ((args.source, "源图"), (args.render, "渲染图"),
                     (args.placement, "定位记录")):
        if not os.path.isfile(p):
            print(f"找不到{label}: {p}", file=sys.stderr)
            return 1

    with open(args.placement, encoding="utf-8") as f:
        pl = json.load(f)

    page_w, page_h = pl["page_mm"]
    cb = pl.get("content_bbox_mm")
    if not cb:
        print("定位记录里没有 content_bbox_mm，无法配准。", file=sys.stderr)
        return 1
    cx0, cy0, cx1, cy1 = cb

    os.makedirs(args.out, exist_ok=True)

    W = args.width
    S = W / page_w                       # 整页 px/mm
    H = int(round(page_h * S))
    render_w = int(round((cx1 - cx0) * S))
    # 渲染图导出的是**内容包围盒**，所以左上角要贴在 (cx0, cy0) 而不是 (0, cy0)。
    # 坑：横向曾写死 0，只要内容左边界不为 0（实测 3.54mm ≈ 42px），
    # 整幅渲染图就横向错位，元素越小被罚得越狠（大色块 −14pp、小图标 −70pp），
    # 看起来像"描摹质量差"，其实是**校验本身错了**。下面的自检会拦住这类错误。
    render_left = int(round(cx0 * S))
    render_top = int(round(cy0 * S))

    src_img = load_gray(args.source, W)
    ren_img = load_gray(args.render, render_w)

    print(f"页面 {page_w:g} x {page_h:g} mm  ->  比对位图 {W} x {H}")
    print(f"内容区 x {cx0:.3f}..{cx1:.3f}  y {cy0:.3f}..{cy1:.3f} mm "
          f"({cx1-cx0:.3f} x {cy1-cy0:.3f})")
    print(f"渲染图 {Image.open(args.render).size} -> 归一到宽 {render_w}px，"
          f"贴到 ({render_left}, {render_top})px")
    exp_h = int(round((cy1 - cy0) * S))
    if abs(ren_img.size[1] - exp_h) > 2:
        print(f"[警告] 渲染图贴入高度 {ren_img.size[1]}px 与内容区期望 "
              f"{exp_h}px 相差 {abs(ren_img.size[1]-exp_h)}px，"
              f"说明导出范围与记录的包围盒不一致，比对结果会偏保守。")

    src_page = Image.new("L", (W, H), 255)
    src_page.paste(src_img, (0, 0))
    src_page.save(os.path.join(args.out, "page_source.png"))

    ren_page = Image.new("L", (W, H), 255)
    ren_page.paste(ren_img, (render_left, render_top))
    ren_page.save(os.path.join(args.out, "page_render.png"))

    side = Image.new("L", (W * 2 + 24, H), 200)
    side.paste(src_page, (0, 0))
    side.paste(ren_page, (W + 24, 0))
    side.save(os.path.join(args.out, "side_by_side.png"))

    a = np.array(src_page) < args.threshold
    b = np.array(ren_page) < args.threshold

    rgb = np.full((H, W, 3), 255, np.uint8)
    rgb[a & b] = (45, 45, 45)
    rgb[a & ~b] = (235, 60, 60)
    rgb[b & ~a] = (60, 120, 235)
    Image.fromarray(rgb).save(os.path.join(args.out, "overlay_diff.png"))

    # -- 配准自检：确认"零偏移"确实是最优的 -------------------------------
    # 这类校验最大的风险不是算错分数，而是**贴错了位置还报出低分**，
    # 让人误以为描摹质量差。所以每次都比一遍邻域：若某个平移明显更好，
    # 说明配准本身有问题，必须报出来而不是把低分当成结论。
    def _iou_shift(dx, dy):
        bb = np.roll(np.roll(b, dy, axis=0), dx, axis=1)
        u = int((a | bb).sum())
        return (int((a & bb).sum()) / u * 100) if u else 0.0

    base = _iou_shift(0, 0)
    coarse = max(((dx, dy) for dy in range(-12, 13, 3)
                  for dx in range(-12, 13, 3)), key=lambda d: _iou_shift(*d))
    fine = max(((dx, dy) for dy in range(coarse[1] - 3, coarse[1] + 4)
                for dx in range(coarse[0] - 3, coarse[0] + 4)),
               key=lambda d: _iou_shift(*d))
    best_iou = _iou_shift(*fine)
    if fine != (0, 0) and best_iou - base > 1.0:
        print()
        print("!" * 68)
        print(f"[配准自检失败] 零偏移 IoU {base:.2f}%，"
              f"但平移 dx={fine[0]} dy={fine[1]} 可达 {best_iou:.2f}%"
              f"（+{best_iou - base:.2f}pp）")
        print(f"  位置换算：dx={fine[0]}px = {fine[0]/S:.3f}mm，"
              f"dy={fine[1]}px = {fine[1]/S:.3f}mm")
        print("  这说明渲染图**没贴在内容包围盒的正确位置**，")
        print("  下面的分数是被错位拖低的假数字，不要据此判断描摹质量。")
        print("!" * 68)
    else:
        print()
        print(f"配准自检通过：零偏移即最优（邻域内最好 {best_iou:.2f}%"
              f"{'，与零偏移同分' if fine == (0, 0) else f'，提升 {best_iou - base:.2f}pp 可忽略'}）")

    print()
    print("=== 整页配准指标（墨迹像素）===")
    m = metrics(a, b)
    print(f"源图墨迹        : {m['source_ink']:9d}")
    print(f"渲染墨迹        : {m['render_ink']:9d}")
    print(f"IoU             : {m['iou']:6.2f}%")
    print(f"召回（源被覆盖）: {m['recall']:6.2f}%")
    print(f"精确（绘制正确）: {m['precision']:6.2f}%")

    # 按区域拆分
    items = [it for it in pl.get("items", []) if it.get("kind") == "svg"]
    if items:
        print()
        print("=== 分区域指标 ===")
        print(f"{'区域':14s} {'IoU%':>7s} {'召回%':>7s} {'精确%':>7s}")
        report = {}
        for it in items:
            x0, y0, x1, y1 = it["bbox_mm"]
            pad = 2.0                      # 留一点边，避免裁掉描摹外沿
            bx0 = max(0, int((x0 - pad) * S))
            by0 = max(0, int((y0 - pad) * S))
            bx1 = min(W, int((x1 + pad) * S))
            by1 = min(H, int((y1 + pad) * S))
            if bx1 <= bx0 or by1 <= by0:
                continue
            mm = metrics(a, b, (bx0, by0, bx1, by1))
            report[it["name"]] = mm
            print(f"{it['name']:14s} {mm['iou']:7.2f} {mm['recall']:7.2f} "
                  f"{mm['precision']:7.2f}")
        with open(os.path.join(args.out, "metrics.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"overall": m, "regions": report}, f,
                      ensure_ascii=False, indent=2)
        print(f"\n指标已写入 {os.path.join(args.out, 'metrics.json')}")

    # 放大对照
    if args.zoom and items:
        zdir = os.path.join(args.out, "zoom")
        os.makedirs(zdir, exist_ok=True)
        ov = Image.open(os.path.join(args.out, "overlay_diff.png"))
        sp = Image.open(os.path.join(args.out, "page_source.png"))
        rp = Image.open(os.path.join(args.out, "page_render.png"))
        for it in items:
            x0, y0, x1, y1 = it["bbox_mm"]
            pad = 3.0
            box = (max(0, int((x0 - pad) * S)), max(0, int((y0 - pad) * S)),
                   min(W, int((x1 + pad) * S)), min(H, int((y1 + pad) * S)))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            w2 = box[2] - box[0]
            sc = max(1, min(6, 1400 // max(1, w2)))
            for tag, img in (("diff", ov), ("src", sp), ("render", rp)):
                c = img.crop(box)
                c = c.resize((c.size[0] * sc, c.size[1] * sc), Image.NEAREST)
                c.save(os.path.join(zdir, f"{tag}_{it['name']}.png"))
            pair = Image.new("RGB", ((box[2] - box[0]) * 2 + 16,
                                     box[3] - box[1]), (200, 200, 200))
            pair.paste(sp.crop(box).convert("RGB"), (0, 0))
            pair.paste(rp.crop(box).convert("RGB"), (box[2] - box[0] + 16, 0))
            pair = pair.resize((pair.size[0] * sc, pair.size[1] * sc),
                               Image.NEAREST)
            pair.save(os.path.join(zdir, f"pair_{it['name']}.png"))
        print(f"放大对照已写入 {zdir}")

    print()
    print("图例: 深灰=一致  红=仅源图(漏画)  蓝=仅渲染(多画)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
