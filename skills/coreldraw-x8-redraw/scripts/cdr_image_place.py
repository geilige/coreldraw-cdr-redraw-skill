#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按描摹清单在 CorelDRAW 中构建 CDR，并导出预览。

读 cdr_image_trace.py 产出的 manifest.json，把每个区域的 SVG 导入指定图层，
按清单尺寸缩放、按清单包围盒左上角定位，另可附加原生矢量矩形（色带/底块）。
最后保存 CDR、导出预览图，并写出 placement.json 供校验脚本使用。

依赖: pywin32（需要本机安装完整版 CorelDRAW X8 或更高）
    python -m pip install pywin32

--------------------------------------------------------------------------
CorelDRAW X8 上的实测要点（改动前先读）
--------------------------------------------------------------------------
1. 连接用 Dispatch("CorelDRAW.Application.18")。GetActiveObject 在 X8 上常抛
   com_error(-2147221021, '操作无法使用')（MK_E_UNAVAILABLE），不要依赖它。
   Dispatch 会复用已打开的实例，不会另开一个。
2. **可选 VT_DISPATCH 参数会让 win32com 抛
   TypeError: The Python instance can not be converted to a COM object。**
   凡是签名里带可选对象参数的调用，末尾都要显式补 None：
       doc.SaveAs(path, None)
       lay.Import(svg_path, 0, None)
       doc.Export(png_path, filter_id, 1, None, None)
3. **ExportEx 在 X8 上会"返回成功但不写文件"。** 以 Export 为主，并且必须
   用 os.path.exists + 文件大小确认，不能只看有没有抛异常。
4. 导出位图滤镜常量：PNG = 802。颜色模式 RGB = 4。
5. 单位常量：cdrMillimeter = 3（不是 4）。页面方向 cdrPortrait = 0。
6. 参考点常量：cdrTopLeft = 3。设 doc.ReferencePoint = 3 之后，
   SetPosition(x, y) 就是"左上角坐标"。
7. **CorelDRAW 的 y 轴向上**，而设计稿习惯以左上为原点。转换：
       y_cdr = page_height_mm - y_top_mm
8. **Layer.Import 不返回形状对象，而且它把新形状插到图层"底部"（索引 1），
   不是追加到末尾。** 所以**不能盲取 `Item(Count)`**：如果图层里已有形状
   （比如同一个图层里先放了垫底矩形），`Item(Count)` 会取到那个旧形状，
   于是把新形状的目标尺寸/位置/填充写到旧形状上——表现为两个形状属性互换。
   定位新形状要用"导入前后名称集合的差"，见 `_find_new_shape()`。
   最省事的做法是**一个区域一个图层**，让导入时图层是空的，索引无歧义。
9. 集合索引是 **1 基**：Shapes(1)、Pages(1)、Layers(1)。
10. page.Shapes.All 是**方法**，要写成 page.Shapes.All().Count。
11. 批量操作前 app.Optimization = True、app.EventsEnabled = False；
    结束后必须复位并 app.Refresh()，否则画面不刷新、文件可能没落盘。
12. 新建文档默认带一个"图层 1"，直接改名复用，避免多出一个空图层。
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdr_common as C  # noqa: E402

CDR_MM = 3            # cdrMillimeter
CDR_PORTRAIT = 0      # cdrPortrait
CDR_LANDSCAPE = 1     # cdrLandscape（实测：SetSize(210,167.16) 后 Orientation 自报 1）
CDR_TOPLEFT = 3
CDR_PNG = 802
CDR_RGB = 4


def indexed(i, name):
    """给图层名加两位序号前缀；已经带序号（如 03_LOGO）的原样保留。"""
    return name if re.match(r"^\d+_", name) else f"{i:02d}_{name}"


def parse_rect(spec):
    """name:x,y,w,h,color  ->  (name, dict)。y 是距页面顶部的毫米。"""
    name, rest = spec.split(":", 1)
    p = [t.strip() for t in rest.split(",")]
    if len(p) < 5:
        raise ValueError(f"矩形格式应为 name:x,y,w,h,color，收到 {spec!r}")
    color = p[4].lstrip("#")
    return name, {
        "x": float(p[0]), "y": float(p[1]),
        "w": float(p[2]), "h": float(p[3]),
        "rgb": tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)),
    }


def parse_map(specs):
    """name=LAYER 或 name=white 之类的键值对列表。"""
    out = {}
    for s in specs or []:
        if "=" not in s:
            raise ValueError(f"映射格式应为 区域=值，收到 {s!r}")
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _layer_shape_names(lay):
    """返回 {索引: 名称}，索引 1 基。"""
    return {i: str(lay.Shapes.Item(i).Name)
            for i in range(1, lay.Shapes.Count + 1)}


def _find_new_shape(lay, before_names):
    """导入后定位新形状。

    X8 的 Layer.Import 把新形状插到图层**底部（索引 1）**，不是追加到末尾，
    所以 `Item(Count)` 只在"导入前图层是空的"时才碰巧正确。
    这里用导入前后名称集合的差来定位，对插入位置不敏感。
    返回 (shape, 索引)；定位不唯一时回退到末尾并返回 None 索引。
    """
    after = _layer_shape_names(lay)
    added = [i for i, nm in after.items() if nm not in set(before_names.values())]
    if len(added) == 1:
        return lay.Shapes.Item(added[0]), added[0]
    return lay.Shapes.Item(lay.Shapes.Count), None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按描摹清单在 CorelDRAW 中构建 CDR",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help="manifest.json 路径")
    ap.add_argument("--output", required=True, help="输出 CDR 路径")
    ap.add_argument("--preview", default=None, help="预览 PNG 路径（省略则不导出）")
    ap.add_argument("--progid", default="CorelDRAW.Application.18",
                    help="CorelDRAW ProgID，默认 CorelDRAW.Application.18")
    ap.add_argument("--rect", action="append", default=[],
                    help="附加原生矢量矩形 name:x,y,w,h,#RRGGBB（y 为距页顶毫米），"
                         "可重复，按给定顺序先画（垫底）")
    ap.add_argument("--rect-layer", default="RECT",
                    help="承载 --rect 矩形的图层名（默认 RECT）。"
                         "配合 --layer-order 时，该名必须出现在 order 里，"
                         "否则矩形无处安放会报 KeyError")
    ap.add_argument("--layer", action="append", default=[],
                    help="图层映射 区域名=图层名，可重复；缺省用区域名大写")
    ap.add_argument("--white", action="append", default=[],
                    help="需要填白的区域名（深底上的白色字标），可重复")
    ap.add_argument("--layer-order", default=None,
                    help="图层自下而上的顺序，逗号分隔；缺省按区域出现顺序")
    ap.add_argument("--reuse-layer1", default="图层 1",
                    help="复用新建文档自带的图层名（默认 '图层 1'）")
    ap.add_argument("--close-existing", default="",
                    help="构建前关闭名称以这些前缀开头的已打开文档，逗号分隔")
    ap.add_argument("--keep-optimization", action="store_true",
                    help="结束后不复位 Optimization/EventsEnabled（调试用）")
    args = ap.parse_args(argv)

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        print("缺少 pywin32。请先执行: python -m pip install pywin32", file=sys.stderr)
        return 1

    if not os.path.isfile(args.manifest):
        print(f"找不到清单: {args.manifest}", file=sys.stderr)
        return 1

    # CorelDRAW 的 SaveAs/Export 会按它自己的工作目录解析相对路径，
    # 必须先把所有输出路径转成绝对路径，否则文件会落到意外位置。
    args.manifest = os.path.abspath(args.manifest)
    args.output = os.path.abspath(args.output)
    if args.preview:
        args.preview = os.path.abspath(args.preview)

    with open(args.manifest, encoding="utf-8") as f:
        man = json.load(f)
    regions = man.get("regions", {})
    if not regions:
        print("清单里没有任何区域。", file=sys.stderr)
        return 1

    page_w, page_h = man["page_mm"][0], man["page_mm"][1]
    layer_map = parse_map(args.layer)
    white_set = set(args.white)

    rects = [parse_rect(s) for s in args.rect]

    # 图层自下而上的顺序：先矩形所在图层，再各区域图层
    order = []
    if args.layer_order:
        order = [t.strip() for t in args.layer_order.split(",") if t.strip()]
    else:
        order = [args.rect_layer] + [layer_map.get(n, n.upper()) for n in regions]

    out_dir = os.path.dirname(args.output)
    os.makedirs(out_dir, exist_ok=True)

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch(args.progid)
    except Exception as exc:
        print(f"无法连接 CorelDRAW（{args.progid}）: {type(exc).__name__}: {exc}\n"
              "请确认已安装完整版 CorelDRAW 且已至少启动过一次。", file=sys.stderr)
        return 1

    print(f"CorelDRAW {app.VersionMajor}.{app.VersionMinor}")

    try:
        app.Visible = True
        app.Optimization = True          # 要点 11
        app.EventsEnabled = False
    except Exception:
        pass

    if args.close_existing:
        prefixes = tuple(t.strip() for t in args.close_existing.split(",") if t.strip())
        for i in range(app.Documents.Count, 0, -1):
            try:
                d = app.Documents.Item(i)
                if str(d.Name).startswith(prefixes):
                    d.Close()
            except Exception:
                pass

    # 目标文件若正被 CorelDRAW 打开（上一轮自动化刻意"只开不关"留下的文档，
    # 也可能是用户自己开着看），`SaveAs` / `Save` 会**静默失败**：不抛异常、
    # 不写盘，日志照常打印"已保存"，只有磁盘没变。整条流水线就会作用在一个
    # 旧文件上而日志全绿。所以重写之前先把占用解开。
    released, why = C.release_document(args.output)
    if released:
        print(f"已关闭 CorelDRAW 中打开的旧文档，解除占用：{args.output}")
    elif why not in ("文件不存在", "未在 CorelDRAW 中打开"):
        print(f"[警告] 未能释放 {args.output}：{why}", file=sys.stderr)

    doc = app.CreateDocument()
    doc.Unit = CDR_MM
    doc.SaveAs(args.output, None)        # 要点 2
    page = doc.ActivePage
    page.SetSize(page_w, page_h)
    # 方向必须由 page_w/page_h 决定，**不能写死**。
    # `SetSize` 本身已经会按宽高设对方向（宽>高 → Orientation=1 横向），
    # 但这里曾经无条件写 `page.Orientation = CDR_PORTRAIT(0)`，于是横向图
    # 被交换成纵向：实测 daiion 那张 210 x 167.16 的稿子变成了
    # 167.16 x 210，而内容仍按 210 宽排布 → 右侧 43mm 溢出页面。
    # 纵向图（vonder 那张 210 x 286.4）恰好是对的，所以一直没暴露。
    page.Orientation = CDR_LANDSCAPE if page_w > page_h else CDR_PORTRAIT
    print(f"页面 {page.SizeWidth:.3f} x {page.SizeHeight:.3f} mm，单位代码 {doc.Unit}")

    def cdr_y(top_mm):
        return page_h - top_mm           # 要点 7

    def layer(name, reuse=None):
        for i in range(1, page.Layers.Count + 1):
            lay = page.Layers.Item(i)
            if reuse and str(lay.Name) == reuse:
                lay.Name = name
                return lay
            if str(lay.Name) == name:
                return lay
        return page.CreateLayer(name)

    # 先建全部图层，保证 z 序 = order 顺序
    layers = {}
    for idx, lname in enumerate(order, 1):
        nm = indexed(idx, lname)
        layers[lname] = layer(nm, reuse=args.reuse_layer1 if idx == 1 else None)
    print("图层（自下而上）:",
          [page.Layers.Item(i).Name for i in range(1, page.Layers.Count + 1)])

    placed = []

    for name, r in rects:
        lay = layers[args.rect_layer]
        doc.ReferencePoint = CDR_TOPLEFT
        sh = lay.CreateRectangle2(r["x"], cdr_y(r["y"] + r["h"]), r["w"], r["h"])
        sh.Fill.ApplyUniformFill(app.CreateRGBColor(*r["rgb"]))
        sh.Outline.SetNoOutline()
        sh.Name = name
        # 矩形也要验：CreateRectangle2 的参数语义在各版本间有差异，
        # 只打印请求值而不核对实际值会掩盖错误（本脚本曾经因此漏掉一个 bug）。
        doc.ReferencePoint = CDR_TOPLEFT
        ax, ay = sh.PositionX, page_h - sh.PositionY
        ex, ey = abs(ax - r["x"]), abs(ay - r["y"])
        ew = abs(sh.SizeWidth - r["w"])
        eh = abs(sh.SizeHeight - r["h"])
        flag = "OK" if max(ex, ey, ew, eh) < 0.02 else "偏差"
        placed.append({"name": name, "kind": "rect", "layer": lay.Name,
                       "bbox_mm": [r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]],
                       "error_mm": [round(ex, 4), round(ey, 4)]})
        print(f"{name:14s} 矩形 目标 x{r['x']:8.3f} y{r['y']:8.3f} "
              f"{r['w']:7.3f}x{r['h']:7.3f}  |  "
              f"实际 x{ax:8.3f} y{ay:8.3f} "
              f"{sh.SizeWidth:7.3f}x{sh.SizeHeight:7.3f}  "
              f"误差 {ex:.3f}/{ey:.3f} mm  [{flag}]")

    print()
    for key, info in regions.items():
        lname = layer_map.get(key, key.upper())
        if lname not in layers:
            layers[lname] = layer(indexed(len(layers) + 1, lname))
        lay = layers[lname]

        svg = info["svg"]
        if not os.path.isfile(svg):
            print(f"{key:14s} [跳过] 找不到 {svg}")
            continue

        before_n = lay.Shapes.Count
        before_names = _layer_shape_names(lay)
        lay.Import(svg, 0, None)          # 要点 2
        if lay.Shapes.Count == before_n:
            print(f"{key:14s} [失败] 导入后图层形状数未增加")
            continue
        sh, new_idx = _find_new_shape(lay, before_names)   # 要点 8
        if new_idx is None:
            print(f"{key:14s} [警告] 无法唯一定位新形状（图层内已有同名形状？），"
                  f"回退到图层末尾；建议一个区域一个图层")

        tw, th = info["size_mm"]
        sh.SetSize(tw, th)
        x0, y0 = info["bbox_mm"][0], info["bbox_mm"][1]
        doc.ReferencePoint = CDR_TOPLEFT  # 要点 6
        sh.SetPosition(x0, cdr_y(y0))

        if key in white_set:
            sh.Fill.ApplyUniformFill(app.CreateRGBColor(255, 255, 255))
            sh.Outline.SetNoOutline()
        else:
            # 清单里的 fill 是权威来源：多色稿一个区域一种色，SVG 里虽然已经
            # 写了 fill，但 CorelDRAW 的 SVG 解析对 fill 的处理不完全可靠，
            # 导入后再显式赋一次，保证颜色确实落上。
            frgb = info.get("fill")
            if frgb:
                sh.Fill.ApplyUniformFill(
                    app.CreateRGBColor(*[int(v) for v in frgb]))
                sh.Outline.SetNoOutline()

        doc.ReferencePoint = CDR_TOPLEFT
        px, py = sh.PositionX, sh.PositionY
        err_x = abs(px - x0)
        err_y = abs((page_h - py) - y0)
        flag = "OK" if err_x < 0.02 and err_y < 0.02 else "偏差"
        print(f"{key:14s} 目标 x{x0:8.3f} y{y0:8.3f} {tw:7.3f}x{th:7.3f}  |  "
              f"实际 x{px:8.3f} y{page_h-py:8.3f} "
              f"{sh.SizeWidth:7.3f}x{sh.SizeHeight:7.3f}  "
              f"误差 {err_x:.3f}/{err_y:.3f} mm  [{flag}]  类型{sh.Type}")

        placed.append({
            "name": key, "kind": "svg", "layer": lay.Name,
            "bbox_mm": [x0, y0, x0 + tw, y0 + th],
            "size_mm": [tw, th],
            "error_mm": [round(err_x, 4), round(err_y, 4)],
        })

    if not args.keep_optimization:
        try:
            app.Optimization = False
            app.EventsEnabled = True
            app.Refresh()
        except Exception:
            pass

    # 保存必须**回读磁盘确认**，不能只看 API 没抛异常。
    # 目标被别的进程占用时 `Save()` 既不报错也不写盘（内存里改动都在，
    # 所以回读内存核验会全对，只有磁盘没变）。指纹比对是唯一可靠的判据。
    before = C.file_fingerprint(args.output)
    doc.Save()
    after = C.file_fingerprint(args.output)
    if after is None:
        print(f"[失败] 保存后找不到文件: {args.output}\n"
              f"       请检查路径是否可写，或用 doc.SaveAs 另存到其他位置。",
              file=sys.stderr)
        return 2
    if before == after:
        # 指纹没变有两种可能，必须区分开，否则会误报：
        #   a) 保存真的失败（文件被别的进程占用）—— 要报错；
        #   b) 本次保存本就无内容可写 —— 正常，不能报错。
        # 判据：文件能否被本进程独占打开。打不开 = 被占用 = 属于 a)。
        locked = False
        try:
            with open(args.output, "r+b"):
                pass
        except OSError:
            locked = True
        if locked:
            print(f"[失败] Save() 未改变磁盘内容，且文件无法独占打开"
                  f"（{args.output}，{after[0]} 字节）。\n"
                  f"       文件被其他进程占用时 Save() 不抛异常也不写盘。\n"
                  f"       请关闭 CorelDRAW 中打开的该文档后重试。",
                  file=sys.stderr)
            return 3
        print("[提示] Save() 未改变磁盘内容，但文件未被占用"
              "（本次保存无内容可写），按成功处理。")
    size = os.path.getsize(args.output)
    print(f"\n已保存 {args.output}  ({size} 字节)")

    # 记录内容并集包围盒，供校验脚本把渲染图贴回页面
    xs = [p["bbox_mm"][0] for p in placed] + [p["bbox_mm"][2] for p in placed]
    ys = [p["bbox_mm"][1] for p in placed] + [p["bbox_mm"][3] for p in placed]
    placement = {
        "cdr": os.path.abspath(args.output),
        "page_mm": [page_w, page_h],
        "content_bbox_mm": [min(xs), min(ys), max(xs), max(ys)] if placed else None,
        "items": placed,
        "source_image": man.get("source_image"),
    }
    pp = os.path.join(out_dir, "placement.json")
    with open(pp, "w", encoding="utf-8") as f:
        json.dump(placement, f, ensure_ascii=False, indent=2)
    print(f"定位记录: {pp}")

    if args.preview:
        # 要点 3：ExportEx 在 X8 上不可靠，以 Export 为准，并且必须验文件。
        #
        # 这里原来写的是"先删掉旧预览，再导出，然后看文件在不在"。删除是为了让
        # "文件存在"等价于"这次导出真的写了"，但**在受限运行环境里会被安全策略
        # 判为批量删除并直接掐掉进程**（实测：日志只留一行
        # SAFE_DELETE_BULK_CONFIRM_REQUIRED，整条流水线莫名 EXIT=1，
        # 而 CDR 其实已经保存好了，很容易误判成保存失败）。
        #
        # 改成**指纹比对**，语义等价且不删任何文件：导出前记一次
        # `(大小, mtime_ns)`，导出后必须"文件存在且指纹变了"才算成功。
        # 旧文件被覆盖也会变 mtime，所以不会漏判。
        before = C.file_fingerprint(args.preview)
        ok = False
        for label, fn in (("Export", lambda: doc.Export(args.preview, CDR_PNG, 1, None, None)),
                          ("ExportEx", lambda: doc.ExportEx(args.preview, CDR_PNG, 1, None, None))):
            if ok:
                break
            try:
                fn()
            except Exception as exc:
                print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
                continue
            after = C.file_fingerprint(args.preview)
            if after is not None and after[0] > 0 and after != before:
                print(f"[OK] {label} 导出预览 {args.preview} ({after[0]} 字节)")
                ok = True
            else:
                print(f"[FAIL] {label} 未写出文件（X8 已知问题，改用 Export）")
        if not ok:
            print("[警告] 预览导出失败；可直接用 doc.Export 手动另存为 PNG。")

    print()
    print("=== 汇总 ===")
    print(f"页面 {page.SizeWidth:.2f} x {page.SizeHeight:.2f} mm，"
          f"活动页形状 {page.Shapes.All().Count}")
    for i in range(1, page.Layers.Count + 1):
        lay = page.Layers.Item(i)
        print(f"  {lay.Name:12s} {lay.Shapes.Count:3d} 个形状")
    return 0


if __name__ == "__main__":
    sys.exit(main())
