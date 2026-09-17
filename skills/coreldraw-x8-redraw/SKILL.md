---
name: coreldraw-x8-redraw
description: 通过 COM 自动化操作 CorelDRAW X8（及更高版本），剖析、精确重绘并校验 CDR 文件。当需要重建 / 复刻 / 批量处理 CorelDRAW 文档、提取 CDR 的页面·图层·形状·文本结构、把源 CDR 或 PDF/图片参考重建为可编辑 CDR，或校验重建结果与源文件是否一致时使用。Trigger keywords - CorelDRAW, CorelDRAW X8, CDR, 重绘, 复刻, 重建, redraw, rebuild, vector graphics automation, VGCore, pywin32。
agent_created: true
---

# CorelDRAW X8 CDR 重绘

用三种输入模式之一，然后校验产出的 CDR：

1. **源 CDR 模式**：剖析 CDR，生成标准化的文件专属提示词，并精确重建。
2. **PDF 派生模式**：把 PDF 转成可审计的中间 CDR，再用源 CDR 流程重建并校验。
3. **图片 + 尺寸模式**：把参考图片与权威尺寸归一化为参数规格，生成矢量几何并校验。

本技能与 AutoCAD 的 `autocad-dwg-redraw` 技能同构：**剖析 → 生成提示词 → 重建 → 校验**。
区别在于操作对象是 CorelDRAW 的文档 / 页面 / 图层 / 形状模型，而非 DWG 的 ModelSpace / PaperSpace。

## 核心原则

源 CDR 可用时，**不要**仅凭截图或视觉风格去推断。先提取或复制真实形状，再与原始文件校验。

最终交付优先使用 CorelDRAW COM 的**形状级精确复制**（`Shape.CopyToLayer`）。
只有在用户明确需要可审计源码、且已提取完整结构化数据时，才生成重建代码。

图片 + 尺寸输入时，把用户给出的尺寸、单位、数量与版面约束视为权威；
像素只用于判断拓扑、顺序与视觉关系。当图片与给定尺寸冲突时，以尺寸为准并报告冲突。

PDF 输入时，除非手上有原始 CDR，否则**不得**声称与源文件完全一致。
先判断 PDF 内容构成：可提取矢量路径、嵌入位图、可提取文本，还是混合；
把证据转成中间 CDR，记录转换限制，再走常规 CDR 校验流程。

## 源 CDR 流程

用户提供 CDR 时，按以下可重复流程执行：

1. **剖析源 CDR**
   ```powershell
   python scripts\cdr_prompt_builder.py --source input.cdr --output outputs\input-redraw-prompt.md
   ```
2. **审阅生成的定制提示词**
   确认文件名、页面数、页面尺寸、图层表、形状类型分布、文本清单与风险提示。
3. **创建精确重绘**
   ```powershell
   python scripts\cdr_redraw.py --source input.cdr --output outputs\redraw_exact.cdr --mode clone
   ```
4. **校验**
   比较源与目标的页面数、每页图层数与图层名、每图层顶层形状数与形状总数、
   全文档形状类型分布、文本内容与视觉版面。

## PDF 派生 CDR 流程

用户提供 PDF 且没有源 CDR 时使用。目标是从 PDF 证据得到可复现的 CDR 重绘，
再以常规流程对中间 CDR 校验。

1. **检查 PDF 内容**
   - 记录页数、页面尺寸、单位（若可知）、元数据、可提取文本数、嵌入位图数、
     是否存在矢量绘制路径。
   - 有矢量路径时优先用矢量路径，而非栅格描摹。
   - PDF 以栅格为主时，高 DPI 渲染后当作视觉证据，而非精确源数据。
2. **创建中间 CDR**
   - 矢量 PDF：把路径转成 CDR 矢量对象，用页面尺寸或图纸尺寸标定单位。
   - 栅格 / 混合 PDF：高 DPI 渲染 + 线稿矢量化 + 文本提取。只有可提取或经
     OCR 确认的文本才写成真正的文本对象；否则保留为描摹几何或标记为不确定。
   - 除非 PDF 或用户给出图层规范，否则使用通用图层：
     `LINEWORK`、`TEXT`、`BORDER`、`DIM`、`CENTER`、`CONSTRUCTION`。
   - 中间 CDR 另存为新文件，不覆盖 PDF 与任何原始 CDR。
3. **剖析中间 CDR**
   ```powershell
   python scripts\cdr_prompt_builder.py --source intermediate.cdr --output outputs\intermediate-redraw-prompt.md
   ```
4. **用源 CDR 流程重建并校验**
   ```powershell
   python scripts\cdr_redraw.py --source intermediate.cdr --output outputs\redraw_from_pdf.cdr --mode clone
   ```
   校验应以中间 CDR 的计数与分布为准，同时把中间 CDR 与最终 CDR 都和原 PDF 做视觉比对。
5. **报告转换限制**
   说明几何来自矢量路径、栅格描摹、OCR / 文本提取还是推断；指出不可读文本、
   不可编辑的描摹文本、近似曲线、缺失尺寸与任何比例假设。

## 图片 + 尺寸流程

用户提供 PNG / JPG 参考图外加书面尺寸或参数表时使用。

1. **归一化输入规格**
   记录图片路径、单位、总体尺寸、重复模数尺寸、厚度、偏移、特征数量、内部布局、
   所需视图、图层、标注、3D 需求、输出路径与容差。缺失值一律显式标为未知，不要静默猜测。
2. **确定几何权威顺序**
   先书面尺寸，再派生算术，最后图片比例。绘制前先确认净空与重复模数合计。
3. **规划 CorelDRAW 构建**
   优先用 VBA 做可审计的参数化构建；Python 只用于生成参数文件或编排 COM。
   确定性地创建所需图层、文本 / 轮廓样式、群组与保存目标。
4. **确定性绘制**
   按"环境准备 → 主轮廓 → 内部几何 → 标注 → 效果"分阶段构建，保证同一参数产出同一文件。
5. **校验并交付**
   检查尺寸为正、算术合计正确、对象边界合理、无意外重叠、所需图层齐全、
   形状 / 群组计数正确、最终 CDR 保存成功。与参考图做视觉比对，但不推翻权威尺寸。

### 最小参数规格

- 参考图片路径。
- 单位系统与总体宽、高、深（若适用）。
- 构件尺寸、厚度、偏移、数量与重复间距规则。
- 所需视图、图层、标注、文本、群组。
- 输出 CDR 路径。
- 对尺寸未覆盖细节的显式假设与容差。

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

手动撰写定制提示词时，以 `references/prompt-template.md` 为模板。
有 CorelDRAW COM 可用时，优先用 `scripts/cdr_prompt_builder.py` 自动生成。

## 精度要求

- 复制全部页面与全部图层，除非用户明确要求仅处理某页或某图层。
- 保留文本、位图、群组、表格、度量、艺术笔、符号、网状填充、填充、轮廓、效果与对象数据。
- 源文件含有的文本、群组或效果若在目标中缺失，视为校验失败。
- 不覆盖源 CDR。始终写入新的输出路径（脚本会自动为已存在的路径追加时间戳）。
- 文件使用链接位图、自定义效果或跨文档引用时，报告风险并在 CorelDRAW 中做视觉校验。
- PDF 派生模式中，明确区分精确矢量转换、栅格 / 矢量描摹、文本提取 / OCR 与推断几何。
- 不得把 PDF 派生的中间 CDR 说成原始 CDR 的精确副本。
- 图片 + 尺寸模式中，拒绝非正尺寸、越界构件、无法解释的重叠与不符合总体尺寸的算术合计。
- 绝不用图片比例替代给定的数值尺寸。
- 保留参数块或参数文件，使文件可用变更后的尺寸重新生成。

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

## 源数据提取

当精确复制不可接受、且用户确实需要生成的源码时，先提取结构化数据：

- 页面：`Page.Name`、`SizeWidth/SizeHeight`、`Orientation`。
- 图层：`Layer.Name`、`Visible`、`Editable`、`Printable`、`Color`。
- 形状：`Type`、`Name`、`PositionX/PositionY`、`SizeWidth/SizeHeight`、
  `CenterX/CenterY`、`Rotation`。
- 曲线：`Curve.SubPaths` → `SubPath.Segments` → `Segment.StartNode/EndNode`、
  控制柄位置与角度、`SubPath.Closed`。
- 文本：`Text.Type`、`Text.Story`、字体、字号、对齐、字距、行距。
- 外观：`Fill.Type` 与色值、`Outline.Type/Width/Color/Style`。
- 效果：透明度、阴影、立体化、封套、透视、透镜、网状填充。
- 对象数据：`ObjectData` / `ObjectDataEx`。
- 样式与调色板：`Document.StyleSets`、`Document.Palette`。

生成重建代码时，用 `references/prompt-template.md` 作为紧凑模板。

## 附带脚本

用 `scripts/cdr_prompt_builder.py` 生成文件专属提示词：

```powershell
python scripts\cdr_prompt_builder.py --source input.cdr --output outputs\input-redraw-prompt.md
python scripts\cdr_prompt_builder.py --source input.cdr --progid CorelDRAW.Application.18
```

用 `scripts/cdr_redraw.py` 做确定性重建与校验：

```powershell
python scripts\cdr_redraw.py --source input.cdr --output outputs\redraw_exact.cdr --mode clone
python scripts\cdr_redraw.py --source input.cdr --output outputs\redraw_copy.cdr --mode duplicate
python scripts\cdr_redraw.py --source input.cdr --output outputs\redraw_exact.cdr --report outputs\validation.json
```

用 `scripts/cdr_common.py` 复用连接、重试、遍历与统计逻辑（被上面两个脚本导入）。

用 `scripts/selftest_offline.py` 在**没有 CorelDRAW 的环境**下做离线冒烟测试，
验证提示词渲染与校验比对逻辑（用桩模块替代 pywin32）：

```powershell
python scripts\selftest_offline.py
```

脚本要求 Windows、CorelDRAW X8 或更高、Python 3.10+ 与 `pywin32`：

```powershell
python -m pip install pywin32
```

## 参考文档

- `references/prompt-template.md`：定制提示词的完整模板与骨架。
- `references/coreldraw-object-model.md`：CorelDRAW X8 COM 对象模型、枚举与跨文档复制要点。
