---
name: coreldraw-x8-redraw
description: 给一张位图（PNG/JPG 设计稿、扫描件、包装稿导出图），自动操作 CorelDRAW 把它画成矢量图——先整图识别文字/符号/图形（含文字朝向与中英文 OCR），再把文字区判定为"转可编辑活字"或"保留描摹轮廓"，然后逐区描摹非文字部分、在 CDR 中建页建图层并精确定位、配准式像素校验，一条命令跑完。也支持源 CDR 剖析重建、PDF 派生、图片+尺寸参数化绘制，以及校验重建结果与源文件是否一致。Trigger keywords - CorelDRAW, CorelDRAW X8, CDR, 位图转矢量, 矢量化, 描摹, 自动绘制, 重绘, 复刻, 重建, 文字识别, 转活字, redraw, rebuild, trace, vectorize, bitmap to vector, OCR, live text, VGCore, pywin32。
agent_created: true
---

# CorelDRAW X8 位图矢量化与 CDR 重绘

## 主用途

**用户提供一张位图，本技能自动操作 CorelDRAW 软件，画出这张位图对应的矢量化图形。**

用户给的是一张图（设计稿截图、扫描件、包装稿导出图、参考照片），要求"照着这张图在
CorelDRAW 里画出来"——走下面的位图矢量化主线，一条命令跑完。

> **⚠️ 图纸里有文字时，必须先识别文字，不能直接整张描摹。**
> 直接描摹会把文字也描成轮廓曲线：**看着像，但不可编辑、不可改字、换不了字体**，
> 等于把可编辑内容降级成了死图形。用户明确要求过这一点
> （"没有第一时间识别文字 中英文 导致有些文字也是描线的 就不对"、
> "第一步应该是识别图片中的文字和符号"）。
>
> 正确顺序是**识别在前、描摹在后**，且两者范围互斥：
>
> | 步骤 | 脚本 | 作用 |
> | --- | --- | --- |
> | 1 | `cdr_scan_text.py` | **整图识别**：文字、符号、图形块，以及每块文字的朝向（0° / 180°） |
> | 2 | `cdr_text_live.py` | 逐区判定 `convert`（转活字）/ `keep_trace`（保留描摹），只判定不写 |
> | 3 | `cdr_bitmap_to_cdr.py` | 描摹 **非文字块 + keep_trace 的文字区** |
> | 4 | `cdr_text_live.py --apply` | 把 `convert` 的文字建成可编辑文本 |
>
> 关键是**第 3 步的描摹范围要包含 `keep_trace` 的文字区**。漏掉它，品牌字标、
> 特殊符号就会整个从成品里消失（踩过：`daiion` 字标和 `4#` 都不见了）。
> 而 `--scan-json` 挖除清单只能喂 `convert` 的，喂全量会把 `keep_trace` 的字标一起挖掉。
>
> 判定为 `keep_trace` 不等于失败——**字库里没有接近的字体时，保留描摹轮廓才是对的**。
> 实测 `daiion` 是定制品牌字标（首字母高度等于 x-height，扫遍字体库无一款如此），
> 强行套 Arial Bold（IoU 0.60）反而是降级。此时必须在报告里说明原因。

## 快速开始（一条命令）

```powershell
python scripts\cdr_bitmap_to_cdr.py --image ref.png --page-width 210 --output out\ref.cdr
```

只用给**源图**和**页面宽度**。脚本自动完成全部六步，产出：

| 产物 | 位置 |
| --- | --- |
| 矢量 CDR | `--output` 指定 |
| 预览 PNG | `<out-dir>/<文件名>_preview.png` |
| 描摹 SVG 与清单 | `<out-dir>/svg/`（每区一个 SVG + `manifest.json`） |
| 定位记录 | CDR 同目录 `placement.json` |
| 配准校验与差异图 | `<out-dir>/compare/`（`overlay_diff.png`、`zoom/`、`metrics.json`） |
| 报告 | `<out-dir>/report.md` + `report.json` |

`<out-dir>` 默认是 CDR 同目录下的 `<文件名>_work`。

常用变体：

```powershell
# 用标准纸型（高度按纸型取，不按图比例推导）
python scripts\cdr_bitmap_to_cdr.py --image ref.png --page-size A4 --output out\ref.cdr

# 没有 CorelDRAW 也能跑：只描摹出 SVG 与清单
python scripts\cdr_bitmap_to_cdr.py --image ref.png --page-width 210 ^
  --output out\ref.cdr --trace-only

# 裁掉左侧扫描残留后重新标定
python scripts\cdr_bitmap_to_cdr.py --image ref.png --page-width 210 ^
  --output out\ref.cdr --crop-left 4

# 自动分区不满意时手工指定（覆盖自动结果）
python scripts\cdr_bitmap_to_cdr.py --image ref.png --page-width 210 ^
  --output out\ref.cdr ^
  --region "art:6,500,1217,900" ^
  --region "logo:0,901,1217,1059,invert"

# 只重建报告，不重跑描摹与建 CDR
python scripts\cdr_bitmap_to_cdr.py --report-only --out-dir out\ref_work ^
  --output out\ref.cdr
```

`--report-only` 的存在理由：报告里的数字来自两个阶段（逐区寻优在前、配准校验在后），
早先的实现在校验**之前**就写了报告，于是报告里的还原度停留在有 bug 时期的旧值
（整页 24.47%），真实值是 93.62%。顺序修好后仍不够——改一行措辞也不该重跑
十几分钟描摹。所以清单里补齐了每区的 `iou/recall/precision/ink_ratio`、
垫底矩形 `rects`、图层顺序 `layer_order` 与 `page_h_explicit`，
让报告可以完全脱离描摹过程独立重建。清单缺这些字段时会显示 `—` 而不是 0
（0 会被误读成"完全不像"）。

## 位图矢量化流程（脚本内部六步）

1. **标定**
   `mm_per_px = 页面宽度mm / 图片宽度px`。核对图宽高比与页面宽高比——
   不一致说明源图裁切过，纵向内容高度会与页面高度产生差异，必须在报告中说明
   （实测 vonder 图：比例 0.7331 ≠ A4 的 0.7071，按宽度 210mm 标定后纵向内容
   高 286.442mm，比页高少 10.6mm）。
2. **边缘残留探测**
   探测四周是否有扫描/裁切残留暗边。**这一步不能跳**，残留会污染后续分区：
   贯穿全高的黑边条让**每一行**都含墨迹像素，行投影就永远找不到空隙，
   分区会把本应分开的区块粘成一整块（实测 vonder 图左侧 4px 黑边条
   把易碎标、插图、页脚粘成了一个 `y 0..901` 的大块）。
   脚本用 `strip_residue()` 把贯穿边缘的行/列从**投影判定**里抹掉，
   但**不复刻为设计内容**；要用 `--crop-left N` 才能真正裁掉并重新标定。
3. **自动分区**
   行投影切内容带 → 带内判定满版色带 → 色带之外再按行细切 → 列方向收紧与切分。
   详见下节。
4. **逐区阈值寻优 + 描摹**
   每个区域扫若干二值化阈值，取 IoU 最高者。关键参数与陷阱见下节。
5. **在 CorelDRAW 中重建**
   建页、按类型建图层、导入 SVG、按毫米包围盒精确定位、附加垫底原生矩形、
   保存 CDR、导出预览。**导入后必须逐项比对"目标坐标/尺寸 vs 实际坐标/尺寸"，
   误差应 < 0.02 mm。**
6. **配准式像素校验 + 报告**
   把渲染图**贴回整页坐标系**后与源图逐像素比对，输出 IoU / 召回 / 精确
   与差异叠加图。直接拿内容包围盒导出图与整页源图比是错的（会得到 ~12% 的
   无意义 IoU）。同时算**墨迹面积比**判定笔画粗细。

## 自动分区怎么做的

脚本按顺序用这几个判据：

1. **行投影切内容带**：行内墨迹像素数 ≥ `min_ink_px`（默认 4）视为有效行；
   有效行之间空白 < `min_gap_px`（默认 6）则合并；短于 `min_run_px`（默认 6）的段丢弃。
2. **带内判定"满版色带"——必须用行的中位数灰度，不要用覆盖率。**
   这是本流程最容易踩的坑：色带中间常被反白字标掏空，覆盖率会掉到 0.85 以下，
   用覆盖率判定会把整条色带误判成普通内容区。
   实测 vonder 图：

   | 判定信号 | 结果 |
   | --- | --- |
   | 行覆盖率 ≥ 0.85 | 碎成 `901..960` + `1037..1059` 两段（错） |
   | **行中位数 < 128** | **`901..1059` 整条**（对） |
   | 行 25 分位 < 128 | 多出 `661..784` 假色带（插图密集区，错） |

   行中位数只看"这一行整体是深还是浅"，不受中间被掏空影响。
   但**光有中位数还不够**，必须再加一条**横向贯通**判据（见下）。
3. **横向贯通校验（`_is_full_bleed`）**：候选色带在其行范围内**每一列都必须有墨迹**。
   为什么必须有这一条：行中位数只问"这一行整体偏深吗"，**两个并排的实心内容块
   也能把中位数压到 128 以下**。实测合成图（600px 宽，两个 150px 实心黑块并排，
   304/600 像素为暗）中位数就是暗的，于是整条内容行带被误判成色带——后果是把
   两块内容当成"垫底矩形 + 反白描摹"，输出完全错。

   满版色带的定义特征是横向贯通（底色顶到左右页边），所以它的行范围内每一列
   都至少有一个墨迹像素；两块并排内容之间的空隙列则一个墨迹像素都没有。
   用**未剥离**的墨迹图判定，因为残留条本身也是贯通的（vonder 图左侧 4px），
   它不该让真色带被否掉。真色带即使中间被反白字标掏空，字的上下仍有纯底色行，
   所以被掏空的列照样有墨迹——实测 vonder 色带 y 901..1059、字标占 y 926..1040，
   其上方 25 行与下方 19 行是纯底色，全列通过；而内容带 y 559..885、y 345..440
   都不贯通，正确落回内容。
4. **色带拆成两件事**：
   - 垫底的**原生矢量矩形**（颜色从源图暗像素的 RGB 中位数取，实测 vonder 得 `#121011`）
   - 同一框内的**反白描摹**（`invert=True`，描出白字标）

   色带的横向范围用**未剥离残留**的墨迹算——满版底色本来就该顶到页边，
   残留剥离只用于"哪里是内容"的判定，不该把色带缩进去。
5. **列方向收紧 + 切分**：列间空隙超过 `col_gap_frac * 图宽`（默认 0.05）时切分，
   `--no-split` 可关。色带不切，保持满版矩形。
6. **命名**：色带 `band01`（其反白描摹叫 `band01_ink`），其余按形状猜语义——
   高度 < 6mm 叫 `text`，宽 < 60mm 且高 < 40mm 叫 `mark`，其余叫 `art`。
   **这只是命名启发式，不代表真实语义**，可用 `--region` 覆盖。

实测 vonder 图自动分区与手工调优结果对照：

| 区域 | 自动分区 | 手工调优 | 判定 |
| --- | --- | --- | --- |
| 色带 | mm 0.000,155.472..210.000,182.736 | 0,155.472..210,182.736 | 一致 |
| 插图 | mm 56.253,96.631..170.312,152.539 | 56.253,96.545..170.291,152.604 | 一致 |
| 易碎标 | mm 152.539,59.532..194.988,75.924 | 152.474,59.596..195.074,75.924 | 一致 |
| 页脚 | mm 81.274,186.878..145.292,189.121 | 81.252,186.921..145.249,189.703 | 一致 |

## 描摹参数与陷阱

先跑 `--trace-only` 摸底（不需要 CorelDRAW），确认分区与各区域 IoU 后再正式建 CDR。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--upscale` | 8 | **描摹前对灰度做 LANCZOS 上采样再二值化**。细部保真的关键：原分辨率直接二值化会把小图标的平顶圆滑成尖拱、细笔画简化成折线。实测 U=4 得 95.2%、U=8 得 98.3% |
| `--threshold` | 逐区寻优 | 二值化阈值，**直接决定笔画粗细**。细笔画文字（6pt 级）对它极敏感：页脚在 118 处 IoU 有明显峰值，不是越接近 128 越好。给了该参数就不寻优 |
| `--tune-thresholds` | 112,118,128,138,148 | 寻优候选。面积 > `--max-tune-px`（默认 400k 源像素）的区域只扫 3 个以控时 |
| `--alphamax` | 1.0 | **不要用 0**。0 会让 potrace 输出纯多边形（圆角变折线），而它的像素 IoU 反而最高——只看 IoU 会选出视觉最差的结果。脚本会先只在"有真实曲线段"的候选里选 |
| `--turdsize` | 2 | 斑点面积阈值，单位是**源图像素**，内部按 `upscale²` 折算 |
| `--opttolerance` | 0.1 | 曲线优化容差，越大越简并 |

其他实现细节（potracer 的 invert 约定、evenodd、`Z M` 分隔符、反白区域要剔除
接触裁剪边界的白色连通域）见 `references/raster-to-vector-notes.md`，那份文档
是实测踩坑结论，照做可以省掉大量试错。

### 为什么不用 CorelDRAW 内置描摹（PowerTRACE）

**结论：内置描摹能跑，但质量差一个量级，且不可控不可测，所以不用于主路径。**

先澄清一个我自己写错过的结论——第一轮实测曾得出"`Finish()` 不产出矢量""参数被忽略"，
**两条都是错的**：`Finish()` 的产物是 `cdrGroupShape = 7`（群组），我的计数只看了
`Type == 3`（曲线）把它整个漏掉了；而"参数不生效"是因为我挑了 128 与 255 两个
**恰好同档**的值去比（刻度是 0–255，128 以上进平台期）。**判某功能无效之前，
先确认自己的探针能看见它。**

接口与实测事实：

- 入口是 `Shape.Bitmap.Trace(...)`（`Trace` 挂在 `IVGBitmap` 上，**不在** `IVGShape` 上），
  返回的 `TraceSettings` 只算预览（`CurveCount` / `NodeCount`）；`Finish()` 才提交，
  提交后原位图被替换成**一个群组**（要递归展开才数得到曲线）。
- 六个轮廓预设确有区别（art01_1x：线条图 165 曲线/2711 节点、徽标 21/728、
  详细徽标 30/839、剪贴画 186/2181）。
- 中心线（`cdrTraceTechnical = 7` / `cdrTraceLineDrawing = 8`）输出**开放路径 + 描边、
  无填充**，与"复刻外观"的目标不同，不能替代轮廓描摹。

**质量对比（同一区域，公平对齐分辨率后各算 IoU）：**

| 方案 | IoU% | 面积比 |
| --- | --- | --- |
| **我们 potrace（U=8 + 阈值寻优）** | **99.67** | 0.998 |
| PowerTRACE 轮廓-线条图（1× / 2×） | 68.05 / 72.38 | 1.251 / 1.190 |
| PowerTRACE 轮廓-剪贴画（1× / 2×） | 67.49 / 71.77 | 1.231 / 1.173 |
| PowerTRACE 中心线（1× / 2×） | 29.26 / 36.13 | 0.463 / 0.609 |

上采样对它也有用，但远不足以追平；面积比普遍偏离 1，说明它没有"笔画粗细"这个可调量，
而我们的阈值寻优正是拿 IoU + 面积比双指标去卡这个的。

另外两个硬问题：**大位图会让 X8 崩**（5296×2608 直接进程消失；
`Smoothing/DetailLevel` 推到 192 也崩）；**`doc.Export` 在"被 COM 拉起"的实例上
必然失败**（连纯矩形都失败，是实例状态问题，不是描摹的问题）。

唯一值得吸收的是**中心线思想**：细笔画文字用轮廓描摹要 73 子路径 / 840 段，
中心线只要 6 曲线 / 6 节点，差两个数量级。该思路可自研
（Zhang-Suen 细化 → 骨架 → 折线提取 → Douglas-Peucker，纯 numpy + cv2 无新依赖），
但简化参数需调优、视觉保真需验证，**目前未并入主流程**。

完整实测数据（接口签名、枚举真值、参数扫描、逐预设对照、自我纠错记录）见
`references/raster-to-vector-notes.md` §9。

### 细笔画文字：更彻底的一条路是转活字

比"中心线瘦身"更彻底：**把文字识别出来、用最接近的字体重建成真文本**。
实测 vonder 页脚那行 6pt 文字：识别 → 字体匹配到 **Swis721 Cn BT / Bold**
（逐词对齐 IoU 0.7235，领先候选集中位 **1.68 倍**）→ 重建为 1 个文本对象，
**文字可编辑**，而描摹轮廓是 73 子路径 / 840 段。

```bash
python scripts\cdr_text_live.py --image 源图.png ^
    --region footer=1083,1100,470,842 --mm-per-px 0.17256 --out-dir out\text
```

`--region` 的坐标直接用流水线报告「分区与参数」表的「源框 px」列。

加 `--apply out\cover.cdr --replace-traced footer=05_TEXT` 才真的建活字
（建在 `<区名>_LIVE` 图层，并**删掉该区的描摹轮廓**）。

**⚠️ `--replace-traced` 不是可选项。** 描摹轮廓与活字同位置，叠加等于把这一行
字加粗一遍。实测同一页脚：描摹轮廓 IoU 92.82%／墨迹比 1.046，
活字 70.50%／1.141，**叠加 77.70%／1.286（比源图粗 28.6%）**——
叠加两头不讨好。删除只在活字定位误差 ≤ 0.05mm 之后才执行。

产出 `live_text.json`（含逐区文本、选定字体、IoU、lift、**四条判据的取值**、判定）、
`font_match_<区>.json`、`compare_<区>.png` 三联对照图。

**但默认不自动替换描摹轮廓**——字体猜错比描摹失真更糟。必须看 `verdict`：

- `keep_trace` + `reject_kind=weak_match` → 保留轮廓，报告里列出库里最接近的候选；
- `keep_trace` + `reject_kind=not_text` → **这块根本不是一行文字**，该去查区域切分。

六个关键设计点（详见 `references/live-text-design.md`）：

1. **OCR 必须二值化，而且不能按置信度选变体**。实测同一区域同一裁切框：
   喂原始灰度 **4 套变体全错**（`O.V.D.` 大小写错、词间空格被吃掉）；
   喂二值图 **12 套里 7 套完全正确**。原因是低分辨率扫描件的笔画边缘全是
   抗锯齿，检测器分不清字间浅灰缝隙。更关键的是**错误的灰度变体置信度反而
   最高**（0.921 vs 正确 0.918），所以选择器改用**列投影切出的词数**
   （独立于 OCR 的几何证据，实测 100% 分对），置信度只用于打破平局。
   教训：**模型自报的置信度不能当选择依据**。
2. **多套预处理 + 多套留白**。2×+20px 留白出乱码、2×+纵向 0 留白出正确答案——
   **没有一套参数对所有图都稳**。
3. **相似度不能用绝对 IoU 门槛**。16px 小字即使文本与字体都对，IoU 也只有 0.61；
   用 0.62 的绝对门槛会把完美匹配也拒掉。改用 `lift = 最佳 ÷ 候选中位`（与字号无关）。
4. **但光有相似度不够，必须补"这是不是文字"的硬门槛**。实测把工具插图区
   当文字区喂进来，OCR 幻觉出 `'wander'`，lift 1.53、IoU 0.386 **两个相似度判据
   全过**，于是在 CDR 里建了一行 `'wander'`。根因：lift 与 IoU 都在"横向拉伸到
   同宽"**之后**算，而拉伸本身就把最大的差异抹掉了。补两条与字号无关的比值：
   **分量数÷字符数**（真文字 1.10 / 误判区 11.00）与**自然宽度比**
   （真文字 0.992 / 误判区 1.887），两条都过才允许转。
5. **易混符号与"上下同形字母的大小写"直接量几何**：`•` 宽高比 1.25/密度 0.80
   vs `-` 宽高比 4.00 → 直采；`cosvwxz` 的大小写用**行内字高**判（x-height 与
   cap-height 的中位数，待判字归到更近的一边）→ 直采。剩下的大小写才走整体 IoU
   逐词裁决——单个窄字形只占整行约 1% 面积，全局 IoU 分不出来。
6. **`--replace-traced` 不是可选项，是必需项**。描摹轮廓与活字同位置，
   叠加等于把这一行字**加粗一遍**（实测墨迹比 1.286 = 比源图粗 28.6%，
   而单独描摹 1.046、单独活字 1.141）。转活字的意义是**替代**描摹。
   删除时机：只在活字建好、且定位误差 ≤ 0.05mm 之后删。

**⚠️ 保存必须核验落盘，不能只看"没报错"。** 真踩过：整轮跑完、内存里回读核验
全对、日志打印"已保存"，重新打开文件发现改动**根本没进去**，白跑一轮还差点当成
交付完成。根因是目标 CDR 被另一个进程打开（用户自己开着 CorelDRAW），文件只读，
而 `doc.Save()` 在这种情况下**既不抛异常也不写文件**。所以保存前后各取一次
`(大小, mtime_ns)` 指纹比对，没变就报错并以退出码 3 结束。
**一般化教训：`调用没报错` ≠ `结果发生了`——跨进程落盘的写操作必须回读磁盘确认。**

**⚠️ 验证时一定要用真正的源图**。早期"验证通过"跑在一个人工做的测试裁切上，
那个裁切恰好是**二值图**（PIL mode `1`），所以看起来一切正常；
换回源图直裁后 OCR 立刻失效。**测试夹具不能比真实输入更"干净"**。

## 判断还原度：不要靠眼睛

**对照图必须同尺度。** 源图原生密度（如 5.795 px/mm）与 CorelDRAW 导出密度
（如 11.81 px/mm）不同，直接把同一毫米范围的裁切按同倍数并排，导出侧字面会
大一圈，让人以为重绘又大又粗——这是纯尺度假象。

**判断笔画粗细要算墨迹面积比**（与分辨率无关）：
矢量几何面积（按 60 px/mm 光栅化数像素）÷ 源图墨迹面积。
比值落在 **0.98~1.02** 就说明粗细吻合。

> 反面教训：把导出图降采样回源图尺寸再阈值化，抗锯齿边缘会被算成墨迹，
> 细笔画被补边，实测能把 1.00 放大成 1.39，凭空报出"页脚偏粗 39%"的假结论。

达标参考（vonder 图，U=8 + 逐区寻优后）：

| 区域 | 原生分辨率 IoU | 面积比 |
| --- | --- | --- |
| vonder 字标 | 99.51% | 0.999 |
| Fragile 易碎标 | 98.25% | 1.001 |
| 工具群插图 | 97.55% | 1.007 |
| 6pt 页脚文字 | 93.81% | 0.993 |
| **整页配准** | **93.48%**（召回 95.33% / 精确 97.97%） | — |

## 交付说明必写

- 报告 CDR 路径、内容构成、各区域还原度、已知偏差及其来源
  （哪些是描摹近似、哪些是源图残留未复刻、哪些是小字天然失真）。
- **绝不把描摹结果说成"原始矢量"**，明确标注哪些是描摹近似。
- 源图边缘的扫描/裁切残留**不得复刻为设计内容**，但必须在交付说明中提及，
  避免被误判为漏画。
- 不得声称与源文件"完全一致"；应给出配准指标（IoU / 召回 / 精确）与已知偏差。
- 报告里的还原度必须与 `compare/metrics.json` **一致**。两者矛盾说明报告是在
  校验之前写的——这是真实踩过的坑（`report.md` 写 24.47%，
  `metrics.json` 是 93.62%）。交付物自相矛盾比数字偏低更糟。
- 保留参数块或参数文件（`manifest.json` / `report.json`），
  使文件可用变更后的尺寸重新生成。

## 其他输入模式

主用途之外，本技能也覆盖以下场景（与 AutoCAD 的 `autocad-dwg-redraw` 技能同构：
**剖析 → 生成提示词 → 重建 → 校验**，区别在于操作对象是 CorelDRAW 的
文档/页面/图层/形状模型，而非 DWG 的 ModelSpace/PaperSpace）：

### 源 CDR 模式

源 CDR 可用时，**不要**仅凭截图或视觉风格去推断。先提取或复制真实形状，
再与原始文件校验。最终交付优先用形状级精确复制（`Shape.CopyToLayer`）。

```powershell
python scripts\cdr_prompt_builder.py --source input.cdr --output outputs\input-redraw-prompt.md
python scripts\cdr_redraw.py --source input.cdr --output outputs\redraw_exact.cdr --mode clone
```

### PDF 派生模式

没有源 CDR 时使用。先判断 PDF 内容构成：可提取矢量路径、嵌入位图、可提取文本，
还是混合。有矢量路径时优先用矢量路径而非栅格描摹。
**除非手上有原始 CDR，否则不得声称与源文件完全一致。**

### 图片 + 尺寸模式

用户给参考图外加书面尺寸或参数表时使用。把用户给出的尺寸、单位、数量与版面约束
视为权威；像素只用于判断拓扑、顺序与视觉关系。当图片与给定尺寸冲突时，
以尺寸为准并报告冲突。**绝不用图片比例替代给定的数值尺寸。**

最小参数规格：参考图片路径；单位系统与总体宽高深；构件尺寸、厚度、偏移、数量与
重复间距规则；所需视图、图层、标注、文本、群组；输出 CDR 路径；
对尺寸未覆盖细节的显式假设与容差。

## 重绘提示词标准

每份生成的定制提示词必须包含：

- 文件标识：源文件名、完整路径、文件大小、文档单位、页面数、形状总数。
- 文件分类：排版稿、单页设计稿、多页宣传册、标签、包装刀模稿，或未知。
- 所需流程：CorelDRAW 可用时用形状级精确复制；仅在有完整结构化数据时生成代码。
- 环境假设：Windows、CorelDRAW X8 或更高、Python 3.10+、COM、pywin32。
- 页面清单：页面名、宽、高、方向、图层数、形状数。
- 图层清单：页面、图层名、可见性、可编辑性、可打印性、顶层形状数、形状总数。
- 形状类型分布：矩形、椭圆、曲线、多边形、位图、文本、群组、表格、度量、艺术笔、符号、网状填充。
- 文本清单：美术字与段落文本，含所在页面与图层。
- 数据提取要求：页面 / 图层 / 形状属性 / 曲线节点 / 填充 / 轮廓 / 效果 / 对象数据。
- 校验标准：页面数、图层名集合、顶层形状数、形状总数、类型分布、文本内容、视觉版面。
- 已知风险：精简版缺 COM 注册、效果被简化、位图断链、字体缺失、跨文档 `CopyToLayer` 版本差异。

手动撰写时以 `references/prompt-template.md` 为模板；有 CorelDRAW COM 可用时
优先用 `scripts/cdr_prompt_builder.py` 自动生成。

## 精度要求

- 复制全部页面与全部图层，除非用户明确要求仅处理某页或某图层。
- 保留文本、位图、群组、表格、度量、艺术笔、符号、网状填充、填充、轮廓、效果与对象数据。
- 源文件含有的文本、群组或效果若在目标中缺失，视为校验失败。
- 不覆盖源文件。始终写入新的输出路径。
- 文件使用链接位图、自定义效果或跨文档引用时，报告风险并在 CorelDRAW 中做视觉校验。
- 位图矢量化模式中，导入后必须逐项比对目标坐标/尺寸与实际值，误差 < 0.02 mm。
- 图片 + 尺寸模式中，拒绝非正尺寸、越界构件、无法解释的重叠与不符合总体尺寸的算术合计。

## CorelDRAW 稳定性说明

- 精简版 / 绿色版 CorelDRAW 常缺失 COM 注册与 VBA 组件，会抛
  `Invalid class string` 或 `ActiveX component can't create object`。必须使用官方完整版。
- CorelDRAW 忙时可能抛 "Call was rejected by callee"。等待片刻后重试；
  若持续失败，关闭多余实例后重启 CorelDRAW。
- 批量操作前设置 `app.Optimization = True` 与 `app.EventsEnabled = False`，
  结束后务必复位并 `app.Refresh()`。
- 自动化运行期间不要手动点击 CorelDRAW 窗口，否则会打断 COM 调用。
- 集合索引为 **1 基**（`Shapes(1)`、`Pages(1)`、`Layers(1)`）。
- `Clone()` / `Duplicate()` 仅限同文档；跨文档重建用 `Shape.CopyToLayer(target_layer)`。
- 若某版本 `CopyToLayer` 不支持跨文档，回退到文件级复制（`--mode duplicate`）作为保底。
- 不同版本的形状类型 / 单位枚举数值存在差异：脚本优先从类型库常量取值，
  并始终同时输出原始数值。

### X8 实测补充

- **连接方式**：`GetActiveObject` 在 X8 上常抛
  `com_error(-2147221021, '操作无法使用')`（MK_E_UNAVAILABLE）。
  改用 `win32com.client.Dispatch("CorelDRAW.Application.18")`，会复用已打开实例。
- **可选 VT_DISPATCH 参数会炸**：`doc.SaveAs(p, None)`、`lay.Import(p, 0, None)`、
  `doc.Export(p, 802, 1, None, None)` —— 末尾必须显式补 `None`，
  否则抛 `TypeError: The Python instance can not be converted to a COM object`。
- **`ExportEx` 在 X8 上"返回成功但不写文件"**：以 `Export` 为主，
  且必须用 `os.path.exists` + 文件大小确认，不能只看有没有抛异常。
- `page.Shapes.All` 是**方法**：写 `page.Shapes.All().Count`。
- **`Layer.Import` 不返回形状对象，而且把新形状插到图层"底部"（索引 1），
  不是追加到末尾。** 所以**不能盲取 `Item(Count)`**：如果图层里已有形状
  （例如同一图层先放了垫底矩形），`Item(Count)` 会取到那个旧形状，于是把新形状
  的目标尺寸/位置/填充写到旧形状上——表现为两个形状属性互换。
  实测后果：色带矩形拿到字标的几何、字标拿到矩形的深色填充，导出图里整条
  色带消失、字标变成黑字。
  定位新形状要用"导入前后名称集合的差"；最省事的做法是**一个区域一个图层**，
  让导入时图层为空，索引就没有歧义。
- 矩形也要验：`CreateRectangle2` 的参数语义在各版本间有差异，
  只打印请求值而不核对 `PositionX/SizeWidth` 会掩盖错误。
- **y 轴向上**，设计稿以左上为原点：
  `doc.ReferencePoint = 3`（cdrTopLeft）后，`shape.SetPosition(x_mm, page_h - y_top_mm)`。
- 常量实测值：`cdrMillimeter = 3`（不是 4）、`cdrPortrait = 0`、`cdrTopLeft = 3`、
  导出滤镜 `cdrPNG = 802`、颜色模式 `cdrRGB = 4`。
- **`Document.Unit` 不随文件保存**：设成毫米并保存后，重新打开会回到英寸（1）。
  几何数据本身是绝对单位、不受影响，只是显示/读值单位变了。
  读坐标前先 `doc.Unit = 3`，否则拿到的是英寸数值。
- `Application.ActiveDocument` 是**只读属性**，不能赋值；要切换活动文档用 `doc.Activate()`。
- **`SaveAs` / `Export` 会按 CorelDRAW 自己的工作目录解析相对路径**，
  必须先把输出路径转成绝对路径，否则文件会落到意外位置。
- 新建文档自带"图层 1"，直接改名复用，否则会多出一个空图层。
- **文件被 CorelDRAW 自己打开时，`SaveAs` / `Save()` 会静默变成空操作**——
  不抛异常、不写盘，日志照常打印"已保存"。而**占用往往就是自动化自己造成的**：
  `cdr_common.open_document()` 刻意"只开不关"（把成果留在窗口里给用户接着改），
  于是下一轮重建同一个文件时目标被上一轮的文档占着，`os.replace` 抛
  `PermissionError [WinError 32]`。
  修法：重写目标前先调 `cdr_common.release_document(path)`（只关路径匹配的那一个，
  `Dirty=True` 时拒绝关闭），保存前后再用 `cdr_common.file_fingerprint()`
  比对 `(大小, mtime_ns)` 确认真的落盘了。
  指纹没变但文件**能**独占打开时按成功处理——那是"本次保存无内容可写"，不是故障。
- **不要为了"让文件存在检查有意义"而先删旧文件。** 受限运行环境会把 `os.remove`
  判为批量删除并**直接掐掉进程**：日志里只剩一行 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`，
  整条流水线莫名 `EXIT=1`，而 CDR 其实已经保存好了，极易误判成保存失败。
  用**改名归档**（`os.replace(f, f+'.bak')`）或**指纹比对**替代删除。
- `BoundingBox` 返回的是 **`IVGRect` 对象，不是元组**，不能下标取；
  用 `.Left/.Right/.Bottom/.Top/.Width/.Height`。

## 源数据提取

当精确复制不可接受、且用户确实需要生成的源码时，先提取结构化数据：

- 页面：`Page.Name`、`SizeWidth/SizeHeight`、`Orientation`。
- 图层：`Layer.Name`、`Visible`、`Editable`、`Printable`、`Color`。
- 形状：`Type`、`Name`、`PositionX/PositionY`、`SizeWidth/SizeHeight`、`CenterX/CenterY`、`Rotation`。
- 曲线：`Curve.SubPaths` → `SubPath.Segments` → `Segment.StartNode/EndNode`、
  控制柄位置与角度、`SubPath.Closed`。
- 文本：`Text.Type`、`Text.Story`、字体、字号、对齐、字距、行距。
- 外观：`Fill.Type` 与色值、`Outline.Type/Width/Color/Style`。
- 效果：透明度、阴影、立体化、封套、透视、透镜、网状填充。
- 对象数据：`ObjectData` / `ObjectDataEx`。
- 样式与调色板：`Document.StyleSets`、`Document.Palette`。

生成重建代码时，用 `references/prompt-template.md` 作为紧凑模板。

## 附带脚本

### 主入口（位图 → CDR）

`scripts/cdr_bitmap_to_cdr.py` —— 一条命令跑完全流程。参数见 `--help`。

```text
python scripts\cdr_bitmap_to_cdr.py --image ref.png --page-width 210 --output out\ref.cdr
python scripts\cdr_bitmap_to_cdr.py --image ref.png --page-size A4 --output out\ref.cdr --trace-only
python scripts\cdr_bitmap_to_cdr.py --report-only --out-dir out\ref_work --output out\ref.cdr
```

退出码：`0` 成功（或 `--trace-only`）；`1` 参数/文件问题；`2` CDR 未生成
（CorelDRAW 不可用、pywin32 缺失等），此时 SVG 与报告仍然有效。

### 位图矢量化三件套（主入口内部会调用，也可单独用）

`scripts/cdr_image_trace.py` —— 标定 + 分区 + 描摹 + 参数寻优：

```text
# 正式生成：显式给区域（坐标是源图像素，左上原点）
python scripts\cdr_image_trace.py --image ref.png --page-width 210 --out svg ^
  --region "art_tools:6,500,1217,900" ^
  --region "logo_vonder:0,901,1217,1059,invert"

# 参数寻优：扫上采样倍数与 potrace 参数，输出各区域 IoU 最优组合
python scripts\cdr_image_trace.py --image ref.png --page-width 210 --out svg --probe

# 自动分区摸底
python scripts\cdr_image_trace.py --image ref.png --page-width 210 --out svg --auto
```

产出 `{region}.svg` + `manifest.json`（含各区域毫米包围盒、尺寸、参数、IoU）。

`scripts/cdr_image_place.py` —— 按清单在 CorelDRAW 中重建：

```text
python scripts\cdr_image_place.py --manifest svg\manifest.json ^
  --output out\cover.cdr --preview out\cover_preview.png ^
  --rect "band:0,155.472,210,27.264,#111111" ^
  --rect-layer BAND --layer-order "BAND,MARK,ART,TEXT" ^
  --layer "logo_vonder=LOGO" --white logo_vonder
```

产出 CDR、预览 PNG 与 `placement.json`（含各元素实际定位误差与内容包围盒）。
注意：`--rect-layer` 指定的名字**必须出现在 `--layer-order` 里**，
否则矩形无处安放会报 KeyError。

`scripts/cdr_visual_diff.py` —— 配准式像素校验：

```text
python scripts\cdr_visual_diff.py --source ref.png ^
  --render out\cover_preview.png --placement out\placement.json --out compare
```

产出整页 IoU / 召回 / 精确、分区域指标、差异叠加图与放大对照图。

### 整图识别（流水线第一步）

`scripts/cdr_scan_text.py` —— 一张图里"哪些是文字、哪些是符号、哪些是图形"：

```text
python scripts\cdr_scan_text.py --image ref.png --out-dir out\scan ^
  --mm-per-px 0.17256 --emit-live out\live_regions.txt --emit-trace out\trace_regions.txt
```

产出 `scan.json`（逐条：框、文本、朝向、置信度、是否可疑）、`scan_preview.png`
（彩色框预览：蓝=正置文字、橙=180° 文字、红=图标、绿=实心块、黄=混合块），
以及两份可直接喂下游的区域清单：

| 清单 | 格式 | 喂给 |
| --- | --- | --- |
| `--emit-live` | `NAME=y0,y1,x0,x1[,#RRGGBB][,rot=180]`（**y 在前**） | `cdr_text_live.py --region` |
| `--emit-trace` | `name:x0,y0,x1,y1[,#RRGGBB]`（**x 在前、冒号分隔**） | `cdr_bitmap_to_cdr.py --region` |

> 两份清单格式不同是历史原因（一个按行优先、一个按列优先），**别手抄**——
> 手抄两份必然不同步。流水线里由 `build_all.py` 从同一份 `scan.json` 派生。

它解决的几个具体问题：

- **调色板自动提取。** 抗锯齿像素是"背景色 → 墨色"的线性混合，所以按
  **方向**（不是欧氏最近色）聚类才能得到精确墨色。用最近色会把黑字的灰边判给
  洋红；用"最暗 5%"会被两色交界的混色带偏（实测拿到 `#383637`，真值是 `#1A1819`）。
- **文字朝向判定。** OCR 对长文本的方向不敏感，必须用**渲染字形的 IoU** 复核
  0° / 180°。上排整体倒置的标签就是靠这个才读得出来。
- **文字框吸附。** OCR 给的是紧框，会切掉相连的笔画（实测倒置字标的 `i` 点
  在 y529..535、OCR 框只到 y531）。`snap_box_to_ink` 把框扩到**与之相连**的
  墨迹边界——是连通分量判定，不是"附近有墨迹就长"，所以不会吞掉相邻元素。
- **块分类。** `solid` / `line_art` / `icon` / `mixed` 决定描摹参数与图层命名。

### 文字转活字（可选，独立一步）

`scripts/cdr_text_live.py` —— 识别文字 + 匹配字体 + 判定能否转成真文本：

```text
python scripts\cdr_text_live.py --image ref.png ^
  --region footer=1083,1100,470,842 --mm-per-px 0.17256 --out-dir out\text
python scripts\cdr_text_live.py --image ref.png --region "t:0,20,0,400" ^
  --mm-per-px 0.17256 --out-dir out\text --corel-only
python scripts\cdr_text_live.py --image ref.png ^
  --region footer=1083,1100,470,842 --mm-per-px 0.17256 --out-dir out\text ^
  --apply out\cover.cdr --replace-traced footer=05_TEXT
```

产出 `live_text.json`（逐区文本 / 选定字体 / IoU / lift / **四条判据取值** / 判定）、
`font_match_<区>.json`、`compare_<区>.png` 三联对照图。
**`verdict` 为 `keep_trace` 时不要转**，保留描摹轮廓并在报告里说明；
同时看 `reject_kind` 区分是"库里没有接近的"（`weak_match`）
还是"这块根本不是文字"（`not_text`，该去查区域切分）。

活字**单独放 `<区名>_LIVE` 图层**，与描摹轮廓分层——万一字体猜错，
用户能一眼看出是哪层、整层删掉，不污染描摹结果。

**大小写不靠 OCR 定，靠"行内字高"量。** `c o s v w x z` 这六个字母上下同形、
只差高度，OCR 在小字号二值图上会稳定地把大写读成小写（实测页脚 `O.V.D.` 被读成
`O.v.D.`，6 个二值化变体**全错**，多数投票救不了）。脚本对这六个字母额外做一次
几何裁决：行内用无升降部小写字母的高度中位数定 `x-height`、用大写字母定
`cap-height`，把待判字母归到更近的一边。**必须行内相对**（绝对阈值会把 16px 字里
的 `m/r/a/e/s` 全判成大写，IoU 0.606→0.36），**只能用高度不能用宽度**
（相邻的弱墨迹会污染列段宽度）。安全约束是宁可漏判不可错判：
锚点不足、该行区分度不够、或高度差不到 1px 一律不动。
日志里会写依据 `v->V(高12: x9/cap12)`，可直接与源图核对。

> **⚠️ 相似度不能拿"整行 IoU"算。** 整行比要把渲染结果横向拉伸到源同宽，
> 字形位置误差**累积**，小字号笔画只有 1~2px，分数就由"拉伸相位"决定——
> 实测同一字体只改一个字母大小写：错的文本 0.7244、对的 0.5090，**方向是反的**，
> 还会把字体选成笔画明显更重的那个。现在用**逐词对齐**（切词后逐词紧裁再比），
> 方向与真值一致，且与"墨迹密度"这条独立证据吻合。
> **换度量必须连门槛一起重校**：`final_iou` 0.7244→0.7235 几乎没动，
> 但 `lift` 2.675→**1.68**（中位候选也一起抬高了）；`rebuild_iou` 必须与
> `match_fonts` 同源，否则 lift 是废数。

> **⚠️ 重写目标 CDR 之前必须解除 CorelDRAW 的占用。**
> 文件被占用时是只读的，`SaveAs` / `Save()` 会**静默变成空操作**——日志照常
> 打印"已保存"，盘上却什么都没变；而改名/删除则直接抛
> `PermissionError [WinError 32]`。
>
> 麻烦的是**占用往往是自动化自己造成的**：`cdr_text_live.py --apply` 走
> `cdr_common.open_document()`，刻意"只开不关"（把成果留在 CorelDRAW 窗口里
> 让用户接着改）。于是**下一轮**重建时目标被上一轮留下的文档占着，
> 流水线在第 3 步前就死，报错还只是个 WinError 32。
>
> 现在的做法：`cdr_image_place.py` 在 `SaveAs` 之前、流水线在归档旧文件之前，
> 都先调 `cdr_common.release_document()` 把目标关掉。该函数**只关路径匹配的
> 那一个文档**（按绝对路径规范化比对，同名不同目录不会误伤），且
> **`Dirty=True` 时拒绝关闭**——那种改动是用户的，不能替用户丢。
> 释放失败会明确报错退出，不再静默往下跑。
>
> 落盘核验仍然是最后一道闸：保存前后各取一次 `(大小, mtime_ns)` 指纹，
> 没变就报错并返回**退出码 3**。指纹没变但文件**能**独占打开时按成功处理
> （那是"本次保存无内容可写"，不是故障）。

### CDR 剖析与重建

```text
python scripts\cdr_prompt_builder.py --source input.cdr --output outputs\input-redraw-prompt.md
python scripts\cdr_redraw.py --source input.cdr --output outputs\redraw_exact.cdr --mode clone
```

`scripts/cdr_common.py` 提供连接、重试、遍历与统计逻辑（被上述脚本导入）。

### 依赖与自检

```text
python -m pip install numpy opencv-python pillow potracer
python -m pip install pywin32          # 只有要建 CDR 时才需要
python scripts\selftest_offline.py     # 无 CorelDRAW 环境的离线回归测试（196 项断言）
```

文字转活字另需 OCR（离线，无需联网）：

```text
python -m pip install --no-deps rapidocr-onnxruntime
python -m pip install onnxruntime pyclipper shapely six flatbuffers protobuf pyyaml
```

注意两点：`rapidocr` 声明的依赖是 `opencv-python`（非 headless），直接装会试图替换
已有的 `cv2` 并被安全删除拦截，所以要用 `--no-deps` 分两步装；
Windows 上 `onnxruntime` 还缺 `vcruntime140_1.dll` 与 `msvcp140_1.dll`
（前者 Python 安装目录自带），缺了会报 `DLL load failed`。
用 `pefile` 查 `.pyd` 的导入表能直接看出缺哪个。详见 `references/live-text-design.md`。

> **⚠️ 依赖装到哪个解释器，就必须用哪个解释器跑脚本。**
> 上面几条 `pip install` 装进的是**当前** `python` 所属的环境。若机器上有多个
> Python（隔离环境、conda、系统 Python 并存），用另一个解释器跑脚本会直接
> `ModuleNotFoundError: No module named 'numpy'` —— 而这个报错看起来像"没装依赖"，
> 很容易被误判成安装失败、白折腾一轮。
> 排查口径：报错时先 `python -c "import sys; print(sys.executable)"` 确认是哪个解释器，
> 再确认依赖装在了哪个。**跑脚本时始终用装了依赖的那个绝对路径**，
> 不要依赖 `PATH` 里的 `python`。

`selftest_offline.py` 覆盖七块：纯逻辑（提示词渲染、CDR 结构比对）；
**用一张几何已知的合成图**验证自动分区的每一处坑（残留剥离、外沿外扩、
满版色带贯通判据、行中位数、列方向切分、超采样度量的偏差量级）；
文字转活字的判定（连通分量、文字/线稿的统计特征、四条判据的边界）；
OCR 的预处理与变体选择（Otsu 平台期、二值化极性、变体选择规则）；
保存落盘核验与**占用释放**（文件指纹写了必须变、没写必须不变；
`release_document` 按绝对路径匹配、同名不同目录不误伤、
**`Dirty=True` 必须拒绝关闭**）；
**字形级纠错与字体相似度**（灰度/布尔切段必须一致、逐词对齐不受整行相位影响、
字高判据正反两向 + 五条安全约束 + 传字符串必须抛错）；
**整图识别**（调色板按方向聚类不被抗锯齿拆成假色、按色分离不串色、
块包围盒是紧框、文字朝向判定、OCR 框吸附相连笔画但不吞相邻元素）。
**夹具全部用实测值而不是编的数**——包括那张"置信度会选错"的变体表。

改动 `auto_partition` / `strip_residue` / `_tighten` / `rasterize` /
`cc_sizes` / `decide_convert` / `_otsu_gray` / `binarize` / `pick_variant` /
`_stat_sig` / `_as_mask` / `_col_runs` / `_glyph_h` / `case_by_height` /
`_word_aligned_iou` / `match_fonts`
之后**必须重跑它**——这些函数的错误在真实图上
往往只表现为"某块内容描歪了""多了一行不该有的文字""某个字大小写错了"，
肉眼很难定位。

> **这个仓库里最贵的一类 bug 是"不报错、只是安静地不干活"**：
> `doc.Save()` 被占用时静默变成空操作、灰度数组当布尔掩膜用、
> 调用方把整串文本当词列表传。三个都踩过，都不抛异常、都只是结果为空。
> **凡是"某种输入下会静默返回空"的函数，都要先问一句：这个空是真的没意见，
> 还是参数根本没对上？** 对应的修法是：参数类型不对就**直接抛错**，
> 不要"顺手兼容一下"把调用方的错掩盖掉。

## 参考文档

- `references/raster-to-vector-notes.md`：**位图矢量化必读**。potracer 的 invert 约定、
  evenodd、`Z M` 分隔符、上采样对细部保真的影响、`alphamax=0` 的多边形陷阱、
  阈值决定笔画粗细、用面积比判粗细、对照图必须同尺度、X8 的 `ExportEx` 缺陷、
  配准校验方法与交付自查清单；§9 是 CorelDRAW 内置 PowerTRACE 的完整实测
  （含一次判断错误的自我纠错记录）。
- `references/live-text-design.md`：**文字转活字必读**。OCR 多套预处理投票、
  列投影切词、字形级纠错、字体匹配打分、为什么不能用绝对 IoU 判定、
  **为什么必须补"这是不是文字"的硬门槛**（含把插图区误判成 `'wander'` 的完整反例）、
  CDR 侧字号与宽度分开解、依赖安装的两个坑、适用边界与已知限制。
- `references/coreldraw-object-model.md`：CorelDRAW X8 COM 对象模型、枚举与跨文档复制要点。
- `references/prompt-template.md`：定制提示词的完整模板与骨架。
