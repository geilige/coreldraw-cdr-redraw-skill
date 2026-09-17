"""临时测试：用桩模块验证 cdr_prompt_builder.render_prompt 与 cdr_redraw.compare_profiles 的逻辑。"""

import sys
import types
from pathlib import Path

# ---- 注入 pywin32 桩模块，使 cdr_common 可被导入 ----
pythoncom = types.ModuleType("pythoncom")
pythoncom.CoInitialize = lambda: None
pythoncom.VT_ARRAY = 0
pythoncom.VT_DISPATCH = 0
sys.modules["pythoncom"] = pythoncom

win32com = types.ModuleType("win32com")
client = types.ModuleType("win32com.client")
client.GetActiveObject = lambda *a, **k: None
client.Dispatch = lambda *a, **k: None
client.VARIANT = object
constants = types.ModuleType("win32com.client.constants")
client.constants = constants
win32com.client = client
sys.modules["win32com"] = win32com
sys.modules["win32com.client"] = client
sys.modules["win32com.client.constants"] = constants

sys.path.insert(0, str(Path(__file__).parent))

import cdr_prompt_builder  # noqa: E402
import cdr_redraw  # noqa: E402


def fake_profile():
    return {
        "name": "样例设计稿.cdr",
        "full_file_name": r"D:\work\样例设计稿.cdr",
        "unit_code": 4,
        "page_count": 2,
        "shape_total": 37,
        "overall_type_counts": {
            "CurveShape(曲线)[3]": 20,
            "TextShape(文本)[6]": 10,
            "RectangleShape(矩形)[1]": 5,
            "BitmapShape(位图)[5]": 2,
        },
        "pages": [
            {
                "name": "页面 1",
                "width": 210.0,
                "height": 297.0,
                "orientation": 1,
                "layer_count": 3,
                "shape_total": 25,
                "layers": [
                    {"name": "刀模线", "visible": True, "editable": True, "printable": True,
                     "top_level": 5, "total_all": 5,
                     "type_counts": {"CurveShape(曲线)[3]": 5}, "text_items": []},
                    {"name": "图层 1", "visible": True, "editable": True, "printable": True,
                     "top_level": 18, "total_all": 18,
                     "type_counts": {"CurveShape(曲线)[3]": 10, "TextShape(文本)[6]": 8},
                     "text_items": ["产品名称", "规格：A4", "SN-0001"]},
                    {"name": "位图", "visible": True, "editable": True, "printable": True,
                     "top_level": 2, "total_all": 2,
                     "type_counts": {"BitmapShape(位图)[5]": 2}, "text_items": []},
                ],
            },
            {
                "name": "页面 2",
                "width": 210.0,
                "height": 297.0,
                "orientation": 1,
                "layer_count": 1,
                "shape_total": 12,
                "layers": [
                    {"name": "图层 1", "visible": True, "editable": True, "printable": True,
                     "top_level": 12, "total_all": 12,
                     "type_counts": {"RectangleShape(矩形)[1]": 5, "TextShape(文本)[6]": 2,
                                     "CurveShape(曲线)[3]": 5},
                     "text_items": ["封底"]},
                ],
            },
        ],
    }


def main():
    profile = fake_profile()
    prompt = cdr_prompt_builder.render_prompt(
        Path(r"D:\work\样例设计稿.cdr"), profile, None, "Millimeter(毫米)"
    )
    assert "# 样例设计稿 定制 CDR 重绘提示词" in prompt
    assert "页面总数：2" in prompt
    assert "形状总数（含群组内部）：37" in prompt
    assert "| 页面 1 | 210.0 | 297.0 |" in prompt
    assert "刀模线" in prompt
    assert "SN-0001" in prompt
    assert "CurveShape(曲线)[3] | 20" in prompt
    print("[1/3] render_prompt 渲染通过，长度 =", len(prompt), "字符")

    # ---- compare_profiles：完全一致 ----
    same = fake_profile()
    passed, diffs = cdr_redraw.compare_profiles(profile, same)
    assert passed and not diffs, diffs
    print("[2/3] compare_profiles 相同文件判定为 PASS")

    # ---- compare_profiles：存在差异 ----
    changed = fake_profile()
    changed["pages"][0]["layers"][1]["total_all"] = 15  # 少 3 个形状
    changed["page_count"] = 1
    passed2, diffs2 = cdr_redraw.compare_profiles(profile, changed)
    assert not passed2
    joined = "\n".join(diffs2)
    assert "页面数不一致" in joined
    assert "图层 1 形状总数不一致" in joined
    print("[3/3] compare_profiles 正确检出差异：")
    for line in diffs2:
        print("      ", line)

    print("\n全部测试通过。")


if __name__ == "__main__":
    main()
