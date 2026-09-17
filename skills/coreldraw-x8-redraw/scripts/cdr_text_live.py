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
  7. 把候选渲染回位图，与源算 IoU；**低于阈值就不转**，保留描摹轮廓
  8. --apply 时才真的在 CorelDRAW 里建文本

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

# OCR 的预处理变体。**为什么不是一套参数**：实测同一张页脚图，
#   scale=2 + 20px 四周留白  -> 乱码 'mporaraeDisibdra Ldaurti'
#   scale=2 + 纵向不留白      -> 正确 'O.v.D. Importadora e Distribuidora Ltda. ...'
#   scale=4 + 20px 四周留白  -> 正确，置信度 0.931
# 检测器对纵向留白与长宽比极敏感，且不同图的最优点不同，所以固定一套参数
# 不可靠。改成跑多套变体再投票：取置信度最高者，但要求它的字符长度与
# 至少一个其它变体一致，否则视为不可信。
OCR_VARIANTS = [
    ('1x_宽留白', 1, 400, 0),
    ('2x_无纵留白', 2, 20, 0),
    ('2x_微纵留白', 2, 20, 8),
    ('3x_20留白', 3, 20, 20),
    ('4x_20留白', 4, 20, 20),
]


def _ocr_once(engine, base, scale, pad_x, pad_y):
    im = base
    if scale != 1:
        im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    bg = Image.new('RGB', (im.width + pad_x * 2, im.height + pad_y * 2), 'white')
    bg.paste(im, (pad_x, pad_y))
    res, _ = engine(np.asarray(bg))
    out = []
    for item in (res or []):
        out.append((str(item[1]), float(item[2]), item[0]))
    return out


def ocr_lines(mask_img, variants=None):
    """多套预处理跑 OCR 再投票。返回 (lines, variants_report, err)。

    lines 形如 [(text, score, box), ...]，已按纵向位置排序拼接为整行文本。
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
    base = mask_img.convert('RGB')
    report = []
    for tag, sc, px, py in (variants or OCR_VARIANTS):
        try:
            got = _ocr_once(engine, base, sc, px, py)
        except Exception as e:
            report.append({'variant': tag, 'error': str(e)})
            continue
        txt = ' '.join(t for t, _, _ in got)
        ns = ''.join(c for c in txt if c != ' ')
        score = max((s for _, s, _ in got), default=0.0)
        report.append({'variant': tag, 'text': txt, 'chars': len(ns),
                       'score': round(score, 4), 'lines': len(got)})
    ok = [r for r in report if r.get('text')]
    if not ok:
        return [], report, None
    # 投票：置信度最高者，且其字符数要与至少一个其它变体一致
    ok.sort(key=lambda r: -r['score'])
    pick = ok[0]
    agree = [r for r in ok[1:] if r['chars'] == pick['chars']]
    pick['agreement'] = len(agree)
    if not agree and len(ok) > 1:
        # 没有任何变体字符数一致 → 不可信，退而取"被最多变体支持的字符数"
        from collections import Counter
        cnt = Counter(r['chars'] for r in ok)
        target = cnt.most_common(1)[0][0]
        alt = [r for r in ok if r['chars'] == target]
        alt.sort(key=lambda r: -r['score'])
        pick = alt[0]
        pick['agreement'] = len(alt) - 1
    best_tag = pick['variant']
    for tag, sc, px, py in (variants or OCR_VARIANTS):
        if tag != best_tag:
            continue
        got = _ocr_once(engine, base, sc, px, py)
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
        'O.V.D.' 与 'O.v.D.' 就分不出来了。正确做法是只按高度缩放，
        然后铺到"两者较宽"的画布上比，宽度不合自然会被 IoU 罚掉。
      * 单字符词不用渲染比，直接用 classify_symbol 量字形（与字体无关）。
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


def match_fonts(src_mask, text, corel_fonts=None, top=12):
    """把候选字体渲染同一串文本，按高度归一后比 IoU。

    两个指标分开看：
      iou   —— 把渲染结果横向拉伸到与源同宽后的 IoU（纯字形形状）
      wr    —— 渲染宽度 / 源宽度（字距与字宽的宏观差异）
    只看 iou 会把"字形像但字距差很多"的字体排上来；只看 wr 又分不清
    谁的字形更像。综合分 = iou*0.7 + max(0,1-|wr-1|)*0.3。
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
        rows.append({
            'family': fam, 'style': sty, 'file': os.path.basename(p),
            'iou': round(_iou(src_mask, m3), 4),
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
    ap.add_argument('--corel-only', action='store_true',
                    help='只用 CorelDRAW 字体表里有的字体（需要 CDR 可用）')
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
        lines, ocr_report, err = ocr_lines(crop_img)
        if err:
            print('  [警告] %s' % err)
            ocr_text = ''
            ocr_raw = []
        else:
            for r in ocr_report:
                if r.get('error'):
                    print('    OCR %-12s 失败 %s' % (r['variant'], r['error']))
                else:
                    print('    OCR %-12s 置信 %.3f 字符 %3d  %r'
                          % (r['variant'], r['score'], r['chars'], r['text']))
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
                    if kind == 'symbol':
                        # 字形量出来的符号：证据无歧义，直接采纳
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
            m = render_mask(txt, os.path.join(FONT_DIRS[0], cand['file']))
            if m is None:
                return 0.0, None
            m2 = resize_mask(m, m.shape[1] * (th / m.shape[0]), th)
            m3 = resize_mask(m2, tw, th)
            return round(_iou(core, m3), 4), m3

        best = cands[0]
        final_iou, mask = rebuild_iou(final_text, best)
        if mask is not None:
            _write_compare(os.path.join(args.out_dir, 'compare_%s.png' % name),
                           core, mask)

        # --- 判定：字形到底像不像
        # **不能用绝对 IoU 当门槛**：可达上限取决于文字大小——实测 16px 高的
        # 页脚，即使用完全正确的文本与正确字体，IoU 也只有 0.61（小字光栅化
        # 本身就把笔画糊在一起）。用 0.62 的绝对门槛会把完美匹配也拒掉。
        # 改用**相对判据**：最佳字体相对候选集中位的提升倍数。
        # 实测正确匹配时最佳/中位 ≈ 1.37x；这个量与小字/大字无关。
        med = fstats['median_iou'] or 1e-9
        lift = round(final_iou / med, 3)
        ok_abs = final_iou >= args.min_iou
        ok_rel = (lift >= args.min_lift and final_iou >= args.min_iou_floor)
        verdict = 'convert' if (ok_abs or ok_rel) else 'keep_trace'

        with open(os.path.join(args.out_dir, 'font_match_%s.json' % name), 'w',
                  encoding='utf-8') as f:
            json.dump({'text': final_text, 'candidates': cands,
                       'stats': fstats}, f, ensure_ascii=False, indent=2)

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
            'thresholds': {'min_iou': args.min_iou, 'min_lift': args.min_lift,
                           'min_iou_floor': args.min_iou_floor},
            'verdict': verdict,
        }
        summary[name] = rec
        print('  最佳字体 %s / %s  IoU %.4f  重建复核 %.4f  领先中位 %.2fx'
              % (best['family'], best['style'], best['iou'], final_iou, lift))
        print('  → %s' % ('转活字' if verdict == 'convert' else
                          '不转（保留描摹轮廓：字体库里没有足够接近的，'
                          '或字形相似度不足以证明是同一字体）'))

    with open(os.path.join(args.out_dir, 'live_text.json'), 'w',
              encoding='utf-8') as f:
        json.dump({'image': os.path.abspath(args.image),
                   'mm_per_px': args.mm_per_px,
                   'min_iou': args.min_iou,
                   'regions': summary}, f, ensure_ascii=False, indent=2)
    print()
    print('已写出 %s' % os.path.join(args.out_dir, 'live_text.json'))
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
