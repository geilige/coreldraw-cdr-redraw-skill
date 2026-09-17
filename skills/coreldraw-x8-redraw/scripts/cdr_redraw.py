"""通过 CorelDRAW COM 重建 CDR 文件并校验。

用法：
    # 形状级精确重建（默认，真正的"重绘"）
    python cdr_redraw.py --source input.cdr --output outputs/redraw_exact.cdr --mode clone

    # 文件级安全复制（保底方案，内容 100% 一致）
    python cdr_redraw.py --source input.cdr --output outputs/redraw_copy.cdr --mode duplicate

    # 指定 CorelDRAW X8 的 ProgID
    python cdr_redraw.py --source input.cdr --output outputs/redraw.cdr --progid CorelDRAW.Application.18

对应 AutoCAD 技能里的 dwg_redraw.py：把源文件重建为新文件，并逐项校验一致性。
两种模式：

- clone：新建文档，按源文件重建页面与图层，再用 Shape.CopyToLayer 逐形状复制，
  产出的是"可编辑、结构对齐"的新文件。这是真正意义上的重绘。
- duplicate：直接文件级复制，内容与源文件完全一致，作为保底 / 回归基线。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from cdr_common import (
    DEFAULT_PROGID,
    com_retry,
    connect_coreldraw,
    create_document,
    iter_layers,
    iter_pages,
    iter_shapes,
    open_document,
    profile_document,
    release_optimization,
    safe_get,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def unique_output_path(path: Path) -> Path:
    """若目标已存在，则追加时间戳，绝不覆盖。"""
    if not path.exists():
        return path
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.stem}_{stamp}{path.suffix}")


def ensure_page_count(doc, count: int) -> None:
    """确保目标文档至少有 count 个页面。"""
    current = com_retry(lambda: doc.Pages.Count)
    if current >= count:
        return
    com_retry(lambda: doc.AddPages(count - current), attempts=10, delay=1.0)


def ensure_layer(page, name: str, source_layer):
    """在目标页面上找到或创建同名图层，并同步可见性 / 可编辑性 / 可打印性。"""
    target = None
    for layer in iter_layers(page):
        if str(safe_get(layer, "Name", "")) == name:
            target = layer
            break
    if target is None:
        target = com_retry(lambda: page.CreateLayer(name), attempts=10, delay=1.0)
    for attr in ("Visible", "Editable", "Printable"):
        try:
            setattr(target, attr, bool(safe_get(source_layer, attr, True)))
        except Exception:  # noqa: BLE001
            pass
    return target


def copy_page(source_page, target_page, label: str) -> tuple[int, int]:
    """把一个页面的全部图层与形状复制到目标页面。

    返回 (尝试复制的形状数, 成功复制的形状数)。
    """
    # 同步页面尺寸与方向
    try:
        target_page.SetSize(source_page.SizeWidth, source_page.SizeHeight)
    except Exception as exc:  # noqa: BLE001
        print(f"  提示：{label} 设置页面尺寸失败：{exc}")
    try:
        target_page.Orientation = source_page.Orientation
    except Exception:  # noqa: BLE001
        pass

    attempted = 0
    succeeded = 0
    for source_layer in iter_layers(source_page):
        layer_name = str(safe_get(source_layer, "Name", ""))
        target_layer = ensure_layer(target_page, layer_name, source_layer)
        for shape in iter_shapes(source_layer):
            attempted += 1
            try:
                com_retry(lambda s=shape, t=target_layer: s.CopyToLayer(t),
                          attempts=6, delay=0.5)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  警告：{label} 形状复制失败（{type(exc).__name__}）：{exc}")
    return attempted, succeeded


def rebuild_via_clone(source_doc, target_doc) -> None:
    """按源文档重建页面与图层，并逐形状复制。"""
    source_pages = list(iter_pages(source_doc))
    ensure_page_count(target_doc, len(source_pages))

    # 让目标文档使用与源文档相同的单位（直接复制原始单位代码，避免枚举误判）
    try:
        target_doc.Unit = safe_get(source_doc, "Unit")
    except Exception:  # noqa: BLE001
        pass

    target_pages = list(iter_pages(target_doc))
    total_attempted = 0
    total_succeeded = 0
    for index, source_page in enumerate(source_pages):
        label = f"页面[{index + 1}]"
        target_page = target_pages[index]
        attempted, succeeded = copy_page(source_page, target_page, label)
        total_attempted += attempted
        total_succeeded += succeeded
        print(f"  {label}：尝试复制 {attempted} 个形状，成功 {succeeded} 个。")
    print(f"合计：尝试 {total_attempted}，成功 {total_succeeded}。")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def compare_profiles(source: dict, target: dict) -> tuple[bool, list[str]]:
    """比较源 / 目标文档剖析结果，返回 (是否通过, 差异列表)。"""
    diffs: list[str] = []

    if source["page_count"] != target["page_count"]:
        diffs.append(f"页面数不一致：源 {source['page_count']} / 目标 {target['page_count']}")

    if source["overall_type_counts"] != target["overall_type_counts"]:
        diffs.append("全文档形状类型分布不一致：")
        names = set(source["overall_type_counts"]) | set(target["overall_type_counts"])
        for name in sorted(names):
            s = source["overall_type_counts"].get(name, 0)
            t = target["overall_type_counts"].get(name, 0)
            if s != t:
                diffs.append(f"    {name}：源 {s} / 目标 {t}")

    for s_page, t_page in zip(source["pages"], target["pages"]):
        if s_page["layer_count"] != t_page["layer_count"]:
            diffs.append(
                f"页面 {s_page['name']} 图层数不一致：源 {s_page['layer_count']} / 目标 {t_page['layer_count']}"
            )
        s_layers = {layer["name"]: layer for layer in s_page["layers"]}
        t_layers = {layer["name"]: layer for layer in t_page["layers"]}
        for name, s_layer in s_layers.items():
            if name not in t_layers:
                diffs.append(f"页面 {s_page['name']} 缺少图层：{name}")
                continue
            t_layer = t_layers[name]
            if s_layer["top_level"] != t_layer["top_level"]:
                diffs.append(
                    f"图层 {name} 顶层形状数不一致：源 {s_layer['top_level']} / 目标 {t_layer['top_level']}"
                )
            if s_layer["total_all"] != t_layer["total_all"]:
                diffs.append(
                    f"图层 {name} 形状总数不一致：源 {s_layer['total_all']} / 目标 {t_layer['total_all']}"
                )

    return (len(diffs) == 0), diffs


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="通过 CorelDRAW COM 重建 CDR 并校验。")
    parser.add_argument("--source", required=True, help="源 CDR 文件。")
    parser.add_argument("--output", default="outputs/redraw.cdr", help="输出 CDR 文件。")
    parser.add_argument("--mode", choices=("clone", "duplicate"), default="clone",
                        help="clone=形状级重建（默认）；duplicate=文件级复制。")
    parser.add_argument("--progid", default=DEFAULT_PROGID,
                        help=f"CorelDRAW ProgID，默认 {DEFAULT_PROGID}。")
    parser.add_argument("--invisible", action="store_true", help="以不可见方式运行 CorelDRAW。")
    parser.add_argument("--keep-source-open", action="store_true", help="结束后保持源文档打开。")
    parser.add_argument("--report", help="可选：输出 JSON 校验报告路径。")
    args = parser.parse_args()

    source_path = Path(args.source).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"找不到源文件：{source_path}")
    output_path = unique_output_path(Path(args.output).resolve())
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- 保底模式：文件级复制 ----
    if args.mode == "duplicate":
        shutil.copy2(source_path, output_path)
        print(f"已文件级复制：{output_path}")

    app = connect_coreldraw(progid=args.progid, visible=not args.invisible)
    report = {"mode": args.mode, "source": str(source_path), "output": str(output_path)}
    try:
        source_doc = open_document(app, source_path)
        source_profile = profile_document(source_doc)

        if args.mode == "clone":
            target_doc = create_document(app)
            print(f"已新建目标文档：{safe_get(target_doc, 'Name')}")
            rebuild_via_clone(source_doc, target_doc)
            com_retry(lambda: target_doc.SaveAs(str(output_path)), attempts=5, delay=2.0)
            print(f"已保存：{output_path}")
            target_doc = open_document(app, output_path)
        else:
            target_doc = open_document(app, output_path)

        target_profile = profile_document(target_doc)
        passed, diffs = compare_profiles(source_profile, target_profile)

        report["source_profile"] = _profile_summary(source_profile)
        report["target_profile"] = _profile_summary(target_profile)
        report["passed"] = passed
        report["differences"] = diffs

        print("=" * 60)
        print(f"源文件：页面 {source_profile['page_count']}，形状 {source_profile['shape_total']}")
        print(f"目标文件：页面 {target_profile['page_count']}，形状 {target_profile['shape_total']}")
        if passed:
            print("校验结果：PASS（结构一致）")
        else:
            print("校验结果：CHECK WARNINGS（存在差异）")
            for line in diffs:
                print(f"  - {line}")

        if not args.keep_source_open:
            try:
                source_doc.Close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        release_optimization(app)

    if args.report:
        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出校验报告：{report_path}")

    return 0 if report.get("passed") else 2


def _profile_summary(profile: dict) -> dict:
    """把完整剖析压缩为报告用的精简结构。"""
    return {
        "page_count": profile["page_count"],
        "shape_total": profile["shape_total"],
        "overall_type_counts": dict(profile["overall_type_counts"]),
        "pages": [
            {
                "name": page["name"],
                "width": str(page["width"]),
                "height": str(page["height"]),
                "layer_count": page["layer_count"],
                "shape_total": page["shape_total"],
                "layers": [
                    {"name": layer["name"], "top_level": layer["top_level"], "total_all": layer["total_all"]}
                    for layer in page["layers"]
                ],
            }
            for page in profile["pages"]
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
