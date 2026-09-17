# CorelDRAW CDR Redraw Skills

**给一张位图，自动操作 CorelDRAW 把它画成矢量图。**

一个 WorkBuddy / Codex 技能，通过 **COM 自动化**操作 **CorelDRAW X8（及更高版本）**：
标定 → 自动分区 → 逐区描摹 → 在 CDR 中建页建图层并精确定位 → 配准式像素校验，
**一条命令跑完**。也支持剖析、精确重绘并校验既有 `.cdr` 文件。

与 [autocad-dwg-redraw-skill](https://github.com/pengxiaoan/autocad-dwg-redraw-skill) 同构：
**剖析 → 生成定制提示词 → 重建 → 校验**。

中文教程：[CDR 自动重绘使用教程](docs/图片与CDR自动重绘使用教程.md)

## 一句话上手

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\ref.cdr
```

只用给**源图**和**页面宽度**。脚本自动完成全部六步，产出：

| 产物 | 位置 |
| --- | --- |
| 矢量 CDR | `--output` 指定 |
| 预览 PNG | `<out-dir>\<文件名>_preview.png` |
| 描摹 SVG 与清单 | `<out-dir>\svg\`（每区一个 SVG + `manifest.json`） |
| 定位记录 | CDR 同目录 `placement.json` |
| 配准校验与差异图 | `<out-dir>\compare\`（`overlay_diff.png`、`zoom\`、`metrics.json`） |
| 报告 | `<out-dir>\report.md` + `report.json` |

`<out-dir>` 默认是 CDR 同目录下的 `<文件名>_work`。

常用变体：

```powershell
# 用标准纸型（高度按纸型取，不按图比例推导）
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-size A4 --output out\ref.cdr

# 没有 CorelDRAW 也能跑：只描摹出 SVG 与清单
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\ref.cdr --trace-only

# 裁掉左侧扫描残留后重新标定
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\ref.cdr --crop-left 4

# 自动分区不满意时手工指定（覆盖自动结果）
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\ref.cdr ^
  --region "art:6,500,1217,900" ^
  --region "logo:0,901,1217,1059,invert"

# 只重建报告，不重跑描摹与建 CDR（几秒钟）
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --report-only --out-dir out\ref_work --output out\ref.cdr
```

## 这个技能能做什么

- **位图转矢量 CDR（主用途）**：给 PNG/JPG 参考图，自动分区描摹并在 CorelDRAW 中
  按毫米坐标重建，用配准式像素比对（IoU / 召回 / 精确）与墨迹面积比证明还原度。
- **剖析**任意 CDR：页面、页面尺寸与方向、图层（名称 / 可见性 / 可编辑性 / 可打印性）、
  形状类型分布、文本清单。
- **生成定制重绘提示词**：为每个源文件产出一份 `文件名-redraw-prompt.md`，
  包含完整"文件指纹"与校验标准。
- **精确重建**：通过 `Shape.CopyToLayer` 做形状级复制，产出可编辑、结构对齐的新 CDR。
- **结构校验**：逐页、逐图层比较页面数、图层名集合、顶层形状数、形状总数、
  全文档形状类型分布，输出 PASS / CHECK WARNINGS 与 JSON 报告。

## 自动分区是怎么做到的

几条判据按顺序用，每条都对应一个真实踩过的坑：

1. **行投影切内容带**：行内墨迹像素数 ≥ 4 视为有效行，空白 < 6 行则合并。
2. **带内判定"满版色带"——必须用行的中位数灰度，不要用覆盖率。**
   色带中间常被反白字标掏空，覆盖率会掉到 0.85 以下，用覆盖率判定会把整条色带
   误判成普通内容区。实测同一张图：

   | 判定信号 | 结果 |
   | --- | --- |
   | 行覆盖率 ≥ 0.85 | 碎成 `901..960` + `1037..1059` 两段（错） |
   | **行中位数 < 128** | **`901..1059` 整条**（对） |
   | 行 25 分位 < 128 | 多出 `661..784` 假色带（插图密集区，错） |

3. **再加横向贯通校验**：光有中位数不够——两个并排的实心内容块也能把中位数压到
   128 以下（各占 25% 宽度时暗像素就过半了），于是内容行带被误判成色带，
   输出变成"垫底矩形 + 反白描摹"。满版色带的定义特征是**横向贯通**
   （底色顶到左右页边），所以要求它的行范围内**每一列都有墨迹**。
   实测合成图（600px 宽、两个 150px 实心黑块并排）：中位数判定为色带（错），
   贯通判据判定为内容（对）。加这条判据后真实图的分区结果逐像素不变。
4. **贯穿边缘的扫描残留必须先剥离**，否则每一行都含墨迹像素，行投影永远找不到
   空隙，分区会把本应分开的区块粘成一整块。实测 vonder 图左侧 4px 黑边条正是
   这样把易碎标、插图、页脚粘成了一个 `y 0..901` 的大块。
5. **区域外沿要用宽松墨迹图向外扩**：细笔画的末梢每行只有两三个墨迹像素，
   会被 `min_ink_px` 当空白切掉（实测页脚切掉下缘后召回从 96.69% 掉到 86.65%）。

色带会拆成两件事：垫底的原生矢量**矩形**（颜色自动取自源图暗像素中位数）
+ 同一框内的**反白描摹**。实测自动分区与手工调优结果一致到 0.1mm：

| 区域 | 自动分区（mm） | 手工调优（mm） |
| --- | --- | --- |
| 色带 | 0.000,155.472..210.000,182.736 | 0,155.472..210,182.736 |
| 插图 | 56.253,96.631..170.312,152.539 | 56.253,96.545..170.291,152.604 |
| 易碎标 | 152.539,59.532..194.988,75.924 | 152.474,59.596..195.074,75.924 |
| 页脚 | 81.274,186.878..145.292,189.121 | 81.252,186.921..145.249,189.703 |

这套判据有**离线回归测试**（`scripts/selftest_offline.py`，110 项断言，不需要
CorelDRAW）：用一张几何已知的合成图覆盖残留剥离、外沿外扩、贯通判据、
行中位数、列切分、超采样度量等每一处坑，改坏了立刻报出来。

## 还原度：不要靠眼睛判断

**对照图必须同尺度。** 源图原生密度（如 5.795 px/mm）与 CorelDRAW 导出密度
（如 11.81 px/mm）不同，直接把同一毫米范围的裁切按同倍数并排，导出侧字面会大
一圈，让人以为重绘又大又粗——这是纯尺度假象。

**判断笔画粗细要算墨迹面积比**（与分辨率无关）：矢量几何面积 ÷ 源图墨迹面积，
落在 **0.98~1.02** 即吻合。

> 反面教训：把导出图降采样回源图尺寸再阈值化，抗锯齿边缘会被算成墨迹，
> 细笔画被补边，实测能把 1.00 放大成 1.39，凭空报出"页脚偏粗 39%"的假结论。

实测达标数据（1217×1660 包装稿，U=8 + 逐区寻优）：

| 区域 | 原生分辨率 IoU | 面积比 |
| --- | --- | --- |
| vonder 字标 | 99.51% | 0.999 |
| Fragile 易碎标 | 98.25% | 1.001 |
| 工具群插图 | 97.55% | 1.007 |
| 6pt 页脚文字 | 93.81% | 0.993 |
| **整页配准** | **93.48%**（召回 95.33% / 精确 97.97%） | — |

**全自动流水线（一条命令，同一张图）** —— 分区坐标由脚本自己判出来，
与上面手工调优的毫米包围盒**一致到 0.1 mm**：

| 区域 | 自动选中的阈值 | 原生分辨率 IoU | 面积比 |
| --- | --- | --- | --- |
| `band01_ink`（反白字标） | 138 | 99.51% | 1.000 |
| `mark01`（易碎标） | 128 | 98.47% | 1.006 |
| `art01`（工具群插图） | 128 | 97.62% | 1.015 |
| `text01`（6pt 页脚） | 118 | 93.59% | 0.998 |
| **整页配准** | — | **93.70%**（召回 95.54% / 精确 97.98%） | — |

整页 93.70% 略高于手工版 93.48%，阈值选择（138/128/128/118）也与手工细调吻合。
剩下几个点差在哪：源图最左侧 4px 贯穿全高的裁切残留**有意未复刻**，
差异图 `overlay_diff.png` 里那条红线就是它；其余为描摹固有的 1px 边缘偏差。

## 为什么不用 CorelDRAW 内置的"轮廓描摹/中心线描摹"

X8 内置 PowerTRACE 的宏接口**能跑**，但质量差一个量级，所以不用于主路径。
同一区域（art01，662×326，对齐分辨率后各算 IoU）：

| 方案 | IoU% | 面积比 |
| --- | --- | --- |
| **本技能 potrace（U=8 + 阈值寻优）** | **99.67** | 0.998 |
| PowerTRACE 轮廓-线条图（1× / 2×） | 68.05 / 72.38 | 1.251 / 1.190 |
| PowerTRACE 轮廓-剪贴画（1× / 2×） | 67.49 / 71.77 | 1.231 / 1.173 |
| PowerTRACE 中心线（1× / 2×） | 29.26 / 36.13 | 0.463 / 0.609 |

上采样对它也有用，但远不足以追平。面积比普遍偏离 1，说明它**没有"笔画粗细"
这个可调量**——而阈值寻优正是拿 IoU + 面积比双指标去卡这个的。中心线输出的是
**开放路径 + 描边、无填充**，与"复刻外观"的目标不同，不能替代轮廓描摹。

另外两个硬问题：大位图会让 X8 崩（5296×2608 直接进程消失）；`doc.Export` 在
"被 COM 拉起"的实例上必然失败（连纯矩形都失败，是实例状态问题）。

> 顺带记一次自我纠错：第一轮实测曾得出"`Finish()` 不产出矢量""参数被忽略"，
> **两条都是错的**。`Finish()` 的产物是 `cdrGroupShape = 7`（群组），而我的计数
> 只看了 `Type == 3`（曲线），把它整个漏掉了；"参数不生效"是因为挑了 128 与 255
> 两个恰好同档的值去比。**判某功能无效之前，先确认自己的探针能看见它。**

完整实测数据（接口签名、枚举真值、参数扫描、逐预设对照）见
`skills/coreldraw-x8-redraw/references/raster-to-vector-notes.md` §9。

## 文字能不能转成可编辑的活字

能，而且比"中心线瘦身"更彻底。实测 vonder 页脚那行 6pt 文字：

- 识别出 `O.v.D. Importadora e Distribuidora Ltda. • Curitiba - PR`
- 字体匹配到 **Swis721 Cn BT / Bold**，IoU **0.7244**，领先候选集中位 **2.67 倍**
- 重建为 **1 个文本对象**，文字可编辑；而描摹轮廓是 73 子路径 / 840 段

```powershell
# 只看识别与判定，不碰 CorelDRAW
python skills\coreldraw-x8-redraw\scripts\cdr_text_live.py ^
  --image ref.png --region footer=1083,1100,470,842 ^
  --mm-per-px 0.17256 --out-dir out\text

# 真的建活字，并删掉该区的描摹轮廓
python skills\coreldraw-x8-redraw\scripts\cdr_text_live.py ^
  --image ref.png --region footer=1083,1100,470,842 ^
  --mm-per-px 0.17256 --out-dir out\text ^
  --apply out\cover.cdr --replace-traced footer=05_TEXT
```

`--region` 的坐标直接用流水线报告「分区与参数」表的「源框 px」列。

**⚠️ `--replace-traced` 不是可选项。** 描摹轮廓与活字在**同一位置**，叠加等于
把这一行字加粗一遍。实测同一页脚：

| 方案 | 区域 IoU | 墨迹比 |
| --- | --- | --- |
| 描摹轮廓（原状） | **92.82%** | 1.046 |
| 活字（替代） | 70.50% | 1.141 |
| 两者叠加（不替换） | 77.70% | **1.286** |

叠加**两头不讨好**：IoU 比描摹轮廓低，墨迹比还比源图粗 28.6%。
所以"保留轮廓当保险"是错的。删除只在活字定位误差 ≤ 0.05mm 之后才执行。

整页影响很小（页脚只占 371×16 px / 1217×1660）：**94.65% → 94.49%**，
即用 0.16 个百分点的整页还原度换这一行字可编辑。

**字体猜错比描摹失真更糟**，所以必须看产出的 `verdict`：

| `verdict` | `reject_kind` | 含义与处置 |
| --- | --- | --- |
| `convert` | — | 可以转。报告里写明字体与分数，并注明"字体是形状匹配推断，非源文件信息" |
| `keep_trace` | `weak_match` | 是文字，但库里没有够接近的。保留描摹轮廓，列出最接近的候选 |
| `keep_trace` | `not_text` | **这块根本不是一行文字**，该去查区域切分 |

活字**单独放 `<区名>_LIVE` 图层**，与描摹轮廓分层——万一猜错，用户能一眼看出
是哪一层、整层删掉即可。该图层里已有形状时会**直接跳过**，不叠字也不自动清空。

五个关键设计点：

1. **OCR 必须二值化，而且不能按置信度选变体**。这一条是踩坑踩出来的：早期
   "验证通过"跑在一个人工做的测试裁切上，那个裁切恰好是**二值图**，所以看起来
   一切正常；换回**源图直裁**后 OCR 立刻失效。实测同一区域同一裁切框：

   | 预处理 | 变体数 | 完全正确 |
   | --- | --- | --- |
   | 原始灰度 | 4 | **0** |
   | 二值化（118 / 128 / Otsu） | 12 | **7** |

   灰度全错，且错法一致（`O.V.D.` 大小写错、词间空格被吃掉）。原因是低分辨率
   扫描件的笔画边缘全是抗锯齿，检测器分不清字间浅灰缝隙。更关键的是
   **错误的灰度变体置信度反而最高**（0.921 vs 正确 0.918），所以选择器改用
   **列投影切出的词数**——独立于 OCR 的几何证据，实测 100% 分对；
   置信度只用于打破平局。
   > 教训：**模型自报的置信度不能当选择依据**；**测试夹具不能比真实输入更"干净"**。

2. **相似度不能用绝对 IoU 门槛**。16px 小字即使文本与字体都对，IoU 也只有 0.61；
   用 0.62 的绝对门槛会把完美匹配也拒掉。改用 `lift = 最佳 ÷ 候选中位`（与字号无关）。
3. **但光有相似度不够，必须补"这是不是文字"的硬门槛**。这一条也是踩坑踩出来的：
   把工具插图区当文字区喂进来时，OCR 幻觉出 `'wander'`（6 个字），
   `lift = 1.53`、`IoU = 0.386` —— **两个相似度判据全过**，于是在 CDR 里
   建了一行 `'wander'`。根因是 lift 与 IoU 都在"横向拉伸到同宽"**之后**算，
   而拉伸本身就把最大的差异抹掉了。补两条与字号无关的比值当硬门槛：

   | 判据 | 真页脚实测 | 插图误判区实测 |
   | --- | --- | --- |
   | 连通分量数 ÷ 字符数 | **1.10** | **11.00** |
   | 最佳候选的自然宽度比 | **0.987** | **1.887** |

   两条都过才允许转。它们**互补**：短幻觉靠前者抓（比值被抬高），
   长幻觉靠后者抓（自然宽度远大于源宽度）。

4. **易混符号直接量字形**（`•` 宽高比 1.25 / 墨密度 0.80，`-` 宽高比 4.00），
   大小写改动才走整体 IoU 逐词裁决——单个窄字形只占整行约 1% 面积，全局 IoU 分不出来。

详见 `skills/coreldraw-x8-redraw/references/live-text-design.md`。

## 安装

把 `skills/coreldraw-x8-redraw` 目录放到你的技能目录下：

| 客户端 | 技能目录 |
| --- | --- |
| WorkBuddy | `%USERPROFILE%\.workbuddy-ai\skills\` |
| Codex | `%USERPROFILE%\.codex\skills\` |

```powershell
# 克隆仓库
git clone https://github.com/<your-name>/coreldraw-cdr-redraw-skill.git
cd coreldraw-cdr-redraw-skill

# 复制技能到 WorkBuddy 技能目录
xcopy /E /I skills\coreldraw-x8-redraw "%USERPROFILE%\.workbuddy-ai\skills\coreldraw-x8-redraw"
```

安装后**重启客户端**，让它扫描到新技能。

依赖：

```powershell
python -m pip install numpy opencv-python pillow potracer
python -m pip install pywin32
```

> **必须使用官方完整版 CorelDRAW。** 第三方"精简版 / 绿色版"通常未注册 COM 组件与
> VBA 宿主，会报 `Invalid class string` 或 `ActiveX component can't create object`。

## 使用

对 AI 说：

```
使用 coreldraw-x8-redraw，把 D:\ref.png 画成 CorelDRAW 矢量图，成品宽 210mm，
输出 D:\out\ref.cdr。画完给我还原度指标和已知偏差。
```

或者要重建既有 CDR：

```
使用 coreldraw-x8-redraw。源文件 D:\work\a.cdr。
先剖析并生成定制重绘提示词给我审阅，确认后再重建，最后输出结构校验报告。
保留全部页面/图层/文本/位图/群组；不覆盖源文件；输出到 outputs\。
```

## 脚本

技能内含八个脚本，主入口会调用其余几个。

**主入口（位图 → CDR）：**

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\ref.cdr
```

**位图矢量化三件套**（主入口内部调用，也可单独用）：

```powershell
# 描摹：标定 + 分区 + 参数寻优
python skills\coreldraw-x8-redraw\scripts\cdr_image_trace.py ^
  --image ref.png --page-width 210 --out svg --probe

# 在 CorelDRAW 中重建
python skills\coreldraw-x8-redraw\scripts\cdr_image_place.py ^
  --manifest svg\manifest.json --output out\cover.cdr --preview out\cover_preview.png

# 配准式像素校验
python skills\coreldraw-x8-redraw\scripts\cdr_visual_diff.py ^
  --source ref.png --render out\cover_preview.png ^
  --placement out\placement.json --out compare
```

**文字转活字（可选，独立一步）：**

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_text_live.py ^
  --image ref.png --region footer=0,17,0,372 ^
  --mm-per-px 0.17256 --out-dir out\text
```

识别文字 → 匹配系统字体 → 判定能否转成真文本。产出 `live_text.json`、
`font_match_<区>.json` 与三联对照图。**`verdict` 为 `keep_trace` 时不要转**，
保留描摹轮廓。

**CDR 剖析与重建：**

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_prompt_builder.py ^
  --source input.cdr --output outputs\input-redraw-prompt.md

python skills\coreldraw-x8-redraw\scripts\cdr_redraw.py ^
  --source input.cdr --output outputs\redraw_exact.cdr --mode clone
```

**离线回归测试**（不需要 CorelDRAW，110 项断言）：

```powershell
python skills\coreldraw-x8-redraw\scripts\selftest_offline.py
```

覆盖两块：纯逻辑（提示词渲染、CDR 结构比对），以及**用一张几何已知的合成图**
验证自动分区的每一处坑——残留剥离、外沿外扩、满版色带贯通判据、行中位数、
列方向切分、超采样度量的偏差量级。改坏了立刻报出来。

## 目录

```
skills/coreldraw-x8-redraw/
├── SKILL.md                          技能定义与完整流程
├── scripts/
│   ├── cdr_bitmap_to_cdr.py          ★ 主入口：位图 → CDR 一条命令
│   ├── cdr_image_trace.py            标定 + 分区 + 描摹 + 参数寻优
│   ├── cdr_image_place.py            按清单在 CorelDRAW 中重建
│   ├── cdr_visual_diff.py            配准式像素校验
│   ├── cdr_text_live.py              文字识别 + 字体匹配 → 转可编辑活字
│   ├── cdr_prompt_builder.py         生成文件专属重绘提示词
│   ├── cdr_redraw.py                 形状级精确重建与结构校验
│   ├── cdr_common.py                 COM 连接、重试、遍历、统计
│   └── selftest_offline.py           离线回归测试（110 项断言，含自动分区合成图、活字判定、OCR 预处理）
└── references/
    ├── raster-to-vector-notes.md     ★ 位图矢量化必读（实测踩坑结论）
    │                                 §9 = 内置 PowerTRACE 完整实测
    ├── live-text-design.md           ★ 文字转活字的设计、判据与产出
    ├── coreldraw-object-model.md     X8 COM 对象模型与枚举
    └── prompt-template.md            定制提示词模板
```

## 许可

见 [LICENSE](LICENSE)。
