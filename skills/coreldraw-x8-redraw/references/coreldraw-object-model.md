# CorelDRAW X8 COM 对象模型速查

本文件供技能执行时按需查阅。CorelDRAW 的 COM 接口（VGCore 类型库）与 AutoCAD 差异较大，
以下为实际操作中最常用的部分。

## 1. 连接与版本

| 版本 | 主版本号 | ProgID |
| --- | --- | --- |
| X4 | 14 | `CorelDRAW.Application.14` |
| X5 | 15 | `CorelDRAW.Application.15` |
| X6 | 16 | `CorelDRAW.Application.16` |
| X7 | 17 | `CorelDRAW.Application.17` |
| **X8** | **18** | **`CorelDRAW.Application.18`** |
| 2018 | 20 | `CorelDRAW.Application.20` |

不指定版本时用 `CorelDRAW.Application`，由系统解析到默认注册版本。

```python
import pythoncom, win32com.client
pythoncom.CoInitialize()
try:
    app = win32com.client.GetActiveObject("CorelDRAW.Application")
except Exception:
    app = win32com.client.Dispatch("CorelDRAW.Application")
app.Visible = True
```

> 注意：非官方"精简版 / 绿色版"往往未注册 COM 与 VBA 组件，会报
> `Invalid class string` 或 `ActiveX component can't create object`。
> 必须使用官方完整安装版。

## 2. 对象层次

```text
Application
├── Documents            (Documents 集合)
│   └── Document
│       ├── Pages        (Pages 集合)
│       │   └── Page
│       │       └── Layers   (Layers 集合)
│       │           └── Layer
│       │               └── Shapes   (Shapes 集合)
│       │                   └── Shape
│       │                       ├── Curve   → SubPaths → SubPath → Segments → Segment → Node
│       │                       ├── Text
│       │                       ├── Fill
│       │                       ├── Outline
│       │                       ├── ObjectData / ObjectDataEx
│       │                       └── Shapes  (群组内部的子形状)
│       ├── Selection / SelectionRange
│       ├── StyleSets
│       └── Palette
```

**索引为 1 基**：`Shapes(1)`、`Pages(1)`、`Layers(1)`、`Documents(1)`。
（`Pages(0)` 是特例，表示"默认页面设置"，不是真实页面。）

## 3. Application 常用成员

| 成员 | 说明 |
| --- | --- |
| `Documents` | 文档集合 |
| `ActiveDocument` | 当前活动文档 |
| `CreateDocument()` | 新建文档，返回 Document |
| `OpenDocument(path)` | 打开文件，返回 Document |
| `CreateCurve(doc)` | 创建独立 Curve 对象 |
| `CreateCMYKColor(c,m,y,k)` | 创建 CMYK 颜色 |
| `CreateRGBColor(r,g,b)` | 创建 RGB 颜色 |
| `Visible` | 是否可见 |
| `Optimization` | `True` 关闭屏幕刷新，加速批量操作 |
| `EventsEnabled` | `False` 关闭事件回调 |
| `Refresh()` | 强制刷新 |
| `VersionMajor` / `VersionMinor` | 版本号 |

## 4. Document 常用成员

| 成员 | 说明 |
| --- | --- |
| `Name` / `FileName` / `FilePath` / `FullFileName` | 文档标识 |
| `Pages` | 页面集合 |
| `ActivePage` | 当前活动页面 |
| `ActiveLayer` | 当前活动图层 |
| `Layers` | 文档图层集合 |
| `Unit` | 文档单位（枚举代码） |
| `Selection` / `SelectionRange` | 当前选择（Shape / ShapeRange） |
| `ClearSelection()` | 取消所有选择 |
| `Save()` / `SaveAs(path)` | 保存 |
| `Close()` | 关闭 |
| `Export(path, filter)` | 简单导出 |
| `ExportEx(...)` | 高可配置导出 |
| `ExportBitmap(...)` | 导出位图 |
| `PublishToPDF(path)` / `PDFSettings` | 发布 PDF |
| `AddPages(n)` / `InsertPages(...)` | 增页 |
| `CreateLayer(name)` | 在活动页面创建图层 |
| `BeginCommandGroup()` / `EndCommandGroup()` | 把一系列操作合并为一次撤销 |
| `StyleSets` | 样式表 |

## 5. Page 常用成员

| 成员 | 说明 |
| --- | --- |
| `Name` | 页面名 |
| `SizeWidth` / `SizeHeight` | 页面宽高（文档单位） |
| `Orientation` | 方向 |
| `SetSize(w, h)` | 设置尺寸 |
| `Layers` | 图层集合 |
| `CreateLayer(name)` | 创建图层 |
| `Shapes` | 页面上所有形状 |
| `ActiveLayer` | 活动图层 |
| `Activate()` | 激活 |
| `Delete()` | 删除 |
| `FindShapes(...)` | 按条件查找形状 |

## 6. Layer 常用成员

| 成员 | 说明 |
| --- | --- |
| `Name` | 图层名 |
| `Shapes` | 图层上的形状集合 |
| `Visible` | 可见性 |
| `Editable` | 可编辑性（`False` = 锁定） |
| `Printable` | 可打印性 |
| `Color` | 图层颜色 |
| `Activate()` | 激活 |
| `CreateRectangle(l, t, r, b)` | 创建矩形（左、上、右、下） |
| `CreateRectangle2(x, y, w, h)` | 创建矩形（左下角 + 宽高） |
| `CreateEllipse(l, t, r, b)` | 创建椭圆（外接框） |
| `CreateEllipse2(cx, cy, rx, ry)` | 创建椭圆（中心 + 半径） |
| `CreateCurve(curve)` | 由 Curve 对象创建曲线形状 |
| `CreateArtisticText(x, y, text)` | 创建美术字 |
| `CreateParagraphText(...)` | 创建段落文本 |
| `Import(path)` | 导入文件到该图层 |

## 7. Shape 常用成员

| 成员 | 说明 |
| --- | --- |
| `Type` | 形状类型（cdrShapeType） |
| `Name` | 形状名 |
| `Layer` | 所属图层 |
| `Curve` | 曲线数据（仅曲线类形状） |
| `Text` | 文本数据（仅文本形状） |
| `Shapes` | 群组内部子形状 |
| `PositionX` / `PositionY` | 位置 |
| `SizeWidth` / `SizeHeight` | 尺寸 |
| `CenterX` / `CenterY` | 中心点 |
| `Rotation` / `RotationCenterX/Y` | 旋转 |
| `Fill` | 填充 |
| `Outline` | 轮廓 |
| `ObjectData` / `ObjectDataEx` | 对象数据 |
| `Selected` | 是否被选中 |
| `CreateSelection()` | 单选该形状 |
| `AddToSelection()` | 加入选择 |
| `Duplicate(x, y)` | 原位 / 偏移复制（同文档） |
| `Clone()` | 克隆（同文档） |
| `CopyToLayer(layer)` | **复制到指定图层（跨文档重建的关键）** |
| `Ungroup()` / `UngroupAll()` | 取消群组 |
| `OrderToFront()` / `OrderToBack()` | 调整 Z 序 |
| `Move(dx, dy)` | 平移 |
| `Rotate(angle)` | 旋转 |
| `Flip(direction)` | 镜像 |
| `AlignToPage(flags)` / `AlignToPageCenter(flags)` | 对齐页面 |

## 8. Shapes 集合

| 成员 | 说明 |
| --- | --- |
| `Count` | 顶层形状数 |
| `Item(i)` / `(i)` | 取第 i 个（1 基） |
| `All` | 包含群组内部所有形状的 ShapeRange |
| `FindShapes(...)` | 条件查找 |
| `CreateSelection()` | 全选 |

## 9. 常用枚举

### cdrShapeType（`Shape.Type`）

数值随版本略有差异，脚本会同时输出原始数值。常见值：

| 值 | 名称 |
| --- | --- |
| 0 | `cdrNoShape` |
| 1 | `cdrRectangleShape` |
| 2 | `cdrEllipseShape` |
| 3 | `cdrCurveShape` |
| 4 | `cdrPolygonShape` |
| 5 | `cdrBitmapShape` |
| 6 | `cdrTextShape` |
| 7 | `cdrGroupShape` |
| 8 | `cdrMeshFillShape` |
| 9 | `cdrEPSShape` |
| 10 | `cdrOLEShape` |
| 11 | `cdrCustomShape` |
| 12 | `cdrSymbolShape` |
| 13 | `cdrPerfectShapeShape` |
| 14 | `cdrConnectorShape` |
| 15 | `cdrArtisticMediaShape` |
| 16 | `cdrDimensionShape` |
| 18 | `cdrTableShape` |

> 更准确的做法：用 `win32com.client.gencache.EnsureDispatch` 生成类型库后，
> 通过 `win32com.client.constants.cdrRectangleShape` 等常量读取真实值。

### cdrTextType（`Shape.Text.Type`）

| 值 | 名称 |
| --- | --- |
| 1 | `cdrArtisticText`（美术字） |
| 2 | `cdrParagraphText`（段落文本） |

### cdrUnit（`Document.Unit`）

| 值 | 名称 |
| --- | --- |
| 1 | `cdrTenthMicron` |
| 2 | `cdrInch` |
| 3 | `cdrFoot` |
| 4 | `cdrMillimeter` |
| 5 | `cdrCentimeter` |
| 6 | `cdrPica` |
| 7 | `cdrPoint` |
| 10 | `cdrPixel` |
| 11 | `cdrMeter` |

> 由于单位枚举存在版本差异，脚本在设置目标文档单位时**直接复制源文档的原始
> 单位代码**，避免误判。

### cdrAlignType（对齐）

| 值 | 名称 |
| --- | --- |
| 1 | `cdrAlignLeft` |
| 2 | `cdrAlignRight` |
| 3 | `cdrAlignHCenter` |
| 4 | `cdrAlignTop` |
| 8 | `cdrAlignBottom` |
| 12 | `cdrAlignVCenter` |

### 导出过滤器（`Document.Export`）

`cdrJPEG`、`cdrPNG`、`cdrGIF`、`cdrBMP`、`cdrTIFF`、`cdrEPS`、`cdrSVG`、`cdrPDF`、`cdrAI`、`cdrWMF`、`cdrEMF`。

## 10. 跨文档复制的实践要点

1. **首选 `Shape.CopyToLayer(target_layer)`**：把源形状复制到目标文档的目标图层。
   这是"精确重建"的核心 API。
2. **`Clone()` / `Duplicate()` 仅限同文档**，不能跨文档。
3. 若 `CopyToLayer` 在某些版本上不支持跨文档，改用以下保底路径：
   - 选中源形状 → 复制到剪贴板 → 激活目标文档 → 粘贴；或
   - 直接文件级复制（`shutil.copy2`），内容 100% 一致。
4. 复制顺序会影响 Z 序，建议按源图层内形状的原始顺序逐个复制。
5. 群组形状作为整体复制即可，`CopyToLayer` 会保留群组嵌套。
6. 跨文档复制后必须重新校验计数，因为部分效果（阴影、立体化、网状填充）
   可能在复制中被简化。

## 11. 稳定性与性能

- 大批量操作前设置 `app.Optimization = True`、`app.EventsEnabled = False`，
  结束后复位并 `app.Refresh()`。
- 用 `Document.BeginCommandGroup()` / `EndCommandGroup()` 把批量操作合并为
  单次撤销，避免撤销栈爆炸。
- CorelDRAW 忙时可能抛 "Call was rejected by callee"，用重试封装处理。
- 自动化过程中不要手动点击 CorelDRAW 窗口，否则会打断 COM 调用。
