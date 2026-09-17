---
name: coreldraw-x8-redraw
description: 给一张位图（PNG/JPG 设计稿、扫描件、包装稿导出图），自动操作 CorelDRAW 把它画成矢量图——标定、自动分区、逐区描摹、在 CDR 中建页建图层并精确定位、配准式像素校验，一条命令跑完。也支持源 CDR 剖析重建、PDF 派生、图片+尺寸参数化绘制，以及校验重建结果与源文件是否一致。Trigger keywords - CorelDRAW, CorelDRAW X8, CDR, 位图转矢量, 矢量化, 描摹, 自动绘制, 重绘, 复刻, 重建, redraw, rebuild, trace, vectorize, bitmap to vector, VGCore, pywin32。
agent_created: true
---

# CorelDRAW X8 位图矢量化与 CDR 重绘

## 主用途

**用户提供一张位图，本技能自动操作 CorelDRAW 软件，画出这张位图对应的矢量化图形。**

用户给的是一张图（设计稿截图、扫描件、包装稿导出图、参考照片），要求"照着这张图在
CorelDRAW 里画出来"——走下面的位图矢量化主线，一条命令跑完。

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
```

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

脚本用三个投影信号，按顺序判定：

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
3. **色带拆成两件事**：
   - 垫底的**原生矢量矩形**（颜色从源图暗像素的 RGB 中位数取，实测 vonder 得 `#121011`）
   - 同一框内的**反白描摹**（`invert=True`，描出白字标）

   色带的横向范围用**未剥离残留**的墨迹算——满版底色本来就该顶到页边，
   残留剥离只用于"哪里是内容"的判定，不该把色带缩进去。
4. **列方向收紧 + 切分**：列间空隙超过 `col_gap_frac * 图宽`（默认 0.05）时切分，
   `--no-split` 可关。色带不切，保持满版矩形。
5. **命名**：色带 `band01`（其反白描摹叫 `band01_ink`），其余按形状猜语义——
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
```

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
python scripts\selftest_offline.py     # 无 CorelDRAW 环境的离线冒烟测试
```

## 参考文档

- `references/raster-to-vector-notes.md`：**位图矢量化必读**。potracer 的 invert 约定、
  evenodd、`Z M` 分隔符、上采样对细部保真的影响、`alphamax=0` 的多边形陷阱、
  阈值决定笔画粗细、用面积比判粗细、对照图必须同尺度、X8 的 `ExportEx` 缺陷、
  配准校验方法与交付自查清单。
- `references/coreldraw-object-model.md`：CorelDRAW X8 COM 对象模型、枚举与跨文档复制要点。
- `references/prompt-template.md`：定制提示词的完整模板与骨架。
