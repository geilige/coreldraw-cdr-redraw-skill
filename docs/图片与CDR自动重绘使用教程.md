# CDR 自动重绘使用教程

本教程面向第一次使用 `coreldraw-x8-redraw` 技能的人，从环境准备到跑出第一个重绘文件。

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

### 2. 安装 Python 与 pywin32

```powershell
python --version          # 需要 3.10 或更高
python -m pip install pywin32
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

## 六、常见问题

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

---

## 七、脚本参数速查

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

---

## 八、退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 校验通过 |
| `2` | 校验存在差异（CHECK WARNINGS） |
| 其他 | 运行失败，异常信息在 stderr |

适合接入 CI 或批处理脚本。
