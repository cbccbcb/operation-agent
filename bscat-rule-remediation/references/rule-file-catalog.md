# 配置表目录（溯源用）

每条规则候选在写 `source_trace` / `target_file` 之前，先确认「证据属于哪张表、这张表存什么、能不能追加」。

- **机器路由**：`bscat.py route --repo <repo> --category <c> [--engine <e>] ...` 直接查运行时 `rule_index.json`，回答目标引擎/表、匹配语义、执行顺序、追加可行性、覆盖者与升级条件；不要每次重新解析本文或 `CLAUDE.md`。索引 schema 见 `references/rule-index.md`。
- 运行时权威版本：`bscat.py catalog --repo <repo> [--engine <engine>] [--json]`，它直接读 `RULE_FILES` 和真实表头。
- 本文与 `RULE_FILES` 不一致时，以 `RULE_FILES` + 实际表头为准，并同步更新本文与 `rule_index.py` 的登记。
- 只允许写 `RULE_FILES` 中 `appendable=true` 的表；其余表只读。

## 快速选择

| 证据/问题 | 优先查的表 | 说明 |
|---|---|---|
| 商户名、品牌名、平台名 | `initial_engine/merchant_kb.csv` | 先 `search` 查重；品牌名绝不进 catch_all |
| 转账描述（Fast Transfer / Osko / 内部划转） | `transfer_engine/resources/transfer_*` | 内部 vs 外部先分清 |
| 拒付/退票（dishonour） | `dishonour_engine/resources/dishonour_rules.csv` | 通用拒付词 |
| 赌博/博彩平台或通用博彩词 | `gambling_engine/resources/gambling_rules.csv` | 平台名走 institution 层，通用词走 rule 层 |
| 工资、福利、Centrelink、自雇收入 | `income_engine/resources/income_pattern_rules.csv` | 不是纯文本规则，看 `pattern_group` |
| 催收 | `liability_engine/resources/debt_collection_rules.csv` | 机构名/催收描述 |
| 债务整合 | `liability_engine/resources/debt_consolidation_rules.csv` | — |
| 透支 | `liability_engine/resources/overdrawn_rules.csv` | — |
| 信用卡还款 | `liability_engine/resources/credit_card_rules.csv` | `keyword` 列实际是 regex |
| 房贷、车贷 | `liability_engine/resources/home_loan_car_loan_rules.csv` | `target_field` 区分 home/car |
| 其他贷款机构、个人贷、BNPL 放款/还款 | `liability_engine/resources/counterparty_keyword_rules.csv` | liability 默认贷款表 |
| 退款、返现、利息（入账） | `all_other_credit_engine/resources/all_other_credit_rules.csv` | 只处理 credit、只支持 keyword |
| 费用描述 | `fee_engine/resources/fee_classification_rules.csv` | 大小写不敏感 regex |
| 房租/租金 | `rent_engine/resources/rent_rules.csv` | 机构层优先于 rule 层 |
| 通用描述词（非商户名） | `catch_all_engine/resources/catch_all_rules.csv` | 只接前面引擎未认领的行 |

> 证据同时像多张表时：先按引擎实际执行顺序判断谁会真正接管，再选能覆盖证据的**最窄**表；在 `rationale` 写明为什么不是另一张表。拿不准就降级 `suggestion`。

## transfer（priority 1）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `transfer_engine/resources/transfer_external_high_confidence_rules.csv` | 外部转账高置信 regex | `pattern`，小写文本；priority 升序第一匹配；`category` 列被忽略 | appendable |
| `transfer_engine/resources/transfer_external_medium_confidence_rules.csv` | 外部转账中置信 regex | 同上，惯例 priority 从 101 起 | appendable |
| `transfer_engine/resources/transfer_internal_regex_rules.csv` | 内部转账 regex | `pattern`；输出恒 `Internal Transfer` | appendable |
| `transfer_engine/resources/transfer_pairing_exclusions.csv` | 转账配对排除 | `keyword` + priority 降序 | 只读 |
| `transfer_engine/resources/transfer_counterparty_rules.csv` | 交易对手映射 | `keyword` 子串，默认 `Miscellaneous Funds Transfer` | 只读 |
| `transfer_engine/resources/transfer_indicator_patterns.csv` | 内部配对指示器 | `pattern` regex | 只读 |
| `transfer_engine/resources/transfer_group_exclusion_patterns.csv` | 内部配对组排除 | `pattern` regex | 只读 |
| `transfer_engine/resources/transfer_row_exclusion_patterns.csv` | 内部配对行排除 | `pattern` regex | 只读 |

## initial（priority 10）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `initial_engine/merchant_kb.csv` | 商户名 → 分类 | `merchant_name, keywords, category`；keywords 用 `|` 分隔、每商户 ≤50；Aho-Corasick 全词、最长胜出；关键词必须预清洗为**大写、仅 `[A-Z0-9 ]`、压缩空格** | appendable |

## dishonour（priority 150）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `dishonour_engine/resources/dishonour_rules.csv` | 拒付/退票规则 | `rule_type, pattern, required_terms`；keyword 为 `re.escape` 子串（大小写不敏感），regex 为 IGNORECASE；`required_terms` 分号分隔、AND | appendable |

## gambling（priority 180）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `gambling_engine/resources/gambling_rules.csv` | 博彩平台名（institution）与通用博彩词（rule） | 7 列；`source=institution` 用 `|` 变体、`clean_text_with_channel_prefix` 全词最长、结构性优先；`source=rule` 用 `clean_text`、最高 confidence、让位 transfer/initial/dishonour | appendable |

## income（priority 200）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `income_engine/resources/income_pattern_rules.csv` | 收入行为规则 | `pattern_group, pattern, match_type, description`；每行 regex，在 `clean_text_with_seams` 上匹配；`pattern_group` 决定语义：`strong_wage` / `medium_income` / `centrelink` / `self_employed_gig` / `hard_negative` / `soft_negative` / `transfer_from` / `pay_signal` / `gig_*` / `behavior_exclusion` 等；只有部分组直接产出分类，其余是门控 | appendable（高风险） |

## liability（priority 300，多表）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `liability_engine/resources/counterparty_keyword_rules.csv` | 贷款机构/交易对手关键词 → `product_type` | `keyword, counterparty, product_type, match_type`；keyword 分号分隔；边界 `(?<![A-Za-z])...(?!A-Za-z])`（数字可穿透）；最先执行，第一匹配胜出；**liability 默认贷款表** | appendable |
| `liability_engine/resources/credit_card_rules.csv` | 信用卡还款 regex | `keyword` 列实际是 regex（IGNORECASE）；priority 分 `>=90` 与 `<90` 两档；只填充未认领行；`product_type` 决定最终 bscat | appendable |
| `liability_engine/resources/home_loan_car_loan_rules.csv` | 房贷/车贷规则（Format A 12 列） | `target_field` 为 `is_home_loan` / `is_car_loan`；`match_scope` / `amount_gt` 是门控；keyword 约束会被丢弃 | appendable |
| `liability_engine/resources/dishonours_rules.csv` | liability 侧拒付规则 | `rule_type, pattern, required_terms`；keyword 为小写子串，regex 大小写敏感 | appendable |
| `liability_engine/resources/overdrawn_rules.csv` | 透支规则（Format B） | `counterparty, rule_id, match_type, pattern, note`；regex IGNORECASE | appendable |
| `liability_engine/resources/debt_collection_rules.csv` | 催收关键词 | `counterparty, product_type, rule_id, match_type, keyword`；分号关键词合并为 `\b(?:...)\b` IGNORECASE | appendable |
| `liability_engine/resources/debt_consolidation_rules.csv` | 债务整合关键词 | 同上（无 `rule_id`） | appendable |
| `liability_engine/resources/bnpl_maximum_limits.csv` | BNPL 限额配置 | `rate_type, counterparty, value`；**不是 matcher，不生成规则** | 只读 |

### liability 选表口诀

1. 催收 → `debt_collection_rules.csv`；债务整合 → `debt_consolidation_rules.csv`。
2. 透支 → `overdrawn_rules.csv`；拒付 → `dishonours_rules.csv`。
3. 信用卡还款 → `credit_card_rules.csv`（`keyword` 写 regex）。
4. 明确是房贷/车贷描述 → `home_loan_car_loan_rules.csv`。
5. 其他贷款机构、个人贷、BNPL 放款/还款 → `counterparty_keyword_rules.csv`。
6. BNPL 限额只查 `bnpl_maximum_limits.csv`，不改它。

> `counterparty` 必须与证据中的机构名称一致；`product_type` 只能取目标表已有值。

## all_other_credit（priority 400）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `all_other_credit_engine/resources/all_other_credit_rules.csv` | 退款/返现/利息等入账 | `rule_type, pattern, required_terms`；**只支持 keyword**（regex 与 `required_terms` 静默忽略），`re.escape` 子串；只处理 `dr_cr=credit`；可覆盖 External Transfers | appendable |

## fee（priority 500）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `fee_engine/resources/fee_classification_rules.csv` | 各类费用 | `priority, rule_name, category, pattern, counterparty, match_type, zero_amount_reject, description, unclassified_only, dr_cr`；regex IGNORECASE；priority 升序第一匹配；`category` 为 `fee`（→Fees）或 `Overdrawn` | appendable |

## rent（priority 800）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `rent_engine/resources/rent_rules.csv` | 房租/租金机构（institution）与通用词（rule） | 7 列；`source=institution` 用 `|` 变体、全词最长、结构性优先于 rule 层；`source=rule` 用 `clean_text`、最高 confidence | appendable |

## catch_all（priority 999）

| 配置表 | 存什么 | 关键列 / 匹配 | 写入 |
|---|---|---|---|
| `catch_all_engine/resources/catch_all_rules.csv` | 通用描述词（非商户名） | `rule_name, category, pattern, match_type, confidence`；`clean_text` 大写；keyword 全词 / regex；最高 confidence 胜出；`exclude_prior_claimed`；confidence 建议 0.55–0.90 | appendable |

## 溯源记录怎么写

```json
"source_trace": {
  "target_file": "liability_engine/resources/home_loan_car_loan_rules.csv",
  "candidate_file_hint": "liability_engine/resources/counterparty_keyword_rules.csv",
  "trace_method": "manual",
  "override_reason": "证据是房贷描述（HOME LOAN），home_loan_car_loan 才是对应表；counterparty 表不是最窄归属",
  "matched_rule": {"file": "...", "line": 0, "rule_id": "...", "pattern": "..."}
}
```

- `override_reason` 必须写清「为什么证据提示的表不对、为什么这张表更窄」，不能只写“人工判断”。
- `edit` / `delete` / `keyword_update` 的 `matched_rule.file` 必须就是实际命中的表。
