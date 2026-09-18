#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把位图里的文字转成 CorelDRAW 活字：识别 → 字体匹配 → 重建 → 校验。

为什么要有这一步
----------------
主流程把 6pt 页脚描摹成轮廓，视觉上能看，但**不可编辑**、节点数还多
（实测该区域 73 子路径 / 840 段）。若能把文字识别出来、用最接近的字体
重建为真文本，既省节点又能改字。风险是**字体猜错比描摹失真更糟**，
所以本脚本的核心不是"识别"，而是**每一步都算分，分数不够就拒绝转换**。

流水线
------
  1. 紧裁文字区（OCR 检测器对大片留白会直接失效，必须先紧裁）
  2. 上采样 2–3 倍后 OCR → 文本串 + 逐行框
  3. 用**列投影间隙**找回词边界（OCR 对窄字距常把空格吃掉）
  4. 字形级纠错：按字符高度定大小写、按墨量定 ·/• 之类易混符号
  5. 字体匹配：把系统字体逐个渲染同串文本，按高度归一后比 IoU
  6. 解字号（决定高度）与横向缩放（决定宽度），两者分开解
  7. 判定，**四条判据缺一不可**（`decide_convert`），见下：
       硬门槛 A：连通分量数 ÷ 字符数 —— 这块到底是不是一行文字
       硬门槛 B：最佳候选的自然宽度比 —— 有没有字体的字宽与它相符
       相似度 C：重建 IoU 绝对值（适合大字号）
       相似度 D：领先候选集中位的倍数（与字号无关，适合小字）
     C 或 D 过即可，但 A 与 B 必须都过；否则保留描摹轮廓，不转
  8. --apply 时才真的在 CorelDRAW 里建文本；再给 --replace-traced
     就把该区域的描摹轮廓删掉（**必须删**，否则与活字叠加等于把字加粗一遍）

产出
----
  <out>/ocr.json          识别文本、逐行框、置信度
  <out>/font_match.json   候选字体与分数（含"库里没有接近的"时的前 N 名）
  <out>/live_text.json    最终决定：字体、字号、缩放、位置、校验分、是否降级
  <out>/compare_<name>.png 源 / 活字渲染 并排对照

依赖
----
  numpy, pillow, opencv-python-headless
  rapidocr-onnxruntime + onnxruntime  （缺失时脚本会明确报出来，
  并降级为"只做字体匹配"，不做 OCR）
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FONT_DIRS = [r'C:\Windows\Fonts']
RENDER_PX = 160
# 转换门槛：候选字体渲染回位图后，与源文字的 IoU 低于它就**不转活字**
DEFAULT_MIN_IOU = 0.62


# --------------------------------------------------------------------------
# 位图侧：紧裁、列投影、间隙切词
# --------------------------------------------------------------------------

def load_gray(path):
    return np.asarray(Image.open(path).convert('L'))


def strip_full_height_cols(ink):
    """剥掉贯穿全高的列（扫描残留边），否则包围盒会被撑满整幅。"""
    h = ink.shape[0]
    keep = ink.sum(axis=0) < h * 0.9
    return ink[:, keep] if keep.any() else ink


def tight_text_box(ink):
    """去掉四周留白，返回 (y0, y1, x0, x1)。"""
    ys = np.where(ink.sum(axis=1) > 0)[0]
    xs = np.where(ink.sum(axis=0) > 0)[0]
    if not len(ys) or not len(xs):
        return None
    return int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1


def gap_split(mask):
    """按列投影的**间隙宽度**切词。

    做法：先找连续的墨迹列段（glyph run），再取相邻段之间的空隙宽度，
    对这批空隙做一维 Otsu —— 空隙会自然分成"字内"与"词间"两簇，
    阈值取在两簇之间。比"固定阈值"稳，比"按 OCR 的空格数"可靠
    （OCR 对窄字距常把空格吃掉，空格数本身就不可信）。

    返回 (words, gaps)：words 是每个词的 (x0, x1) 列区间。
    """
    col = mask.sum(axis=0) > 0
    runs = []
    i = 0
    n = len(col)
    while i < n:
        if col[i]:
            j = i
            while j < n and col[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    if len(runs) <= 1:
        return [((runs[0][0], runs[0][1]) if runs else (0, mask.shape[1]))], []
    gaps = [runs[k + 1][0] - runs[k][1] for k in range(len(runs) - 1)]
    thr = _otsu_1d(np.array(gaps, float))
    words = []
    start = runs[0][0]
    for k, g in enumerate(gaps):
        if g > thr:
            words.append((start, runs[k][1]))
            start = runs[k + 1][0]
    words.append((start, runs[-1][1]))
    return words, gaps


def _otsu_1d(vals):
    """一维 Otsu 阈值。样本太少时退回"最大值的一半"。"""
    if len(vals) < 3:
        return float(vals.max()) * 0.5 if len(vals) else 0.0
    lo, hi = float(vals.min()), float(vals.max())
    if hi - lo < 1e-9:
        return lo
    best_t, best_v = lo, -1.0
    for t in np.linspace(lo, hi, 64):
        a, b = vals[vals <= t], vals[vals > t]
        if not len(a) or not len(b):
            continue
        w = len(a) / len(vals)
        v = w * (1 - w) * (a.mean() - b.mean()) ** 2
        if v > best_v:
            best_v, best_t = v, float(t)
    return best_t


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------

# OCR 的预处理变体 (标签, 缩放, 横向留白, 纵向留白, 二值化阈值)。
#
# **为什么不是一套参数**：检测器对纵向留白与长宽比极敏感，且不同图的最优点
# 不同——实测同一张页脚图，`scale=2 + 20px 四周留白`出乱码、
# `scale=2 + 纵向 0 留白`出正确答案。所以固定一套参数不可靠，要跑多套再选。
#
# **二值化必须是独立的一维，而且是决定性的那一维**。实测同一区域、同一裁切框
# （源图直裁 371x16，灰度 min 8 / max 255，是个低分辨率扫描件）：

#     模式          变体数   完全正确
#     原始灰度        4        0
#     二值化         12        7
#
# 原始灰度**全错**，且错法一致：`O.V.D.`（大小写错）＋词间空格被吃掉
# （9 个词读成 7 个）。原因是 6pt 的字在这个分辨率下笔画边缘全是抗锯齿，
# 检测器分不清字与字之间的浅灰缝隙；二值化把笔画还原成锐利边缘后就分得清。
# 阈值取 **118/128/Otsu** 都能出正确答案，所以三档都放进变体里。
#
# **⚠️ 置信度不能用来选**：上面那张表里，错误的灰度变体置信度最高到 0.921，
# 而正确的二值变体只有 0.849~0.918。按置信度选会**系统性地选错**。
# 所以 `ocr_lines` 改成用**列投影切出的词数**当第一判据——那是独立于 OCR 的
# 几何证据（实测：词数 == 9 的变体全部正确，≠9 的全部错误，这条判据
# 在该表上 100% 分对）。
OCR_VARIANTS = [
    ('1x_宽留白', 1, 400, 0, None),
    ('2x_无纵留白', 2, 20, 0, None),
    ('2x_微纵留白', 2, 20, 8, None),
    ('3x_20留白', 3, 20, 20, None),
    ('4x_20留白', 4, 20, 20, None),
    ('2x_二值118', 2, 20, 8, 118),
    ('4x_二值118', 4, 20, 20, 118),
    ('2x_二值128', 2, 20, 8, 128),
    ('4x_二值128', 4, 20, 20, 128),
    ('4x_二值Otsu', 4, 20, 20, 'otsu'),
    ('1x_二值118_宽留白', 1, 400, 0, 118),
]


def _otsu_gray(arr):
    """灰度图的 1D Otsu 阈值（按直方图，256 档全扫）。

    **⚠️ 平台期必须取中点，不能取第一个最大值**。类间方差在"两个峰之间"
    是一段**完全相等的平台**：一个只有 40 与 220 两个灰度的图，t 取 40..219
    算出的方差一模一样。若用严格大于保留第一个，就会返回 40——阈值落在
    暗峰的**边缘**上，`a < 40` 一个像素都不剩，二值图全白，OCR 直接失效。
    实测踩过：合成双峰图返回 40、二值后墨迹占比 0.00。
    这与"参数有平台期时不能只比两个点"是同一类错误。
    """
    hist = np.bincount(arr.ravel(), minlength=256).astype(float)
    total = hist.sum()
    if total <= 0:
        return 128
    s_total = float((hist * np.arange(256, dtype=float)).sum())
    best_v, lo, hi = -1.0, 128, 128
    w0 = 0.0
    s0 = 0.0
    for t in range(256):
        w0 += hist[t]
        s0 += hist[t] * t
        w1 = total - w0
        if w0 <= 0 or w1 <= 0:
            continue
        v = w0 * w1 * (s0 / w0 - (s_total - s0) / w1) ** 2
        if v > best_v * (1 + 1e-12) + 1e-12:
            best_v, lo, hi = v, t, t
        elif v >= best_v * (1 - 1e-12) - 1e-12:
            hi = t
    return (lo + hi) // 2


def binarize(im, thr):
    """按阈值二值化（黑字白底）。thr 为 'otsu' 时按图自算。

    返回灰度 PIL（0/255）而不是 mode '1'：后面 `_ocr_once` 要转 RGB，
    保持灰度能让所有变体走同一条路径。

    **极性保护**：本函数假定"深色字 + 浅色底"。若二值后墨迹占比超过一半，
    说明这块是**反白字**（深底浅字），直接反相——否则整块会变成一团黑，
    OCR 只会吐垃圾。反白字本来就不适合转活字（见文档），但也不该崩出乱码。
    """
    a = np.asarray(im.convert('L'))
    t = _otsu_gray(a) if thr == 'otsu' else int(thr)
    b = a < t
    if b.mean() > 0.5:
        b = ~b
    return Image.fromarray(np.where(b, 0, 255).astype(np.uint8))


def _ocr_once(engine, base, scale, pad_x, pad_y, binz=None):
    """跑一套预处理。`binz` 是二值化阈值（None=不二值化，'otsu'=自算）。"""
    im = binarize(base, binz) if binz is not None else base
    if scale != 1:
        im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    bg = Image.new('RGB', (im.width + pad_x * 2, im.height + pad_y * 2), 'white')
    bg.paste(im.convert('RGB'), (pad_x, pad_y))
    res, _ = engine(np.asarray(bg))
    out = []
    for item in (res or []):
        out.append((str(item[1]), float(item[2]), item[0]))
    return out


def pick_variant(ok):
    """从成功识别出文本的变体里挑一个。就地写回 `agreement` 字段。

    **选择顺序**（这是关键，别改回"按置信度选"）：
      1. `word_gap` 升序 —— `|OCR 词数 − 列投影词数|`。列投影是**独立于
         OCR 的几何证据**；实测它在该区 100% 分对（9 词的全对、≠9 词的全错），
         而置信度会把错误的变体排在正确的前面（错误 0.921 > 正确 0.918）。
      2. 置信度降序 —— 仅在 word_gap 相同时打破平局。
      3. 字符数与其它变体的一致性 —— 沿用"至少一个变体同字符数"的校验，
         但**只在 word_gap > 0（几何证据没认可）时才用它翻盘**；
         word_gap == 0 说明几何已经认可，不该被"多数变体"推翻。
    """
    ok.sort(key=lambda r: (r['word_gap'], -r['score']))
    pick = ok[0]
    agree = [r for r in ok[1:] if r['chars'] == pick['chars']]
    pick['agreement'] = len(agree)
    if not agree and len(ok) > 1 and pick['word_gap'] > 0:
        from collections import Counter
        cnt = Counter(r['chars'] for r in ok)
        target = cnt.most_common(1)[0][0]
        alt = [r for r in ok if r['chars'] == target]
        alt.sort(key=lambda r: (r['word_gap'], -r['score']))
        pick = alt[0]
        pick['agreement'] = len(alt) - 1
    return pick


def ocr_lines(mask_img, variants=None, expect_words=None):
    """多套预处理跑 OCR 再选。返回 (lines, variants_report, err)。

    选择规则见 `pick_variant`。`expect_words` 给 None 时 word_gap 恒为 0，
    退化为纯置信度选择（老行为）。
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:
        return None, [], ('缺少 OCR 引擎：%s。请执行\n'
                          '  pip install --no-deps rapidocr-onnxruntime\n'
                          '  pip install onnxruntime pyclipper shapely six '
                          'flatbuffers protobuf pyyaml\n'
                          '（Windows 上 onnxruntime 还需 msvcp140_1.dll 与 '
                          'vcruntime140_1.dll；后者 Python 安装目录里自带）' % e)
    engine = RapidOCR()
    base = mask_img.convert('L')
    vs = variants or OCR_VARIANTS
    report = []
    for tag, sc, px, py, binz in vs:
        try:
            got = _ocr_once(engine, base, sc, px, py, binz)
        except Exception as e:
            report.append({'variant': tag, 'error': str(e)})
            continue
        txt = ' '.join(t for t, _, _ in got)
        ns = ''.join(c for c in txt if c != ' ')
        score = max((s for _, s, _ in got), default=0.0)
        nw = len([w for w in txt.split(' ') if w])
        report.append({'variant': tag, 'text': txt, 'chars': len(ns),
                       'words': nw, 'score': round(score, 4),
                       'lines': len(got),
                       'binarize': binz,
                       # 词数与列投影的距离：越小越可信。给 None 时置 0，
                       # 让纯置信度模式的行为不变。
                       'word_gap': (abs(nw - expect_words)
                                    if expect_words is not None else 0)})
    ok = [r for r in report if r.get('text')]
    if not ok:
        return [], report, None
    pick = pick_variant(ok)
    best_tag = pick['variant']
    for tag, sc, px, py, binz in vs:
        if tag != best_tag:
            continue
        got = _ocr_once(engine, base, sc, px, py, binz)
        return got, report, None
    return [], report, None


# --------------------------------------------------------------------------
# 字形级纠错
# --------------------------------------------------------------------------

def classify_symbol(sub, xheight):
    """单字符词：**直接量字形**判断它到底是 ·/•/- 里的哪一个。

    不靠 OCR、也不靠字体渲染——这三者的几何差得非常开，实测页脚那条：
        '•'  宽 5  高 4  宽高比 1.25  墨密度 0.800
        '-'  宽 4  高 1  宽高比 4.00  墨密度 1.000
    判据只用三个量：宽高比、墨密度、高度与 x-height 的比。
    与字体无关，所以在字体还没定准的时候也能用。

    返回符号或 None（None 表示"不像这几个易混符号"，保持原样）。
    """
    ys = np.where(sub.sum(axis=1) > 0)[0]
    xs = np.where(sub.sum(axis=0) > 0)[0]
    if not len(ys) or not len(xs):
        return None
    h = int(ys.max() - ys.min() + 1)
    w = int(xs.max() - xs.min() + 1)
    if h <= 0 or w <= 0:
        return None
    aspect = w / h
    dens = float(sub.sum()) / (h * w)
    if h <= 2 and aspect >= 2.5:
        return '-'                       # 连字符：极扁极宽
    if h <= max(3.0, xheight * 0.28) and aspect < 2.0:
        return '\u00b7'                  # 中点：小且不太圆
    if 0.8 <= aspect <= 2.0 and dens >= 0.60:
        return '\u2022'                  # 圆点：近方、致密
    return None


# 大小写**只有高度一个区别**的字母（上下同形）。
# 只有这些字母才适合用字高判大小写：形状本来就不同的（a/A、e/E、g/G…）
# OCR 基本不会错，而字高也不是它们的判据。
HEIGHT_ONLY_CASE = 'cosvwxz'


def _as_mask(sub):
    """把灰度裁切或布尔掩膜统一成布尔掩膜（True = 墨迹）。

    **为什么必须显式归一化**（真踩过）：`ndarray.any(axis=0)` 对布尔数组是
    "这一列有没有墨"，对 uint8 灰度数组却是"这一列有没有**非零值**"——
    背景灰度约 240，全部非零，于是整词被判成**一个**列段。不报错、不抛异常，
    只是静默地把"逐字切分"退化成"整词一块"，后续字高判据全部失效
    （实测：`O.v.D.` 的 5 个列段被读成 1 个，高 16 = 整幅裁切高度）。
    这里统一按管线口径 `< 128` 取墨迹，与 `propose_fixes` 内的用法一致。
    """
    a = np.asarray(sub)
    return a if a.dtype == bool else (a < 128)


def _col_runs(sub):
    """把一段掩膜按"空列"切成若干列段，每个列段对应一个字形。

    接受布尔掩膜或灰度裁切（见 `_as_mask`）。
    """
    runs, s = [], None
    cols = _as_mask(sub).any(axis=0)
    for i, v in enumerate(cols):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i))
            s = None
    if s is not None:
        runs.append((s, len(cols)))
    return runs


def _glyph_h(sub, a, b):
    """列段 `[a, b)` 内墨迹的竖直跨度（像素）。接受布尔掩膜或灰度裁切。"""
    ys = np.where(_as_mask(sub)[:, a:b].any(axis=1))[0]
    return int(ys.max() - ys.min() + 1) if len(ys) else 0


def case_by_height(core, words, toks, min_gap=1.0, min_anchor_gap=2.0):
    """用**行内字高**判"上下同形"字母的大小写。返回与 `propose_fixes` 同格式的建议。

    **为什么必须补这一条**（真踩过）：源图页脚是 `O.V.D. ...`，OCR 交出来的是
    `O.v.D. ...`，而且——**所有 6 个二值化变体都读成小写 v**，只有 2 个灰度变体
    读对。所以：
      * **多数投票救不了**（6 : 2，多数是错的）；
      * **置信度救不了**（错的对的都在 0.906~0.921）；
      * `propose_fixes` 现有的打分**还会反向选**：它按"左对齐 + 自然宽度"比，
        而大写 V 更宽、整词右移错位，`O.V.D.` 只拿到 0.1432，反而低于
        `O.v.D.` 的 0.2280 —— 这个分被**累积宽度漂移**主导，不是被那个字的形状主导。
    根因是二值化削掉了 V 顶端的细笔画，V 看起来就像 v。

    **判据只用几何、与字体无关**：在**同一行内**比较
      * `x-height`  ← "无升降部小写字母"（a c e m n o r s u v w x z）高度的中位数
      * `cap-height`← 大写字母高度的中位数
    再把待判字母的高度 `h` 归到更近的一边。实测那条页脚：x-height 9px、
    cap-height 12~13px，待判的 V 高 12px → 离 cap 0.5px、离 x 2.5px，明确是大写。

    **⚠️ 必须是"行内相对"，不能用绝对阈值。** 早先用过一个绝对字高阈值，
    把 `Importadora` 的 m/r/a/e/s 全判成大写（16px 字里 x-height 与 cap 只差
    3px），IoU 从 0.606 掉到 0.36。行内相对比较天然规避这个问题：
    那些字母的高度就等于本行的 x-height，怎么比都是小写。

    **怎么把列段对到字母上**（这一步不做对，后面全是空谈）：
    先按"空列"切出列段，再**滤掉小碎块**——句点、连字符这类记号高度远小于
    主体（实测句点高 2px、连字符 4px，而字母 9~13px），它们不参与大小写判断。
    剩下的列段数若等于词里字母数，就能按顺序一一对上。
    实测那条页脚九个词**全部对得上**：`O.v.D.`→3 段(O,v,D)、`Importadora`→11、
    `Distribuidora`→13、`Curitiba`→8、`PR`→2。
    **注意不能要求"列段数 == 字符数"**：`O.V.D.` 里 V 与 D 之间那个句点在
    二值图上墨迹为 0（太淡），列段只有 5 个而字符有 6 个——按字符数要求会直接
    把这个词跳过，等于白写。**按字母数要求才对。**

    **⚠️ 判据只能用高度，不能用宽度。** 那个几乎看不见的句点会在二值图上
    留下几个**杂散像素**（实测落在 V 右边 x21~23），被并进 V 的列段里，
    于是 V 的"列段宽度"量出来是 10px，而它真正的笔画只有 8px（x13~20）——
    正好等于该字体大写 V 的宽度。宽度会被相邻的弱墨迹污染，高度不会
    （杂散像素只有 1~2 行高，落在基线附近，抬不动 cap-height 那一端的跨度）。

    **安全约束（宁可漏判，不可错判）**：
      * 列段与字母对不上号的词直接跳过；
      * 只处理 `HEIGHT_ONLY_CASE` 里的字母；
      * 两组锚点各至少 2 个样本，且 cap−x 的差至少 `min_anchor_gap`，
        否则说明这一行本身区分度不够，整体放弃；
      * 高度差要超过 `min_gap` 才动，避免把测量噪声当证据；
      * x 锚点**排除** `HEIGHT_ONLY_CASE` 里的字母——它们正是待判对象，
        混进去会自己抬高 x-height、把判据泡软。
    """
    if isinstance(toks, str):
        # **不要**在这里"顺手 split 一下"把调用方的错掩盖掉：整串文本的
        # `len()` 是字符数，恰好会走进下面"词数不一致 → 放弃"的守卫，
        # 于是整条修复静默失效、日志上什么也看不出来。实测就这么漏过一次。
        raise TypeError(
            'toks 必须是词列表，收到字符串（%d 个字符）。传整串文本会让 '
            'len() 变成字符数、命中"词数不一致"守卫并静默返回空建议。'
            % len(toks))
    if len(words) != len(toks):
        return [], []

    # 先切列段并量高度；小碎块的判据用"全行最高列段"的 35%，
    # 这样不必先知道 x-height（避免鸡生蛋）
    raw = []
    for tok, (wx0, wx1) in zip(toks, words):
        sub = core[:, max(wx0, 0):max(wx1, 1)]
        runs = _col_runs(sub)
        hs = [_glyph_h(sub, a, b) for a, b in runs]
        raw.append((sub, runs, hs))
    hmax = max((h for _, _, hs in raw for h in hs), default=0)
    if hmax <= 0:
        return [], []
    mark_cut = 0.35 * hmax

    per_word = []
    x_samples, cap_samples = [], []
    for tok, (sub, runs, hs) in zip(toks, raw):
        letters = [(a, b, h) for (a, b), h in zip(runs, hs) if h > mark_cut]
        n_letter = sum(1 for c in tok if c.isalpha())
        if not letters or len(letters) != n_letter:
            per_word.append(None)
            continue
        per_word.append((letters, [c for c in tok if c.isalpha()]))
        for (a, b, h), ch in zip(letters, [c for c in tok if c.isalpha()]):
            if ch.isupper():
                cap_samples.append(h)
            elif ch.lower() in 'acemnorsuvwxz' and ch.lower() not in HEIGHT_ONLY_CASE:
                x_samples.append(h)

    if len(x_samples) < 2 or len(cap_samples) < 2:
        return [], []
    x_h = float(np.median(x_samples))
    cap_h = float(np.median(cap_samples))
    if cap_h - x_h < min_anchor_gap:
        return [], []          # 这一行 x-height 与 cap 太接近，分不出来，放弃

    proposals, notes = [], []
    for idx, pw in enumerate(per_word):
        if pw is None:
            continue
        letters, alpha = pw
        chars = list(toks[idx])
        # 把字母按顺序对回原 token 的下标
        pos = [i for i, c in enumerate(chars) if c.isalpha()]
        for (a, b, h), ch, ci in zip(letters, alpha, pos):
            low = ch.lower()
            if low not in HEIGHT_ONLY_CASE or h <= 0:
                continue
            d_x = abs(h - x_h)
            d_c = abs(h - cap_h)
            want = None
            if d_c + min_gap < d_x:
                want = ch.upper()
            elif d_x + min_gap < d_c:
                want = ch.lower()
            if want and want != ch:
                new = ''.join(chars[:ci] + [want] + chars[ci + 1:])
                proposals.append((idx, new, 'case-height'))
                notes.append('%s->%s(高%d: x%.0f/cap%.0f)'
                             % (ch, want, h, x_h, cap_h))
    return proposals, notes


def propose_fixes(core, words, text, font_file, margin=0.08):
    """逐词提出修改**建议**（不直接采纳），交给调用方用整体 IoU 逐个裁决。

    为什么只提建议不直接改：逐词渲染比对是有噪声的（字体还没定准的时候
    尤其明显）。实测出现过"同一批修改里 '•' 改对了、但 'Ltda.'/'PR' 被
    错误地改成小写"，整批一起采纳会让总 IoU 从 0.4654 掉到 0.4181。
    全有全无地回退会把改对的那个也一起丢掉。

    所以拆成两步：这里只产出 (词序, 新写法) 的建议清单，调用方每次只试一个，
    总 IoU 变好才留下。这样改对的留下、改错的被否掉，互不牵连。

    两个必须注意的点：
      * **不要先把候选横向拉伸到与源同宽再比**。那样会把字宽差异抹掉，
        于是分不出"字距不对"和"字形不对"。
        （**但这条理由不能推到"分不出大小写"**：实测 `O.V.D.` vs `O.v.D.`
        拉伸到同宽后 IoU 仍有 0.7476 : 0.7103 的可分差距——因为横向拉伸
        不改变**高度**，而大写 V 与小写 v 的区别正在高度上。
        这里旧注释曾写成"就分不出来了"，是错的，已按实测改正。）
      * 单字符词不用渲染比，直接用 classify_symbol 量字形（与字体无关）。

    **⚠️ 本函数的打分有个已知弱点**：它按"左对齐 + 自然宽度"比，
    所以**整词的累积宽度漂移会盖过单个字的形状差异**。实测 `O.v.D.`
    得 0.2280 而正确的 `O.V.D.` 只有 0.1432（大写 V 更宽 → 后面全错位），
    **这个分是反向的**。所以"上下同形字母的大小写"不要指望这里，
    交给 `case_by_height` 用行内字高判——那是几何证据，与字体无关。
    """
    toks = [t for t in text.split(' ') if t]
    if len(toks) != len(words):
        return [], []
    # 先估 x-height：取所有多字符词的高度中位数再打个折，近似主体高度
    hs = []
    for tok, (wx0, wx1) in zip(toks, words):
        if len(tok) <= 1:
            continue
        sub = core[:, max(wx0, 0):max(wx1, 1)]
        ys = np.where(sub.sum(axis=1) > 0)[0]
        if len(ys):
            hs.append(int(ys.max() - ys.min() + 1))
    xheight = float(np.median(hs)) * 0.8 if hs else 8.0

    proposals, notes = [], []
    for idx, (tok, (wx0, wx1)) in enumerate(zip(toks, words)):
        sub = core[:, max(wx0, 0):max(wx1, 1)]
        if sub.shape[1] < 1:
            continue
        wh, ww = sub.shape
        if len(tok) == 1:
            sym = classify_symbol(sub, xheight)
            if sym and sym != tok:
                # 字形证据无歧义，且与字体无关 → 直接采纳，不走全局 IoU 裁决
                # （单个窄字形只占整行 1% 面积，全局 IoU 分不出来）
                proposals.append((idx, sym, 'symbol'))
                notes.append('%s->%s(字形)' % (tok, sym))
            continue
        if not any(c.isalpha() for c in tok):
            continue

        def score(c):
            m = render_mask(c, font_file)
            if m is None:
                return None
            m2 = resize_mask(m, m.shape[1] * (wh / m.shape[0]), wh)
            W = max(ww, m2.shape[1])
            a = np.zeros((wh, W), bool)
            b = np.zeros((wh, W), bool)
            a[:, :ww] = sub
            b[:, :m2.shape[1]] = m2
            return _iou(a, b)

        base = score(tok)
        if base is None:
            continue
        best = (tok, base)
        for c in dict.fromkeys([tok.upper(), tok.lower(),
                                tok[:1].upper() + tok[1:].lower()]):
            if c == tok:
                continue
            sc = score(c)
            if sc is not None and sc > best[1]:
                best = (c, sc)
        if best[0] != tok and best[1] >= base + margin:
            proposals.append((idx, best[0], 'case'))
            notes.append('%s->%s(%.3f->%.3f)' % (tok, best[0], base, best[1]))
    return proposals, notes


# --------------------------------------------------------------------------
# 字体匹配
# --------------------------------------------------------------------------

def font_files():
    out = []
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(('.ttf', '.otf', '.ttc')):
                out.append(os.path.join(d, n))
    return out


def family_of(path):
    try:
        fam, sty = ImageFont.truetype(path, 20).getname()
        return fam, sty
    except Exception:
        return os.path.basename(path), ''


def render_mask(text, font_path, px=RENDER_PX):
    try:
        f = ImageFont.truetype(font_path, px)
    except Exception:
        return None
    tmp = Image.new('L', (10, 10), 255)
    l, t, r, b = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=f)
    w, h = max(r - l, 1), max(b - t, 1)
    img = Image.new('L', (w + 24, h + 24), 255)
    ImageDraw.Draw(img).text((12 - l, 12 - t), text, font=f, fill=0)
    a = np.asarray(img) < 128
    ys, xs = np.where(a)
    if not len(ys):
        return None
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def resize_mask(m, w, h):
    im = Image.fromarray((m * 255).astype(np.uint8))
    return np.asarray(im.resize((max(int(w), 1), max(int(h), 1)),
                                Image.LANCZOS)) > 127


def _iou(a, b):
    if a.shape != b.shape:
        return 0.0
    u = int((a | b).sum())
    return (int((a & b).sum()) / u) if u else 0.0


def _tight_mask(m):
    """紧裁到墨迹包围盒；全空返回 None。"""
    ys = np.where(m.any(axis=1))[0]
    xs = np.where(m.any(axis=0))[0]
    if not len(ys) or not len(xs):
        return None
    return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _word_aligned_iou(src_mask, rend_mask):
    """按列投影切词、**逐词紧裁后**再比的 IoU。切不齐返回 None。

    为什么不拿整行 IoU 排字体（真踩过，代价是选错字体）：
    把渲染结果横向拉伸到源宽度时，每个字形的横向位置误差是**累积**的，
    行末能到几像素；而小字号下笔画只有 1~2px 宽，于是整行 IoU 基本由
    "拉伸相位"决定，而不是由字形决定。实测**同一个字体**只改一个字母的
    大小写（源图真值是大写 V）：
        文本        整行 IoU    逐词对齐 IoU
        'O.v.D. …'  0.7244  ← 错的文本反而高   0.7056
        'O.V.D. …'  0.5090  ← 对的文本反而低   0.7235
    整行口径**把方向搞反了**，并据此把最佳字体从 `Swis721 Cn BT / Bold`
    选成了 `Swis721 BlkCn BT / Black`（笔画明显更重）。
    逐词对齐后方向与真值一致，也与独立证据"墨迹密度"一致：
    源 0.2852、Cn BT/Bold 0.3106、BlkCn BT/Black 0.3563。

    **对齐能纠回方向，但不能当成"字形级判别器"用**：单字形的大小写差异
    仍在噪声底附近（0.7235 vs 0.7056，差 0.018）。真正定案靠的是
    直接量字形——源图那个字高 12px（= cap-height，x-height 只有 9px）、
    宽 8px（= 该字体大写 V 的宽度，小写 v 只有 6~7px）。见 `case_by_height`。

    代价与边界：逐词拉伸抹掉了词内字距差异，所以**词间宽度差必须继续由
    `width_ratio` 单独把关**（它算的是整行自然宽度比，不经拉伸）。
    词数切不一致时返回 None，调用方回退到整行 IoU。
    """
    sw_runs, _ = gap_split(src_mask)
    rw_runs, _ = gap_split(rend_mask)
    if not sw_runs or len(sw_runs) != len(rw_runs):
        return None
    inter = uni = 0
    for (a0, a1), (b0, b1) in zip(sw_runs, rw_runs):
        A = _tight_mask(src_mask[:, a0:a1])
        B = _tight_mask(rend_mask[:, b0:b1])
        if A is None or B is None:
            continue
        B = resize_mask(B, A.shape[1], A.shape[0])
        inter += int((A & B).sum())
        uni += int((A | B).sum())
    return (inter / uni) if uni else None


def match_fonts(src_mask, text, corel_fonts=None, top=12):
    """把候选字体渲染同一串文本，按高度归一后比 IoU。

    两个指标分开看：
      iou   —— **逐词对齐**后的 IoU（纯字形形状；切不齐时回退整行 IoU）
      wr    —— 渲染宽度 / 源宽度（字距与字宽的宏观差异）
    只看 iou 会把"字形像但字距差很多"的字体排上来；只看 wr 又分不清
    谁的字形更像。综合分 = iou*0.7 + max(0,1-|wr-1|)*0.3。

    **为什么 iou 用逐词对齐而不是整行**：小字号下笔画只有 1~2px 宽，
    整行 IoU 会被"横向拉伸的相位"主导——实测只改一个字母大小写就能让
    同一字体的整行 IoU 从 0.7244 掉到 0.5090，并且把最佳字体选错。
    详见 `_word_aligned_iou`。`iou_line` 字段保留整行值以便对照。
    """
    sh, sw = src_mask.shape
    rows = []
    for p in font_files():
        m = render_mask(text, p)
        if m is None:
            continue
        s = sh / m.shape[0]
        m2 = resize_mask(m, m.shape[1] * s, sh)
        wr = m2.shape[1] / sw
        m3 = resize_mask(m2, sw, sh)
        fam, sty = family_of(p)
        if corel_fonts and fam not in corel_fonts:
            continue
        iou_line = _iou(src_mask, m3)
        iou_al = _word_aligned_iou(src_mask, m3)
        rows.append({
            'family': fam, 'style': sty, 'file': os.path.basename(p),
            'iou': round(iou_al if iou_al is not None else iou_line, 4),
            'iou_line': round(iou_line, 4),
            'iou_aligned': (round(iou_al, 4) if iou_al is not None else None),
            'width_ratio': round(wr, 3),
            'rendered_w_px': int(m2.shape[1]),
        })
    for r in rows:
        r['score'] = round(r['iou'] * 0.7 + max(0.0, 1 - abs(r['width_ratio'] - 1)) * 0.3, 4)
    rows.sort(key=lambda r: -r['score'])
    ious = sorted(r['iou'] for r in rows)
    stats = {'n': len(rows), 'median_iou': 0.0, 'p75_iou': 0.0, 'max_iou': 0.0}
    if ious:
        stats['median_iou'] = round(float(np.median(ious)), 4)
        stats['p75_iou'] = round(float(np.percentile(ious, 75)), 4)
        stats['max_iou'] = round(float(max(ious)), 4)
    return rows[:top], stats


# --------------------------------------------------------------------------
# 判定：这块墨迹到底是不是"一行文字"
# --------------------------------------------------------------------------

def cc_sizes(mask, min_px=2):
    """8 邻域连通分量的**尺寸列表**（降序）。用行程 + 并查集，不逐像素 DFS。

    为什么不调 cv2.connectedComponents：本脚本的位图侧刻意只依赖 numpy + PIL
    （重依赖只有 OCR 那一半），这样在没有装 opencv 的机器上字体匹配仍能跑。
    行程并查集在文字这种"行程数远少于像素数"的图上比逐像素扫描快得多
    （一行文字 16 行、每行几十个行程，而像素有六千个）。

    min_px 过滤掉抗锯齿产生的碎点，否则分量数会被噪声抬高。
    """
    h, w = mask.shape
    if not mask.any():
        return []
    runs = []           # [y, x0, x1)
    row_idx = []        # 每行的行程下标
    for y in range(h):
        row = mask[y]
        idx, x = [], 0
        while x < w:
            if row[x]:
                x0 = x
                while x < w and row[x]:
                    x += 1
                idx.append(len(runs))
                runs.append([y, x0, x])
            else:
                x += 1
        row_idx.append(idx)

    parent = list(range(len(runs)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for y in range(1, h):
        prev, cur = row_idx[y - 1], row_idx[y]
        i = j = 0
        while i < len(prev) and j < len(cur):
            _, py0, py1 = runs[prev[i]]
            _, cy0, cy1 = runs[cur[j]]
            # 8 邻域：两个列区间只要相交或仅隔 1 列就算连通
            if py0 <= cy1 and cy0 <= py1:
                ra, rb = find(prev[i]), find(cur[j])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            if py1 <= cy1:
                i += 1
            else:
                j += 1

    agg = {}
    for k, (_, x0, x1) in enumerate(runs):
        r = find(k)
        agg[r] = agg.get(r, 0) + (x1 - x0)
    return sorted((s for s in agg.values() if s >= min_px), reverse=True)


def glyph_stats(core):
    """量这块墨迹"像不像一行文字"——只用几何，不看 OCR 文本。

    实测（同一批真实区域，`references/live-text-design.md` §1.6b 有完整表）：

        区域             分量  中位   最大    max/med  填墨率  尺寸CV
        text01(真文字)    53   32     63      1.97     0.285   0.582
        mark01(字标)      21  328   5803     17.69     0.463   2.326
        art01(插图)       66   20.5 19800   965.85     0.368   2.951

    真文字这一行的特征很清楚：分量多、**大小均匀**（max/med 约 2）、
    填墨率低（笔画细）。线稿正相反。这里只负责把统计量算出来，
    要不要据此拒绝交给 decide_convert（阈值必须可调、且必须记进产物里）。
    """
    sizes = cc_sizes(core)
    if not sizes:
        return {'n_cc': 0, 'cc_median_px': 0.0, 'cc_max_px': 0,
                'cc_max_over_median': 0.0, 'ink_fill': 0.0, 'cc_size_cv': 0.0}
    arr = np.asarray(sizes, float)
    med = float(np.median(arr))
    return {
        'n_cc': len(sizes),
        'cc_median_px': round(med, 1),
        'cc_max_px': int(sizes[0]),
        'cc_max_over_median': round(sizes[0] / med, 2) if med else 0.0,
        'ink_fill': round(float(core.sum()) / core.size, 4),
        'cc_size_cv': round(float(arr.std() / (arr.mean() or 1)), 4),
    }


def decide_convert(final_iou, lift, width_ratio, n_cc, n_char,
                   min_iou=0.72, min_lift=1.25, min_iou_floor=0.35,
                   min_wr=0.65, max_wr=1.50,
                   min_cc_per_char=0.25, max_cc_per_char=4.0):
    """这一块到底该不该转成活字。返回 verdict + 每条判据的取值 + 拒绝理由。

    四条判据。**前两条是硬门槛**（"这到底是不是一行文字"），后两条是相似度
    （"像不像这个字体"），后两条满足其一即可：

      A. 分量数 / 字符数      必须落在 [min_cc_per_char, max_cc_per_char]
      B. 最佳候选的自然宽度比  必须落在 [min_wr, max_wr]
      C. 绝对 IoU >= min_iou
      D. lift >= min_lift 且 final_iou >= min_iou_floor

    **为什么 A、B 可以用绝对区间，而相似度不能**：
      IoU 的可达上限随字号退化——小字号下笔画只有 1~2px 宽，栅格化与
      横向拉伸的相位误差就能吃掉一大截。实测 16px 高的页脚，用最佳字体
      配完全正确的文本也只有 **0.7235**（`min_iou` 因此定在 0.72，
      而不是常见的 0.8+）。所以相似度主要看"相对候选集中位的提升倍数"，
      绝对门槛只当兜底。而 A 与 B 都是**与字号无关的比值**：
      A 的期望值恒为 1（一个字形一个分量），B 的期望值也恒为 1
      （字体的自然字宽就是源宽度），所以绝对区间不仅合理，而且是唯一稳的判据。

      ⚠️ **换度量必须连门槛一起重校。** 实测把 `iou` 从"整行"换成
      "逐词对齐"（见 `_word_aligned_iou`）后，同一页脚的
      `final_iou` 0.7244→0.7235（几乎没动），但 `lift` 2.675→**1.68**
      ——因为中位候选的 IoU 也一起抬高了。若只改度量不改门槛，
      一个本该 convert 的页脚会被"相似度不足"拒掉（真踩过）。
      同时 `rebuild_iou` 必须与 `match_fonts` 同源，否则
      `final_iou` 与 `median_iou` 来自两套度量，`lift` 算出来是废数。

    **为什么 A、B 必须是硬门槛（不能像 C/D 那样"满足其一"）**：
      它们是"这块东西是不是文字"的前提。前提不成立时，C、D 算出来的高分
      是**拉伸出来的假象**——B 的分子（渲染宽度）正是靠横向拉伸才对齐的，
      而拉伸这一步本身就把最大的差异抹掉了。实测反例：把工具插图区当文字区
      喂进来，OCR 幻觉出 'wander'（6 字），最佳候选 Impact 的
      wr=1.887、rebuild_iou=0.386、lift=1.531 —— C 不过，D 却全过
      （1.531>1.25、0.386>0.35），于是判定 convert，真的在 CDR 里建了
      一行 'wander'。而 A 一眼看穿：那块图有 66 个连通分量，OCR 却说
      只有 6 个字，比值 11.0（真页脚是 1.10，差 10 倍）。

    **A 与 B 互补**，这是它们能一起用住的原因：
      短幻觉（几个字盖住复杂图形）→ A 抓到，比值偏大；
      长幻觉（长串盖住简单图形）  → B 抓到，自然宽度远大于源宽度。
    """
    nch = max(int(n_char), 1)
    ccr = float(n_cc) / nch
    checks = {
        'cc_per_char': {'value': round(ccr, 3), 'min': min_cc_per_char,
                        'max': max_cc_per_char, 'n_cc': int(n_cc),
                        'n_char': int(n_char),
                        'ok': min_cc_per_char <= ccr <= max_cc_per_char},
        'width_ratio': {'value': round(width_ratio, 3), 'min': min_wr,
                        'max': max_wr,
                        'ok': min_wr <= width_ratio <= max_wr},
        'abs_iou': {'value': round(final_iou, 4), 'min': min_iou,
                    'ok': final_iou >= min_iou},
        'rel_lift': {'value': round(lift, 3), 'min': min_lift,
                     'floor': min_iou_floor,
                     'ok': (lift >= min_lift and final_iou >= min_iou_floor)},
    }
    reasons = []
    if not checks['cc_per_char']['ok']:
        reasons.append('分量数/字符数 %.2f 超出 [%.2f, %.2f]——%d 个连通分量'
                       '对 %d 个字符，这不像一行文字'
                       % (ccr, min_cc_per_char, max_cc_per_char, n_cc, n_char))
    if not checks['width_ratio']['ok']:
        reasons.append('最佳字体的自然宽度比 %.3f 超出 [%.2f, %.2f]——没有哪个'
                       '字体的自然字宽与这块区域的宽高比相符'
                       % (width_ratio, min_wr, max_wr))
    gate_ok = checks['cc_per_char']['ok'] and checks['width_ratio']['ok']
    sim_ok = checks['abs_iou']['ok'] or checks['rel_lift']['ok']
    if gate_ok and not sim_ok:
        reasons.append('相似度不足（IoU %.4f < %.2f，且领先候选集中位仅 '
                       '%.2f× < %.2f）'
                       % (final_iou, min_iou, lift, min_lift))
    verdict = 'convert' if (gate_ok and sim_ok) else 'keep_trace'
    return {
        'verdict': verdict,
        'checks': checks,
        'reasons': [] if verdict == 'convert' else reasons,
        # 两类拒绝要分开：not_text 是"这根本不是文字"，weak_match 是
        # "像文字但字体库里没有足够接近的"。处置方式不同——
        # 前者该去查区域切分，后者只能保留描摹轮廓。
        'reject_kind': (None if verdict == 'convert'
                        else ('not_text' if not gate_ok else 'weak_match')),
    }


# --------------------------------------------------------------------------
# CorelDRAW 侧
# --------------------------------------------------------------------------

def corel_font_families(app):
    """CorelDRAW 自己能用的字体族名（拿它过滤候选，避免推荐了 CDR 里没有的）。"""
    fams = set()
    try:
        fl = app.FontList
        for i in range(1, fl.Count + 1):
            try:
                fams.add(str(fl.Item(i)))
            except Exception:
                pass
    except Exception:
        return None
    return fams


def solve_size_and_width(shp, target_h_mm, target_w_mm, app, lo=4.0, hi=40.0):
    """字号只决定高度，宽度靠非等比拉伸补——两者分开解，别用字号凑宽度。

    CorelDRAW 的字号是点值，形状高度随之线性变化，所以先二分字号让高度对上，
    再用 Stretch 把宽度拉到目标（Stretch 只改宽度、不改高度）。
    """
    best = None
    for _ in range(18):
        mid = (lo + hi) / 2
        try:
            shp.Text.Story.Size = mid
            app.Refresh()
        except Exception:
            break
        h = shp.SizeHeight
        if best is None or abs(h - target_h_mm) < abs(best[1] - target_h_mm):
            best = (mid, h, shp.SizeWidth)
        if h < target_h_mm:
            lo = mid
        else:
            hi = mid
    if best is None:
        return None
    size, h, w = best
    try:
        shp.Text.Story.Size = size
        app.Refresh()
        w = shp.SizeWidth
    except Exception:
        pass
    scale = (target_w_mm / w) if w else 1.0
    return {'size_pt': round(size, 3), 'height_mm': round(h, 4),
            'width_mm_before': round(w, 4), 'x_scale': round(scale, 5)}


def _hex_to_rgb(s):
    s = s.lstrip('#')
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _get_layer(page, name, reuse_first=True):
    """取名为 name 的图层；没有就新建。

    注意 `CreateLayer` 挂在 **Page** 上，不在 `Layers` 集合上——
    `page.Layers.CreateLayer(...)` 会报 `IVGLayers object has no attribute`。
    集合本身只有 Item / Count / Find / Top / Bottom。
    """
    for i in range(1, page.Layers.Count + 1):
        lay = page.Layers.Item(i)
        if str(lay.Name) == name:
            return lay
    if reuse_first and page.Layers.Count == 1:
        lay = page.Layers.Item(1)
        # 只复用**默认名**的图层（"图层 1" / "Layer 1"）。
        # 否则多个区域会反复改名，把上一个区域刚建好的图层名覆盖掉。
        nm = str(lay.Name)
        if nm.startswith('图层') or nm.lower().startswith('layer'):
            lay.Name = name
            return lay
    return page.CreateLayer(name)


def apply_region(app, page, rec, mm_per_px, layer_suffix, rgb, log=print):
    """把一条判定为 convert 的记录真的建成活字，并回读校验定位误差。

    坐标约定：`doc.ReferencePoint = 3`（cdrTopLeft）时，`PositionX/Y` 就是
    形状包围盒的左上角；而 CorelDRAW 的 y 轴向上、原点在页面左下，
    所以目标"距页顶 y_top"要写成 `page_h - y_top`。

    建完必须**回读包围盒再校正**：`Stretch` 之后位置会漂，
    只设一次坐标不校验的话误差能到零点几毫米。
    """
    page_h = float(page.SizeHeight)
    box = rec['source_box_px']
    x_mm = box[0] * mm_per_px
    y_top_mm = box[1] * mm_per_px
    tgt_w, tgt_h = rec['size_mm']
    tgt_y = page_h - y_top_mm

    lay = _get_layer(page, rec['region'] + layer_suffix)
    # **幂等保护**：该图层已有形状就不动它。
    # 否则重跑同一命令会在同一位置叠出第二个文本——两行字重叠，比不转更糟。
    # 也不自动清空：用户可能已经改过这里的文字，静默删除等于吃掉他的编辑。
    # 想重来就先把这一层删掉或改名，或换一个 --live-layer-suffix。
    try:
        existing = int(lay.Shapes.Count)
    except Exception:
        existing = 0
    if existing:
        return {'ok': False,
                'error': '图层 %r 里已有 %d 个形状，跳过以免叠字。'
                         '要重做请先删掉该图层，或换一个 --live-layer-suffix'
                         % (str(lay.Name), existing)}

    shp = lay.CreateArtisticText(x_mm, tgt_y, rec['text'])
    if shp is None:
        return {'ok': False, 'error': 'CreateArtisticText 返回空'}

    # 字体名要用 family 名（不是文件名）
    fam = rec['font']['family']
    try:
        shp.Text.Story.Font = fam
    except Exception as e:
        return {'ok': False, 'error': '设置字体 %r 失败: %s' % (fam, e)}

    info = solve_size_and_width(shp, tgt_h, tgt_w, app)
    if not info:
        return {'ok': False, 'error': '字号求解失败'}
    try:
        shp.Stretch(info['x_scale'], 1.0)
        app.Refresh()
    except Exception as e:
        return {'ok': False, 'error': '横向缩放失败: %s' % e}

    # 填色 / 去轮廓。用主流程验证过的写法：
    #   Fill.ApplyUniformFill(app.CreateRGBColor(...)) 而不是 UniformColor.RGBAssign
    #   Outline.SetNoOutline() 而不是 Outline.Type = 0
    try:
        shp.Fill.ApplyUniformFill(app.CreateRGBColor(*rgb))
        shp.Outline.SetNoOutline()
    except Exception:
        try:
            shp.Fill.UniformColor.RGBAssign(*rgb)
            shp.Outline.Type = 0
        except Exception:
            pass

    # 定位 + 校正
    err = None
    for _ in range(8):
        try:
            bb = shp.BoundingBox
            left, top = float(bb.Left), float(bb.Top)
        except Exception:
            break
        dx, dy = x_mm - left, tgt_y - top
        err = (abs(dx), abs(dy))
        if abs(dx) < 0.005 and abs(dy) < 0.005:
            break
        try:
            shp.PositionX = shp.PositionX + dx
            shp.PositionY = shp.PositionY + dy
            app.Refresh()
        except Exception:
            break

    try:
        bb = shp.BoundingBox
        got = {'left': round(float(bb.Left), 4), 'top': round(float(bb.Top), 4),
               'w': round(float(bb.Width), 4), 'h': round(float(bb.Height), 4)}
        err = (round(abs(got['left'] - x_mm), 4),
               round(abs(got['top'] - tgt_y), 4),
               round(abs(got['w'] - tgt_w), 4),
               round(abs(got['h'] - tgt_h), 4))
    except Exception:
        got = None
    return {'ok': True, 'layer': str(lay.Name), 'size': info, 'bbox_mm': got,
            'target_mm': [round(x_mm, 4), round(tgt_y, 4),
                          round(tgt_w, 4), round(tgt_h, 4)],
            'max_error_mm': max(err) if err else None}


def _stat_sig(path):
    """文件的 `(大小, mtime_ns)` 指纹，用来判断一次保存**是否真的落盘**。

    返回 `(None, None)` 表示文件还不存在（新建文档时是正常情况）。

    存在的意义：跨进程写文件时，"调用没报错"完全不代表"内容写进去了"。
    CDR 被别的程序打开时 `doc.Save()` 会静默变成空操作，只看日志是发现不了的。
    """
    try:
        st = os.stat(path)
    except OSError:
        return (None, None)
    return (st.st_size, st.st_mtime_ns)


def _parse_replace(specs):
    """解析 `--replace-traced` 的 `REGION=LAYER`（可重复）。"""
    out = {}
    for s in specs or []:
        if '=' not in s:
            raise ValueError('--replace-traced 要写成 REGION=LAYER，收到 %r' % s)
        k, v = s.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def _drop_layer_shapes(page, layer_name, log=print):
    """删掉 `layer_name` 里的全部形状。

    **为什么必须删**：描摹轮廓与活字在**同一位置**。两者叠加等于把这一行字
    加粗一遍，视觉上比源图重——转活字的意义是"替代轮廓"，不是"再叠一层"。

    **只在活字建好且回读校验通过之后才调**（调用点负责把关）：
    先删后建的话，一旦建字失败就两头落空，用户的轮廓也没了。

    删完图层空了就连图层一起删——留一个空的 `05_TEXT` 只会让人困惑
    （文字明明在 `footer_LIVE` 里）。
    """
    lay = None
    for i in range(1, page.Layers.Count + 1):
        if str(page.Layers.Item(i).Name) == layer_name:
            lay = page.Layers.Item(i)
            break
    if lay is None:
        # 图层不在。多半是**已经替换过**（上次跑成功、图层被删了），
        # 也可能是图层名写错。都不该让整轮失败——只报出来，不动退出码。
        return {'ok': False, 'removed': 0,
                'error': '没有名为 %r 的图层（可能已经替换过，或图层名写错）'
                         % layer_name}

    total = int(lay.Shapes.Count)
    removed = 0
    # **从后往前删**：正序删会让 Item 索引逐次前移，漏掉一半。
    for j in range(total, 0, -1):
        try:
            lay.Shapes.Item(j).Delete()
            removed += 1
        except Exception as e:
            log('  [警告] 删除 %s 第 %d 个形状失败: %s' % (layer_name, j, e))

    out = {'ok': True, 'layer': layer_name, 'removed': removed, 'was': total}
    if total and removed == total:
        try:
            lay.Delete()
            out['layer_deleted'] = True
        except Exception as e:
            # 删图层不一定支持；空图层留着也不影响几何，不算失败
            out['layer_deleted'] = False
            out['layer_delete_error'] = str(e)
    return out


def apply_all(args, summary, log=print):
    """打开/新建 CDR，把 convert 的区域建成活字，保存并回读校验。"""
    import cdr_common as C

    conv = {k: v for k, v in summary.items()
            if isinstance(v, dict) and v.get('verdict') == 'convert'}
    skipped = [k for k, v in summary.items()
               if isinstance(v, dict) and v.get('verdict') != 'convert']
    if not conv:
        log('没有判定为 convert 的区域，不建活字。')
        if skipped:
            log('  跳过：%s' % ', '.join(skipped))
        return 0

    app = C.connect_coreldraw()
    log('CorelDRAW %s' % app.Version)
    target = os.path.abspath(args.apply)
    rgb = _hex_to_rgb(args.live_color)

    if os.path.isfile(target):
        doc = C.open_document(app, target)
    else:
        # 没有现成 CDR：按源图尺寸新建一个，方便先看效果
        gray = load_gray(args.image)
        doc = app.CreateDocument()
        doc.Unit = 3
        page = doc.Pages.Item(1)
        page.SetSize(gray.shape[1] * args.mm_per_px,
                     gray.shape[0] * args.mm_per_px)
        doc.SaveAs(target, None)
        log('新建文档 %s  页面 %.3f x %.3f mm'
            % (target, page.SizeWidth, page.SizeHeight))

    try:
        doc.Unit = 3
        doc.ReferencePoint = 3          # 左上角参考点，Position 即包围盒左上
        page = doc.ActivePage
    except Exception as e:
        log('设置文档状态失败: %s' % e)
        return 1

    repl = _parse_replace(args.replace_traced)
    results = {}
    for name, rec in conv.items():
        log()
        log('建活字：%s  %r' % (name, rec['text']))
        log('  字体 %s / %s' % (rec['font']['family'], rec['font']['style']))
        r = apply_region(app, page, rec, args.mm_per_px,
                         args.live_layer_suffix, rgb, log)
        results[name] = r
        rec['apply_result'] = r          # 记进总表，便于事后核对
        if not r['ok']:
            log('  [失败] %s' % r['error'])
            continue
        log('  字号 %.3f pt  横向缩放 %.5f  →  %.4f x %.4f mm'
            % (r['size']['size_pt'], r['size']['x_scale'],
               r['bbox_mm']['w'], r['bbox_mm']['h']))
        log('  目标 %.4f,%.4f %.4f x %.4f  →  最大误差 %.4f mm'
            % (r['target_mm'][0], r['target_mm'][1],
               r['target_mm'][2], r['target_mm'][3], r['max_error_mm']))

        # 活字建好、位置误差也可接受，才动描摹轮廓。
        # 误差门槛用 0.05mm：比它大说明活字没落在该在的地方，
        # 这时候删轮廓等于用一个位置不对的东西替换掉一个位置对的。
        if name in repl:
            max_err = r.get('max_error_mm') or 0.0
            if max_err > 0.05:
                log('  [跳过替换] 定位误差 %.4f mm > 0.05 mm，'
                    '保留描摹轮廓更安全' % max_err)
                r['replaced'] = {'ok': False, 'error': '定位误差过大'}
            else:
                log('  删掉描摹轮廓层 %s（活字已就位）' % repl[name])
                r['replaced'] = _drop_layer_shapes(page, repl[name], log)
                log('    → %s' % ('已删 %d 个形状%s'
                                  % (r['replaced']['removed'],
                                     '，图层已删' if r['replaced'].get('layer_deleted')
                                     else '，图层保留（空）')
                                  if r['replaced']['ok']
                                  else r['replaced']['error']))

    # 保存，并**核验真的落盘**。
    #
    # 为什么必须核验：目标 CDR 被另一个进程打开时（最典型的就是用户自己开着
    # CorelDRAW 在看这个文件），文件是**只读**的，而 `doc.Save()` 在这种情况下
    # 既不抛异常、也不写文件——日志照样打印"已保存"，磁盘上却还是旧内容。
    #
    # 这个坑真踩过：转活字整轮跑完、内存里回读核验全对、日志打印"已保存"，
    # 结果重新打开文件发现 `05_TEXT` 还在、活字根本没有，白跑一轮还差点当成
    # 交付完成。**"调用没报错"不等于"结果发生了"**，凡是跨进程落盘的写操作
    # 都要回读磁盘确认。
    #
    # 判据用 (大小, mtime_ns) 是否变化：便宜且足够——真写了必然变。
    save_ok = True
    before = _stat_sig(target)
    try:
        C.release_optimization(app)
        doc.Save()
    except Exception as e:
        log('保存失败: %s' % e)
        save_ok = False
    else:
        after = _stat_sig(target)
        if after == before:
            log()
            log('[失败] 保存没有落盘：%s 的大小与修改时间都没变。' % target)
            log('       文件多半正被另一个程序打开（比如 CorelDRAW），处于只读状态。')
            log('       请关闭该文件后重跑——本轮改动只在内存里，没有写进文件。')
            save_ok = False
        else:
            log()
            log('已保存 %s（%s → %s 字节）'
                % (target, before[0] if before[0] is not None else '新建',
                   after[0]))

    # 回读核验：形状类型必须是 cdrTextShape(6)
    log()
    log('回读核验：')
    for name, r in results.items():
        if not r['ok']:
            continue
        try:
            lay = None
            for i in range(1, page.Layers.Count + 1):
                if str(page.Layers.Item(i).Name) == r['layer']:
                    lay = page.Layers.Item(i)
            n = lay.Shapes.Count
            s = lay.Shapes.Item(1)
            txt = ''
            try:
                txt = str(s.Text.Story.Text)
            except Exception:
                pass
            log('  %s 层 %s：形状 %d 个，类型 %d（6=文本），内容 %r'
                % (name, r['layer'], n, s.Type, txt[:60]))
        except Exception as e:
            log('  %s 回读失败: %s' % (name, e))

    if skipped:
        log()
        log('以下区域未转活字（保留描摹轮廓）：%s' % ', '.join(skipped))
    return 0 if save_ok else 3


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def parse_region(spec):
    """NAME=y0,y1,x0,x1 或 NAME=px:y0,y1,x0,x1（源图像素，左上原点）。"""
    if '=' not in spec:
        raise ValueError('区域要写成 NAME=y0,y1,x0,x1')
    name, rest = spec.split('=', 1)
    rest = rest.split(':', 1)[-1]
    v = [int(t) for t in rest.replace(' ', '').split(',')]
    if len(v) != 4:
        raise ValueError('区域坐标要 4 个数')
    return name.strip(), tuple(v)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='把位图文字转成 CorelDRAW 活字（识别 → 字体匹配 → 重建 → 校验）')
    ap.add_argument('--image', required=True, help='源位图')
    ap.add_argument('--region', action='append', required=True,
                    help='文字区域 NAME=y0,y1,x0,x1（源图像素，左上原点），可多次给')
    ap.add_argument('--mm-per-px', type=float, required=True, help='标定比例')
    ap.add_argument('--out-dir', required=True, help='产物目录')
    ap.add_argument('--min-iou', type=float, default=0.72,
                    help='绝对门槛：重建 IoU 高于它直接判定可转活字（默认 0.72，'
                         '适合大字号；小字够不到这个数，看下面的相对判据）')
    ap.add_argument('--min-lift', type=float, default=1.25,
                    help='相对门槛：最佳字体 IoU ÷ 候选集中位 IoU 的倍数'
                         '（默认 1.25，与字号无关）')
    ap.add_argument('--min-iou-floor', type=float, default=0.35,
                    help='相对判据的下限，避免在噪声上判"可转"（默认 0.35）')
    ap.add_argument('--min-width-ratio', type=float, default=0.65,
                    help='硬门槛：最佳候选字体的自然宽度比下限（默认 0.65）。'
                         '文字比字体自然宽度更窄时（负字距/压缩）会偏小')
    ap.add_argument('--max-width-ratio', type=float, default=1.50,
                    help='硬门槛：最佳候选字体的自然宽度比上限（默认 1.50）。'
                         '**这条是拦住"非文字区被 OCR 幻觉成文字"的关键**')
    ap.add_argument('--min-cc-per-char', type=float, default=0.25,
                    help='硬门槛：连通分量数 ÷ 字符数 的下限（默认 0.25）。'
                         '字形连在一起时会偏小')
    ap.add_argument('--max-cc-per-char', type=float, default=4.0,
                    help='硬门槛：连通分量数 ÷ 字符数 的上限（默认 4.0）。'
                         '**这条是拦住"短幻觉"的关键**——真文字实测 1.10，'
                         '把插图区当文字区时会到 11.0')
    ap.add_argument('--corel-only', action='store_true',
                    help='只用 CorelDRAW 字体表里有的字体（需要 CDR 可用）')
    ap.add_argument('--apply', metavar='CDR', default=None,
                    help='把判定为 convert 的区域真的建成活字并写入这个 CDR。'
                         '文件不存在则按源图尺寸新建')
    ap.add_argument('--live-layer-suffix', default='_LIVE',
                    help='活字所在图层的后缀，默认 _LIVE（即 footer_LIVE）')
    ap.add_argument('--live-color', default='#000000',
                    help='活字填充色，默认 #000000')
    ap.add_argument('--replace-traced', action='append', metavar='REGION=LAYER',
                    help='建好活字后删掉这个图层里的**描摹轮廓**（可重复）。'
                         '描摹轮廓与活字同位置，叠加等于把这一行字加粗一遍——'
                         '转活字的意义是替代它，不是再叠一层。'
                         '例：--replace-traced footer=05_TEXT。'
                         '只在活字定位误差 ≤ 0.05mm 时才执行')
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    gray = load_gray(args.image)

    corel_fonts = None
    if args.corel_only:
        try:
            import cdr_common as C
            corel_fonts = corel_font_families(C.connect_coreldraw())
            print('CorelDRAW 字体族 %d 个' % len(corel_fonts))
        except Exception as e:
            print('读 CorelDRAW 字体表失败（改为不限）：%s' % e)

    summary = {}
    for spec in args.region:
        name, (y0, y1, x0, x1) = parse_region(spec)
        print()
        print('=' * 74)
        print('区域 %s  px %d,%d..%d,%d' % (name, x0, y0, x1, y1))
        print('=' * 74)
        sub = gray[y0:y1, x0:x1]
        ink = strip_full_height_cols(sub < 128)
        box = tight_text_box(ink)
        if box is None:
            print('  该区域没有墨迹，跳过')
            summary[name] = {'status': 'empty'}
            continue
        ty0, ty1, tx0, tx1 = box
        core = ink[ty0:ty1, tx0:tx1]
        th, tw = core.shape
        print('  紧裁后文字 %dx%d px  = %.3f x %.3f mm'
              % (tw, th, tw * args.mm_per_px, th * args.mm_per_px))

        # --- 列投影切词
        words, gaps = gap_split(core)
        print('  列投影切出 %d 个词；间隙 %s' % (len(words), gaps[:24]))

        # --- OCR
        crop_img = Image.open(args.image).convert('L').crop(
            (x0 + tx0, y0 + ty0, x0 + tx1, y0 + ty1))
        # 把列投影的词数当**几何证据**交给 OCR 选择器（见 ocr_lines 的说明）
        lines, ocr_report, err = ocr_lines(crop_img, expect_words=len(words))
        if err:
            print('  [警告] %s' % err)
            ocr_text = ''
            ocr_raw = []
        else:
            for r in ocr_report:
                if r.get('error'):
                    print('    OCR %-16s 失败 %s' % (r['variant'], r['error']))
                else:
                    print('    OCR %-16s 置信 %.3f 词 %2d(距 %d) 字符 %3d  %r'
                          % (r['variant'], r['score'], r['words'],
                             r['word_gap'], r['chars'], r['text']))
            ocr_raw = [{'text': t, 'score': round(s, 4),
                        'box': [[int(p[0]), int(p[1])] for p in bx]}
                       for t, s, bx in lines]
            ocr_text = ' '.join(t for t, _, _ in lines)
            print('  采用: %r' % ocr_text)

        # --- 词边界核对
        # 列投影切出的词数是**几何证据**，OCR 给的空格是**统计证据**。
        # 两者一致才敢拿去做逐词形状搜索；不一致就只用 OCR 原样文本，
        # 不做逐词修正（宁可少修，不可修错）。
        text = ocr_text
        toks = [t for t in text.split(' ') if t]
        if text and len(toks) != len(words):
            print('  [注意] 列投影切出 %d 个词，OCR 给出 %d 个词，'
                  '不一致 → 跳过逐词形状修正' % (len(words), len(toks)))
        if not text:
            print('  没识别出文字 → 不转活字，保留描摹轮廓')
            summary[name] = {'status': 'no_ocr'}
            continue

        final_text = text
        case_notes = []
        applied = []
        if text and len(toks) == len(words):
            # 第一遍：用 OCR 原样文本匹配字体（大小写错了也能排出大致正确的候选，
            # 因为字形结构不受大小写影响太多）
            pre, _ = match_fonts(core, text, corel_fonts, top=1)
            if pre:
                fpath = os.path.join(FONT_DIRS[0], pre[0]['file'])

                def rebuild(txt):
                    m = render_mask(txt, fpath)
                    if m is None:
                        return 0.0
                    m2 = resize_mask(m, m.shape[1] * (th / m.shape[0]), th)
                    return _iou(core, resize_mask(m2, tw, th))

                props, notes = propose_fixes(core, words, text, fpath)
                # 注意第三个参数是**词列表**，不是整串文本。曾经传成 `text`，
                # `len('O.v.D. …')`=60 对上 `len(words)`=9 → 守卫判定"词数不一致"
                # → 静默返回空建议，整条修复等于没接线（见函数内的类型检查）。
                hprops, hnotes = case_by_height(core, words, toks)
                # 同一个词被两个来源都提了建议时保留"几何"那条：
                # 它不依赖字体、也不依赖 OCR 的置信度，证据更硬。
                claimed = {i for i, _, _ in hprops}
                props = [p for p in props if p[0] not in claimed] + hprops
                notes = notes + hnotes
                if notes:
                    print('  形状修正建议: %s' % '; '.join(notes[:10]))
                # 逐条试：**每次只加一个改动，整体 IoU 变好才留下**。
                # 这样改对的留下、改错的被否掉，互不牵连——比"全有全无回退"
                # 精确得多（实测曾因同批有改错的，把改对的 '•' 一起撤销了）。
                cur = text
                cur_iou = rebuild(cur)
                for idx, newtok, kind in props:
                    tk = cur.split(' ')
                    if idx >= len(tk):
                        continue
                    trial = list(tk)
                    trial[idx] = newtok
                    trial_text = ' '.join(trial)
                    if kind in ('symbol', 'case-height'):
                        # 字形/字高量出来的：证据无歧义、与字体无关，**直接采纳**。
                        # 单个字符只占整行约 1% 面积，全局 IoU 根本分不出来
                        # （`•` 是这样，`v`/`V` 也是这样）。
                        applied.append({'word': tk[idx], 'to': newtok,
                                        'kind': kind, 'adopted': 'geometry'})
                        cur = trial_text
                        continue
                    ti = rebuild(trial_text)
                    if ti > cur_iou + 1e-6:
                        applied.append({'word': tk[idx], 'to': newtok,
                                        'kind': kind,
                                        'iou_before': round(cur_iou, 4),
                                        'iou_after': round(ti, 4)})
                        cur, cur_iou = trial_text, ti
                    else:
                        applied.append({'word': tk[idx], 'to': newtok,
                                        'kind': kind, 'rejected': True,
                                        'iou_before': round(cur_iou, 4),
                                        'iou_after': round(ti, 4)})
                final_text = cur
                if applied:
                    print('  逐词裁决: %s'
                          % '; '.join('%s->%s %s' % (a['word'], a['to'],
                                                     '✅' if not a.get('rejected')
                                                     else '✗')
                                      for a in applied[:10]))
                if final_text != text:
                    print('  最终文本: %r (IoU %.4f -> %.4f)'
                          % (final_text, rebuild(text), cur_iou))

        # --- 字体匹配（用最终文本）
        cands, fstats = match_fonts(core, final_text, corel_fonts)
        print('  候选字体 %d 个，前 5：' % fstats['n'])
        for i, c in enumerate(cands[:5], 1):
            print('    %d. %-28s %-14s 综合 %.4f  IoU %.4f  宽度比 %.3f'
                  % (i, c['family'], c['style'], c['score'], c['iou'],
                     c['width_ratio']))

        if not cands:
            print('  没有可用的候选字体 → 不转活字')
            summary[name] = {'status': 'no_font', 'text': final_text}
            continue

        def rebuild_iou(txt, cand):
            """重建复核：用最终文本 + 最佳字体重渲染，再算一次相似度。

            **必须与 `match_fonts` 用同一个口径**（逐词对齐，切不齐才回退整行）。
            踩过一次：排名已经改成词级对齐、这里却还留着整行 IoU，于是
            `final_iou` 与 `fstats['median_iou']` 来自两套度量，lift 被算成
            1.18（真实是 1.68），一个本来该判 convert 的页脚被"相似度不足"拒掉。
            同一批数字必须同源。
            """
            m = render_mask(txt, os.path.join(FONT_DIRS[0], cand['file']))
            if m is None:
                return 0.0, None
            m2 = resize_mask(m, m.shape[1] * (th / m.shape[0]), th)
            m3 = resize_mask(m2, tw, th)
            al = _word_aligned_iou(core, m3)
            return round(al if al is not None else _iou(core, m3), 4), m3

        best = cands[0]
        final_iou, mask = rebuild_iou(final_text, best)
        if mask is not None:
            _write_compare(os.path.join(args.out_dir, 'compare_%s.png' % name),
                           core, mask)

        # --- 判定：先问"这到底是不是一行文字"，再问"像不像这个字体"
        med = fstats['median_iou'] or 1e-9
        lift = round(final_iou / med, 3)
        gstat = glyph_stats(core)
        n_char = len(final_text.replace(' ', ''))
        dec = decide_convert(
            final_iou, lift, best['width_ratio'], gstat['n_cc'], n_char,
            min_iou=args.min_iou, min_lift=args.min_lift,
            min_iou_floor=args.min_iou_floor,
            min_wr=args.min_width_ratio, max_wr=args.max_width_ratio,
            min_cc_per_char=args.min_cc_per_char,
            max_cc_per_char=args.max_cc_per_char)
        verdict = dec['verdict']
        print('  字形统计 %d 个连通分量 / %d 个字符 = %.2f'
              '（中位 %g px，最大/中位 %.2f，填墨率 %.3f）'
              % (gstat['n_cc'], n_char, dec['checks']['cc_per_char']['value'],
                 gstat['cc_median_px'], gstat['cc_max_over_median'],
                 gstat['ink_fill']))
        for r in dec['reasons']:
            print('  [拒绝] %s' % r)

        with open(os.path.join(args.out_dir, 'font_match_%s.json' % name), 'w',
                  encoding='utf-8') as f:
            json.dump({'text': final_text, 'candidates': cands,
                       'stats': fstats, 'glyph_stats': gstat,
                       'decision': dec}, f, ensure_ascii=False, indent=2)

        rec = {
            'region': name,
            'source_box_px': [x0 + tx0, y0 + ty0, x0 + tx1, y0 + ty1],
            'size_px': [tw, th],
            'size_mm': [round(tw * args.mm_per_px, 4), round(th * args.mm_per_px, 4)],
            'text': final_text,
            'ocr': {'raw': ocr_raw, 'joined': ocr_text,
                    'variants': ocr_report},
            'case_fixes': applied,
            'reverted': None,
            'word_count_by_projection': len(words),
            'font': best,
            'font_candidates': cands[:5],
            'font_stats': fstats,
            'font_match_iou': best['iou'],
            'rebuild_iou': final_iou,
            'lift_over_median': lift,
            'glyph_stats': gstat,
            'decision': dec,
            'reject_kind': dec['reject_kind'],
            'thresholds': {'min_iou': args.min_iou, 'min_lift': args.min_lift,
                           'min_iou_floor': args.min_iou_floor,
                           'min_width_ratio': args.min_width_ratio,
                           'max_width_ratio': args.max_width_ratio,
                           'min_cc_per_char': args.min_cc_per_char,
                           'max_cc_per_char': args.max_cc_per_char},
            'verdict': verdict,
        }
        summary[name] = rec
        print('  最佳字体 %s / %s  IoU %.4f  重建复核 %.4f  领先中位 %.2fx'
              % (best['family'], best['style'], best['iou'], final_iou, lift))
        if verdict == 'convert':
            print('  → 转活字')
        elif dec['reject_kind'] == 'not_text':
            print('  → 不转（这块区域**不是一行文字**：区域切分可能把它切错了，'
                  '或它本来就是图形。保留描摹轮廓）')
        else:
            print('  → 不转（保留描摹轮廓：字体库里没有足够接近的字体，'
                  '字形相似度不足以证明是同一字体）')

    with open(os.path.join(args.out_dir, 'live_text.json'), 'w',
              encoding='utf-8') as f:
        json.dump({'image': os.path.abspath(args.image),
                   'mm_per_px': args.mm_per_px,
                   'thresholds': {
                       'min_iou': args.min_iou,
                       'min_lift': args.min_lift,
                       'min_iou_floor': args.min_iou_floor,
                       'min_width_ratio': args.min_width_ratio,
                       'max_width_ratio': args.max_width_ratio,
                       'min_cc_per_char': args.min_cc_per_char,
                       'max_cc_per_char': args.max_cc_per_char},
                   'regions': summary}, f, ensure_ascii=False, indent=2)
    print()
    print('已写出 %s' % os.path.join(args.out_dir, 'live_text.json'))

    if args.apply:
        print()
        print('=' * 74)
        print('建活字到 %s' % os.path.abspath(args.apply))
        print('=' * 74)
        rc = apply_all(args, summary)
        # 把落盘结果补进 JSON，便于事后核对"判了什么、实际建了什么"
        for name, r in (summary.items() if rc == 0 else []):
            if isinstance(r, dict) and r.get('verdict') == 'convert':
                r['applied'] = True
        with open(os.path.join(args.out_dir, 'live_text.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'image': os.path.abspath(args.image),
                       'mm_per_px': args.mm_per_px,
                       'min_iou': args.min_iou,
                       'min_lift': args.min_lift,
                       'min_iou_floor': args.min_iou_floor,
                       'applied_to': os.path.abspath(args.apply),
                       'regions': summary}, f, ensure_ascii=False, indent=2)
        return rc
    return 0


def _write_compare(path, src_mask, cand_mask):
    """源 / 候选 上下并排对照图，便于肉眼复核（红=只源有，蓝=只候选有）。"""
    h, w = src_mask.shape
    scale = max(1, int(400 / max(w, 1)))
    S = Image.fromarray(np.where(src_mask, 0, 255).astype(np.uint8)).convert('RGB')
    C = Image.fromarray(np.where(cand_mask, 0, 255).astype(np.uint8)).convert('RGB')
    diff = np.zeros((h, w, 3), np.uint8)
    diff[...] = 255
    diff[src_mask & ~cand_mask] = (220, 40, 40)
    diff[~src_mask & cand_mask] = (40, 90, 220)
    diff[src_mask & cand_mask] = (0, 0, 0)
    D = Image.fromarray(diff)
    if scale > 1:
        S = S.resize((w * scale, h * scale), Image.NEAREST)
        C = C.resize((w * scale, h * scale), Image.NEAREST)
        D = D.resize((w * scale, h * scale), Image.NEAREST)
    out = Image.new('RGB', (S.width, S.height * 3 + 16), 'white')
    out.paste(S, (0, 0))
    out.paste(C, (0, S.height + 8))
    out.paste(D, (0, S.height * 2 + 16))
    out.save(path)


if __name__ == '__main__':
    sys.exit(main())
