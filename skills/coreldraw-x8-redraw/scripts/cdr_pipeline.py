"""位图 → CDR 四步流水线（识别在前）。

为什么需要这一层：`cdr_bitmap_to_cdr.py` 是"整张描摹"，图纸里有文字时会
把文字也描成轮廓——**看着像，但改不了字、换不了字体**，等于把可编辑内容
降级成死图形。正确顺序是**先识别、后描摹**，且两步范围互斥：

    描摹范围 = 非文字块 + 判定为 keep_trace 的文字区
    活字范围 = 判定为 convert 的文字区

而"判定"只有跑完 `cdr_text_live.py` 才知道，所以必须有个编排层。

```
1. cdr_scan_text.py          整图识别文字/符号/图形块 + 每块文字朝向
2. cdr_text_live.py          逐区判定 convert / keep_trace（只判定，不写）
3. cdr_bitmap_to_cdr.py      描摹【非文字块 + keep_trace 的文字区】
4. cdr_text_live.py --apply  把 convert 的文字建成可编辑美术字
```

两个**必须由本脚本保证**的不变量（手工拼命令时最容易错）：

1. **第 3 步的描摹范围必须含 `keep_trace` 的文字区。** 漏掉它，品牌字标和
   特殊符号会整个从成品里消失（实测踩过：`daiion` 字标、`4#` 都没了）。
2. **`--scan-json` 挖除清单只能喂 `convert` 的条目。** 喂全量会把
   `keep_trace` 的字标一起挖掉，等于主动删内容。

另外两份区域清单格式不同（活字用 `NAME=y0,y1,x0,x1`、描摹用
`name:x0,y0,x1,y1`），所以**只能从同一份 `scan.json` 派生**——手抄必然不同步。

用法：

    python cdr_pipeline.py --image ref.png --page-width 210 --output out\\panel.cdr
    python cdr_pipeline.py --image ref.png --page-width 210 --output out\\panel.cdr ^
        --skip-scan --skip-judge        # 调描摹参数时复用前两步
    python cdr_pipeline.py --image ref.png --page-width 210 --output out\\panel.cdr ^
        --skip-scan --skip-judge --skip-trace   # 只重建活字

退出码：0 成功；1 参数/文件问题；3 目标 CDR 被占用且无法自动释放；
其余为子步骤的退出码（透传）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def run(cmd, log_path, log=print):
    """跑一个子步骤，输出同时进日志文件与终端（只回显尾部，避免刷屏）。"""
    log("$ %s %s ..." % (os.path.basename(cmd[1]), " ".join(cmd[2:4])))
    with open(log_path, "w", encoding="utf-8") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        log("  -> 失败（exit %d），日志尾部：" % p.returncode)
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines()[-25:]:
                log("     " + line)
        return p.returncode
    log("  -> %s" % os.path.basename(log_path))
    return 0


def parse_live(line):
    """活字清单一行 -> (name, y0, y1, x0, x1, color, rot)。

    格式 `NAME=y0,y1,x0,x1[,#RRGGBB][,rot=180]`。**注意 y 在前**——
    与描摹清单的 `name:x0,y0,x1,y1` 正好相反，这是历史原因，
    所以解析必须按位置而不是按语义猜。

    无法识别的可选字段**直接抛错**，不静默忽略：拼错一个字段却当没事，
    下游会拿着一份"看起来正常"的清单跑出错误结果，这类故障最难查
    （实测踩过：shell 把 `#` 当注释剥掉，颜色和 `rot=180` 一起消失，
    倒置文字没被转正、OCR 全 0，而日志一切正常）。
    """
    name, rest = line.split("=", 1)
    parts = [p for p in rest.split(",") if p.strip()]
    nums = [int(p) for p in parts[:4]]
    if len(nums) < 4:
        raise ValueError("区域声明坐标不足 4 个：%r" % line)
    color, rot = None, 0
    for p in parts[4:]:
        if p.startswith("#"):
            color = p
        elif p.startswith("rot="):
            rot = int(p.split("=", 1)[1])
        else:
            raise ValueError("无法识别的可选字段 %r：%r" % (p, line))
    return name, nums[0], nums[1], nums[2], nums[3], color, rot


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f
                if l.strip() and not l.strip().startswith("#")]


def image_width(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.width


def release_target(path, log=print):
    """重写前解除 CorelDRAW 对目标文件的占用。返回 0 / 退出码。

    占用往往是**上一轮自动化自己造成的**：第 4 步 `--apply` 走
    `open_document()`，刻意"只开不关"（把成果留在窗口里给用户接着改）。
    于是这一轮 `os.replace` 抛 `PermissionError [WinError 32]`；
    而若不改名直接写，`SaveAs`/`Save()` 会**静默失败**（不抛异常、不写盘）。
    """
    sys.path.insert(0, HERE)
    import cdr_common as C  # noqa: PLC0415

    ok, why = C.release_document(path)
    log("  释放目标文件：%s" % why)
    if not ok and why not in ("文件不存在", "未在 CorelDRAW 中打开"):
        log("  [失败] 目标文件被占用且无法自动释放：%s" % why)
        log("         请先在 CorelDRAW 里关闭它。若该文档 Dirty=True，"
            "说明有未保存改动，脚本不会替你丢弃。")
        return 3
    return 0


def archive(path, log=print):
    """改名归档旧产物。

    用改名而不是删除：一来留一份可回溯的上一版，二来**受限运行环境会把
    `os.remove` 判为批量删除并直接掐掉进程**（日志里只剩一行
    `SAFE_DELETE_BULK_CONFIRM_REQUIRED`，整条流水线莫名失败，而产物其实
    已经生成好了，极易误判）。
    """
    if not os.path.exists(path):
        return
    bak = "%s.%s.bak" % (path, time.strftime("%H%M%S"))
    os.replace(path, bak)
    log("  旧的 %s 已归档为 %s" % (os.path.basename(path),
                                    os.path.basename(bak)))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="位图 → CDR 四步流水线（先识别文字，再描摹图形）")
    ap.add_argument("--image", required=True, help="源位图路径")
    ap.add_argument("--output", required=True, help="输出 CDR 路径")
    ap.add_argument("--page-width", type=float, default=None,
                    help="页面宽度 mm（用于标定；与 --mm-per-px 二选一）")
    ap.add_argument("--page-size", default=None,
                    help="标准纸型（A4/A3/...），透传给描摹步骤")
    ap.add_argument("--mm-per-px", type=float, default=None,
                    help="直接给标定比例；不给则按 page_width / 图像宽 推导")
    ap.add_argument("--out-dir", default=None,
                    help="中间产物目录，默认 <CDR 同目录>/<文件名>_pipeline")
    ap.add_argument("--live-color", default="#000000",
                    help="活字默认颜色（逐区识别到颜色时以逐区为准）")
    ap.add_argument("--upscale", type=int, default=8, help="描摹上采样倍数")
    ap.add_argument("--allow-palette-mismatch", action="store_true",
                    help="调色板与源图不符时仍继续（默认中止，退出码 3）")
    ap.add_argument("--skip-scan", action="store_true", help="复用已有识别结果")
    ap.add_argument("--skip-judge", action="store_true", help="复用已有判定结果")
    ap.add_argument("--skip-trace", action="store_true",
                    help="复用已有 CDR，只重建活字（描摹最慢，调活字时用）")
    ap.add_argument("--keep-trace", action="store_true",
                    help="全部文字都保留描摹、不建活字（只想看识别结果时用）")
    ap.add_argument("--min-text-iou", type=float, default=None,
                    help="文字判定的字形 IoU 门槛，透传 cdr_scan_text")
    args = ap.parse_args(argv)

    src = os.path.abspath(args.image)
    out = os.path.abspath(args.output)
    if not os.path.isfile(src):
        print("找不到源图：%s" % src, file=sys.stderr)
        return 1

    work = args.out_dir or os.path.join(
        os.path.dirname(out),
        os.path.splitext(os.path.basename(out))[0] + "_pipeline")
    scan_dir = os.path.join(work, "scan")
    live_dir = os.path.join(work, "live")
    trace_work = os.path.join(work, "trace")
    for d in (work, scan_dir, live_dir, trace_work):
        os.makedirs(d, exist_ok=True)

    scan_json = os.path.join(scan_dir, "scan.json")
    live_regions = os.path.join(work, "live_regions.txt")
    trace_regions = os.path.join(work, "trace_regions.txt")
    live_json = os.path.join(live_dir, "live_text.json")

    # --- 标定：必须与描摹步骤用**同一个**比例，否则识别框与描摹内容整体错位
    if args.mm_per_px:
        mmpp = float(args.mm_per_px)
    elif args.page_width:
        mmpp = args.page_width / image_width(src)
    else:
        print("必须给出 --page-width 或 --mm-per-px（标定用）。",
              file=sys.stderr)
        return 1
    print("源图 %s" % src)
    print("标定 1 px = %.8f mm" % mmpp)
    print("工作目录 %s" % work)

    # --- 1. 识别
    if not args.skip_scan:
        cmd = [PY, "-u", os.path.join(HERE, "cdr_scan_text.py"),
               "--image", src, "--out-dir", scan_dir,
               "--mm-per-px", repr(mmpp),
               "--emit-live", live_regions, "--emit-trace", trace_regions]
        if args.min_text_iou is not None:
            cmd += ["--min-text-iou", repr(args.min_text_iou)]
        rc = run(cmd, os.path.join(work, "step1_scan.log"))
        if rc:
            return rc
    else:
        print("（--skip-scan：复用 %s）" % scan_json)

    if not os.path.exists(scan_json):
        print("缺少识别结果 %s，去掉 --skip-scan 重跑。" % scan_json,
              file=sys.stderr)
        return 1

    live_lines = read_lines(live_regions)
    if not live_lines:
        print("没有识别到任何文字区域。若图纸确实没有文字，"
              "直接用 cdr_bitmap_to_cdr.py 整张描摹即可。")
    print("识别到文字区域 %d 处" % len(live_lines))

    # --- 2. 判定（只判定，不写文件）
    if live_lines:
        if not args.skip_judge:
            cmd = [PY, "-u", os.path.join(HERE, "cdr_text_live.py"),
                   "--image", src, "--mm-per-px", repr(mmpp),
                   "--out-dir", live_dir]
            for l in live_lines:
                cmd += ["--region", l]
            rc = run(cmd, os.path.join(work, "step2_judge.log"))
            if rc:
                return rc
        else:
            print("（--skip-judge：复用 %s）" % live_json)
        if not os.path.exists(live_json):
            print("缺少判定结果 %s，去掉 --skip-judge 重跑。" % live_json,
                  file=sys.stderr)
            return 1

    regions = {}
    if os.path.exists(live_json):
        with open(live_json, encoding="utf-8") as f:
            regions = json.load(f).get("regions", {})

    # --- 3. 按判定分成两类
    keep, conv = [], []
    for l in live_lines:
        name, y0, y1, x0, x1, color, rot = parse_live(l)
        r = regions.get(name) or {}
        if args.keep_trace:
            keep.append((name, y0, y1, x0, x1, color, rot, "forced"))
            continue
        if r.get("verdict") == "convert":
            conv.append((name, l))
        else:
            keep.append((name, y0, y1, x0, x1, color, rot,
                         r.get("verdict") or "no_verdict"))
    print()
    print("判定：convert %d 处 / keep_trace %d 处" % (len(conv), len(keep)))
    for name, _, _, _, _, _, _, v in keep:
        print("   keep_trace  %-28s %s" % (name, v))

    # --- 4. 描摹 = 非文字块 + keep_trace 的文字区
    #     （漏掉后半句，品牌字标与特殊符号会整个从成品里消失）
    trace_args = []
    for l in read_lines(trace_regions):
        trace_args += ["--region", l]
    for name, y0, y1, x0, x1, color, rot, _ in keep:
        trace_args += ["--region", "%s:%d,%d,%d,%d,%s"
                       % (name, x0, y0, x1, y1, color or "#111111")]

    # 挖除清单**只喂 convert**：喂全量会把 keep_trace 的字标一起挖掉。
    # 匹配按**框心落在哪个 convert 区域里**，不按名字——scan.json 的文字项
    # 没有 name 字段（名字是 --emit-live 临时生成的），按名字过滤会一条都
    # 匹配不上、静默变成"不挖除"。
    filt = None
    with open(scan_json, encoding="utf-8") as f:
        scan = json.load(f)
    conv_box = [(x0, y0, x1, y1) for _, y0, y1, x0, x1, _, _ in
                [parse_live(l) for _, l in conv]]
    kept = []
    for t in scan.get("texts", []):
        if t.get("suspect"):
            continue
        bx0, by0, bx1, by1 = [int(round(v)) for v in t["box"]]
        cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
        if any(x0 - 4 <= cx <= x1 + 4 and y0 - 4 <= cy <= y1 + 4
               for x0, y0, x1, y1 in conv_box):
            kept.append(t)
    scan["texts"] = kept
    filt = os.path.join(work, "scan_convert_only.json")
    with open(filt, "w", encoding="utf-8") as f:
        json.dump(scan, f, ensure_ascii=False, indent=2)
    print("  挖除清单（只含 convert）%d 条 -> %s"
          % (len(kept), os.path.basename(filt)))

    # --- 5. 描摹
    if not args.skip_trace:
        rc = release_target(out)
        if rc:
            return rc
        archive(out)
        cmd = [PY, "-u", os.path.join(HERE, "cdr_bitmap_to_cdr.py"),
               "--image", src, "--output", out, "--out-dir", trace_work,
               "--scan-json", filt, "--upscale", str(args.upscale),
               "--turdsize", "2", "--alphamax", "1.0",
               "--opttolerance", "0.1"]
        if args.page_width:
            cmd += ["--page-width", repr(args.page_width)]
        if args.page_size:
            cmd += ["--page-size", args.page_size]
        if args.allow_palette_mismatch:
            cmd += ["--allow-palette-mismatch"]
        cmd += trace_args
        rc = run(cmd, os.path.join(work, "step3_trace.log"))
        if rc:
            return rc
    else:
        print("（--skip-trace：复用已有 %s）" % out)
        if not os.path.exists(out):
            print("目标 CDR 不存在，去掉 --skip-trace 重跑。", file=sys.stderr)
            return 1

    # --- 6. 建活字（内部会跳过 keep_trace 的区域）
    if conv:
        rc = release_target(out)
        if rc:
            return rc
        cmd = [PY, "-u", os.path.join(HERE, "cdr_text_live.py"),
               "--image", src, "--mm-per-px", repr(mmpp),
               "--out-dir", live_dir, "--apply", out,
               "--live-color", args.live_color]
        for l in live_lines:
            cmd += ["--region", l]
        rc = run(cmd, os.path.join(work, "step4_live.log"))
        if rc:
            return rc
    else:
        print("（没有 convert 的区域，跳过建活字）")

    print()
    print("CDR -> %s" % out)
    print("日志 %s/step1_scan.log … step4_live.log" % work)
    print("识别预览 %s" % os.path.join(scan_dir, "scan_preview.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
