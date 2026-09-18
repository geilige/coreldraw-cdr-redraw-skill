"""CorelDRAW X8 COM 通用工具模块。

被 cdr_prompt_builder.py 与 cdr_redraw.py 共用，提供：

- 与 CorelDRAW 建立 / 复用 COM 连接
- COM 调用的重试封装（CorelDRAW 偶发拒绝调用是常见现象）
- 枚举值解析（优先从已安装的 CorelDRAW 类型库读取，读不到则回退到内置表）
- 文档 / 页面 / 图层 / 形状的遍历与统计
- 形状类型、单位等的中文名称映射

仅依赖 pywin32。要求 Windows + 已安装的完整版 CorelDRAW（X8 或更高）。
"""

from __future__ import annotations

import collections
import time

try:
    import pythoncom
    import win32com.client
except ImportError as exc:  # pragma: no cover - 环境缺失时的友好提示
    raise SystemExit(
        "缺少 pywin32。请先运行: python -m pip install pywin32"
    ) from exc


# ---------------------------------------------------------------------------
# CorelDRAW 版本与 ProgID
# ---------------------------------------------------------------------------
# CorelDRAW 主版本号映射：X4=14, X5=15, X6=16, X7=17, X8=18, 2018=20, ...
# 不指定版本时使用无版本的 ProgID，由系统解析到已注册的默认版本。
DEFAULT_PROGID = "CorelDRAW.Application"
X8_PROGID = "CorelDRAW.Application.18"


# ---------------------------------------------------------------------------
# 枚举回退表
# ---------------------------------------------------------------------------
# 说明：以下数值来自 CorelDRAW VBA/VGCore 类型库的常见版本。不同版本可能存在
# 差异，因此脚本优先通过 win32com 类型库常量取值（见 enum_value），只有取不到
# 时才回退到这些表。报告中始终同时输出原始数值，避免误读。

SHAPE_TYPE_NAMES = {
    0: "NoShape(无形状)",
    1: "RectangleShape(矩形)",
    2: "EllipseShape(椭圆)",
    3: "CurveShape(曲线)",
    4: "PolygonShape(多边形)",
    5: "BitmapShape(位图)",
    6: "TextShape(文本)",
    7: "GroupShape(群组)",
    8: "MeshFillShape(网状填充)",
    9: "EPSShape(EPS)",
    10: "OLEShape(OLE 对象)",
    11: "CustomShape(自定义形状)",
    12: "SymbolShape(符号)",
    13: "PerfectShapeShape(完美形状)",
    14: "ConnectorShape(连接线)",
    15: "ArtisticMediaShape(艺术笔)",
    16: "DimensionShape(度量)",
    17: "ConnectorLineShape(连接线)",
    18: "TableShape(表格)",
}

UNIT_NAMES = {
    1: "TenthMicron(0.1微米)",
    2: "Inch(英寸)",
    3: "Foot(英尺)",
    4: "Millimeter(毫米)",
    5: "Centimeter(厘米)",
    6: "Pica(派卡)",
    7: "Point(点)",
    8: "Cicero(西塞罗)",
    9: "Didot(迪多点)",
    10: "Pixel(像素)",
    11: "Meter(米)",
    12: "Kilometer(千米)",
    13: "Mile(英里)",
    14: "Yard(码)",
    15: "Q(昆)",
    16: "HPoint",
}

# 文本子类型（shape.Text.Type）
TEXT_TYPE_NAMES = {
    1: "ArtisticText(美术字)",
    2: "ParagraphText(段落文本)",
}


# ---------------------------------------------------------------------------
# COM 基础封装
# ---------------------------------------------------------------------------
def com_retry(action, attempts: int = 30, delay: float = 0.4):
    """反复尝试执行 COM 调用，直到成功或耗尽次数。

    CorelDRAW 在忙时会抛出 "Call was rejected by callee" 之类的错误，
    重试是处理该现象的标准做法。
    """
    last_error = None
    for _ in range(attempts):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - 需要吞掉并重试
            last_error = exc
            time.sleep(delay)
    raise last_error


def safe_get(obj, attr: str, default=""):
    """安全读取 COM 属性，失败或为 None 时返回默认值。"""
    try:
        value = getattr(obj, attr)
        return value if value is not None else default
    except Exception:  # noqa: BLE001
        return default


def enum_value(name: str, fallback: int | None = None):
    """解析 CorelDRAW 枚举常量。

    优先使用 win32com 生成/缓存的类型库常量（最准确），取不到时回退。
    """
    try:
        import win32com.client.constants as constants  # noqa: PLC0415

        return getattr(constants, name)
    except Exception:  # noqa: BLE001
        return fallback


# ---------------------------------------------------------------------------
# 连接与文档
# ---------------------------------------------------------------------------
def connect_coreldraw(progid: str = DEFAULT_PROGID, visible: bool = True,
                      optimization: bool = True):
    """连接或启动 CorelDRAW。

    先尝试附着到已在运行的实例，失败后再启动新实例。
    `optimization=True` 会关闭屏幕刷新以加快批量操作（结束后请复位）。
    """
    pythoncom.CoInitialize()
    try:
        app = win32com.client.GetActiveObject(progid)
        print(f"已连接到正在运行的 CorelDRAW ({progid})。")
    except Exception:  # noqa: BLE001
        app = win32com.client.Dispatch(progid)
        print(f"已启动新的 CorelDRAW 实例 ({progid})。")
    try:
        app.Visible = bool(visible)
    except Exception as exc:  # noqa: BLE001
        print(f"提示：无法设置 Visible 属性：{exc}")
    if optimization:
        try:
            app.Optimization = True  # 关闭屏幕刷新，加速自动化
        except Exception:  # noqa: BLE001
            pass
        try:
            app.EventsEnabled = False  # 关闭事件，避免宏回调干扰
        except Exception:  # noqa: BLE001
            pass
    return app


def release_optimization(app) -> None:
    """恢复 CorelDRAW 的屏幕刷新与事件。"""
    try:
        app.EventsEnabled = True
    except Exception:  # noqa: BLE001
        pass
    try:
        app.Optimization = False
    except Exception:  # noqa: BLE001
        pass
    try:
        app.Refresh()
    except Exception:  # noqa: BLE001
        pass


def open_document(app, source_path):
    """打开（或复用已打开的）文档，返回 Document 对象。"""
    import os  # noqa: PLC0415

    target = os.path.abspath(str(source_path)).lower()
    try:
        count = com_retry(lambda: app.Documents.Count)
        for index in range(1, count + 1):
            doc = com_retry(lambda i=index: app.Documents.Item(i))
            full = str(safe_get(doc, "FullFileName", "") or "").lower()
            if full == target:
                com_retry(lambda: doc.Pages.Count, attempts=40, delay=0.5)
                print(f"复用已打开的文档：{safe_get(doc, 'Name')}")
                return doc
    except Exception:  # noqa: BLE001
        pass
    print(f"正在打开源文件：{source_path}")
    doc = com_retry(lambda: app.OpenDocument(str(source_path)), attempts=5, delay=2.0)
    com_retry(lambda: doc.Pages.Count, attempts=60, delay=0.5)
    return doc


def create_document(app):
    """新建空白文档。"""
    doc = com_retry(lambda: app.CreateDocument(), attempts=5, delay=2.0)
    com_retry(lambda: doc.Pages.Count, attempts=60, delay=0.5)
    return doc


def attach_coreldraw(progids=None):
    """附加到**已在运行**的 CorelDRAW，返回 app；失败返回 None。

    两个都必须试，只试前者会误判成"没有实例"：

    - `GetActiveObject` 走 ROT。**用户手动启动的 CorelDRAW 不在 ROT 里**，
      所以这里会抛 `-2147221021 操作无法使用`（实测 X8 必现）。
    - `Dispatch` 走 CLSID 解析，手动启动的实例往往仍能附加上去。

    与 `connect_coreldraw()` 的区别：本函数不设置 Visible/Optimization，
    不改变用户正在看的窗口状态，适合"只想查一下/关一个文档"的轻量场景。
    """
    if progids is None:
        progids = (X8_PROGID, DEFAULT_PROGID)
    pythoncom.CoInitialize()
    for progid in progids:
        try:
            return win32com.client.Dispatch(progid)
        except Exception:  # noqa: BLE001
            continue
    return None


def find_open_document(app, path):
    """在 app 已打开的文档里按**绝对路径**找 `path`，找不到返回 None。

    路径比对必须用绝对路径：CorelDRAW 的 `Name` 只有文件名，不同目录的同名
    文件会互相误伤；`FilePath` 末尾带反斜杠、大小写也不固定，所以两边都
    规范化后再比。
    """
    import os  # noqa: PLC0415

    target = os.path.abspath(str(path)).lower()
    try:
        count = com_retry(lambda: app.Documents.Count)
    except Exception:  # noqa: BLE001
        return None
    for index in range(1, count + 1):
        try:
            doc = com_retry(lambda i=index: app.Documents.Item(i))
        except Exception:  # noqa: BLE001
            continue
        full = str(safe_get(doc, "FullFileName", "") or "")
        if not full:
            full = "%s%s" % (safe_get(doc, "FilePath", "") or "",
                             safe_get(doc, "Name", "") or "")
        if os.path.abspath(full).lower() == target:
            return doc
    return None


def release_document(path, allow_dirty: bool = False):
    """把 `path` 从 CorelDRAW 里关掉，解除它对磁盘文件的占用。

    为什么需要这个函数 —— 自动化流水线是**反复重写同一个 CDR** 的：

    1. `open_document()` 刻意"只开不关"（跑完把成果留在 CorelDRAW 窗口里，
       用户能直接接着改），于是**下一轮**重建时目标就被自己上一轮留下的
       文档占着；
    2. 文件被占用时 `SaveAs` / `Save` 会**静默失败** —— 不抛异常、不写盘，
       日志照常打印"已保存"，只有磁盘没变（内存里改动都在，回读内存核验
       还全对）。这是最难查的一类故障；
    3. 改名/删除则是直接抛 `PermissionError [WinError 32]`。

    返回 `(ok: bool, reason: str)`。`allow_dirty=False` 时，文档有未保存
    改动就**拒绝关闭** —— 那种改动是用户的，不能替用户丢掉。
    """
    import os  # noqa: PLC0415

    if not os.path.exists(str(path)):
        return False, "文件不存在"
    app = attach_coreldraw()
    if app is None:
        return False, "无法附加到 CorelDRAW"
    doc = find_open_document(app, path)
    if doc is None:
        return False, "未在 CorelDRAW 中打开"
    if bool(safe_get(doc, "Dirty", False)) and not allow_dirty:
        return False, "文档有未保存改动（Dirty=True），拒绝关闭以免丢数据"
    try:
        com_retry(lambda: doc.Close(), attempts=10, delay=0.5)
    except Exception as exc:  # noqa: BLE001
        return False, "关闭失败：%s" % exc
    return True, "已关闭"


def file_fingerprint(path):
    """返回 `(大小, mtime_ns)`；文件不存在返回 None。

    用于**跨进程落盘操作的写前写后比对**：CDR 被别的进程占用时保存会静默
    失败，只有指纹变了才说明真的写进磁盘了。
    """
    import os  # noqa: PLC0415

    try:
        st = os.stat(str(path))
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


# ---------------------------------------------------------------------------
# 遍历
# ---------------------------------------------------------------------------
def iter_collection(collection):
    """遍历 CorelDRAW 集合（1 基索引）。"""
    total = com_retry(lambda: collection.Count)
    for index in range(1, total + 1):
        yield com_retry(lambda i=index: collection.Item(i))


def iter_pages(doc):
    return iter_collection(doc.Pages)


def iter_layers(page):
    return iter_collection(page.Layers)


def iter_shapes(container):
    """遍历图层 / 页面上的顶层形状（不含群组内部）。"""
    return iter_collection(container.Shapes)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def shape_type_name(shape) -> str:
    """返回形状类型的中文可读名称。"""
    raw = safe_get(shape, "Type", -1)
    try:
        code = int(raw)
    except Exception:  # noqa: BLE001
        return f"Unknown({raw})"
    label = SHAPE_TYPE_NAMES.get(code, "Unknown")
    return f"{label}[{code}]"


def text_subtype_name(shape) -> str:
    """返回文本形状的子类型名称（美术字 / 段落文本）。"""
    try:
        raw = shape.Text.Type
        code = int(raw)
    except Exception:  # noqa: BLE001
        return ""
    return TEXT_TYPE_NAMES.get(code, f"Unknown[{raw}]")


def profile_layer(layer) -> dict:
    """统计单个图层的形状构成。

    同时给出：
    - top_level：顶层形状数（不含群组内部）
    - total_all：包含群组内部所有形状的总数（Shapes.All）
    - type_counts：按形状类型统计
    - text_items：文本形状的内容摘要（前若干条）
    """
    type_counts = collections.Counter()
    text_items = []
    top_level = 0
    for shape in iter_shapes(layer):
        top_level += 1
        type_counts[shape_type_name(shape)] += 1
        if len(text_items) < 50 and "TextShape" in shape_type_name(shape):
            story = safe_get(getattr(shape, "Text", None), "Story", "")
            text_items.append(str(story))
    total_all = top_level
    try:
        total_all = int(layer.Shapes.All.Count)
    except Exception:  # noqa: BLE001
        pass
    return {
        "name": str(safe_get(layer, "Name", "")),
        "visible": bool(safe_get(layer, "Visible", True)),
        "editable": bool(safe_get(layer, "Editable", True)),
        "printable": bool(safe_get(layer, "Printable", True)),
        "top_level": top_level,
        "total_all": total_all,
        "type_counts": type_counts,
        "text_items": text_items,
    }


def profile_page(page) -> dict:
    """统计单个页面：尺寸、方向、图层及其形状构成。"""
    layers = [profile_layer(layer) for layer in iter_layers(page)]
    return {
        "name": str(safe_get(page, "Name", "")),
        "width": safe_get(page, "SizeWidth", ""),
        "height": safe_get(page, "SizeHeight", ""),
        "orientation": safe_get(page, "Orientation", ""),
        "layers": layers,
        "layer_count": len(layers),
        "shape_total": sum(item["total_all"] for item in layers),
    }


def profile_document(doc) -> dict:
    """统计整个文档：单位、页面列表、总体形状类型分布。"""
    pages = [profile_page(page) for page in iter_pages(doc)]
    overall = collections.Counter()
    for page in pages:
        for layer in page["layers"]:
            overall.update(layer["type_counts"])
    return {
        "name": str(safe_get(doc, "Name", "")),
        "full_file_name": str(safe_get(doc, "FullFileName", "")),
        "unit_code": safe_get(doc, "Unit", ""),
        "pages": pages,
        "page_count": len(pages),
        "overall_type_counts": overall,
        "shape_total": sum(page["shape_total"] for page in pages),
    }


def unit_label(doc) -> str:
    """返回文档单位的可读标签。"""
    raw = safe_get(doc, "Unit", "")
    try:
        code = int(raw)
    except Exception:  # noqa: BLE001
        return f"Unknown({raw})"
    return UNIT_NAMES.get(code, f"Unknown[{code}]")


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------
def markdown_table(headers, rows) -> str:
    """把字典列表渲染为 Markdown 表格。"""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)
