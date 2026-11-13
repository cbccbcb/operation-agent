# 机器可读规则索引（rule_index.json）

`CLAUDE.md` 和 `references/rule-file-catalog.md` 是解释性文档，不是运行时索引。每次任务都重新解析这些长文，既慢又容易读漏。`scripts/_internal/rule_index.py` 把同一套知识固化成数据，并与仓库真实配置合并，输出 `rule_index.json`；入口路由 `bscat.py route` 只查索引，不重新读文档。

索引**在运行时生成**，不签入仓库、不手工编辑。仓库换了分支、改了 `pipeline.json`/`category_catalog.json`，下一次生成就会反映出来；与登记不一致的地方写进 `index_warnings`，并把相关路由升级为 `full`。

## 生成

```bash
<venv-python> scripts/bscat.py route --repo <repo> --input <file> \
    --out <review-dir>
```

`prepare` 也会在阶段一自动生成 `<review-dir>/rule_index.json` 与 `task_profile.json`。

数据来源：

| 来源 | 提供什么 |
|---|---|
| `scripts/_internal/common.py:RULE_FILES` | 有哪些表、属于哪个引擎、列/匹配模式/是否可追加 |
| `scripts/_internal/rule_index.py:ENGINE_PROFILES` | 归一化、匹配语义、执行带、跨引擎守卫、先声明策略、结构风险 |
| `scripts/_internal/rule_index.py:OVERRIDE_RULES` | `CLAUDE.md` 里的跨引擎覆盖/让位例外 |
| `configs/pipeline.json` | 启用引擎、真实 priority、执行顺序 |
| `configs/category_catalog.json` | 分类归属（`owner_engine_id`）与启用状态 |
| 目标表真实表头 | 校验 schema 是否与登记一致（漂移进 `index_warnings`） |

## 顶层结构

```jsonc
{
  "schema_version": 1,
  "generated_at": "...",
  "repo": "...",
  "source": { "...": "每个数据块的来源" },
  "execution_order": ["transfer", "initial", "..."],   // pipeline 实际顺序
  "priorities": {"transfer": 1, "...": 0},
  "enabled_engines": ["..."],
  "engines": { "<engine_id>": { ... } },
  "categories": { "<category>": { ... } },
  "override_rules": [ ... ],
  "escalation_policy": { ... },
  "index_warnings": ["..."]
}
```

### `engines[]`

| 字段 | 含义 |
|---|---|
| `priority` / `execution_rank` / `band` | 真实优先级、执行名次（1 起）、执行带（`first`/`early`/`mid`/`late`/`last`） |
| `enabled` | 是否在 `pipeline.json` 启用 |
| `structural` | 是否为结构性引擎（income/liability/transfer），任何候选都高风险 |
| `normalization` / `match_summary` / `match_modes` | 文本预处理与匹配语义 |
| `output_categories` | 引擎能输出的分类（`null` 表示由规则/商户决定） |
| `can_append_category` | 能否通过追加实现目标分类（`owned_only`/`limited`/`unknown` 等） |
| `prior_claim_policy` / `prior_claim_policy_label` | 对更早引擎声明的处理方式（`first`/`overwrite_prior`/`exclude_prior_claimed` 等） |
| `prior_claim_exceptions` / `excluded_by_engines` / `final_against_engines` | 让位与终局认领的特殊关系 |
| `guards` / `warnings` / `escalation` | 写规则时必须知道的约束、风险、升级条件 |
| `layer_semantics` | 双层引擎（gambling/rent）的 institution/rule 层语义 |
| `table_order` / `row_filters` / `cleared_categories` | liability 子步骤顺序、行过滤、initial 被清除的分类 |
| `default_target_file` / `tables[]` | 默认表与全部规则表（含 `appendable`、`match_mode`、`schema_warnings`） |

### `categories[]`

每个分类给出 `owners`（catalog 原始归属，可能多个）、`enabled_owners`、`recommended_engine`（结合 `CATEGORY_ENGINE_PREFERENCE` 与启用状态）、`tables[]`、`appendable` 和分类级 `escalation`。`recommended_engine` 只是**建议**，不是归属复核的替代。

### `override_rules[]`

`effect` 取值：

| effect | 含义 | 路由处理 |
|---|---|---|
| `later_overwrites` | 后执行引擎行级覆盖先执行引擎 | 作为基线，不逐个引擎展开 |
| `row_level_override` | 特定后续引擎会覆盖该分类 | 进入 `incoming_overrides` |
| `from_engine_wins` | 先执行引擎反过来压住后执行引擎（gambling 对 income/liability） | 高风险 `incoming_overrides` |
| `prior_claim_blocks` | 候选只在未认领行生效，或被更早声明挡住 | 进入 `append_blocking_rules` 或 `non_overriding_later_engines` |
| `conditional` | 满足条件才覆盖/让位 | 按条件进入覆盖或阻断列表 |
| `within_engine` | 同引擎内层级/子表优先级 | `within_engine_precedence`（局部验证） |
| `from_engine_dropped` | 分类在 pipeline 中被清除 | 作为守卫提示 |

`target_tables` 不为空时，只对相关表生效；避免把 gambling/rent 的层级规则误报给别的表。

## 入口六问

`route_task()` 对每个任务返回 `answers`：

| 问题 | 字段 | 直接回答 |
|---|---|---|
| Q1 目标分类在哪个引擎、哪张表？ | `q1_engine_and_table` | `engine`、`target_file`、`owner_engines`、`candidate_tables`、`attribution_risk` |
| Q2 这张表怎么匹配？ | `q2_match_logic` | `match_mode`、`normalization`、`summary`、`guards` |
| Q3 先执行还是后执行？ | `q3_early_or_late` | `priority`、`rank`、`band`、`runs_before`、`runs_after` |
| Q4 能否靠追加实现？ | `q4_append_feasible` | `can_append`、`blockers`、`caveats` |
| Q5 谁可能覆盖/阻断它？ | `q5_overriders` | `risk`、`incoming_overrides`、`append_blocking_rules`、`non_overriding_later_engines`、`unrelated_later_engines`、`within_engine`、`outgoing_special_rules` |
| Q6 何时升级为完整诊断？ | `q6_escalation` | `tier`、`escalate_to_full`、`reasons`、`code_diagnosis` |

`attribution_risk` 非空表示路由给出的表可能**归属不对**：

- `catch_all_holds_merchant`：文本像商户名/品牌名，应复核是否走 initial；
- `initial_holds_generic`：文本像通用描述词，应复核是否走 catch_all 或专属引擎。

路由结论是入口判断；阶段三仍必须走 `CLAUDE.md` 的归属决策树。

## 匹配能力与 I4 选项

`rule_index.json` 的 `match_modes` 是运行时匹配语义来源；I4 的 `match_review.json` 按目标表的 `match_mode` 生成选项，**不提供引擎会忽略的写法**：

| `match_mode` | 代表表 | I4 可用选项 |
|---|---|---|
| `fee_regex` / `transfer_regex` / `income_regex` / `raw_regex_ci` / `liability_credit_card` | fee、transfer、income、liability overdrawn/信用卡 | 按建议 / 更窄 `^(?:…)$` / 更宽 `^(?:…)` / 拒绝 |
| `merchant_rule`（`match_type=regex` 或 keyword） | catch_all、gambling/rent rule 层 | 按建议 / 更窄 / 更宽 / 拒绝；`source=institution` 只允许改变体 |
| `dishonour` | dishonour | 按建议 / 更窄（`rule_type=regex`）/ 更宽 / 拒绝 |
| `initial_variants` | initial | 按建议 / 调整变体 / 拒绝（无正则） |
| `all_other_credit` | all_other_credit | 按建议 / 调整关键词 / 拒绝（regex 会被静默忽略） |
| `liability_counterparty` / `word_boundary_ci` / `substring` | liability 关键词表 | 按建议 / 调整关键词 / 拒绝（无行内正则） |
| `liability_homecar` | 房贷车贷 | 按建议 / 调整 `match_scope`/`amount_gt` / 拒绝 |

若用户要求的语义不在上表（例如 all_other_credit 用正则），不要生成候选行：改用关键词/变体，或降级 `suggestion` 并说明引擎限制。

## I3 归属确认与目标表映射

`prepare` 在 `task_profile.json` 之后生成 `<review-dir>/attribution_review.json`，把六问 Q1 的归属风险固化成可对话确认的数据。`propose` 读取它和 `dialog_decisions.json#attribution` 后才写候选：

| 字段 | 含义 |
|---|---|
| `required` | 是否有任何聚类触发 I3；`guided` 时只要存在聚类就为 true |
| `clusters[].cluster_id` | 与 `task_profile`/`diagnosis` 的聚类 ID 一致 |
| `clusters[].triggers` | `attribution_risk`、`owner_ambiguity`、`unresolved_layer`、`multiple_candidate_tables`、`missing_target_file`、`missing_candidate_file_hint`、`routing_obstacle` |
| `clusters[].recommended` | 推荐 `(engine, target_file)`；来自 `task_profile.target_file` 与索引 |
| `clusters[].candidate_tables` | 允许用户选择的全部表；用户选 `alternative` 时必须命中这里 |
| `clusters[].alternatives` | 备选表及理由（归属、匹配模式、可追加性） |
| `clusters[].questions` | 交给 `AskUserQuestion` 的逐聚类问题与选项 |
| `fingerprint` | 触发项 + 推荐表 + 候选表 + 聚类的哈希；决策必须回填 |

处理规则：

- `recommended`：保持推荐表；若决策里写了 `engine`/`target_file`，必须与推荐一致。
- `alternative`：只保留与所选 `(engine, target_file)` 一致的草稿；全都对不上时硬错，不写 `suggestion`。
- `evidence`：必须带 `fallback`；`suggestion` 使该聚类降级为 `suggestion`（只报告），`search_then_retry`/`full_diagnosis`/`stop` 阻塞 `propose`。
- `stop`：丢弃该聚类草稿；不得绕过。
- 任何规则草稿的 `trace_method=engine_default` 且对应聚类无 I3 决策时，`propose` 硬错。
- I3 未触发时，旧决策不会过滤新一轮草稿；只有仍走 `engine_default` 的草稿需要补一条针对性决策，且该决策的 `fingerprint` 必须与当前 `attribution_review.json` 一致，否则 `propose` 硬错要求重新确认。
- `interaction_mode=guided` 时，即使无触发也要为每个聚类确认目标表；`standard` 才按触发项跳过。
- `approve --build` 复核 confirmed 候选的 `engine`/`target_file` 与 I3 选择一致，防止审批前改表。

`attribution_review.json` 缺失时（旧 review 目录），先重跑 `prepare`；不要手工补决策。

## 验证档位

档位取所有触发条件中的**最高档**：

| 档位 | 触发条件（任一） | 代码诊断 | 写后验证 |
|---|---|---|---|
| `target`（T0） | 干净 append + 单一非结构性引擎 + 无覆盖风险 + 影响 ≤5 行 | `skip` | `--verify-scope target` |
| `local`（T1） | `edit`/`delete`/`keyword_update`；diff/extra；同引擎竞争；后续引擎覆盖；6–20 行；多归属分类；attribution 复核；双层未定 | `optional` | `--verify-scope local` |
| `full`（T2） | income/liability/transfer；`priority`/`catalog`；跨引擎守卫或删除接管不明；>20 行；无法确定引擎/表；目标表缺失或 schema 漂移；索引漂移 | `required` | `--verify-scope full`（自动 baseline） |

`prepare --diagnose auto` 用 `task_profile.overall.needs_code_diagnosis`；diff/extra、异常 routing status 和 `full` 档位会置为 `true`。档位**只改变诊断深度和验证范围**，不跳过阶段四校验、对话审批、dry-run、写入备份、写后真实回归和回滚。

## 产物

| 文件 | 生成者 | 内容 |
|---|---|---|
| `rule_index.json` | `prepare` / `route --out` | 上面的完整索引 |
| `route_decision.json` | `route --out` | `overall` 档位 + 逐任务六问 + `index_warnings` |
| `task_profile.json` | `prepare` | 逐聚类 `validation_tier`、`escalation_reasons`、`target_file`、`needs_code_diagnosis` |
| `parse_review.json` | `prepare` | I2 解析摘要、触发项、列候选、sheet、问题类型分布、提问与 `fingerprint` |
| `attribution_review.json` | `prepare` | I3 逐聚类 `target_file` 候选、推荐、备选、触发项、提问与 `fingerprint` |
| `precheck.json` | `check --only reconcile` | I6 触发项、建议验证范围、问题、影响面与 `fingerprint` |
| `revise_preview.json` | `revise --dry-run`（含兼容 CSV 合并） | I5 修订字段、触发项、`patch_sha256` 与 `fingerprint` |
| `intake.json` | Agent（I1） | 问题类型、方向、范围、交付方式；`prepare`/`propose` 默认读取 |
| `match_review.json` | `check --only validate` | 按目标表分组的匹配逻辑、正反例、选项与哈希 |
| `match_review_decisions.json` | Agent（I4） | 用户选择的匹配方式与确认哈希；`approve --build` 校验 |
| `dialog_decisions.json` | Agent（I2/I3/I5/I6/I8） | 条件门禁决策；各段带 `fingerprint`/`decided_at`（`revise` 用 `patch_sha256`） |

`task_profile.json` 是写后验证档位的首选来源；`validate_after.py --scope auto` 会把它与候选自身影响面取较高者。没有 profile 时按 `evidence.json` 推导，再退到目标行。

## 维护

引擎代码或配置表变化时：

1. 改 `common.RULE_FILES`（新增/删除表、列、匹配模式）；
2. 改 `ENGINE_PROFILES`（归一化、匹配语义、守卫、先声明策略）；
3. 改 `OVERRIDE_RULES`（新增/调整跨引擎例外，补 `source` 指向 `CLAUDE.md` 章节）；
4. 若字段含义变化，提升 `SCHEMA_VERSION`；
5. 重跑 `bscat.py route`，确认 `index_warnings` 为空或可解释。

不要手改 `rule_index.json`：它是运行时产物，下一次 `prepare`/`route` 会覆盖。

## 漂移与排查

| 现象 | 含义 | 处理 |
|---|---|---|
| `index_warnings` 提到目标表/引擎 | 表不存在或表头与 `RULE_FILES` 登记不一致 | 按 `full` 处理；核对真实表头，修 `RULE_FILES` 或仓库 |
| `ENGINE_PROFILES 缺少该引擎` | pipeline/RULE_FILES 出现未登记引擎 | 补 profile；补之前任何相关候选按 `full` |
| `unknown_engine` / `missing_engine` | 路由无法确定目标引擎 | 不得猜表；先 `catalog`/`search` + 只读诊断 |
| `engine_disabled` | 引擎在 `pipeline.json` 未启用 | 停止，向用户确认是否改错仓库/分支 |
| `append_not_feasible` | 该表不能追加或分类不归它 | 改 `edit`/`delete`/`keyword_update`，或升级诊断 |
| `owner_ambiguity` | 分类有多个启用归属引擎 | 按证据选最窄归属；无法确定时 `full` |
| `unresolved_layer` | gambling/rent 未指定层 | 指定 `--layer institution|rule`；商户名走 institution，通用词走 rule |
| `target_fallback`（验证报告） | 目标范围回退到问题行 | 确认候选/`apply_report.json` 存在，或重新生成 `task_profile.json` |
| 范围外 `claim_changes` | 局部验证发现别处声明变化 | 逐条复核；不确定就补跑 `--scope full` |
