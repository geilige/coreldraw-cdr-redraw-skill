# 位图文字 → CorelDRAW 活字：设计与产出

主流程把文字当图形描摹（`cdr_bitmap_to_cdr.py` 的第 5 区）。视觉上能看，
但**不可编辑**、节点数还多——实测 vonder 页脚那行 6pt 文字，轮廓描摹出
**73 子路径 / 840 段**，而且用户想改一个字就得重画。

这份文档记录"把文字识别出来、用最接近的字体重建成真文本"这条路的
完整设计与实测结论。脚本：`scripts/cdr_text_live.py`。

> **核心风险**：**字体猜错比描摹失真更糟**。描摹失真顶多是"不够像"，
> 字体猜错会让整行字的字形结构都不对，而且用户可能不会逐字检查。
> 所以本设计的重点不是"识别得多准"，而是**每一步都算分，分数不够就拒绝转换**。

---

## 1. 流水线

```
紧裁文字区 → 多套预处理 OCR 投票 → 列投影切词 → 字形级纠错（形状裁决）
   → 字体匹配（全系统字体逐个渲染比 IoU）→ 判定可否转换
   → 解字号 + 横向缩放 → 在 CDR 里建美术字 → 渲染回位图复核
```

### 1.1 为什么必须"紧裁"

OCR 的检测器对**纵向留白**极敏感。实测页脚那条文字：

| 预处理 | 结果 |
| --- | --- |
| 原图（含大片上下留白） | 一个框都检测不到 |
| 紧裁后 1× + 20px 四周留白 | 检测不到 |
| 紧裁后 2× + 20px 四周留白 | 检测到，但**乱码** `'mporaraeDisibdra Ldaurti'` |
| 紧裁后 2× + **纵向 0 留白** | ✅ `'O.v.D. Importadora e Distribuidora Ltda. - Curitiba - PR'` 0.879 |
| 紧裁后 4× + 20px 四周留白 | ✅ 同上，0.917 |

所以**先紧裁，再上采样，纵向留白要小**。

### 1.2 为什么是"多套预处理投票"而不是一套参数

上表已经说明：同一张图，换个留白就从"正确"变成"乱码"。**没有一套参数对所有图都稳。**
所以脚本跑 5 套变体（`OCR_VARIANTS`），取置信度最高者，但要求它的**字符数**
与至少一个其它变体一致；都不一致时取"被最多变体支持的字符数"里置信度最高的那个。

实测同一张页脚图的 5 套变体：

```
1x_宽留白     置信 0.000 字符   0  ''                              ← 完全失效
2x_无纵留白   置信 0.879 字符  48  'O.v.D. Importadora e Distribuidora Ltda. - Curitiba - PR'
2x_微纵留白   置信 0.908 字符  48  'O.v.D. Importadora e Distribuidora Ltda. - Curitiba - PR'
3x_20留白     置信 0.859 字符  47  'O.v.D. Importadora e Distribuidora Ltda.  Curitiba -PR'
4x_20留白     置信 0.917 字符  48  'O.v.D. Importadora e Distribuidora Ltda. - Curitiba - PR'
```

注意 `1x_宽留白` 这套在这一张图上直接失效——**如果只配一套参数，就有一类图会
整行识别不出来**，所以投票是必要的，不是过度设计。

### 1.3 词边界：用列投影，不信任 OCR 的空格

OCR 在窄字距上常把空格吃掉（实测另一张图给出
`'O.V.D.ImportadoraeDistribuidoraLtda.·Curitiba-PR'`）。而**列投影是几何证据**：
先找连续的墨迹列段（glyph run），取相邻段之间的空隙宽度，对这批空隙做**一维 Otsu**——
空隙会自然分成"字内"与"词间"两簇，阈值取在两簇之间。比固定阈值稳。

实测页脚：切出 9 个词，与真实分词完全一致。

**但两条证据不一致时不要硬凑**：脚本会核对"列投影词数 == OCR 词数"，
不一致就跳过逐词纠错（宁可少修，不可修错）。

### 1.4 字形级纠错：形状搜索 + 逐词裁决

OCR 在小字上会错大小写与易混符号。实测 `'O.V.D.'` 被认成 `'O.v.D.'`，
`'•'` 被认成 `'-'`。

**不要用"字高阈值"判大小写**——试过，是错的：按"高度 ≥ cap 的 0.86 倍算大写"去判，
把 `Importadora` 里的 `m/r/a/e/s` 全判成大写（16px 高的字里，x-height 与 cap height
只差 3 像素，噪声就把阈值打穿），输出 `'IMpoRtAdora...'`，字体 IoU 从 0.606 掉到 0.36。

**正确做法分两类**：

**(a) 易混符号：直接量字形，与字体无关。** 实测页脚那两个符号的几何差得非常开：

| 符号 | 宽 | 高 | 宽高比 | 墨密度 |
| --- | --- | --- | --- | --- |
| `•` | 5 | 4 | 1.25 | 0.800 |
| `-` | 4 | 1 | **4.00** | 1.000 |

判据只用三个量（宽高比、墨密度、高度 ÷ x-height），证据无歧义 → **直接采纳**。

**(b) 大小写：逐词渲染比形状，但只提建议，由整体 IoU 逐条裁决。**

- 渲染候选时**不要先把宽度拉伸到与源同宽**——那会把字宽差异抹掉，
  `'O.V.D.'` 与 `'O.v.D.'` 就分不出来。正确做法是只按高度缩放，
  然后铺到"两者较宽"的画布上比，宽度不合自然被 IoU 罚掉。
- 只接受**改善超过 margin（默认 0.08）** 的改动。
- **最关键的一条**：逐条试，每次只加一个改动，**整体 IoU 变好才留下**。
  实测曾出现"同一批里 `•` 改对了、但 `Ltda.`/`PR` 被错改成小写"，
  整批采纳会让 IoU 从 0.4654 掉到 0.4181；而**全有全无地回退**又会把改对的
  `•` 一起丢掉。逐词裁决让改对的留下、改错的被否掉，互不牵连。

实测逐词裁决的输出：

```
形状修正建议: Ltda.->ltda.(0.234->0.329); -->•(字形); PR->pr(0.284->0.464)
逐词裁决:     Ltda.->ltda. ✗;  -->• ✅(字形直接采纳);  PR->pr ✗
最终文本: 'O.v.D. Importadora e Distribuidora Ltda. • Curitiba - PR'
```

修正那个 `•` 之后，字体匹配的 IoU 从 **0.5714 跳到 0.7244**——
**一个字符的修正直接把字体识别带到了正确的那一款**。

### 1.5 字体匹配：靠字形，不靠名字

枚举系统字体（`C:\Windows\Fonts`，本机 250 个文件；CorelDRAW X8 自报 846 个可用
字体族），逐个渲染**同一串文本**，按高度等比缩放后与源条带比：

- `iou` —— 横向拉伸到同宽后的 IoU（纯字形形状）
- `width_ratio` —— 渲染宽度 ÷ 源宽度（字距与字宽的宏观差异）
- `score = iou × 0.7 + max(0, 1 - |width_ratio - 1|) × 0.3`

两个指标必须分开看：只看 `iou` 会把"字形像但字距差很多"的排上来；
只看 `width_ratio` 又分不清谁的字形更像。

`--corel-only` 时用 `Application.FontList` 过滤候选，避免推荐出 CorelDRAW 里
根本没有的字体。

实测页脚匹配结果：

| 排名 | 字体 | 样式 | IoU | 宽度比 |
| --- | --- | --- | --- | --- |
| 1 | **Swis721 Cn BT** | **Bold** | **0.7244** | 0.987 |
| 2 | Swis721 Cn BT | Bold Italic | 0.6075 | 1.000 |
| 3 | Square721 Cn BT | Bold | 0.5079 | 0.992 |
| 4 | Geometr706 BlkCn BT | Black | 0.4808 | 0.968 |
| 5 | Square721 Cn BT | Roman | 0.4744 | 0.927 |

Swis721 是 Helvetica 的克隆，`Cn` = Condensed——**源图那种粗窄无衬线，
确实是 Helvetica Bold Condensed 一类的字体**，匹配结果合理。

### 1.6 判定可否转换：**不能用绝对 IoU 门槛**

这是最容易踩的坑。**可达上限取决于文字大小**：实测 16px 高的页脚，
即使用完全正确的文本与正确字体，IoU 也只有 0.61（小字光栅化本身就把笔画糊在一起）。
若设 0.62 的绝对门槛，**完美匹配也会被拒掉**。

改用**相对判据**：

```
lift = 最佳字体 IoU ÷ 候选集中位 IoU        （与小字/大字无关）
可转 = (重建 IoU ≥ --min-iou)            绝对通道，适合大字号
     或 (lift ≥ --min-lift 且 重建 IoU ≥ --min-iou-floor)
```

实测该页脚：最佳 0.7244，候选集中位 0.2716（248 个字体），**lift = 2.67×** → 判定可转。
对比：修正 `•` 之前 lift 只有 2.18×、IoU 0.5714——两个判据都指向"证据更弱"。

默认值：`--min-iou 0.72`、`--min-lift 1.25`、`--min-iou-floor 0.35`。

### 1.7 CDR 侧重建

```python
shp = layer.CreateArtisticText(x_mm, y_mm, text)   # 类型 = cdrTextShape(6)
shp.Text.Story.Font = 'Swis721 Cn BT'              # 字体名要用 family 名，不是文件名
shp.Text.Story.Size = size_pt                      # 二分逼近目标高度
shp.Stretch(x_scale, 1.0)                          # 再把宽度拉到目标
```

**字号与宽度要分开解**：

- **字号只决定高度**（点值与高度线性），二分几次就能对上目标高度；
- **宽度靠非等比拉伸补**。实测该页脚：字号 6.00 pt → 高 2.0321 mm、宽 53.355 mm；
  目标 2.243 × 64.018 mm，`Stretch(1.1999, 1.0)` 后宽度精确到 **64.0180 mm**。

不要试图用字号去凑宽度——那会把高度带偏。

---

## 2. 产出

### 2.1 命令行

```bash
python scripts/cdr_text_live.py \
    --image 源图.png \
    --region footer=0,17,0,372 \
    --mm-per-px 0.17256 \
    --out-dir out/text
```

`--region` 可给多次（一张图里多个文字区）；坐标是**源图像素、左上原点**。

### 2.2 产物文件

| 文件 | 内容 |
| --- | --- |
| `live_text.json` | 总表：每区的最终文本、选定字体、IoU、lift、判定、坐标与尺寸 |
| `font_match_<区名>.json` | 该区的候选字体全表（前 N 名 + 全集统计） |
| `compare_<区名>.png` | 三联对照图：源 / 候选渲染 / 差异（红=只源有，蓝=只候选有） |

`live_text.json` 里每个区的字段：

```json
{
  "region": "footer",
  "source_box_px": [0, 0, 371, 16],
  "size_px": [371, 16],
  "size_mm": [64.02, 2.761],
  "text": "O.v.D. Importadora e Distribuidora Ltda. • Curitiba - PR",
  "ocr": {"raw": [...], "joined": "...", "variants": [...]},
  "case_fixes": [
    {"word": "-", "to": "•", "kind": "symbol", "adopted": "geometry"},
    {"word": "Ltda.", "to": "ltda.", "kind": "case", "rejected": true,
     "iou_before": 0.5714, "iou_after": 0.5698}
  ],
  "word_count_by_projection": 9,
  "font": {"family": "Swis721 Cn BT", "style": "Bold", "file": "tt0010m_.ttf",
           "iou": 0.7244, "width_ratio": 0.987, "score": 0.8032},
  "font_stats": {"n": 248, "median_iou": 0.2716, "max_iou": 0.7244},
  "font_match_iou": 0.7244,
  "rebuild_iou": 0.7244,
  "lift_over_median": 2.67,
  "thresholds": {"min_iou": 0.72, "min_lift": 1.25, "min_iou_floor": 0.35},
  "verdict": "convert"
}
```

**`verdict` 是最重要的一栏**：

- `convert` —— 可以转活字；报告里要写明**选用的字体与匹配分数**，
  并注明"字体为形状匹配推断，非源文件自带信息"。
- `keep_trace` —— **不转**，保留描摹轮廓。报告里要写明原因
  （"字体库里没有足够接近的"或"字形相似度不足以证明是同一字体"），
  并给出库里最接近的 3 个及分数，让用户决定。

### 2.3 交付说明必须写的

- 识别出的文本，以及**哪些字符是推断修正的**（`case_fixes` 里逐条记着）。
- 选用字体 + 匹配分数 + lift；**不得声称"这就是原字体"**，
  只能说"字体库中最接近的"。
- 若降级为描摹，说明原因并列出最接近的候选。
- 活字的位置、字号、横向缩放——用户后续要改字时会用到。

---

## 3. 依赖与安装

```
numpy, pillow, opencv-python-headless      已有
rapidocr-onnxruntime                        OCR 引擎（离线，无需联网）
onnxruntime                                 推理后端
pyclipper, shapely, six, flatbuffers, protobuf, pyyaml
```

安装要点（**都实测踩过**）：

```bash
# rapidocr 依赖 opencv-python（非 headless），会试图替换已有的 cv2 而被安全删除拦截
pip install --no-deps rapidocr-onnxruntime
pip install onnxruntime pyclipper shapely six flatbuffers protobuf pyyaml
```

**Windows 上 onnxruntime 还缺两个 VC 运行库**，否则报
`ImportError: DLL load failed while importing onnxruntime_pybind11_state`：

- `vcruntime140_1.dll` —— Python 官方安装目录里自带，直接拷到 venv 的 `Scripts/`
  和 `onnxruntime/capi/` 即可；
- `msvcp140_1.dll` —— 系统 `System32` 里往往只有 `msvcp140.dll`，缺 `_1` 这个。
  可从其它装了 VC++ 2015-2022 x64 运行库的程序目录里取，或安装官方
  VC++ Redistributable。

用 `pefile` 查 `.pyd` 的导入表能直接看出缺哪个：

```python
import pefile
pe = pefile.PE('.../onnxruntime/capi/onnxruntime_pybind11_state.pyd', fast_load=True)
pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
for e in pe.DIRECTORY_ENTRY_IMPORT: print(e.dll.decode())
```

**没有 OCR 引擎时脚本不会崩**：会明确打印缺什么、怎么装，并跳过 OCR
（此时无法得到文本，自然也不会转活字）。

---

## 4. 适用边界

**适合转活字**：

- 字号较大（≥ 12pt / 40px 高）的标题、正文——匹配证据强，`min_iou` 通道就能过。
- 无衬线常规体、常见字族（Helvetica/Arial/Times 系）——字体库里大概率有接近的。
- 用户明确说"文字要能改"的场景。

**不适合 / 应保持描摹**：

- **反白字**（深底白字，如易碎标的 `CUIDADO FRÁGIL`）——文字是**图形的一部分**，
  转成文本会丢掉与底块的位置耦合；而且反白会干扰 OCR。
- 艺术字、变形字、字距被手工调过的排版。
- 字体库里确实没有的字族（中文书法体、特殊商业字体）——lift 上不去，会正确降级。
- 文字与图形咬合（如文字沿路径、文字与线条叠印）。

**已知限制**：

- OCR 对 < 12px 高的文字仍会错字符，脚本靠 lift 判据拒绝转换来兜底，
  但**不能保证识别全对**——所以产物里必须逐条记录修正痕迹。
- 逐词大小写裁决用的是"整行 IoU"，而**单个窄字形只占整行约 1% 面积**，
  全局 IoU 分不出来。所以符号类改动走"量字形直接采纳"，只有大小写走 IoU 裁决。
- 字体匹配只枚举 `C:\Windows\Fonts` 的文件。CorelDRAW 若装了额外字体包
  （`--corel-only` 时会去读 `Application.FontList`），需要把字体目录加进 `FONT_DIRS`。

---

## 5. 与主流程的关系

`cdr_text_live.py` 是**独立的一步**，不自动嵌入主流程：

```
cdr_bitmap_to_cdr.py          位图 → CDR（文字区默认走描摹）
cdr_text_live.py              文字区 → 识别 + 字体匹配 + 判定（产出 JSON）
   ↓ verdict == convert 时，才用匹配到的字体建活字，替换掉那层的描摹轮廓
```

**为什么不默认自动替换**：字体猜错比描摹失真更糟。默认走描摹是安全的；
要转活字必须显式跑这一步并看 `verdict`。报告里也要如实写明哪些是活字、
哪些是描摹轮廓、以及活字用的是哪个字体。
