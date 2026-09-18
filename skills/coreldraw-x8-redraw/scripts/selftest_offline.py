"""离线自检：不需要 CorelDRAW、不需要联网。

覆盖两块：

A. 纯逻辑（不需要图像）
   - cdr_prompt_builder.render_prompt 的渲染
   - cdr_redraw.compare_profiles 的判定

B. 自动分区与度量（用**合成图**，有已知真值）
   - strip_residue     贯穿边缘的残留剥离
   - _tighten          区域外沿用宽松墨迹图向外扩、遇空隙即停
   - auto_partition    行中位数判色带 / 色带横向用未剥离墨迹 / 列方向切分
   - rasterize         超采样覆盖率（对比硬边填充的系统性偏差）

C. 文字转活字的判定（纯逻辑，不需要 OCR、不需要 CorelDRAW）
   - cc_sizes          连通分量（8 邻域 / 行程并查集 / 碎点过滤）
   - glyph_stats       文字与线稿的统计特征
   - decide_convert    四条判据，**用实测的两个案例当夹具**
                       （真页脚 n_cc=53/n_char=48；把插图区当文字区 n_cc=66/n_char=6）

D. OCR 预处理与变体选择（纯逻辑）
   - _otsu_gray        双峰图必须取平台中点，否则二值图全白
   - binarize          反白字的极性保护
   - pick_variant      用列投影词数选变体，**不能按置信度选**

E. 保存落盘核验与占用释放（纯逻辑）
   - _stat_sig         判断"保存有没有真的写进文件"
   - file_fingerprint  同上，公共模块版本
   - find_open_document 按绝对路径找已打开的文档（同名不同目录不能误伤）
   - release_document  重写目标 CDR 前解除 CorelDRAW 的占用；
                       Dirty=True 必须拒绝关闭

合成图是刻意设计的，每一处都对应一个真实踩过的坑：
左侧 4 列 + 顶部 3 行贯穿残留、满版色带被反白字掏空、同一行两个内容块、
细笔画文字带一圈比二值化阈值更浅的抗锯齿外沿。
真值全部以**切片语义**给出（x1/y1 是开区间）。

跑法：
    python scripts\\selftest_offline.py
退出码 0 = 全部通过。
"""

import os
import sys
import tempfile
import time
import types
from pathlib import Path

# ---- 注入 pywin32 桩模块，使 cdr_common 可被导入 ----
pythoncom = types.ModuleType("pythoncom")
pythoncom.CoInitialize = lambda: None
pythoncom.VT_ARRAY = 0
pythoncom.VT_DISPATCH = 0
sys.modules["pythoncom"] = pythoncom

win32com = types.ModuleType("win32com")
client = types.ModuleType("win32com.client")
client.GetActiveObject = lambda *a, **k: None
client.Dispatch = lambda *a, **k: None
client.VARIANT = object
constants = types.ModuleType("win32com.client.constants")
client.constants = constants
win32com.client = client
sys.modules["win32com"] = win32com
sys.modules["win32com.client"] = client
sys.modules["win32com.client.constants"] = constants

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np                                        # noqa: E402
from PIL import Image                                     # noqa: E402

import cdr_prompt_builder                                 # noqa: E402
import cdr_redraw                                         # noqa: E402
import cdr_common as C                                    # noqa: E402
import cdr_image_trace as T                               # noqa: E402
import cdr_bitmap_to_cdr as B                             # noqa: E402
import cdr_text_live as L                                 # noqa: E402
import cdr_scan_text as S                                 # noqa: E402


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

_FAILED = []


def check(name, cond, detail=""):
    """记录一条断言结果。"""
    if cond:
        print(f"  [通过] {name}")
    else:
        print(f"  [失败] {name}  {detail}")
        _FAILED.append(f"{name}  {detail}")


def near(a, b, tol=1):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# A. 纯逻辑
# ---------------------------------------------------------------------------


def fake_profile():
    return {
        "name": "样例设计稿.cdr",
        "full_file_name": r"D:\work\样例设计稿.cdr",
        "unit_code": 4,
        "page_count": 2,
        "shape_total": 37,
        "overall_type_counts": {
            "CurveShape(曲线)[3]": 20,
            "TextShape(文本)[6]": 10,
            "RectangleShape(矩形)[1]": 5,
            "BitmapShape(位图)[5]": 2,
        },
        "pages": [
            {
                "name": "页面 1",
                "width": 210.0,
                "height": 297.0,
                "orientation": 1,
                "layer_count": 3,
                "shape_total": 25,
                "layers": [
                    {"name": "刀模线", "visible": True, "editable": True, "printable": True,
                     "top_level": 5, "total_all": 5,
                     "type_counts": {"CurveShape(曲线)[3]": 5}, "text_items": []},
                    {"name": "图层 1", "visible": True, "editable": True, "printable": True,
                     "top_level": 18, "total_all": 18,
                     "type_counts": {"CurveShape(曲线)[3]": 10, "TextShape(文本)[6]": 8},
                     "text_items": ["产品名称", "规格：A4", "SN-0001"]},
                    {"name": "位图", "visible": True, "editable": True, "printable": True,
                     "top_level": 2, "total_all": 2,
                     "type_counts": {"BitmapShape(位图)[5]": 2}, "text_items": []},
                ],
            },
            {
                "name": "页面 2",
                "width": 210.0,
                "height": 297.0,
                "orientation": 1,
                "layer_count": 1,
                "shape_total": 12,
                "layers": [
                    {"name": "图层 1", "visible": True, "editable": True, "printable": True,
                     "top_level": 12, "total_all": 12,
                     "type_counts": {"RectangleShape(矩形)[1]": 5, "TextShape(文本)[6]": 2,
                                     "CurveShape(曲线)[3]": 5},
                     "text_items": ["封底"]},
                ],
            },
        ],
    }


def test_render_prompt():
    profile = fake_profile()
    prompt = cdr_prompt_builder.render_prompt(
        Path(r"D:\work\样例设计稿.cdr"), profile, None, "Millimeter(毫米)"
    )
    check("render_prompt 标题", "# 样例设计稿 定制 CDR 重绘提示词" in prompt)
    check("render_prompt 页面总数", "页面总数：2" in prompt)
    check("render_prompt 形状总数", "形状总数（含群组内部）：37" in prompt)
    check("render_prompt 页面尺寸行", "| 页面 1 | 210.0 | 297.0 |" in prompt)
    check("render_prompt 图层名", "刀模线" in prompt)
    check("render_prompt 文本内容", "SN-0001" in prompt)
    check("render_prompt 类型统计", "CurveShape(曲线)[3] | 20" in prompt)


def test_compare_profiles():
    profile = fake_profile()
    passed, diffs = cdr_redraw.compare_profiles(profile, fake_profile())
    check("compare_profiles 相同文件判 PASS", passed and not diffs, str(diffs))

    changed = fake_profile()
    changed["pages"][0]["layers"][1]["total_all"] = 15
    changed["page_count"] = 1
    passed2, diffs2 = cdr_redraw.compare_profiles(profile, changed)
    joined = "\n".join(diffs2)
    check("compare_profiles 检出页面数差异", not passed2 and "页面数不一致" in joined)
    check("compare_profiles 检出形状数差异", "图层 1 形状总数不一致" in joined)


# ---------------------------------------------------------------------------
# B. 自动分区（合成图，有已知真值）
# ---------------------------------------------------------------------------

# 合成图几何（全部是像素，切片语义：x1/y1 开区间）
W, H = 600, 800
RES_L, RES_T = 4, 3                    # 左侧残留列数、顶部残留行数
BLK_A = (50, 100, 200, 200)            # x0,y0,x1,y1  —— 左内容块（实心）
BLK_B = (400, 100, 550, 200)           # 右内容块（与 A 同一行带，靠列空隙切开）
BAND = (500, 580)                      # 满版色带的 y 范围
BAND_HOLE = (250, 520, 350, 560)       # 色带里被反白字掏空的区域
# 细笔画文字：8 行黑条（>= min_run_px=6，否则会被当噪点滤掉；真实 6pt 页脚是 17 行）
# + 紧邻其后 3 行的浅灰外沿（180，比二值化阈值 128 浅，模拟 descender/抗锯齿）
BAR = (200, 700, 400, 708)
HALO = (195, 708, 405, 711)

PAGE_W_MM = 300.0                      # 600px -> 0.5 mm/px，便于推算类型


def build_synthetic(out_path):
    """画一张有已知真值的合成图，返回它的 Source。"""
    img = np.full((H, W), 255, np.uint8)

    # 贯穿整幅的裁切残留：左侧 4 列、顶部 3 行（灰度 16）
    img[:, :RES_L] = 16
    img[:RES_T, :] = 16

    # 两个内容块（纯黑）
    img[BLK_A[1]:BLK_A[3], BLK_A[0]:BLK_A[2]] = 0
    img[BLK_B[1]:BLK_B[3], BLK_B[0]:BLK_B[2]] = 0

    # 满版色带（灰 18），中间掏出一块白（模拟反白字标）
    img[BAND[0]:BAND[1], :] = 18
    img[BAND_HOLE[1]:BAND_HOLE[3], BAND_HOLE[0]:BAND_HOLE[2]] = 255

    # 细笔画文字：黑条 + 紧邻其后的浅灰外沿（模拟 descender / 抗锯齿）
    img[BAR[1]:BAR[3], BAR[0]:BAR[2]] = 0
    img[HALO[1]:HALO[3], HALO[0]:HALO[2]] = 180

    Image.fromarray(img).convert("RGB").save(out_path)
    return T.Source(out_path, PAGE_W_MM)


def _find(regions, **want):
    """按给定键值挑出唯一的区域；找不到或有多于一个就返回 None。"""
    hits = [r for r in regions
            if all(r.get(k) == v for k, v in want.items())]
    return hits[0] if len(hits) == 1 else None


def test_strip_residue():
    ink = np.zeros((H, W), bool)
    ink[:, :RES_L] = True
    ink[:RES_T, :] = True
    ink[300:310, 100:110] = True          # 一个真实内容块，必须保留
    out, nl, nr, nt, nb = B.strip_residue(ink)
    check("strip_residue 识别左侧残留列数", nl == RES_L, f"得到 {nl}")
    check("strip_residue 识别顶部残留行数", nt == RES_T, f"得到 {nt}")
    check("strip_residue 右侧无误判", nr == 0, f"得到 {nr}")
    check("strip_residue 底部无误判", nb == 0, f"得到 {nb}")
    check("strip_residue 残留列已抹除", not out[:, :RES_L].any())
    check("strip_residue 残留行已抹除", not out[:RES_T, :].any())
    check("strip_residue 真实内容未被误抹", out[300:310, 100:110].all())
    # 残留条会让**每一行**都有墨迹像素——这正是行投影找不到空隙的根因
    check("strip_residue 剥离前每行都有墨迹（说明必须剥离）",
          bool((ink.sum(axis=1) > 0).all()))


def test_tighten():
    ink_ext = np.zeros((H, W), bool)
    ink_ext[BAR[1]:BAR[3], BAR[0]:BAR[2]] = True      # 严格部分
    ink_ext[HALO[1]:HALO[3], HALO[0]:HALO[2]] = True  # 外沿部分
    r = {"kind": "content", "y0": BAR[1], "y1": BAR[3], "x0": BAR[0], "x1": BAR[2]}
    out = B._tighten(ink_ext, r)
    check("_tighten 下沿扩到外沿底部", out["y1"] == HALO[3],
          f"期望 {HALO[3]}，得到 {out['y1']}")
    check("_tighten 左沿扩到外沿左端", out["x0"] == HALO[0],
          f"期望 {HALO[0]}，得到 {out['x0']}")
    check("_tighten 右沿扩到外沿右端", out["x1"] == HALO[2],
          f"期望 {HALO[2]}，得到 {out['x1']}")
    check("_tighten 上沿不越过真实边界", out["y0"] == BAR[1],
          f"期望 {BAR[1]}，得到 {out['y0']}")

    # 遇空隙即停：把外沿挪到 6 行之外，就不该再被吸过来
    far = np.zeros((H, W), bool)
    far[BAR[1]:BAR[3], BAR[0]:BAR[2]] = True
    far[HALO[3] + 6:HALO[3] + 9, BAR[0]:BAR[2]] = True
    out2 = B._tighten(far, dict(r))
    check("_tighten 遇空隙即停（不跨空白吸邻块）", out2["y1"] == BAR[3],
          f"期望 {BAR[3]}，得到 {out2['y1']}")


def test_auto_partition(tmpdir):
    src = build_synthetic(os.path.join(tmpdir, "synthetic.png"))
    color = np.array(Image.open(src.path).convert("RGB"))
    regions, residue = B.auto_partition(src, color, col_gap_frac=0.05)

    check("auto_partition 残留计数正确",
          residue == {"left": RES_L, "right": 0, "top": RES_T, "bottom": 0},
          str(residue))

    # ---- 满版色带 ----
    band = _find(regions, kind="band")
    check("auto_partition 找出 1 条满版色带", band is not None,
          f"区域数 {len(regions)}: {[(r.get('kind'), r['y0'], r['y1']) for r in regions]}")
    if band:
        check("色带 y 范围正确", near(band["y0"], BAND[0]) and near(band["y1"], BAND[1]),
              f"期望 {BAND}，得到 ({band['y0']},{band['y1']})")
        # 关键：色带横向范围用**未剥离**的墨迹算，必须顶到 x=0，
        # 而不是被残留剥离缩到 x=4（满版底色本来就该顶到页边）
        check("色带横向顶到页边（用未剥离墨迹）",
              band["x0"] == 0 and band["x1"] == W,
              f"期望 (0,{W})，得到 ({band['x0']},{band['x1']})")
        check("色带标记为反白", band["invert"] is True)
        check("色带颜色取自源图中位数", band.get("color") is not None
              and max(abs(c - 18) for c in band["color"]) <= 2,
              f"得到 {band.get('color')}")
        # 色带中间被反白字掏空；行中位数仍是深色，而**逐行**覆盖率会掉下来。
        # 用整体覆盖率会看不出来（空洞只占色带一半高度，整体覆盖率还有 0.92），
        # 这正是"覆盖率法"必须按行看、而按行看又会把色带切碎的原因。
        rowcov = (src.gray[BAND[0]:BAND[1]] < 128).mean(axis=1)
        check("行中位数法不受反白字掏空影响",
              float(np.median(src.gray[BAND[0]:BAND[1]])) < 128)
        check("色带中段的逐行覆盖率会掉到 0.90 以下（所以不能用覆盖率判）",
              float(rowcov.min()) < 0.90, f"最低逐行覆盖率 {float(rowcov.min()):.3f}")
        check("而整体覆盖率看不出问题（说明必须逐行看）",
              float((src.gray[BAND[0]:BAND[1]] < 128).mean()) > 0.90,
              f"整体覆盖率 {float((src.gray[BAND[0]:BAND[1]] < 128).mean()):.3f}")

    # ---- 同一行带里的两个内容块，靠列空隙切开 ----
    left = _find(regions, kind="content", y0=BLK_A[1], y1=BLK_A[3], x0=BLK_A[0], x1=BLK_A[2])
    right = _find(regions, kind="content", y0=BLK_B[1], y1=BLK_B[3], x0=BLK_B[0], x1=BLK_B[2])
    check("列方向切开左内容块", left is not None)
    check("列方向切开右内容块", right is not None)

    # ---- 细笔画文字：严格框被外沿扩到真实边界 ----
    text = None
    for r in regions:
        if r.get("kind") == "content" and near(r["y0"], BAR[1], 2) and r["x0"] < BAR[0]:
            text = r
            break
    check("找到细笔画文字区域", text is not None,
          f"区域: {[(r.get('kind'), r['y0'], r['y1'], r['x0'], r['x1']) for r in regions]}")
    if text:
        check("文字区域下沿被外沿补全（未切掉 descender）",
              near(text["y1"], HALO[3]), f"期望 {HALO[3]}，得到 {text['y1']}")
        check("文字区域横向被外沿补全",
              near(text["x0"], HALO[0]) and near(text["x1"], HALO[2]),
              f"期望 ({HALO[0]},{HALO[2]})，得到 ({text['x0']},{text['x1']})")

    # ---- 命名与类型 ----
    # 注意 name 与 trace_name 是两件事：色带的 name 是 band01（垫底矩形），
    # trace_name 是 band01_ink（反白描摹），两者在 CDR 里落到不同图层。
    named = B.name_regions(regions, src)
    by_trace = {r["trace_name"]: r for r in named}
    by_name = {r["name"]: r for r in named}
    check("共 4 个区域（1 色带 + 2 内容块 + 1 文字）", len(named) == 4,
          str(sorted(by_trace)))
    check("色带的 name 是 band01（垫底矩形）", "band01" in by_name, str(sorted(by_name)))
    check("色带的 trace_name 是 band01_ink（反白描摹，与矩形分开）",
          "band01_ink" in by_trace, str(sorted(by_trace)))
    check("色带的 trace_type 是 INK", 
          by_trace.get("band01_ink", {}).get("trace_type") == "INK")
    check("色带 type 是 BAND", by_trace.get("band01_ink", {}).get("type") == "BAND")
    text_names = sorted(n for n, r in by_trace.items() if r.get("type") == "TEXT")
    check("细笔画文字被判为 TEXT（高 4mm < 6mm）", text_names == ["text01"],
          str(text_names))
    art_names = sorted(n for n, r in by_trace.items() if r.get("type") == "ART")
    check("两个内容块被判为 ART（宽 75mm，不小于 60mm）",
          art_names == ["art01", "art02"], str(art_names))


def test_rasterize_supersample():
    """超采样必须给出**覆盖率**；硬边填充会系统性低估面积（这里是高估填充量）。

    注意这里断言的是"偏差的**量级**"，不是理想化的精确面积：
    `cv2.fillPoly` 把多边形左右两个端点列都算作填充，于是每条扫描线多出
    1 个**超采样**像素，折算到输出网格是恒定的 +1/ss 像素。ss=8 时 +0.125px，
    ss=1 时 +1.0px。这个恒定偏差对 1px 细笔画就是 +100% 的面积，正是它让
    寻优一路选到最粗的阈值；超采样把偏差压到 1/8，方向虽仍是偏粗，
    但已经小到不影响阈值选择（实测选中 138 而非 148，与手工细调一致）。
    """

    def bar(w, ss):
        svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<svg xmlns="http://www.w3.org/2000/svg" width="%gmm" height="1mm" '
               'viewBox="0 0 %g 1">\n'
               '<path fill="#111111" fill-rule="evenodd" '
               'd="M0,0 L%g,0 L%g,1 L0,1 Z"/>\n</svg>\n' % (w, w, w, w))
        return float(T.rasterize(svg, int(w) + 4, 1, 1.0, 0.0, 0.0, ss=ss).sum())

    cov8 = T.rasterize(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="2mm" height="1mm" '
        'viewBox="0 0 2 1">'
        '<path fill="#111111" fill-rule="evenodd" d="M0,0 L0.5,0 L0.5,1 L0,1 Z"/></svg>',
        2, 1, 1.0, 0.0, 0.0, ss=8)
    check("超采样返回浮点覆盖率（不是 0/1 布尔）", cov8.dtype.kind == "f", str(cov8.dtype))

    for w in (1.0, 3.0, 10.0):
        e8 = bar(w, 8) - w
        e1 = bar(w, 1) - w
        check(f"超采样({w:g}px) 恒定偏差 ≤ 1/ss = 0.125px", e8 <= 0.13,
              f"偏差 {e8:+.3f}")
        check(f"硬边({w:g}px) 恒定偏差 = 1.0px（系统性偏粗）",
              abs(e1 - 1.0) < 1e-6, f"偏差 {e1:+.3f}")
        check(f"细笔画下超采样把相对误差从 {e1 / w * 100:.0f}% 降到 {e8 / w * 100:.0f}%",
              e8 / w < e1 / w / 4, f"{e8 / w:.3f} vs {e1 / w:.3f}")

    check("硬边填充（ss=1）返回布尔",
          T.rasterize('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
                      'width="2mm" height="1mm" viewBox="0 0 2 1">'
                      '<path d="M0,0 L0.5,0 L0.5,1 L0,1 Z"/></svg>',
                      2, 1, 1.0, 0.0, 0.0, ss=1).dtype == bool)


def test_corner_segments():
    """角点段必须输出**两个** L（顶点 + 段末中点），漏掉顶点会把角整个切掉。

    真踩过：一个 174x217 的实心洋红色块，召回只剩 **75%**，而且五个
    二值化阈值全在 75% 上下打转——因为它是几何错误，不是参数问题。

    根因在 potrace 的曲线表示：段 j 的 `c[1]` 是多边形顶点 `v_j`、
    `c[2]` 是**下一条边的中点** `mid(v_j, v_{j+1})`（见 potracer
    `_smooth` 里 `c[1]=vertex`、`c[2]=p4=interval(1/2, vertex[k], vertex[j])`）。
    所以一条角点段是"经过顶点、到下一个中点"的折线；只写 `L c[2]`
    就等于只连各边中点——**实心矩形被描成菱形**。

    一般图形察觉不到，是因为 potrace 的多边形顶点通常密到 ~1px 一个，
    切掉顶点只损失不到 1px。只有顶点稀疏的简单形状（实心矩形、直边色块）
    才暴露。所以这条断言必须用**稀疏多边形**做夹具。
    """

    def trace(m):
        svg, subs, _ = T.mask_to_svg(m, 1.0, 0, 0, 0, 1.0, 0.1)
        vx, vy = T.svg_viewbox(svg)[:2]
        cov = T.rasterize(svg, m.shape[1], m.shape[0], 1.0, vx, vy, ss=4)
        a = cov >= 0.5
        return svg, subs, a

    H, W = 40, 60
    m = np.zeros((H, W), bool)
    m[5:35, 10:50] = True
    svg, subs, a = trace(m)
    d = subs[0]
    for cx, cy in ((10, 5), (50, 5), (50, 35), (10, 35)):
        check(f"实心矩形角点 ({cx},{cy}) 出现在路径里（切角时会缺）",
              f"L{cx}.0000,{cy}.0000" in d, d[:70])
    iou = (a & m).sum() / (a | m).sum()
    check("实心矩形描摹 IoU ≥ 99%（只连边中点时会掉到 ~50%）", iou >= 0.99,
          f"IoU {iou * 100:.2f}%")
    check("实心矩形渲染面积与掩膜同量级（切角时会只剩一半）",
          abs(int(a.sum()) - int(m.sum())) / m.sum() < 0.05,
          f"{a.sum()} vs {m.sum()}")

    # 贴着画布右边界的实心块：描摹结果不能出现楔形缺口
    m2 = np.zeros((H, W), bool)
    m2[5:35, 10:W] = True
    _, _, a2 = trace(m2)
    iou2 = (a2 & m2).sum() / (a2 | m2).sum()
    check("贴右边界的实心块 IoU ≥ 99%（曾出现楔形缺口）", iou2 >= 0.99,
          f"IoU {iou2 * 100:.2f}%")

    # 带镂空：内轮廓也必须闭合、不被切角
    m3 = m.copy()
    m3[15:25, 25:45] = False
    _, subs3, a3 = trace(m3)
    iou3 = (a3 & m3).sum() / (a3 | m3).sum()
    check("带矩形镂空 IoU ≥ 98%", iou3 >= 0.98, f"IoU {iou3 * 100:.2f}%")
    check("镂空区在渲染结果里确实是空的", not a3[19, 34], str(a3[19, 34]))

    # 两个分离的实心块：各是一条子路径，都不许被切角
    m4 = np.zeros((H, W), bool)
    m4[5:35, 10:30] = True
    m4[10:30, 35:50] = True
    _, subs4, a4 = trace(m4)
    iou4 = (a4 & m4).sum() / (a4 | m4).sum()
    check("两个分离实心块 -> 2 条子路径", len(subs4) == 2, str(len(subs4)))
    check("两个分离实心块 IoU ≥ 99%", iou4 >= 0.99, f"IoU {iou4 * 100:.2f}%")


def test_full_bleed_guard():
    """满版色带的横向贯通判据：每一列都必须有墨迹。"""

    ink = np.zeros((H, W), bool)
    # 真满版色带：横向贯通（中间被反白字掏空，但字的上下仍有纯底色行）
    ink[500:580, :] = True
    ink[520:560, 250:350] = False
    check("满版色带（中间被反白字掏空）判定为贯通",
          B._is_full_bleed(ink, 500, 580) is True)

    # 两个并排实心块：中间 200 列一个墨迹像素都没有
    two = np.zeros((H, W), bool)
    two[100:200, 50:200] = True
    two[100:200, 400:550] = True
    check("两个并排实心块判定为不贯通（不会被误判成色带）",
          B._is_full_bleed(two, 100, 200) is False)

    # 行中位数确实会把上面那种情况判成"深"——所以光靠中位数不够
    check("两个并排实心块确实能把行中位数压到阈值以下（说明必须有贯通判据）",
          float(np.median(np.where(two[100:200], 0, 255))) < 128)

    check("空区间判为不贯通", B._is_full_bleed(ink, 500, 500) is False)


def test_pick_thresholds():
    thr = [112, 118, 128, 138, 148]
    check("小区域扫全部候选阈值", B.pick_thresholds(1000, thr, 200000) == thr)
    check("大区域降到 3 档（首/中/末）",
          B.pick_thresholds(10 ** 7, thr, 200000) == [112, 128, 148],
          str(B.pick_thresholds(10 ** 7, thr, 200000)))
    check("候选本来就 ≤3 档时不动", B.pick_thresholds(10 ** 7, [112, 128], 200000) == [112, 128])


def test_shim_source(tmpdir):
    """--report-only 的清单容错：缺字段不能崩。"""
    manifest = {"source_image": "a.png"}
    src = B._ShimSource(manifest, "a.png")
    check("_ShimSource 缺 image_px 不崩", src.img_w == 0 and src.img_h == 0)
    check("_ShimSource 缺 page_mm 不崩", src.page_w_mm == 0.0)
    check("_ShimSource 缺 mm_per_px 不崩", src.mm_per_px == 0.0)

    full = {"source_image": "a.png", "image_px": [600, 800],
            "page_mm": [300.0, 400.0], "mm_per_px": 0.5}
    src2 = B._ShimSource(full, "a.png")
    check("_ShimSource 正常清单取值正确",
          src2.img_w == 600 and src2.page_w_mm == 300.0
          and near(src2.mm_per_px, 0.5, 1e-9)
          and near(src2.page_h_mm_derived, 400.0, 1e-6))


# ---------------------------------------------------------------------------
# C. 文字转活字的判定
# ---------------------------------------------------------------------------

def test_cc_sizes():
    """连通分量：8 邻域、行程并查集、碎点过滤。

    8 邻域这个选择不是随意的：小字号下 'i' 的点与杆经常只对角相接，
    用 4 邻域会把一个字形数成两个，直接把"分量数/字符数"判据顶穿。
    """
    check("空图返回空列表", L.cc_sizes(np.zeros((10, 20), bool)) == [])

    m = np.zeros((10, 20), bool)
    m[1:4, 1:4] = True
    m[1:4, 10:13] = True
    check("两个分离方块 → 2 个分量", L.cc_sizes(m) == [9, 9], str(L.cc_sizes(m)))

    d = np.zeros((10, 10), bool)
    d[1, 1] = True
    d[2, 2] = True
    check("对角相接算连通（8 邻域；4 邻域会数成 2 个）",
          len(L.cc_sizes(d)) == 1, str(L.cc_sizes(d)))

    # 同一行两个行程被下一行的一个行程连起来：必须并成一个
    u = np.zeros((4, 12), bool)
    u[0, 1:3] = True
    u[0, 6:8] = True
    u[1, 2:7] = True
    check("跨行的多个行程并成一个分量", L.cc_sizes(u) == [2 + 2 + 5],
          str(L.cc_sizes(u)))

    s = np.zeros((10, 10), bool)
    s[1:4, 1:4] = True
    s[8, 8] = True
    check("1px 碎点被默认 min_px=2 滤掉", L.cc_sizes(s) == [9], str(L.cc_sizes(s)))
    check("min_px=1 时碎点保留", len(L.cc_sizes(s, min_px=1)) == 2,
          str(L.cc_sizes(s, min_px=1)))


def test_glyph_stats():
    """文字与线稿的统计特征差在哪——用合成图把方向钉死。"""
    # 文字样：20 个同样大小的竖条，间距均匀
    txt = np.zeros((20, 240), bool)
    for k in range(20):
        txt[4:16, 4 + k * 11:8 + k * 11] = True
    g1 = L.glyph_stats(txt)
    check("文字样：分量数 = 字形数", g1["n_cc"] == 20, str(g1["n_cc"]))
    check("文字样：最大/中位 = 1（字形大小均匀）",
          near(g1["cc_max_over_median"], 1.0, 0.01), str(g1["cc_max_over_median"]))

    # 线稿样：一大块实心 + 几个小碎块
    art = np.zeros((60, 200), bool)
    art[5:55, 5:60] = True
    for k in range(8):
        art[10:14, 80 + k * 12:88 + k * 12] = True
    g2 = L.glyph_stats(art)
    check("线稿样：最大/中位 远大于文字样",
          g2["cc_max_over_median"] > 20, str(g2["cc_max_over_median"]))
    check("线稿样的尺寸离散度也远大于文字样",
          g2["cc_size_cv"] > g1["cc_size_cv"] * 3,
          f'{g2["cc_size_cv"]:.3f} vs {g1["cc_size_cv"]:.3f}')

    check("空图不崩", L.glyph_stats(np.zeros((5, 5), bool))["n_cc"] == 0)


def test_decide_convert():
    """四条判据。夹具是**实测值**，不是编的数：

        页脚（真文字，应转）  n_cc=53  n_char=48  wr=0.992  iou=0.7235  lift=1.68
        插图（非文字，应拒）  n_cc=66  n_char=6   wr=1.887  iou=0.3859  lift=1.53

    第二行就是修复前的真 bug：C 判据没过，但 D 判据（lift 1.531 > 1.25 且
    iou 0.3859 > 0.35）全过，于是在 CDR 里建了一行 'wander'。

    （页脚那行是 IoU 口径改成"逐词对齐"之后重测的：0.7244/2.675 → 0.7235/1.68。
    绝对值几乎没动，但 lift 掉了一半——因为中位候选的 IoU 也一起抬高了，
    相对优势本就比整行口径下小。这正是"换度量必须连门槛一起重校"的原因。）
    """
    FOOTER = dict(final_iou=0.7235, lift=1.68, width_ratio=0.992,
                  n_cc=53, n_char=48)
    ART = dict(final_iou=0.3859, lift=1.531, width_ratio=1.887,
               n_cc=66, n_char=6)

    d = L.decide_convert(**FOOTER)
    check("真文字判 convert", d["verdict"] == "convert", str(d["reasons"]))
    check("真文字四条判据全过",
          all(c["ok"] for c in d["checks"].values()), str(d["checks"]))
    check("convert 时没有拒绝理由、没有 reject_kind",
          d["reasons"] == [] and d["reject_kind"] is None, str(d))

    d = L.decide_convert(**ART)
    check("非文字区判 keep_trace（修复前是 convert）",
          d["verdict"] == "keep_trace", str(d["checks"]))
    check("非文字区归为 not_text", d["reject_kind"] == "not_text",
          str(d["reject_kind"]))
    check("非文字区同时触发**两条**独立理由（分量比 + 宽度比）",
          len(d["reasons"]) == 2, str(d["reasons"]))

    # 把两条硬门槛放开，这个案例就会被放过——证明"拦住的正是这两条"
    old = L.decide_convert(**ART, max_cc_per_char=1e9, max_wr=1e9)
    check("放开两条硬门槛后非文字区确实会被放过（说明门槛真在起作用）",
          old["verdict"] == "convert", str(old["checks"]))
    check("且此时只剩相似度判据在管（D 过、C 不过）",
          old["checks"]["abs_iou"]["ok"] is False
          and old["checks"]["rel_lift"]["ok"] is True, str(old["checks"]))

    # 边界
    check("cc/字符 正好 4.0 通过",
          L.decide_convert(0.9, 2.0, 1.0, 40, 10)["checks"]["cc_per_char"]["ok"])
    check("cc/字符 4.1 不通过",
          not L.decide_convert(0.9, 2.0, 1.0, 41, 10)["checks"]["cc_per_char"]["ok"])
    check("宽度比 1.50 通过",
          L.decide_convert(0.9, 2.0, 1.50, 40, 20)["checks"]["width_ratio"]["ok"])
    check("宽度比 1.51 不通过",
          not L.decide_convert(0.9, 2.0, 1.51, 40, 20)["checks"]["width_ratio"]["ok"])
    check("宽度比 0.65 通过",
          L.decide_convert(0.9, 2.0, 0.65, 40, 20)["checks"]["width_ratio"]["ok"])
    check("宽度比 0.64 不通过",
          not L.decide_convert(0.9, 2.0, 0.64, 40, 20)["checks"]["width_ratio"]["ok"])

    # 像文字、几何也合理，但字体库里没有够接近的 → 该保留描摹轮廓
    d = L.decide_convert(0.30, 1.05, 1.0, 20, 12)
    check("像文字但字体都不像 → keep_trace 且归为 weak_match",
          d["verdict"] == "keep_trace" and d["reject_kind"] == "weak_match",
          str(d))
    check("weak_match 的理由只提相似度（不冤枉成 not_text）",
          len(d["reasons"]) == 1 and "相似度不足" in d["reasons"][0],
          str(d["reasons"]))

    # 大字号：绝对 IoU 单独就该放行（不依赖 lift）
    d = L.decide_convert(0.95, 1.02, 1.0, 30, 25)
    check("大字号靠绝对 IoU 放行（lift 只有 1.02）",
          d["verdict"] == "convert", str(d["checks"]))

    # 字符数为 0 不能除零
    d = L.decide_convert(0.9, 2.0, 1.0, 0, 0)
    check("字符数为 0 不崩（按 1 算，比值 0 落在下限外 → 拒绝）",
          d["verdict"] == "keep_trace", str(d["checks"]))


def test_otsu_gray():
    """Otsu 的平台期必须取中点——取第一个最大值会把阈值落在峰边缘上。

    实测踩过：一张只有 40 与 220 两个灰度的图，类间方差在 t=40..219 上
    **完全相等**，严格大于保留第一个就返回 40，`a < 40` 一个像素都不剩，
    二值图全白，OCR 直接失效。这和"参数有平台期时不能只比两个点"同类。
    """
    two = np.concatenate([np.full(500, 40, np.uint8),
                          np.full(500, 220, np.uint8)]).reshape(25, 40)
    t = L._otsu_gray(two)
    check("双峰图 Otsu 取平台中点（129），不是峰边缘（40）", t == 129, f"得到 {t}")
    b = np.asarray(L.binarize(Image.fromarray(two), 'otsu')) < 128
    check("双峰图二值后墨迹占比 ≈ 0.5（不是 0 或 1）",
          near(float(b.mean()), 0.5, 0.01), f"得到 {float(b.mean()):.3f}")

    three = np.concatenate([np.full(300, 30, np.uint8), np.full(400, 128, np.uint8),
                            np.full(300, 240, np.uint8)]).reshape(20, 50)
    check("三峰图不崩且阈值落在中间带",
          30 < L._otsu_gray(three) < 240, str(L._otsu_gray(three)))

    check("全同灰度返回 128（不除零）",
          L._otsu_gray(np.full((5, 5), 200, np.uint8)) == 128)
    check("空图返回 128（不崩）", L._otsu_gray(np.zeros((0, 0), np.uint8)) == 128)


def test_binarize_polarity():
    """二值化的极性保护：反白字不该被二值成一团黑。"""
    dark_text = np.full((20, 60), 240, np.uint8)
    dark_text[5:15, 10:20] = 30
    b = np.asarray(L.binarize(Image.fromarray(dark_text), 128)) < 128
    check("深字浅底：墨迹就是字",
          near(float(b.mean()), 100 / 1200.0, 0.01), f"{float(b.mean()):.3f}")

    inv = np.asarray(L.binarize(Image.fromarray(255 - dark_text), 128)) < 128
    check("浅字深底（反白）：极性保护后墨迹仍是字，不是整块",
          near(float(inv.mean()), 100 / 1200.0, 0.01), f"{float(inv.mean()):.3f}")
    check("反白输入的二值结果与正相输入一致",
          bool((b == inv).all()))


def test_pick_variant():
    """OCR 变体选择：**必须用列投影的词数，不能用置信度**。

    夹具是实测值（源图直裁 371x16 页脚，真值 9 词 / 48 字符）：

        变体              置信    词数  词距  正确?
        4x_20留白         0.921    7     2     ✗   ← 置信度最高
        2x_二值118        0.918    9     0     ✓
        4x_二值118        0.918    9     0     ✓
        ...

    按置信度选会选到错误的那一个（0.921 > 0.918）。这是"置信度是自报的，
    列投影是独立几何证据"的直接证据。
    """
    def V(tag, score, words, chars, gap):
        return {'variant': tag, 'text': 'x' * chars, 'chars': chars,
                'words': words, 'score': score, 'lines': 1,
                'word_gap': gap}

    real = [
        V('1x_宽留白', 0.893, 1, 48, 8),
        V('2x_无纵留白', 0.885, 3, 47, 6),
        V('2x_微纵留白', 0.897, 7, 48, 2),
        V('3x_20留白', 0.894, 5, 47, 4),
        V('4x_20留白', 0.921, 7, 48, 2),      # 置信度最高但错
        V('2x_二值118', 0.918, 9, 48, 0),
        V('4x_二值118', 0.918, 9, 48, 0),
        V('2x_二值128', 0.908, 9, 48, 0),
        V('4x_二值128', 0.917, 9, 48, 0),
        V('4x_二值Otsu', 0.907, 9, 48, 0),
        V('1x_二值118_宽留白', 0.894, 9, 48, 0),
    ]
    pick = L.pick_variant([dict(r) for r in real])
    check("选出词距为 0 的变体，而不是置信度最高的那个",
          pick['word_gap'] == 0, f"选出 {pick['variant']} gap={pick['word_gap']}")
    check("选出的变体确实是二值化那套", '二值' in pick['variant'],
          pick['variant'])
    check("词距为 0 的变体置信度（0.918）低于被淘汰的（0.921）——"
          "说明判据真的不是置信度",
          pick['score'] < max(r['score'] for r in real),
          f"{pick['score']} vs {max(r['score'] for r in real)}")
    check("同词距时取置信度更高者（0.918 > 0.917 > 0.908）",
          near(pick['score'], 0.918, 1e-9), str(pick['score']))

    # 旧行为：不给几何证据时退化为按置信度选 —— 会选错，记录这一点防止回退
    old = [dict(r, word_gap=0) for r in real]
    check("不给词数证据时按置信度选，会选到错误的 4x_20留白（旧行为，已废弃）",
          L.pick_variant(old)['variant'] == '4x_20留白')

    # 几何证据本身失效（词距全 >0）时才允许"多数变体字符数"翻盘
    allbad = [V('a', 0.9, 5, 48, 3), V('b', 0.8, 6, 47, 2),
              V('c', 0.7, 7, 47, 1), V('d', 0.6, 8, 47, 4)]
    check("词距全 >0 时退而取被最多变体支持的字符数（47）",
          L.pick_variant([dict(r) for r in allbad])['chars'] == 47)

    check("单个变体也能选（不崩）",
          L.pick_variant([V('only', 0.5, 9, 48, 0)])['variant'] == 'only')


def test_stat_sig(tmpdir):
    """保存落盘核验用的文件指纹 `(大小, mtime_ns)`。

    这是判断"保存到底有没有写进去"的**唯一判据**，契约要测死：
    - 文件不存在 → `(None, None)`（新建文档时是正常情况，不能当成失败）
    - 文件存在 → 大小正确
    - **写了必须变**，**没写必须不变** —— 少任何一半，核验都会失去意义

    动机是一次真实事故：目标 CDR 被另一个进程打开成只读时，`doc.Save()`
    既不抛异常也不写文件，日志照样打印"已保存"，盘上却还是旧内容。
    当时整轮跑完、回读核验全对，差点当成交付完成。
    """
    p = os.path.join(tmpdir, 'sig.txt')

    check("文件不存在时返回 (None, None)，且不抛异常",
          L._stat_sig(p) == (None, None))

    with open(p, 'w') as f:
        f.write('abc')
    s1 = L._stat_sig(p)
    check("文件存在时大小正确", s1[0] == 3, f"得到 {s1[0]}")
    check("mtime_ns 已填充", isinstance(s1[1], int) and s1[1] > 0, f"得到 {s1[1]}")

    time.sleep(0.01)
    with open(p, 'w') as f:
        f.write('abcdef')
    s2 = L._stat_sig(p)
    check("真的写入后指纹必须变（否则漏报）", s2 != s1, f"{s1} -> {s2}")

    s3 = L._stat_sig(p)
    check("没有写入时指纹保持不变（否则核验形同虚设，抓不到\"没落盘\"）",
          s3 == s2, f"{s2} -> {s3}")


class _FakeDoc:
    """最小 Document 桩，只实现 release_document 用到的属性。"""

    def __init__(self, path, dirty=False, full=True):
        self._path = path
        self.Dirty = dirty
        self.closed = 0
        self._full = full

    @property
    def FullFileName(self):
        return self._path if self._full else ""

    @property
    def FilePath(self):
        return os.path.dirname(self._path) + os.sep

    @property
    def Name(self):
        return os.path.basename(self._path)

    def Close(self):
        self.closed += 1


class _FakeDocs:
    def __init__(self, docs):
        self._docs = list(docs)

    @property
    def Count(self):
        return len(self._docs)

    def Item(self, i):
        return self._docs[i - 1]


class _FakeApp:
    def __init__(self, docs=()):
        self.Documents = _FakeDocs(docs)


def test_release_document(tmpdir):
    """`file_fingerprint` / `find_open_document` / `release_document`。

    动机是一连串真实故障，三条互相咬合：

    1. `cdr_text_live.py --apply` 走 `open_document()`，**刻意只开不关**
       （把成果留在 CorelDRAW 窗口里给用户接着改）；
    2. 于是**下一轮**重建时目标 CDR 被自己上一轮留下的文档占着，
       `os.replace` 抛 `PermissionError [WinError 32]`，流水线在第 3 步前就死；
    3. 更坏的是 `SaveAs` / `Save` 在被占用时**静默失败** —— 不抛异常、不写盘，
       日志照常"已保存"，只有磁盘没变。整轮跑完、回读内存核验还全对。

    所以这里要测死的契约：
    - 路径比对按**绝对路径规范化**，不同目录的同名文件不能误伤；
    - `FullFileName` 拿不到时要能退回 `FilePath + Name`；
    - **Dirty=True 必须拒绝关闭**（那是用户的改动，不能替用户丢）；
    - 一切"没能关掉"的路径都必须返回 False，让调用方看得见。
    """
    # ---- file_fingerprint ----
    miss = os.path.join(tmpdir, "nope.cdr")
    check("指纹：文件不存在返回 None", C.file_fingerprint(miss) is None)

    p = os.path.join(tmpdir, "sig.cdr")
    with open(p, "wb") as f:
        f.write(b"x" * 7)
    fp = C.file_fingerprint(p)
    st = os.stat(p)
    check("指纹：(大小, mtime_ns) 与 os.stat 一致",
          fp == (st.st_size, st.st_mtime_ns), f"得到 {fp}")

    time.sleep(0.01)
    with open(p, "ab") as f:
        f.write(b"yy")
    check("指纹：追加写入后必须变（否则核验抓不到\"没落盘\"）",
          C.file_fingerprint(p) != fp)

    # ---- find_open_document ----
    target = os.path.join(tmpdir, "panel.cdr")
    # 必须真的落到磁盘上：release_document 第一道闸就是"文件存不存在"，
    # 不建文件的话后面每条都会短路成 (False, '文件不存在')，测了个寂寞。
    with open(target, "wb") as f:
        f.write(b"cdr")
    other_dir = os.path.join(tmpdir, "sub")
    os.makedirs(other_dir, exist_ok=True)
    same_name = os.path.join(other_dir, "panel.cdr")

    d_hit = _FakeDoc(target)
    d_same = _FakeDoc(same_name)
    d_other = _FakeDoc(os.path.join(tmpdir, "other.cdr"))
    app = _FakeApp([d_same, d_other, d_hit])

    check("查找：按绝对路径命中正确的那一个",
          C.find_open_document(app, target) is d_hit)
    check("查找：不同目录的同名文件不会误伤",
          C.find_open_document(app, same_name) is d_same)
    check("查找：没打开时返回 None",
          C.find_open_document(app, os.path.join(tmpdir, "ghost.cdr")) is None)

    check("查找：大小写与斜杠方向不同也能命中",
          C.find_open_document(
              _FakeApp([d_hit]), target.replace("\\", "/").upper()) is d_hit)

    d_nofull = _FakeDoc(target, full=False)
    check("查找：FullFileName 为空时退回 FilePath + Name",
          C.find_open_document(_FakeApp([d_nofull]), target) is d_nofull)

    check("查找：文档集合为空时安全返回 None",
          C.find_open_document(_FakeApp([]), target) is None)

    # ---- release_document ----
    real_attach = C.attach_coreldraw
    try:
        C.attach_coreldraw = lambda *a, **k: None
        check("释放：文件不存在 -> (False, 文件不存在)",
              C.release_document(miss) == (False, "文件不存在"))
        check("释放：附加不上 CorelDRAW -> 返回 False 而不是抛异常",
              C.release_document(target) == (False, "无法附加到 CorelDRAW"))
    finally:
        C.attach_coreldraw = real_attach

    d_absent = _FakeDoc(os.path.join(tmpdir, "other.cdr"))
    try:
        C.attach_coreldraw = lambda *a, **k: _FakeApp([d_absent])
        ok, why = C.release_document(target)
        check("释放：文档没打开 -> (False, 未在 CorelDRAW 中打开)",
              (ok, why) == (False, "未在 CorelDRAW 中打开"), f"得到 {why}")

        # Dirty=False：干净文档，可以关
        d_clean = _FakeDoc(target, dirty=False)
        C.attach_coreldraw = lambda *a, **k: _FakeApp([d_clean])
        ok, why = C.release_document(target)
        check("释放：干净文档被关闭", ok is True and why == "已关闭",
              f"得到 {(ok, why)}")
        check("释放：确实调用了 Close()", d_clean.closed == 1,
              f"Close 调用了 {d_clean.closed} 次")

        # Dirty=True：有未保存改动，必须拒绝
        d_dirty = _FakeDoc(target, dirty=True)
        C.attach_coreldraw = lambda *a, **k: _FakeApp([d_dirty])
        ok, why = C.release_document(target)
        check("释放：Dirty=True 必须拒绝关闭（用户的改动不能替用户丢）",
              ok is False and "拒绝关闭" in why, f"得到 {(ok, why)}")
        check("释放：拒绝关闭时绝不能调 Close()", d_dirty.closed == 0,
              f"Close 调用了 {d_dirty.closed} 次")

        # 显式 allow_dirty：调用方已知情并承担
        ok, why = C.release_document(target, allow_dirty=True)
        check("释放：allow_dirty=True 时才允许关掉脏文档",
              ok is True and d_dirty.closed == 1, f"得到 {(ok, why)}")
    finally:
        C.attach_coreldraw = real_attach


# ---------------------------------------------------------------------------
# F. 字形级纠错与字体相似度（纯逻辑 + 合成图）
# ---------------------------------------------------------------------------


def test_as_mask():
    """`_col_runs` / `_glyph_h` 必须同时吃布尔掩膜和灰度裁切。

    真踩过：这两个函数是按布尔掩膜写的，`ndarray.any(axis=0)` 对布尔数组是
    "这一列有没有墨"，对 uint8 灰度数组却是"这一列有没有**非零值**"——
    背景灰度约 240 全部非零，于是整个词被判成**一个**列段。
    不报错、不抛异常，只是把"逐字切分"静默退化成"整词一块"，
    后续所有字高判据一起失效（实测 `O.v.D.` 的 5 个列段被读成 1 个、
    高 16 = 整幅裁切高度）。
    """
    gray = np.full((16, 30), 240, np.uint8)
    gray[4:16, 0:6] = 0
    gray[4:16, 10:16] = 0
    ink = gray < 128

    check("布尔掩膜切出 2 个列段", L._col_runs(ink) == [(0, 6), (10, 16)],
          str(L._col_runs(ink)))
    check("**灰度裁切**也切出 2 个列段（归一化生效）",
          L._col_runs(gray) == [(0, 6), (10, 16)], str(L._col_runs(gray)))
    check("灰度裁切下字高正确（12，不是整幅的 16）",
          L._glyph_h(gray, 0, 6) == 12, str(L._glyph_h(gray, 0, 6)))
    check("_as_mask 对布尔输入不复制、原样返回",
          L._as_mask(ink) is ink)
    check("_as_mask 对灰度输入按管线口径 <128 取墨",
          L._as_mask(gray)[0, 0] is np.False_ or L._as_mask(gray)[0, 0] == False)


def test_word_aligned_iou():
    """字体相似度必须**逐词对齐**，不能被整行拉伸的相位主导。

    真踩过：整行 IoU 把渲染结果横向拉伸到源宽度，字形位置误差是累积的；
    小字号下笔画只有 1~2px 宽，于是这个分基本由"拉伸相位"决定。
    实测同一个字体只改一个字母大小写：
        'O.v.D. …' 整行 0.7244（错的文本反而高）  'O.V.D. …' 整行 0.5090
    直接导致最佳字体从 `Swis721 Cn BT/Bold` 被选成 `Swis721 BlkCn BT/Black`。
    """
    H, BW = 12, 3          # 画布高 / 竖条宽
    WSTARTS = (0, 30, 60)  # 三个词的起点（词宽 19、词内间隙 5、词间间隙 11）
    IN_GAP = 5             # 词内竖条间距

    def line(word_shifts):
        """每个词整体平移 word_shifts[i] 像素后画出来。"""
        m = np.zeros((H, 90), bool)
        for ws, sh in zip(WSTARTS, word_shifts):
            for k in range(3):
                x = ws + sh + k * (BW + IN_GAP)
                m[2:H, x:x + BW] = True
        return m

    src = line((0, 0, 0))
    # 累积漂移：三个词分别右移 0 / 2 / 4 像素（模拟拉伸造成的行内错位）
    rend = line((0, 2, 4))

    W = max(src.shape[1], rend.shape[1])
    a = np.zeros((H, W), bool); a[:, :src.shape[1]] = src
    b = np.zeros((H, W), bool); b[:, :rend.shape[1]] = rend
    line_iou = L._iou(a, b)
    al = L._word_aligned_iou(src, rend)

    check("源切出 3 个词", len(L.gap_split(src)[0]) == 3,
          str(L.gap_split(src)[0]))
    check("渲染切出 3 个词", len(L.gap_split(rend)[0]) == 3,
          str(L.gap_split(rend)[0]))
    check("逐词对齐 IoU = 1.0（词内完全一致，只是整体平移）",
          al is not None and near(al, 1.0, 0.02), str(al))
    check("同一对图整行 IoU 明显更低（证明漂移确实在主导整行口径）",
          line_iou < al - 0.10, '整行 %.4f vs 对齐 %.4f' % (line_iou, al))

    # 词数切不齐 → 必须返回 None 让调用方回退，而不是硬算一个假分。
    # （不能拿"某个词的列区间"当单词语料：`gap_split` 的 Otsu 在间隙全相等时
    #   阈值会退化，切出来的仍是 3 个词——这个坑也踩过。）
    one = np.zeros((H, 10), bool)
    one[2:H, :] = True
    check("单块掩膜确实只切出 1 个词", len(L.gap_split(one)[0]) == 1,
          str(L.gap_split(one)[0]))
    check("词数切不齐时返回 None（调用方回退整行 IoU）",
          L._word_aligned_iou(one, src) is None)
    check("任一侧全空也不崩", L._word_aligned_iou(np.zeros((H, 10), bool), src)
          is None)


def test_case_by_height():
    """行内字高判大小写——真踩过的 `O.v.D.` → `O.V.D.`。

    源图页脚是 `O.V.D. Importadora …`，OCR 交出来 `O.v.D. …`，而且
    **6 个二值化变体全读成小写 v**（二值化削掉了 V 顶端的细笔画），
    只有 2 个灰度变体读对。所以：
      * 多数投票救不了（6 : 2，多数是错的）；
      * 置信度救不了（错的对的都在 0.906~0.921）；
      * 逐词 IoU 也救不了——两种口径给出**相反**结论：
        按高度归一、各自紧裁、铺到较宽画布比 → `O.V.D.` 0.7476 > `O.v.D.` 0.7103；
        逐词拉伸到源包围盒比 → `O.V.D.` 0.6912 < `O.v.D.` 0.7910。
        38×13 px 的词里，单字形的大小写差异已在 IoU 类指标的噪声底之下。
    唯一稳的是**直接量那个字的高度**：x-height 9px、cap-height 12~13px。
    """
    XH, CAP, H = 9, 13, 20
    MARK = '\u00b7'          # 句点类小碎块，高 2px

    def build(spec):
        """spec: [('字', 高, 宽) | None]，None 表示词间空隙。
        返回 (gray, words)：gray 是**灰度**图（背景 240、墨迹 0），
        words 是每个词的 (x0, x1) 列区间。用灰度而不是布尔，是为了
        顺带把 `_as_mask` 那条路径也测进去。
        """
        boxes, words = [], []
        x, start = 0, None
        for g in spec:
            if g is None:
                if start is not None:
                    words.append((start, x - 1))
                    start = None
                x += 8
                continue
            if start is None:
                start = x
            _, h, w = g
            boxes.append((x, x + w, h))
            x += w + 1
        if start is not None:
            words.append((start, x - 1))
        gray = np.full((H, x), 240, np.uint8)
        for x0, x1, h in boxes:
            gray[H - h:, x0:x1] = 0
        return gray, words

    # 词 1 = 'O.v.D.'（6 个字符）：源图上 V 与 D 之间那个句点墨迹为零，
    # 所以只有 5 个列段。这正是"列段数 == 字符数"那道守卫会误杀的情形——
    # 必须按**字母数**对齐。
    TAIL = [None, ('m', XH, 5), ('e', XH, 5), ('a', XH, 5)]   # 立 x-height 锚点

    gray, words = build([('O', CAP, 5), (MARK, 2, 2), ('v', CAP - 1, 5),
                         ('D', CAP - 1, 5), (MARK, 2, 2)] + TAIL)
    w0 = words[0]
    runs_bool = L._col_runs((gray < 128)[:, w0[0]:w0[1]])
    runs_gray = L._col_runs(gray[:, w0[0]:w0[1]])
    check("灰度裁切与布尔掩膜切出同样的列段（_as_mask 归一化生效）",
          runs_bool == runs_gray, '%s vs %s' % (runs_bool, runs_gray))
    check("词 1 只有 5 个列段，而它有 6 个字符（不能按字符数对齐）",
          len(runs_gray) == 5, str(runs_gray))

    props, notes = L.case_by_height(gray, words, ['O.v.D.', 'mea'])
    check("大写 V 被按字高纠正", props == [(0, 'O.V.D.', 'case-height')],
          str(props))
    check("理由带上实测字高，便于人工复核",
          bool(notes) and 'v->V' in notes[0] and 'x9/cap12' in notes[0],
          str(notes))

    props, _ = L.case_by_height(gray, words, ['O.V.D.', 'mea'])
    check("已经是大写时**不提建议**（幂等、不制造无谓改动）",
          props == [], str(props))

    # 反方向也必须成立：x 高度的 'V' 该被判成小写 v。
    # 若判据是"总是改成大写"而不是"比高度"，这条会挂。
    gray2, words2 = build([('O', CAP, 5), (MARK, 2, 2), ('V', XH, 5),
                           ('D', CAP - 1, 5), (MARK, 2, 2)] + TAIL)
    props, _ = L.case_by_height(gray2, words2, ['O.V.D.', 'mea'])
    check("x 高度的 V 反向判成小写 v（说明判据是高度，不是倾向）",
          props == [(0, 'O.v.D.', 'case-height')], str(props))

    # --- 安全约束：宁可漏判，不可错判 ---
    gray3, words3 = build([('O', XH, 5), ('v', XH + 1, 5), ('D', XH, 5)] + TAIL)
    props, _ = L.case_by_height(gray3, words3, ['OvD', 'mea'])
    check("x-height 与 cap 只差 1px 时整体放弃（不拿噪声当证据）",
          props == [], str(props))

    g4, w4 = build([('O', CAP, 5), ('v', CAP - 1, 5)])
    props, _ = L.case_by_height(g4, w4, ['Ov'])
    check("锚点不足（没有 x-height 样本）时不动", props == [], str(props))

    props, _ = L.case_by_height(gray, words, ['O.v.D.'])
    check("词数与词列表不一致时放弃（2 个词区间 vs 1 个 token）",
          props == [], str(props))

    # 真踩过：调用方把整串文本当成词列表传进来。`len('O.v.D. mea')`=10 对上
    # `len(words)`=2 的守卫 → 静默返回空建议，整条修复等于没接线。
    # 必须**报错**，不能"顺手 split 一下"把调用方的错掩盖掉。
    raised = False
    try:
        L.case_by_height(gray, words, 'O.v.D. mea')
    except TypeError:
        raised = True
    check("第三个参数传字符串时抛 TypeError（不再静默返回空建议）", raised)


def test_parse_region_fields():
    """区域声明的字段解析：坐标是**位置固定**的，不是靠"像不像色值"猜。

    真踩过：`K_C1=635,654,43,129` 里的 `635` 是 3 位十六进制合法长度，
    被 `len(p) in (3, 6)` 那条"像色值"的启发式判成了颜色，于是 4 个坐标
    只剩 3 个 → 整条区域声明报错。坐标必须在前面按位置吃，可选字段才按形态认。
    """
    name, box, opt = L.parse_region('K_C1=635,654,43,129')
    check("3 位数字坐标不被误判成色值", box == (635, 654, 43, 129), str(box))
    check("无可选字段时 opt 为空", opt == {}, str(opt))

    _, box, opt = L.parse_region('K_A5=270,288,326,412,#1A1819,rot=180')
    check("坐标 + 颜色 + 旋转都解析出来",
          box == (270, 288, 326, 412) and opt.get('rot') == 180
          and opt.get('color') == '#1A1819', f"{box} {opt}")

    _, _, opt = L.parse_region('X=1,2,3,4,#abc')
    check("3 位简写色值在**可选字段位置**能识别", opt.get('color') == '#abc',
          str(opt))

    for bad in ('W=1,2,3', 'V=1,2,3,4,zzz'):
        try:
            L.parse_region(bad)
            check(f"非法区域声明 {bad!r} 必须报错", False, "竟然通过了")
        except ValueError:
            check(f"非法区域声明 {bad!r} 报 ValueError", True)


def _aa_square(a, r0, r1, c0, c1, ink, rings=((2, 0.25), (1, 0.5))):
    """在画布 `a` 上画一个**带抗锯齿斜坡**的实心方块。

    抗锯齿像素 = 背景与墨色的线性混合，混合系数就是覆盖率——真实位图的
    边缘正是这个样子，`detect_palette` / `color_space` 的全部设计都针对它。
    斜坡由外向内写，保证内圈覆盖外圈。
    """
    bg = np.array([255.0, 255.0, 255.0], np.float32)
    ink = np.asarray(ink, np.float32)
    a[r0:r1, c0:c1] = ink.astype(np.uint8)
    for off, cov in sorted(rings, reverse=True):
        for y in range(r0 - off, r1 + off):
            for x in range(c0 - off, c1 + off):
                if r0 <= y < r1 and c0 <= x < c1:
                    continue
                if 0 <= y < a.shape[0] and 0 <= x < a.shape[1]:
                    a[y, x] = (bg + cov * (ink - bg)).astype(np.uint8)
    return a


def test_detect_palette():
    """调色板提取必须**穿过抗锯齿斜坡**找到墨色本身。

    两个实测踩过的坑，各对应一条断言：

    * 按**颜色半径**聚类 → 同一个洋红被拆成
      `#C62F7C / #B63B7A / #BD6A94 / #CF6EA4 / #DF89B6` 五个假色，
      连背景都被误报成墨色。本图实测自动提取出 5 色。
    * 代表色取**簇内最远的像素** → 被重采样过冲带偏：洋红报成 `#BF2B75`、
      黑报成 `#161415`（真值 `#C62F7C` / `#1A1819`），CDR 填色跟着错。
    """
    MAG, BLK = (198, 47, 124), (26, 24, 25)
    a = np.full((60, 120, 3), 255, np.uint8)
    _aa_square(a, 6, 25, 11, 50, MAG)
    _aa_square(a, 35, 54, 11, 50, BLK)

    pal, bg = S.detect_palette(a)
    check("恰好提取出 2 个墨色（斜坡没被拆成多个假色）", len(pal) == 2, str(pal))
    check("背景取到精确色 #FFFFFF", tuple(bg) == (255, 255, 255), str(bg))

    for want in (MAG, BLK):
        hit = min(pal, key=lambda c: max(abs(c[i] - want[i]) for i in range(3)),
                  default=None)
        d = max(abs(hit[i] - want[i]) for i in range(3)) if hit else 999
        check(f"墨色 #{want[0]:02X}{want[1]:02X}{want[2]:02X} 分量误差 ≤ 2",
              d <= 2, f"实测 {hit}，最大分量差 {d}")


def test_color_space_direction():
    """按**相对背景的方向**分色：黑的抗锯齿灰边不能被算成洋红。

    欧氏最近色在这里是错的：`#8C8C8C` 到洋红 `#C62F7C` 的距离比到黑
    `#1A1819` **更近**（110.8 < 199.2），于是黑字/黑图标的整圈灰边被判给
    洋红，每个图标位置都凭空多出一份"假洋红"块。
    """
    MAG, BLK = (198, 47, 124), (26, 24, 25)
    a = np.full((60, 120, 3), 255, np.uint8)
    _aa_square(a, 6, 25, 11, 50, MAG)
    _aa_square(a, 35, 54, 11, 50, BLK)

    pal, bg = S.detect_palette(a)
    if len(pal) != 2:
        check("分色测试需要先提取出 2 色", False, str(pal))
        return

    def near(c):
        return min(range(len(pal)),
                   key=lambda i: max(abs(pal[i][k] - c[k]) for k in range(3)))

    mi, bi = near(MAG), near(BLK)
    label, _ = S.color_space(a, pal, bg)

    # 含抗锯齿边的整片区域（外扩到方块之外 2px）
    blk_area = label[33:56, 9:52]
    mag_area = label[4:27, 9:52]
    n_bad = int((blk_area == mi).sum())
    check("黑块（含灰边）里没有一个像素被判成洋红", n_bad == 0,
          f"误判 {n_bad} px")
    n_bad2 = int((mag_area == bi).sum())
    check("洋红块（含灰边）里没有一个像素被判成黑", n_bad2 == 0,
          f"误判 {n_bad2} px")

    # 把"为什么不能用欧氏最近色"钉住：这条对照断言让测试本身有意义
    gray = np.array([140, 140, 140], np.float32)
    d_mag = float(np.linalg.norm(gray - np.asarray(MAG, np.float32)))
    d_blk = float(np.linalg.norm(gray - np.asarray(BLK, np.float32)))
    check("（对照）欧氏最近色会把黑字的灰边判给洋红",
          d_mag < d_blk, f"到洋红 {d_mag:.1f} < 到黑 {d_blk:.1f}")


def test_blocks_tight_bbox():
    """块的包围盒与掩膜必须**同源**（都取紧框），否则填墨率的分母分子对不上。

    真踩过：包围盒取紧框、掩膜取膨胀后的整块矩形，于是一圈细边框的
    填墨率被算成 **0.928**（真实 0.074），块分类跟着全错。
    """
    m = np.zeros((60, 100), bool)
    m[10, 10:90] = True
    m[49, 10:90] = True
    m[10:50, 10] = True
    m[10:50, 89] = True

    blks = S._blocks(m, 1)
    check("细边框聚成 1 块", len(blks) == 1, f"{len(blks)} 块")
    if len(blks) != 1:
        return
    b = blks[0]
    check("包围盒是紧框（不是膨胀后的框）", b['box'] == [10, 10, 90, 50],
          str(b['box']))
    check("掩膜尺寸与包围盒一致", b['mask'].shape == (40, 80),
          str(b['mask'].shape))

    f = S.block_features(b)
    check("细边框填墨率 < 0.15", f['fill'] < 0.15, f"fill={f['fill']}")
    check("细边框判为线稿", S.classify_block(f) == 'line_art',
          S.classify_block(f))

    # 实心块走另一支：填墨率高 + 面积够 → solid
    solid = np.zeros((60, 100), bool)
    solid[10:50, 10:90] = True
    fs = S.block_features(S._blocks(solid, 1)[0])
    check("实心块判为 solid", S.classify_block(fs) == 'solid',
          f"fill={fs['fill']} kind={S.classify_block(fs)}")
    check("实心块的 thick_px 是内径（远大于笔画宽）", fs['thick_px'] > 20,
          f"thick_px={fs['thick_px']}")


def test_orientation_votes():
    """方向判定必须靠**字形证据**，不能只靠 OCR。

    真踩过：`www.daiion.com` 与 `daiion` 正置倒置在 0° 那遍**都**读得出来，
    只有 2 个字符的 `4#` 在 0° 那遍整块漏检。于是"只在 180° 出现才算倒置"
    这条规则只对短文本有效，本图 4 处长文本被全部误标成正置。

    这里用"渲染一份、再翻转比对"的自洽测试：正置掩膜应判 0°，
    翻转后的同一掩膜应判 180°。掩膜按 px=64 渲染、函数内部按
    RENDER_PX=160 渲染再缩回来，所以 IoU 不是 1.0，是真在比形状。
    """
    fonts = S.orient_fonts(3)
    if not fonts:
        check("方向判定需要至少一款参照字体", False, "本机没找到")
        return

    m = L.render_mask('daiion', fonts[0], px=64)
    if m is None:
        check("参照字体可渲染", False, fonts[0])
        return

    i0, i180, deg = S.orientation_votes(m, 'daiion', fonts)
    check("正置掩膜判为 0°", deg == 0, f"iou0={i0} iou180={i180} deg={deg}")
    check("正置时 iou0 高于 iou180", i0 > i180, f"{i0} vs {i180}")

    f0, f180, fdeg = S.orientation_votes(m[::-1, ::-1], 'daiion', fonts)
    check("翻转后的掩膜判为 180°", fdeg == 180,
          f"iou0={f0} iou180={f180} deg={fdeg}")
    check("翻转后 iou180 高于 iou0", f180 > f0, f"{f180} vs {f0}")

    # 方向证据必须**显著**才改判：左右对称性强的短串差距小，不能硬分
    check("方向判定的差距明显（不是勉强分的）", min(abs(i0 - i180),
          abs(f180 - f0)) > 0.05, f"{abs(i0 - i180):.4f}")


def test_snap_box_to_ink():
    """OCR 的框只包住字形芯部，必须扩到**与之相连**的墨迹边界。

    真踩过（两处，都不报错）：

    * 倒置的 `daiion` 字标：两个 `i` 点朝下落在 y529..535，OCR 框止于
      y531 —— 点被切掉一半，成品里活字缺字 / 描摹区漏元素；
    * vonder 页脚那个几乎看不见的句点：3 个杂散像素被并进相邻字的列段，
      把"字宽"从 8px 撑成 10px，直接毁掉大小写判据。

    而**不能**按"框外有墨迹就往外长"：实测 `www.daiion.com` 的框离下面的
    框线只有 5px，那样会把整条框线并进来。所以判据是"连通分量是否与框相交"。
    """
    if S.cv2 is None:
        check("框吸附需要 opencv", False, "本机没有 cv2")
        return

    H, W = 60, 80
    # 字形 1：竖杆 + 相连的横杆（同一个连通分量）
    m = np.zeros((H, W), bool)
    m[20:50, 30:34] = True          # 竖杆
    m[16:20, 20:44] = True          # 横杆，与竖杆相接
    # 字形 2：一个**分离**的小点（另一个连通分量）
    m[16:22, 60:66] = True

    # (a) 框只盖住竖杆下段，但横杆同属一个连通分量 -> 必须长上去
    box = [30, 24, 34, 50]
    got = S.snap_box_to_ink(m, box, max_grow=20)
    check("相连的笔画被吸附进来（竖杆 -> 含横杆）",
          got[0] <= 20 and got[1] <= 16,
          f"框 {box} -> {got}（应含 x20..44 / y16）")

    # (b) 分离的点**不**与框相交 -> 不能被吞进来
    check("不相交的分离元素不被吞进来", got[2] <= 44,
          f"x1={got[2]}（应 <= 44，不能把 x60 那个点并进来）")

    # (c) 点与框**部分相交** -> 整颗点都要，不能只取相交那一半
    box2 = [60, 19, 66, 40]         # y19 落在点的 y16..22 里，点被切掉上半
    got2 = S.snap_box_to_ink(m, box2, max_grow=20)
    check("部分相交的点扩成整颗", got2[1] <= 16,
          f"框 {box2} -> {got2}（应上扩到 y16）")

    # (d) 框本来就包全了 -> 不变
    box3 = [20, 16, 44, 50]
    got3 = S.snap_box_to_ink(m, box3, max_grow=20)
    check("框已包全时不变", got3 == box3, f"{box3} -> {got3}")

    # (e) 空掩膜不能崩，也不能把框改坏
    empty = np.zeros((H, W), bool)
    check("空掩膜原样返回", S.snap_box_to_ink(empty, [10, 10, 20, 20]) ==
          [10, 10, 20, 20])

    # (f) max_grow 之外的东西够不到（窗口限制）
    far = np.zeros((H, W), bool)
    far[20:30, 30:34] = True
    far[20:30, 70:74] = True        # 距框 40px
    got4 = S.snap_box_to_ink(far, [30, 20, 34, 30], max_grow=5)
    check("max_grow 之外的不被吸附", got4[2] <= 40,
          f"x1={got4[2]}（应 <= 40）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    print("=== A. 纯逻辑 ===")
    test_render_prompt()
    test_compare_profiles()

    print("\n=== B. 自动分区与度量（合成图，已知真值）===")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_strip_residue()
        print()
        test_tighten()
        print()
        test_full_bleed_guard()
        print()
        test_auto_partition(tmpdir)
        print()
        test_rasterize_supersample()
        print()
        test_corner_segments()
        print()
        test_shim_source(tmpdir)
    print()
    test_pick_thresholds()

    print("\n=== C. 文字转活字的判定（纯逻辑）===")
    test_cc_sizes()
    print()
    test_glyph_stats()
    print()
    test_decide_convert()

    print("\n=== D. OCR 预处理与变体选择（纯逻辑）===")
    test_otsu_gray()
    print()
    test_binarize_polarity()
    print()
    test_pick_variant()

    print("\n=== E. 保存落盘核验与占用释放（纯逻辑）===")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_stat_sig(tmpdir)
        print()
        test_release_document(tmpdir)

    print("\n=== F. 字形级纠错与字体相似度（纯逻辑 + 合成图）===")
    test_as_mask()
    print()
    test_word_aligned_iou()
    print()
    test_case_by_height()
    print()
    test_parse_region_fields()

    print("\n=== G. 整图识别：调色板 / 分色 / 块 / 方向（合成图 + 纯逻辑）===")
    test_detect_palette()
    print()
    test_color_space_direction()
    print()
    test_blocks_tight_bbox()
    print()
    test_orientation_votes()
    print()
    test_snap_box_to_ink()

    print()
    if _FAILED:
        print(f"有 {len(_FAILED)} 项失败：")
        for line in _FAILED:
            print("  -", line)
        return 1
    print("全部测试通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
