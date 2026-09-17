# CDR 重绘提示词模板（CorelDRAW X8）

用本模板为任意 CorelDRAW 源文件生成一份可复用的
`文件名-redraw-prompt.md`，用来驱动：

- 通过 CorelDRAW COM 做精确的形状级重建，或
- 在已提取完整结构化数据时，生成可审计的重建代码（VBA / Python）。

## 定制提示词的标准结构

每份定制提示词必须包含以下 11 个部分：

1. **文件指纹**
   - 源文件名、完整路径、文件大小
   - 文档单位（原始代码 + 可读名称）
   - 页面总数、形状总数（含群组内部）
   - 形状类型分布
2. **文件分类**
   - 排版稿 / 单页设计稿 / 多页宣传册 / 标签 / 包装刀模稿 / 未知
   - 判断依据：页面数、图层命名（刀模线、出血、烫金）、文本量、位图比例
3. **全局重绘策略**
   - 最终交付优先使用 CorelDRAW COM 的形状级精确复制
   - 绝不覆盖源文件
   - 保留全部页面、图层、文本、位图、群组、效果
4. **环境**
   - Windows + CorelDRAW X8（或更高）
   - Python 3.10+
   - pywin32
   - ProgID：`CorelDRAW.Application` 或 `CorelDRAW.Application.18`
5. **页面清单**
   - 页面名、宽、高、方向、图层数、形状数
6. **图层清单**
   - 页面、图层名、可见性、可编辑性、可打印性、顶层形状数、形状总数
7. **形状类型分布**
   - 矩形 / 椭圆 / 曲线 / 多边形 / 位图 / 文本 / 群组 / 表格 / 度量 / 艺术笔 / 符号 / 网状填充
8. **文本清单**
   - 美术字与段落文本分别列出，含所在页面与图层
9. **数据提取计划**
   - 页面 / 图层 / 形状属性 / 曲线节点 / 填充 / 轮廓 / 效果 / 对象数据
10. **执行命令**
    - 提示词生成命令
    - 精确重绘命令
11. **校验标准**
    - 页面数一致
    - 每页图层数与图层名一致
    - 每图层顶层形状数与形状总数一致
    - 全文档形状类型分布一致
    - 文本内容逐条一致
    - 视觉版面一致

## 代码级重建所需数据

在生成重建代码之前，先从 CorelDRAW 中提取：

- 文档单位、页面尺寸、方向、页面顺序。
- 图层表：名称、可见性、可编辑性、可打印性、图层颜色。
- 形状逐个属性：
  - `Type`、`Name`
  - 边界框：`PositionX/PositionY`、`SizeWidth/SizeHeight`、`CenterX/CenterY`、`Rotation`、`RotationCenterX/Y`
  - 填充：`Fill.Type`、`Fill.UniformColor`、渐变 / 图案 / 纹理 / PostScript 参数
  - 轮廓：`Outline.Type`、`Outline.Width`、`Outline.Color`、`Outline.Style`、线帽、箭头
  - 曲线：`Curve.SubPaths` → `SubPath.Segments` → `Segment.StartNode` / `EndNode`、
    控制柄位置与角度、`SubPath.Closed`
  - 文本：`Text.Type`、`Text.Story`、`Text.Font`、`Text.Size`、对齐、字距、行距
  - 群组：`Shape.Shapes` 子集合的嵌套结构
  - 效果：透明度、阴影、立体化、封套、透视、透镜、网状填充
  - 对象数据：`ObjectData` / `ObjectDataEx` 字段
- 样式表：`Document.StyleSets`、图形样式、文本样式、颜色样式。
- 调色板：`Document.Palette` 颜色列表。

## 提示词骨架

```text
# 角色
你是一名资深 CorelDRAW 自动化工程师与印前 / 矢量图形专家。

# 任务
为 [文件名] 制定一套定制重绘流程，产出必须与源文件在页面、图层、形状、
文本、填充、轮廓与版面视觉上一致。

# 文件指纹
- 源文件：[文件名]
- 文件大小：[大小]
- 页面数：[数量]
- 形状总数：[数量]
- 形状类型分布：
[类型统计]

# 文件分类
[排版稿 / 单页设计稿 / 多页宣传册 / 标签 / 包装刀模稿 / 未知]
[简要理由]

# 全局重绘策略
- CorelDRAW 可用时，最终交付使用形状级精确复制。
- 不覆盖源文件。
- 保留全部页面、图层、文本、位图、群组与效果。
- 校验源 / 目标的页面、图层与形状计数。

# 环境
- Windows + CorelDRAW X8 或更高
- 单位：[mm/inch]
- ProgID：CorelDRAW.Application.18

# 页面
[粘贴页面表]

# 图层
[粘贴图层表]

# 形状类型分布
[粘贴类型统计]

# 文本
[粘贴文本清单]

# 数据提取计划
- 用 Shape 属性读取边界框与类型。
- 用 Curve.SubPaths / Segments / Node 读取几何。
- 用 Fill / Outline 读取外观。
- 用 ObjectData 读取对象数据。

# 执行命令
生成提示词：
python scripts\cdr_prompt_builder.py --source "[文件名]" --output "outputs\[基名]-redraw-prompt.md"

精确重绘：
python scripts\cdr_redraw.py --source "[文件名]" --output "outputs\[基名]_redraw_exact.cdr" --mode clone

# 要求
1. 不得遗漏任何形状。
2. 不得遗漏文本、位图、群组、表格、度量与效果。
3. 生成代码时使用提取到的精确坐标与属性。
4. 生成代码时优先复用已有的图层 / 样式。
5. 结束后恢复 CorelDRAW 的 Optimization 与 EventsEnabled。
6. 在结尾打印页面、图层与形状计数。
7. 仅在源数据不足处添加 TODO 注释。

# 校验
- 源与目标页面数必须相等。
- 每页图层数与图层名必须一致。
- 每图层顶层形状数与形状总数必须一致。
- 全文档形状类型分布必须一致。
- 文本内容必须逐条一致。
- 打开后页面尺寸、方向与版面必须视觉一致。
```

## 实践指引

- 有源 CDR 且 CorelDRAW 可用时，优先使用形状级精确复制以保证还原度。
- 仅当用户需要可审计源码或参数化重建时，才使用生成代码。
- 若只有截图或 PDF，结果必须标注为"近似重建"，不得声称与原始 CDR 完全一致。
- 跨文档 `CopyToLayer` 的支持情况随版本而异，务必用校验步骤兜底。
