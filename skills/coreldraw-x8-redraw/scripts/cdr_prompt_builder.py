"""剖析 CorelDRAW 源文件，生成一份"定制重绘提示词"（*-redraw-prompt.md）。

用法：
    python cdr_prompt_builder.py --source input.cdr --output outputs/input-redraw-prompt.md
    python cdr_prompt_builder.py --source input.cdr --progid CorelDRAW.Application.18

该脚本对应 AutoCAD 技能里的 dwg_prompt_builder.py：
先把源文件"指纹"（页面、图层、形状、文本、类型分布）完整提取出来，
再据此生成一份可复用的重绘提示词，供后续 cdr_redraw.py 或人工审阅使用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cdr_common import (
    DEFAULT_PROGID,
    connect_coreldraw,
    markdown_table,
    open_document,
    profile_document,
    release_optimization,
    unit_label,
)


def render_prompt(source_path: Path, profile: dict, doc, unit_text: str) -> str:
    """把文档剖析结果渲染为 Markdown 提示词。"""
    basename = source_path.stem
    try:
        file_size = source_path.stat().st_size
    except OSError:
        file_size = "未知"

    # 逐页 / 逐图层明细
    layer_rows = []
    page_rows = []
    for page in profile["pages"]:
        page_rows.append({
            "Page": page["name"],
            "Width": page["width"],
            "Height": page["height"],
            "Orientation": page["orientation"],
            "Layers": page["layer_count"],
            "Shapes": page["shape_total"],
        })
        for layer in page["layers"]:
            layer_rows.append({
                "Page": page["name"],
                "Layer": layer["name"],
                "Visible": layer["visible"],
                "Editable": layer["editable"],
                "Printable": layer["printable"],
                "TopLevel": layer["top_level"],
                "TotalAll": layer["total_all"],
            })

    # 全文档形状类型分布（对 Counter 与普通 dict 都安全）
    type_counts = profile["overall_type_counts"]
    ordered = sorted(type_counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    type_rows = [{"ShapeType": name, "Count": count} for name, count in ordered]

    # 文本清单（跨页收集，最多 100 条）
    text_rows = []
    for page in profile["pages"]:
        for layer in page["layers"]:
            for item in layer["text_items"]:
                if len(text_rows) >= 100:
                    break
                text_rows.append({"Page": page["name"], "Layer": layer["name"], "Text": item})

    text_section = markdown_table(["Page", "Layer", "Text"], text_rows) if text_rows else "_（未检测到文本形状，或文本内容为空）_"

    return f"""# {basename} 定制 CDR 重绘提示词

> 由 `cdr_prompt_builder.py` 基于 CorelDRAW COM 剖析自动生成。请先审阅本文件的
> "文件指纹"，确认无误后再执行重绘。

## 1. 文件指纹

- 源文件：`{source_path.name}`
- 文件大小：{file_size} 字节
- CorelDRAW 文档名：`{profile['name']}`
- 完整路径：`{profile['full_file_name']}`
- 文档单位：{unit_text}（原始代码 {profile['unit_code']}）
- 页面总数：{profile['page_count']}
- 形状总数（含群组内部）：{profile['shape_total']}

## 2. 重绘策略

- 最终交付优先使用 CorelDRAW COM 的**形状级精确复制**（`Shape.CopyToLayer`）。
- 复制全部页面与全部图层，除非用户明确要求仅处理某一页 / 某一图层。
- 不得省略文本、位图、群组、表格、度量、艺术笔、符号、网状填充等任何形状类型。
- 不得覆盖源文件；所有输出写入 `outputs/` 或用户指定目录。
- 若需要"可审计的源码级重建"，先从本文件第 6 节提取结构化数据。

## 3. 推荐命令

生成本提示词：

```powershell
python scripts\\cdr_prompt_builder.py --source "{source_path.name}" --output "outputs\\{basename}-redraw-prompt.md"
```

执行精确重绘：

```powershell
python scripts\\cdr_redraw.py --source "{source_path.name}" --output "outputs\\{basename}_redraw_exact.cdr" --mode clone
```

## 4. 页面清单

{markdown_table(["Page", "Width", "Height", "Orientation", "Layers", "Shapes"], page_rows)}

## 5. 图层清单

{markdown_table(["Page", "Layer", "Visible", "Editable", "Printable", "TopLevel", "TotalAll"], layer_rows)}

## 6. 形状类型分布（全文档）

{markdown_table(["ShapeType", "Count"], type_rows)}

## 7. 文本清单

{text_section}

## 8. 代码级重建所需数据

当需要生成可审计的重建代码（而非直接复制形状）时，请先从源文件提取：

- 每个页面的尺寸、方向与页面顺序。
- 每个图层的名称、可见性、可编辑性、可打印性、图层颜色。
- 形状逐个信息：
  - `Type`（形状类型）与 `Name`（形状名）。
  - 边界框：`PositionX/PositionY`、`SizeWidth/SizeHeight`、`CenterX/CenterY`。
  - 填充：`Fill.Type`、纯色 / 渐变 / 图案 / 纹理 / PostScript 的色值与参数。
  - 轮廓：`Outline.Type`、宽度、颜色、线型、线帽、箭头、虚线。
  - 曲线：`Curve.SubPaths` → `SubPath.Segments` → `Node` 坐标与控制柄、是否闭合。
  - 文本：`Text.Type`（美术字 / 段落）、`Text.Story`、字体、字号、字距、行距、对齐。
  - 群组：`Shapes` 子集合的嵌套结构。
  - 效果：透明度、阴影、立体化、封套、透视、网状填充、透镜。
  - 对象数据：`ObjectData` 与 `ObjectDataEx` 中的字段。
- 样式表：`Document.StyleSets`、图形样式、文本样式、颜色样式。
- 调色板：`Document.Palette` 中的颜色列表。

## 9. 校验标准

- 目标文件页面数等于源文件页面数：{profile['page_count']}
- 每个页面的图层数与图层名称集合一致。
- 每个图层的顶层形状数一致。
- 每个图层的形状总数（含群组内部）一致。
- 全文档形状类型分布一致。
- 文本内容逐条一致（美术字与段落文本分类一致）。
- 在 CorelDRAW 中打开后，页面尺寸、方向与版面视觉一致。
- 群组嵌套关系、填充与轮廓外观一致。

## 10. 已知风险

- 非官方"精简版 / 绿色版"CorelDRAW 常缺失 COM 注册与 VBA 组件，会导致
  `Invalid class string` 或 `ActiveX component can't create object`。
- 部分效果（阴影、立体化、网状填充、透镜）在跨文档复制时可能被简化。
- 位图链接：外部链接的位图需一并复制，否则会变成断链占位。
- 字体缺失：源文件使用的字体若未安装，文本会回退为替代字体。
- 不同 CorelDRAW 版本对 `CopyToLayer` 的跨文档支持存在差异，务必校验。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="剖析 CorelDRAW 源文件并生成定制重绘提示词。")
    parser.add_argument("--source", required=True, help="源 CDR 文件。")
    parser.add_argument("--output", help="输出 Markdown 提示词路径。")
    parser.add_argument("--progid", default=DEFAULT_PROGID, help=f"CorelDRAW ProgID，默认 {DEFAULT_PROGID}。")
    parser.add_argument("--invisible", action="store_true", help="以不可见方式运行 CorelDRAW。")
    args = parser.parse_args()

    source_path = Path(args.source).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"找不到源文件：{source_path}")

    output_path = (
        Path(args.output).resolve()
        if args.output
        else source_path.with_name(f"{source_path.stem}-redraw-prompt.md")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    app = connect_coreldraw(progid=args.progid, visible=not args.invisible)
    try:
        doc = open_document(app, source_path)
        profile = profile_document(doc)
        unit_text = unit_label(doc)
        prompt = render_prompt(source_path, profile, doc, unit_text)
    finally:
        release_optimization(app)

    output_path.write_text(prompt, encoding="utf-8")
    print(f"已写出定制重绘提示词：{output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"提示词生成失败：{exc}", file=sys.stderr)
        raise
