# CorelDRAW CDR Redraw Skills

一个 WorkBuddy / Codex 技能，通过 **COM 自动化**操作 **CorelDRAW X8（及更高版本）**，
剖析、精确重绘并校验 `.cdr` 文件。

与 [autocad-dwg-redraw-skill](https://github.com/pengxiaoan/autocad-dwg-redraw-skill) 同构：
**剖析 → 生成定制提示词 → 重建 → 校验**。

中文教程：[CDR 自动重绘使用教程](docs/图片与CDR自动重绘使用教程.md)

## 这个技能能做什么

- **剖析**任意 CDR：页面、页面尺寸与方向、图层（名称 / 可见性 / 可编辑性 / 可打印性）、
  形状类型分布、文本清单。
- **生成定制重绘提示词**：为每个源文件产出一份 `文件名-redraw-prompt.md`，
  包含完整"文件指纹"与校验标准。
- **精确重建**：通过 `Shape.CopyToLayer` 做形状级复制，产出可编辑、结构对齐的新 CDR。
- **结构校验**：逐页、逐图层比较页面数、图层名集合、顶层形状数、形状总数、
  全文档形状类型分布，输出 PASS / CHECK WARNINGS 与 JSON 报告。

## 安装

把 `skills/coreldraw-x8-redraw` 目录放到你的技能目录下：

| 客户端 | 技能目录 |
| --- | --- |
| WorkBuddy | `%USERPROFILE%\.workbuddy-ai\skills\` |
| Codex | `%USERPROFILE%\.codex\skills\` |

```powershell
# 克隆仓库
git clone https://github.com/<your-name>/coreldraw-cdr-redraw-skill.git
cd coreldraw-cdr-redraw-skill

# 复制技能到 WorkBuddy 技能目录
xcopy /E /I skills\coreldraw-x8-redraw "%USERPROFILE%\.workbuddy-ai\skills\coreldraw-x8-redraw"
```

安装后**重启客户端**，让它扫描到新技能。

依赖：

```powershell
python -m pip install pywin32
```

> **必须使用官方完整版 CorelDRAW。** 第三方"精简版 / 绿色版"通常未注册 COM 组件与
> VBA 宿主，会报 `Invalid class string` 或 `ActiveX component can't create object`。

## 使用

对 AI 说：

```
使用 coreldraw-x8-redraw。源文件 D:\work\a.cdr。
先剖析并生成定制重绘提示词给我审阅，确认后再重建，最后输出结构校验报告。
保留全部页面/图层/文本/位图/群组；不覆盖源文件；输出到 outputs\。
```

或者只给图片 + 尺寸：

```
使用 coreldraw-x8-redraw 的图片+尺寸模式：参考图 D:\ref.png，成品尺寸 210×297mm，
单位毫米，输出 D:\out\redraw.cdr。尺寸与图片冲突时以我给的尺寸为准。
```

## 脚本

技能内含三个脚本，也可以直接命令行调用。

生成文件专属的定制重绘提示词：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_prompt_builder.py ^
  --source input.cdr ^
  --output outputs\input-redraw-prompt.md
```

形状级精确重建（真正的"重绘"）：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_redraw.py ^
  --source input.cdr ^
  --output outputs\redraw_exact.cdr ^
  --mode clone
```

文件级安全复制（保底方案，内容 100% 一致）：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_redraw.py ^
  --source input.cdr ^
  --output outputs\redraw_copy.cdr ^
  --mode duplicate
```

带 JSON 校验报告：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_redraw.py ^
  --source input.cdr ^
  --output outputs\redraw_exact.cdr ^
  --report outputs\validation.json
```

指定 CorelDRAW X8 的 ProgID：

```powershell
python skills\coreldraw-x8-redraw\scripts\cdr_prompt_builder.py ^
  --source input.cdr --progid CorelDRAW.Application.18
```

离线冒烟测试（**不需要安装 CorelDRAW**，用桩模块验证渲染与比对逻辑）：

```powershell
python skills\coreldraw-x8-redraw\scripts\selftest_offline.py
```

## 标准流程

1. 提供源 `.cdr`（或先把 PDF / 图片转成可审计的中间 CDR）。
2. 用 `cdr_prompt_builder.py` 生成 `*-redraw-prompt.md`。
3. 审阅提示词里的文件指纹：页面数、页面尺寸、图层表、形状类型分布、文本清单、风险提示。
4. 用 `cdr_redraw.py --mode clone` 产出最终交付文件。
5. 校验目标与源的页面数、图层名、形状计数与类型分布一致。

## 三种输入模式

| 模式 | 适用场景 | 权威来源 |
| --- | --- | --- |
| 源 CDR | 手上有原始 `.cdr` | 源文件实体 |
| PDF 派生 | 只有 PDF，没有源 CDR | PDF 矢量路径 > 栅格描摹；结果标注为 PDF 派生 |
| 图片 + 尺寸 | 只有截图 / 照片 / 草图 + 书面尺寸 | 书面尺寸 > 派生算术 > 图片比例 |

## 与 AutoCAD 技能的关键差异

| 维度 | AutoCAD | CorelDRAW |
| --- | --- | --- |
| ProgID | `AutoCAD.Application` | `CorelDRAW.Application`（X8 = `.18`） |
| 文档空间 | ModelSpace / PaperSpace | Pages → Layers → Shapes |
| 集合索引 | 0 基 | **1 基** |
| 跨文档复制 | `Document.CopyObjects` | **`Shape.CopyToLayer`** |
| 单位/类型枚举 | 稳定 | **随版本有差异** |

## 已知限制

- `Shape.CopyToLayer` 的**跨文档**支持情况随 CorelDRAW 版本而异。脚本会逐形状统计
  成功/失败数；若失败率过高，请改用 `--mode duplicate` 保底。
- 部分效果（阴影、立体化、网状填充、透镜）在跨文档复制时可能被简化。
- 链接位图需一并复制，否则会变成断链占位。
- 源文件使用的字体若未安装，文本会回退为替代字体。
- 形状类型 / 单位枚举数值存在版本差异：脚本优先从类型库常量取值，并**始终同时输出
  原始数值**；设置目标单位时直接复制源文档的原始单位代码，不做数字映射。

## 仓库结构

```
coreldraw-cdr-redraw-skill/
├── README.md
├── LICENSE
├── .gitignore
├── docs/
│   └── 图片与CDR自动重绘使用教程.md
└── skills/
    └── coreldraw-x8-redraw/
        ├── SKILL.md
        ├── scripts/
        │   ├── cdr_common.py            # COM 连接 / 重试 / 枚举 / 遍历 / 统计
        │   ├── cdr_prompt_builder.py    # 剖析源 CDR → 生成定制提示词
        │   ├── cdr_redraw.py            # 重建 CDR + 结构校验
        │   └── selftest_offline.py      # 离线冒烟测试
        └── references/
            ├── prompt-template.md        # 定制提示词模板
            └── coreldraw-object-model.md # CDR COM 对象模型速查
```

## 说明

- 仓库中不包含任何个人 CDR 文件或本机路径。
- 运行环境要求：Windows + CorelDRAW X8 或更高 + Python 3.10+ + pywin32。

## License

MIT
