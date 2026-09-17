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
import cdr_image_trace as T                               # noqa: E402
import cdr_bitmap_to_cdr as B                             # noqa: E402


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
        test_shim_source(tmpdir)
    print()
    test_pick_thresholds()

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
