#!/usr/bin/env python3
"""Machine-readable rule index and entry-point task router.

The prose documents (``CLAUDE.md``, ``references/rule-file-catalog.md``) explain
engine semantics at length.  Re-reading them for every task is slow, easy to
misread, and lets the router drift between runs.  This module exports the same
knowledge as data and merges it with the repository's real configuration:

* ``common.RULE_FILES`` -- which tables exist, their columns and match modes;
* ``ENGINE_PROFILES`` -- normalisation, precedence, guards, prior-claim policy,
  structural risk and append constraints;
* ``OVERRIDE_RULES`` -- the cross-engine exceptions documented in CLAUDE.md;
* ``configs/pipeline.json`` + ``configs/category_catalog.json`` -- actual
  priorities, enabled engines and category owners.

``build_index(repo)`` writes/returns ``rule_index.json``.  ``route_task(...)``
answers the six entry-point questions and chooses a validation tier:

1. which engine/table owns the category;
2. how that table matches;
3. whether it runs early or late;
4. whether an append can realise the category;
5. which other rules can override or block it;
6. what forces escalation to full diagnosis.

The index is generated at runtime so it cannot silently outlive the repository.
Drift warnings are part of the payload and escalate the route to ``full``.

    python rule_index.py --repo D:/Projects/AUS_BS_CAT_Model \
        --category Health --problem-type miss --json
    python rule_index.py --repo D:/Projects/AUS_BS_CAT_Model \
        --input sample.xlsx --problem-type all --out reviews/run-001
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Windows pipes default to the active code page; keep Chinese output readable
# in Claude Code and in subprocesses that inherit this environment.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

SCHEMA_VERSION = 1

TIER_ORDER = {"target": 0, "local": 1, "full": 2}
TIER_LABELS = {
    "target": "T0 目标案例验证",
    "local": "T1 局部冲突验证",
    "full": "T2 全局完整验证",
}
STRUCTURAL_ENGINES = {"income", "liability", "transfer"}
CONFIG_OPERATIONS = {"priority", "catalog"}
RULE_OPERATIONS = {"append", "edit", "delete", "keyword_update"}

GENERIC_GAMBLING_WORDS = {
    "BET", "BETS", "BETTING", "CASINO", "LOTTERY", "LOTTO", "POKER",
    "POKIES", "SLOT", "SLOTS", "SPORTSBET", "GAMBLING", "WAGER", "KENO",
}
GENERIC_RENT_WORDS = {
    "RENT", "RENTAL", "LEASE", "LANDLORD", "TENANCY", "STRATA",
}
GENERIC_DESCRIPTOR_WORDS = {
    "REFUND", "CASHBACK", "INTEREST", "FEE", "FEES", "CHARGE", "CHARGEBACK",
    "TRANSFER", "PAYMENT", "DEPOSIT", "WITHDRAWAL", "SALARY", "WAGES",
    "OVERDRAWN", "REVERSAL", "ADJUSTMENT", "CREDIT", "DEBIT",
}
MERCHANT_MARKER_WORDS = {"PTY", "LTD", "AU", "AUS", "AUSTRALIA"}


def _looks_like_merchant(text: str) -> bool:
    cleaned = common.clean_text(text)
    tokens = cleaned.split()
    if not tokens:
        return False
    if "&" in text:
        return True
    if any(token in MERCHANT_MARKER_WORDS for token in tokens):
        return True
    if any(any(char.isdigit() for char in token) for token in tokens):
        return True
    generic = sum(1 for token in tokens if token in GENERIC_DESCRIPTOR_WORDS)
    return generic == 0 and len(tokens) >= 2


def _looks_like_generic(text: str) -> bool:
    tokens = set(common.clean_text(text).split())
    return bool(tokens & GENERIC_DESCRIPTOR_WORDS) and not _looks_like_merchant(text)

# ── engine profiles ────────────────────────────────────────────────────────
#
# Every field is deliberately machine-readable.  ``guards`` is the short form
# shown to the router; the long explanation stays in CLAUDE.md.  When engine
# code changes, edit this table and bump ``SCHEMA_VERSION``.

_PROFILE_DEFAULTS: dict[str, Any] = {
    "band": "unknown",
    "structural": False,
    "default_risk": "high",
    "normalization": "unknown",
    "match_summary": "索引未登记该引擎的匹配语义；必须读取引擎源码确认",
    "match_modes": [],
    "output_categories": None,
    "can_append_category": "unknown",
    "prior_claim_policy": "unknown",
    "prior_claim_policy_label": "未登记",
    "prior_claim_exceptions": [],
    "excluded_by_engines": [],
    "final_against_engines": [],
    "guards": ["索引未登记该引擎，必须读取源码"],
    "warnings": ["ENGINE_PROFILES 缺少该引擎"],
    "escalation": ["任何该引擎候选都必须升级为 full"],
    "layer_semantics": None,
    "table_order": [],
    "row_filters": [],
    "cleared_categories": [],
    "code_diagnosis_required": False,
}

ENGINE_PROFILES: dict[str, dict[str, Any]] = {
    "transfer": {
        "band": "first",
        "structural": True,
        "default_risk": "high",
        "normalization": "lower().strip()",
        "match_summary": (
            "外部/内部转账 regex 在小写文本上匹配；外部按 priority 升序第一匹配；"
            "内部表命中即 Internal Transfer；对手方 keyword 子串、第一匹配胜出；"
            "配对排除 priority 降序"
        ),
        "match_modes": ["transfer_regex", "transfer_counterparty"],
        "output_categories": ["Internal Transfer", "External Transfers"],
        "can_append_category": "owned_only",
        "prior_claim_policy": "first",
        "prior_claim_policy_label": "最先执行，没有更早引擎可覆盖",
        "guards": [
            "regex 必须小写（引擎先 lower().strip()）",
            "category 列不读取；输出分类由表决定",
            "对手方 keyword 是子串匹配，不是全词",
            "配对排除表 priority 降序，与规则匹配的升序相反",
        ],
        "warnings": ["transfer 的声明会被任何后续引擎的行级声明覆盖"],
        "escalation": ["任何 transfer 候选", "涉及配对排除/对手方表"],
    },
    "initial": {
        "band": "early",
        "structural": False,
        "default_risk": "low",
        "normalization": "clean_text(): 大写、仅 [A-Z0-9 ]、压缩空格",
        "match_summary": (
            "Aho-Corasick 全词；最长 keyword 胜出；同串 keyword 后行胜出"
        ),
        "match_modes": ["initial_variants"],
        "output_categories": None,
        "can_append_category": "owned_only",
        "prior_claim_policy": "overwrite_prior",
        "prior_claim_policy_label": "可覆盖更早引擎（transfer）的声明",
        "guards": [
            "keywords 必须预清洗为大写、仅 [A-Z0-9 ]、压缩空格，否则静默失配",
            "每商户最多 50 个变体，超出部分静默失效",
            "97 个 STOPWORDS 作为独立 keyword 被丢弃",
            "Financial Institutions 整行被丢弃；Financial Institutions/"
            "Debt Collection/Debt Consolidation 分类在 pipeline 中被清除",
            "最长 keyword 胜出；同串 keyword 后行胜出（loader 后行覆盖前行）",
        ],
        "warnings": ["新增商户前必须先 search 查重；品牌名/商户名才走 initial"],
        "escalation": [
            "keyword_update",
            "edit/delete 商户行",
            "同串关键词竞争",
            "分类为 Financial Institutions/Debt Collection/Debt Consolidation",
        ],
        "cleared_categories": [
            "Financial Institutions", "Debt Collection", "Debt Consolidation",
        ],
    },
    "dishonour": {
        "band": "mid",
        "structural": False,
        "default_risk": "medium",
        "normalization": "原始文本；keyword 大小写不敏感字面子串；regex IGNORECASE",
        "match_summary": (
            "keyword: re.escape 后子串匹配；regex: required_terms 全部分号 AND "
            "命中后再匹配 pattern"
        ),
        "match_modes": ["dishonour"],
        "output_categories": ["Dishonours"],
        "can_append_category": "owned_only",
        "prior_claim_policy": "overwrite_prior",
        "prior_claim_policy_label": "可覆盖更早引擎的声明",
        "guards": [
            "keyword 是子串匹配，不要求全词，收窄时注意误伤",
            "required_terms 分号分隔、AND 条件",
        ],
        "warnings": ["gambling institution 层会让位给 dishonour 认领"],
        "escalation": ["删除后可能由 liability 接管"],
    },
    "gambling": {
        "band": "mid",
        "structural": False,
        "default_risk": "medium",
        "normalization": (
            "institution: clean_text_with_channel_prefix(); rule: clean_text()"
        ),
        "match_summary": (
            "institution: 竖线变体全词最长胜出（结构性）；rule: keyword 全词/"
            "regex，最高 confidence 胜出；institution 层优先于 rule 层"
        ),
        "match_modes": ["merchant_rule"],
        "output_categories": ["Gambling"],
        "can_append_category": "owned_only",
        "prior_claim_policy": "mixed_guarded",
        "prior_claim_policy_label": "分层守卫：rule 层让位先前认领，institution 层结构性优先",
        "excluded_by_engines": ["transfer", "initial", "dishonour"],
        "final_against_engines": ["income", "liability"],
        "guards": [
            "institution 层优先于 rule 层（结构性 inst_win）",
            "institution 命中后让位给 fee/dishonour 认领，以及 initial 的等长/更长 keyword",
            "rule 层走 exclude_prior_claimed：对 transfer/initial/dishonour 认领让位",
            "认领对 income/liability 终局：orchestrator 丢弃已认领行上的 income/liability 预测",
            "商户名已命中 merchant_kb 时，往 rule 层加规则没有效果，必须走 institution 层",
            "institution 行的 match_type/confidence 列不被读取",
        ],
        "warnings": [
            "赌博平台/商户名走 institution 行；BET/CASINO/LOTTERY 等通用词才走 rule 行",
        ],
        "escalation": ["任何 gambling 候选", "商户名 vs 通用词归属", "商户已在 merchant_kb"],
        "layer_semantics": {
            "institution": {
                "source": "institution",
                "matches": "竖线分隔商户变体，clean_text_with_channel_prefix 后全词最长",
                "wins": "结构性优先于 rule 层",
                "yields_to": ["fee", "dishonour"],
                "yields_to_initial_if": "initial keyword 等长或更长",
            },
            "rule": {
                "source": "rule",
                "matches": "clean_text 后 keyword 全词/regex，最高 confidence",
                "exclude_prior_claimed": ["transfer", "initial", "dishonour"],
                "final_against": ["income", "liability"],
            },
        },
    },
    "income": {
        "band": "mid",
        "structural": True,
        "default_risk": "high",
        "normalization": "clean_text_with_seams(): 大写 + 数字/字母接缝插空格",
        "match_summary": (
            "regex + 金额阈值 + payer_key + 频率/稳定性行为特征；"
            "pattern_group 决定语义，部分组只是门控/排除"
        ),
        "match_modes": ["income_regex"],
        "output_categories": ["Wages", "Centrelink"],
        "can_append_category": "via_pattern_group",
        "prior_claim_policy": "overwrite_prior",
        "prior_claim_policy_label": "可覆盖更早引擎，但对 gambling 已认领行终局让位",
        "final_against_engines": [],
        "excluded_by_engines": ["gambling"],
        "guards": [
            "金额 < $100 除非有工资历史或小额强关键词 [50,100)，否则不判工资；gig 路径不受门控",
            "pattern_group 决定语义：strong_wage/medium_income/centrelink/"
            "self_employed_gig/hard_negative/soft_negative/transfer_from/pay_signal/"
            "gig_*/behavior_exclusion",
            "medium_income 需配合行为特征；hard/soft negative 会挡住 SALARY 关键词",
            "transfer_from 是豁免信号，不是排除",
            "liability 运行前排除 Wages/Centrelink 行",
        ],
        "warnings": ["新增 strong_wage 模式最安全；其他组需要行为特征配合"],
        "escalation": ["任何 income 候选", "新增 strong_wage 以外的组", "金额/行为特征相关"],
    },
    "liability": {
        "band": "mid",
        "structural": True,
        "default_risk": "high",
        "normalization": (
            "counterparty: upper().strip(); Format A/B: 原始文本 + IGNORECASE; "
            "credit_card: regex IGNORECASE"
        ),
        "match_summary": (
            "多文件顺序执行：counterparty → home/car → credit_card → dishonours → "
            "special → overdrawn → debt_collection → debt_consolidation → streams → "
            "bscat → generic_loan；先认领者胜出"
        ),
        "match_modes": [
            "liability_counterparty", "liability_homecar", "liability_credit_card",
            "liability_dishonour", "raw_regex_ci", "word_boundary_ci",
        ],
        "output_categories": None,
        "can_append_category": "via_product_type",
        "prior_claim_policy": "overwrite_prior_except",
        "prior_claim_policy_label": "可覆盖更早引擎，但 income 已认领的 Wages/Centrelink 行被剔除",
        "prior_claim_exceptions": [
            {"engines": ["income"], "categories": ["Wages", "Centrelink"]},
        ],
        "excluded_by_engines": ["income"],
        "guards": [
            "执行顺序：counterparty → home/car → credit_card → dishonours → special → "
            "overdrawn → debt_collection → debt_consolidation → streams → bscat → "
            "generic_loan",
            "counterparty keyword 边界 (?<![A-Za-z])...(?!A-Za-z])，不是 \\b，数字可穿透",
            "counterparty loader 只读 rule_type 列，但 CSV 表头实际是 match_type；"
            "加 regex 规则必须新增 rule_type 列，否则 regex 被当 keyword",
            "credit_card 的 keyword 列实际是 regex；已有 product_type 的行整个被排除，"
            "只填充未认领行（不是覆写模式）",
            "home_loan_car_loan 是 Format A 12 列；match_scope/amount_gt 是门控，"
            "keyword 约束被丢弃",
            "Format B 三文件列各异；overdrawn 用 pattern regex",
            "product_type 只能取目标表已有值，决定最终 bscat；loan_NNN 只是编号",
        ],
        "warnings": [
            "liability 的分类由 product_type/bank/stream 优先级推导，规则行的 category 不一定是最终分类",
        ],
        "escalation": [
            "任何 liability 候选", "product_type 不确定", "选表不确定",
            "credit_card/counterparty 表",
        ],
        "table_order": [
            "liability_engine/resources/counterparty_keyword_rules.csv",
            "liability_engine/resources/home_loan_car_loan_rules.csv",
            "liability_engine/resources/credit_card_rules.csv",
            "liability_engine/resources/dishonours_rules.csv",
            "liability_engine/resources/overdrawn_rules.csv",
            "liability_engine/resources/debt_collection_rules.csv",
            "liability_engine/resources/debt_consolidation_rules.csv",
        ],
    },
    "all_other_credit": {
        "band": "mid",
        "structural": False,
        "default_risk": "low",
        "normalization": "原始文本；大小写不敏感",
        "match_summary": (
            "仅 keyword: re.escape 后子串；只处理 dr_cr=credit；"
            "regex 未实现，required_terms 被忽略"
        ),
        "match_modes": ["all_other_credit"],
        "output_categories": ["All Other Credits"],
        "can_append_category": "owned_only",
        "prior_claim_policy": "unclassified_or_external_transfers",
        "prior_claim_policy_label": "只处理未认领行；External Transfers 例外可覆盖",
        "guards": [
            "只处理入账（dr_cr=credit）",
            "只支持 keyword 模式；regex 写了也不会匹配，required_terms 被忽略",
            "允许覆盖 External Transfers",
            "排除所有其他已有声明",
        ],
        "warnings": ["只生成 keyword 规则；不要写 regex"],
        "escalation": ["非 credit 行", "新增 regex", "删除/收窄后 transfer 接管"],
    },
    "fee": {
        "band": "mid",
        "structural": False,
        "default_risk": "low",
        "normalization": "原始文本、压缩空格、IGNORECASE",
        "match_summary": (
            "regex IGNORECASE；priority 升序第一匹配；"
            "unclassified_only/dr_cr/zero_amount_reject 在匹配后过滤"
        ),
        "match_modes": ["fee_regex"],
        "output_categories": ["Fees", "Overdrawn"],
        "can_append_category": "owned_only",
        "prior_claim_policy": "overwrite_prior",
        "prior_claim_policy_label": "可覆盖更早引擎；unclassified_only 行例外",
        "guards": [
            "category 仅 fee（→Fees）或 Overdrawn",
            "Overdrawn 规则 priority 必须小于通用费用规则",
            "pattern 通常 ^ 锚定",
            "unclassified_only 行只处理未认领行；fee_keyword_catchall 即此类",
        ],
        "warnings": ["新增规则要设置合适 priority；大小写不敏感"],
        "escalation": ["Overdrawn 优先级调整", "unclassified_only 行", "删除后 rent/catch_all 接管"],
        "row_filters": ["unclassified_only", "dr_cr", "zero_amount_reject"],
    },
    "rent": {
        "band": "late",
        "structural": False,
        "default_risk": "medium",
        "normalization": (
            "institution: clean_text_with_channel_prefix(); rule: clean_text()"
        ),
        "match_summary": (
            "institution: 竖线变体全词最长、结构性优先于 rule；"
            "rule: keyword 全词/regex，最高 confidence"
        ),
        "match_modes": ["merchant_rule"],
        "output_categories": ["Rent"],
        "can_append_category": "owned_only",
        "prior_claim_policy": "mixed_guarded",
        "prior_claim_policy_label": "institution 层结构性优先；income/liability 已认领行被剔除",
        "excluded_by_engines": ["income", "liability"],
        "guards": [
            "institution 层命中优先于 rule 层（结构性 inst_win），提高 confidence 也没用",
            "orchestrator 在 rent 前排除已被 income/liability 认领的行",
            "institution 行的 confidence 列不被读取",
            "institution 层也有 50 变体上限，超出部分静默失效",
            "REAL ESTATE 类全词规则会漏掉无空格拼写（REAL ESTATEAGENT）",
        ],
        "warnings": ["物业机构/商户名走 institution 行；通用租金词走 rule 行"],
        "escalation": ["institution vs rule 层", "商户名 vs 通用词", "收入/负债已认领行"],
        "layer_semantics": {
            "institution": {
                "source": "institution",
                "matches": "竖线分隔机构变体，clean_text_with_channel_prefix 后全词最长",
                "wins": "结构性优先于 rule 层",
                "blocked_by": ["income", "liability"],
            },
            "rule": {
                "source": "rule",
                "matches": "clean_text 后 keyword 全词/regex，最高 confidence",
                "beaten_by": "任何 institution 命中",
            },
        },
    },
    "catch_all": {
        "band": "last",
        "structural": False,
        "default_risk": "low",
        "normalization": "clean_text(): 大写、仅 [A-Z0-9 ]、压缩空格",
        "match_summary": (
            "keyword 全词/regex；最高 confidence 胜出（不是第一匹配）；"
            "exclude_prior_claimed 只接未分类行"
        ),
        "match_modes": ["merchant_rule"],
        "output_categories": None,
        "can_append_category": "owned_only",
        "prior_claim_policy": "unclassified_only",
        "prior_claim_policy_label": "只匹配尚无任何声明的行；不覆盖已认领行",
        "guards": [
            "只匹配尚无任何声明的行（exclude_prior_claimed）",
            "只收通用描述词，非商户名/品牌名/平台名",
            "最高 confidence 胜出，不是第一匹配",
            "confidence 建议 0.55–0.90",
        ],
        "warnings": ["品牌名/平台名不属于 catch_all；应走 initial 或专属引擎"],
        "escalation": ["商户名/品牌名误入", "多引擎已有声明"],
    },
}

GENERIC_PROFILE = dict(_PROFILE_DEFAULTS)


def _profile(engine: str) -> dict[str, Any]:
    profile = dict(_PROFILE_DEFAULTS)
    profile.update(ENGINE_PROFILES.get(engine, {}))
    return profile


# ── cross-engine override rules ────────────────────────────────────────────
#
# ``effect`` values:
#   later_overwrites  -- normal row-level replacement by a later engine;
#   row_level_override-- a later engine replaces the claim when its rule fires;
#   from_engine_wins  -- the earlier engine's claim is final;
#   prior_claim_blocks-- the from engine's existing claim prevents this rule;
#   conditional       -- only under the stated condition;
#   within_engine     -- precedence inside one engine, not a cross-engine edge.

OVERRIDE_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "later_engine_overwrites_earlier",
        "from_engines": ["*"],
        "to_engines": ["*"],
        "effect": "later_overwrites",
        "condition": "later engine emits a claim on the same row",
        "description": (
            "后执行的引擎行级覆盖先执行的引擎（bscat + counterparty 成对替换），"
            "不管 confidence 高低"
        ),
        "source": "CLAUDE.md 跨引擎覆盖规则 1",
        "severity": "baseline",
    },
    {
        "id": "gambling_final_vs_income_liability",
        "from_engines": ["gambling"],
        "to_engines": ["income", "liability"],
        "effect": "from_engine_wins",
        "condition": "gambling 已认领该行",
        "description": (
            "orchestrator 在 commit 前丢弃 income/liability 在 gambling 已认领行上的预测；"
            "赌客派彩不会被重打成 Wages"
        ),
        "source": "CLAUDE.md 跨引擎覆盖规则 2",
        "severity": "special",
    },
    {
        "id": "gambling_rule_yields_to_prior",
        "from_engines": ["transfer", "initial", "dishonour"],
        "to_engines": ["gambling"],
        "effect": "prior_claim_blocks",
        "condition": "候选写 rule 层（source=rule）且该行已被 from 引擎认领",
        "target_layer": "rule",
        "target_tables": ["gambling_engine/resources/gambling_rules.csv"],
        "description": "gambling 的 rule 层走 exclude_prior_claimed，对 transfer/initial/dishonour 让位",
        "source": "CLAUDE.md 跨引擎覆盖规则 3",
        "severity": "special",
    },
    {
        "id": "gambling_institution_yields",
        "from_engines": ["fee", "dishonour", "initial"],
        "to_engines": ["gambling"],
        "effect": "conditional",
        "condition": (
            "institution 层；fee/dishonour 已认领，或 initial 有等长/更长 keyword"
        ),
        "target_layer": "institution",
        "target_tables": ["gambling_engine/resources/gambling_rules.csv"],
        "description": "gambling institution 层命中后让位给 fee/dishonour 与等长/更长 initial keyword",
        "source": "CLAUDE.md gambling 章节",
        "severity": "special",
    },
    {
        "id": "income_blocks_liability",
        "from_engines": ["income"],
        "to_engines": ["liability"],
        "effect": "prior_claim_blocks",
        "condition": "行分类为 Wages/Centrelink；orchestrator 在 liability 前剔除",
        "applies_to_current_categories": ["Wages", "Centrelink"],
        "description": "income → liability：liability 运行前排除 Wages/Centrelink 行",
        "source": "CLAUDE.md 跨引擎覆盖规则 5",
        "severity": "special",
    },
    {
        "id": "income_liability_block_rent",
        "from_engines": ["income", "liability"],
        "to_engines": ["rent"],
        "effect": "prior_claim_blocks",
        "condition": "该行已被 income/liability 认领",
        "description": "income/liability → rent：rent 运行前排除已认领行",
        "source": "CLAUDE.md 跨引擎覆盖规则 6",
        "severity": "special",
    },
    {
        "id": "transfer_overridden_by_all_other_credit",
        "from_engines": ["transfer"],
        "to_engines": ["all_other_credit"],
        "effect": "conditional",
        "condition": "dr_cr=credit 且分类为 External Transfers",
        "applies_to_categories": ["External Transfers"],
        "description": "transfer → all_other_credit：all_other_credit 可覆盖 External Transfers",
        "source": "CLAUDE.md 跨引擎覆盖规则 7",
        "severity": "special",
    },
    {
        "id": "catch_all_requires_unclassified",
        "from_engines": ["*"],
        "to_engines": ["catch_all"],
        "effect": "prior_claim_blocks",
        "condition": "任何更早引擎已认领该行",
        "description": "catch_all 走 exclude_prior_claimed，只匹配尚无声明的行",
        "source": "CLAUDE.md 跨引擎覆盖规则 8",
        "severity": "guard",
    },
    {
        "id": "initial_cleared_categories",
        "from_engines": ["initial"],
        "to_engines": ["liability", "dishonour"],
        "effect": "from_engine_dropped",
        "condition": "initial 规则分类为 Financial Institutions/Debt Collection/Debt Consolidation",
        "applies_to_categories": [
            "Financial Institutions", "Debt Collection", "Debt Consolidation",
        ],
        "description": "initial 的这些分类在 pipeline 中被清除，由 liability/dishonour 兜底",
        "source": "CLAUDE.md 跨引擎覆盖规则 4",
        "severity": "guard",
    },
    {
        "id": "fee_unclassified_only_rows",
        "from_engines": ["*"],
        "to_engines": ["fee"],
        "effect": "conditional",
        "condition": "fee 规则行 unclassified_only=true 时只处理未认领行",
        "description": "fee 的 unclassified_only 行会被更早的归属声明挡住",
        "source": "CLAUDE.md fee 章节",
        "severity": "guard",
    },
    {
        "id": "gambling_institution_beats_rule",
        "from_engines": ["gambling"],
        "to_engines": ["gambling"],
        "effect": "within_engine",
        "condition": "institution 层与 rule 层同时命中",
        "target_tables": ["gambling_engine/resources/gambling_rules.csv"],
        "description": "gambling institution 层结构性优先于 rule 层",
        "source": "CLAUDE.md gambling 章节",
        "severity": "guard",
    },
    {
        "id": "rent_institution_beats_rule",
        "from_engines": ["rent"],
        "to_engines": ["rent"],
        "effect": "within_engine",
        "condition": "institution 层与 rule 层同时命中",
        "target_tables": ["rent_engine/resources/rent_rules.csv"],
        "description": "rent institution 层结构性优先于 rule 层",
        "source": "CLAUDE.md rent 章节",
        "severity": "guard",
    },
    {
        "id": "liability_credit_card_unclaimed_only",
        "from_engines": ["liability"],
        "to_engines": ["liability"],
        "effect": "within_engine",
        "condition": "credit_card_rules 只填充未认领行",
        "target_tables": ["liability_engine/resources/credit_card_rules.csv"],
        "description": "liability credit_card 规则不是覆写模式，只填充未认领行",
        "source": "CLAUDE.md liability 章节",
        "severity": "guard",
    },
)


# ── category → engine preferences ──────────────────────────────────────────
#
# The catalog's owner list is an ownership statement, not an execution
# preference.  Some multi-owner categories have a clearly better engine; the
# router returns this as a hint and always lists every owner.

CATEGORY_ENGINE_PREFERENCE: dict[str, list[str]] = {
    "Gambling": ["gambling", "initial"],
    "Rent": ["rent", "initial"],
    "Dishonours": ["dishonour", "liability"],
    "Overdrawn": ["fee", "liability"],
    "Retail": ["initial", "liability", "catch_all"],
}

# Default table hint per engine.  Liability and transfer are category-aware.
DEFAULT_TABLE_HINTS: dict[str, Any] = {
    "transfer": {
        "Internal Transfer": (
            "transfer_engine/resources/transfer_internal_regex_rules.csv"
        ),
        "_default": (
            "transfer_engine/resources/transfer_external_high_confidence_rules.csv"
        ),
    },
    "initial": "initial_engine/merchant_kb.csv",
    "dishonour": "dishonour_engine/resources/dishonour_rules.csv",
    "gambling": "gambling_engine/resources/gambling_rules.csv",
    "income": "income_engine/resources/income_pattern_rules.csv",
    "liability": {
        "Debt Collection": "liability_engine/resources/debt_collection_rules.csv",
        "Debt Consolidation": "liability_engine/resources/debt_consolidation_rules.csv",
        "Overdrawn": "liability_engine/resources/overdrawn_rules.csv",
        "Dishonours": "liability_engine/resources/dishonours_rules.csv",
        "Credit Card Repayments": "liability_engine/resources/credit_card_rules.csv",
        "_default": "liability_engine/resources/counterparty_keyword_rules.csv",
    },
    "all_other_credit": (
        "all_other_credit_engine/resources/all_other_credit_rules.csv"
    ),
    "fee": "fee_engine/resources/fee_classification_rules.csv",
    "rent": "rent_engine/resources/rent_rules.csv",
    "catch_all": "catch_all_engine/resources/catch_all_rules.csv",
}


# ── index building ─────────────────────────────────────────────────────────

def read_header(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle), [])


def _schema_warnings(spec: common.RuleFileSpec, columns: list[str]) -> list[str]:
    if not columns:
        return []
    required = [spec.pattern_column]
    for extra in (spec.category_column, spec.rule_id_column, spec.row_mode_column):
        if extra:
            required.append(extra)
    if spec.match_mode == "initial_variants":
        required.extend(["merchant_name", "keywords", "category"])
    missing = [name for name in dict.fromkeys(required) if name not in columns]
    if not missing:
        return []
    return [
        f"{spec.relative_path} 表头缺少索引登记的列 {missing}；"
        "规则表 schema 可能已变化，必须重新核对"
    ]


def _table_entry(
    repo: Path,
    spec: common.RuleFileSpec,
    priority: int,
) -> dict[str, Any]:
    path = repo / spec.relative_path
    columns = read_header(path)
    return {
        "engine": spec.engine,
        "target_file": spec.relative_path,
        "priority": priority,
        "exists": path.exists(),
        "columns": columns,
        "pattern_column": spec.pattern_column,
        "match_mode": spec.match_mode,
        "category_column": spec.category_column or "",
        "rule_id_column": spec.rule_id_column or "",
        "implicit_category": spec.implicit_category or "",
        "row_mode_column": spec.row_mode_column or "",
        "appendable": spec.appendable,
        "is_matcher": spec.is_matcher,
        "role": common.table_role(spec),
        "schema_warnings": _schema_warnings(spec, columns),
    }


def _enabled_pipeline(repo: Path) -> tuple[dict[str, int], set[str], list[str], dict[str, Any]]:
    payload = common.load_pipeline(repo)
    priorities: dict[str, int] = {}
    enabled: set[str] = set()
    order: list[str] = []
    for engine in payload.get("pipeline", []):
        engine_id = str(engine.get("engine_id", "")).strip()
        if not engine_id:
            continue
        priorities[engine_id] = int(engine.get("priority", 500))
        if engine.get("enabled", True):
            enabled.add(engine_id)
            order.append(engine_id)
    order.sort(key=lambda engine_id: priorities.get(engine_id, 9999))
    return priorities, enabled, order, payload


def _default_table_hint(engine: str, category: str) -> str:
    hint = DEFAULT_TABLE_HINTS.get(engine, "")
    if isinstance(hint, dict):
        return str(hint.get(category) or hint.get("_default") or "")
    return str(hint or "")


def _recommended_engine(
    category: str,
    owners: list[str],
    enabled: set[str],
    explicit: str = "",
) -> str:
    available = [owner for owner in owners if owner in enabled]
    if explicit and explicit in available:
        return explicit
    for candidate in CATEGORY_ENGINE_PREFERENCE.get(category, []):
        if candidate in available:
            return candidate
    if available:
        return available[0]
    return explicit or (owners[0] if owners else "")


def _category_escalation(
    category: str,
    owners: list[str],
    enabled_owners: list[str],
) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    if not owners:
        reasons.append({
            "id": "catalog_missing",
            "tier": "full",
            "reason": f"分类 {category!r} 在 category_catalog.json 中没有归属引擎",
        })
        return reasons
    if not enabled_owners:
        reasons.append({
            "id": "owner_disabled",
            "tier": "full",
            "reason": f"分类 {category!r} 的归属引擎 {owners} 全部未启用",
        })
        return reasons
    structural = [owner for owner in enabled_owners if owner in STRUCTURAL_ENGINES]
    if structural:
        reasons.append({
            "id": "structural_owner",
            "tier": "full",
            "reason": f"分类 {category!r} 的归属引擎包含结构性引擎 {structural}",
        })
    if len(enabled_owners) > 1:
        reasons.append({
            "id": "owner_ambiguity",
            "tier": "local",
            "reason": f"分类 {category!r} 有多个启用归属引擎 {enabled_owners}；必须按证据选最窄归属",
        })
    if category in {"Gambling", "Rent"}:
        reasons.append({
            "id": "layer_ambiguity",
            "tier": "local",
            "reason": f"{category} 是双层引擎；必须先定 institution 还是 rule 层",
        })
    if category in {"Debt Collection", "Debt Consolidation", "Dishonours", "Overdrawn"}:
        reasons.append({
            "id": "multi_table_owner",
            "tier": "local",
            "reason": f"{category} 在 liability/dishonour/fee 之间有多张表，必须按证据选表",
        })
    if "initial" in enabled_owners and category in {
        "Financial Institutions", "Debt Collection", "Debt Consolidation",
    }:
        reasons.append({
            "id": "initial_cleared_category",
            "tier": "local",
            "reason": f"initial 的 {category} 分类会被 pipeline 清除，不能靠 initial 追加",
        })
    return reasons


def build_index(repo: Path) -> dict[str, Any]:
    """Merge code-level profiles with the repository's live configuration."""
    repo = repo.expanduser().resolve()
    if not (repo / "configs" / "pipeline.json").exists():
        raise SystemExit(f"不是有效的 AUS_BS_CAT_Model 仓库: {repo}")

    priorities, enabled, order, _pipeline = _enabled_pipeline(repo)
    catalog = common.load_catalog(repo)
    warnings: list[str] = []

    tables = [
        _table_entry(repo, spec, priorities.get(spec.engine, 9999))
        for spec in common.RULE_FILES
    ]
    for table in tables:
        warnings.extend(table["schema_warnings"])
        if not table["exists"]:
            warnings.append(f"规则表不存在: {table['target_file']}")

    engines: OrderedDict[str, dict[str, Any]] = OrderedDict()
    engine_ids = list(order)
    for engine_id in sorted({spec.engine for spec in common.RULE_FILES}):
        if engine_id not in engine_ids:
            engine_ids.append(engine_id)
    for engine_id in ENGINE_PROFILES:
        if engine_id not in engine_ids:
            engine_ids.append(engine_id)

    for engine_id in engine_ids:
        profile = _profile(engine_id)
        rank = order.index(engine_id) if engine_id in order else None
        engines[engine_id] = {
            "engine_id": engine_id,
            "priority": priorities.get(engine_id, 9999),
            "enabled": engine_id in enabled,
            "execution_rank": (rank + 1) if rank is not None else None,
            "band": profile["band"],
            "structural": profile["structural"],
            "default_risk": profile["default_risk"],
            "normalization": profile["normalization"],
            "match_summary": profile["match_summary"],
            "match_modes": profile["match_modes"],
            "output_categories": profile["output_categories"],
            "can_append_category": profile["can_append_category"],
            "prior_claim_policy": profile["prior_claim_policy"],
            "prior_claim_policy_label": profile["prior_claim_policy_label"],
            "prior_claim_exceptions": profile["prior_claim_exceptions"],
            "excluded_by_engines": profile["excluded_by_engines"],
            "final_against_engines": profile["final_against_engines"],
            "guards": profile["guards"],
            "warnings": profile["warnings"],
            "escalation": profile["escalation"],
            "layer_semantics": profile["layer_semantics"],
            "table_order": profile["table_order"],
            "row_filters": profile["row_filters"],
            "cleared_categories": profile["cleared_categories"],
            "code_diagnosis_required": profile["code_diagnosis_required"],
            "default_target_file": _default_table_hint(engine_id, ""),
            "tables": [table for table in tables if table["engine"] == engine_id],
        }
        if engine_id not in ENGINE_PROFILES:
            warnings.append(
                f"pipeline/RULE_FILES 中的引擎 {engine_id!r} 没有 ENGINE_PROFILES 登记；"
                "任何相关候选都会升级为 full"
            )

    categories: OrderedDict[str, dict[str, Any]] = OrderedDict()
    raw_categories = catalog.get("categories", {})
    for category, info in raw_categories.items():
        if not isinstance(info, dict):
            continue
        if info.get("active") is False:
            continue
        raw = str(info.get("owner_engine_id", ""))
        owners = [part.strip() for part in raw.split(",") if part.strip()]
        enabled_owners = [owner for owner in owners if owner in enabled]
        recommended = _recommended_engine(category, owners, enabled)
        escalation = _category_escalation(category, owners, enabled_owners)
        for owner in owners:
            if owner not in engines:
                warnings.append(
                    f"分类 {category!r} 的归属引擎 {owner!r} 不在 pipeline/RULE_FILES 中"
                )
        category_tables = []
        for owner in enabled_owners:
            for table in engines[owner]["tables"]:
                if not table["is_matcher"]:
                    continue
                category_tables.append({
                    "engine": owner,
                    "target_file": table["target_file"],
                    "appendable": table["appendable"],
                    "match_mode": table["match_mode"],
                    "is_default": table["target_file"] == _default_table_hint(owner, category),
                })
        categories[category] = {
            "category": category,
            "active": True,
            "owners": owners,
            "enabled_owners": enabled_owners,
            "owner_priorities": {
                owner: priorities.get(owner) for owner in owners
            },
            "recommended_engine": recommended,
            "tables": category_tables,
            "appendable": any(item["appendable"] for item in category_tables),
            "escalation": escalation,
        }

    for category, entry in categories.items():
        if not entry["owners"]:
            warnings.append(f"分类 {category!r} 没有 owner_engine_id")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "source": {
            "rule_files": "scripts/_internal/common.py:RULE_FILES",
            "engine_profiles": "scripts/_internal/rule_index.py:ENGINE_PROFILES",
            "override_rules": "scripts/_internal/rule_index.py:OVERRIDE_RULES",
            "pipeline": "configs/pipeline.json",
            "catalog": "configs/category_catalog.json",
            "table_headers": "runtime read",
        },
        "execution_order": order,
        "priorities": priorities,
        "enabled_engines": sorted(enabled),
        "engines": engines,
        "categories": categories,
        "override_rules": list(OVERRIDE_RULES),
        "escalation_policy": escalation_policy(),
        "index_warnings": warnings,
    }


def escalation_policy() -> dict[str, Any]:
    return {
        "tiers": {
            "target": "只验证目标聚类行 + 命中新/旧 pattern 的样本行；不做全局 baseline",
            "local": "目标行 + 同引擎/同期望分类的局部冲突行；不做全局 baseline",
            "full": "全部样本行 + 全部引擎 claim + baseline.py diff",
        },
        "full_chain_triggers": [
            {"id": "structural_engine", "when": "target engine in ['income','liability','transfer']"},
            {"id": "config_operation", "when": "operation in ['priority','catalog']"},
            {"id": "cross_engine_guard", "when": "routing.status in ['blocked_guard','overwritten_by_later','owner_disabled','catalog_missing']"},
            {"id": "delete_takeover_unknown", "when": "delete 且后续引擎可能接管（存在 row-level overrider）"},
            {"id": "wide_sample_impact", "when": "affected rows > 20"},
            {"id": "owner_ambiguity", "when": "多个启用归属引擎且未确定最窄归属"},
            {"id": "missing_target_table", "when": "目标表不存在或表头与索引登记不一致"},
            {"id": "index_drift", "when": "index_warnings 涉及目标引擎/表"},
        ],
        "local_validation_triggers": [
            {"id": "edit_delete_keyword_update", "when": "operation in ['edit','delete','keyword_update']"},
            {"id": "diff_or_extra", "when": "problem_type in ['diff','extra']"},
            {"id": "later_engine_override", "when": "存在 row-level 或 conditional overrider"},
            {"id": "medium_sample_impact", "when": "6 <= affected rows <= 20"},
            {"id": "multi_owner_category", "when": "目标分类有多个启用归属引擎"},
            {"id": "unresolved_layer", "when": "gambling/rent 未指定 institution 或 rule 层"},
        ],
        "target_validation_triggers": [
            {"id": "clean_append", "when": "append + 单一非结构性引擎 + 无覆盖风险 + 影响行 <= 5"},
        ],
        "never_skippable": [
            "阶段四校验", "阶段五对话审批", "dry-run", "写入前备份",
            "写后真实流水线回归", "回滚能力",
        ],
    }


# ── route helpers ──────────────────────────────────────────────────────────

def _norm(value: Any) -> str:
    return "" if common.is_missing(value) else str(value).strip()


_ENGINE_SENTINELS = {
    "unclassified", "none", "null", "n/a", "na", "-", "unknown",
}


def _norm_engine(value: Any) -> str:
    """Return a real engine id; pipeline output uses ``unclassified`` as a sentinel."""
    text = _norm(value)
    return "" if text.lower() in _ENGINE_SENTINELS else text


def _split(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _engine_rank(index: dict[str, Any], engine: str) -> int | None:
    return index["engines"].get(engine, {}).get("execution_rank")


def _priority(index: dict[str, Any], engine: str) -> int:
    return int(index["engines"].get(engine, {}).get("priority", 9999))


def _later_engines(index: dict[str, Any], engine: str) -> list[str]:
    order = index["execution_order"]
    if engine not in order:
        return []
    return order[order.index(engine) + 1 :]


def _earlier_engines(index: dict[str, Any], engine: str) -> list[str]:
    order = index["execution_order"]
    if engine not in order:
        return []
    return order[: order.index(engine)]


def _layer_hint(engine: str, text: str) -> str:
    upper = common.clean_text(text)
    if not upper:
        return ""
    if engine == "gambling":
        words = set(upper.split())
        return "rule" if words & GENERIC_GAMBLING_WORDS else "institution"
    if engine == "rent":
        words = set(upper.split())
        return "rule" if words & GENERIC_RENT_WORDS else "institution"
    return ""


def _table_candidates(
    index: dict[str, Any],
    engine: str,
    category: str,
) -> list[dict[str, Any]]:
    entry = index["engines"].get(engine)
    if not entry:
        return []
    hint = _default_table_hint(engine, category)
    tables = []
    for table in entry["tables"]:
        if not table["is_matcher"]:
            continue
        score = 0
        if table["target_file"] == hint:
            score += 10
        if table["implicit_category"] and table["implicit_category"] == category:
            score += 5
        if table["category_column"]:
            score += 2
        if table["appendable"]:
            score += 1
        item = dict(table)
        item["is_default_hint"] = table["target_file"] == hint
        item["score"] = score
        tables.append(item)
    tables.sort(key=lambda item: (-item["score"], item["target_file"]))
    return tables


def _engine_can_output(
    index: dict[str, Any],
    engine: str,
    category: str,
) -> bool:
    """Whether this engine can plausibly emit the category on this row.

    Catalog ownership is authoritative.  A later engine that cannot emit the
    target category is not an overrider for this task, even though it executes
    later; listing it as a risk would push almost every clean append to T2.
    """
    if not category:
        return True
    info = index["categories"].get(category)
    if info:
        return engine in (info.get("owners") or [])
    profile = index["engines"].get(engine, {})
    outputs = profile.get("output_categories")
    if outputs is None:
        return True
    return category in outputs


def _prior_claim_effect(
    index: dict[str, Any],
    target_engine: str,
    target_category: str,
    target_layer: str,
    later_engine: str,
) -> dict[str, Any] | None:
    profile = index["engines"].get(later_engine, {})
    policy = str(profile.get("prior_claim_policy", "unknown"))
    base = {
        "engine": later_engine,
        "priority": profile.get("priority"),
        "execution_rank": profile.get("execution_rank"),
        "policy": policy,
        "policy_label": profile.get("prior_claim_policy_label", ""),
    }
    if policy == "first":
        return None
    if not _engine_can_output(index, later_engine, target_category):
        return {
            **base,
            "effect": "unrelated_category",
            "risk": "low",
            "condition": f"该引擎不产出分类 {target_category!r}，不是本任务的覆盖者",
        }
    if policy == "overwrite_prior":
        return {
            **base,
            "effect": "row_level_override",
            "risk": "high",
            "condition": "目标行同时命中该引擎的规则",
        }
    if policy == "overwrite_prior_except":
        for exception in profile.get("prior_claim_exceptions") or []:
            if (
                target_engine in (exception.get("engines") or [])
                or target_category in (exception.get("categories") or [])
            ):
                return {
                    **base,
                    "effect": "blocked",
                    "risk": "low",
                    "condition": exception.get("condition")
                    or "目标分类被该引擎的排除规则挡住",
                }
        return {
            **base,
            "effect": "row_level_override",
            "risk": "high",
            "condition": "目标行同时命中该引擎的规则（未触发排除例外）",
        }
    if policy == "mixed_guarded":
        return {
            **base,
            "effect": "conditional",
            "risk": "medium",
            "condition": "由该引擎的层级守卫决定；见 guards/layer_semantics",
        }
    if policy == "unclassified_or_external_transfers":
        if target_category == "External Transfers":
            return {
                **base,
                "effect": "row_level_override",
                "risk": "high",
                "condition": "External Transfers 例外：all_other_credit 可覆盖",
            }
        return {
            **base,
            "effect": "only_unclassified",
            "risk": "low",
            "condition": "目标行必须未被更早引擎认领（External Transfers 例外）",
        }
    if policy == "unclassified_only":
        return {
            **base,
            "effect": "not_an_override",
            "risk": "low",
            "condition": "目标规则命中时该引擎不能再认领；只影响未分类行",
        }
    return {
        **base,
        "effect": "unknown",
        "risk": "high",
        "condition": "索引未登记该引擎的 prior_claim_policy",
    }


def _special_edges(
    index: dict[str, Any],
    target_engine: str,
    target_category: str,
    target_layer: str,
    target_file: str,
    *,
    incoming: bool,
) -> list[dict[str, Any]]:
    results = []
    for rule in index["override_rules"]:
        if rule["effect"] == "later_overwrites":
            continue
        from_engines = rule["from_engines"]
        to_engines = rule["to_engines"]
        if incoming:
            if target_engine not in to_engines:
                continue
        else:
            if target_engine not in from_engines:
                continue
        applies = rule.get("applies_to_categories")
        if applies and target_category not in applies:
            continue
        current_applies = rule.get("applies_to_current_categories")
        # Callers only pass the target category; current-category edges are
        # annotated but not filtered here.
        if rule.get("target_layer") and target_layer:
            if rule["target_layer"] != target_layer:
                continue
        target_tables = rule.get("target_tables")
        if target_tables and target_file and target_file not in target_tables:
            continue
        results.append(dict(rule, applies_to_current_categories=current_applies))
    return results


def _default_operation(problem_type: str) -> str:
    if problem_type == "miss":
        return "append"
    if problem_type == "extra":
        return "delete"
    if problem_type == "diff":
        return "edit"
    return "append"


def _tier_max(left: str, right: str) -> str:
    return left if TIER_ORDER.get(left, 0) >= TIER_ORDER.get(right, 0) else right


def _tier_rank(tier: str) -> int:
    return TIER_ORDER.get(tier, 0)


def _append_analysis(
    index: dict[str, Any],
    engine: str,
    category: str,
    problem_type: str,
    operation: str,
    tables: list[dict[str, Any]],
    layer: str,
    text: str,
) -> tuple[bool, list[str], list[str], dict[str, Any]]:
    """Return (can_append, blockers, caveats, chosen_table)."""
    blockers: list[str] = []
    caveats: list[str] = []
    chosen = tables[0] if tables else {}
    profile = index["engines"].get(engine, {})

    if problem_type == "extra":
        blockers.append("多识别问题不能追加分类规则；必须删除或收窄当前规则")
    if operation not in {"append", "auto"}:
        caveats.append(f"当前操作是 {operation}，追加可行性仅供参考")
    if not chosen:
        blockers.append(f"引擎 {engine!r} 没有已登记的分类器表")
        return False, blockers, caveats, chosen
    if not chosen.get("appendable"):
        blockers.append(f"{chosen.get('target_file')} 不是可追加的分类器表")
    if not chosen.get("is_matcher"):
        blockers.append(f"{chosen.get('target_file')} 不是分类规则表")
    if chosen.get("schema_warnings"):
        blockers.extend(chosen["schema_warnings"])

    category_info = index["categories"].get(category, {}) if category else {}
    owners = category_info.get("owners") or []
    if category and owners and engine not in owners:
        blockers.append(
            f"引擎 {engine!r} 不拥有分类 {category!r}；归属引擎是 {owners}"
        )
    if category and not owners:
        blockers.append(
            f"分类 {category!r} 不在 category_catalog.json 中；不能直接追加"
        )

    append_mode = profile.get("can_append_category", "unknown")
    if append_mode == "via_product_type":
        caveats.append(
            "liability 的分类由 product_type 推导；row 必须写表内已有的 product_type，"
            "最终 bscat 不是由 category 列直接决定"
        )
    elif append_mode == "via_pattern_group":
        caveats.append(
            "income 的分类由 pattern_group 决定；只有 strong_wage/centrelink 等可产出分类，"
            "其余组只是门控"
        )
    elif append_mode == "unknown":
        blockers.append("索引未登记该引擎的追加策略，必须读源码确认")

    guards = profile.get("guards") or []
    if engine == "initial":
        caveats.append("keywords 必须大写、仅 [A-Z0-9 ]、压缩空格；每商户最多 50 个变体")
        if text and _looks_like_generic(text):
            caveats.append(
                "文本像通用描述词而不是商户名；initial 只收商户名/品牌名，"
                "通用词应复核 catch_all 或专属引擎"
            )
        if category in (profile.get("cleared_categories") or []):
            blockers.append(f"initial 的 {category} 分类会被 pipeline 清除")
    if engine == "gambling":
        if not layer:
            caveats.append("未确定 institution/rule 层；商户名走 institution，通用博彩词走 rule")
        if layer == "rule" or not layer:
            caveats.append(
                "商户名已命中 merchant_kb 时往 rule 层追加没有效果；先 search 查重"
            )
    if engine == "rent" and not layer:
        caveats.append("未确定 institution/rule 层；物业机构走 institution，通用租金词走 rule")
    if engine == "all_other_credit":
        caveats.append("只处理 dr_cr=credit；只支持 keyword，regex 行静默忽略")
    if engine == "fee":
        caveats.append("regex IGNORECASE；Overdrawn 规则的 priority 必须小于通用费用规则")
    if engine == "catch_all":
        caveats.append("只接未分类行；只收通用描述词，品牌名/商户名不属于 catch_all")
        if text and _looks_like_merchant(text):
            caveats.append(
                "文本像商户名/品牌名；catch_all 不是正确归属，"
                "应复核 initial（商户名）或专属引擎"
            )
    if engine == "dishonour":
        caveats.append("keyword 是子串匹配，不是全词")
    if engine == "transfer":
        caveats.append("regex 必须小写；category 列不读取")
    if engine == "income":
        caveats.append("金额 < $100 默认不判工资（gig 路径除外）；medium_income 需行为特征")
    if engine == "liability":
        caveats.append("product_type 只能取目标表已有值；counterparty 必须与证据机构名一致")

    if text and engine == "gambling":
        hint = _layer_hint(engine, text)
        if hint:
            caveats.append(f"文本更像 {hint} 层（自动提示，仍需按证据复核）")
    if text and engine == "rent":
        hint = _layer_hint(engine, text)
        if hint:
            caveats.append(f"文本更像 {hint} 层（自动提示，仍需按证据复核）")

    # De-duplicate while preserving order.
    blockers = list(dict.fromkeys(blockers))
    caveats = list(dict.fromkeys(caveats))
    can_append = not blockers
    return can_append, blockers, caveats, chosen


def _scope_for_task(
    tier: str,
    operation: str,
    problem_type: str,
) -> dict[str, Any]:
    rows = {
        "target": "目标聚类行 + 命中新/旧 pattern 的样本行",
        "local": "目标行 + 同引擎已认领行 + 同期望分类行",
        "full": "全部样本行",
    }[tier]
    claims = {
        "target": "只比较目标行上的各引擎 claim",
        "local": "比较目标行与局部冲突行上的各引擎 claim",
        "full": "比较全部样本行的全部引擎 claim",
    }[tier]
    return {
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "row_scope": rows,
        "claim_scope": claims,
        "baseline": tier == "full",
        "operation": operation,
        "problem_type": problem_type,
    }


def _steps_for_tier(tier: str) -> list[str]:
    diagnose = "always" if tier == "full" else "auto"
    verify = tier
    steps = [
        f"prepare --diagnose {diagnose}",
        "propose",
        "check",
        "approve --build",
        f"apply --verify-scope {verify}",
    ]
    if tier == "full":
        steps.append("复核 validation_after.json 的 baseline 与全量 claim 变化")
    return steps


def route_task(
    index: dict[str, Any],
    *,
    category: str = "",
    engine: str = "",
    problem_type: str = "",
    operation: str = "",
    current_engine: str = "",
    current_category: str = "",
    rows: int = 0,
    text: str = "",
    layer: str = "",
) -> dict[str, Any]:
    """Answer the six router questions for one task and choose a tier."""
    problem_type = common.normalise_problem_type(problem_type) or (
        "all" if not problem_type else problem_type
    )
    if problem_type not in {"miss", "extra", "diff", "all"}:
        problem_type = "all"
    operation = operation.strip().lower()
    if operation not in RULE_OPERATIONS | CONFIG_OPERATIONS | {"auto", ""}:
        operation = "auto"
    if operation in {"", "auto"}:
        operation = _default_operation(problem_type)
    layer = layer.strip().lower()
    if layer not in {"institution", "rule", ""}:
        layer = ""

    expected_category = "" if problem_type == "extra" else _norm(category)
    extra_category = _norm(current_category) if problem_type == "extra" else ""
    if problem_type == "extra":
        target_category = extra_category or _norm(category)
    else:
        target_category = expected_category or _norm(current_category)

    category_info = index["categories"].get(target_category, {}) if target_category else {}
    owners = list(category_info.get("owners") or [])
    enabled_owners = list(category_info.get("enabled_owners") or [])

    explicit_engine = _norm_engine(engine)
    if problem_type == "extra":
        recommended = _norm_engine(current_engine) or _recommended_engine(
            target_category, owners, set(index["enabled_engines"]), explicit_engine
        )
    else:
        recommended = _recommended_engine(
            target_category, owners, set(index["enabled_engines"]), explicit_engine
        )
    target_engine = explicit_engine or recommended
    engine_entry = index["engines"].get(target_engine, {})
    tables = _table_candidates(index, target_engine, target_category)
    layer_hint = layer or _layer_hint(target_engine, text)

    can_append, append_blockers, append_caveats, chosen_table = _append_analysis(
        index, target_engine, target_category, problem_type, operation,
        tables, layer_hint, text,
    )

    reasons: list[dict[str, str]] = []

    def add(reason_id: str, tier: str, reason: str) -> None:
        if not any(item["id"] == reason_id and item["tier"] == tier for item in reasons):
            reasons.append({"id": reason_id, "tier": tier, "reason": reason})

    # Category-level facts from the catalog.  An explicit engine that owns the
    # category is an evidence-backed choice, so multi-owner ambiguity is only a
    # caveat rather than a forced local validation.
    # For `extra`, the current engine is evidence of who produced the wrong
    # claim, so it counts as an explicit engine choice.
    engine_choice = explicit_engine or (
        _norm_engine(current_engine) if problem_type == "extra" else ""
    )
    category_reason_ids = {
        item["id"] for item in category_info.get("escalation") or []
    }
    for item in category_info.get("escalation") or []:
        if item["id"] == "owner_ambiguity" and engine_choice in enabled_owners:
            continue
        if item["id"] == "layer_ambiguity" and layer:
            continue
        add(item["id"], item["tier"], item["reason"])

    if not target_category:
        add("missing_category", "local", "没有提供目标分类；只能按引擎/表做局部判断")
    if not target_engine:
        add("missing_engine", "full", "无法确定目标引擎；必须升级为完整诊断")
    if target_engine and target_engine not in index["engines"]:
        add("unknown_engine", "full", f"引擎 {target_engine!r} 不在索引中")
    elif engine_entry and not engine_entry.get("enabled", False):
        add("engine_disabled", "full", f"引擎 {target_engine!r} 在 pipeline.json 中未启用")
    if target_engine and target_engine in STRUCTURAL_ENGINES:
        add(
            "structural_engine", "full",
            f"{target_engine} 是结构性引擎（行为阈值/多阶段/优先级），任何候选都高风险",
        )
    if operation in CONFIG_OPERATIONS:
        add("config_operation", "full", f"{operation} 变更属于结构性配置，必须完整诊断")
    if problem_type == "extra":
        add("extra_delete_narrow", "local", "多识别需要删除或收窄当前规则，不能追加")
        if target_engine in STRUCTURAL_ENGINES:
            add("extra_structural", "full", "多识别发生在结构性引擎上，接管关系必须完整验证")
    if problem_type == "diff":
        add("diff_pair", "local", "不一致通常需要删除/收窄旧规则并追加归属规则")
    if operation in {"edit", "delete", "keyword_update"}:
        add(
            "edit_delete_keyword_update", "local",
            f"{operation} 会改动既有规则，必须逐条看清接管与影响面",
        )
    if (
        target_engine in {"gambling", "rent"} and not layer_hint
        and "layer_ambiguity" not in category_reason_ids
    ):
        add(
            "unresolved_layer", "local",
            f"{target_engine} 是双层引擎；必须确定 institution 还是 rule 层",
        )
    if rows > 20:
        add("wide_sample_impact", "full", f"样本影响 {rows} 行，超过 20 行，需要全局验证")
    elif rows > 5:
        add("medium_sample_impact", "local", f"样本影响 {rows} 行，需要局部冲突验证")
    if not can_append and operation in {"append", "auto"}:
        add("append_not_feasible", "full", "追加不可行，必须改用 edit/delete 或完整诊断")
    if chosen_table.get("schema_warnings"):
        add(
            "missing_target_table", "full",
            "目标表不存在或表头与索引登记不一致，必须重新核对",
        )
    if target_engine and target_engine not in ENGINE_PROFILES:
        add("engine_not_profiled", "full", f"索引没有引擎 {target_engine!r} 的语义登记")

    attribution_risk = ""
    if (
        target_engine == "catch_all" and text
        and _looks_like_merchant(text) and "initial" in owners
    ):
        attribution_risk = "catch_all_holds_merchant"
        add(
            "attribution_review", "local",
            "目标引擎是 catch_all，但文本像商户名/品牌名；catch_all 只收通用描述词，"
            "必须复核是否应走 initial",
        )
    elif (
        target_engine == "initial" and text
        and _looks_like_generic(text) and "catch_all" in owners
    ):
        attribution_risk = "initial_holds_generic"
        add(
            "attribution_review", "local",
            "目标引擎是 initial，但文本像通用描述词；必须复核是否应走 catch_all 或专属引擎",
        )

    # Current-engine relation (miss/diff/extra often have a claim already).
    current_engine = _norm_engine(current_engine)
    if current_engine and target_engine and current_engine != target_engine:
        current_priority = _priority(index, current_engine)
        target_priority = _priority(index, target_engine)
        if target_priority < current_priority:
            takeover_exception = (
                current_engine == "catch_all"
                or (
                    current_engine == "all_other_credit"
                    and target_category != "External Transfers"
                )
            )
            if takeover_exception:
                add(
                    "owner_runs_earlier_but_excluded", "local",
                    f"归属引擎 {target_engine} 先执行，但 {current_engine} 只接未分类/排除已有声明；"
                    "追加前仍需确认当前规则不会继续命中",
                )
            else:
                structural_link = (
                    target_engine in STRUCTURAL_ENGINES
                    or current_engine in STRUCTURAL_ENGINES
                )
                add(
                    "owner_runs_earlier",
                    "full" if structural_link else "local",
                    f"归属引擎 {target_engine}(priority {target_priority}) 比当前引擎 "
                    f"{current_engine}(priority {current_priority}) 先执行；仅追加归属规则会被覆盖，"
                    "必须同时删除/收窄当前规则",
                )
        elif target_priority > current_priority:
            add(
                "owner_runs_later", "local",
                f"归属引擎 {target_engine}(priority {target_priority}) 在 {current_engine}"
                f"(priority {current_priority}) 之后执行，会覆盖当前声明；仍需确认旧规则是否残留",
            )
    elif current_engine and current_engine == target_engine and problem_type != "extra":
        add("same_engine_edit", "local", "同引擎改规则，需要检查同一文本是否被其他规则抢走")

    # Cross-engine override / block analysis.
    incoming_overrides: list[dict[str, Any]] = []
    append_blockers_edges: list[dict[str, Any]] = []
    non_overriding: list[dict[str, Any]] = []
    unrelated_later_engines: list[dict[str, Any]] = []
    for later in _later_engines(index, target_engine):
        effect = _prior_claim_effect(
            index, target_engine, target_category, layer_hint, later
        )
        if not effect:
            continue
        if effect["effect"] in {"row_level_override", "conditional"}:
            incoming_overrides.append(effect)
        elif effect["effect"] in {"only_unclassified", "not_an_override", "blocked"}:
            non_overriding.append(effect)
        elif effect["effect"] == "unrelated_category":
            unrelated_later_engines.append(effect)
        else:
            incoming_overrides.append(effect)

    chosen_file = chosen_table.get("target_file", "")
    incoming_special = _special_edges(
        index, target_engine, target_category, layer_hint, chosen_file,
        incoming=True,
    )
    outgoing_special = _special_edges(
        index, target_engine, target_category, layer_hint, chosen_file,
        incoming=False,
    )
    for edge in incoming_special:
        if edge["effect"] in {"from_engine_wins", "conditional"}:
            incoming_overrides.append({
                "engine": ", ".join(edge["from_engines"]),
                "effect": edge["effect"],
                "risk": "high" if edge["effect"] == "from_engine_wins" else "medium",
                "condition": edge["condition"],
                "description": edge["description"],
                "source": edge["source"],
                "rule_id": edge["id"],
            })
        else:
            append_blockers_edges.append(edge)

    override_risk = "low"
    for item in incoming_overrides:
        if item.get("risk") == "high":
            override_risk = "high"
            break
        if item.get("risk") == "medium":
            override_risk = "medium"
    if override_risk in {"high", "medium"}:
        add(
            "later_engine_override", "local",
            "后续引擎可能覆盖/影响该行；必须做局部冲突验证",
        )
    if operation == "delete" and any(
        item.get("effect") == "row_level_override" for item in incoming_overrides
    ):
        add(
            "delete_takeover_unknown", "full",
            "删除后可能有后续引擎接管该行；必须完整验证接管结果",
        )

    within_engine = []
    seen_within: set[str] = set()
    for edge in incoming_special + outgoing_special:
        if edge["effect"] != "within_engine" or edge["id"] in seen_within:
            continue
        seen_within.add(edge["id"])
        within_engine.append(edge)
    for edge in within_engine:
        add("within_engine_precedence", "local", edge["description"])

    tier = "target"
    for item in reasons:
        tier = _tier_max(tier, item["tier"])
    escalate_to_full = tier == "full"
    code_diagnosis = {
        "target": "skip",
        "local": "optional",
        "full": "required",
    }[tier]

    if not target_engine:
        code_diagnosis = "required"

    rank = _engine_rank(index, target_engine)
    execution = {
        "priority": engine_entry.get("priority"),
        "rank": rank,
        "band": engine_entry.get("band", "unknown"),
        "runs_before": _earlier_engines(index, target_engine),
        "runs_after": _later_engines(index, target_engine),
    }

    answers = {
        "q1_engine_and_table": {
            "engine": target_engine,
            "target_file": chosen_table.get("target_file", ""),
            "table_role": chosen_table.get("role", ""),
            "attribution_risk": attribution_risk,
            "owner_engines": owners,
            "enabled_owner_engines": enabled_owners,
            "candidate_tables": [
                {
                    "engine": table["engine"],
                    "target_file": table["target_file"],
                    "appendable": table["appendable"],
                    "match_mode": table["match_mode"],
                    "is_default_hint": table["is_default_hint"],
                }
                for table in tables
            ],
        },
        "q2_match_logic": {
            "match_mode": chosen_table.get("match_mode", ""),
            "normalization": engine_entry.get("normalization", ""),
            "summary": engine_entry.get("match_summary", ""),
            "guards": engine_entry.get("guards", []),
        },
        "q3_early_or_late": execution,
        "q4_append_feasible": {
            "can_append": can_append,
            "appendable": bool(chosen_table.get("appendable")),
            "blockers": append_blockers,
            "caveats": append_caveats,
        },
        "q5_overriders": {
            "risk": override_risk,
            "incoming_overrides": incoming_overrides,
            "append_blocking_rules": append_blockers_edges,
            "non_overriding_later_engines": non_overriding,
            "unrelated_later_engines": unrelated_later_engines,
            "outgoing_special_rules": outgoing_special,
            "within_engine": within_engine,
        },
        "q6_escalation": {
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "escalate_to_full": escalate_to_full,
            "reasons": reasons,
            "code_diagnosis": code_diagnosis,
            "policy": "escalation_policy.full_chain_triggers",
        },
    }

    return {
        "task": {
            "problem_type": problem_type,
            "operation": operation,
            "category": target_category,
            "expected_category": expected_category,
            "current_category": extra_category or _norm(current_category),
            "current_engine": current_engine,
            "engine": target_engine,
            "layer": layer_hint,
            "rows": int(rows or 0),
            "can_append": can_append,
        },
        "validation_tier": tier,
        "tier_label": TIER_LABELS[tier],
        "escalate_to_full": escalate_to_full,
        "escalation_reasons": reasons,
        "code_diagnosis": code_diagnosis,
        "validation_scope": _scope_for_task(tier, operation, problem_type),
        "recommended_steps": _steps_for_tier(tier),
        "override_risk": override_risk,
        "answers": answers,
        "index_warnings": [
            warning for warning in index.get("index_warnings", [])
            if (target_engine and target_engine in warning)
            or (target_category and target_category in warning)
        ],
    }


# ── input inspection (entry triage without running the pipeline) ───────────

def collect_input_task_info(
    path: Path,
    sheet: str | None = None,
) -> dict[str, Any]:
    """Read a sample's expected/current labels without running the pipeline."""
    import parse_input as pi

    if not path.exists():
        raise SystemExit(f"输入文件不存在: {path}")
    frame, sheet_name, sheets = pi.read_input(path, sheet)
    columns = [str(column) for column in frame.columns]

    def column_for(field: str) -> str | None:
        return pi.find_column(columns, field)

    expected_col = column_for("expected_category")
    current_col = column_for("current_category")
    problem_col = column_for("problem_type")
    engine_col = column_for("input_current_engine")

    tasks: Counter[tuple[str, str, str, str]] = Counter()
    for _, row in frame.iterrows():
        expected = (
            common.normalise_expected_category(row.get(expected_col))
            if expected_col else ""
        )
        current = (
            common.normalise_expected_category(row.get(current_col))
            if current_col else ""
        )
        current_engine = (
            _norm_engine(row.get(engine_col)) if engine_col else ""
        )
        explicit = (
            common.normalise_problem_type(row.get(problem_col))
            if problem_col else ""
        )
        if explicit:
            problem_type = explicit
        elif expected and not current:
            problem_type = "miss"
        elif expected and current and expected != current:
            problem_type = "diff"
        elif current and not expected:
            problem_type = "extra"
        else:
            problem_type = "correct"
        if problem_type not in {"miss", "extra", "diff"}:
            continue
        if problem_type in {"miss", "diff"}:
            tasks[(problem_type, expected, current, current_engine)] += 1
        else:
            tasks[(problem_type, "", current, current_engine)] += 1

    return {
        "path": str(path),
        "sheet": sheet_name,
        "sheets": sheets,
        "rows": len(frame),
        "columns": columns,
        "tasks": [
            {
                "problem_type": problem_type,
                "expected_category": expected,
                "current_category": current,
                "current_engine": engine_id,
                "rows": count,
            }
            for (problem_type, expected, current, engine_id), count in tasks.items()
        ],
    }


# ── payload assembly ───────────────────────────────────────────────────────

def _dedupe_tasks(tasks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    unique = []
    for task in tasks:
        key = (
            str(task["task"]["problem_type"]),
            str(task["task"]["category"]),
            str(task["task"]["engine"]),
            str(task["task"]["operation"]),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique


def build_route_payload(
    repo: Path,
    index: dict[str, Any],
    *,
    categories: Iterable[str] = (),
    engines: Iterable[str] = (),
    problem_type: str = "",
    operation: str = "",
    current_engine: str = "",
    current_category: str = "",
    rows: int = 0,
    text: str = "",
    layer: str = "",
    input_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    categories = [_norm(item) for item in categories if _norm(item)]
    engines = [_norm_engine(item) for item in engines if _norm_engine(item)]

    if input_info:
        for item in input_info.get("tasks") or []:
            if problem_type and problem_type != "all" and item["problem_type"] != problem_type:
                continue
            tasks.append(
                route_task(
                    index,
                    category=item.get("expected_category") or item.get("current_category") or "",
                    engine="",
                    problem_type=item["problem_type"],
                    operation=operation,
                    current_engine=item.get("current_engine") or current_engine,
                    current_category=item.get("current_category") or current_category,
                    rows=int(item.get("rows") or 0),
                    text=text,
                    layer=layer,
                )
            )
    elif categories:
        # --category and --engine are often supplied together: the category
        # names the goal, the engine records the evidence-backed choice.
        target_engines = engines or [""]
        for category in categories:
            for target_engine in target_engines:
                tasks.append(
                    route_task(
                        index,
                        category=category,
                        engine=target_engine,
                        problem_type=problem_type or "all",
                        operation=operation,
                        current_engine=current_engine,
                        current_category=current_category,
                        rows=rows,
                        text=text,
                        layer=layer,
                    )
                )
    elif engines:
        for engine in engines:
            tasks.append(
                route_task(
                    index,
                    category=current_category,
                    engine=engine,
                    problem_type=problem_type or "all",
                    operation=operation,
                    current_engine=current_engine,
                    current_category=current_category,
                    rows=rows,
                    text=text,
                    layer=layer,
                )
            )
    else:
        tasks.append(
            route_task(
                index,
                category=current_category,
                engine=current_engine,
                problem_type=problem_type or "all",
                operation=operation,
                current_engine=current_engine,
                current_category=current_category,
                rows=rows,
                text=text,
                layer=layer,
            )
        )

    tasks = _dedupe_tasks(tasks)
    overall_tier = "target"
    for task in tasks:
        overall_tier = _tier_max(overall_tier, task["validation_tier"])
    code_diagnosis = {
        "target": "skip",
        "local": "optional",
        "full": "required",
    }[overall_tier]
    if not tasks:
        overall_tier = "local"
        code_diagnosis = "optional"
    reasons = []
    seen_reasons: set[str] = set()
    for task in tasks:
        for reason in task["escalation_reasons"]:
            if reason["id"] in seen_reasons:
                continue
            seen_reasons.add(reason["id"])
            reasons.append(reason)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "inputs": {
            "categories": categories,
            "engines": engines,
            "problem_type": problem_type,
            "operation": operation,
            "current_engine": current_engine,
            "current_category": current_category,
            "rows": rows,
            "text": text,
            "layer": layer,
            "input_info": input_info,
        },
        "overall": {
            "validation_tier": overall_tier,
            "tier_label": TIER_LABELS[overall_tier],
            "escalate_to_full": overall_tier == "full",
            "code_diagnosis": code_diagnosis,
            "reasons": reasons,
            "recommended_steps": _steps_for_tier(overall_tier),
            "validation_scope": _scope_for_task(overall_tier, operation, problem_type or "all"),
            "never_skippable": escalation_policy()["never_skippable"],
        },
        "tasks": tasks,
        "index_warnings": index.get("index_warnings", []),
    }


# ── evidence-driven task profile (prepare) ─────────────────────────────────

def build_task_profile(
    repo: Path,
    evidence: dict[str, Any],
    index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarise impact scope from collected evidence (before candidates exist)."""
    index = index or build_index(repo)
    clusters = evidence.get("clusters") or []
    task_entries = []
    overall_tier = "target"
    needs_code_diagnosis = False
    for cluster in clusters:
        problem_type = str(cluster.get("problem_type", "") or "")
        category = (
            str(cluster.get("expected_category", "") or "")
            or str(cluster.get("current_category", "") or "")
        )
        routing = cluster.get("routing") or {}
        current_engine = _norm_engine(cluster.get("current_engine", ""))
        engine = _norm_engine(routing.get("owner_engine", "")) or current_engine
        operation = _default_operation(problem_type)
        decision = route_task(
            index,
            category=category,
            engine=engine,
            problem_type=problem_type,
            operation=operation,
            current_engine=current_engine,
            current_category=str(cluster.get("current_category", "") or ""),
            rows=int(cluster.get("count", 0) or 0),
            text=str((cluster.get("samples") or [{}])[0].get("text", "")
                     if cluster.get("samples") else ""),
        )
        tier = decision["validation_tier"]
        overall_tier = _tier_max(overall_tier, tier)
        status = str(routing.get("status", "") or "")
        if problem_type in {"diff", "extra"}:
            needs_code_diagnosis = True
        if status in {
            "blocked_guard", "overwritten_by_later", "owner_disabled",
            "catalog_missing", "same_owner_wrong_category",
        }:
            needs_code_diagnosis = True
        q1 = decision["answers"]["q1_engine_and_table"]
        q2 = decision["answers"]["q2_match_logic"]
        task_entries.append({
            "cluster_id": cluster.get("cluster_id", ""),
            "problem_type": problem_type,
            "category": category,
            "engine": engine,
            "current_engine": current_engine,
            "rows": int(cluster.get("count", 0) or 0),
            "routing_status": status,
            "routing_actions": routing.get("actions", []),
            "validation_tier": tier,
            "escalate_to_full": decision["escalate_to_full"],
            "escalation_reasons": decision["escalation_reasons"],
            "escalation_reason_ids": [
                str(item.get("id", ""))
                for item in decision["escalation_reasons"]
            ],
            "target_file": q1.get("target_file", ""),
            "candidate_file_hint": str(
                (routing.get("candidate_file_hint") or {}).get("file", "")
            ),
            "attribution_risk": q1.get("attribution_risk", ""),
            "owner_engines": q1.get("owner_engines", []),
            "enabled_owner_engines": q1.get("enabled_owner_engines", []),
            "candidate_tables": q1.get("candidate_tables", []),
            "match_mode": q2.get("match_mode", ""),
            "can_append": bool(decision["answers"]["q4_append_feasible"].get("can_append")),
            "sample_texts": [
                str(sample.get("text", "") or "")
                for sample in (cluster.get("samples") or [])[:3]
                if str(sample.get("text", "") or "").strip()
            ],
            "override_risk": decision["override_risk"],
        })
    if overall_tier == "full":
        needs_code_diagnosis = True
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "source": "evidence.json",
        "overall": {
            "validation_tier": overall_tier,
            "tier_label": TIER_LABELS[overall_tier],
            "escalate_to_full": overall_tier == "full",
            "code_diagnosis": "required" if needs_code_diagnosis else {
                "target": "skip", "local": "optional", "full": "required",
            }[overall_tier],
            "needs_code_diagnosis": needs_code_diagnosis,
            "recommended_steps": _steps_for_tier(overall_tier),
            "never_skippable": escalation_policy()["never_skippable"],
        },
        "clusters": task_entries,
        "index_warnings": index.get("index_warnings", []),
    }


def build_attribution_review(
    profile: dict[str, Any],
    *,
    interaction_mode: str = "standard",
) -> dict[str, Any]:
    """Build the I3 blueprint from ``task_profile`` entries.

    I3 only asks when target-table selection is genuinely ambiguous: the
    router flagged an attribution risk, several owner engines or tables are
    plausible, the gambling/rent layer is unresolved, the route has no
    evidence-level file hint, or routing hit a guard/overwrite/disabled step.
    """
    clusters: list[dict[str, Any]] = []
    for entry in profile.get("clusters") or []:
        if not isinstance(entry, dict):
            continue
        triggers: list[str] = []
        if entry.get("attribution_risk"):
            triggers.append("attribution_risk")
        owner_engines = [
            str(value)
            for value in (entry.get("enabled_owner_engines")
                          or entry.get("owner_engines") or [])
            if str(value).strip()
        ]
        if len(set(owner_engines)) > 1:
            triggers.append("owner_ambiguity")
        reason_ids = {
            str(value) for value in entry.get("escalation_reason_ids") or []
        }
        if "unresolved_layer" in reason_ids:
            triggers.append("unresolved_layer")
        tables = [
            table for table in entry.get("candidate_tables") or []
            if isinstance(table, dict)
        ]
        target_files = {
            str(table.get("target_file", ""))
            for table in tables
            if str(table.get("target_file", "")).strip()
        }
        if len(target_files) > 1:
            triggers.append("multiple_candidate_tables")
        if not str(entry.get("target_file", "") or "").strip():
            triggers.append("missing_target_file")
        if not str(entry.get("candidate_file_hint", "") or "").strip():
            triggers.append("missing_candidate_file_hint")
        if str(entry.get("routing_status", "") or "") in {
            "blocked_guard", "overwritten_by_later", "owner_disabled",
            "catalog_missing", "delete_or_edit_current_rule",
        }:
            triggers.append("routing_obstacle")
        recommended = {
            "engine": str(entry.get("engine", "") or ""),
            "target_file": str(entry.get("target_file", "") or ""),
        }
        alternatives = [
            {
                "engine": str(table.get("engine", "") or ""),
                "target_file": str(table.get("target_file", "") or ""),
                "match_mode": str(table.get("match_mode", "") or ""),
                "appendable": bool(table.get("appendable")),
            }
            for table in tables
            if (
                str(table.get("engine", "") or ""),
                str(table.get("target_file", "") or ""),
            ) != (recommended["engine"], recommended["target_file"])
        ]
        clusters.append({
            "cluster_id": str(entry.get("cluster_id", "") or ""),
            "problem_type": str(entry.get("problem_type", "") or ""),
            "category": str(entry.get("category", "") or ""),
            "rows": int(entry.get("rows", 0) or 0),
            "routing_status": str(entry.get("routing_status", "") or ""),
            "attribution_risk": str(entry.get("attribution_risk", "") or ""),
            "owner_engines": owner_engines,
            "match_mode": str(entry.get("match_mode", "") or ""),
            "recommended": recommended,
            "candidate_tables": tables,
            "alternatives": alternatives,
            "triggers": triggers,
            "sample_texts": entry.get("sample_texts") or [],
            "question": {
                "header": f"I3 归属 {entry.get('cluster_id', '')}",
                "prompt": (
                    f"{entry.get('problem_type', '')} / "
                    f"{entry.get('category', '') or '(无分类)'}，"
                    f"{entry.get('rows', 0)} 行；推荐 "
                    f"{recommended['engine'] or '(未定)'}/"
                    f"{recommended['target_file'] or '(未定)'}。"
                ),
                "option_keys": ["recommended", "alternative", "evidence", "stop"],
            },
        })
    required = any(item["triggers"] for item in clusters) or (
        interaction_mode == "guided" and bool(clusters)
    )
    material = {
        "clusters": [
            {
                key: item.get(key)
                for key in (
                    "cluster_id", "problem_type", "category", "routing_status",
                    "attribution_risk", "owner_engines", "recommended",
                    "candidate_tables", "triggers", "sample_texts",
                )
            }
            for item in clusters
        ]
    }
    confirm_count = (
        len(clusters) if interaction_mode == "guided"
        else sum(1 for item in clusters if item["triggers"])
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "required": required,
        "triggers": sorted({
            trigger for item in clusters for trigger in item["triggers"]
        }),
        "clusters": clusters,
        "questions": [{
            "header": "I3 归属/目标表",
            "prompt": (
                f"{confirm_count} 个聚类需要确认"
                "目标表/归属引擎；每个聚类按 recommended / alternative / "
                "evidence / stop 记录。"
            ),
            "option_keys": ["recommended", "alternative", "evidence", "stop"],
        }] if required else [],
        "fingerprint": common.sha256_json(material),
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path


# ── rendering ──────────────────────────────────────────────────────────────

def render_text(payload: dict[str, Any]) -> str:
    overall = payload.get("overall") or {}
    lines = [
        "任务路由（rule_index）",
        "=" * 64,
        f"- 仓库: {payload.get('repo', '')}",
        f"- 总体验证档位: {overall.get('tier_label', overall.get('validation_tier', ''))}",
        f"- 代码诊断: {overall.get('code_diagnosis', '')}",
    ]
    if overall.get("escalate_to_full"):
        lines.append("- 必须升级完整链路: 是")
    reasons = overall.get("reasons") or []
    if reasons:
        lines.append("- 升级原因:")
        for reason in reasons:
            lines.append(f"  - [{reason.get('tier')}] {reason.get('reason')}")
    for task in payload.get("tasks") or []:
        info = task.get("task") or {}
        answers = task.get("answers") or {}
        q1 = answers.get("q1_engine_and_table") or {}
        q2 = answers.get("q2_match_logic") or {}
        q3 = answers.get("q3_early_or_late") or {}
        q4 = answers.get("q4_append_feasible") or {}
        q5 = answers.get("q5_overriders") or {}
        lines += [
            "",
            f"## {info.get('category') or '(未指定分类)'} / "
            f"{info.get('problem_type')} / {info.get('operation')}",
            f"- 引擎: {q1.get('engine') or '(未定)'} | 表: {q1.get('target_file') or '(未定)'}",
            f"- 匹配: {q2.get('match_mode', '')} | 归一化: {q2.get('normalization', '')}",
            f"- 执行顺序: rank {q3.get('rank')} / priority {q3.get('priority')} "
            f"({q3.get('band')})",
            f"- 追加可行: {'是' if q4.get('can_append') else '否'}",
            f"- 覆盖风险: {q5.get('risk')}",
            f"- 验证档位: {task.get('tier_label')}",
        ]
        if q4.get("blockers"):
            lines.append("- 追加阻断: " + "; ".join(q4["blockers"]))
        if q4.get("caveats"):
            lines.append("- 追加注意: " + "; ".join(q4["caveats"]))
        if task.get("escalation_reasons"):
            lines.append(
                "- 升级原因: "
                + "; ".join(item["reason"] for item in task["escalation_reasons"])
            )
    if payload.get("index_warnings"):
        lines += ["", "## 索引漂移警告", ""]
        lines += [f"- {warning}" for warning in payload["index_warnings"]]
    return "\n".join(lines) + "\n"


# ── CLI ────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成 rule_index.json，并在入口回答目标引擎/表、匹配语义、覆盖风险与验证档位。"
    )
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--category", default="", help="期望分类，逗号分隔；extra 时表示当前分类。")
    parser.add_argument("--engine", default="", help="目标引擎，逗号分隔。")
    parser.add_argument(
        "--problem-type", choices=["miss", "extra", "diff", "all"], default="",
    )
    parser.add_argument(
        "--operation",
        choices=["append", "edit", "delete", "keyword_update", "priority", "catalog", "auto"],
        default="auto",
    )
    parser.add_argument("--current-engine", default="", help="当前给出分类的引擎。")
    parser.add_argument("--current-category", default="", help="当前分类（diff/extra 用）。")
    parser.add_argument("--rows", type=int, default=0, help="预计影响行数。")
    parser.add_argument("--text", default="", help="示例交易描述，用于 institution/rule 层提示。")
    parser.add_argument("--layer", choices=["institution", "rule", ""], default="")
    parser.add_argument("--input", default=None, help="可选：样本文件，入口直接按期望/当前分类分组。")
    parser.add_argument("--sheet", default=None, help="--input 的工作表。")
    parser.add_argument("--out", default=None, help="写入目录：rule_index.json + route_decision.json。")
    parser.add_argument("--index-out", default=None, help="只写 rule_index.json 的路径。")
    parser.add_argument("--json", action="store_true", help="输出 JSON。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).expanduser().resolve()
    index = build_index(repo)

    if args.out or args.index_out:
        from cli import ensure_outside_repo

        if args.out:
            ensure_outside_repo(repo, Path(args.out).expanduser().resolve())
        if args.index_out:
            ensure_outside_repo(
                repo, Path(args.index_out).expanduser().resolve().parent
            )

    input_info = None
    if args.input:
        input_info = collect_input_task_info(
            Path(args.input).expanduser().resolve(), args.sheet
        )

    payload = build_route_payload(
        repo,
        index,
        categories=_split(args.category),
        engines=_split(args.engine),
        problem_type=args.problem_type,
        operation=args.operation,
        current_engine=args.current_engine,
        current_category=args.current_category,
        rows=args.rows,
        text=args.text,
        layer=args.layer,
        input_info=input_info,
    )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(payload), end="")

    if args.index_out:
        path = write_json(Path(args.index_out).expanduser().resolve(), index)
        print(f"\nrule_index.json -> {path}")
    if args.out:
        out_dir = Path(args.out).expanduser().resolve()
        index_path = write_json(out_dir / "rule_index.json", index)
        route_path = write_json(out_dir / "route_decision.json", payload)
        print(f"\nrule_index.json -> {index_path}")
        print(f"route_decision.json -> {route_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
