---
name: bscat-rule-remediation
description: 仅在用户明确要求修复交易分类中的漏识别（miss，应分未分）、多识别（extra，不应分却分了）或差异/不一致（diff，错分/分错）问题时使用；用于读取本地 AUS_BS_CAT_Model 工作分支的规则配置，生成增/删/改候选，经对话内分级审批后写入 CSV/JSON 规则表，并在输入样本上做前后验证与回滚，同时只读诊断引擎代码问题、绝不修改源码。若用户只提到 BSCAT、AUS_BS_CAT_Model、交易分类不一致、错分、误分类、规则配置、商户关键词或分类引擎规则调整，但没有明确说明要修复漏识别、多识别还是差异问题，不要触发本 skill，先询问用户要修复哪类问题。
argument-hint: "[输入 Excel/CSV] [审核目录]"
---

# BSCAT 规则修复

将问题交易样本转换为经对话审批、有证据支撑的规则表修改，并在同一批样本上验证。主路径只修改规则配置表，不修改引擎源码。

**文档范围**：本文件是主运行时文档，包含执行流程、命令、门禁、引擎事实、归属决策、审批示例、产物与排错。候选字段与 `dialog_patch.json` 格式看 `references/rulebook.md`；`rule_index.json` schema 与六问语义看 `references/rule-index.md`；配置表目录看 `references/rule-file-catalog.md`。项目根 `CLAUDE.md` 只保留项目边界；`README.md` 只保留人类入口，均不是运行时依赖。

## 0. 运行约定

### 语言与交互

- 对话用**简体中文**；代码/脚本/字段/命令/引擎名/category 保持英文。
- 命令、脚本和文件写入全部由 Agent 完成；用户不需要打开终端，也不需要手工编辑 CSV/JSON。
- 用户只在对话框做决定：**入口定位（I1）、解析确认（I2）、归属/目标表（I3）、范围选择、匹配逻辑（I4）、修订预览（I5）、校验/影响面预审（I6）、审批、最终写入授权、写后下一步（I8）**。
- **I1 入口定位**（`route` 后、`prepare` 前）写 `<review>/intake.json`；**I4 匹配逻辑确认**（`check --only validate` 后、审批前）写 `<review>/match_review_decisions.json`。两者是硬门禁。**I2/I3/I5/I6/I8 是条件门禁**：触发时用 `AskUserQuestion` 确认，答案写入 `<review>/dialog_decisions.json` 的对应段；每段带 `fingerprint` 和 `decided_at`，过期即硬停，不得手改哈希。细节见 §1.1.1、§3.0.1 和各阶段门禁段。
- 交互密度由 `intake.json` 的 `interaction_mode` 控制：`guided` 每个条件门都问；`standard` 只在触发时问；`minimal` 只保留歧义/高风险门禁（I2 的 `zero_rows`、I3 的归属风险、I5 的 `match_key`/`source_trace`、I6 的 `simulate_mismatch`），其余条件门跳过。单轮弹窗软上限 12 次；每个弹窗必须给推荐项和停止/取消项。同一事实不在 I1、阶段二、阶段五重复问。
- 审批按 `target_file` 一表一弹窗，按**高风险 → 中风险 → 低风险**；能用 `AskUserQuestion` 就用弹窗。`candidates.json`、审阅 CSV、`match_review.json`、`approvals.json` 是 Agent 的产物和证据，不是用户界面。
- 1–20 行或单个文本意图走「快路径」，用紧凑卡片汇报（§7.1）；卡片只压缩汇报，不替代 I1/I4、阶段五审批和任何硬门禁。

### 硬性边界

- 只处理用户指定样本、`AUS_BS_CAT_Model` 工作副本和**仓库外**审核/历史目录；不切分支、不建 worktree、不碰其他仓库或生产库。
- 只读 `configs/`、`<engine>_engine/resources/`、`initial_engine/merchant_kb.csv`、与聚类诊断直接相关的源码；引擎事实以本文件 §4–§6 为准。
- **联网边界**：阶段零只做 `git fetch`；除此之外不发起网络请求，不用 WebSearch/WebFetch。
- 只写审核目录、历史日志目录和 `RULE_FILES` 登记表；`catalog`/`priority` 仅在高风险逐条确认后写；绝不写引擎 `.py`、pickle、未登记文件或仓库外路径。
- 只运行 `bscat.py`（及内部后端）、只读 git/文件命令和上述只读检索；不装依赖、不建 venv、不派生 subagent、不调用其他 skill/MCP。
- 人工门：I1 入口定位、I2 解析确认、I3 归属/目标表、阶段二范围、I4 匹配逻辑、I5 修订预览、I6 校验/影响面预审、阶段五对话审批、阶段六写入确认、I8 写后下一步；Agent 不得代替用户选择或确认。不 commit/push/reset/checkout/stash/rebase；不改引擎源码；不编造规则/证据；不绕过校验、dry-run、备份或审批。
- 越界、非白名单 `target_file`、未选中聚类、校验/dry-run 失败、写入后出现未审批文件：立即停止或回滚。

### 目标仓库与边界

| 项 | 说明 |
|---|---|
| 仓库 | `AUS_BS_CAT_Model`（默认 `D:/Projects/AUS_BS_CAT_Model`，先确认路径） |
| 规则表 | `configs/`（`category_catalog.json`、`pipeline.json` 等）与 `<engine>_engine/resources/`；initial 例外，规则表是 `initial_engine/merchant_kb.csv` |
| 白名单 | `scripts/_internal/common.py` 的 `RULE_FILES`；`target_file` 必须命中 |
| 引擎声明 | 阶段一生成的 `<review-dir>/claims.csv`；目标引擎必须在其中有声明 |
| 审核/历史目录 | 必须在目标仓库外，例如 `reviews/bscat-<时间戳>/` |

### 输入与默认值

| 输入 | 默认值 | 说明 |
|---|---|---|
| 仓库 | `D:/Projects/AUS_BS_CAT_Model` | 先确认路径 |
| 输入样本 | 用户提供 | `.xlsx`、`.xlsm` 或 `.csv` |
| 审核/历史目录 | `reviews/bscat-<YYYYMMDD-HHMMSS>` / 用户提供 | 运行期与 `apply --history`；都必须在目标仓库外 |
| 解释器 | 用户已有环境 | 例如 `.venv-bscat`；不要新建 venv |

输入列：`miss`/`diff` 必须有**期望分类**；`extra` 的期望分类可为空，表示“应保持未分类”；交易描述始终必需。

## 1. 入口与档位

### 1.1 route

```bash
# 整批样本：只按期望/当前分类分组，不跑流水线
<venv-python> scripts/bscat.py route --repo <repo> --input <file> [--sheet <name>] [--problem-type all] [--out <review-dir>]
# 单任务
<venv-python> scripts/bscat.py route --repo <repo> --category <expected> [--engine <engine>] --problem-type <miss|extra|diff> [--current-engine <engine>] [--rows <n>] [--text "<交易描述>"] [--layer institution|rule]
# 文本意图：只有“文本 X 应归为 Y”，没有样本行
<venv-python> scripts/bscat.py route --repo <repo> --category <expected> --problem-type <miss|diff> --text "<交易描述>" [--engine <engine>] [--layer institution|rule] [--out <review-dir>]
```

`--text` 只作层/归属提示，不能证明规则已覆盖该文本；文本意图仍要 `search` 查重和合成探针验证。商户名/品牌名传 `--layer institution`（gambling/rent），通用博彩词/费用词走 rule/费用表；`--layer` 自动提示只是启发式，归属以 §6.1 和 `search` 为准。

`route` 读 `rule_index.json`，回答六问：①目标引擎/表与 `attribution_risk`；②匹配与归一化；③执行顺序；④能否追加（`can_append`/blockers）；⑤后续覆盖/阻断；⑥升级条件（`escalation_reasons`/`validation_tier`）。语义见 `references/rule-index.md`。

输出 `route_decision.json`；`prepare` 另写 `task_profile.json`。路由只是**入口判断**，阶段三仍按 §6.1 复核归属；`route --input` 只按输入自带分类分组，不跑流水线，真实问题类型/归属以 `prepare` 为准。出现任一情况按 `full`：`validation_tier=full`、`index_warnings` 涉及目标引擎/表、目标引擎/表无法确定或表头不一致、目标为 income/liability/transfer（文本意图快路径停止）、计划 `append` 但 `can_append=false`（`extra` 的 delete/收窄不适用）、`attribution_risk` 非空且复核后仍无法归属。

### 1.1.1 I1 入口定位（强制）

`route` 之后、`prepare` 之前，用 `AskUserQuestion` 确认“修哪类、往哪个方向修、覆盖到哪、交付到哪一步”，把答案写入 `<review-dir>/intake.json`。**不要跳过**，除非用户在本轮消息里已经明确说清问题类型和修复方向；这种情况把 `source` 记为 `user_message` 并在对话里复述确认。

**弹窗一（3 问，问题类型 / 覆盖范围 / 交付方式）**：

```text
问题类型：miss（漏识别）/ diff（错分）/ extra（多识别）/ 全部或不确定
覆盖范围：只覆盖样本文本 / 同聚类相似文本（推荐）/ 主动扩大到相似商户 / 不确定，按最窄建议
交付方式：分析 + 改表 + 前后验证（推荐）/ 只评估不改 / 做到 dry-run 为止 / 先出报告再决定
```

**弹窗二（1 问，修复方向；选项随问题类型变化）**：

| 问题类型 | 选项（最多 4 个） |
|---|---|
| `miss` | 新增规则优先（推荐）/ 先修旧规则 / 只做关键词变体 / 按证据决定 |
| `diff` | 删旧规则 + 新增规则成对处理（推荐）/ 只修旧规则 / 只新增规则 / 按证据决定 |
| `extra` | 删除误命中规则（推荐）/ 收窄误命中规则 / 只出建议不写 / 按证据决定 |
| `all` | 按证据分别处理（推荐）/ 全部新增优先 / 全部修正旧规则 / 只出建议 |

`AskUserQuestion` 一次调用里后一问不能依赖前一问的答案，所以问题类型与修复方向**必须分两次弹窗**；不要把方向选项硬塞进同一次调用。

写文件：

```json
{
  "problem_type": "miss",
  "direction": "append_first",
  "scope": "cluster",
  "delivery": "full",
  "interaction_mode": "standard",
  "source": "popup",
  "note": "用户确认：先处理漏识别，最窄新增，覆盖同聚类"
}
```

- `problem_type`：`miss|diff|extra|all`；`prepare`/`propose` 不传 `--problem-type` 时自动读取，`miss|diff|extra` 仍自动写 `target_scope.json`。
- `direction`：`append_first|fix_existing|delete_extra|paired|evidence_driven|report_only`；`all` 可用 `directions` 对象按类型分别记录。它只约束设计倾向，`propose` 仍只生成证据支持的候选。
- `scope`：`sample_only|cluster|broader_similar|not_sure`。选 `broader_similar` 时，任何超出同聚类的模式都必须在 I4 里再确认一次。
- `delivery`：`full|precheck_only|dry_run_only|report_first`。`precheck_only`/`report_first` 停在阶段四报告；`dry_run_only` 允许到阶段六 dry-run，但不得 `--apply`。
- `interaction_mode`：默认 `standard`；用户说“少问一点”用 `minimal`（跳过 I4 弹窗和大部分条件门禁，但仍打印匹配明细、保留 `zero_rows`/归属风险/`match_key`/`source_trace`/`simulate_mismatch` 硬触发，并受审批门禁约束），说“每一步都确认”用 `guided`（所有条件门禁都问）。
- 用户改口：更新 `intake.json` 后重跑 `prepare`/`propose`；已生成候选与新的问题类型/方向冲突时，先停下说明，不要静默沿用旧候选。

### 1.2 验证档位

| 档位 | 典型条件 | prepare | 写后验证 |
|---|---|---|---|
| **T0 target** | 单一非结构性引擎的干净 `append`；无覆盖/阻断；影响 ≤5 行 | `--diagnose auto`（通常跳过代码诊断） | `apply --verify-scope target` |
| **T1 local** | `edit`/`delete`/`keyword_update`；diff/extra；局部冲突；6–20 行；非结构性跨引擎关系；gambling/rent 双层未定 | `--diagnose auto` | `--verify-scope local` |
| **T2 full** | income/liability/transfer；`priority`/`catalog`；跨引擎守卫或删除接管不明；>20 行；索引漂移 | `--diagnose always` | `--verify-scope full`（自动跑 baseline） |

档位只改变诊断深度和验证范围，不改变任何硬门禁：阶段四校验、阶段五审批、dry-run、写入前备份、写后真实流水线回归和回滚在任何档位都必做。

写后验证 `scope`：`target` = 目标聚类行 + 命中新/旧 pattern 的样本行；`local` = target + 同引擎已认领行 + 同期望/同输出分类行；`full` = 全部样本行 + 全部引擎 claim + `baseline.py diff`。即使 `target`/`local`，**范围外行级回归/丢失分类仍硬失败**；范围外 claim 变化只要求复核，并提示改 `full`。

## 2. 快路径（1–20 行 / 文本意图）

快路径不是新流水线，也不新增命令：`route` 分流后复用 `prepare → propose → revise → check → approve → apply`。阶段零同步、阶段四校验/对账、阶段五审批、dry-run、写后回归和回滚一个都不能少。**只压缩“看哪些、跑多深”，不压缩门禁。**

### 2.1 适用与不适用

适用：1–20 行、单一期望分类/归属引擎，路由 A 类（`feasible_add`、`same_owner_wrong_category`、`extra_current_rule` 或未分类）；单个文本意图且目标是 fee/catch_all/dishonour/all_other_credit/initial/gambling/rent 的非结构性追加；不涉及 `priority`/`catalog`。

不适用（停下或转完整流程）：income/liability/transfer（T2）；B 类 `overwritten_by_later`/`delete_or_edit_current_rule`（先处理当前/覆盖规则）；C 类 `blocked_guard`/`owner_disabled`（人工策略）；目标表不支持所需匹配方式（all_other_credit 仅 keyword、initial 仅大写全词），或 `edit`/`delete` 无法恰好命中 1 行；>20 行或跨多个引擎（可 `route --input` 分流，但走完整流程）。

### 2.2 样本快路径

```bash
<venv-python> scripts/bscat.py route --repo <repo> --input <file> [--sheet <name>] --problem-type <miss|extra|diff|all> --out <review-dir>
# I1 弹窗后写 <review-dir>/intake.json
<venv-python> scripts/bscat.py prepare --repo <repo> --input <file> --review <review-dir> --diagnose auto --sync-max-age 300
# I2 若 parse_review.json 要求（guided 无触发也要求）：弹窗后写 dialog_decisions.json#parse
# I3 若 attribution_review.json 要求（guided 无触发也要求）：弹窗后写 dialog_decisions.json#attribution
<venv-python> scripts/bscat.py propose --repo <repo> --review <review-dir>
# I5 若触发（guided 全部修订）：先 revise --dry-run，看 revise_preview.json，确认后写 dialog_decisions.json#revise
<venv-python> scripts/bscat.py revise --repo <repo> --review <review-dir> --patch <review-dir>/dialog_patch.json [--dry-run] [--validate]
<venv-python> scripts/bscat.py check --only validate --repo <repo> --review <review-dir>
# I4 按 match_review.json 弹窗后写 <review-dir>/match_review_decisions.json
<venv-python> scripts/bscat.py check --only reconcile --repo <repo> --review <review-dir>
# I6 若 precheck.json 要求（guided 有可审批候选也要求）：弹窗后写 dialog_decisions.json#precheck
<venv-python> scripts/bscat.py approve --build --review <review-dir> --confirmed <IDs> [--rejected <IDs>] [--risk-acknowledged <IDs>]
<venv-python> scripts/bscat.py apply --repo <repo> --review <review-dir> --approvals <review-dir>/approvals.json
<venv-python> scripts/bscat.py apply --repo <repo> --review <review-dir> --approvals <review-dir>/approvals.json --apply --history <history-dir> [--allow-high-risk] [--verify-scope auto|target|local|full]
```

要点：

- `route --input` 只按输入自带分类分组；真实问题类型/归属以 `prepare` 的 `evidence.json` 为准。
- I1 的 `problem_type` 写入 `intake.json` 后，`prepare`/`propose` 默认读取；`miss|diff|extra` 自动写 `target_scope.json`，阶段二不弹范围窗；`all` 且仅一组时也不弹，多组用 `AskUserQuestion` 选。需要临时覆盖时仍可显式传 `--problem-type`。
- `--diagnose auto` 是默认，不要传 `never`；干净 miss 通常跳过诊断，diff/extra 或结构性引擎跑只读诊断。
- `check --only validate` 写 `candidates.json`/`validation.json`/`approval_groups.json`/`match_review.json`/`validation_report.md`；**先完成 I4 并写 `match_review_decisions.json`，再** `check --only reconcile` 做影响面对账（约 0.5s）并写 `precheck.json`；若 I6 触发，先确认 `dialog_decisions.json#precheck`，然后进阶段五。仅需 CSV/`candidate_report.md` 时才 `render`/完整 `check`。
- 写入前重跑 `prepare` 用 `--sync-max-age 0`。
- 735MB initial pickle 和全引擎回放是固定成本，1 行样本也要付，不承诺“秒级”。

### 2.3 文本意图快路径

用户只说“交易文本显示为 `X` 的需分为 `Y`”或“`X` 应打为 `Y` 而不是 `Z`”时，按五步走，全部使用现有命令。

1. **意图与路由**：拆出期望分类 `Y`、文本模式 `X`（原文）、匹配语义（包含/前缀/精确/正则）、问题类型（`miss` 当前未分类 / `diff` 当前是 `Z`）、`dr_cr`/bank/amount/account_type 限定。语义不明时用 `AskUserQuestion` 只问缺项；默认整段“包含”、完整文本“精确”、“以…开头”取“前缀”，用户给正则才用正则。执行 `route --text`（见 §1.1），读 `route_decision.json` 的 `answers.q1/q2/q4/q5/q6`；目标为 income/liability/transfer、`can_append=false`、`append_blocking_rules` 非空、`attribution_risk` 非空或目标表不支持所需语义时停止。仍按 §1.1.1 写 `intake.json`；问题类型已由用户明说时可只确认方向，`miss` 默认 `append_first`、`diff` 默认 `fix_existing`。
2. **查重**：
   ```bash
   <venv-python> scripts/bscat.py search --repo <repo> --query "<X>" --file "<target_file>" --json
   <venv-python> scripts/bscat.py search --repo <repo> --query "<X>" --fuzzy --json   # 商户名/品牌名还要查 merchant_kb
   ```
   完全相同且同分类 = no-op；已有商户变体 = `keyword_update`；过宽旧规则 = 评估 `edit`/`delete`/收窄；零命中 = 进入探针。
3. **合成探针**：没有真实样本时，在**仓库外**审核目录写明确标注 `synthetic` 的 CSV，至少 2 条正例（不同前缀/格式）+ 1 条反例（应保持原分类或未分类）。推荐列：`application_id,transaction_id,text,amount,dr_cr,expected_category,current_category,bank_account_id,account_type,bank,transaction_date`。反例不要用“也会命中新模式”的文本；探针只是 match/流水线级预演，报告写明“合成探针”。
4. **生成并改成精确模式**：`prepare ... --diagnose auto --sync-max-age 300`；若 `parse_review.json` 触发 I2 先确认；若有 `attribution_review.json` 触发项先完成 I3；再 `propose ...`。`propose` 按聚类各给一条草稿；一个文本意图通常合并成一条最窄规则。用 `revise --patch` 改成第 1 步的语义；若触发 I5，先 `--dry-run` 看 `revise_preview.json` 并确认；重复/更宽的降级 `suggestion`，在 `reason`/`rationale` 写明“不应匹配什么”。`target_file`/`engine` 不能在 `revise` 里改；给错表时降级并停下重新路由。pattern 写法见 §4.4。
5. **校验、I4、审批、写入、结果**：`check --only validate` 后按阶段四 I4 段确认匹配逻辑并写 `match_review_decisions.json`，再 `reconcile`；若 `precheck.json` 触发 I6 先确认，再阶段五、阶段六；写后成功按 I8 问下一步。含反例的探针用 `--verify-scope full`；低风险批量前先做归属复核（阶段五 5.2）。结果卡写明正例修复数、反例是否保持、范围外回归、实际档位、规则文件 diff，以及“证据是合成探针，建议真实样本复跑后再提交”。

### 2.4 硬停条件

- `propose` 对 A 类意图只给 `suggestion`，或候选的 `target_file`/`engine` 与路由不一致；
- `search` 发现已有规则已覆盖该文本（no-op），仍有人想追加；
- 结构性引擎、B/C 状态、`priority`/`catalog` 变更；
- 探针反例被误伤、`validation_after` 出现范围外回归或丢失分类；
- `apply` 的 `git diff --name-only` 出现未审批文件。

以上任一情况：停在当前阶段，按 §9 故障排查处理，不进入审批或写入。至少遵守：同步非 `0 0` 停；校验/dry-run 失败停；写入后回归或未审批文件立即 `apply --rollback --apply`。

## 3. 阶段流程

### 3.0 总览与命令索引

入口 `route` → **I1 入口定位** → 零同步 → 一解析/取证/诊断（触发时 **I2**）→ 二选范围 → 三设计候选（触发时 **I3**、**I5**）→ 四校验/对账（**I4**；触发时 **I6**）→ 五审批 → 六 dry-run/写入/验证（成功后 **I8**）→ 七报告。硬门禁是 I1、I4、阶段二范围、阶段五审批、阶段六写入确认；I2/I3/I5/I6/I8 是条件门禁，触发时必须问、答案必须落 `dialog_decisions.json`。其余自动。阶段零/校验/对账/dry-run 失败或写后回归必须停下，不能跳阶段；入口失败或索引漂移按 `full`。任何档位都不能跳过硬门禁。

单步重跑（排错用）：

```bash
<venv-python> .../bscat.py prepare --only sync|parse|evidence|diagnose --repo <repo> --review <dir> [参数]
<venv-python> .../bscat.py check   --only validate|render|reconcile|preview --repo <repo> --review <dir> [参数]
```

#### 3.0.1 交互门位置与跳过条件

| 门 | 位置 | 产物 | 何时可跳过 |
|---|---|---|---|
| **I1 入口定位** | `route` 后、`prepare` 前 | `intake.json` | 仅当用户本轮已明确问题类型 + 修复方向；记录 `source=user_message` 并复述 |
| **I2 解析确认** | `prepare` 解析后、`propose` 前 | `parse_review.json` → `dialog_decisions.json#parse` | 无触发；`guided` 也确认；`minimal` 仍硬停 `zero_rows` |
| 阶段二范围 | `prepare` 后、`propose` 前 | `target_scope.json` | `problem_type=miss/diff/extra` 自动写；`all` 只有一组时 |
| **I3 归属/目标表** | `prepare` 写 `attribution_review.json` 后、`propose` 前 | `attribution_review.json` → `dialog_decisions.json#attribution` | 路由唯一且无归属风险；`guided` 逐聚类确认；`minimal` 只问归属风险类触发 |
| **I4 匹配逻辑** | `check --only validate` 后、`reconcile`/审批前 | `match_review_decisions.json` | `interaction_mode=minimal`；仍须打印匹配明细，`approve` 跳过哈希门禁 |
| **I5 修订预览** | 用户要求修改后、`revise` 非 dry-run 写入前 | `revise_preview.json` → `dialog_decisions.json#revise` | 无 `match_key`/`source_trace`/多字段变更；`guided` 所有修订确认；`minimal` 只强制前两类 |
| **I6 校验/影响面预审** | `check --only reconcile` 后、阶段五前 | `precheck.json` → `dialog_decisions.json#precheck` | 无触发；`guided` 有可审批候选也确认；`minimal` 仍强制 `simulate_mismatch` |
| 阶段五审批 | `reconcile` + I6 后、`approve --build` 前 | `approvals.json` | 不跳过；无候选或用户停止时直接结束 |
| 阶段六写入 | dry-run 与 confirmed 预览后 | `--apply` 授权 | 不跳过；`delivery` 为 `precheck_only/report_first/dry_run_only` 时不写入 |
| **I8 写后下一步** | `apply --apply` 且 `validation_after` 成功后 | `dialog_decisions.json#outcome` | 写后失败/未审批文件/硬回归时不问，走回滚路径 |

### 阶段零 — 规则同步

`prepare` 默认 `git fetch` + `git rev-list --left-right --count HEAD...@{upstream}`，必须是 `0 0`，否则停止让用户手动同步；不做 stash/checkout/reset，记录 `git status --short`。

快路径 `--sync-max-age 300`：仅当仓库路径、remote、分支、upstream、HEAD 与缓存一致且缓存来自干净的 `0 0` fetch 时复用；工作区始终重读。缓存默认在系统临时目录（`--sync-cache` 可指定），**必须在仓库外**；命中时 `git_sync.json` 写 `cache_reused`/`fetched_at`/`cache_age_seconds`。默认 `0` = 每次 fetch，写入前重跑用 `0`；非 `0` 不写缓存并丢弃旧缓存，损坏/过期重新 fetch。`prepare --only sync` 接受同样参数。

### 阶段一 — 解析、取证、按需诊断

```bash
<venv-python> scripts/bscat.py prepare --repo <repo> --input <file> --review <review-dir> \
    [--problem-type <miss|extra|diff|all>] [--intake <intake.json>] [--diagnose auto|always|never]
```

不传 `--problem-type` 时读 `<review>/intake.json`；`--intake` 可指向别的入口文件。常用参数：`--blank-expected extra`、`--sheet`、`--column-map`、`--app-id`、`--dedupe-keys suffix`、`--drop-missing-expected`、`--sync-max-age 300`（写入前用 `0`）、`--sync-cache`；`--skip-sync-check` 仅限离线/测试。若 CLI 的 `problem_type` 与 intake 不一致，`prepare` 会打印覆盖提示并以 CLI 为准；后续 `propose` 也要显式传同一个 `--problem-type`，否则它会按 `intake.json` 过滤。

`--diagnose auto`（默认）只对 diff/extra、结构性引擎、跨引擎守卫、路由异常或 `full` 运行 `diagnose_code.py`，干净 miss 不强制看源码；`always` 用于查看代码或索引/表头可疑；`never` 仅用户明确要求，且 `task_profile` 需要诊断时必须提示风险。

始终写 `rule_index.json`（引擎/表/分类/覆盖关系）、`task_profile.json`（逐聚类档位、升级原因、目标表、诊断要求）、`parse_review.json`（I2 蓝图）和 `attribution_review.json`（I3 蓝图）。

**I2 解析确认（条件门禁）**

`parse_input.py` 写 `parse_review.json`。仅在触发时向用户确认：`parse_warnings`（非信息性警告）、`multiple_sheets`（多 sheet 且未显式 `--sheet`）、`column_ambiguity`（同一字段命中多个列别名）、`missing_expected`/`missing_expected_column`（期望标签缺失）、`label_conflict`（输入自带期望与当前分类不一致）、`zero_rows`（0 行，硬停）、`problem_type_mismatch`（入口类型与样本分布不符）。无触发时 `required=false`，`standard` 跳过；`guided` 即使无触发也确认第 1 问。

弹窗 1–3 问，全部选项必须有推荐项和 `stop`：

1. 解析结果：`continue`（推荐）/ `change_columns` / `change_sheet` / `adjust_problem_type`（仅类型分布不符）/ `stop`。
2. 问题类型分布（仅 `problem_type_mismatch`）：`follow_sample`（推荐，重跑 `--problem-type all`）/ `keep_intake` / `one_type` / `stop`。
3. 期望分类权威（仅标签类触发）：`sample`（推荐）/ `current`（需交换两列并重跑 `prepare --column-map`）/ `manual` / `stop`。

答案写入 `dialog_decisions.json#parse`，必须带 `fingerprint`（取自 `parse_review.json`）和 `decided_at`；`change_*`/`adjust_problem_type` 表示“先重跑再继续”，重跑前 `propose` 会硬拒绝。`zero_rows` 任何模式都停。细节与 schema 见 `references/rulebook.md`。

检查：行数 > 0；问题类型一致；`miss`/`diff` 期望分类已识别；`extra` 空期望按“保持未分类”；交易描述/唯一键已识别；目标引擎在 `claims.csv` 有声明；`validation_tier` 与逐聚类档位可解释。任一不满足先停下，不手工模拟引擎，**不要设计候选**。

### 阶段二 — 分析摘要与靶向

读 `evidence.json`、`clusters.csv`、`claims.csv`、`gap_summary.md`、`task_profile.json`，按 `engine × problem_type` 分组（miss/diff 用 `owner_engine`，extra 用 `current_engine`），打印行数/问题数/聚类数/引擎组/诊断分布及每组 `T0/T1/T2`、`needs_code_diagnosis`、升级原因；档位以 `task_profile.json` 为准。

用 **AskUserQuestion 多选**确认目标：按问题行数降序最多 4 组；推荐项第一并标“（推荐）”，不默认全选；超过 4 组保留前 3 组，第 4 项“其余全部组（N 簇）”。已指定或仅一组时不问；engine 不明的聚类不进入选项，列为未处理。

I1 的 `problem_type=miss|diff|extra`（或 CLI `--problem-type`）自动写 `target_scope.json`（`source=user_prespecified`），阶段二不弹范围窗；只有 `all` 或要求收窄时才人工选。`selected_groups[].cluster_ids` 必须来自 `clusters.csv`，`deferred_groups` 记未处理项；未选聚类不生成候选，阶段七列为“仍未处理”；聚类数 0 时直接交付“未发现差异”。

### 阶段三 — 设计候选

先用 `task_profile.json`（或重跑 `route`）的六问定位，再读 §6.1、§4.2、§6.3 复核归属，并与 `category_catalog.json` 的 `owner_engine_id` 交叉验证；结论不一致时停下。**不要重抄决策树。** 设计时同时读 `intake.json` 的 `problem_type`/`direction`/`scope`：`direction` 只影响“优先新增还是优先改旧规则”的设计倾向，不改变证据与归属；`scope=sample_only` 时不得把 pattern 扩到样本外，`broader_similar` 必须逐条说明扩大依据。

- `attribution_risk` 非空先复核归属；候选只给归属引擎，商户名/品牌名走 `initial`，绝不进 `catch_all`。
- `can_append=false` 或 `append_blocking_rules` 非空时追加不生效，改走 `edit`/`delete`/`keyword_update` 或升级 `full`；按 `routing_actions` 处理顺序/守卫，不改 `priority`。
- **先溯源、再设计**：从证据/路由定位实际命中的表并写 `source_trace`，不许先猜引擎；`target_file` 必须命中 `RULE_FILES`，`edit`/`delete`/`keyword_update` 的 `match_key` 恰好命中 1 行。
- 按 §6.3 处理 miss/extra/diff；一条候选只改一行，用最窄模式并写明不应匹配什么；规则错误才 `edit`/`delete`；证据不足用 `suggestion`；代码诊断只进 `rationale`/`suggestion`，绝不生成改 `.py` 的候选。

**I3 归属/目标表确认（条件门禁，`propose` 前）**

`prepare` 写 `attribution_review.json`，按聚类列出推荐表、候选表、证据样本和 `triggers`。触发任一情况时必须先确认：`attribution_risk`、`owner_ambiguity`（多个启用归属引擎）、`unresolved_layer`（gambling/rent 分层未定）、`multiple_candidate_tables`、`missing_target_file`、`missing_candidate_file_hint`、`routing_obstacle`（`blocked_guard`/`overwritten_by_later`/`owner_disabled`/`catalog_missing`/`delete_or_edit_current_rule`）。路由唯一且无上述风险时 `required=false`，`standard` 跳过；`guided` 仍逐聚类确认。

每个触发聚类问一次，选项为 `recommended`（推荐）/ `alternative`（改用候选表，写 `engine`+`target_file`）/ `evidence`（由证据决定，同时写 `fallback`）/ `stop`。`fallback` 只能是 `suggestion`（降级为建议）/ `search_then_retry` / `full_diagnosis` / `stop`；后三者表示尚未定表，会阻塞 `propose`。答案写入 `dialog_decisions.json#attribution`，带 `fingerprint` 和 `decided_at`。

`propose` 会按决策过滤：`stop` 丢弃该聚类草稿；`recommended`/`alternative` 只保留所选物理表；`evidence` 保留证据表，`fallback=suggestion` 时降级。任何 `trace_method=engine_default` 的规则草稿若所在聚类没有 I3 决策，`propose` 硬停——禁止用默认表兜底进入审批。I3 未触发时，旧决策不会过滤新一轮草稿；只有仍走 `engine_default` 的草稿需要补一条针对性决策。若 `attribution_review.json` 缺失或已更新（fingerprint 变化），这条救援决策会被拒绝，必须重新确认 I3。

**3.1 候选种子生成**

```bash
<venv-python> scripts/bscat.py propose --repo <repo> --review <review-dir> \
    [--problem-type miss|extra|diff|all] [--intake <intake.json>] [--dry-run] [--validate]
```

不传 `--problem-type` 时读 `intake.json`，并把 `problem_type`/`direction`/`scope`/`delivery` 记入 `proposal_summary.intake`。读 `evidence.json`/`target_scope.json`，按聚类生成草稿，遵循 `routing.actions`，只给归属引擎。新候选一律 `status=pending`、`needs_review=true`，**绝不自动确认**；合并进已有 `candidates.json`，不覆盖旧候选。覆盖 miss 的 `append`、diff/extra 的 `edit`/`delete`/`keyword_update`；结构性引擎的 diff 只给 `suggestion`。生成器只是草稿：逐条复核 `target_file`、实际规则行、最窄模式和“不应匹配什么”，错的改写/替换/降级。

**3.2 配置表溯源（强制）**

每条规则候选都必须把“证据命中哪张表、为什么在这张表补”写进 `source_trace`；字段/示例/`override_reason` 见 `references/rulebook.md` 1.1。缺 `source_trace`、`target_file` 不一致、覆盖 `candidate_file_hint` 却没有 `override_reason`、命中既有规则不在 `target_file` → 校验报错，不得进入审批。

- 溯源必须来自**实际读取的规则表**：先 `bscat.py catalog --repo <repo> [--engine <engine>]`（或 `references/rule-file-catalog.md`）确认存什么/列/可追加性，再用 `search`/读表确认语义；不得按引擎名猜表。
- `candidate_file_hint` 只是路由草稿。liability 默认指向 `counterparty_keyword_rules.csv`；证据属催收/债务整合/透支/信用卡/房贷车贷时，必须改到对应表并写清 `override_reason`。
- `trace_method=engine_default` 说明没有证据级提示，审批时必须展示这条 warning。
- liability 的 `product_type` 决定最终归属（`loan_NNN` 只是编号，不能从前缀读产品类型），**只能取目标表已有值**（常见值见 §5.6）；`counterparty` 必须与证据机构名一致，不得猜；不确定降级 `suggestion`。

**3.3 检索与 keyword_update**

```bash
<venv-python> scripts/bscat.py search --repo <repo> --query <商户或规则片段> [--fuzzy] [--limit 20] [--min-score 0.6] [--file <规则表>] [--json]
```

默认流式检索 `initial_engine/merchant_kb.csv`（约 874k 行）。**新增 initial 商户前必须查重**；已有变体按本节判断 `append` 还是 `keyword_update`。

`keyword_update` 只写 `initial_engine/merchant_kb.csv`。`match_key` 用唯一 `merchant_name`，恰好命中 1 行；`row` 写完整 `keywords` 或 `add_keywords`/`remove_keywords`。关键词须预清洗为**大写、仅 `[A-Z0-9 ]`、压缩空格**；独立 STOPWORD、>50 变体、删空会被拒绝或转 `delete`。商户不存在用 `append`；分类冲突用 `edit` 改 `category`。

**3.4 对话内修订（默认）**

**人工修改只在对话框完成。** `check` 渲染的 `candidates/*_candidates.csv`/`candidates_all.csv` 只是**只读导出**；不要让用户打开/编辑 CSV，也不要只丢文件路径。逐条展示候选后，把用户明确要求的修改写成 `dialog_patch.json`，再执行：

```bash
<venv-python> scripts/bscat.py revise --repo <repo> --review <review-dir> \
    --patch <review-dir>/dialog_patch.json [--dry-run] [--validate] \
    [--preview-json <review-dir>/revise_preview.json] [--decisions <review-dir>/dialog_decisions.json]
```

**I5 修订预览（条件门禁）**：用户要求修改后，先跑 `--dry-run`。`revise` 每次都写 `revise_preview.json`，列出将改的候选、字段、触发项（`match_key` / `source_trace` / `multiple_changes` / `multiple_fields`）和 `fingerprint`。触发时必须用 1 个 `AskUserQuestion` 问“应用 / 继续编辑 / 取消”；选“应用”后在 `dialog_decisions.json#revise` 写 `decision=apply`、`patch_sha256`（取自预览）、`decided_at`，再跑不带 `--dry-run` 的同一条命令。`continue_editing`/`cancel` 会硬拒绝写入；`patch_sha256` 过期（补丁改过）也会硬拒绝，必须重新预览。`interaction_mode=guided` 时所有修订都要确认；`interaction_mode=minimal` 只在改 `match_key`/`source_trace` 时要求 I5；单字段模式微调且用户已明确要求时可直接应用。

补丁格式见 `references/rulebook.md` §3；只接受 `row`/`match_key`/`reason`/`rationale`/`source_trace`/`operation`，**不接受 `status`**。`operation` 只能降级为 `suggestion`，且不能与 `row`/`match_key` 同次提交。任一变更都会重置为 `pending` 并清空 `validation`，必须重新 `check`、重新审批；未知/重复 ID、非法字段、定位 0/多行一律硬错误。CSV 按最新候选重渲染，不用旧 CSV；兼容模式下直接编辑 CSV 也会生成 `revise_preview.json` 并走 I5 的 `patch_sha256` 门禁，不能绕过 `match_key`/`source_trace` 确认。每个候选的 `evidence_cluster_ids` 必须属于 `selected_groups`；范围无可落地候选时说明原因，不进入审批或写入。

### 阶段四 — 校验与 AI 预筛

```bash
<venv-python> scripts/bscat.py check --repo <repo> --review <review-dir>
```

有对话修改时先用 `revise --patch ... --validate` 合并再重跑。**快路径校验见 §2.2**：`validate` 后必须再跑 `reconcile`；仅需 CSV/`candidate_report.md` 时才 `render`/完整 `check`；`--only validate` 不渲染 CSV 不是错误。

读 `validation_report.md`/`candidate_report.md`，修正或降级无效候选直到零无效。重点看 `conflicts`、`already_correct`、`resolved_rows`、`shadowed_rows`、`conflicts_extra`、`delete_blocked_extra_rows` 和分类归属。`shadowed_rows`（initial 专用）是文本命中但被更长关键词或同串后置行抢走，不计入 `resolved_rows`，`warnings` 列出胜出项。

**影响面/命中数对账（必做）**：生成器命中数应与模拟器修复/增益行数基本一致；对不上先怀疑模拟器未按该引擎归一化/匹配语义跑，再怀疑规则。`extra` 的删除/收窄要区分“删后仍被别的引擎接管”与“模拟器语义不对”。对账写 `impact_reconcile.json`；`simulate_mismatch=true` 时 `check` 非零退出，必须先查清。

**I6 校验/影响面预审（条件门禁，阶段五前）**

`reconcile` 每次都写 `precheck.json`，按可审批候选汇总触发项：`simulate_mismatch`、`validation_warnings`、`shadowed_rows`、`conflicts`、`engine_default_trace`、`approximate_effect`（edit/delete/keyword_update/配置变更的真实效果只能写后观察）、`reconcile_incomplete`（缺少样本上下文或 append 对账未完成）。无触发时 `required=false`，`standard` 跳过；`guided` 且存在可审批候选时也要求确认；`minimal` 只强制 `simulate_mismatch`。

触发时按顺序问 2–3 问，每问都有停止项：

1. 是否继续：`continue`（推荐，仅当影响可接受）/ `revise` / `degrade` / `stop`。
2. 影响面处置：`accept`（推荐）/ `narrow` / `need_samples` / `stop`。
3. 写后验证范围：`auto`（按 `task_profile`，推荐）/ `local` / `full` / `current`（沿用命令默认）。

答案写入 `dialog_decisions.json#precheck`，带 `fingerprint` 和 `decided_at`；`decision` 必须是 `continue`，`impact` 必须是 `accept`。`simulate_mismatch` 还必须写 `acknowledge_mismatch=true`——弹窗前必须逐条解释差异，不能把偏差说成“零增益”或静默继续。`approve --build`/审批导入会再次执行 I2/I3/I6 门禁；`apply --verify-scope auto` 读取 I6 的 `verify_scope`（命令行显式指定始终优先）。

**审批分组**：`check` 写 `approval_groups.json`，按 `target_file` 汇总 `pending` 且 `valid` 的候选，给出 `risk_groups`/`popup_plan`；`suggestion`、已拒绝和无效候选不进入分组。

校验即 error：缺 `source_trace`、`target_file` 与溯源不一致、命中既有规则不在 `target_file`、`product_type` 不在目标表已有取值。

**AI 预筛分档**（只作提示，不改变状态；结果写入 `risk_level`，阶段五沿用）：

| 档位 | 典型条件 |
|---|---|
| 🟢 low | 干净 append、至少修复一行、无冲突、不命中已正确行、样本命中 ≤5 行，且引擎不是 income/liability/transfer |
| 🟡 medium | edit/delete 未触发高风险原因；append 命中 >5 行；edit/delete 零增益；有 warning 或跨引擎接管不明 |
| 🔴 high | income/liability/transfer 的任何候选、`priority`/`catalog` 变更、冲突/误伤已正确行、extra append、删除伤及正确行或删后仍被同引擎命中 |
| ⚫ invalid | 冲突、给 extra 新增分类或删除后仍命中；不进审批，先修正/降级 |

“sample simulation is match-level only” 不作为降级依据。

**I4 匹配逻辑确认（强制；`interaction_mode=minimal` 除外）**

`check --only validate` 同时写 `match_review.json`：按 `target_file` 分组，每条待审批规则候选给出 `match_mode`、`pattern`、`recommended`、`options`、`positive_examples`、`negative_examples`、`expected_effect`、`row_hash` 和 `question`。选项由目标表能力生成（`common.match_review_options`）：支持行内 regex 的表给“按建议 / 更窄：整串精确 / 更宽：前缀 / 拒绝”；纯关键词表（`all_other_credit`、initial、liability counterparty / word_boundary / substring）只给“按建议 / 调整关键词 / 拒绝”；gambling/rent 的 `source=institution` 行只允许调整变体。`all_other_credit`、initial 等不支持行内正则的表**不会**出现正则选项。**不要自行发明表不支持的匹配语法。**

逐表执行：

1. 按 `groups[].order`，弹窗前打印该表 `table_note`、每条候选的 `pattern`、`expected_effect`、`positive_examples`（应命中）与 `negative_examples`（需避免/仍被其它规则命中）以及 `row_hash` 前 19 位。
2. **每张表独立调用 `AskUserQuestion`**，问题顺序高→中→低；高/中风险每条候选**一个单选问题**，选项直接用 `options[].label`，`description` 写 `options[].note` + 一正一反示例；低风险整组一问：“全部按建议（N 条）（推荐）/ 逐条调整 / 全部拒绝”。一次最多 4 问，超过则同表续窗。
3. 用户选“调整关键词/变体”或“更换 match_key”时，用 `Other` 收自由文本；不要把它硬塞进预设选项。
4. 每个有决策的候选写一条 `match_review_decisions.json`：`candidate_id`、`selected`（`recommended|narrower|wider|adjust|custom|reject`）、`row`（机械选项的可选补丁）、`row_hash`、`reason`。`reject` 不进入阶段五确认，后续按 `rejected` 处理。低风险批量的 `batch_*` 只用于弹窗：选“全部按建议”就为组内每条写 `recommended`，选“全部拒绝”就逐条写 `reject`，选“逐条调整”则同表续窗逐条问，最终仍按候选写独立决策；`batch_*` 不能写进 `selected`。
5. 哈希规则：
   - `recommended`：`row_hash` 用该候选当前 `row_hash`（等于 `options[recommended].row_hash_after`）。
   - `narrower`/`wider`：先把 `options[].row` 写入 `dialog_patch.json`，执行 `revise --patch ... --validate`；决策的 `row_hash` 用该选项的 `row_hash_after`。重新校验后当前候选哈希必须与它一致。
   - `adjust`/`custom`：先 `revise --patch ... --validate`，再从重新生成的 `match_review.json` 读取该候选新的 `row_hash` 写入决策；不要用修改前的哈希。
6. 全部决策写完后重跑 `check --only validate`（确保 `match_review.json` 与候选同步），再 `check --only reconcile` 生成 `precheck.json`；完成 I6（若触发）才进入阶段五。候选在 I4 后又改过 → 旧决策哈希失效，必须重新确认，**不要手改哈希**。
7. `approve --build` 的硬门禁：`match_review.json` 存在且 `interaction_mode != minimal` 时，每个 `confirmed` 的规则候选都必须有决策且 `row_hash` 与当前候选一致；缺失/过期/`selected=reject` 一律拒绝生成 `approvals.json`，并在 `approvals.json` 写 `match_logic_confirmed`。`minimal` 模式下跳过哈希门禁，但仍要打印匹配明细。同时执行 I2/I3/I6 门禁：任何 confirmed 规则候选都会重新检查解析/归属决策、I3 目标表一致性和 `precheck.json` 指纹。

### 阶段五 — 按配置表弹窗审批

**审批只用 AskUserQuestion；不得生成网页或外部审批界面。** 弹窗蓝图来自 `approval_groups.json`；`suggestion`、已拒绝和无效候选不在其中。**进入阶段五前必须已完成 I4 和（触发时的）I6**：`match_review.json` 存在（且 `interaction_mode != minimal`）时，缺少决策或哈希过期的规则候选不得进入确认列表；`precheck.json` 必须已由 `check --only reconcile` 生成，要求 I6 时还必须有有效的 `dialog_decisions.json#precheck`，缺失/过期/触发器变化后沿用旧决策都不得进入审批。先回到对应门禁补齐，不要绕过。

**5.1 弹窗前的明细打印（强制）**：按 `approval_groups.json` 的 `order` 逐表处理；每表弹窗前打印**该表全部候选**的完整明细（高→中→低），必须含 `row`/`match_key`、证据样本、`expected_effect`、`source_trace`、`risk_reasons`/`warnings`，不得只念 `reason`。格式见 §7.2。

**5.2 每张配置表一个弹窗**：对每张表**独立调用 `AskUserQuestion`**（必要时同表续窗），问题顺序固定高→中→低；不按“每 4 条候选”切表，不把两张表混进同一次调用。

- 一次调用最多 4 个 `questions`；`popup_plan.questions` 已给出分块。`popups_needed > 1`（或低风险选“逐条审核”）时同表再弹，每次最多 4 问，全部问完才进入下一张表。
- `question` 放紧凑明细（ID、操作、pattern/match_key、预期收益、风险点、溯源、`table_note`）；选项只放动作，每个问题最多 4 个。
- **高风险**（每问 ≤3 条）：`multiSelect: true`，每候选一个“`P0011 确认通过`”选项，第 4 个固定“全部拒绝（本组 N 条）”。勾选 = 逐条确认（`risk_acknowledged=true` 的唯一来源）；未勾选保持 `pending`。同时勾候选和“全部拒绝”属冲突，重新弹窗。
- **中风险**（每问 ≤3 条）：`multiSelect: true`，每候选一个选项 + “全部拒绝”。勾选 = 逐条通过，记 `confirmed`，`risk_acknowledged` 保持 `false`；不得“全部通过”批量替代。
- **低风险**：整组一个问题、单选：“全部通过（N 条）（推荐）/ 逐条审核 / 全部拒绝”。批量前复核归属：`trace_method=routing.candidate_file_hint` 且文本像商户名/品牌名（或路由有 `attribution_risk`）时，先 `search`/`route --text`；不一致的降级 `suggestion`，不得混进批量。“逐条审核”改用中风险分块；“全部拒绝”才标 `rejected`，未涉及保持 `pending`。

只有 `confirmed`/`rejected` 写入 `approvals.json`；未选择、暂缓、冲突未决保持 `pending`。用户只分析/暂停/停止时立即停止，不生成 `approvals.json`。状态只由本轮对话选择生成：不得从 CSV 推断、沿用上一轮或补选用户没选过的候选。

**5.3 生成 approvals.json**

```bash
<venv-python> scripts/bscat.py approve --build --review <review-dir> \
    --confirmed P0001,P0002 --rejected P0003 [--risk-acknowledged P0001] \
    [--match-review <review-dir>/match_review_decisions.json] [--note "本轮说明"]
```

`--risk-acknowledged` 只能列**在高风险问题里被用户逐条勾选**的候选；遗漏时脚本拒绝生成。`approve --build` 再次执行 `source_trace` 门禁、I4 哈希门禁（默认读 `<review>/match_review_decisions.json`，可用 `--match-review` 覆盖）、I2/I3 决策门禁和 I6 预审门禁；规则候选缺少最新 I4 决策、I3 目标表与当前候选不一致，或 `precheck.json` 要求 I6 但决策缺失/过期，都会被硬拒绝。生成的 `approvals.json` 每条带 `match_logic_confirmed`。全部表问完后打印审批汇总（每表通过/拒绝/暂缓、高风险确认数），再进入阶段六。

### 阶段六 — dry-run、写入与验证

用户只在对话框做最终授权；命令由 Agent 执行。

```bash
<venv-python> scripts/bscat.py apply --repo <repo> --review <review-dir> \
    --approvals <review-dir>/approvals.json \
    [--match-review <review-dir>/match_review_decisions.json]
```

展示已确认 ID/风险数/目标文件/预期修复/新分类/冲突/未审批数；无已确认候选或 dry-run 报错时停止。`apply --approvals` 先重新导入审批（再次执行 `source_trace` 与 I4 哈希门禁）再 dry-run；可用 `check --only preview` 重跑预览。`--match-review` 默认读 `<review-dir>/match_review_decisions.json`；如果 `approve --build` 用过自定义路径，这里必须传同一个文件。

写后验证默认 `--verify-scope auto`：读 `task_profile.json` 逐聚类档位，与候选影响面取较高者，也可显式指定档位。若 I6 的 `dialog_decisions.json#precheck.verify_scope` 为 `local`/`full`，`auto` 会被替换为该值；命令行显式 `--verify-scope` 始终优先；I6 选 `current` 表示沿用命令默认。`full` 在**写入前**跑 `baseline.py diff` 存 `baseline_before.json`，写后再跑做集合差；范围外行级回归/丢失分类在 `target`/`local` 下仍硬失败。判定看**写后相对写前新增的语义差异行**，不看 baseline 退出码；无写前快照时退化为单次 diff，报告标注“无法区分既有噪声”。

`edit`/`delete`/`keyword_update` 按 `match_key` 精确匹配并流式重写目标表（`merchant_kb.csv` 约 874k 行）；命中 0/多行硬错误，不会写半成品。

**confirmed 集预览（最终授权前必做）**：在样本上跑已确认候选，确认预期修复/新分类/冲突/回归，`extra` 满足 `resolved > 0` 且 `newly_classified == 0`；与阶段四矛盾时停下。`simulate.py preview` 写 `preview_report.json`，属 match-level 近似，最终以 `validation_after.py` 为准。

用 **AskUserQuestion** 做最终写入授权：`确认写入` / `只保留 dry-run` / `取消`；选“确认写入”才执行。无法调用时用文字请求用户明确回复“确认写入”。

```bash
<venv-python> scripts/bscat.py apply --repo <repo> --review <review-dir> \
    --approvals <review-dir>/approvals.json --apply --history <history-dir> \
    [--allow-high-risk] [--report] [--verify-scope auto|target|local|full]
# 仅当已逐条确认高风险候选时才加 --allow-high-risk
```

`apply` 备份到 `<review-dir>/backups/<timestamp>/`、原子写入、失败自动恢复，写后按 `--verify-scope` 跑真实流水线并生成 `repair_log.*`；传 `--history` 时追加到 `<history-dir>/repair_log.jsonl` 并归档到 `runs/<run_id>/`。绝不 commit/push。

写后读 `validation_after.json`：`scope.scope`/`reason`/`target_fallback` 说明实际档位与回退；`out_of_scope_claim_changes` 或 `counts.claim_changes` 非空时逐条复核，必要时用 `full` 重跑再决定回滚。

**验收（任何档位都适用）**：

- `regressed == 0`；miss/diff `lost_classification == 0`；extra `by_problem_type.extra.resolved > 0` 且无新 `newly_classified`。
- **范围外不能有未解释的行级回归**；范围外 claim 变化逐条复核或补跑 `full`；目标引擎外无可解释的声明变化。
- `full`：`baseline.counts.unexpected == 0`、`baseline.files.unexpected_changes` 为空；`counts.semantic` 每行可解释为预期影响（0 最干净，非 0 ≠ 回归但需逐条确认）；`files.touched_but_unchanged` 非空需确认空操作；`drift_before` 是写前漂移，不算本次回归；无写前快照不能声称“0 差异”。
- `git -C <repo> diff --stat` / `diff -- <changed files>` / `diff --name-only`：只允许已审批 `target_file`、阶段零已记录既存改动和流水线自动更新的 pickle；其他文件立即回滚。
- 流水线无异常，已看 diff。

**I8 写后下一步（条件门禁，`validation_after` 成功后）**

展示 `validation_after.json` 的关键结果后，用 1–2 个 `AskUserQuestion` 确认下一步；答案写入 `dialog_decisions.json#outcome`，至少含 `decision`、`remaining`、`decided_at`。

1. 下一步：`accept`（接受本次写入）/ `rollback`（回滚）/ `full_then_decide`（先补跑 full 验证再决定）/ `export_report`（导出报告）。
2. 本轮剩余：`continue`（继续下一批）/ `end`（结束）/ `export_pending`（导出未处理清单）。

`rollback` 仍需用户明确授权后才执行 `apply --rollback --apply`；`full_then_decide` 先跑 `full` 验证再重新询问。写后验证失败、出现未审批文件或硬回归时**不进入 I8**：按失败路径停止/回滚；只有回滚本身失败时才升级告知用户。`interaction_mode=minimal` 时 I8 可只问 `accept`/`rollback`，其余默认 `end`。

失败时回滚：

```bash
<venv-python> scripts/bscat.py apply --review <review-dir> --rollback          # dry-run
<venv-python> scripts/bscat.py apply --review <review-dir> --rollback --apply  # 恢复
```

### 阶段七 — 交付报告

汇总输入行数和问题数量；选中的引擎/聚类及 `deferred_groups`；已确认/已应用/已拒绝/suggestion 数量；**本轮实际验证档位（T0/T1/T2）、范围外检查结果和升级/降级理由**；修复、回归、新分类、丢失分类、仍未修复和引擎声明变化；只读代码诊断结论（跳过时说明原因）；修改文件与备份目录；写入是否全部在白名单内；历史日志位置；并注明**未提交、未 push**。

`bscat.py apply ... --report` 会在 dry-run/写入结束后生成 `<review-dir>/stage_report.md`（同步/样本/候选/校验/写入/写后回归/下一步），再据此写最终交付。

## 4. 引擎事实

> ⚠️ **引擎机制来源与验证**：以下优先级、文本归一化、匹配方式、跨引擎覆盖关系，来自同一套引擎代码（bscat_V2 / ServiFlow-AI）的逐行分析；`AUS_BS_CAT_Model` 的布局为 `configs/` + `<engine>_engine/resources/`（initial 用 `initial_engine/merchant_kb.csv`）。路径已于 2026-10-22 核对；**规则数、列名/schema 和引擎源码版本仍需首次真实运行前逐项核对**。引擎源码是判断「某条规则改完会怎么跑」的唯一依据。

### 4.1 引擎与优先级

> 执行顺序与覆盖关系见 §4.2；后执行引擎行级覆盖先执行引擎。规则文件位置见 §5 与 `references/rule-file-catalog.md`。

| 优先级 | 引擎 | 职责 |
|--------|------|------|
| 1 | transfer | 内部/外部转账 |
| 10 | initial | 商户名 → 分类 |
| 150 | dishonour | 拒付/退票 |
| 180 | gambling | 赌博/博彩（双层） |
| 200 | income | 工资/福利等收入 |
| 300 | liability | 贷款/信用卡还款 |
| 400 | all_other_credit | 退款/返现/利息 |
| 500 | fee | 各类费用 |
| 800 | rent | 房租/租金（双层） |
| 999 | catch_all | 兜底通用关键词 |

> 实际引擎集合、优先级以 `configs/pipeline.json` 与 `configs/category_catalog.json` 为准；`claims.csv` 是阶段一生成的逐行引擎声明，不放在目标仓库里。

### 4.2 跨引擎覆盖规则（改/删规则前必读）

删或收窄某条规则前，必须判断「删掉后这一行会被谁接管」。关键关系：

1. **后执行覆盖先执行**，不管 confidence 高低。transfer(1) 的分类会被 initial(10) 覆盖。
2. **gambling 认领对 income/liability 是终局**：orchestrator 在 commit 前丢弃 income(200)/liability(300) 在 gambling 已认领行上的预测——赌客派彩不会被重打成 Wages。这是唯一「先执行反压后执行」的关系。
3. **gambling 的 rule 层走 `exclude_prior_claimed`**：对 transfer/initial/dishonour 的认领让位。商户名已命中 merchant_kb 时，往 rule 层加规则**无效**，必须走 institution 层。
4. **initial 的「Financial Institutions」/「Debt Collection」/「Debt Consolidation」在 pipeline 中被清除**，由 liability/dishonour 兜底。
5. **income → liability**：orchestrator 在 liability 前排除 Wages/Centrelink 行。
6. **income/liability → rent**：orchestrator 在 rent 前排除已被 income/liability 认领的行。
7. **transfer → all_other_credit**：all_other_credit 可覆盖 External Transfers（保留在 candidates 中）。
8. **catch_all 走 `exclude_prior_claimed`**：排除所有前面引擎的分类。

### 4.3 文本预处理差异速查

| 引擎 | 预处理 | 大小写 |
|------|--------|--------|
| initial | `clean_text()`：仅 `[A-Z0-9 ]`，去通道前缀 | 大写 |
| transfer | `lower().strip()` | 小写 |
| dishonour | 无（`str.contains(..., case=False)`） | 不敏感 |
| gambling | 双层：institution `clean_text_with_channel_prefix()`；rule `clean_text()` | 大写 |
| income | `clean_text_with_seams()`：大写 + 数字-字母接缝插空格 | 大写 |
| liability | 各子模块不同：counterparty `upper().strip()`；Format A/B 用原始 text + IGNORECASE | 大写/不敏感 |
| all_other_credit | 无（`case=False`） | 不敏感 |
| fee | 仅压缩空格 | **不敏感(IGNORECASE)** |
| rent | 双层：institution `clean_text_with_channel_prefix()`；rule `clean_text()` | 大写 |
| catch_all | `clean_text()`（同 initial） | 大写 |

### 4.4 文本意图的匹配语义写法

用户只说「文本 X 应分为 Y」时，按下表把意图翻译成目标表的 pattern；语义以引擎源码为准。

| 目标表 | 字段/匹配 | 包含 | 前缀 | 精确 | 注意 |
|--------|-----------|------|------|------|------|
| fee | regex + `search`，IGNORECASE | `X` | `^X` | `^X$` | 转义正则元字符；`Overdrawn` 的 priority 必须更小 |
| catch_all | keyword 全词（空格边界）或 regex `search` | keyword `X` | regex `^X` | regex `^X$` | 只收通用词；商户名走 initial |
| dishonour | keyword 子串（`re.escape`，不区分大小写）或 regex | keyword `X` | keyword 做不到真正前缀 | keyword 做不到精确 | 子串会误伤；必要时用 regex + `required_terms` |
| all_other_credit | keyword 子串，仅入账 | keyword `X` | 做不到 | 做不到 | regex 不生效；只生成 keyword |
| initial | 大写 `[A-Z0-9 ]` 全词最长 | 关键词变体 `X` | 不支持前缀语义 | 写完整商户名/变体 | ≤50 变体；先查重；`keyword_update` 优先 |
| gambling / rent | rule 层 keyword 全词或 regex；institution 层商户全词 | rule keyword `X` | rule regex `^X` | rule regex `^X$` | 商户名走 institution；institution 优先于 rule；已进 merchant_kb 时 rule 层无效 |

## 5. 各引擎要点

### 5.1 transfer（优先级 1，最先执行）

- 只输出 `Internal Transfer` / `External Transfers`，不输出其他分类。
- 外部转账 regex 分 high_confidence（priority 从 1 起）/ medium_confidence（从 101 起），按 priority 升序第一匹配胜出；文本 `lower().strip()`，regex 必须小写（见 §4.3）。
- 对手方 keyword 是子串匹配（非全词），第一匹配胜出，默认 `Miscellaneous Funds Transfer`。
- 配对排除（`transfer_pairing_exclusions.csv`）按 priority 降序（与 regex 升序相反）。
- **对修改的影响**：transfer 最先执行，分类可能被后续引擎覆盖（见 §4.2）。

### 5.2 initial（优先级 10）

- `merchant_name, keywords, category` 三列；keywords 是 `|` 分隔的变体，每商户最多 50 个。
- 匹配在 `clean_text()` 后做 Aho-Corasick 全词 + 最长胜出。
- ⚠️ keyword 加载**不再** clean_text（只 strip + STOPWORDS 过滤 + 去重）——写入前必须预清洗为**大写、仅 `[A-Z0-9 ]`、压缩空格**，否则静默失配。
- 97 个 STOPWORDS 作为独立 keyword 被丢弃；`Financial Institutions` 整行被丢弃。
- **对修改的影响**：keyword 格式错误是静默失配的元凶；收窄商户时注意最长匹配语义（`COLES` 与 `COLES EXPRESS` 同时命中，后者胜出）。

### 5.3 dishonour（优先级 150）

- `rule_type, pattern, required_terms`；`required_terms` 分号分隔、AND 条件。
- keyword 模式：`re.escape(pattern)` 后子串匹配，大小写不敏感；regex 模式：先全部 required_terms 都在，再 regex。
- **对修改的影响**：keyword 是子串匹配（不要求全词），改/删时要评估是否会误伤含该子串的文本。

### 5.4 gambling（优先级 180，双层）

- 7 列，列见 `references/rule-file-catalog.md`（与 rent 同构）。
- **institution 层**：pattern 是 `|` 分隔变体，`clean_text_with_channel_prefix()` 后全词最长胜出；命中后让位 fee/dishonour 认领及 initial 的等长/更长 keyword。
- **rule 层**（`source=rule`）：`clean_text()` 后 keyword 全词 / regex，最高 confidence 胜出；让位规则见 §4.2。
- category 恒 `Gambling`；institution 行的 `match_type`/`confidence` 不被读取；对 income/liability 终局（见 §4.2）。
- **对修改的影响**：新商户走 institution 行（counterparty=商户名，confidence 惯例 0.95）；通用博彩词走 rule 行。商户名已进 merchant_kb 时，往 rule 层加规则无效。

### 5.5 income（优先级 200，最复杂）

- 不是纯文本匹配：regex + 金额阈值 + payer_key + 频率/稳定性行为特征。
- 文本 `clean_text_with_seams()`：大写 + 仅 `[A-Z0-9 ]` + 数字-字母接缝处插空格（`3780.7Salary` → `3780 7 SALARY`），使 `\bSALARY\b` 能命中拼接文本。
- pattern_group 分多组（strong_wage / medium_income / centrelink / self_employed_gig / hard_negative / soft_negative / transfer_from / pay_signal / gig_* / behavior_exclusion 等）。
- 硬门控：金额 < $100 除非有工资历史（或小额强关键词 [50,100)），否则不判工资；gig 路径不受此门控。
- **对修改的影响**：新增 strong_wage 模式最安全；medium_income 需配合行为特征；hard/soft negative 会挡住 PayPal/Afterpay 等的 SALARY 关键词；`transfer_from` 是豁免信号不是排除。

### 5.6 liability（优先级 300，多文件多格式）

- 执行顺序：counterparty → home/car → credit_card → dishonours → overdrawn → debt_collection → debt_consolidation → generic_loan 兜底；最后统一重命名 stream_id 为 `loan_NNN`（不能从前缀读产品类型）。
- **counterparty_keyword_rules.csv**：`keyword, counterparty, product_type, match_type`；keyword 分号分隔；匹配边界 `(?<![A-Za-z])...(?!A-Za-z])`（不是 `\b`，数字可穿透）；第一匹配胜出。⚠️ loader 只读 `rule_type`，但表头实际是 `match_type`——加 regex 必须新增 `rule_type` 列，否则 regex 被当 keyword 转大写 + re.escape 成死规则。
- **credit_card_rules.csv**：`keyword` 列实际是 regex；已有 product_type 的行整个排除（只填未认领行，无覆写模式）。
- **home_loan_car_loan_rules.csv**：Format A，12 列，match_scope/match_type（含 `always` 纯条件规则）。
- **Format B 三文件列各异**：`debt_collection`（有 rule_id）、`debt_consolidation`、`overdrawn`（无 keyword 列，用 pattern 列 + regex）。Format A/B 的 keyword 都是 `\b(?:...)\b`（数字不穿透）——与 counterparty 那份不同。
- **`product_type` 取值**：只能取目标表已有值（`counterparty_keyword_rules.csv` 常见 `personal_loan`、`car_loan`、`home_loan`、`bnpl`、`wage_advance`、`generic_loan`、`bank`，以表内为准），且须与证据一致、不得自造；不确定降级 `suggestion`。

### 5.7 all_other_credit（优先级 400）

- 列见 `references/rule-file-catalog.md`，与 dishonour 共用 loader。
- 只处理入账；只用 keyword（regex 未实现，`required_terms` 被忽略）；`re.escape` 子串匹配。
- 覆盖 External Transfers（见 §4.2）。**对修改的影响**：regex 写了也不会匹配，只生成 keyword。

### 5.8 fee（优先级 500）

- 列见 `references/rule-file-catalog.md`；可选列 `unclassified_only` / `dr_cr` / `zero_amount_reject`。
- category 仅 `fee`（→ `Fees`）或 `Overdrawn`；pattern 通常 `^` 锚定；priority 升序第一匹配胜出。
- 大小写不敏感（`re.compile(..., re.IGNORECASE)`，自 2026-08-20）。
- **对修改的影响**：新增规则设合适 priority；`Overdrawn` 的 priority 必须更小。

### 5.9 rent（优先级 800，双层）

- 7 列，列见 `references/rule-file-catalog.md`（与 gambling 同构）。
- institution 层 `clean_text_with_channel_prefix()` + 全词最长胜出；rule 层 `clean_text()` + keyword 全词 / regex，最高 confidence 胜出。
- institution 命中结构性优先于 rule（`inst_win`）：命中任何 institution 后，rule 层加规则/提高 confidence 都无效。
- category 恒 `Rent`；institution 行的 `confidence` 不被读取。
- **对修改的影响**：REAL ESTATE 全词规则漏 `REAL ESTATEAGENT` 这类无空格拼写；institution 层超过 50 变体静默失效。

### 5.10 catch_all（优先级 999，兜底）

- 列见 `references/rule-file-catalog.md`；`clean_text()` 大写。
- keyword 全词 / regex，**最高 confidence** 胜出（不是第一匹配）；排除前面引擎的规则见 §4.2。
- confidence 建议 0.55–0.90；只收通用描述词，品牌名/平台名走 initial。

## 6. 归属与规则

### 6.1 引擎归属决策树（设计候选前强制走完）

> 这是「归属复核」的落地形式，防止把候选错配给错误引擎。每条候选生成前，按顺序回答：

```
1. 这是赌博/博彩平台或博彩关键词吗？ → 是 → gambling（优先于下面各条）
     商户名/平台名 → institution 层；通用博彩词（BET/CASINO/LOTTERY）→ rule 层
2. 这是商户名/品牌名/平台名吗？ → 是 → initial（走 merchant_kb 查重/新增，绝不进 catch_all）
3. 这是费用描述吗？ → 是 → fee
4. 这是入账交易吗？（利息/退款/返现） → 是 → 优先 all_other_credit
5. 这是转账描述吗？（Fast Transfer/Osko Payment） → 是 → transfer
6. 都不属于 → 按 category_catalog.json 的 owner_engine_id 归属，并做交叉验证
```

### 6.2 常见错配表（每次运行后回填新发现的错配）

| 错配（要避免） | 正确归属 | 原因 |
|----------------|----------|------|
| 商户名/品牌名 → catch_all | **initial** | catch_all 只收通用描述词 |
| 赌博商户 → initial / catch_all | **gambling**（institution 行） | 博彩派彩若走 initial 会被 income 重打成 Wages |
| 通用博彩词 → initial | **gambling**（rule 行） | BET/CASINO/LOTTERY 是通用词不是商户 |
| 费用描述 → catch_all | **fee** | FEE 关键词优先 fee |
| 利息/退款/返现 → catch_all | **all_other_credit** | 入账类优先 all_other_credit |
| 转账描述 → catch_all | **transfer** | Fast Transfer/Osko 是转账 |
| 内部转账 → initial | **transfer** | EVERYDAY ROUND UP 是内部转账 |

复核补充：从 `configs/category_catalog.json` 查期望 category 的 `owner_engine_id`，候选只能给该引擎；与决策树不一致时停下。执行顺序/守卫见 §4.2，按 `routing_actions` 处理，**不改 `priority` 走捷径**。`target_file` 必须命中 `RULE_FILES`；`edit`/`delete` 的 `match_key` 恰好命中 1 行。

### 6.3 问题类型动作速查

| 问题类型 | 动作 |
|----------|------|
| 漏识别 `miss` | 向归属引擎 `append` 最窄规则；若会被后续引擎覆盖，同时处理覆盖规则 |
| 多识别 `extra` | 删除 / 收窄同引擎所有命中规则；**绝不追加分类规则**；删除后确认无其他规则继续命中 |
| 不一致 `diff` | 删除 / 收窄旧规则 + 追加归属规则，通常成对出现并在 `rationale` 中互相引用 |

通用原则：风险分级见阶段四 AI 预筛表；一条候选只改一行；规则本身错误才 `edit`/`delete`，已有商户增删关键词用 `keyword_update`；证据不足用 `suggestion`（仅报告，不进入审批）。

## 7. 审批与汇报

### 7.1 汇报卡片示例

卡片只用于压缩汇报；所有需要用户决定的地方仍用 `AskUserQuestion`。

快路径按五块逐条汇报：**入口**（I1 问题类型/方向/范围/交付、样本/期望/当前）→ **分流**（A 可直改 / B 先处理旧规则 / C 需人工策略）→ **候选**（`engine`/`operation`/风险、`target_file`/定位、I4 匹配逻辑、证据/预期、反例/溯源）→ **写入确认**（目标/风险/验证档位/未审批/备份/选项）→ **结果**（写入/修复/回归/新分类/丢失分类、验证档位、未审批文件、未 commit/push、下一步）。

### 7.2 I4 与审批明细示例

每张配置表弹 I4 前，先打印匹配逻辑明细（字段来自 `match_review.json`）：

```text
【匹配逻辑 1/N】fee_engine/resources/fee_classification_rules.csv | fee | 高 1 / 中 0 / 低 2 | 弹窗 1
P0011  low  fee_regex  `example monthly fee`
  应命中: "EXAMPLE MONTHLY FEE"（期望 Fees）
  需避免: "example monthly fee refund"（否则会误伤退款）
  预期: 命中 2 / 修复 2 / 冲突 0 / 已正确 0 | hash sha256:0c2154d8…
  选项: recommended=按建议正则；narrower=整串精确；wider=前缀；reject=拒绝
低风险 P0012/P0013 可整组按建议；选中“逐条调整”再逐条问。
```

每张配置表审批弹窗前，再打印该表全部候选的完整明细（字段来自 `candidates.json` + `validation.json` + `approval_groups.json`）：

```text
【配置表 1/N】liability.../counterparty_keyword_rules.csv | liability | 高 2 / 中 1 / 低 3 | 弹窗 1
P0011  miss  high  liability/append
  新增行: keyword=... | counterparty=... | product_type=... | 证据: C0007 3 行
  示例: "..." | 预期: 修复 3 / 新增 0 / 冲突 0 | 溯源: routing... → <表>
  风险/警告: <risk_reasons + warnings> | rationale: <为什么加 + 反例>
中/低风险同格式列出；低风险可整组通过。
```

### 7.3 条件门禁示例（I2/I3/I5/I6/I8）

**I2 解析确认**（`parse_review.json` 触发 `multiple_sheets` + `label_conflict`）：

```text
【I2 解析确认】已写入 128 行；sheet=Sheet2（另有 1 个 sheet 未用）；问题范围=miss
触发: multiple_sheets, label_conflict（12 行输入期望与当前分类不一致）
问1: 解析结果 → 继续（推荐）/ 改列映射 / 换工作表 / 停止
问2: 期望分类权威 → 以样本期望为准（推荐）/ 以当前分类为准 / 人工指定 / 停止
```

确认后写：

```json
{
  "schema_version": 1,
  "parse": {
    "decision": "continue",
    "expected_authority": "sample",
    "fingerprint": "sha256:...",
    "decided_at": "2026-11-02T10:00:00"
  }
}
```

**I3 归属/目标表**（`attribution_review.json` 触发 `owner_ambiguity`）：

```text
【I3 归属 C0003】miss / Fees，14 行；推荐 fee/fee_engine/resources/fee_classification_rules.csv
备选: catch_all_engine/resources/generic_keywords.csv（仅通用词）
选择: recommended（推荐）/ alternative（写 engine+target_file）/ evidence（写 fallback）/ stop
```

**I5 修订预览**（改 `match_key`）：

```text
【I5 修订预览】P0007 将改 match_key = {"rule_id":"F-102"}；
触发 match_key；patch_sha256=sha256:...；确认应用？
选项: apply（推荐）/ continue_editing / cancel
```

**I6 影响面预审**（`validation_warnings` + `approximate_effect`）：

```text
【I6 校验/影响面预审】可审批 6 条；触发 validation_warnings, approximate_effect
建议写后范围 full（edit/delete 的真实接管只能写后观察）
问1: 继续（推荐）/ 重新校验 / 降级 / 停止
问2: 影响面 accept（推荐）/ narrow / need_samples / stop
问3: 写后范围 auto（推荐）/ local / full / current
```

**I8 写后下一步**（验证成功）：

```text
【I8 写后下一步】修复 9 行、回归 0、未审批文件 0，实际范围 full
下一步: 接受本次写入（推荐）/ 回滚 / 先补跑 full 再决定 / 导出报告
本轮剩余: 继续下一批 / 结束（推荐）/ 导出未处理清单
```

## 8. 产物与留存

`<review-dir>/` 是运行期目录，可清理。**运行期产物**：`intake.json`（I1）、`parse_review.json`（I2）、`attribution_review.json`（I3）、`revise_preview.json`（I5）、`precheck.json`（I6）、`dialog_decisions.json`（所有条件门禁答案）、`dialog_patch.json`（对话修订补丁）、`rule_index.json`/`task_profile.json`/`route_decision.json`、`candidates/*.csv`、`validation.json`/`match_review.json`/`match_review_decisions.json`（I4）/`impact_reconcile.json`/`preview_report.json`、`approval_groups.json`、`stage_report.md`、`approvals.json`。**建议保留**：`repair_log.*`、`apply_report.json`、`validation_after.json`、`backups/<timestamp>/`。`git_sync.json` 记录缓存状态；同步缓存位于系统临时目录，可随时删除。

## 9. 故障排查

| 现象 | 处理 |
|------|------|
| 阶段零不是 `0 0` | 停止，让用户手动同步；绝不自动 pull/merge/rebase |
| 同步缓存未复用，或 `cache_reused=true` 但分支/HEAD 已变 | 仅当仓库/remote/分支/upstream/HEAD 全一致才复用；用 `--sync-max-age 0` 重跑，重新 fetch 后自动重写，不要手工改缓存 |
| `initial_engine/merchant_kb.csv.automaton.pickle` 被修改 | 引擎加载重建的 Aho-Corasick 缓存，不参与规则 diff/审批；不要手工编辑，是否提交由维护者决定 |
| `index_warnings` 涉及目标引擎/表，或路由无法确定目标引擎/表 | 索引/表头漂移：重跑 `prepare`/`route`，按 `full`，用 `catalog`/`search` + 只读诊断定位，不得猜表；必要时修 `rule_index.py` 登记 |
| 路由 `attribution_risk` 非空 | 不按路由给的表直接追加；按 §6.1 重新归属（商户名→initial、赌博→gambling、费用→fee 等） |
| 文本意图 `propose` 只给 `suggestion` / 给错表 | 不要用 `revise` 改 `target_file`；先 `route --text`/`search` 复核，换审核目录重跑 `propose`，或保留报告 |
| 合成探针反例被误伤 | 收窄 pattern、加 `dr_cr`/锚点或换引擎；仍无法区分就降级 `suggestion`，不要写入 |
| 路由 `can_append=false` 或 `append_blocking_rules` 非空 | 追加会被顶掉；改用 `edit`/`delete`/`keyword_update`，或升级 `T2` |
| 范围外 claim 变化 / 行级回归 / 丢失分类 / 未审批文件 | claim 变化逐条复核，必要时补跑 `full`；行级回归、丢失分类、未审批文件属硬失败，立即 `apply --rollback --apply` |
| `target_file` 不在白名单 | 删除候选或降级 `suggestion`，绝不写入 |
| 缺少 `source_trace` / 与 `target_file` 不一致 / 旧审核目录候选失效 | 按 `rulebook.md` 1.1 先读表溯源；hint 有误写 `override_reason`；旧目录用 `revise --patch` 补齐或重跑 |
| `product_type` 不在目标表已有取值 | 不要新造值；按 §5.6 重判，仍不确定就降级 `suggestion` |
| 高风险同时勾选候选和“全部拒绝”，或缺少 `risk_acknowledged` | 冲突选择重新弹窗；未逐条确认不得写入；重新走阶段五 |
| `approve --build` 报“缺少匹配逻辑确认（I4）” | 先按阶段四 I4 段逐表弹窗并写 `match_review_decisions.json`；不要用 `--risk-acknowledged` 绕过 |
| `approve --build` 报“匹配逻辑在 I4 确认后又被修改”（hash 不一致） | 候选在 I4 后改过；重新打印新匹配、重新确认并更新决策哈希；不得手改 `row_hash` |
| `revise` 后 `match_review.json` 的候选哈希变了 | 正常：校验产物随候选重建；按新 `match_review.json` 重新确认，旧决策自动失效 |
| `propose` 报“条件门禁未完成”（I2/I3） | 先看 `parse_review.json`/`attribution_review.json`，用 `AskUserQuestion` 补齐 `dialog_decisions.json`；`change_*` 类决策要先重跑 `prepare` 再改成 `continue`，不得手改 fingerprint |
| `propose` 报 engine_default 聚类没有 I3 决策 | 该候选会写到默认表兜底；先完成 I3 选表（或降级），不要用 `revise` 改 `target_file` 绕过 |
| `propose` 报“I3 归属蓝图缺失或已更新，旧决策不能用于本轮 engine_default 草稿” | `attribution_review.json` 缺失或 fingerprint 已变；重新查看归属蓝图并确认目标表，不要沿用旧决策或手改哈希 |
| `revise` 报“I5 修订确认未完成”或 `patch_sha256` 过期 | 先 `--dry-run` 生成 `revise_preview.json`，展示后确认并写入 `dialog_decisions.json#revise`；补丁改过必须重新预览，不得手改哈希 |
| `approve --build` 报“I6 校验/影响面预审未完成” | 先跑 `check --only reconcile` 生成 `precheck.json`，逐条解释触发项后确认；`simulate_mismatch` 还必须 `acknowledge_mismatch=true`，但仍要先查清差异 |
| `apply` 的写后范围与 I6 选择不一致 | 命令行显式 `--verify-scope` 优先；否则 `auto` 读取 `dialog_decisions.json#precheck.verify_scope`（`current`=沿用默认）；改选择后重跑 `approve` 前确认指纹 |
| 用户要的匹配语义目标表不支持（如 `all_other_credit` 用 regex） | 不要生成引擎会静默忽略的行；改用关键词/变体，或降级 `suggestion` 并说明限制 |
| `intake.json` 的 `problem_type` 与用户当前说法冲突 | 先停下确认；更新 `intake.json` 后重跑 `prepare`/`propose`，不要静默沿用旧类型 |
| `conflicts` / `conflicts_extra` / 删除后仍被分类 | 模式太宽、会给 extra 新分类或后续引擎接管；收窄/删除/降级，查 `claims_after.csv`、`delete_blocked_extra_rows` |
