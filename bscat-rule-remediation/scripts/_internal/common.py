#!/usr/bin/env python3
"""Shared repository knowledge for the BSCAT rule-remediation skill.

Every normaliser and match mode below mirrors how AUS_BS_CAT_Model actually
loads and matches its rule tables.  Getting one wrong is silent: a candidate
simply reports gain=0, or validation approves a rule the engine can never fire.
The citations point at the source file the behaviour was copied from; when the
repository changes, re-check those files before trusting a result.

Engine execution order (ascending priority, later engines overwrite earlier
ones) is read from ``configs/pipeline.json`` at runtime.  See
``references/rulebook.md`` for the human-readable table.
"""

from __future__ import annotations

import csv
import hashlib
import json
import pickle
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

# merchant_kb.csv contains very long pipe-separated keyword fields.  Windows
# C long is 32-bit, so sys.maxsize overflows; clamp to the safe 32-bit maximum.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# ── text normalisation ──────────────────────────────────────────────────────

_CLEAN_RE = re.compile(r"[^A-Z0-9]+")
_SEAM_RE = re.compile(r"(?<=\d)(?=[A-Z])|(?<=[A-Z])(?=\d)")
_CHANNEL_PREFIX_RE = re.compile(
    r"^(?:"
    r"BILL\s*PAY(?:MENT)?\s+|"
    r"VISA\s+(?:WDL|PURCHASE|DEBIT)\s+(?:PURCHASE\s+)?(?:\d+\w+\s+)?\s*|"
    r"VISA\s+CREDIT\s+\d*\s*|"
    r"EFTPOS\s+(?:DEBIT|WDL)(?:\s+\d+)?\s+|"
    r"MISCELLANEOUS\s+DEBIT\s+V\d+\s+\d+\s+\d+\s+|"
    r"DEBIT\s+CARD\s+PURCHASE\s+|"
    r"EFT\s+Dep\s+"
    r")",
    re.IGNORECASE,
)
_EFTPOS_TS_RE = re.compile(
    r"^EFTPOS\s+DEBIT\s+(?:EFTPOS\s+)?\d{1,2}/\d{1,2}\s+\d{1,2}:\d{1,2}",
    re.IGNORECASE,
)


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)  # NaN / pd.NA
    except (TypeError, ValueError):
        return True


def clean_text(value: Any) -> str:
    """Uppercase, non-alphanumerics -> one space, collapse, trim.

    ``classification_core/text.py:26-32``.  Used by initial, gambling, rent
    and catch_all.
    """
    if is_missing(value):
        return ""
    return " ".join(_CLEAN_RE.sub(" ", str(value).upper()).split())


def clean_text_with_channel_prefix(value: Any) -> str:
    """``clean_text`` plus EFTPOS-timestamp/channel-prefix stripping.

    ``classification_core/text.py:64-80``.  Used by the rent and gambling
    *institution* layers (merchant-KB style keywords).
    """
    if is_missing(value):
        return ""
    text = str(value).upper()
    text = _EFTPOS_TS_RE.sub("", text)
    text = _CLEAN_RE.sub(" ", text)
    text = _CHANNEL_PREFIX_RE.sub("", text)
    text = _CLEAN_RE.sub(" ", text)
    return " ".join(text.split())


def clean_text_with_seams(value: Any) -> str:
    """``clean_text`` plus a space at every digit/letter seam.

    ``classification_core/text.py:83-96``.  Income only: lets ``\\bSALARY\\b``
    match ``...3780.7Salary``.
    """
    if is_missing(value):
        return ""
    return _SEAM_RE.sub(" ", clean_text(value))


def normalize_lower(value: Any) -> str:
    """Transfer's ``normalize_text``: collapse whitespace, lowercase.

    ``transfer_engine/domain/classification.py:188-192``.
    """
    if is_missing(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip().lower())


def normalize_upper(value: Any) -> str:
    """Liability's counterparty-rule text: strip, uppercase, collapse."""
    if is_missing(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip().upper())


def normalize_for_mode(mode: str, value: Any) -> str:
    if mode in {"clean", "clean_keyword", "clean_regex", "initial_variants"}:
        return clean_text(value)
    if mode in {"merchant_rule", "clean_variants"}:
        return clean_text(value)
    if mode in {"institution_variants", "liability_homecar"}:
        return clean_text_with_channel_prefix(value)
    if mode in {"lower", "transfer_regex"}:
        return normalize_lower(value)
    if mode in {"upper", "liability_counterparty"}:
        return normalize_upper(value)
    if mode == "income_regex":
        return clean_text_with_seams(value)
    return "" if is_missing(value) else str(value)


# ── match primitives ────────────────────────────────────────────────────────

def split_variants(pattern: Any, separator: str = "|") -> list[str]:
    if is_missing(pattern):
        return []
    return [part.strip() for part in str(pattern).split(separator) if part.strip()]


def whole_word_anywhere(keyword: str, text: str) -> bool:
    """Space/string-edge whole word, scanning every occurrence.

    Matches the initial/institution Aho-Corasick boundary check
    (``initial_engine/domain/classification.py:478-544``).
    """
    if not keyword or not text:
        return False
    start = text.find(keyword)
    while start != -1:
        before_ok = start == 0 or text[start - 1] == " "
        end = start + len(keyword)
        after_ok = end == len(text) or text[end] == " "
        if before_ok and after_ok:
            return True
        start = text.find(keyword, start + 1)
    return False


def first_keyword_whole_word(keyword: str, text: str) -> bool:
    """Only the first occurrence is checked, exactly like the gambling, rent
    and catch_all keyword layers (``gambling_engine/engine.py:131-143``)."""
    if not keyword or not text:
        return False
    pos = text.find(keyword)
    if pos == -1:
        return False
    if pos > 0 and text[pos - 1] != " ":
        return False
    end = pos + len(keyword)
    return end >= len(text) or text[end] == " "


def alpha_edge_regex(keyword: str) -> str:
    """Liability counterparty-rule boundary.

    ``(?<![A-Za-z])kw(?![A-Za-z])`` -- digits and punctuation may touch the
    keyword, letters may not (``liability_engine/domain/counterparty.py:189``).
    """
    return rf"(?<![A-Za-z]){re.escape(keyword)}(?![A-Za-z])"


def compile_pattern(mode: str, pattern: str, match_type: str = "") -> tuple[bool, str]:
    """Validate a pattern as the engine will compile it."""
    if mode == "initial_variants":
        variants = split_variants(pattern)
        if not variants:
            return False, "没有关键词变体"
        bad = [v for v in variants if v != clean_text(v)]
        if bad:
            return False, (
                "initial 关键词会原样插入，并与清洗后的大写文本匹配；"
                f"请先归一化: {bad[:5]}"
            )
        return True, ""
    if mode == "merchant_rule" and str(match_type).strip().lower() == "keyword":
        if not str(pattern).strip():
            return False, "关键词为空"
        return True, ""
    if mode in {"income_regex", "clean_regex", "transfer_regex", "raw_regex_ci",
                "word_boundary_ci", "fee_regex", "all_other_credit",
                "liability_homecar", "liability_credit_card", "liability_counterparty",
                "liability_dishonour", "dishonour"}:
        if not str(pattern).strip() and mode != "liability_homecar":
            return False, "pattern 为空"
        if mode == "dishonour" and str(match_type).strip().lower() == "keyword":
            return True, ""
        if mode == "liability_dishonour" and str(match_type).strip().lower() == "keyword":
            return True, ""
        if mode == "all_other_credit" and str(match_type).strip().lower() != "regex":
            return True, ""
        try:
            re.compile(pattern)
        except re.error as exc:
            return False, f"正则编译错误: {exc}"
        return True, ""
    return True, ""


@lru_cache(maxsize=100_000)
def _compiled_regex(pattern: str, flags: int) -> re.Pattern[str] | None:
    """Compile once per (pattern, flags); invalid patterns cache as None."""
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


def _regex_search(pattern: str, text: str, flags: int = 0) -> bool:
    if not pattern or text == "":
        return False
    compiled = _compiled_regex(pattern, flags)
    return bool(compiled and compiled.search(text) is not None)


def _variant_word_boundary(pattern: str, text: str) -> bool:
    variants = split_variants(pattern, ";") or split_variants(pattern, "|")
    joined = "|".join(re.escape(v) for v in variants)
    return _regex_search(rf"\b(?:{joined})\b", text, re.IGNORECASE)


# ── rule-file catalogue ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class RuleFileSpec:
    engine: str
    relative_path: str
    pattern_column: str
    match_mode: str
    category_column: str | None = None
    rule_id_column: str | None = None
    implicit_category: str | None = None
    appendable: bool = True
    is_matcher: bool = True
    row_mode_column: str | None = None
    note: str = ""


RULE_FILES: tuple[RuleFileSpec, ...] = (
    # ── transfer (priority 1; lowercased text, case-sensitive regex) ──────
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_external_high_confidence_rules.csv",
        "pattern", "transfer_regex", None, "rule_name",
        implicit_category="External Transfers",
        note="first match by ascending priority; category column is ignored",
    ),
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_external_medium_confidence_rules.csv",
        "pattern", "transfer_regex", None, "rule_name",
        implicit_category="External Transfers",
        note="first match by ascending priority; category column is ignored",
    ),
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_internal_regex_rules.csv",
        "pattern", "transfer_regex", None, "rule_name",
        implicit_category="Internal Transfer",
        note="no category column; engine assigns Internal Transfer",
    ),
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_pairing_exclusions.csv",
        "keyword", "transfer_pairing_exclusion", None, None,
        appendable=False, is_matcher=False,
        note="pairing-exclusion table, not a classifier",
    ),
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_counterparty_rules.csv",
        "keyword", "transfer_counterparty", None, None,
        appendable=False, is_matcher=False,
        note="counterparty mapping, not a category rule",
    ),
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_indicator_patterns.csv",
        "pattern", "raw_regex_ci", None, "pattern_id",
        appendable=False, is_matcher=False,
        note="internal-pairing indicator, not a classifier",
    ),
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_group_exclusion_patterns.csv",
        "pattern", "raw_regex_ci", "category", "pattern_id",
        appendable=False, is_matcher=False,
        note="internal-pairing group exclusion, not a classifier",
    ),
    RuleFileSpec(
        "transfer", "transfer_engine/resources/transfer_row_exclusion_patterns.csv",
        "pattern", "raw_regex_ci", None, "pattern_id",
        appendable=False, is_matcher=False,
        note="internal-pairing row exclusion, not a classifier",
    ),
    # ── initial (priority 10; merchant KB, cleaned uppercase whole words) ──
    RuleFileSpec(
        "initial", "initial_engine/merchant_kb.csv",
        "keywords", "initial_variants", "category", "merchant_name",
        note="merchant_name + pipe-separated keyword variants; longest keyword wins",
    ),
    # ── dishonour (priority 150; raw text) ────────────────────────────────
    RuleFileSpec(
        "dishonour", "dishonour_engine/resources/dishonour_rules.csv",
        "pattern", "dishonour", None, "rule_type",
        implicit_category="Dishonours",
        note="keyword = case-insensitive literal substring; regex = IGNORECASE",
    ),
    # ── gambling (priority 180) ────────────────────────────────────────────
    RuleFileSpec(
        "gambling", "gambling_engine/resources/gambling_rules.csv",
        "pattern", "merchant_rule", "category", "rule_name",
        row_mode_column="source",
        note="source=institution uses pipe variants on clean_text_with_channel_prefix",
    ),
    # ── income (priority 200; every row is a regex, no match_type) ────────
    RuleFileSpec(
        "income", "income_engine/resources/income_pattern_rules.csv",
        "pattern", "income_regex", None, "pattern_group",
        note="category comes from pattern_group; only salary_packaging/centrelink "
             "produce a category directly, other groups are gates",
    ),
    # ── liability (priority 300) ───────────────────────────────────────────
    RuleFileSpec(
        "liability", "liability_engine/resources/credit_card_rules.csv",
        "keyword", "liability_credit_card", "product_type", None,
        note="all rows regex IGNORECASE on raw text; priority tiers >=90 then <90",
    ),
    RuleFileSpec(
        "liability", "liability_engine/resources/counterparty_keyword_rules.csv",
        "keyword", "liability_counterparty", "product_type", None,
        note="letters-only boundaries on uppercase text; keywords run before regex rows",
    ),
    RuleFileSpec(
        "liability", "liability_engine/resources/home_loan_car_loan_rules.csv",
        "pattern", "liability_homecar", None, "rule_id",
        implicit_category="Non SACC Loans",
        note="match_scope/amount_gt gate the match; keyword constraints are dropped",
    ),
    RuleFileSpec(
        "liability", "liability_engine/resources/dishonours_rules.csv",
        "pattern", "liability_dishonour", None, "rule_type",
        implicit_category="Dishonours",
        note="keyword is a lowercase literal substring; regex is case-sensitive",
    ),
    RuleFileSpec(
        "liability", "liability_engine/resources/overdrawn_rules.csv",
        "pattern", "raw_regex_ci", None, "rule_id",
        implicit_category="Overdrawn",
        note="Format B regex IGNORECASE on raw text",
    ),
    RuleFileSpec(
        "liability", "liability_engine/resources/debt_collection_rules.csv",
        "keyword", "word_boundary_ci", None, "rule_id",
        implicit_category="Debt Collection",
        note="semicolon keywords merged into \\b(?:...)\\b IGNORECASE",
    ),
    RuleFileSpec(
        "liability", "liability_engine/resources/debt_consolidation_rules.csv",
        "keyword", "word_boundary_ci", None, None,
        implicit_category="Debt Consolidation",
        note="semicolon keywords merged into \\b(?:...)\\b IGNORECASE",
    ),
    RuleFileSpec(
        "liability", "liability_engine/resources/bnpl_maximum_limits.csv",
        "counterparty", "substring", None, None,
        appendable=False, is_matcher=False,
        note="limit configuration, not a matcher",
    ),
    # ── all_other_credit (priority 400; keyword rules only) ───────────────
    RuleFileSpec(
        "all_other_credit", "all_other_credit_engine/resources/all_other_credit_rules.csv",
        "pattern", "all_other_credit", None, "rule_type",
        implicit_category="All Other Credits",
        note="rule_type=keyword only; regex rows are silently ignored",
    ),
    # ── fee (priority 500; every row regex IGNORECASE, raw text) ──────────
    RuleFileSpec(
        "fee", "fee_engine/resources/fee_classification_rules.csv",
        "pattern", "fee_regex", "category", "rule_name",
        note="first match by ascending priority; unclassified_only/zero_amount_reject "
             "filters apply after matching",
    ),
    # ── rent (priority 800) ────────────────────────────────────────────────
    RuleFileSpec(
        "rent", "rent_engine/resources/rent_rules.csv",
        "pattern", "merchant_rule", "category", "rule_name",
        row_mode_column="source",
        note="source=institution (pipe keywords) beats source=rule; highest confidence wins",
    ),
    # ── catch_all (priority 999; unclassified rows only) ──────────────────
    RuleFileSpec(
        "catch_all", "catch_all_engine/resources/catch_all_rules.csv",
        "pattern", "merchant_rule", "category", "rule_name",
        note="highest confidence wins; only rows with no prior claim",
    ),
)


# Chinese one-liners shown in approval popups and the catalog CLI.  Kept
# separate from ``note`` so the English spec comments stay close to the code
# semantics and this layer can stay UI-friendly.
TABLE_ROLES_ZH: dict[str, str] = {
    "transfer_engine/resources/transfer_external_high_confidence_rules.csv":
        "外部转账高置信正则；文本先转小写，priority 升序第一匹配，category 列不读取",
    "transfer_engine/resources/transfer_external_medium_confidence_rules.csv":
        "外部转账中置信正则；文本先转小写，priority 升序第一匹配，category 列不读取",
    "transfer_engine/resources/transfer_internal_regex_rules.csv":
        "内部转账正则；无 category 列，命中即输出 Internal Transfer",
    "transfer_engine/resources/transfer_pairing_exclusions.csv":
        "转账配对排除表（priority 降序）；只影响配对，不产出分类",
    "transfer_engine/resources/transfer_counterparty_rules.csv":
        "转账对手方名称映射；keyword 为子串、第一匹配胜出，不产出分类",
    "transfer_engine/resources/transfer_indicator_patterns.csv":
        "内部转账配对指示器正则；只用于识别配对信号，不产出分类",
    "transfer_engine/resources/transfer_group_exclusion_patterns.csv":
        "内部转账配对组排除；只影响配对分组，不产出分类",
    "transfer_engine/resources/transfer_row_exclusion_patterns.csv":
        "内部转账配对行排除；只影响单行配对，不产出分类",
    "initial_engine/merchant_kb.csv":
        "商户知识库：商户名 + 竖线分隔关键词变体；全词最长胜出，写入前必须预清洗为大写",
    "dishonour_engine/resources/dishonour_rules.csv":
        "拒付/退票规则；keyword 为大小写不敏感字面子串，regex 为 IGNORECASE，可带 required_terms",
    "gambling_engine/resources/gambling_rules.csv":
        "赌博双层表；institution 行为竖线商户变体全词匹配，rule 行为通用博彩词/正则，最高 confidence 胜出",
    "income_engine/resources/income_pattern_rules.csv":
        "收入规则；每行都是正则，分类由 pattern_group 决定，部分组只是门控/排除",
    "liability_engine/resources/credit_card_rules.csv":
        "信用卡还款规则；keyword 列实际是正则，全部 IGNORECASE 匹配原始文本，priority 分 >=90 与 <90 两档",
    "liability_engine/resources/counterparty_keyword_rules.csv":
        "贷款机构/对手方关键词表；大写文本上按字母边界匹配，keyword 行先于 regex 行，个人贷等默认选这张",
    "liability_engine/resources/home_loan_car_loan_rules.csv":
        "房贷/车贷规则（Format A 12 列）；match_scope/amount_gt 为门控，keyword 约束不生效",
    "liability_engine/resources/dishonours_rules.csv":
        "贷款侧拒付规则；keyword 为小写字面子串，regex 大小写敏感",
    "liability_engine/resources/overdrawn_rules.csv":
        "透支规则（Format B）；pattern 为正则、IGNORECASE 匹配原始文本，category 固定 Overdrawn",
    "liability_engine/resources/debt_collection_rules.csv":
        "债务催收规则；分号关键词合并为 \\b(?:...)\\b、IGNORECASE，category 固定 Debt Collection",
    "liability_engine/resources/debt_consolidation_rules.csv":
        "债务整合规则；分号关键词合并为 \\b(?:...)\\b、IGNORECASE，category 固定 Debt Consolidation",
    "liability_engine/resources/bnpl_maximum_limits.csv":
        "BNPL 限额配置；只用于限额判断，不参与分类，禁止修改",
    "all_other_credit_engine/resources/all_other_credit_rules.csv":
        "入账类退款/返现/利息规则；只支持 rule_type=keyword，regex 行被静默忽略，仅处理 credit",
    "fee_engine/resources/fee_classification_rules.csv":
        "费用规则；pattern 为正则、IGNORECASE，priority 升序第一匹配，unclassified_only/zero_amount_reject 在匹配后过滤",
    "rent_engine/resources/rent_rules.csv":
        "房租双层表；institution 行竖线变体优先于 rule 行，最高 confidence 胜出",
    "catch_all_engine/resources/catch_all_rules.csv":
        "兜底通用关键词表；最高 confidence 胜出，只处理前面引擎未认领的行，只收通用描述词",
}


def table_role(spec: RuleFileSpec) -> str:
    """Chinese table description for UI/reports, falling back to the spec note."""
    return TABLE_ROLES_ZH.get(spec.relative_path, spec.note)


def spec_for(engine: str, target_file: str) -> RuleFileSpec | None:
    for spec in RULE_FILES:
        if spec.engine == engine and spec.relative_path == target_file:
            return spec
    return None


def specs_for_engine(engine: str) -> list[RuleFileSpec]:
    return [spec for spec in RULE_FILES if spec.engine == engine]


def appendable_specs(engine: str) -> list[RuleFileSpec]:
    return [spec for spec in specs_for_engine(engine) if spec.appendable]


def matcher_specs(engine: str) -> list[RuleFileSpec]:
    return [spec for spec in specs_for_engine(engine) if spec.is_matcher]


# ── repository config ───────────────────────────────────────────────────────

def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_pipeline(repo: Path) -> dict[str, Any]:
    return load_json(repo / "configs" / "pipeline.json")


def load_catalog(repo: Path) -> dict[str, Any]:
    return load_json(repo / "configs" / "category_catalog.json")


def engine_priorities(repo: Path) -> dict[str, int]:
    payload = load_pipeline(repo)
    return {
        str(engine.get("engine_id")): int(engine.get("priority", 500))
        for engine in payload.get("pipeline", [])
    }


def enabled_engines(repo: Path) -> set[str]:
    payload = load_pipeline(repo)
    return {
        str(engine.get("engine_id"))
        for engine in payload.get("pipeline", [])
        if engine.get("enabled", True)
    }


def category_owner_map(repo: Path) -> dict[str, list[str]]:
    catalog = load_catalog(repo)
    owners: dict[str, list[str]] = {}
    for category, info in catalog.get("categories", {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("active") is False:
            continue
        raw = str(info.get("owner_engine_id", ""))
        owners[str(category)] = [part.strip() for part in raw.split(",") if part.strip()]
    return owners


def all_categories(repo: Path) -> dict[str, dict[str, Any]]:
    return {
        str(key): value
        for key, value in load_catalog(repo).get("categories", {}).items()
        if isinstance(value, dict)
    }


def category_allowed(repo: Path, category: str, engine: str) -> bool:
    owners = category_owner_map(repo).get(str(category))
    return bool(owners and engine in owners)


# ── problem taxonomy ────────────────────────────────────────────────────────
#
# miss  漏识别: an expected category exists but the pipeline did not emit it.
# extra 多识别: no category is expected (the row should stay unclassified) but
#              the pipeline emitted one.
# diff  不一致: both labels exist and differ.
# correct / unknown are kept so validation still has negative examples.

PROBLEM_TYPES = ("miss", "extra", "diff")
PROBLEM_TYPE_LABELS = {
    "miss": "漏识别",
    "extra": "多识别",
    "diff": "不一致",
    "correct": "已正确",
    "unknown": "未标注",
}
PROBLEM_TYPE_ALIASES = {
    "miss": "miss", "missing": "miss", "false_negative": "miss", "fn": "miss",
    "漏识别": "miss", "漏分": "miss", "未识别": "miss", "缺失": "miss",
    "extra": "extra", "over": "extra", "false_positive": "extra", "fp": "extra",
    "多识别": "extra", "误识别": "extra", "多分": "extra", "误分": "extra",
    "diff": "diff", "mismatch": "diff", "wrong": "diff", "incorrect": "diff",
    "不一致": "diff", "错分": "diff", "分类错误": "diff", "差异": "diff",
    "correct": "correct", "ok": "correct", "pass": "correct",
    "正确": "correct", "已正确": "correct",
    "unknown": "unknown", "unlabeled": "unknown", "未标注": "unknown",
}

# Values that mean "this row should not be classified".  Anything else is a
# real expected category.  The empty string is handled separately.
UNCLASSIFIED_VALUES = {
    "unclassified", "not classified", "no category", "none", "null", "n/a",
    "na", "-", "--", "/", "__unclassified__", "should_be_unclassified",
    "should not be classified", "无需分类", "不需分类", "不应分类", "未分类",
    "无", "空", "不分类",
}


def normalise_problem_type(value: Any) -> str:
    """Canonical ``miss`` / ``extra`` / ``diff`` / ``correct`` / ``unknown``."""
    text = "" if is_missing(value) else str(value).strip().lower()
    if not text:
        return ""
    return PROBLEM_TYPE_ALIASES.get(text, "")


def problem_type_label(value: Any) -> str:
    canonical = normalise_problem_type(value) or str(value or "")
    return PROBLEM_TYPE_LABELS.get(canonical, canonical)


def expected_is_unclassified(value: Any) -> bool:
    """True when the expected label explicitly means 'leave unclassified'."""
    if is_missing(value):
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.lower() in UNCLASSIFIED_VALUES


def normalise_expected_category(value: Any) -> str:
    """Blank out sentinel values that mean 'should stay unclassified'."""
    if is_missing(value):
        return ""
    text = str(value).strip()
    return "" if expected_is_unclassified(text) else text


def infer_problem_type(
    expected: str,
    pipeline_category: str,
    explicit: str = "",
    blank_expected: str = "unknown",
) -> str:
    """Classify one row.

    ``explicit`` (from a problem_type column) always wins.  Otherwise:

    * expected but no pipeline output -> ``miss``
    * no expected and pipeline output, when blank expected means ``extra`` ->
      ``extra``
    * both present and different -> ``diff``
    * both present and equal -> ``correct``
    * no expected and no output -> ``unknown``
    """
    canonical = normalise_problem_type(explicit)
    if canonical:
        return canonical
    if expected:
        if not pipeline_category:
            return "miss"
        if pipeline_category == expected:
            return "correct"
        return "diff"
    if pipeline_category and blank_expected == "extra":
        return "extra"
    return "unknown"


def problem_scope_types(scope: str) -> set[str]:
    canonical = str(scope or "all").strip().lower()
    if canonical in {"", "all", "auto"}:
        return set(PROBLEM_TYPES)
    return {canonical} & set(PROBLEM_TYPES)


# ── rule tables ─────────────────────────────────────────────────────────────

@dataclass
class RuleRow:
    engine: str
    relative_path: str
    line_number: int
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    @property
    def pattern(self) -> str:
        for key in ("pattern", "keywords", "keyword"):
            value = self.values.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    @property
    def category(self) -> str:
        return str(self.values.get("category", ""))


_RULE_CACHE: dict[tuple[str, str], list[RuleRow]] = {}
_INITIAL_AUTOMATON: dict[str, Any] = {}
_INITIAL_COMPETITION: dict[
    tuple[str, tuple[str, ...]], dict[str, list[dict[str, str]]]
] = {}


def read_rule_rows(repo: Path, spec: RuleFileSpec) -> list[RuleRow]:
    key = (str(repo.resolve()), spec.relative_path)
    if key in _RULE_CACHE:
        return _RULE_CACHE[key]
    path = repo / spec.relative_path
    if not path.exists():
        _RULE_CACHE[key] = []
        return []
    rows: list[RuleRow] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for values in reader:
            rows.append(
                RuleRow(
                    engine=spec.engine,
                    relative_path=spec.relative_path,
                    line_number=reader.line_num,
                    values={str(key): str(value) for key, value in values.items()},
                )
            )
    _RULE_CACHE[key] = rows
    return rows


def clear_caches() -> None:
    _RULE_CACHE.clear()
    _INITIAL_AUTOMATON.clear()
    _INITIAL_COMPETITION.clear()


def load_initial_automaton(repo: Path) -> Any | None:
    """Load the prebuilt merchant_kb Aho-Corasick automaton.

    Scanning 874k merchant rows per cluster would dominate runtime; the
    initial engine itself uses this sidecar cache.
    """
    key = str(repo.resolve())
    if key in _INITIAL_AUTOMATON:
        return _INITIAL_AUTOMATON[key]
    csv_path = repo / "initial_engine" / "merchant_kb.csv"
    # The pipeline run in collect_evidence.py has already loaded the engine's
    # automaton in this process.  Reuse that object instead of unpickling the
    # 700MB sidecar a second time.
    engine_module = sys.modules.get("initial_engine.domain.classification")
    if engine_module is not None:
        cached = getattr(engine_module, "_cached_automaton", None)
        cached_path = getattr(engine_module, "_cached_kb_path", None)
        if cached is not None and cached_path:
            try:
                if Path(cached_path).resolve() == csv_path.resolve():
                    _INITIAL_AUTOMATON[key] = cached
                    return cached
            except OSError:
                pass
    path = repo / "initial_engine" / "merchant_kb.csv.automaton.pickle"
    automaton = None
    if path.exists():
        # The pickle references classes under the repo package root, so the
        # repository must be importable while unpickling.  collect_evidence.py
        # already does this; validation and diagnosis also load the automaton
        # directly and need the same import path.
        repo_root = str(repo.resolve())
        added = repo_root not in sys.path
        if added:
            sys.path.insert(0, repo_root)
        try:
            with path.open("rb") as handle:
                _, automaton = pickle.load(handle)
        except Exception:
            automaton = None
        finally:
            if added:
                try:
                    sys.path.remove(repo_root)
                except ValueError:
                    pass
    _INITIAL_AUTOMATON[key] = automaton
    return automaton


def initial_keyword_hits(repo: Path, text: str) -> list[dict[str, str]]:
    """Whole-word merchant_kb hits: [{'keyword','merchant','category'}]."""
    automaton = load_initial_automaton(repo)
    if automaton is None:
        return []
    try:
        hits = automaton.search(clean_text(text))
    except Exception:
        return []
    return [
        {"keyword": keyword, "merchant": merchant, "category": category}
        for keyword, merchant, category in hits
    ]


def initial_keyword_competition(
    repo: Path,
    texts: Iterable[str],
    override: dict[str, Any] | None = None,
    append: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Effective merchant_kb keyword owners for each cleaned text.

    Reproduces the initial loader's keyword map: ``Financial Institutions``
    rows are dropped, each merchant keeps the first ``INITIAL_MAX_VARIANTS``
    variants, every variant is stripped, standalone stopwords are dropped, and
    **the last row wins** for a duplicated keyword string.  Matching is
    whole-word on ``clean_text`` output and the longest matching keyword wins
    (``initial_engine/domain/classification.py``).

    ``override`` simulates an edit: ``{"match_key": {...}, "values": {...}}``
    substitutes one existing row.  ``append`` simulates a new final row, which
    therefore wins every keyword it shares with an existing merchant.

    Only keywords that occur as a whole-word n-gram of one of ``texts`` are
    returned, so one streaming pass over the 874k-row KB keeps memory small.
    A plain scan is cached per (repo, text set); candidate-specific scans are
    not cached because each override produces a different keyword map.
    """
    cleaned_texts = {
        clean_text(text) for text in texts if str(text or "").strip()
    }
    key = (str(repo.resolve()), tuple(sorted(cleaned_texts)))
    if override is None and append is None and key in _INITIAL_COMPETITION:
        return _INITIAL_COMPETITION[key]

    ngrams: set[str] = set()
    for text in cleaned_texts:
        words = text.split()
        for start in range(len(words)):
            for end in range(start + 1, min(start + 21, len(words) + 1)):
                ngrams.add(" ".join(words[start:end]))

    def wanted(values: dict[str, Any]) -> list[str]:
        merchant = str(values.get("merchant_name", "") or "").strip()
        variants = split_variants(values.get("keywords", ""))[
            :INITIAL_MAX_VARIANTS
        ]
        return [
            keyword
            for keyword in variants
            if keyword and keyword not in STOPWORDS and keyword in ngrams
            and merchant
        ]

    def register(
        owner: dict[str, dict[str, str]],
        values: dict[str, Any],
    ) -> None:
        merchant = str(values.get("merchant_name", "") or "").strip()
        category = str(values.get("category", "") or "").strip()
        if not merchant or category == "Financial Institutions":
            return
        for keyword in wanted(values):
            owner[keyword] = {
                "keyword": keyword,
                "merchant": merchant,
                "category": category,
            }

    match_key = {
        str(name): str(value).strip()
        for name, value in ((override or {}).get("match_key") or {}).items()
    }
    owner: dict[str, dict[str, str]] = {}
    path = repo / "initial_engine" / "merchant_kb.csv"
    if path.exists() and ngrams:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if override and match_key and all(
                    str(row.get(name, "") or "").strip() == value
                    for name, value in match_key.items()
                ):
                    register(owner, (override or {}).get("values") or {})
                    continue
                register(owner, row)
    if append:
        register(owner, append.get("values") or {})

    hits_by_text = {text: [] for text in cleaned_texts}
    for hit in owner.values():
        for text in cleaned_texts:
            if whole_word_anywhere(hit["keyword"], text):
                hits_by_text[text].append(dict(hit))
    if override is None and append is None:
        _INITIAL_COMPETITION[key] = hits_by_text
    return hits_by_text


def iter_rule_rows(repo: Path, engine: str | None = None) -> Iterable[RuleRow]:
    for spec in RULE_FILES:
        if engine and spec.engine != engine:
            continue
        for row in read_rule_rows(repo, spec):
            yield row


# ── engine-accurate matching ────────────────────────────────────────────────

def _homecar_matches(row: RuleRow, context: dict[str, Any]) -> bool:
    scope = row.get("match_scope", "text").strip().lower() or "text"
    match_type = row.get("match_type", "keyword").strip().lower() or "keyword"
    enabled = row.get("enabled", "1").strip()
    pattern = row.get("pattern", "")
    if enabled == "0":
        return False
    if scope != "all" and not pattern:
        return False

    if scope == "all":
        for field_name in ("account_type", "dr_cr", "bank"):
            wanted = row.get(field_name, "*").strip().lower() or "*"
            if wanted in {"*", "-"}:
                continue
            actual = str(context.get(field_name, "")).strip().lower()
            if actual != wanted:
                return False
        amount_gt = row.get("amount_gt", "").strip()
        if amount_gt:
            try:
                threshold = float(amount_gt)
            except ValueError:
                return False
            try:
                amount = abs(float(context.get("amount")))
            except (TypeError, ValueError):
                return False
            if not amount > threshold:
                return False
        if match_type == "keyword":
            return _variant_word_boundary(pattern, str(context.get("text", "")))
        if not pattern:
            return True  # match_type=always compiles an empty regex
        return _regex_search(pattern, str(context.get("text", "")), re.IGNORECASE)

    text = str(context.get("text", ""))
    counterparty = str(context.get("counterparty", ""))
    haystack = text if scope == "text" else f"{text} {counterparty}"
    if match_type == "keyword":
        return _variant_word_boundary(pattern, haystack)
    return _regex_search(pattern, haystack, re.IGNORECASE)


def _credit_card_matches(row: RuleRow, context: dict[str, Any]) -> bool:
    pattern = row.get("keyword", "")
    if not _regex_search(pattern, str(context.get("text", "")), re.IGNORECASE):
        return False
    for field_name in ("account_type", "dr_cr", "bank"):
        wanted = row.get(field_name, "*").strip().lower()
        if wanted in {"*", "-", ""}:
            continue
        actual = str(context.get(field_name, "")).strip().lower()
        if actual != wanted:
            return False
    return True


def rule_matches(spec: RuleFileSpec, row: RuleRow, context: dict[str, Any]) -> bool:
    """True when this rule would fire on one transaction.

    ``context`` carries the raw fields the engine reads: ``text``, ``dr_cr``,
    ``amount``, ``account_type``, ``bank`` and ``counterparty``.
    """
    raw = str(context.get("text", ""))
    mode = spec.match_mode
    pattern = row.pattern
    match_type = row.get("match_type", "").strip().lower()

    if not pattern and mode != "liability_homecar":
        return False

    if mode == "initial_variants":
        cleaned = clean_text(raw)
        return any(whole_word_anywhere(v, cleaned) for v in split_variants(pattern))

    if mode == "transfer_regex":
        wanted = row.get("dr_cr", "").strip().lower()
        if wanted and str(context.get("dr_cr", "")).strip().lower() != wanted:
            return False
        return _regex_search(pattern, normalize_lower(raw))

    if mode == "dishonour":
        if match_type == "keyword":
            return _regex_search(re.escape(pattern), raw, re.IGNORECASE)
        terms = [t.strip().lower() for t in row.get("required_terms", "").split(";") if t.strip()]
        lower = raw.lower()
        if not all(_regex_search(term, lower) for term in terms):
            return False
        return _regex_search(pattern, raw, re.IGNORECASE)

    if mode == "merchant_rule":
        source = row.get("source", "rule").strip().lower()
        if source == "institution":
            cleaned = clean_text_with_channel_prefix(raw)
            return any(whole_word_anywhere(v, cleaned) for v in split_variants(pattern))
        cleaned = clean_text(raw)
        if match_type == "keyword":
            return first_keyword_whole_word(pattern, cleaned)
        return _regex_search(pattern, cleaned)

    if mode == "income_regex":
        return _regex_search(pattern, clean_text_with_seams(raw))

    if mode == "liability_counterparty":
        text = normalize_upper(raw)
        if match_type == "regex":
            return _regex_search(pattern, text)
        return any(
            _regex_search(alpha_edge_regex(v), text, re.IGNORECASE)
            for v in split_variants(pattern, ";")
        )

    if mode == "liability_homecar":
        return _homecar_matches(row, context)

    if mode == "liability_credit_card":
        return _credit_card_matches(row, context)

    if mode == "liability_dishonour":
        lower = raw.lower()
        if match_type == "keyword":
            return pattern.lower() in lower
        terms = [t.strip().lower() for t in row.get("required_terms", "").split(";") if t.strip()]
        if not all(term in lower for term in terms):
            return False
        return _regex_search(pattern, raw)

    if mode == "word_boundary_ci":
        return _variant_word_boundary(pattern, raw)

    if mode == "all_other_credit":
        if row.get("rule_type", "keyword").strip().lower() != "keyword":
            return False
        return _regex_search(re.escape(pattern), raw, re.IGNORECASE)

    if mode == "fee_regex":
        wanted = row.get("dr_cr", "").strip().lower()
        if wanted and str(context.get("dr_cr", "")).lower() != wanted:
            return False
        return _regex_search(pattern, raw, re.IGNORECASE)

    if mode == "raw_regex_ci":
        return _regex_search(pattern, raw, re.IGNORECASE)

    if mode in {"clean_keyword", "clean_regex"}:
        cleaned = clean_text(raw)
        if match_type == "keyword" or mode == "clean_keyword":
            return first_keyword_whole_word(pattern, cleaned)
        return _regex_search(pattern, cleaned)

    if mode == "substring":
        return pattern.upper() in raw.upper()

    return False


def rule_output_category(spec: RuleFileSpec, row: RuleRow) -> str:
    """The catalogue category this rule can emit, when that is knowable.

    Liability counterparty/credit-card rules derive bscat from product_type,
    bank and stream logic, so they return ``""``; the candidate's declared
    category is authoritative for those.
    """
    if spec.implicit_category:
        return spec.implicit_category
    if spec.engine == "income":
        return income_group_category(row.get("pattern_group", ""))
    if spec.engine == "fee":
        value = row.get("category", "").strip()
        return "Fees" if value.lower() == "fee" else value
    if spec.engine == "liability" and spec.relative_path.endswith("credit_card_rules.csv"):
        return ""
    if spec.engine == "liability" and spec.relative_path.endswith("counterparty_keyword_rules.csv"):
        return ""
    return row.get(spec.category_column or "category", "")


INCOME_GROUP_CATEGORY = {
    "salary_packaging": "Wages",
    "centrelink": "Centrelink",
    "salary_payg": "Wages",
    "self_employed_gig": "Wages",
    "self_employed_gig_repeat_payer": "Wages",
}


def income_group_category(group: str) -> str:
    return INCOME_GROUP_CATEGORY.get(str(group).strip().lower(), "")


def find_matching_rules(
    repo: Path,
    engine: str,
    context: dict[str, Any],
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Existing rules in ``engine`` that already match this transaction."""
    text = str(context.get("text", ""))
    hits: list[dict[str, Any]] = []

    if engine == "initial":
        for hit in initial_keyword_hits(repo, text)[:limit]:
            hits.append(
                {
                    "file": "initial_engine/merchant_kb.csv",
                    "line": None,
                    "rule_id": hit["merchant"],
                    "pattern": hit["keyword"],
                    "category": hit["category"],
                    "match_type": "keyword",
                }
            )
        return hits

    for spec in matcher_specs(engine):
        for row in read_rule_rows(repo, spec):
            if not row.pattern and spec.match_mode != "liability_homecar":
                continue
            if not rule_matches(spec, row, context):
                continue
            hits.append(
                {
                    "file": spec.relative_path,
                    "line": row.line_number,
                    "rule_id": row.get(spec.rule_id_column or "", ""),
                    "pattern": row.pattern,
                    "category": rule_output_category(spec, row),
                    "match_type": row.get("match_type", ""),
                    "source": row.get("source", ""),
                }
            )
            if len(hits) >= limit:
                return hits
    return hits


def duplicate_rules(
    repo: Path,
    engine: str,
    pattern: str,
    category: str = "",
) -> list[dict[str, Any]]:
    """Rows with the same normalised pattern (and category, when known)."""
    wanted = str(pattern).strip().upper()
    hits: list[dict[str, Any]] = []

    if engine == "initial":
        for hit in initial_keyword_hits(repo, str(pattern)):
            if hit["keyword"].strip().upper() != wanted:
                continue
            if category and hit["category"] and hit["category"] != category:
                continue
            hits.append(
                {
                    "file": "initial_engine/merchant_kb.csv",
                    "line": None,
                    "pattern": hit["keyword"],
                    "category": hit["category"],
                    "merchant": hit["merchant"],
                }
            )
        return hits

    for spec in matcher_specs(engine):
        for row in read_rule_rows(repo, spec):
            if row.pattern.strip().upper() != wanted:
                continue
            row_category = rule_output_category(spec, row)
            if category and row_category and row_category != category:
                continue
            hits.append(
                {
                    "file": spec.relative_path,
                    "line": row.line_number,
                    "pattern": row.pattern,
                    "category": row_category,
                }
            )
    return hits


# ── text clustering / candidate hints ───────────────────────────────────────

_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
_LONG_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_AMOUNT_RE = re.compile(r"\$\s*\d+(?:[.,]\d{2})?")
_LONG_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_CARD_RE = re.compile(r"\b(?:CARD|XX)\s*X*\d{2,6}\b", re.IGNORECASE)


def cluster_text(value: Any) -> str:
    """Strip dates, amounts, card masks and long references for clustering."""
    text = str(value or "")
    text = _LONG_DATE_RE.sub(" ", text)
    text = _DATE_RE.sub(" ", text)
    text = _AMOUNT_RE.sub(" ", text)
    text = _CARD_RE.sub(" ", text)
    text = _LONG_NUMBER_RE.sub(" ", text)
    return clean_text(text)


STOPWORDS = {
    "THE", "AND", "FOR", "FROM", "WITH", "VIA", "AUS", "AU", "NSW", "VIC",
    "QLD", "WA", "SA", "TAS", "NT", "ACT", "PTY", "LTD", "LIMITED", "INC",
    "CARD", "PURCHASE", "VALUE", "DATE", "TRANSACTION", "DIRECT", "DEBIT",
    "CREDIT", "PAYMENT", "TRANSFER", "DEPOSIT", "WITHDRAWAL",
}


def tokens(value: Any) -> list[str]:
    return [
        token for token in clean_text(value).split()
        if len(token) >= 3 and token not in STOPWORDS
    ]


def candidate_keywords(value: Any, max_tokens: int = 4) -> list[str]:
    """Heuristic keyword suggestions: whole cleaned text, then 2-4-grams."""
    cleaned = clean_text(value)
    words = cleaned.split()
    suggestions: list[str] = []
    if 1 <= len(words) <= max_tokens:
        suggestions.append(cleaned)
    for size in range(min(max_tokens, len(words)), 1, -1):
        for start in range(0, len(words) - size + 1):
            phrase = " ".join(words[start:start + size])
            if phrase not in STOPWORDS:
                suggestions.append(phrase)
    seen: set[str] = set()
    unique: list[str] = []
    for suggestion in suggestions:
        if suggestion and suggestion not in seen:
            seen.add(suggestion)
            unique.append(suggestion)
    return unique[:20]


# ── initial keyword maintenance ──────────────────────────────────────────────
#
# The initial engine inserts keywords verbatim and matches them against
# ``clean_text`` output, so a lowercase or punctuation-bearing keyword silently
# never fires.  It also drops standalone stopwords and caps each merchant at 50
# variants.  These helpers are the single place that enforces those rules; the
# proposer, validator and writer all call them.

INITIAL_MAX_VARIANTS = 50


def split_initial_keywords(value: Any) -> list[str]:
    """Split a merchant_kb ``keywords`` cell, discarding empty variants."""
    if is_missing(value):
        return []
    return [part.strip() for part in str(value).split("|") if part.strip()]


def validate_initial_keyword(value: Any) -> tuple[bool, str]:
    """Check one keyword exactly as the initial loader will use it."""
    keyword = str(value or "")
    if not keyword.strip():
        return False, "关键词为空"
    normalised = clean_text(keyword)
    if keyword != normalised:
        return False, (
            "关键词必须预清洗为大写、仅 [A-Z0-9 ]、压缩空格；"
            f"应写成 {normalised!r}"
        )
    if keyword in STOPWORDS:
        return False, f"关键词 {keyword!r} 是停用词，会被 initial loader 丢弃"
    return True, ""


def merge_initial_keywords(
    current: Any,
    update: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return ``(final_keywords, change)`` for a keyword update.

    ``update`` accepts either a full replacement (``keywords``) or an
    add/remove pair (``add_keywords`` / ``remove_keywords``).  Raises
    ``ValueError`` when the result cannot be written safely: empty list,
    unnormalised keyword, standalone stopword, or more than 50 variants.
    """
    existing = split_initial_keywords(current)
    mode = "replace" if update.get("keywords") not in (None, "") else "add_remove"
    if mode == "replace":
        proposed = split_initial_keywords(update.get("keywords"))
    else:
        removes = {
            clean_text(value)
            for value in update.get("remove_keywords") or []
            if str(value).strip()
        }
        proposed = [value for value in existing if clean_text(value) not in removes]
        proposed.extend(
            str(value).strip()
            for value in update.get("add_keywords") or []
            if str(value).strip()
        )

    final: list[str] = []
    seen: set[str] = set()
    for value in proposed:
        ok, message = validate_initial_keyword(value)
        if not ok:
            raise ValueError(message)
        if value in seen:
            continue
        seen.add(value)
        final.append(value)

    if not final:
        raise ValueError("更新后关键词为空；initial 不允许没有关键词的商户行")
    if len(final) > INITIAL_MAX_VARIANTS:
        raise ValueError(
            f"更新后关键词 {len(final)} 个，超过 initial 上限 "
            f"{INITIAL_MAX_VARIANTS}；请先删除不再需要的变体"
        )

    existing_set = set(existing)
    final_set = set(final)
    change = {
        "mode": mode,
        "added": [value for value in final if value not in existing_set],
        "removed": [value for value in existing if value not in final_set],
        "count_before": len(existing),
        "count_after": len(final),
    }
    return "|".join(final), change


# ── candidate helpers ───────────────────────────────────────────────────────

RULE_OPERATIONS = {"append", "edit", "delete", "keyword_update"}
CONFIG_OPERATIONS = {"catalog", "priority"}


def candidate_pattern(candidate: dict[str, Any]) -> str:
    row = candidate.get("row") or {}
    for key in ("pattern", "keywords", "keyword"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def candidate_trace_error(candidate: dict[str, Any]) -> str | None:
    """Explain why a rule candidate lacks a usable config-table trace.

    Every rule change must point at the table the evidence actually came from:
    ``routing.candidate_file_hint`` for appends and the matched rule's ``file``
    for edit/delete/keyword_update.  This is the enforcement half of the
    "溯源到配置表" rule; the proposer writes the draft trace and the agent
    verifies it before approval.
    """
    operation = str(candidate.get("operation", "")).strip()
    if operation not in RULE_OPERATIONS:
        return None
    target_file = str(candidate.get("target_file", "")).strip()
    trace = candidate.get("source_trace")
    if not isinstance(trace, dict) or not trace:
        return (
            "缺少 source_trace：必须先为候选溯源到证据命中的配置表，"
            "再设计/补充规则"
        )
    traced = str(trace.get("target_file", "")).strip()
    if not traced:
        return "source_trace.target_file 为空：请写明候选对应的配置表"
    if traced != target_file:
        return (
            f"source_trace.target_file={traced!r} 与候选 target_file="
            f"{target_file!r} 不一致"
        )
    hint = str(trace.get("candidate_file_hint", "")).strip()
    if hint and hint != target_file:
        override = str(trace.get("override_reason", "")).strip()
        if not override:
            return (
                f"证据提示的配置表 {hint!r} 与 target_file {target_file!r} 不一致；"
                "如确认证据提示有误，请在 source_trace.override_reason 写明原因"
            )
    matched = trace.get("matched_rule") or {}
    matched_file = str(matched.get("file", "")).strip() if isinstance(matched, dict) else ""
    if (
        operation in {"edit", "delete", "keyword_update"}
        and matched_file
        and matched_file != target_file
    ):
        return (
            f"命中的规则位于 {matched_file!r}，与 target_file {target_file!r} 不一致"
        )
    return None


def candidate_trace_warnings(candidate: dict[str, Any]) -> list[str]:
    """Non-fatal trace notes the reviewer must see before approval."""
    if str(candidate.get("operation", "")).strip() not in RULE_OPERATIONS:
        return []
    trace = candidate.get("source_trace") or {}
    if not isinstance(trace, dict):
        return []
    warnings: list[str] = []
    hint = str(trace.get("candidate_file_hint", "")).strip()
    target_file = str(candidate.get("target_file", "")).strip()
    if hint and hint != target_file:
        warnings.append(
            f"配置表溯源被人工覆盖：证据提示 {hint!r}，候选写入 {target_file!r}；"
            f"原因：{trace.get('override_reason', '')}"
        )
    if str(trace.get("trace_method", "")).strip() == "engine_default":
        warnings.append(
            "该候选没有证据级配置表提示，使用引擎默认表兜底；"
            "请确认 pattern 语义与该表一致"
        )
    return warnings


def product_type_values(repo: Path, spec: RuleFileSpec) -> list[str]:
    """Existing ``product_type`` values in one rule table (never invent one)."""
    values = {
        row.get("product_type", "").strip()
        for row in read_rule_rows(repo, spec)
        if row.get("product_type", "").strip()
    }
    return sorted(values)


def candidate_product_type_error(
    repo: Path,
    candidate: dict[str, Any],
    spec: RuleFileSpec,
    header: list[str],
    values: dict[str, Any],
) -> str | None:
    """Reject a liability ``product_type`` outside the target table's values.

    Liability derives the final bscat from ``product_type``, so a made-up value
    would silently misclassify.  A blank value is allowed: the agent fills it
    from the table's existing set using the evidence and the rulebook.
    """
    engine = str(candidate.get("engine", "")).strip()
    operation = str(candidate.get("operation", "")).strip()
    if engine != "liability" or operation not in {"append", "edit"}:
        return None
    if "product_type" not in header:
        return None
    product_type = str(values.get("product_type", "")).strip()
    if not product_type:
        return None
    allowed = product_type_values(repo, spec)
    if allowed and product_type not in allowed:
        return (
            f"product_type={product_type!r} 不在 {spec.relative_path} 的已有"
            f"取值中；允许值: {allowed}"
        )
    return None


METADATA_COLUMNS = {
    "candidate_id", "problem_type", "status", "risk_level", "operation",
    "target_file", "reason", "evidence", "expected_resolved",
    "expected_conflicts", "expected_effect", "validation", "match_key",
    "row", "category", "match_type", "engine", "samples", "evidence_source",
}


# ── dialogue interaction (I1 intake / I4 match review) ─────────────────────
#
# The agent asks these questions through AskUserQuestion, but the runtime files
# stay plain JSON so every answer is auditable and can be replay-checked.  The
# helpers here only describe and validate; they never write rule tables.

INTAKE_FILENAME = "intake.json"
MATCH_REVIEW_FILENAME = "match_review.json"
MATCH_REVIEW_DECISIONS_FILENAME = "match_review_decisions.json"

INTAKE_PROBLEM_TYPES = {"miss", "diff", "extra", "all"}
INTAKE_DIRECTIONS = {
    "append_first",      # miss: 优先新增规则
    "fix_existing",      # diff: 优先修正旧规则
    "delete_extra",      # extra: 优先删除/收窄误命中
    "paired",            # diff: 删除旧规则 + 新增规则成对处理
    "evidence_driven",   # 不预设，按证据决定
    "report_only",       # 只出建议，不改规则表
}
INTAKE_SCOPES = {
    "sample_only",       # 只覆盖样本里的文本
    "cluster",           # 覆盖同一聚类内的相似文本
    "broader_similar",   # 主动扩大到相似商户/相似文本
    "not_sure",          # 由 Agent 给出最窄建议
}
INTAKE_DELIVERIES = {
    "full",              # 分析 + 改表 + 前后验证
    "precheck_only",     # 只分析和评估，不写
    "dry_run_only",      # 到 dry-run 为止
    "report_first",      # 先出报告，再决定是否写
}
INTERACTION_MODES = {"guided", "standard", "minimal"}


def load_intake(path: Path) -> dict[str, Any]:
    """Read ``intake.json`` from a review dir or an explicit file path.

    Missing file returns ``{}`` so older review directories keep working.  An
    unreadable or out-of-vocabulary file is a hard error: a typo in the intake
    must not silently fall back to a different problem type.
    """
    candidate = Path(path)
    file_path = candidate / INTAKE_FILENAME if candidate.is_dir() else candidate
    if not file_path.exists():
        return {}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{file_path} 无法解析: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{file_path} 必须是 JSON 对象")

    def check(field: str, allowed: set[str]) -> None:
        value = str(payload.get(field, "") or "").strip()
        if value and value not in allowed:
            raise SystemExit(
                f"{file_path} {field}={value!r} 不合法；允许: {sorted(allowed)}"
            )
        if field in payload:
            payload[field] = value

    check("problem_type", INTAKE_PROBLEM_TYPES)
    check("direction", INTAKE_DIRECTIONS)
    check("scope", INTAKE_SCOPES)
    check("delivery", INTAKE_DELIVERIES)
    check("interaction_mode", INTERACTION_MODES)

    primary = str(payload.get("problem_type", "") or "").strip()
    directions = payload.get("directions")
    if isinstance(directions, dict):
        for key, value in directions.items():
            if key not in INTAKE_PROBLEM_TYPES or key == "all":
                raise SystemExit(
                    f"{file_path} directions 键 {key!r} 必须是 miss/diff/extra"
                )
            if str(value).strip() not in INTAKE_DIRECTIONS:
                raise SystemExit(
                    f"{file_path} directions[{key!r}]={value!r} 不合法；"
                    f"允许: {sorted(INTAKE_DIRECTIONS)}"
                )
    elif directions not in (None, ""):
        raise SystemExit(f"{file_path} directions 必须是对象")
    if primary == "all" and not isinstance(directions, dict):
        # ``all`` is valid without per-type directions; every type then follows
        # the evidence with no preselected direction.
        payload["directions"] = {}

    payload["_path"] = str(file_path)
    return payload


# ── conditional dialogue gates (I2/I3/I5/I6/I8) ─────────────────────────────
#
# P0 made I1 and I4 hard gates.  P1 adds conditional gates for parsing,
# attribution, revisions, pre-approval impact review and post-write next
# steps.  Each answer still lives in one auditable JSON file; the helpers
# below validate vocabulary and fingerprints, never write rule tables.

DIALOG_DECISIONS_FILENAME = "dialog_decisions.json"
DIALOG_GATES = ("parse", "attribution", "revise", "precheck", "outcome")

PARSE_DECISIONS = {
    "continue", "change_sheet", "change_columns", "adjust_problem_type", "stop",
}
PARSE_AUTHORITIES = {"sample", "current", "manual", "stop"}
PARSE_TYPE_MODES = {"follow_sample", "keep_intake", "one_type", "stop"}
ATTRIBUTION_SELECTIONS = {"recommended", "alternative", "evidence", "stop"}
ATTRIBUTION_FALLBACKS = {
    "suggestion", "search_then_retry", "full_diagnosis", "stop",
}
REVISE_DECISIONS = {"apply", "continue_editing", "cancel"}
PRECHECK_DECISIONS = {"continue", "revise", "degrade", "stop"}
PRECHECK_IMPACTS = {"accept", "narrow", "need_samples", "stop"}
PRECHECK_SCOPES = {"auto", "local", "full", "current"}
OUTCOME_DECISIONS = {"accept", "rollback", "full_then_decide", "export_report"}
OUTCOME_REMAINING = {"continue", "end", "export_pending"}

# Triggers that stay mandatory even in ``minimal`` mode: these are attribution
# ambiguities a wrong table selection would turn into silent rework.
MINIMAL_ATTRIBUTION_TRIGGERS = {
    "attribution_risk", "owner_ambiguity", "unresolved_layer",
}


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def interaction_mode(review_dir: Path) -> str:
    intake = load_intake(review_dir)
    value = str(intake.get("interaction_mode", "") or "").strip()
    return value if value in INTERACTION_MODES else "standard"


def load_dialog_decisions(
    path: Path, *, explicit: bool = False
) -> dict[str, Any]:
    """Read ``dialog_decisions.json`` from a review dir or an explicit path.

    Missing file returns ``{}`` for backward compatibility.  An explicitly
    supplied missing path is fatal, and any malformed section is fatal: a
    typo must never silently drop a user's gate answer.
    """
    candidate = Path(path)
    file_path = candidate / DIALOG_DECISIONS_FILENAME if candidate.is_dir() else candidate
    if not file_path.exists():
        if explicit:
            raise SystemExit(f"找不到对话决策文件: {file_path}")
        return {}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{file_path} 无法解析: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{file_path} 必须是 JSON 对象")
    allowed = {"schema_version", "run_id", "note", *DIALOG_GATES}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SystemExit(f"{file_path} 含未知顶层字段: {unknown}")
    for gate in DIALOG_GATES:
        if gate in payload and not isinstance(payload[gate], dict):
            raise SystemExit(f"{file_path} 的 {gate} 必须是对象")
    payload["_path"] = str(file_path)
    return payload


def dialog_section(review_dir: Path, gate: str) -> dict[str, Any]:
    payload = load_dialog_decisions(review_dir)
    section = payload.get(gate) or {}
    return dict(section) if isinstance(section, dict) else {}


def _gate_stamp_errors(section: dict[str, Any], gate: str) -> list[str]:
    errors: list[str] = []
    if not str(section.get("decided_at", "") or "").strip():
        errors.append(f"{gate} 决策缺少 decided_at；请记录用户确认时间")
    return errors


def _fingerprint_errors(
    section: dict[str, Any], expected: str, gate: str
) -> list[str]:
    actual = str(section.get("fingerprint", "") or "").strip()
    if not actual:
        return [f"{gate} 决策缺少 fingerprint；请从对应审核蓝图读取后重写"]
    if actual != expected:
        return [
            f"{gate} 决策已过期（确认 {actual}，当前 {expected}）；"
            "请重新查看蓝图并重写决策"
        ]
    return []


def load_json_if_exists(path: Path) -> dict[str, Any]:
    if not Path(path).exists():
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def parse_gate_errors(review_dir: Path) -> list[str]:
    """Validate I2 against the current ``parse_review.json``.

    Missing ``parse_review.json`` means an older review directory, so I2 is
    not retroactively enforced.  ``minimal`` still blocks zero-row parses,
    because that is a hard failure rather than an anti-fatigue decision.
    """
    review_path = Path(review_dir) / "parse_review.json"
    if not review_path.exists():
        return []
    review = load_json_if_exists(review_path)
    summary = load_json_if_exists(Path(review_dir) / "parse_summary.json")
    triggers = [str(value) for value in review.get("triggers") or []]
    if "zero_rows" in triggers:
        return ["解析后样本为 0 行；请检查输入/sheet/列映射后再生成候选"]
    section = dialog_section(review_dir, "parse")
    mode = interaction_mode(review_dir)
    required = bool(review.get("required")) or mode == "guided"
    if not required:
        if str(section.get("decision", "")).strip() == "stop":
            return ["用户在 I2 选择停止；本轮不生成候选"]
        return []
    if mode == "minimal":
        if str(section.get("decision", "")).strip() == "stop":
            return ["用户在 I2 选择停止；本轮不生成候选"]
        return []

    if not section:
        return [
            "缺少 I2 解析确认；请先查看 parse_review.json，"
            "用 AskUserQuestion 确认后写入 dialog_decisions.json#parse"
        ]
    errors = _gate_stamp_errors(section, "parse")
    errors += _fingerprint_errors(
        section, str(review.get("fingerprint", "")), "I2"
    )
    decision = str(section.get("decision", "")).strip()
    if decision not in PARSE_DECISIONS:
        errors.append(
            f"I2 decision={decision!r} 不合法；允许: {sorted(PARSE_DECISIONS)}"
        )
        return errors
    if decision == "stop":
        return [*errors, "用户在 I2 选择停止；本轮不生成候选"]
    if decision == "change_sheet":
        wanted = str(section.get("sheet", "") or "").strip()
        return [
            *errors,
            f"I2 要求换 sheet{f'（{wanted}）' if wanted else ''}；"
            "请重跑 prepare 后把 parse 决策更新为 continue",
        ]
    if decision == "change_columns":
        return [
            *errors,
            "I2 要求改列映射；请重跑 prepare --column-map 后把决策更新为 continue",
        ]
    if decision == "adjust_problem_type":
        return [
            *errors,
            "I2 要求调整问题类型；请重跑 prepare --problem-type 后把决策更新为 continue",
        ]

    if (
        any(
            trigger in triggers
            for trigger in (
                "missing_expected", "missing_expected_column", "label_conflict",
            )
        )
        and "expected_authority" not in section
    ):
        errors.append(
            "I2 决策缺少 expected_authority；请在 sample/current/manual/stop 中确认"
        )
    if "problem_type_mismatch" in triggers and "type_distribution" not in section:
        errors.append(
            "I2 决策缺少 type_distribution；请在 follow_sample/keep_intake/"
            "one_type/stop 中确认"
        )

    summary_mapping = summary.get("column_mapping") or {}
    authority = str(section.get("expected_authority", "sample") or "sample").strip()
    if authority not in PARSE_AUTHORITIES:
        errors.append(
            f"I2 expected_authority={authority!r} 不合法；"
            f"允许: {sorted(PARSE_AUTHORITIES)}"
        )
    column_map = section.get("column_map") or {}
    if column_map and not isinstance(column_map, dict):
        errors.append("I2 column_map 必须是对象")
        column_map = {}
    if authority == "stop":
        return [*errors, "用户在 I2 选择停止核对标签；本轮不生成候选"]
    if authority == "manual":
        if not column_map:
            errors.append("I2 选择人工指定时必须在 column_map 写明列映射")
        else:
            for field, value in column_map.items():
                if str(summary_mapping.get(field, "")) != str(value):
                    errors.append(
                        f"I2 column_map[{field!r}]={value!r} 与当前解析结果 "
                        f"{summary_mapping.get(field)!r} 不一致；请重跑 prepare"
                    )
    elif authority == "current":
        expected_source = str(summary_mapping.get("expected_category", "") or "")
        current_source = str(summary_mapping.get("current_category", "") or "")
        if not column_map:
            errors.append(
                "I2 选择以当前分类为权威时，必须在 column_map 写明交换后的映射"
            )
        else:
            # prepare must already have been rerun with the swapped mapping:
            # after the swap, the summary's expected/current sources describe
            # the new roles, so the recorded map must match them exactly.
            for field, value in column_map.items():
                if str(summary_mapping.get(field, "")) != str(value):
                    errors.append(
                        f"I2 column_map[{field!r}]={value!r} 与当前解析结果 "
                        f"{summary_mapping.get(field)!r} 不一致；"
                        "请用 --column-map 交换 expected/current 后重跑 prepare"
                    )
            if expected_source and expected_source == current_source:
                errors.append(
                    "I2 选择以当前分类为权威时，两个分类列不能是同一列；"
                    "请在 --column-map 中指定不同的列并重跑 prepare"
                )
    elif column_map:
        for field, value in column_map.items():
            if str(summary_mapping.get(field, "")) != str(value):
                errors.append(
                    f"I2 column_map[{field!r}] 与当前解析结果不一致；请重跑 prepare"
                )

    type_mode = str(section.get("type_distribution", "") or "").strip()
    if type_mode:
        if type_mode not in PARSE_TYPE_MODES:
            errors.append(
                f"I2 type_distribution={type_mode!r} 不合法；"
                f"允许: {sorted(PARSE_TYPE_MODES)}"
            )
        elif type_mode == "stop":
            errors.append("用户在 I2 选择停止处理该类问题")
        elif type_mode == "follow_sample" and str(summary.get("problem_scope", "")) != "all":
            errors.append(
                "I2 选择按样本实际类型处理，但当前 problem_scope 不是 all；"
                "请重跑 prepare --problem-type all"
            )
        elif type_mode == "one_type":
            wanted = str(section.get("problem_type", "") or "").strip()
            if wanted not in {"miss", "diff", "extra"}:
                errors.append("I2 选择只处理一类时，problem_type 必须是 miss/diff/extra")
            elif str(summary.get("problem_scope", "")) != wanted:
                errors.append(
                    f"I2 指定只处理 {wanted}，但当前 problem_scope="
                    f"{summary.get('problem_scope')!r}；请重跑 prepare --problem-type {wanted}"
                )
    if section.get("problem_type"):
        wanted = str(section.get("problem_type", "")).strip()
        if wanted not in {"all", "miss", "diff", "extra"}:
            errors.append("I2 problem_type 必须是 miss/diff/extra/all")
        elif type_mode in {"", "keep_intake"} and str(summary.get("problem_scope", "")) != wanted:
            errors.append(
                f"I2 确认的 problem_type={wanted!r} 与当前解析 "
                f"{summary.get('problem_scope')!r} 不一致；请重跑 prepare"
            )
    return errors


def attribution_gate_errors(review_dir: Path) -> list[str]:
    """Validate I3 coverage for triggered clusters (all clusters in guided)."""
    review_path = Path(review_dir) / "attribution_review.json"
    if not review_path.exists():
        return []
    review = load_json_if_exists(review_path)
    clusters = [item for item in review.get("clusters") or [] if isinstance(item, dict)]
    triggered = [
        item for item in clusters
        if item.get("triggers")
    ]
    section = dialog_section(review_dir, "attribution")
    mode = interaction_mode(review_dir)
    required = bool(review.get("required")) or (
        mode == "guided" and bool(clusters)
    )
    if not required:
        if str(section.get("fallback", "")).strip() == "stop":
            return ["用户在 I3 选择停止；本轮不生成候选"]
        return []
    if mode == "minimal":
        heavy = {
            trigger
            for item in triggered
            for trigger in item.get("triggers") or []
        } & MINIMAL_ATTRIBUTION_TRIGGERS
        if not heavy:
            if str(section.get("fallback", "") or "").strip() in {
                "search_then_retry", "full_diagnosis", "stop",
            }:
                return ["I3 决策表示尚未定表或停止；本轮不生成候选"]
            return []
    if not section:
        return [
            "缺少 I3 归属/目标表确认；请先查看 attribution_review.json，"
            "用 AskUserQuestion 确认后写入 dialog_decisions.json#attribution"
        ]
    errors = _gate_stamp_errors(section, "attribution")
    errors += _fingerprint_errors(
        section, str(review.get("fingerprint", "")), "I3"
    )
    decisions = section.get("decisions")
    if not isinstance(decisions, list):
        return [*errors, "I3 decisions 必须是数组"]
    decision_map: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(decisions, start=1):
        if not isinstance(entry, dict):
            errors.append(f"I3 decisions[{index}] 必须是对象")
            continue
        cluster_id = str(entry.get("cluster_id", "") or "").strip()
        if not cluster_id:
            errors.append(f"I3 decisions[{index}] 缺少 cluster_id")
            continue
        if cluster_id in decision_map:
            errors.append(f"I3 decisions 中 {cluster_id} 重复出现")
            continue
        decision_map[cluster_id] = entry
        selection = str(entry.get("selection", "") or "").strip()
        if selection not in ATTRIBUTION_SELECTIONS:
            errors.append(
                f"I3 {cluster_id}: selection={selection!r} 不合法；"
                f"允许: {sorted(ATTRIBUTION_SELECTIONS)}"
            )
            continue
        if not str(entry.get("decided_at", "") or "").strip():
            errors.append(f"I3 {cluster_id}: 缺少 decided_at")
        target_file = str(entry.get("target_file", "") or "").strip()
        engine = str(entry.get("engine", "") or "").strip()
        if selection == "alternative":
            if not target_file or not engine:
                errors.append(
                    f"I3 {cluster_id}: 选择备选表时必须写 engine 和 target_file"
                )
                continue
            allowed = {
                (str(table.get("engine", "")), str(table.get("target_file", "")))
                for table in (
                    next(
                        (item for item in clusters
                         if item.get("cluster_id") == cluster_id),
                        {},
                    ).get("candidate_tables") or []
                )
            }
            if (engine, target_file) not in allowed:
                errors.append(
                    f"I3 {cluster_id}: {engine}/{target_file} 不在候选表列表中；"
                    "请重新查看 attribution_review.json"
                )
        elif selection == "recommended":
            recommended = (
                next(
                    (item for item in clusters if item.get("cluster_id") == cluster_id),
                    {},
                ).get("recommended") or {}
            )
            if target_file and target_file != str(recommended.get("target_file", "")):
                errors.append(
                    f"I3 {cluster_id}: recommended 决策的 target_file 与蓝图推荐不一致"
                )
            if engine and engine != str(recommended.get("engine", "")):
                errors.append(
                    f"I3 {cluster_id}: recommended 决策的 engine 与蓝图推荐不一致"
                )
    target_items = clusters if mode == "guided" else triggered
    for item in target_items:
        cluster_id = str(item.get("cluster_id", "") or "").strip()
        if cluster_id and cluster_id not in decision_map:
            errors.append(f"I3 缺少聚类 {cluster_id} 的目标表/归属决策")
    fallback = str(section.get("fallback", "") or "").strip()
    if fallback:
        if fallback not in ATTRIBUTION_FALLBACKS:
            errors.append(
                f"I3 fallback={fallback!r} 不合法；允许: {sorted(ATTRIBUTION_FALLBACKS)}"
            )
        elif fallback in {"search_then_retry", "full_diagnosis", "stop"}:
            errors.append(
                f"I3 fallback={fallback} 表示尚未定表；先完成检索/完整诊断并更新决策"
            )
    if any(
        str(entry.get("selection", "")).strip() == "evidence"
        for entry in decision_map.values()
    ) and not fallback:
        errors.append("I3 选择由证据决定时必须写 fallback")
    return errors


def precheck_gate_errors(review_dir: Path) -> list[str]:
    """Validate I6 before ``approve --build`` creates approvals."""
    precheck_path = Path(review_dir) / "precheck.json"
    if not precheck_path.exists():
        return [
            "缺少 I6 校验/影响面预审蓝图 precheck.json；"
            "请先运行 check --only reconcile 生成后再审批"
        ]
    precheck = load_json_if_exists(precheck_path)
    triggers = [str(value) for value in precheck.get("triggers") or []]
    section = dialog_section(review_dir, "precheck")
    mode = interaction_mode(review_dir)
    summary = precheck.get("summary") or {}
    approvable = int(summary.get("approvable", 0) or 0)
    hard_mismatch = "simulate_mismatch" in triggers
    required = bool(precheck.get("required")) or hard_mismatch
    if mode == "guided" and approvable > 0:
        required = True

    if not required:
        if str(section.get("decision", "")).strip() in {"revise", "degrade", "stop"}:
            return ["用户在 I6 选择不进入审批；请先处理候选"]
        if not section:
            return []
        # The precheck no longer triggers, but a decision written against an
        # older blueprint must not silently ride into approval after the
        # candidates changed.
        errors = _gate_stamp_errors(section, "precheck")
        errors += _fingerprint_errors(
            section, str(precheck.get("fingerprint", "")), "I6"
        )
        return errors

    if mode == "minimal" and not hard_mismatch:
        if str(section.get("decision", "")).strip() in {"revise", "degrade", "stop"}:
            return ["用户在 I6 选择不进入审批；请先处理候选"]
        return []

    if not section:
        return [
            "缺少 I6 校验/影响面预审；请先查看 precheck.json，"
            "用 AskUserQuestion 确认后写入 dialog_decisions.json#precheck"
        ]
    errors = _gate_stamp_errors(section, "precheck")
    errors += _fingerprint_errors(
        section, str(precheck.get("fingerprint", "")), "I6"
    )
    decision = str(section.get("decision", "") or "").strip()
    if decision not in PRECHECK_DECISIONS:
        errors.append(
            f"I6 decision={decision!r} 不合法；允许: {sorted(PRECHECK_DECISIONS)}"
        )
        return errors
    if decision != "continue":
        return [*errors, f"用户在 I6 选择 {decision}；当前不能进入阶段五审批"]
    impact = str(section.get("impact", "") or "").strip()
    if impact not in PRECHECK_IMPACTS:
        errors.append(
            f"I6 impact={impact!r} 不合法；允许: {sorted(PRECHECK_IMPACTS)}"
        )
    elif impact != "accept":
        errors.append(f"用户在 I6 不接受当前影响面（{impact}）；先修正或补样本")
    scope = str(section.get("verify_scope", "") or "").strip()
    if scope and scope not in PRECHECK_SCOPES:
        errors.append(
            f"I6 verify_scope={scope!r} 不合法；允许: {sorted(PRECHECK_SCOPES)}"
        )
    if hard_mismatch and section.get("acknowledge_mismatch") is not True:
        errors.append(
            "I6 存在影响面对账不一致，必须明确 acknowledge_mismatch=true 才能继续"
        )
    return errors


def candidate_row_hash(candidate: dict[str, Any]) -> str:
    """Hash the exact match logic covered by an I4 confirmation.

    The hash is checked again at ``approve --build`` so a candidate edited after
    the user saw its matching logic cannot ride an earlier confirmation into
    the rule table.
    """
    material = {
        "target_file": str(candidate.get("target_file", "")),
        "operation": str(candidate.get("operation", "")),
        "row": candidate.get("row") or {},
        "match_key": candidate.get("match_key") or {},
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _option_row(
    values: dict[str, Any],
    column: str,
    pattern: str,
    mode_column: str | None = None,
    mode_value: str = "regex",
) -> dict[str, Any]:
    """Build a partial row patch for one I4 option.

    Scalar values are stringified to mirror ``revise_candidates``: it stores
    every rule-column value as a string, so ``row_hash_after`` must be computed
    from the same shape or ``approve --build`` would reject a mechanically
    applied option.  Lists are left alone so ``revise`` can reject tables where
    a list-valued column is not valid.
    """
    row = {
        str(key): value if isinstance(value, list) else str(value)
        for key, value in values.items()
    }
    row[column] = pattern
    if mode_column:
        row[mode_column] = mode_value
    return row


def _regex_alternatives(pattern: str) -> tuple[str, str]:
    return f"^(?:{pattern})$", f"^(?:{pattern})"


def _keyword_regex_alternatives(pattern: str) -> tuple[str, str]:
    escaped = re.escape(pattern)
    return f"^(?:{escaped})$", f"^(?:{escaped})"


def match_review_options(
    spec: RuleFileSpec,
    candidate: dict[str, Any],
    validation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Concrete I4 options for one candidate.

    Each option is ``{"key", "label", "row", "requires_input", "input_hint",
    "note"}``.  ``row`` is a ready-to-use ``dialog_patch`` fragment when the
    option can be applied mechanically; input-driven options leave it ``None``
    so the agent asks for the value through AskUserQuestion's free-text field.

    Options are table-specific on purpose: never offer a regex rewrite on a
    table whose engine ignores regex (``all_other_credit``), and never offer an
    institution-row regex on gambling/rent (that layer only reads pipe variants).
    """
    validation = validation or {}
    operation = str(candidate.get("operation", "")).strip()
    raw_values = dict(candidate.get("row") or {})
    values = {str(key): value for key, value in raw_values.items()}
    column = spec.pattern_column
    pattern = str(
        validation.get("pattern") or candidate_pattern(candidate)
    ).strip()
    matched_mode = validation.get("matched_mode") or {}
    if not isinstance(matched_mode, dict):
        matched_mode = {}
    mode = str(
        raw_values.get("match_type") or matched_mode.get("match_type") or ""
    ).strip().lower()
    rule_type = str(
        raw_values.get("rule_type") or matched_mode.get("rule_type") or ""
    ).strip().lower()
    source = str(
        raw_values.get("source") or matched_mode.get("source") or ""
    ).strip().lower()

    options: list[dict[str, Any]] = []

    def add(
        key: str,
        label: str,
        row: dict[str, Any] | None = None,
        requires_input: bool = False,
        input_hint: str = "",
        note: str = "",
    ) -> None:
        options.append({
            "key": key,
            "label": label,
            "row": row,
            "requires_input": requires_input,
            "input_hint": input_hint,
            "note": note,
        })

    def add_keyword_choices(note: str) -> None:
        add("recommended", f"按建议：{pattern}")
        add(
            "adjust", "调整关键词/变体", requires_input=True,
            input_hint="输入更窄或更完整的关键词；该表不支持正则前缀/精确语法。",
            note=note,
        )
        add("reject", "拒绝该匹配逻辑")

    if operation == "delete":
        match_key = json.dumps(
            candidate.get("match_key") or {}, ensure_ascii=False, sort_keys=True
        )
        add("recommended", "按建议删除该行", note=f"match_key={match_key}")
        add(
            "custom", "更换 match_key", requires_input=True,
            input_hint="输入列名和值，必须恰好命中 1 行。",
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    if operation == "keyword_update":
        add("recommended", "按建议更新关键词")
        add(
            "adjust", "调整关键词", requires_input=True,
            input_hint="输入完整 keywords，或 add_keywords / remove_keywords。",
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    if spec.match_mode == "initial_variants":
        add("recommended", f"按建议商户变体：{pattern}")
        add(
            "adjust", "调整商户变体", requires_input=True,
            input_hint="输入要增加/移除的变体；关键词须大写、仅 [A-Z0-9 ]、≤50 个。",
            note="initial 最长全词胜出；变体太长会抢走更短商户。",
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    if spec.match_mode == "liability_homecar":
        add("recommended", "按建议门控条件", note=spec.note)
        add(
            "adjust", "调整 match_scope / amount_gt", requires_input=True,
            input_hint="输入 match_scope 或 amount_gt 的目标值。",
            note=spec.note,
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    if spec.match_mode == "all_other_credit":
        add_keyword_choices(
            "all_other_credit 只认 keyword；rule_type=regex 会被静默忽略。"
        )
        return options

    if spec.match_mode in {"word_boundary_ci", "substring", "liability_counterparty"}:
        add_keyword_choices(
            "该表是关键词/子串匹配；不支持在行内切换正则。"
        )
        return options

    if not pattern:
        add("recommended", "按建议（该候选没有可识别的 pattern）")
        add(
            "adjust", "补充匹配字段", requires_input=True,
            input_hint=f"输入 {column} 列的值。",
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    if spec.match_mode == "merchant_rule":
        if source == "institution":
            add(
                "recommended", f"按建议商户变体：{pattern}",
                note="institution 层是 | 分隔全词变体，不读取 match_type。",
            )
            add(
                "adjust", "调整商户变体", requires_input=True,
                input_hint="输入 | 分隔的商户变体；该层不支持正则改写。",
                note="institution 层优先于 rule 层；已进 merchant_kb 时 rule 层无效。",
            )
            add("reject", "拒绝该匹配逻辑")
            return options
        narrower, wider = _keyword_regex_alternatives(pattern)
        if mode == "regex":
            narrower, wider = _regex_alternatives(pattern)
        add(
            "recommended", f"按建议{('正则' if mode == 'regex' else '关键词')}：{pattern}",
            note=spec.note,
        )
        add(
            "narrower", "更窄：整串精确匹配",
            _option_row(values, column, narrower, "match_type", "regex"),
            note="转成 ^(?:...)$ 正则；先确认该表的文本预处理与大小写语义。",
        )
        add(
            "wider", "更宽：前缀匹配",
            _option_row(values, column, wider, "match_type", "regex"),
            note="转成 ^(?:...) 正则；会命中所有以该文本开头的行。",
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    if spec.match_mode == "dishonour":
        narrower, wider = _keyword_regex_alternatives(pattern)
        if rule_type == "regex":
            narrower, wider = _regex_alternatives(pattern)
        add("recommended", f"按建议：{pattern}", note=spec.note)
        add(
            "narrower", "更窄：整串精确匹配",
            _option_row(values, column, narrower, "rule_type", "regex"),
            note="dishonour regex 为 IGNORECASE；keyword 是子串，收紧后误伤更少。",
        )
        add(
            "wider", "更宽：前缀匹配",
            _option_row(values, column, wider, "rule_type", "regex"),
            note="dishonour regex 为 IGNORECASE；前缀会命中同前缀的其它文本。",
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    if spec.match_mode == "liability_dishonour":
        if rule_type == "regex":
            narrower, wider = _regex_alternatives(pattern)
            add("recommended", f"按建议正则：{pattern}", note=spec.note)
            add(
                "narrower", "更窄：整串精确匹配",
                _option_row(values, column, narrower),
                note="liability dishonour regex 区分大小写。",
            )
            add(
                "wider", "更宽：前缀匹配",
                _option_row(values, column, wider),
                note="liability dishonour regex 区分大小写。",
            )
        else:
            add("recommended", f"按建议关键词：{pattern}", note=spec.note)
            add(
                "adjust", "调整关键词", requires_input=True,
                input_hint="输入小写字面关键词；该表 keyword 按小写子串匹配。",
                note="keyword 与 regex 的大小写语义不同，不自动改写模式。",
            )
        add("reject", "拒绝该匹配逻辑")
        return options

    if spec.match_mode in {
        "transfer_regex", "income_regex", "fee_regex", "raw_regex_ci",
        "liability_credit_card",
    }:
        narrower, wider = _regex_alternatives(pattern)
        add("recommended", f"按建议正则：{pattern}", note=spec.note)
        add(
            "narrower", "更窄：整串精确匹配",
            _option_row(values, column, narrower),
            note=spec.note,
        )
        add(
            "wider", "更宽：前缀匹配",
            _option_row(values, column, wider),
            note=spec.note,
        )
        add("reject", "拒绝该匹配逻辑")
        return options

    # Unknown matcher mode: keep the safe, non-rewriting option set rather than
    # inventing a match type the engine may not support.
    add_keyword_choices(f"未识别的匹配模式 {spec.match_mode!r}；只建议原样关键词。")
    return options
