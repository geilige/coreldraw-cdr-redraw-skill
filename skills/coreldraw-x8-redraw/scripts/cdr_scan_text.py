"""整图识别：先把位图里的**文字**与**符号**认出来，再决定怎么画。

这是重绘流程的**第一步**，顺序不能反。反过来（先整幅描摹、事后补活字）
有两个后果，都是实测踩到的：

  * 文字被当成图形描线，产物里没有可编辑文本；
  * **倒置的文字在正向 OCR 里整块漏检**。本图（daiion 拼版稿）上排整体
    180° 倒置，0° 那一遍里 4 处倒置文字一条都没检出，看着像"这里没有文字"，
    于是被当装饰图形描摹。180° 那一遍才全部出来。

所以识别必须 **两个方向都跑**，再按"只在 180° 出现"判定哪些是倒置的。

输出：

  `scan.json`        可复核的清单（文字项 + 符号/图形块）
  `scan_preview.png` 画框预览，用来一眼核对识别对不对

用法：

    python cdr_scan_text.py --image 稿子.png --out-dir work/scan
    python cdr_scan_text.py --image 稿子.png --out-dir work/scan \
        --palette '#C62F7C,#1A1819' --mm-per-px 0.17485
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

try:
    import cv2
except ImportError:                                   # pragma: no cover
    cv2 = None


# --------------------------------------------------------------------------
# 调色板
# --------------------------------------------------------------------------

def _hex_to_rgb(s):
    s = s.lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def parse_palette(spec):
    if not spec:
        return []
    return [_hex_to_rgb(t) for t in spec.split(',') if t.strip()]


def detect_palette(rgb, max_colors=6, min_frac=0.01, cos_tol=0.995,
                   min_t_frac=0.05, min_count=15):
    """自动提取墨色。返回 `(palette, background)`。

    **核心观察：抗锯齿像素 = 背景色 与 某个墨色 的线性混合。**
    混合只改变长度、不改变方向，所以同一个墨色的**全部**抗锯齿像素，
    其"相对背景的方向" `(p − 背景) / |p − 背景|` 是**同一个单位向量**；
    不同墨色的方向不同（本图洋红 ≈ (−0.23,−0.81,−0.51)、
    黑 ≈ (−0.58,−0.58,−0.58)，夹角 26°）。

    于是"找墨色"变成"**在方向上找峰**"：

    1. 背景取出现最多的**精确色**（本图 `#FFFDFE`；用粗量化格中心会报出
       `#F0F0F0` 这种图上不存在的颜色）；
    2. 按方向聚类（夹角余弦 > `cos_tol`）；
    3. 每个方向簇里**离背景最远**的那个颜色就是墨色本身，簇内像素总数
       就是它的分量；分量低于 `min_frac` 的当噪声丢掉。

    这条路绕开了两个坑，都实测踩过：

    * 按**颜色半径**聚类，会把同一条抗锯齿斜坡切成
      `#C62F7C / #B63B7A / #BD6A94 / #CF6EA4 / #DF89B6` 五个假色，
      连背景都被误报成墨色之一；
    * 按**出现次数最多**挑，会挑到斜坡中段的混色——细笔画文字里
      纯墨色像素（本图黑 `#1A1819` 只有 587 个）反而比混色少得多。
    """
    px = rgb.reshape(-1, 3)
    vv, cc = np.unique(px, axis=0, return_counts=True)
    bg = vv[cc.argmax()].astype(np.float32)
    a = vv.astype(np.float32)
    rel = a - bg
    t = np.linalg.norm(rel, axis=1)
    tmax = float(t.max())
    if tmax <= 0:
        return [], tuple(int(v) for v in bg)

    idx = np.where(t >= min_t_frac * tmax)[0]
    idx = idx[cc[idx] >= min_count]          # 丢掉单像素噪点
    if not len(idx):
        return [], tuple(int(v) for v in bg)
    dirs = rel[idx] / t[idx][:, None]
    counts = cc[idx].astype(np.int64)
    total = int(counts.sum())

    pal = []
    alive = np.ones(len(idx), bool)
    for oi in np.argsort(-t[idx]):
        if not alive[oi] or len(pal) >= max_colors:
            continue
        grp = alive & (dirs @ dirs[oi] > cos_tol)
        mass = int(counts[grp].sum())
        if mass < max(1, int(total * min_frac)):
            alive[oi] = False
            continue
        # 代表色取簇内**高覆盖度像素的众数**，不取最远的那个。
        # 最远的那个会被重采样过冲 / 噪点带偏：实测洋红报成 `#BF2B75`、
        # 黑报成 `#161415`，而真值是 `#C62F7C` / `#1A1819`。
        gi = np.where(grp)[0]
        tt = t[idx][gi]
        hi = gi[tt >= 0.85 * float(tt.max())]
        pal.append(tuple(int(v) for v in a[idx[hi[counts[hi].argmax()]]]))
        alive &= ~grp
    return pal, tuple(int(v) for v in bg)


# --------------------------------------------------------------------------
# 按色分离墨迹
# --------------------------------------------------------------------------

def color_space(rgb, palette, bg):
    """按**相对背景的方向**把每个像素归到调色板。返回 `(label, cov)`。

    `label[i]` ∈ [0, len(palette)) 是所属墨色；`cov[i]` 是该像素的
    "覆盖率" = 它离背景的距离 ÷ 该墨色离背景的距离，1.0 表示纯墨色。

    **为什么不用欧氏最近色**：抗锯齿像素是 背景 与某墨色 的**线性混合**，
    混合只改变长度、不改变方向，所以按方向判定对混叠像素是**精确**的。
    欧氏最近色会整片判错——实测黑的抗锯齿灰 `#808080` 到洋红 `#BF2B75`
    的距离（99）比到黑 `#161415`（184）**更近**，于是黑字/黑图标的灰边
    整圈被算成洋红，每个图标位置都凭空多出一份"假洋红"块。
    """
    shape = rgb.shape[:2]
    px = rgb.reshape(-1, 3).astype(np.float32)
    b = np.asarray(bg, np.float32)
    rel = px - b
    t = np.linalg.norm(rel, axis=1)
    pal = [np.asarray(c, np.float32) for c in palette]
    tc = np.array([float(np.linalg.norm(c - b)) for c in pal], np.float32)
    dirs = np.stack([(c - b) / max(1e-6, float(np.linalg.norm(c - b)))
                     for c in pal])
    cos = np.zeros((len(pal), len(px)), np.float32)
    safe = t > 1e-6
    cos[:, safe] = (dirs @ rel[safe].T) / t[safe]
    best = cos.argmax(axis=0).astype(np.int32)
    cov = t / np.maximum(1e-6, tc[best])
    return best.reshape(shape), cov.reshape(shape)


def ink_gray(rgb, label, i, bg_gray=255):
    """只把第 i 号墨色的像素留下，其余置背景白的灰度图（给 OCR 用）。

    这里**不设覆盖率门槛**：OCR 自己会扫多档二值化，喂它原始灰度更灵活；
    而且方向判定对混叠像素是精确的，浅色像素本来就是"该色的浅色"，
    置成白反而会削细笔画。
    """
    g = np.asarray(Image.fromarray(rgb).convert('L'))
    return np.where(label == i, g, bg_gray).astype(np.uint8)


def ink_mask(label, cov, i, min_cov=0.5):
    """第 i 号墨色的二值掩膜。覆盖率过半才算墨迹（= 视觉上的笔画边界）。"""
    return (label == i) & (cov >= min_cov)


# --------------------------------------------------------------------------
# OCR（两个方向）
# --------------------------------------------------------------------------

def _ocr(engine, arr):
    res, _ = engine(arr)
    out = []
    for box, txt, score in (res or []):
        pts = [[float(p[0]), float(p[1])] for p in box]
        out.append({'text': str(txt), 'score': float(score), 'box': pts})
    return out


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _flip_box(box, w, h):
    """180° 旋转后的框映射回原图坐标。"""
    x0, y0, x1, y1 = box
    return [w - x1, h - y1, w - x0, h - y0]


def _iou_box(a, b):
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def ocr_both_ways(engine, arr, label=''):
    """0° 与 180° 各跑一遍，合并。

    **合并规则**：0° 的结果当基准；180° 的结果里，凡是与基准有重叠
    （IoU > 0.3）的都丢掉，剩下的才收进来并标 `rot=180`。

    为什么这样定：实测**正置文字在两个方向都能读对**（RapidOCR 对正置
    文字不敏感），而**倒置文字只有 180° 那遍能检出**。所以"只在 180°
    出现"才是倒置的充分证据；反过来若按置信度选方向，正置文字会有一半
    概率被判成倒置（实测 `www.daiion.com` 两遍分数 0.905 / 0.907，
    基本是抛硬币）。
    """
    h, w = arr.shape[:2]
    base = []
    for d in _ocr(engine, arr):
        b = _bbox(d['box'])
        base.append({'text': d['text'].replace(' ', ''), 'score': d['score'],
                     'box': [round(v, 2) for v in b], 'rot': 0, 'pass': label})
    # 基准内部也去重（同一处被检两次）
    dedup = []
    for d in sorted(base, key=lambda r: -r['score']):
        if any(_iou_box(d['box'], e['box']) > 0.5 for e in dedup):
            continue
        dedup.append(d)

    rot_img = np.asarray(Image.fromarray(arr).rotate(180, expand=True))
    for d in _ocr(engine, rot_img):
        b = _flip_box(_bbox(d['box']), w, h)
        if any(_iou_box(b, e['box']) > 0.3 for e in dedup):
            continue
        dedup.append({'text': d['text'].replace(' ', ''), 'score': d['score'],
                      'box': [round(v, 2) for v in b], 'rot': 180,
                      'pass': label})
    return dedup


# --------------------------------------------------------------------------
# 符号 / 图形块
# --------------------------------------------------------------------------

def snap_box_to_ink(m, box, max_grow=14):
    """把 OCR 的紧框扩到**与它相连的墨迹**的边界。

    OCR 的框只包住字形芯部：点、撇、细笔画都可能落在框外。实测本图那个
    倒置字标，两个 `i` 点整体在框外 4px —— 切掉的后果不是"少画一点"，
    而是活字建出来缺字、或描摹区漏元素，两种都很难在成品上肉眼发现。

    做法：在框外扩 `max_grow` 的窗内取连通分量，**只保留与框相交的那些**，
    取它们的并集包围盒。用"连通"而不是"有墨迹就往外长"，是为了不把紧邻
    的另一个元素吞进来 —— 实测 `www.daiion.com` 的框离下面的框线只有 5px，
    按"有墨迹就长"会把它整条框线并进来。

    只吸附一次（不迭代）：一轮就够——OCR 的框本来就贴着字形，缺的只是
    同一连通域里伸出去的那部分。
    """
    if cv2 is None:
        return [int(round(v)) for v in box]
    h, w = m.shape
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    wx0, wy0 = max(0, x0 - max_grow), max(0, y0 - max_grow)
    wx1, wy1 = min(w, x1 + max_grow), min(h, y1 + max_grow)
    if wx1 <= wx0 or wy1 <= wy0:
        return [x0, y0, x1, y1]
    win = m[wy0:wy1, wx0:wx1]
    if not win.any():
        return [x0, y0, x1, y1]
    n, lab = cv2.connectedComponents(win.astype(np.uint8), 8)
    ix0, iy0, ix1, iy1 = x0 - wx0, y0 - wy0, x1 - wx0, y1 - wy0
    keep = np.zeros(win.shape, bool)
    for i in range(1, n):
        ys, xs = np.where(lab == i)
        if not ys.size:
            continue
        if (ys.max() >= iy0 - 1 and ys.min() <= iy1
                and xs.max() >= ix0 - 1 and xs.min() <= ix1):
            keep |= (lab == i)
    if not keep.any():
        return [x0, y0, x1, y1]
    ys, xs = np.where(keep)
    return [int(wx0 + xs.min()), int(wy0 + ys.min()),
            int(wx0 + xs.max() + 1), int(wy0 + ys.max() + 1)]


def _blocks(mask, gap):
    """把掩膜按"膨胀后连通"聚成块，包围盒与掩膜都取**原始掩膜**的范围。

    注意 `mask` 必须是**紧包围盒**的那一块：曾经返回膨胀后的整块矩形
    （`mask[y:y+h, x:x+w]` 用膨胀分量的框），而 `box` 用的是紧框，
    于是 `fill = ink/(紧框面积)` 的分母和分子对不上，实测把一圈细边框
    的填墨率算成 0.928（真实 0.086），块分类跟着全错。
    """
    if cv2 is None:
        raise RuntimeError('需要 opencv（cv2）做块聚类')
    k = np.ones((gap * 2 + 1, gap * 2 + 1), np.uint8)
    dil = cv2.dilate(mask.astype(np.uint8), k, iterations=1)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(dil, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        sub = mask[y:y + h, x:x + w]
        if not sub.any():
            continue
        ys, xs = np.where(sub)
        bx0, by0 = x + int(xs.min()), y + int(ys.min())
        bx1, by1 = x + int(xs.max()) + 1, y + int(ys.max()) + 1
        out.append({'box': [bx0, by0, bx1, by1],
                    'mask': mask[by0:by1, bx0:bx1]})
    return out


def block_features(b):
    """块的特征：面积、填墨率、笔画宽度、连通分量数。"""
    m = b['mask']
    x0, y0, x1, y1 = b['box']
    w, h = x1 - x0, y1 - y0
    ink = int(m.sum())
    area = max(1, w * h)
    feat = {'area_px': area, 'ink_px': ink, 'fill': round(ink / area, 4),
            'w_px': w, 'h_px': h,
            'aspect': round(w / max(1, h), 3)}
    if cv2 is not None and ink:
        n, _ = cv2.connectedComponents(m.astype(np.uint8), 8)
        feat['n_comp'] = int(n - 1)
        dt = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 3)
        # 最粗处的**厚度** = 最大内切圆直径。细线稿上是笔画宽，实心块上
        # 就是块的内径——所以字段叫 `thick_px` 而不是 `stroke_px`：
        # 拿它当"笔画宽"去判实心块会得出 400 多这种荒唐值。
        feat['thick_px'] = round(float(dt.max()) * 2, 2)
    else:
        feat['n_comp'] = None
        feat['thick_px'] = None
    return feat


# --------------------------------------------------------------------------
# 方向判定：用字形证据复核 OCR
# --------------------------------------------------------------------------

def orient_fonts(limit=6):
    """挑几款常见无衬线字体当方向判定的参照，够用且便宜。"""
    try:
        import cdr_text_live as L
    except ImportError:
        return []
    want = ['Arial', 'Swis721 BT', 'Verdana', 'Tahoma', 'Segoe UI',
            'Franklin Gothic Medium', 'Trebuchet MS', 'Calibri']
    out, got = [], set()
    for p in L.font_files():
        fam, _ = L.family_of(p)
        if fam in want and fam not in got:
            got.add(fam)
            out.append(p)
        if len(out) >= limit:
            break
    return out


def orientation_votes(mask, text, fonts, margin=0.02):
    """用**字形证据**定方向。返回 `(iou0, iou180, 判定角度)`。

    **为什么不能只靠 OCR**：实测长文本在两个方向都能读对——`www.daiion.com`
    与 `daiion` 正置倒置都被 0° 那遍读出来，只有 2 个字符的 `4#` 在 0°
    那遍整块漏检。于是"只在 180° 出现才算倒置"这条规则只对短文本有效，
    本图 4 处长文本会被误标成正置（实测全错）。

    字形证据没有这个问题：把候选文本按正确方向渲染，与"转正后的掩膜"
    比 IoU 必然高于与"未转正的掩膜"比——翻转会颠倒上伸部 / 基线的不对称。
    两者差距小于 `margin` 时不表态（返回 0 度），交给调用方保留原判。
    """
    if not fonts or mask is None or not mask.any():
        return 0.0, 0.0, 0
    try:
        import cdr_text_live as L
    except ImportError:
        return 0.0, 0.0, 0
    h, w = mask.shape
    m180 = mask[::-1, ::-1]
    b0 = b180 = 0.0
    for f in fonts:
        m = L.render_mask(text, f)
        if m is None:
            continue
        m2 = L.resize_mask(m, m.shape[1] * (h / m.shape[0]), h)
        m3 = L.resize_mask(m2, w, h)
        b0 = max(b0, L._iou(mask, m3))
        b180 = max(b180, L._iou(m180, m3))
    b0, b180 = round(b0, 4), round(b180, 4)
    if abs(b0 - b180) < margin:
        return b0, b180, 0
    return b0, b180, 0 if b0 > b180 else 180


def classify_block(feat):
    """粗分：实心块 / 线稿 / 图标（符号）。阈值是经验值，预览图负责兜底。"""
    fill, thick = feat['fill'], feat['thick_px'] or 0
    area, n = feat['area_px'], feat['n_comp'] or 0
    if fill >= 0.85 and area >= 2000:
        return 'solid'
    if thick <= 3.5 and fill <= 0.35 and max(feat['w_px'], feat['h_px']) >= 40:
        return 'line_art'
    if area <= 9000 and 1 <= n <= 24:
        return 'icon'
    return 'mixed'


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _jsonable(o):
    """numpy 标量/数组 -> 原生类型（`json` 不认 `np.int32`）。"""
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError('不能序列化的类型 %r' % type(o))


def main(argv=None):
    ap = argparse.ArgumentParser(description='整图识别文字与符号（重绘第一步）')
    ap.add_argument('--image', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--palette', default=None,
                    help='逗号分隔的墨色，如 "#C62F7C,#1A1819"；不给则自动提取')
    ap.add_argument('--mm-per-px', type=float, default=None,
                    help='标定比例，给了就同时报毫米尺寸')
    ap.add_argument('--group-gap', type=int, default=8,
                    help='块聚类的膨胀半径（px），默认 8')
    ap.add_argument('--min-score', type=float, default=0.0,
                    help='低于该置信度的 OCR 结果丢弃，默认 0（全留）')
    ap.add_argument('--no-orient', action='store_true',
                    help='跳过字形方向复核（只信 OCR 的"只在 180° 出现"规则）')
    ap.add_argument('--min-block-ink', type=int, default=24,
                    help='非文字块的最小墨迹像素数，默认 24（滤掉零星噪点）')
    ap.add_argument('--min-text-iou', type=float, default=0.30,
                    help='字形相似度低于此值的条目标记为 suspect（疑似线稿被误读），'
                         '默认 0.30；只标记不删除')
    ap.add_argument('--emit-live', default=None,
                    help='把文字区写成 cdr_text_live.py --region 的行，'
                         '省掉手工抄坐标；suspect 项默认不输出')
    ap.add_argument('--live-pad', type=int, default=2,
                    help='--emit-live 输出时把框外扩的像素数，默认 2。'
                         'OCR 框已经贴着字形，下游紧裁会切掉笔画——'
                         '实测 2.27mm 的 `4#` 因此 11 套变体全读空')
    ap.add_argument('--emit-trace', default=None,
                    help='把**非文字块**写成 cdr_bitmap_to_cdr.py --region 的行。'
                         '文字不在这里，它由活字路径负责——这正是'
                         '"文字被描线"那个问题的根源')
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    rgb = np.asarray(Image.open(args.image).convert('RGB'))
    h, w = rgb.shape[:2]
    print('图 %d x %d px' % (w, h))

    pal = parse_palette(args.palette)
    if pal:
        px = rgb.reshape(-1, 3)
        vv, cc = np.unique(px, axis=0, return_counts=True)
        bg = tuple(int(v) for v in vv[cc.argmax()])
        print('调色板（给定）%s  背景 #%02X%02X%02X'
              % (', '.join('#%02X%02X%02X' % c for c in pal), *bg))
    else:
        pal, bg = detect_palette(rgb)
        print('调色板（自动）%s  背景 #%02X%02X%02X'
              % (', '.join('#%02X%02X%02X' % c for c in pal), *bg))

    label, cov = color_space(rgb, pal, bg)
    all_ink = np.zeros((h, w), bool)
    for i in range(len(pal)):
        all_ink |= ink_mask(label, cov, i)

    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:
        print('缺少 OCR 引擎：%s' % e)
        print('  pip install --no-deps rapidocr-onnxruntime')
        print('  pip install onnxruntime pyclipper shapely six flatbuffers protobuf pyyaml')
        return 2
    engine = RapidOCR()

    # --- 文字：整图灰度 + 每个颜色单独一遍，两个方向都跑
    texts = []
    gray = np.asarray(Image.open(args.image).convert('L'))
    texts += ocr_both_ways(engine, gray, 'gray')
    for i, c in enumerate(pal):
        g = ink_gray(rgb, label, i)
        texts += ocr_both_ways(engine, g, '#%02X%02X%02X' % c)

    # 跨颜色合并去重：同处只留置信度最高的那条
    merged = []
    for d in sorted(texts, key=lambda r: -r['score']):
        if d['score'] < args.min_score:
            continue
        hit = None
        for e in merged:
            if _iou_box(d['box'], e['box']) > 0.5:
                hit = e
                break
        if hit is None:
            merged.append(d)
        else:
            hit.setdefault('also', []).append(
                {'pass': d['pass'], 'text': d['text'], 'score': d['score']})
    # 框吸附到墨迹：OCR 的框只包住**字形芯部**，点、撇、细笔画常被切在框外。
    # 实测本图那个倒置字标，两个 `i` 点落在框外 4px；不吸附的话后果是
    # "活字建出来缺字"或"描摹区漏元素"，两种都很难在成品上肉眼发现。
    for d in merged:
        d['box_ocr'] = [int(round(v)) for v in d['box']]
        d['box'] = snap_box_to_ink(all_ink, d['box'])

    for d in merged:
        d['kind'] = 'text' if any(ch.isalnum() for ch in d['text']) else 'symbol'
        # 归属色按**该框内的墨迹**判，不能信 OCR 那一遍的名字：同一个字在
        # "整图灰度"那遍也会被检出来，那条的 `pass` 是 `'gray'`，拿它当颜色
        # 会得到一个图上不存在的色号。
        bx0, by0, bx1, by1 = [int(round(v)) for v in d['box']]
        bx0, by0 = max(0, bx0), max(0, by0)
        bx1, by1 = min(w, bx1 + 1), min(h, by1 + 1)
        d['color'] = '#%02X%02X%02X' % pal[0] if pal else '#000000'
        best = -1
        for i, c in enumerate(pal):
            n = int(ink_mask(label, cov, i)[by0:by1, bx0:bx1].sum())
            if n > best:
                best, d['color'] = n, '#%02X%02X%02X' % c
        if args.mm_per_px:
            x0, y0, x1, y1 = d['box']
            d['size_mm'] = [round((x1 - x0) * args.mm_per_px, 3),
                            round((y1 - y0) * args.mm_per_px, 3)]

    # --- 方向复核：OCR 对长文本方向不敏感，必须用字形证据再判一次
    fonts = orient_fonts()
    if fonts and not args.no_orient:
        for d in merged:
            x0, y0, x1, y1 = [int(round(v)) for v in d['box']]
            pad = 2
            x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
            x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
            m = all_ink[y0:y1, x0:x1]
            if not m.any():
                d['orient'] = {'iou0': 0.0, 'iou180': 0.0,
                               'deg': d['rot'], 'ocr_rot': d['rot']}
                continue
            ys, xs = np.where(m)
            m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            i0, i180, deg = orientation_votes(m, d['text'], fonts)
            d['orient'] = {'iou0': i0, 'iou180': i180, 'deg': deg,
                           'ocr_rot': d['rot']}
            # 字形证据只在**有明确倾向**时才改判；两者接近说明这个字符串
            # 本身左右对称性太强，改了反而引入错误。
            if deg and deg != d['rot']:
                d['rot'] = deg
                d['rot_source'] = 'glyph'
            else:
                d['rot_source'] = 'ocr'
            # 字形相似度过低 => 这多半不是文字，是线稿被 OCR 误读成字符。
            # 实测瓶身的线稿被读成 `ft`（最佳 IoU 0.263），而真文字最低也有
            # 0.435。**只标记不删除**：删掉等于静默丢信息，标记出来让人复核。
            d['suspect'] = max(i0, i180) < args.min_text_iou
    merged.sort(key=lambda r: (r['box'][1], r['box'][0]))

    # --- 符号 / 图形：把文字框涂掉，剩下的墨迹聚类
    #
    # `suspect` 项**不涂**：它是 OCR 在线稿上凑出来的假文字（本图那个 `ft`），
    # 既然不建活字，那块墨迹就必须留给描摹。涂掉的后果实测很隐蔽——
    # `ft` 的框吸附后把运输图标的中段吃掉，图标被切成 19x33 与 27x20 两块，
    # 而中段两头都不管，成品上就少一块图形。
    cover = all_ink.copy()
    for d in merged:
        if d.get('suspect'):
            continue
        x0, y0, x1, y1 = [int(round(v)) for v in d['box']]
        pad = 3
        cover[max(0, y0 - pad):min(h, y1 + pad),
              max(0, x0 - pad):min(w, x1 + pad)] = False

    blocks = []
    for i, c in enumerate(pal):
        m = ink_mask(label, cov, i) & cover
        if not m.any():
            continue
        for b in _blocks(m, args.group_gap):
            f = block_features(b)
            if f['ink_px'] < args.min_block_ink:
                continue
            f['kind'] = classify_block(f)
            f['color'] = '#%02X%02X%02X' % c
            f['box'] = b['box']
            if args.mm_per_px:
                f['size_mm'] = [round(f['w_px'] * args.mm_per_px, 3),
                                round(f['h_px'] * args.mm_per_px, 3)]
            blocks.append(f)
    blocks.sort(key=lambda r: (-r['area_px']))

    # --- 输出
    scan = {'image': os.path.abspath(args.image), 'size_px': [w, h],
            'mm_per_px': args.mm_per_px,
            'palette': ['#%02X%02X%02X' % c for c in pal],
            'background': '#%02X%02X%02X' % bg,
            'texts': merged, 'blocks': blocks}
    with open(os.path.join(args.out_dir, 'scan.json'), 'w',
              encoding='utf-8') as f:
        json.dump(scan, f, ensure_ascii=False, indent=2, default=_jsonable)

    print()
    print('=== 文字 / 符号 %d 条 ===' % len(merged))
    print('%4s %-6s %5s %5s  %-24s %6s %7s %7s  %-14s %s'
          % ('rot', 'kind', 'x', 'y', 'text', 'score', 'IoU@0', 'IoU@180',
             '尺寸mm', '方向依据'))
    for d in merged:
        x0, y0, x1, y1 = d['box']
        o = d.get('orient', {})
        print('%4d %-6s %5.0f %5.0f  %-24s %6.3f %7.4f %7.4f  %-14s %s%s'
              % (d['rot'], d['kind'], x0, y0, repr(d['text'])[:24], d['score'],
                 o.get('iou0', 0.0), o.get('iou180', 0.0),
                 str(d.get('size_mm', '')), d.get('rot_source', '-'),
                 '   ← suspect（疑似线稿被误读）' if d.get('suspect') else ''))
    print()
    print('=== 非文字块 %d 个 ===' % len(blocks))
    print('%6s %6s %6s %6s  %-8s %6s %7s %6s  %s'
          % ('x', 'y', 'w', 'h', 'kind', 'fill', 'thick', 'ncomp', 'color'))
    for b in blocks:
        x0, y0, x1, y1 = b['box']
        print('%6d %6d %6d %6d  %-8s %6.3f %7s %6s  %s'
              % (x0, y0, b['w_px'], b['h_px'], b['kind'], b['fill'],
                 b['thick_px'], b['n_comp'], b['color']))

    _preview(rgb, merged, blocks, os.path.join(args.out_dir, 'scan_preview.png'))
    print()
    print('清单 %s' % os.path.join(args.out_dir, 'scan.json'))
    print('预览 %s' % os.path.join(args.out_dir, 'scan_preview.png'))

    # --- 把识别结果**直接喂给下游**，免去手工抄坐标
    if args.emit_live:
        rows = emit_live_regions(merged, pad=args.live_pad, size=(w, h))
        with open(args.emit_live, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rows) + '\n')
        print('活字区域清单 %s（%d 条，供 cdr_text_live.py --region 用）'
              % (args.emit_live, len(rows)))
    if args.emit_trace:
        rows = emit_trace_regions(blocks, args.mm_per_px)
        with open(args.emit_trace, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rows) + '\n')
        print('描摹区域清单 %s（%d 条，供 cdr_bitmap_to_cdr.py --region 用）'
              % (args.emit_trace, len(rows)))
    return 0


def _safe_name(text, box, used):
    """给区域取一个可读且唯一的图层名（图层名会显示在 CorelDRAW 里）。"""
    keep = [c for c in str(text) if c.isalnum()]
    stem = ''.join(keep)[:12] or 'x'
    name = '%s_%d_%d' % (stem, int(box[0]), int(box[1]))
    n, base = 2, name
    while name in used:
        name = '%s_%d' % (base, n)
        n += 1
    used.add(name)
    return name


def emit_live_regions(texts, include_suspect=False, pad=2, size=None):
    """产出 `cdr_text_live.py --region` 的行：`NAME=y0,y1,x0,x1[,#COLOR][,rot=R]`。

    注意两个脚本的格式**故意不同**（一个 y 在前、用 `=`；一个 x 在前、用 `:`），
    这里各按各的来，不要"顺手统一"，否则两个 CLI 都得改。

    `suspect` 项默认**不输出**：它是 OCR 在线稿上凑出来的假文字（本图那个
    `ft` 就是图标被读错），拿去建活字会凭空多一行字。

    `pad` 是必须的：下游 `cdr_text_live.py` 会先 `tight_text_box` 紧裁再识别，
    而这里的框是 OCR 直接给的、已经贴着字形。实测 2.27mm 高的 `4#`，
    OCR 框比真字形紧 1px，紧裁后笔画被切掉一角，**11 套变体全部读空**，
    于是这块被判成"没有文字"而漏掉。外扩几像素对下游无害——
    紧裁会把多余的白边切回去。
    """
    used, out = set(), []
    for t in texts:
        if t.get('suspect') and not include_suspect:
            continue
        x0, y0, x1, y1 = [int(round(v)) for v in t['box']]
        if size is not None:
            w, h = int(size[0]), int(size[1])
            x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
            x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
        name = _safe_name(t['text'], (x0, y0), used)
        row = '%s=%d,%d,%d,%d,%s' % (name, y0, y1, x0, x1, t['color'])
        if int(t.get('rot', 0)) % 360:
            row += ',rot=%d' % (int(t['rot']) % 360)
        out.append(row)
    return out


def emit_trace_regions(blocks, mm_per_px=None):
    """产出 `cdr_bitmap_to_cdr.py --region` 的行：`name:x0,y0,x1,y1,#RRGGBB`。

    这里只输出**非文字块**——文字已经由活字路径负责，再描一遍就是
    "文字被描线"那个问题的来源。
    """
    used, out = set(), []
    for b in blocks:
        x0, y0, x1, y1 = [int(round(v)) for v in b['box']]
        name = _safe_name(b['kind'], (x0, y0), used)
        out.append('%s:%d,%d,%d,%d,%s' % (name, x0, y0, x1, y1, b['color']))
    return out


def _preview(rgb, texts, blocks, path):
    im = Image.fromarray(rgb).convert('RGB')
    d = ImageDraw.Draw(im)
    colors = {'solid': (0, 150, 60), 'line_art': (130, 130, 130),
              'icon': (220, 60, 60), 'mixed': (200, 140, 0)}
    for b in blocks:
        x0, y0, x1, y1 = b['box']
        c = colors.get(b['kind'], (120, 120, 120))
        d.rectangle([x0, y0, x1, y1], outline=c, width=2)
        d.text((x0 + 3, y0 + 2), '%s %.2f' % (b['kind'], b['fill']), fill=c)
    for t in texts:
        x0, y0, x1, y1 = [int(round(v)) for v in t['box']]
        if t.get('suspect'):
            c = (150, 150, 150)
        else:
            c = (30, 90, 230) if t['rot'] == 0 else (255, 120, 0)
        d.rectangle([x0, y0, x1, y1], outline=c, width=3)
        tag = t['text'] + ('' if t['rot'] == 0 else ' (rot180)')
        if t.get('suspect'):
            tag = '? ' + tag
        d.text((x0 + 3, y1 + 2), tag, fill=c)
    im.save(path)


if __name__ == '__main__':
    sys.exit(main())
