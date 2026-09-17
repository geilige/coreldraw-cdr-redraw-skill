# CDR 自动重绘使用教程

本教程面向第一次使用 `coreldraw-x8-redraw` 技能的人，从环境准备到跑出第一个文件。

## 这个技能的主用途

**给一张位图，自动操作 CorelDRAW 把它画成矢量图。**

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\ref.cdr
```

只用给**源图**和**页面宽度**，脚本自动完成六步（标定 → 边缘残留探测 → 自动分区 →
逐区寻优描摹 → 在 CorelDRAW 中重建 → 配准式校验），产出 CDR、预览 PNG、
描摹 SVG 与清单、定位记录、差异对照图、还原度报告。详见**第二节**。

如果你手上有现成的 `.cdr` / `.pdf` / 图纸尺寸，技能也支持：

| 章节 | 模式 | 适用场景 |
| --- | --- | --- |
| **二** | **位图矢量化（主用途）** | **只有位图，要"照着画进 CDR"** |
| 三 | 源 CDR | 手上有原始 `.cdr` |
| 四 | 图片 + 尺寸 | 截图/草图 + 书面尺寸 |
| 五 | PDF 派生 | 只有 PDF，没有源 CDR |

> 第二节是踩坑最多的一节。它对应主入口 `cdr_bitmap_to_cdr.py`、
> 三个子脚本（`cdr_image_trace.py` / `cdr_image_place.py` / `cdr_visual_diff.py`）
> 和一份实战笔记 `references/raster-to-vector-notes.md`。

---

## 一、准备环境

### 1. 安装官方完整版 CorelDRAW X8 或更高

这是**硬性前提**。COM 自动化依赖 CorelDRAW 在安装时写入的注册表项与类型库。

> ⚠️ 第三方"精简版 / 绿色版 / 便携版"通常删掉了 COM 注册脚本与 VBA 宿主组件。
> 用它们会直接报：
>
> ```
> Invalid class string
> ActiveX component can't create object
> ```
>
> 遇到这两个错误，第一反应就应该是"我装的是不是精简版"。

### 2. 安装 Python 与依赖

```powershell
python --version          # 需要 3.10 或更高
python -m pip install pywin32
```

如果要用**位图矢量化模式**（第六节），再装四个：

```powershell
python -m pip install numpy opencv-python pillow potracer
```

### 3. 验证 COM 通路

```powershell
python -c "import win32com.client as w; app=w.Dispatch('CorelDRAW.Application'); app.Visible=True; print('CorelDRAW 版本', app.VersionMajor, app.VersionMinor)"
```

能打印出版本号（X8 应输出 `18`）就说明通路正常。

### 4. 安装技能

把 `skills/coreldraw-x8-redraw` 复制到你的技能目录：

```powershell
xcopy /E /I skills\coreldraw-x8-redraw "%USERPROFILE%\.workbuddy-ai\skills\coreldraw-x8-redraw"
```

然后**重启客户端**。

### 5. 离线自检（可选）

没装 CorelDRAW 也能验证技能自带的逻辑是否正确：

```powershell
python skills\coreldraw-x8-redraw\scripts\selftest_offline.py
```

应输出 `全部测试通过。`

---

## 二、第一个重绘：源 CDR 模式

### 场景

你有一个 `包装设计稿.cdr`，想生成一份结构完全对齐的新文件。

### 步骤 1：剖析并生成定制提示词

对 AI 说：

```
使用 coreldraw-x8-redraw。源文件 D:\work\包装设计稿.cdr。
先生成定制重绘提示词，输出到 D:\work\outputs\。
```

技能会调用：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_prompt_builder.py ^
  --source "D:\work\包装设计稿.cdr" ^
  --output "D:\work\outputs\包装设计稿-redraw-prompt.md"
```

### 步骤 2：审阅提示词

打开生成的 `包装设计稿-redraw-prompt.md`，重点看这几节：

| 章节 | 看什么 |
| --- | --- |
| 1. 文件指纹 | 页面数、形状总数、单位是否符合预期 |
| 4. 页面清单 | 页面尺寸与方向是否正确 |
| 5. 图层清单 | 图层是否齐全，有没有该锁定的图层 |
| 6. 形状类型分布 | 文本 / 位图 / 曲线数量是否合理 |
| 7. 文本清单 | 有没有乱码或空文本 |
| 10. 已知风险 | 有没有需要特别注意的点 |

**这一步很重要。** 如果指纹就已经不对（比如页面数少了），说明源文件本身有问题，
先解决它，别急着往下跑。

### 步骤 3：执行重绘

```
确认无误，执行精确重绘，输出到 D:\work\outputs\。
```

技能会调用：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_redraw.py ^
  --source "D:\work\包装设计稿.cdr" ^
  --output "D:\work\outputs\包装设计稿_redraw_exact.cdr" ^
  --mode clone
```

### 步骤 4：看校验结果

输出末尾会打印：

```
============================================================
源文件：页面 2，形状 37
目标文件：页面 2，形状 37
校验结果：PASS（结构一致）
```

- **PASS**：页面数、图层名、顶层形状数、形状总数、类型分布全部一致。
- **CHECK WARNINGS**：会逐条列出差异，例如：

```
校验结果：CHECK WARNINGS（存在差异）
  - 图层 图层 1 形状总数不一致：源 18 / 目标 15
  - 全文档形状类型分布不一致：
      TextShape(文本)[6]：源 10 / 目标 7
```

出现警告时，先看是**哪一类形状**少了。如果是文本少了，通常是字体缺失导致的复制失败；
如果是效果类形状少了，通常是阴影 / 立体化在跨文档复制时被简化。

---

## 三、保底方案：文件级复制

如果 `--mode clone` 在你的 CorelDRAW 版本上失败率高（`CopyToLayer` 跨文档不支持），
用文件级复制：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_redraw.py ^
  --source "D:\work\包装设计稿.cdr" ^
  --output "D:\work\outputs\包装设计稿_copy.cdr" ^
  --mode duplicate
```

这个模式直接把源文件复制一份，内容 **100% 一致**，然后再用 COM 打开做结构校验。
它不产出"重建后的可编辑新结构"，但作为回归基线和保底交付非常可靠。

---

## 四、图片 + 尺寸模式

### 场景

客户只给了一张 JPG 效果图，外加几个关键尺寸。

### 怎么提问

```
使用 coreldraw-x8-redraw 的图片+尺寸模式：
- 参考图：D:\ref\效果图.jpg
- 成品尺寸：210 × 297 mm
- 单位：毫米
- 关键尺寸：主标题距上边缘 40mm，Logo 宽 60mm
- 输出：D:\out\redraw.cdr
冲突时以我给的尺寸为准。
```

### 规则

技能会按这个优先级解析几何：

```
书面尺寸  >  派生算术  >  图片比例
```

图片只用来判断**拓扑、顺序、视觉关系**，绝不用来替代数值尺寸。

### 结果定性

因为源是图片，技能会把产出标注为**近似重建**，不会声称与某个 CDR 完全一致。
只有当你提供了足够权威的尺寸与单位时，才会做尺寸驱动的重建。

---

## 五、PDF 派生模式

只有 PDF、没有源 CDR 时：

1. 技能先判断 PDF 的构成：可提取矢量路径 / 嵌入位图 / 可提取文本 / 混合。
2. 矢量 PDF 优先走矢量路径转换；栅格 PDF 高 DPI 渲染后当视觉证据。
3. 生成中间 CDR（**不覆盖**原 PDF）。
4. 再对中间 CDR 走常规重绘与校验流程。
5. 报告里会明确说明几何来自矢量路径、栅格描摹、OCR 还是推断。

> 中间 CDR **不是**原始 CDR 的精确副本，报告里不会这么写。

---

## 六、位图矢量化模式

### 场景

你只有一张图（截图、扫描件、设计稿导出图），要求"照着这张图在 CDR 里画出来"。

### 怎么提问

```
使用 coreldraw-x8-redraw 的位图矢量化模式。
参考图 D:\ref.png，成品尺寸 210×297mm，单位毫米，输出 D:\out\cover.cdr。
严格参照图纸的布局与细节。
```

### 一条命令（推荐）

主入口把六步全串起来了，只用给源图和页面宽度：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\cover.cdr
```

产出：

| 产物 | 位置 |
| --- | --- |
| 矢量 CDR | `--output` 指定 |
| 预览 PNG | `<out-dir>\cover_preview.png` |
| 描摹 SVG 与清单 | `<out-dir>\svg\` |
| 定位记录 | CDR 同目录 `placement.json` |
| 差异对照图与指标 | `<out-dir>\compare\` |
| 报告 | `<out-dir>\report.md` |

`<out-dir>` 默认是 CDR 同目录下的 `cover_work`。

常用参数：

```powershell
# 用标准纸型（高度按纸型取，不按图比例推导）
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-size A4 --output out\cover.cdr

# 没有 CorelDRAW 也能跑：只描摹出 SVG 与清单
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\cover.cdr --trace-only

# 裁掉左侧扫描残留后重新标定
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\cover.cdr --crop-left 4

# 自动分区不满意时手工指定（覆盖自动结果）
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --image ref.png --page-width 210 --output out\cover.cdr ^
  --region "art_tools:6,500,1217,900" ^
  --region "logo_vonder:0,901,1217,1059,invert"

# 只重建报告，不重跑描摹与建 CDR
python skills\coreldraw-x8-redraw\scripts\cdr_bitmap_to_cdr.py ^
  --report-only --out-dir out\cover_work --output out\cover.cdr
```

区域坐标是**源图像素**，左上为原点，格式 `名称:x0,y0,x1,y1[,invert]`。
`invert` 用于**深底上的白字/白图**。

### 分步跑（需要精细控制时）

主入口内部调用三个子脚本，也可以单独用。

**① 描摹** —— 先跑参数寻优，再正式生成：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_image_trace.py ^
  --image ref.png --page-width 210 --out svg --probe

python skills\coreldraw-x8-redraw\scripts\cdr_image_trace.py ^
  --image ref.png --page-width 210 --out svg ^
  --region "art_tools:6,500,1217,900" ^
  --region "logo_vonder:0,901,1217,1059,invert" ^
  --region "mark_fragile:876,338,1137,447" ^
  --region "text_footer:440,1075,870,1106"
```

**② 重建** —— 按清单在 CorelDRAW 里建页、建图层、导入定位：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_image_place.py ^
  --manifest svg\manifest.json ^
  --output out\cover.cdr --preview out\cover_preview.png ^
  --rect "band:0,155.472,210,27.264,#111111" ^
  --rect-layer BAND --layer-order "BAND,MARK,ART,TEXT" ^
  --layer "logo_vonder=LOGO" --white logo_vonder
```

`--rect` 用来画满版色块（色带、底块）——这类元素**不要描摹**，直接用原生矢量矩形，
又准又小。`--white` 指定哪些区域要填白（深底上的反白元素）。
注意 `--rect-layer` 指定的名字**必须出现在 `--layer-order` 里**，否则矩形无处安放。

**③ 校验** —— 配准式像素比对：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_visual_diff.py ^
  --source ref.png --render out\cover_preview.png ^
  --placement out\placement.json --out compare
```

输出整页与分区域的 IoU / 召回 / 精确，以及差异叠加图（深灰=一致、
红=漏画、蓝=多画）和放大对照图。

### 自动分区是怎么判的

主入口用三个投影信号，每条都有坑：

**① 行投影切内容带** —— 行内墨迹像素数 ≥ 4 视为有效行，空白 < 6 行则合并。

**② 判定"满版色带"必须用行的中位数，不要用覆盖率。**
色带中间常被反白字标掏空，覆盖率会掉到 0.85 以下，用覆盖率判定会把整条色带
误判成普通内容区。实测同一张图：

| 判定信号 | 结果 |
| --- | --- |
| 行覆盖率 ≥ 0.85 | 碎成 `901..960` + `1037..1059` 两段（错） |
| **行中位数 < 128** | **`901..1059` 整条**（对） |
| 行 25 分位 < 128 | 多出 `661..784` 假色带（插图密集区，错） |

**③ 贯穿边缘的扫描残留必须先剥离。** 残留条让每一行都含墨迹像素，行投影就
永远找不到空隙，分区会把本应分开的区块粘成一整块。实测左侧 4px 黑边条把
易碎标、插图、页脚粘成了一个 `y 0..901` 的大块。

色带会拆成两件事：垫底的原生矢量**矩形**（颜色自动取自源图暗像素中位数）
+ 同一框内的**反白描摹**。实测自动分区与手工调优的毫米包围盒一致到 0.1mm：

| 区域 | 自动分区（mm） | 手工调优（mm） |
| --- | --- | --- |
| 色带 | 0.000,155.472..210.000,182.736 | 0,155.472..210,182.736 |
| 插图 | 56.253,96.631..170.312,152.539 | 56.253,96.545..170.291,152.604 |
| 易碎标 | 152.539,59.532..194.988,75.924 | 152.474,59.596..195.074,75.924 |
| 页脚 | 81.274,186.878..145.292,189.121 | 81.252,186.921..145.249,189.703 |

自动分区给出的名称（`art01` / `mark01` / `text01`）只是按尺寸猜的**命名启发式**，
不代表真实语义。


### 还原度预期

按**源图原生分辨率**比对（210×297mm 包装封面，源图 1217×1660）：

| 元素类型 | 典型 IoU |
| --- | --- |
| 反白大字号字标 | ≈ 99.5% |
| Fragile 标（含小图标） | ≈ 98.3% |
| 精细线稿插图 | ≈ 97.5% |
| 6pt 级小字 | ≈ 93.8% |
| 整页配准 | ≈ 93.7% |

**达不到 100% 是正常的**——描摹必然在边缘产生 1px 级偏差。
重点是看指标偏低的是"漏画"（召回低）还是"多画/变粗"（精确低）。

全自动流水线（一条命令，脚本自己分区）实测与上表一致，甚至略好：

| 区域 | 自动选中阈值 | 原生分辨率 IoU | 面积比 |
| --- | --- | --- | --- |
| `band01_ink`（反白字标） | 138 | 99.51% | 1.000 |
| `mark01`（易碎标） | 128 | 98.47% | 1.006 |
| `art01`（工具群插图） | 128 | 97.62% | 1.015 |
| `text01`（6pt 页脚） | 118 | 93.59% | 0.998 |
| **整页配准** | — | **93.70%**（召回 95.54% / 精确 97.98%） | — |

自动分区判出的毫米包围盒与手工调优**一致到 0.1 mm**，选中的阈值也与手工细调吻合。
所以先用默认参数跑一条命令，只有明显不对的区域才需要 `--region` 手工接管。

### ⚠️ 别用眼睛判断笔画粗细

源图原生密度（如 5.795 px/mm）与 CorelDRAW 导出密度（如 11.81 px/mm）不同。
把两者裁同一毫米范围后并排，导出图像素数多一倍，**放大同样倍数后显示尺寸不同**，
重绘会显得又大又粗——这是对照图的缺陷，不是几何问题。

要判断粗细，算**墨迹面积比**（与分辨率无关）：

```
矢量几何面积（按 60 px/mm 光栅化数像素）÷ 源图墨迹面积
```

比值落在 0.98~1.02 就说明粗细吻合。本流水线实测最终为 **0.993~1.007**。

> 两个容易踩的测量坑：
> 1. 把导出图降采样到源图尺寸后再阈值化，会给细笔画补边——
>    实测能把 1.00 的面积比放大成 1.39。
> 2. 度量必须算在源图原生网格上。任何更高的网格都要上采样源图，
>    模糊后细笔画在固定阈值下缩水，同样会把重绘误判成偏粗。

### 关键注意

- **标定**：`mm_per_px = 页面宽度mm / 图片宽度px`。若图宽高比与页面宽高比不一致，
  说明源图裁切过，纵向内容会与页面高度有偏差，交付说明里会写清楚。
- **源图边缘残留**：扫描/裁切留下的贯穿全高黑边条**不会**被复刻为设计内容，
  但会在报告里标注，避免被误判为漏画。
- **小字号文字**描摹后是轮廓而非活字。需要可编辑文本的话要确认字体后重建——
  字体猜错比描摹失真更严重。
- 详细的踩坑记录见
  [`references/raster-to-vector-notes.md`](../skills/coreldraw-x8-redraw/references/raster-to-vector-notes.md)。

---

## 七、常见问题

### Q1：报 `Invalid class string`

装的是精简版 CorelDRAW。换官方完整版。

### Q2：报 `Call was rejected by callee`

CorelDRAW 忙。脚本会自动重试。若持续失败，关掉多余的 CorelDRAW 窗口再试。

### Q3：自动化跑到一半被我点了一下窗口就断了

自动化期间**不要操作 CorelDRAW 窗口**。脚本会设置
`Optimization = True` + `EventsEnabled = False` 来减少干扰，但手动点击仍会打断 COM 调用。

### Q4：校验结果里文本数量变少了

源文件用了本机没装的字体。装上对应字体后重跑。

### Q5：位图变成红叉 / 断链

源文件用的是**链接位图**。把链接的图片一起复制到新目录，或先在 CorelDRAW 里
把链接位图嵌入（`位图 → 嵌入`）后重跑。

### Q6：`CopyToLayer` 复制失败

不同 CorelDRAW 版本对跨文档复制的支持不同。用 `--mode duplicate` 保底。

### Q7：形状类型 / 单位数值和我查到的不一样

不同版本的 `cdrShapeType` / `cdrUnit` 枚举数值有差异。脚本的设计是：

- 优先从 CorelDRAW 类型库常量读取真实值；
- 取不到时回退到内置表；
- **报告里始终同时输出原始数值**（如 `TextShape(文本)[6]`），避免误读；
- 设置目标单位时**直接复制源文档的原始单位代码**，不做数字映射。

所以看到 `[6]` 这类方括号里的数字时，以它为准。

### Q8：重新打开 CDR 后，坐标数值大了一截（比如 210 变成 8.27）

`Document.Unit` **不随文件保存**。设成毫米并保存后，重新打开会回到英寸（1）。
几何数据本身是绝对单位、完全没受影响，只是读值单位变了。

读坐标前先设置一次：

```python
doc.Unit = 3      # cdrMillimeter
```

### Q9：`SaveAs` 没报错，但文件不在指定位置

`SaveAs` / `Export` 会按 **CorelDRAW 自己的工作目录**解析相对路径。
调用前把路径转成绝对路径：

```python
doc.SaveAs(os.path.abspath(out_path), None)
```

技能自带的脚本已做此处理；自己写代码时要注意。

### Q10：描摹出来的图形边缘是折线，圆角变成了斜切

`alphamax=0` 会让 potrace 输出**纯多边形**（不做任何平滑）。
注意它的像素 IoU 反而可能最高，只看 IoU 会选出最差的结果。

判断方法：数一下 SVG 里的曲线段与直线段：

```python
print(svg.count("C"), svg.count("L"))   # 曲线段为 0 就是多边形
```

调高 `--alphamax`（0.5~1.0）。

### Q11：小图标（箭头、酒杯之类）描摹后变形

细笔画在原图上只有 1–2 px，直接描摹必然失真。
**解法是描摹前先把灰度上采样 4 倍再二值化**（`--upscale 4`），
不是调 alphamax。实测能把这类图标从"明显失真"救回"形状正确"。

### Q12：位图矢量化后比对 IoU 只有十几个百分点

多半是把**内容包围盒**的导出图直接和**整页**源图比了。
导出图通常是内容区（例如 210×130mm），不是整页 A4。
必须按 `placement.json` 记录的内容包围盒把渲染图贴回整页白底再比——
`cdr_visual_diff.py` 已自动处理。

### Q13：源图边缘有一条黑边，需要画进 CDR 吗

那是扫描/裁切残留，**不要复刻**。`cdr_image_trace.py` 会自动探测并提示，
用 `--crop-left N` 排除即可。但要在交付说明里提一句，避免被误认为漏画。

### Q14：对照图里重绘明显比原图粗，怎么办

先别改参数——**多半是对照图本身有缺陷**。源图原生密度（如 5.795 px/mm）
与导出密度（如 11.81 px/mm）不同，裁同一毫米范围后并排，显示尺寸不一致。

正确做法：

1. 把两张图重采样到**同一 px/mm** 再并排；
2. 用**墨迹面积比**判断粗细，不要靠眼睛（见第六节的说明）。

如果面积比确实偏离 1.0 超过 2%，再调 `--threshold`
（越低越细）并重跑 `--probe`。

### Q15：小图标（酒杯、箭头之类）描摹后变形

见 Q11——提高 `--upscale`（默认已是 8），不要靠调 alphamax 解决。

### Q16：`--probe` 跑得太慢

上采样倍数越大越慢。大区域（整版插图）的 U=8 位图可能有数千万像素，
单次描摹就要一分钟以上。对策：

- 大区域用 `--upscale 4`，小区域用 8；
- 或者分区域分别跑 `--probe`，只扫关心的那个区域；
- 主入口有 `--max-tune-px`（默认 400000）：源像素面积超过它的区域只扫 3 个
  候选阈值而不是 5 个。

### Q17：自动分区把好几块内容粘成了一整块

九成是**源图边缘的贯穿残留**造成的。残留条让每一行都含墨迹像素，
行投影就永远找不到空隙。实测左侧 4px 黑边条把易碎标、插图、页脚粘成了一个
`y 0..901` 的大块。

主入口会自动把"整列/整行几乎全暗（占比 > 0.9）"的边条从投影判定里剔除，
日志里会打印 `已从投影中剔除贯穿边缘的残留：左 4 列…`。
如果残留不是贯穿全高（比如只在下半段），自动剔除可能失效，此时用
`--crop-left N` 手工裁掉后重新标定。

### Q18：逐区寻优选中的阈值总是最粗的那个

说明你用的是**硬边光栅化**在做评估。`cv2.fillPoly` 是"取整后填充"，
会系统性低估矢量覆盖面积，造成"笔画越粗 IoU 越高"的单调偏差。

实测同一区域（U=8）：

| 阈值 | 硬边 IoU% | 超采样 IoU% | 超采样面积比 |
| --- | --- | --- | --- |
| 128 | 94.15 | 99.40 | 1.005 |
| **138** | 94.29 | **99.51** | **1.000** |
| 148 | **94.34（硬边最高）** | 99.35 | 0.996 |

硬边把最优判成 148（过粗），超采样判成 138（正确）；而 CorelDRAW 实际导出
比对是 99.15%，说明超采样才贴近真实。

**修法**：评估时先按 8 倍超采样再盒式降采样得到覆盖率，以 `覆盖率 >= 0.5`
作为墨迹判据。技能内已按此实现。另外记得**按子路径自身包围盒分配小画布**
再异或回总画布，否则 483 子路径的区域在 ss=8 下要做 66 亿次像素操作。

> 这个偏差是 sub-pixel 效应。在远高于源图密度的网格上（如按 60 px/mm
> 数矢量面积算面积比）可以忽略，但在源图原生网格上算 IoU 就必须超采样。

### Q19：文字下缘、图形末梢被切掉了

区域外沿不能只用严格墨迹图（gray<128）来定。细笔画的末梢（文字 descender、
抗锯齿边缘）每行只有两三个墨迹像素，会被 `min_ink_px` 当成空白切掉。
实测页脚墨迹真实范围 y 1083..**1098**，严格图只到 1096，切掉下缘后召回从
96.69% 掉到 86.65%。

技能内用**宽松墨迹图**（gray<200，含抗锯齿过渡像素）逐行/逐列向外扩，
**遇到真正的空白行/列就停**。

> 不要改成无脑 padding：padding 会越过区块间空白伸进相邻区域，
> 把邻块墨迹也描一遍，在 CDR 里产生重复几何。

### Q20：整页比对 IoU 只有九十几，剩下几个点差在哪

按这个顺序排查：

1. **源图边缘残留**没复刻（这是有意为之）。差异图里沿边缘的贯穿红线就是它。
2. **小字**天然失真。6pt 级文字受源图分辨率限制，1px 级边缘差无法消除。
3. **比例不一致**。源图宽高比与页面宽高比不同时，纵向内容高度会小于页高，
   内容区之外的差异不算错。
4. **笔画粗细**用面积比核，不要看 IoU。IoU 对 1px 边缘位移很敏感，
   而面积比才反映粗细（见 Q14）。

### Q21：色带整条不见了，字标变成黑字压在插图上

典型症状：导出图里垫底色带消失、反白字标变成黑字且位置错乱，
但定位日志仍然打印"误差 0.000mm [OK]"。

原因是 **`Layer.Import` 把新形状插到图层"底部"（索引 1），不是追加到末尾**。
如果垫底矩形和反白字标在**同一个图层**，导入后取 `Item(Count)` 会拿到矩形，
于是把字标的尺寸/位置/填充写到了矩形上——两个形状属性互换。

实测对照：

| 形状 | 应该是 | 实际变成 |
| --- | --- | --- |
| `band01`（矩形） | 210 × 27.264 mm，深色 `#121011` | 70.856 × 19.801 mm，白色 |
| `band01_ink`（字标） | 70.856 × 19.801 mm，白色 | 70.852 × 19.775 mm，深色 `#111111` |

日志之所以报 OK，是因为它打印的是**请求值**而不是读回的实际值。

**修法**（技能内已修）：一个区域一个图层，让导入时图层为空；
定位新形状改用"导入前后名称集合的差"；矩形也读回 `PositionX/SizeWidth` 核对。

> 手工配置里矩形和字标天然在不同图层，所以这个 bug 只在自动分区把两者
> 归到同一图层时才暴露。

### Q22：报告里的还原度数字对不上（IoU 显示 24%，实际看着挺好）

报告里的还原度来自**配准校验阶段**，而写报告曾经排在它**之前**，
于是报告写的是校验前的空值/旧值。实测症状：`report.md` 里
`整页 IoU 24.47%`、`召回 37.31%`，而 `compare\metrics.json` 里是
`93.62% / 95.14%`。

两件事都修了：

1. 写报告移到校验**之后**（顺序问题）；
2. 清单 `manifest.json` 补记每区的 `iou/recall/precision/ink_ratio`、
   垫底矩形 `rects`、图层顺序 `layer_order`，报告可脱离描摹独立重建：

```powershell
python scripts\cdr_bitmap_to_cdr.py --report-only ^
  --out-dir out\cover_work --output out\cover.cdr
```

几秒钟出新报告，不必重跑十几分钟描摹。细节见「十、报告为什么可以单独重建」。

---

## 八、脚本参数速查

### cdr_bitmap_to_cdr.py（主入口）

| 参数 | 说明 |
| --- | --- |
| `--image` | 源位图路径（PNG/JPG）；`--report-only` 时可省略 |
| `--output` | 输出 CDR 路径；`--report-only` 时可省略 |
| `--page-width` | 页面宽度（毫米） |
| `--page-height` | 页面高度（毫米）；省略则按图比例推导 |
| `--page-size` | 标准纸型 `A4`/`A3`/`A5`/`Letter` 或 `宽x高`；给定时覆盖上面两项的高度 |
| `--region` | 手工分区 `名称:x0,y0,x1,y1[,invert]`，可重复；给出后不再自动分区 |
| `--rect` | 手工追加垫底矩形 `名称:x,y,w,h,#RRGGBB`（毫米，y 距页顶） |
| `--out-dir` | 中间产物与报告目录；默认 `<CDR 同目录>/<文件名>_work` |
| `--upscale` | 描摹前灰度上采样倍数，默认 **8** |
| `--turdsize` | 斑点面积阈值（源图像素），默认 2 |
| `--alphamax` | 拐角阈值，默认 1.0；**不要用 0** |
| `--opttolerance` | 曲线优化容差，默认 0.1 |
| `--threshold` | 固定二值化阈值；给定时不做逐区寻优 |
| `--tune-thresholds` | 寻优候选阈值，默认 `112,118,128,138,148` |
| `--max-tune-px` | 源像素面积超过此值的区域只扫 3 个候选阈值，默认 200000 |
| `--crop-left` | 忽略源图最左侧 N 列（裁切/扫描残留），并按其后的宽度重新标定 |
| `--no-split` | 不做列方向切分 |
| `--trace-only` | 只描摹出 SVG 与清单，不建 CDR（无 CorelDRAW 也能跑） |
| `--no-validate` | 跳过配准式像素校验 |
| `--report-only` | 不重跑描摹与建 CDR，仅用已有清单与指标重建报告 |
| `--keep-existing` | 不自动关闭同名已打开文档 |
| `--progid` | CorelDRAW ProgID，默认 `CorelDRAW.Application.18` |

`--report-only` 时 `--image` / `--output` 可省略，但需要 `--out-dir`
（或用 `--output` 推导出工作目录）。它的存在理由见下节「十、报告为什么可以单独重建」。

### cdr_prompt_builder.py

| 参数 | 说明 |
| --- | --- |
| `--source` | 必填，源 CDR 路径 |
| `--output` | 输出 Markdown 提示词路径 |
| `--progid` | CorelDRAW ProgID，默认 `CorelDRAW.Application` |
| `--invisible` | 以不可见方式运行 |

### cdr_redraw.py

| 参数 | 说明 |
| --- | --- |
| `--source` | 必填，源 CDR 路径 |
| `--output` | 输出 CDR 路径，默认 `outputs/redraw.cdr` |
| `--mode` | `clone`（形状级重建，默认）/ `duplicate`（文件级复制） |
| `--progid` | CorelDRAW ProgID |
| `--invisible` | 以不可见方式运行 |
| `--keep-source-open` | 结束后保持源文档打开 |
| `--report` | 输出 JSON 校验报告路径 |

> 输出路径已存在时，脚本会自动追加时间戳，**绝不覆盖**已有文件。

### cdr_image_trace.py

| 参数 | 说明 |
| --- | --- |
| `--image` | 必填，参考图路径（PNG/JPG） |
| `--page-width` | 必填，页面宽度（毫米），用于标定 mm/px |
| `--page-height` | 页面高度（毫米）；省略则按图比例推导 |
| `--region` | 区域定义 `名称:x0,y0,x1,y1[,invert]`，可重复；坐标是源图像素 |
| `--auto` | 自动按投影分割区域 |
| `--out` | 输出目录，默认 `svg` |
| `--upscale` | 描摹前灰度上采样倍数，默认 **8**（细部保真的关键；超大区域可用 4 控速） |
| `--turdsize` | 斑点面积阈值（源图像素），默认 2；内部按 `upscale²` 折算 |
| `--alphamax` | 拐角阈值，默认 1.0；**0 = 纯多边形，不要用** |
| `--opttolerance` | 曲线优化容差，默认 0.1 |
| `--threshold` | 二值化阈值，默认 128。**越低笔画越细**；细笔画文字建议用 `--probe` 扫 110~150 |
| `--probe` | 参数扫描模式，只输出候选参数 IoU，不写 SVG |
| `--crop-left` | 忽略源图最左侧 N 列（裁切/扫描残留） |

### cdr_image_place.py

| 参数 | 说明 |
| --- | --- |
| `--manifest` | 必填，`cdr_image_trace.py` 产出的 `manifest.json` |
| `--output` | 必填，输出 CDR 路径 |
| `--preview` | 预览 PNG 路径；省略则不导出 |
| `--progid` | CorelDRAW ProgID，默认 `CorelDRAW.Application.18` |
| `--rect` | 附加原生矢量矩形 `名称:x,y,w,h,#RRGGBB`，可重复（y 为距页顶毫米） |
| `--rect-layer` | 承载 `--rect` 矩形的图层名，默认 `RECT`。**配合 `--layer-order` 时该名必须出现在 order 里**，否则矩形无处安放会报 KeyError |
| `--layer` | 图层映射 `区域名=图层名`，可重复 |
| `--white` | 需要填白的区域名（深底上的反白元素），可重复 |
| `--layer-order` | 图层自下而上的顺序，逗号分隔 |
| `--close-existing` | 构建前关闭名称以这些前缀开头的文档，逗号分隔 |

### cdr_visual_diff.py

| 参数 | 说明 |
| --- | --- |
| `--source` | 必填，源参考图路径 |
| `--render` | 必填，CDR 导出的预览图路径 |
| `--placement` | 必填，`cdr_image_place.py` 产出的 `placement.json` |
| `--out` | 输出目录，默认 `compare` |
| `--width` | 比对用整页位图宽度，默认 2480（约 300dpi A4） |
| `--threshold` | 二值化阈值，默认 128 |

---

## 九、退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 校验通过 |
| `2` | 校验存在差异（CHECK WARNINGS） |
| 其他 | 运行失败，异常信息在 stderr |

适合接入 CI 或批处理脚本。

主入口 `cdr_bitmap_to_cdr.py`：`0` = 成功（建出 CDR，或 `--trace-only` 下描摹成功）；
`1` = 输入有误（找不到源图、没给页面尺寸）；`2` = 描摹成功但没建出 CDR
（CorelDRAW 不可用或保存失败）。**描摹段产物与报告在 `2` 时仍然有效**，
可以直接用 `--trace-only` 重跑或单独调用 `cdr_image_place.py` 补建。

---

## 十、报告为什么可以单独重建

报告里的数字来自流水线的**两个不同阶段**：

| 阶段 | 产出 | 报告里的位置 |
| --- | --- | --- |
| 步骤 4 逐区寻优 + 描摹 | 每区的阈值、IoU、面积比 | 「分区与参数」表 |
| 附加 配准式像素校验 | 整页/分区域 IoU、召回、精确 | 「还原度」表 |

早先的实现把写报告放在校验**之前**，于是报告里的还原度停留在有 bug 时期的
旧值——整页 IoU 显示 **24.47%**，而真实值是 **93.62%**。顺序修好后还有第二个问题：
报告措辞改一个字也要重跑十几分钟描摹，不可接受。

所以清单 `manifest.json` 里补齐了报告所需的全部字段：

```json
{
  "source_image": "...", "page_mm": [210.0, 286.4421], "mm_per_px": 0.17255546,
  "image_px": [1217, 1660], "params": {"upscale": 8, "alphamax": 1.0},
  "page_h_explicit": false,
  "layer_order": ["BAND", "02_INK", "03_MARK", "04_ART", "05_TEXT"],
  "rects": [{"name": "band01", "x": 0, "y": 155.47, "w": 210, "h": 27.26,
             "rgb": [18, 16, 17]}],
  "regions": {
    "band01_ink": {
      "svg": "...", "bbox_mm": [...], "size_mm": [...], "invert": true,
      "source_box_px": [0, 901, 1217, 1059], "subpaths": 13,
      "type": "BAND", "threshold": 138, "kind": "band", "color": [18, 16, 17],
      "iou": 99.51, "recall": 99.76, "precision": 99.75, "ink_ratio": 1.0
    }
  }
}
```

有了这些，报告就能完全脱离描摹过程重建：

```powershell
python scripts\cdr_bitmap_to_cdr.py --report-only ^
  --out-dir out\cover_work --output out\cover.cdr
```

它会读 `svg\manifest.json` 与 `compare\metrics.json`，不必打开源位图
（源图被移动或删除也不影响），几秒钟出报告。

两个细节：

- **缺字段显示 `—`，不显示 0。** 旧清单没有 `iou` 时若按 0 打印，
  会被误读成「完全不像」；按 `—` 打印才是诚实的。
- **垫底矩形能反推。** 若清单里没有 `rects`（旧版清单），
  满版色带的垫底矩形可由该区域的 `bbox_mm` 直接推出，报告不会缺这一段。
