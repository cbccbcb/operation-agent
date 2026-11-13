# BSCAT 规则修复

基于 Claude Code 的规则修复项目，把问题交易样本转换为经对话审批、有证据支撑的规则表修改（增 / 删 / 改），并在同一批样本上做前后验证与回滚。主路径只修改规则配置表，不修改引擎源码。

## 项目目标与边界

把 miss / extra / diff 三类问题样本转换为规则表上的**增 / 删 / 改**，经过取证、归属复核、影响面分析、分级审批、写前实测与写后验证，最终落盘到 `AUS_BS_CAT_Model` 工作分支，失败可回滚。

- 目标仓库：`AUS_BS_CAT_Model`（默认 `D:/Projects/AUS_BS_CAT_Model`，先确认路径）。
- 规则表：`configs/`（`category_catalog.json`、`pipeline.json` 等）与 `<engine>_engine/resources/`；initial 例外，规则表是 `initial_engine/merchant_kb.csv`。
- 白名单：`bscat-rule-remediation/scripts/_internal/common.py` 的 `RULE_FILES`；`target_file` 必须命中。
- 引擎声明：阶段一生成的 `<review-dir>/claims.csv`；目标引擎必须在其中有声明。
- 审核目录、历史日志目录必须在目标仓库外；阶段零只 `git fetch`，绝不 pull/merge/rebase/checkout/stash/reset。
- 只读 `configs/`、`<engine>_engine/resources/`、`initial_engine/merchant_kb.csv`、与聚类诊断直接相关的源码；绝不修改引擎源码。

核心约束：规则取能覆盖证据的**最窄**模式；`edit`/`delete`/`keyword_update` 的 `match_key` 必须恰好命中 1 行；归属复核是硬关卡；入口定位（I1）、匹配逻辑确认（I4）和人工审批都只在对话框；`match_review_decisions.json` 的哈希必须与当前候选一致；写前 dry-run、写后验证、失败可回滚。

## 运行时文档

执行流程、命令、门禁、引擎事实（优先级、文本归一化、匹配语义、跨引擎覆盖、归属决策树、文本意图写法）、审批与汇报示例、产物与故障排查，全部以 `bscat-rule-remediation/SKILL.md` 为唯一来源。本文件只保留项目边界与待核对项，避免与 skill 正文重复。

- 候选字段与模板：`bscat-rule-remediation/references/rulebook.md`
- 索引 schema、入口六问、验证档位：`bscat-rule-remediation/references/rule-index.md`
- 每张配置表存什么：`bscat-rule-remediation/references/rule-file-catalog.md`

## 不可越过

- 人工决定只在对话框完成：I1 问题类型/方向、I4 匹配逻辑、阶段五审批、阶段六写入授权；I1 写 `intake.json`，I4 写 `match_review_decisions.json`。
- 高风险逐条确认且 `risk_acknowledged=true`，中风险逐条看清、保持 `false`，低风险可批量。
- `approve --build` 对 `confirmed` 的规则候选校验 I4 哈希；缺失或过期立即停止，不手改 `row_hash`。
- `target_file` 必须命中 `RULE_FILES`；`edit`/`delete`/`keyword_update` 的 `match_key` 必须恰好命中 1 行。
- 校验失败、dry-run 失败、写后回归、出现未审批文件：立即停止或回滚。
- 不 commit / push / reset / checkout / stash / rebase；不装依赖、不建 venv、不派生 subagent、不调用其他 skill/MCP。
- 证据不足用 `suggestion`（仅报告，不进入审批）。

## 引擎机制的最终依据

`SKILL.md` §4–§6 的优先级、匹配方式和覆盖关系，来自同一套引擎代码（bscat_V2 / ServiFlow-AI）的逐行分析；`AUS_BS_CAT_Model` 的仓库布局不同（`configs/` + `<engine>_engine/resources/`；initial 用 `initial_engine/merchant_kb.csv`）。文件路径已于 2026-10-22 按当前工作副本核对；**规则数、列名/schema 和引擎源码版本仍需在首次真实运行前逐项核对**。引擎源码才是判断「某条规则改完会怎么跑」的唯一依据。

## 待核对项（首次真实运行前）

1. ~~引擎集合与优先级~~：已按 `configs/pipeline.json` 与 `configs/category_catalog.json` 核对（2026-10-22）；引擎增删或 `priority` 调整后重新核对。
2. ~~规则文件路径~~：已按当前工作副本核对为 `<engine>_engine/resources/` 与 `initial_engine/merchant_kb.csv`；**列名/schema 仍需逐文件核对**。
3. ~~部署到 `.claude/skills/` 后，确认 `scripts/bscat.py` 的相对路径仍然成立~~：已于 2026-10-29 从 `~/.claude/skills/bscat-rule-remediation/` 运行 `catalog --engine fee` 验证通过。
4. 引擎源码是否落后上游（影响「某条规则改完会怎么跑」的判断）。
