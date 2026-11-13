# BSCAT 规则修复（Rule Remediation）

基于 Claude Code 的规则修复 Skill，把问题交易样本（漏识别 miss / 多识别 extra / 不一致 diff）转换为经对话审批、有证据支撑的规则表修改（增 / 删 / 改），并在同一批样本上做前后验证与回滚。主路径只修改规则配置表，不修改引擎源码。

## 文档分工

| 文档 | 负责 | 什么时候看 |
|------|------|-----------|
| [README.md](README.md) | 人类入口：概览、快速开始、项目结构 | 第一次使用 |
| [bscat-rule-remediation/SKILL.md](bscat-rule-remediation/SKILL.md) | **主运行时文档**：流程、命令、门禁、引擎事实、归属决策、审批/汇报示例、产物与排错 | 实际跑流程 |
| [CLAUDE.md](CLAUDE.md) | 项目边界与待核对项 | 非 skill 会话、项目约束 |
| [references/rule-index.md](bscat-rule-remediation/references/rule-index.md) | 机器索引：`rule_index.json` schema、入口六问、验证档位 | 入口路由 |
| [references/rulebook.md](bscat-rule-remediation/references/rulebook.md) | 候选格式：`candidates.json` 字段、各引擎模板、对话补丁 | 核对候选格式 |
| [references/rule-file-catalog.md](bscat-rule-remediation/references/rule-file-catalog.md) | 每张配置表存什么、关键列、是否可追加 | 选表 |

发生冲突时：流程、门禁与引擎事实以 `SKILL.md` 为准；候选字段格式以 `rulebook.md` 为准；项目边界以 `CLAUDE.md` 为准。

## 快速开始

使用时只需要说明：**处理哪个样本、目标仓库路径、修 miss/extra/diff 中的哪类**。其余步骤由 Agent 执行；所有命令都由 Agent 运行，`<venv-python>` 指已有环境（例如 `.venv-bscat/Scripts/python.exe`）。

```bash
# 入口：先判定目标引擎/表、匹配方式、覆盖风险与验证档位（不跑流水线）
<venv-python> bscat-rule-remediation/scripts/bscat.py route \
    --repo D:/Projects/AUS_BS_CAT_Model --input <input>.csv --problem-type all \
    --out reviews/run-001

# I1：用 AskUserQuestion 确认问题类型 / 修复方向 / 覆盖范围 / 交付方式，
# Agent 写 reviews/run-001/intake.json

# 阶段零/一：同步检查 + 解析 + 取证 + 按需代码诊断（默认读 intake.json）
<venv-python> bscat-rule-remediation/scripts/bscat.py prepare \
    --repo D:/Projects/AUS_BS_CAT_Model --input <input>.csv \
    --review reviews/run-001
```

`prepare` 会写 `parse_review.json`（I2）和 `attribution_review.json`（I3）；触发时先按蓝图弹窗，把答案写入 `dialog_decisions.json`，再运行 `propose`。阶段四 `check --only validate` 后，Agent 按 `match_review.json` 做 I4 匹配逻辑确认并写 `match_review_decisions.json`；`check --only reconcile` 写 `precheck.json`（I6），触发时先确认再 `approve --build`。`approve --build` 会校验 I1–I6 的决策与确认哈希；写后验证成功后再按 I8 问下一步。

完整流程、快路径、文本意图（“文本 X 应分为 Y”）、审批与写入命令见 [SKILL.md](bscat-rule-remediation/SKILL.md)。

## 项目结构

```
operation-agent/
├── bscat-rule-remediation/
│   ├── SKILL.md                    # 主运行时文档（流程 + 引擎事实 + 排错）
│   ├── references/rule-index.md    # 机器索引 schema、入口六问、验证档位
│   ├── references/rulebook.md      # 候选格式与各引擎模板
│   ├── references/rule-file-catalog.md
│   ├── evals/                      # 评测用例与样本
│   └── scripts/
│       ├── bscat.py                # 唯一入口（9 个子命令）
│       └── _internal/              # 内部实现
├── CLAUDE.md                       # 项目边界
├── README.md                       # 本文件
├── samples/                        # 输入样本
└── reviews/                        # 运行期审核目录
```

`prepare.py` / `validate.py` / `apply.py` 保留为阶段编排后端；`bscat.py --only` 直接调用 `_internal/` 阶段模块。新流程统一从 `bscat.py` 进入。

## 关键约束

- **宁漏判不误判**：规则取能覆盖证据的最窄模式，并写明「不应匹配什么」。
- **一条候选只改一行**：`edit` / `delete` / `keyword_update` 的 `match_key` 必须恰好命中 1 行。
- **交互门只在对话框**：I1 确认问题类型/方向/范围/交付，I2 确认解析，I3 确认归属/目标表，I4 逐表确认匹配逻辑，I5 预览修订，I6 预审校验/影响面，I8 决定写后下一步，阶段五按配置表审批；低风险可批量、中风险逐条看清、高风险必须逐条确认。I1/I4 是硬门禁，I2/I3/I5/I6/I8 在触发时强制（`guided` 无触发也确认）并把答案写入 `dialog_decisions.json`。
- **I4 哈希锁定**：`approve --build` 校验 `match_review_decisions.json` 的 `row_hash`；候选在确认后又改过即拒绝审批。
- **写前 dry-run、写后验证、失败可回滚**：写入有备份与原子恢复。
- **审核目录在目标仓库外**：产物、备份、历史日志绝不污染目标仓库。
- **只改规则配置表**：不改引擎源码；不 commit/push/reset/checkout/stash/rebase。

排错、产物清单、汇报卡片与审批明细模板已并入 `SKILL.md` §7–§9。
