# BSCAT 候选格式手册

本文件负责候选、路由产物、I1/I4 交互产物与 `dialog_patch.json` 的字段和模板。执行流程、审批门禁、写入与回滚见 `SKILL.md`；引擎语义与归属规则见 `SKILL.md` §4–§6。

## 0. 入口路由与验证档位产物

设计候选前先看机器路由，不要每次从 `CLAUDE.md` 重新推导：

```bash
<venv-python> scripts/bscat.py route --repo <repo> --input <file> --out <review-dir>
# 或单任务
<venv-python> scripts/bscat.py route --repo <repo> --category <c> [--engine <e>] \
    --problem-type <miss|extra|diff> [--current-engine <e>] [--rows <n>] [--layer institution|rule]
```

| 文件 | 生成者 | 用途 |
|---|---|---|
| `rule_index.json` | `route --out` / `prepare` | 引擎、表、分类、覆盖关系的运行时索引 |
| `route_decision.json` | `route --out` | 六问答案、档位、升级原因 |
| `task_profile.json` | `prepare` | 逐聚类档位、目标表、是否需代码诊断 |
| `intake.json` | Agent（I1 对话后） | 问题类型、修复方向、覆盖范围、交付方式 |
| `parse_review.json` | `prepare`/`parse_input` | I2 解析蓝图：触发项、列候选、问题、fingerprint |
| `attribution_review.json` | `prepare` | I3 归属蓝图：推荐表、候选表、证据、问题、fingerprint |
| `revise_preview.json` | `revise`（含兼容 CSV 合并） | I5 修订预览：变更字段、`patch_sha256`、fingerprint |
| `precheck.json` | `check --only reconcile` | I6 影响面预审：触发项、建议验证范围、问题、fingerprint |
| `dialog_decisions.json` | Agent（I2/I3/I5/I6/I8 对话后） | 条件门禁答案；每段带 fingerprint/decided_at |
| `match_review.json` | `check --only validate` | 每条候选的匹配逻辑、正反例、可选匹配模式与哈希 |
| `match_review_decisions.json` | Agent（I4 对话后） | 用户确认的匹配方式与确认时哈希 |

候选的 `source_trace.candidate_file_hint` 应来自路由 Q1 的 `target_file`；`task_profile` 的档位决定 `prepare --diagnose` 与 `apply --verify-scope` 的默认深度。详见 `references/rule-index.md`。

## 1. `candidates.json`

外层：`{"run_id", "repo", "generated_at", "candidates": [...]}`。

| 字段 | 必需 | 含义 |
|---|---|---|
| `candidate_id` | 是 | `R001`… 本次唯一 |
| `problem_type` | 是 | `miss` / `extra` / `diff` |
| `engine` | 是 | 目标引擎 ID |
| `target_file` | 是 | 仓库相对路径，必须命中 `RULE_FILES` |
| `operation` | 是 | `append` / `edit` / `delete` / `keyword_update` / `catalog` / `priority` / `suggestion` |
| `status` | 是 | 初始必须 `pending`；脚本绝不自动确认 |
| `row` | append/edit/keyword_update | append 为完整行；edit 只放要改的列；keyword_update 为完整关键词或 add/remove |
| `match_key` | edit/delete/keyword_update | 列名 → 精确字符串，必须只匹配一行 |
| `category` | 建议 | 期望分类，用于归属检查 |
| `evidence_cluster_ids` | 建议 | 对应 `clusters.csv` |
| `rationale` | 是 | 为什么加/删/改，以及考虑过的反例 |
| `source_trace` | 规则候选必需 | 证据 → 配置表溯源；`target_file` 必须与候选一致 |

`validation`、`risk_level`、`expected_effect` 由校验脚本填写，不要手写；`source_trace` 由生成器起草、Agent 复核。
`append` 只能用于可追加的分类器表；`suggestion` 不会写入。

定位与写入约束：

- `edit` / `delete` / `keyword_update` 的 `match_key` 必须恰好命中 1 行。
- `append` 不写 `match_key`。
- 一条候选只改一行；规则本身错误才 `edit` / `delete`。
- `income` / `liability` / `transfer` 和 `priority` / `catalog` 一律高风险。

### 1.1 配置表溯源

```json
"source_trace": {
  "cluster_id": "C0007",
  "owner_engine": "liability",
  "candidate_file_hint": "liability_engine/resources/counterparty_keyword_rules.csv",
  "target_file": "liability_engine/resources/counterparty_keyword_rules.csv",
  "trace_method": "routing.candidate_file_hint",
  "matched_rule": {"file": "liability_engine/resources/counterparty_keyword_rules.csv",
                   "line": 42, "rule_id": "loan_017", "pattern": "EXAMPLE FINANCE"},
  "override_reason": ""
}
```

- `target_file` 必须与候选 `target_file` 一致；`edit`/`delete`/`keyword_update` 的 `matched_rule.file` 也必须一致。
- `candidate_file_hint` 取自 `route` 的 `answers.q1_engine_and_table.target_file`（或 `routing.candidate_file_hint`）；与 `target_file` 不同时，`override_reason` 必填，审批明细会展示这条覆盖。
- `trace_method=engine_default` 表示没有证据级提示，校验会给 warning，审批时需要用户确认；若路由已给出 `attribution_risk`，先复核归属再决定是否使用该 hint。

## 2. 操作示例

### append（miss）

```json
{
  "candidate_id": "R001",
  "problem_type": "miss",
  "engine": "catch_all",
  "target_file": "catch_all_engine/resources/catch_all_rules.csv",
  "operation": "append",
  "status": "pending",
  "category": "Health",
  "row": {"rule_name": "MEDIHIRE_SALESHYDRA", "category": "Health",
          "pattern": "MEDIHIRE SALESHYDRA", "match_type": "keyword", "confidence": "0.95"},
  "evidence_cluster_ids": ["C0002"],
  "source_trace": {"candidate_file_hint": "catch_all_engine/resources/catch_all_rules.csv",
                   "target_file": "catch_all_engine/resources/catch_all_rules.csv",
                   "trace_method": "routing.candidate_file_hint"},
  "rationale": "两个样本行期望 Health，现有规则均未命中。"
}
```

### edit（diff，同一引擎分类写错）

```json
{
  "candidate_id": "R010",
  "problem_type": "diff",
  "engine": "initial",
  "target_file": "initial_engine/merchant_kb.csv",
  "operation": "edit",
  "status": "pending",
  "category": "Groceries",
  "match_key": {"merchant_name": "DUNNINGS TOODYAY JUN"},
  "row": {"category": "Groceries"},
  "evidence_cluster_ids": ["C0005"],
  "rationale": "该商户当前映射为 Automotive，样本期望 Groceries。"
}
```

### delete（extra）

```json
{
  "candidate_id": "R011",
  "problem_type": "extra",
  "engine": "fee",
  "target_file": "fee_engine/resources/fee_classification_rules.csv",
  "operation": "delete",
  "status": "pending",
  "match_key": {"rule_name": "overdraw_fee"},
  "evidence_cluster_ids": ["C0003"],
  "rationale": "该规则把本应未分类的行标成 Overdrawn；删除后需确认后续引擎不会重新分类。"
}
```

删除 `merchant_kb.csv` 时，`match_key` 必须匹配整列值；优先用唯一的 `merchant_name`，不要只写 `keywords` 竖线值中的一段。

### keyword_update（initial 关键词维护）

只用于 `initial_engine/merchant_kb.csv` 的已有商户行；不是新增商户（用 `append`），也不是改分类（用 `edit`）。

```json
{
  "candidate_id": "R013",
  "problem_type": "extra",
  "engine": "initial",
  "target_file": "initial_engine/merchant_kb.csv",
  "operation": "keyword_update",
  "status": "pending",
  "match_key": {"merchant_name": "DUNNINGS TOODYAY JUN"},
  "row": {"remove_keywords": ["DUNNINGS TOODYAY JUN"]},
  "evidence_cluster_ids": ["C0007"],
  "rationale": "该变体把本应未分类的文本识别成 Automotive；保留更长的商户变体。"
}
```

- `match_key` 必须恰好命中 1 行，优先用唯一 `merchant_name`。
- `row` 可写完整 `keywords`（`|` 分隔），或 `add_keywords` / `remove_keywords`；关键词须预清洗为
  大写、仅 `[A-Z0-9 ]`、压缩空格，脚本去重并校验，不合法会拒绝。
- 独立 STOPWORD 被拒绝；合并后最多 50 个变体；删空所有变体时转 `delete`。

### diff 配对候选

旧规则给出错误分类、归属引擎被覆盖时，通常需要一对候选。两条共享 `evidence_cluster_ids`，
并在 `rationale` 中互相引用，审批时说明“必须同时应用”。

```json
[
  {"candidate_id": "R012a", "problem_type": "diff", "engine": "transfer",
   "target_file": "transfer_engine/resources/transfer_external_high_confidence_rules.csv",
   "operation": "delete", "status": "pending",
   "match_key": {"rule_name": "external_direct_debit_with_bsb"},
   "evidence_cluster_ids": ["C0001"],
   "rationale": "该规则覆盖了归属引擎；与 R012b 成对。"},
  {"candidate_id": "R012b", "problem_type": "diff", "engine": "catch_all",
   "target_file": "catch_all_engine/resources/catch_all_rules.csv",
   "operation": "append", "status": "pending", "category": "Health",
   "row": {"rule_name": "example_health", "category": "Health",
           "pattern": "EXAMPLE CLINIC", "match_type": "keyword", "confidence": "0.9"},
   "evidence_cluster_ids": ["C0001"],
   "rationale": "删除旧规则后由 catch_all 接住；必须与 R012a 同时应用。"}
]
```

### catalog / priority / suggestion

- `catalog`：`target_file` 为 `configs/category_catalog.json`，`row` 为
  `{"category": "...", "owner_engine_id": ["..."], "active": true}`，始终高风险。
- `priority`：`target_file` 为 `configs/pipeline.json`，`row` 为
  `{"engine_id": "...", "priority": 750}`（也可含 `enabled`），必须逐条明确确认。
- `suggestion`：证据真实但精确规则行属于业务决策时使用，写入 `proposed_change`，仅报告。

## 3. `dialog_patch.json`

由 Agent 根据对话生成，不是用户的编辑界面。通过 `bscat.py revise` 合并回 `candidates.json`；优先用 `--patch`，兼容模式下直接改 CSV 也会经过同一 I5 预览与 `patch_sha256` 校验。

```json
{"changes": [
  {"candidate_id": "P0003", "row": {"pattern": "^NEW\\s+PATTERN"}, "reason": "用户确认的原因"},
  {"candidate_id": "P0004", "match_key": {"rule_name": "new_rule_name"}, "reason": "改名原因"},
  {"candidate_id": "P0008", "source_trace": {"target_file": "liability_engine/resources/counterparty_keyword_rules.csv",
    "trace_method": "manual", "override_reason": "证据提示表缺少 counterparty 列"}},
  {"candidate_id": "P0009", "operation": "suggestion", "reason": "证据不足，降级为建议"}
]}
```

- 只允许 `row` / `match_key` / `reason` / `rationale` / `source_trace` / `operation`；不接受 `status`。
- `delete` 不能改规则列；`keyword_update` 只能改 `keywords` / `add_keywords` / `remove_keywords`。
- `operation` 只能降级为 `suggestion`，且不能与 `row`/`match_key` 同一次提交。
- `source_trace.target_file` 非空时必须与候选 `target_file` 一致；`source_trace` 必须是对象。
- 改 `match_key` 时不能同一次改规则列；未知/重复 ID、定位到 0/多行一律硬错误。
- `row`/`match_key`/`operation`/`source_trace` 任一变更都会把候选重置为 `pending` 并清空 `validation`，必须重新校验、重新审批。

## 3.1 `intake.json`（I1）

```json
{
  "problem_type": "miss",
  "direction": "append_first",
  "directions": {"miss": "append_first", "diff": "paired"},
  "scope": "cluster",
  "delivery": "full",
  "interaction_mode": "standard",
  "source": "popup",
  "confirmed_at": "2026-11-02T12:00:00",
  "note": "用户确认：先修漏识别，最窄新增，覆盖同聚类"
}
```

| 字段 | 允许值 | 作用 |
|---|---|---|
| `problem_type` | `miss` / `diff` / `extra` / `all` | `prepare`/`propose` 默认读取；`miss/diff/extra` 自动写 `target_scope.json` |
| `direction` | `append_first` / `fix_existing` / `delete_extra` / `paired` / `evidence_driven` / `report_only` | 设计倾向；`report_only` 不进入改表 |
| `directions` | 问题类型 → 方向 | `problem_type=all` 时按类型分别记录 |
| `scope` | `sample_only` / `cluster` / `broader_similar` / `not_sure` | 控制 pattern 覆盖边界；扩大需在 I4 再确认 |
| `delivery` | `full` / `precheck_only` / `dry_run_only` / `report_first` | 控制是否允许 `--apply` |
| `interaction_mode` | `guided` / `standard` / `minimal` | `guided` 所有条件门禁都问；`standard` 触发时才问；`minimal` 跳过 I4 和大部分 I2/I3/I5/I6，但保留 `zero_rows`、归属风险、`match_key`/`source_trace`、`simulate_mismatch` 硬触发 |
| `source` | `popup` / `user_message` | 记录入口定位来自弹窗还是用户原话 |

`source=user_message` 只用于用户本轮已明确问题类型 + 方向的情况；Agent 必须在对话里复述，不能自行推断后静默写入。

## 3.2 `match_review.json`（I4）

由 `check --only validate` 生成，只包含 `pending` 且 `valid` 的规则候选（`catalog`/`priority`/`suggestion` 不进入）。

```json
{
  "run_id": "run_20261102_120000",
  "interaction_mode": "standard",
  "required": true,
  "problem_type": "miss",
  "direction": "append_first",
  "groups": [{
    "order": 1,
    "target_file": "fee_engine/resources/fee_classification_rules.csv",
    "engine": "fee",
    "match_mode": "fee_regex",
    "pattern_column": "pattern",
    "table_note": "费用规则；pattern 为正则、IGNORECASE……",
    "counts": {"high": 0, "medium": 0, "low": 1},
    "popup_plan": {"questions": [{"risk": "low", "kind": "batch",
      "candidate_ids": ["P0011"], "option_keys": ["batch_recommended", "batch_review", "batch_reject"]}],
      "popups_needed": 1, "continuation": false},
    "candidates": [{
      "candidate_id": "P0011",
      "operation": "append",
      "problem_type": "miss",
      "risk_level": "low",
      "match_mode": "fee_regex",
      "pattern": "example monthly fee",
      "row": {"priority": "450", "pattern": "example monthly fee", "match_type": "regex"},
      "match_key": {},
      "row_hash": "sha256:0c2154…",
      "recommended": "recommended",
      "options": [
        {"key": "recommended", "label": "按建议正则：example monthly fee",
         "row": null, "requires_input": false, "input_hint": "",
         "note": "fee 表按 re.search + IGNORECASE", "row_hash_after": "sha256:0c2154…"},
        {"key": "narrower", "label": "更窄：整串精确匹配",
         "row": {"priority": "450", "pattern": "^(?:example monthly fee)$", "match_type": "regex"},
         "requires_input": false, "input_hint": "",
         "note": "转为整串锚定正则", "row_hash_after": "sha256:36caf3…"}
      ],
      "positive_examples": [{"kind": "positive", "text": "EXAMPLE MONTHLY FEE", "expected": "Fees"}],
      "negative_examples": [{"kind": "shadow", "text": "EXAMPLE FEE REFUND", "winning_keyword": "..."}],
      "expected_effect": {"matched_rows": 2, "resolved_rows": 2, "conflicts": 0, "already_correct": 0, "shadowed_rows": 0},
      "warnings": [],
      "question": {"header": "匹配逻辑 P0011", "prompt": "……", "option_keys": ["recommended", "narrower", "wider", "reject"]}
    }]
  }]
}
```

选项按目标表能力生成，Agent **不得自造**：

| 表能力 | 选项 |
|---|---|
| regex 表（fee / transfer / income / overdrawn / liability 信用卡 / 已用 regex 的 merchant_rule 行） | `recommended` / `narrower`（`^(?:…)$`）/ `wider`（`^(?:…)`）/ `reject` |
| keyword 表（catch_all 的 keyword 行、dishonour keyword、gambling/rent rule 层 keyword） | `recommended` / `narrower`（先 `re.escape` 再锚定）/ `wider` / `reject` |
| 仅关键词、无行内正则（all_other_credit、initial、liability counterparty / word_boundary / substring） | `recommended` / `adjust` / `reject` |
| gambling/rent 的 `source=institution` | `recommended` / `adjust`（只改变体）/ `reject` |
| `delete` | `recommended` / `custom`（换 `match_key`）/ `reject` |

- `row` 为 `null` 表示“不改候选行”；非空对象可直接作为 `dialog_patch.json` 的 `row`。
- `row_hash_after` 是应用该选项**之后**的候选哈希：机械选项（recommended/narrower/wider）由脚本预计算；`adjust`/`custom` 为 `null`，必须在 `revise` + 重新校验后从新 `match_review.json` 读取。
- `positive_examples` 来自 `expected_effect.examples`；`negative_examples` 来自 `shadow_examples`（被更强规则抢走）或 `delete_remaining_rule_examples`（删除后仍有规则命中）。两者是弹窗必读内容。

## 3.3 `match_review_decisions.json`（I4 结果）

```json
{
  "run_id": "run_20261102_120000",
  "generated_at": "2026-11-02T12:05:00",
  "decisions": [
    {"candidate_id": "P0011", "selected": "recommended", "row_hash": "sha256:0c2154…", "reason": "用户选择按建议"},
    {"candidate_id": "P0012", "selected": "narrower", "row": {"pattern": "^(?:EXAMPLE)$"}, "row_hash": "sha256:36caf3…", "reason": "用户要求整串精确"},
    {"candidate_id": "P0013", "selected": "adjust", "row_hash": "sha256:91ab02…", "reason": "用户补充关键词变体"},
    {"candidate_id": "P0014", "selected": "reject", "row_hash": "", "reason": "用户拒绝该匹配"}
  ]
}
```

- `selected` 只能是 `recommended` / `narrower` / `wider` / `adjust` / `custom` / `reject`。
- 非 `reject` 必须带 `row_hash`；`approve --build` 会与当前候选的 `common.candidate_row_hash()` 比较，不一致即硬拒绝。
- `narrower`/`wider` 的 `row_hash` 用 `options[].row_hash_after`；`adjust`/`custom` 在 `revise --validate` 后用重新生成的 `match_review.json` 里的候选 `row_hash`。
- 决策文件由 Agent 写，用户不手工编辑；不要为了让审批通过而改 `row_hash`。

## 3.4 `dialog_decisions.json`（I2/I3/I5/I6/I8 结果）

条件门禁的答案统一写在这个文件；Agent 根据 `AskUserQuestion` 的实际选择生成，用户不手工编辑。外层只允许以下字段，未知顶层字段直接报错：

```json
{
  "schema_version": 1,
  "run_id": "run_20261102_120000",
  "parse": {...},
  "attribution": {...},
  "revise": {...},
  "precheck": {...},
  "outcome": {...}
}
```

每个门禁段都是对象；`parse`/`attribution`/`precheck` 在门禁要求时必须带对应蓝图里的 `fingerprint` 和 `decided_at`，缺失或过期会硬停。`revise` 用 `patch_sha256` 绑定补丁；`outcome` 只做记录，不参与脚本门禁。

### 3.4.1 `parse`（I2）

```json
{
  "decision": "continue",
  "expected_authority": "sample",
  "column_map": {"expected_category": "期望分类", "current_category": "模型分类"},
  "type_distribution": "follow_sample",
  "problem_type": "all",
  "sheet": "Sheet1",
  "fingerprint": "sha256:...",
  "decided_at": "2026-11-02T10:00:00"
}
```

| 字段 | 允许值 | 说明 |
|---|---|---|
| `decision` | `continue` / `change_sheet` / `change_columns` / `adjust_problem_type` / `stop` | 非 `continue` 表示先重跑 `prepare`；重跑并更新决策前 `propose` 硬拒绝 |
| `expected_authority` | `sample` / `current` / `manual` / `stop` | 标签类触发必填；`current` 需先用 `--column-map` 交换两列并重跑 |
| `column_map` | 字段 → 实际列名 | `manual`/`current` 必填，且必须与当前 `parse_summary.json` 一致 |
| `type_distribution` | `follow_sample` / `keep_intake` / `one_type` / `stop` | `problem_type_mismatch` 触发时必填；`follow_sample` 要求 `problem_scope=all` |
| `problem_type` | `all` / `miss` / `diff` / `extra` | `one_type` 时必须是单一类型，且与当前 `problem_scope` 一致 |
| `sheet` | sheet 名 | `change_sheet` 时写目标 sheet |

`zero_rows` 任何模式都硬停。`interaction_mode=minimal` 跳过 I2，除非 `zero_rows`；`guided` 即使无触发也确认解析结果（第 1 问）。

### 3.4.2 `attribution`（I3）

```json
{
  "fallback": "suggestion",
  "decisions": [
    {"cluster_id": "C0003", "selection": "recommended",
     "engine": "fee", "target_file": "fee_engine/resources/fee_classification_rules.csv",
     "decided_at": "2026-11-02T10:05:00"},
    {"cluster_id": "C0007", "selection": "alternative",
     "engine": "catch_all", "target_file": "catch_all_engine/resources/catch_all_rules.csv",
     "decided_at": "2026-11-02T10:05:00"}
  ],
  "fingerprint": "sha256:...",
  "decided_at": "2026-11-02T10:05:00"
}
```

| 字段 | 允许值 | 说明 |
|---|---|---|
| `selection` | `recommended` / `alternative` / `evidence` / `stop` | `alternative` 必须写 `engine`+`target_file` 且命中蓝图候选表；`stop` 丢弃该聚类草稿 |
| `engine` / `target_file` | 蓝图候选表 | `recommended` 可省略（取蓝图推荐）；写了必须与推荐一致 |
| `fallback` | `suggestion` / `search_then_retry` / `full_diagnosis` / `stop` | `evidence` 必填；后三者表示尚未定表，会阻塞 `propose` |

蓝图 `required=true` 时，每个触发聚类都必须有一条决策；`guided` 时即使无触发也要逐聚类确认。`approve --build` 还会复核 confirmed 候选的 `engine`/`target_file` 与 I3 选择一致。

### 3.4.3 `revise`（I5）

```json
{
  "decision": "apply",
  "patch_sha256": "sha256:...",
  "fingerprint": "sha256:...",
  "decided_at": "2026-11-02T10:10:00"
}
```

| 字段 | 允许值 | 说明 |
|---|---|---|
| `decision` | `apply` / `continue_editing` / `cancel` | 非 `apply` 不写入；`continue_editing` 表示回到对话改补丁 |
| `patch_sha256` | `revise_preview.json` 的值 | 触发 `match_key`/`source_trace`/多字段变更或 `guided` 时必填；补丁改过会过期 |
| `fingerprint` | `revise_preview.json` 的值 | 可选；写了就必须匹配 |

`guided` 时所有修订都要确认；`minimal` 只在 `match_key`/`source_trace` 变更时强制。

### 3.4.4 `precheck`（I6）

```json
{
  "decision": "continue",
  "impact": "accept",
  "verify_scope": "full",
  "acknowledge_mismatch": true,
  "fingerprint": "sha256:...",
  "decided_at": "2026-11-02T10:15:00"
}
```

| 字段 | 允许值 | 说明 |
|---|---|---|
| `decision` | `continue` / `revise` / `degrade` / `stop` | 只有 `continue` 能进入阶段五 |
| `impact` | `accept` / `narrow` / `need_samples` / `stop` | 只有 `accept` 能进入阶段五 |
| `verify_scope` | `auto` / `local` / `full` / `current` | `auto` 按 `task_profile`；`current` 沿用命令默认；命令行显式 `--verify-scope` 优先 |
| `acknowledge_mismatch` | `true` / `false` | `triggers` 含 `simulate_mismatch` 时必须为 `true` |

`precheck.json` 的触发项：`simulate_mismatch`、`validation_warnings`、`shadowed_rows`、`conflicts`、`engine_default_trace`、`approximate_effect`、`reconcile_incomplete`。无触发时 `required=false`，`standard` 跳过；`guided` 且存在可审批候选时也要求确认；`minimal` 只强制 `simulate_mismatch`。`precheck.json` 缺失时 `approve --build` 直接拒绝（先跑 `check --only reconcile`）；已写过的 `precheck` 决策会继续校验 `fingerprint`，触发器消失也不能让旧确认混入新一轮审批。

### 3.4.5 `outcome`（I8）

```json
{
  "decision": "accept",
  "remaining": "end",
  "decided_at": "2026-11-02T10:30:00"
}
```

| 字段 | 允许值 | 说明 |
|---|---|---|
| `decision` | `accept` / `rollback` / `full_then_decide` / `export_report` | `rollback` 仍需用户明确授权后才执行 `--rollback --apply` |
| `remaining` | `continue` / `end` / `export_pending` | 本轮剩余处理方式 |

写后验证失败、未审批文件或硬回归时不写 `outcome`，直接走失败/回滚路径。

## 4. 各引擎追加模板

```json
// initial —— 关键词只能大写字母数字空格，最多 50 个变体
{"merchant_name": "QUINS GOURMET BUTCHER", "keywords": "QUINS GOURMET BUTCHER|QUINS GOURMET", "category": "Groceries"}

// transfer external high/medium —— 小写正则，dr_cr 可选
{"priority": "55", "rule_name": "EXAMPLE_EXTERNAL", "category": "External Transfers", "pattern": "example payee", "dr_cr": "debit", "description": "..."}

// dishonour
{"rule_type": "keyword", "pattern": "INSUFFICIENT FUNDS", "required_terms": ""}

// gambling rule / institution
{"rule_name": "EXAMPLE_BET", "category": "Gambling", "pattern": "EXAMPLEBET", "match_type": "keyword", "confidence": "0.95", "counterparty": "ExampleBet", "source": "rule"}
{"rule_name": "EXAMPLEBET_INST", "category": "Gambling", "pattern": "EXAMPLEBET|EXAMPLE BET", "match_type": "keyword", "confidence": "0.99", "counterparty": "ExampleBet", "source": "institution"}

// income —— 每行都是正则；group 决定分类
{"pattern_group": "salary_packaging", "pattern": "EXAMPLE PAY", "match_type": "regex", "description": "..."}

// liability 交易对手关键词 —— product_type 只能取表内已有值
{"keyword": "EXAMPLE FINANCE; EXAMPLEFIN", "counterparty": "Example Finance", "product_type": "personal_loan", "match_type": "keyword"}

// liability 信用卡 —— keyword 是正则，match_type 是死字段
{"priority": "100", "account_type": "*", "dr_cr": "debit", "bank": "*", "match_type": "regex", "keyword": "EXAMPLE CARD", "min_prefix_len": "", "counterparty": "Example Card", "product_type": "bank", "exclude_pattern": ""}

// all_other_credit —— 只认 keyword
{"rule_type": "keyword", "pattern": "PL REFUND", "required_terms": ""}

// fee —— 每行都是 IGNORECASE 正则
{"priority": "450", "rule_name": "EXAMPLE_FEE", "category": "Fees", "pattern": "example monthly fee", "counterparty": "Example Bank", "match_type": "regex", "zero_amount_reject": "false", "description": "...", "unclassified_only": "false", "dr_cr": "debit"}

// rent
{"rule_name": "EXAMPLE_RENT", "category": "Rent", "pattern": "EXAMPLE PROPERTY", "match_type": "keyword", "confidence": "0.95", "counterparty": "Example Property", "source": "rule"}

// catch_all
{"rule_name": "EXAMPLE_HEALTH", "category": "Health", "pattern": "EXAMPLE CLINIC", "match_type": "keyword", "confidence": "0.9"}
```

以上模板只描述 `row`；完整候选还必须带 `source_trace`。

## 5. `approval_groups.json`

由 `check` 生成，阶段五按它逐表弹窗：

```json
{
  "popup_contract": {
    "per_config_table": true,
    "question_order": ["high", "medium", "low"],
    "max_questions_per_popup": 4,
    "max_options_per_question": 4
  },
  "groups": [{
    "order": 1,
    "target_file": "liability_engine/resources/counterparty_keyword_rules.csv",
    "engine": "liability",
    "counts": {"high": 2, "medium": 1, "low": 3},
    "risk_groups": {
      "high": [{"candidate_id": "P0011", "operation": "append", "pattern": "...",
                "trace_method": "routing.candidate_file_hint"}],
      "medium": [], "low": []
    },
    "popup_plan": {
      "questions": [
        {"risk": "high", "kind": "multi_select", "candidate_ids": ["P0011", "P0012"]},
        {"risk": "low", "kind": "batch", "candidate_ids": ["P0014", "P0015", "P0016"]}
      ],
      "popups_needed": 1,
      "continuation": false
    }
  }]
}
```

- 一个 `groups[]` = 一张 `target_file` = 一次弹窗；`popup_plan.questions` 已按高风险→中风险→低风险排好。
- `multi_select` 每块最多 3 条候选 + “全部拒绝”；`batch` 是低风险整组单选。
- `popups_needed > 1` 时同表续窗，不与别的表混排。
