#!/usr/bin/env python3
"""Stage 3: turn evidence clusters into candidate seeds.

The proposer is deliberately conservative.  It reads the evidence's routing
decision (owner engine, current engine, ``routing.actions``), locates the rule
that actually fired, and emits the narrowest mechanical change it can justify:

* ``miss``  -> append a keyword/regex rule to the owner engine, or add a
  merchant keyword variant when the merchant already exists;
* ``diff``  -> narrow/remove the offending rule (initial keyword variant or
  delete) and append the owner-engine rule when routing asks for it;
* ``extra`` -> remove/delete the rules that produced the false positive; never
  append a classification rule.

Every seed is marked ``generated_by=propose_candidates.py`` and
``needs_review=true``.  Nothing is auto-confirmed; the agent still has to walk
the ownership decision tree and the user still has to approve the result.

    python propose_candidates.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001 [--validate] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

GENERIC_GAMBLING_WORDS = {
    "BET", "BETS", "BETTING", "CASINO", "LOTTERY", "LOTTO", "POKER",
    "POKIES", "SLOT", "SLOTS", "SPORTSBET", "GAMBLING", "WAGER", "KENO",
}
STRONG_WAGE_WORDS = {
    "SALARY", "SALARIES", "WAGE", "WAGES", "PAYROLL", "PAY", "PAYRUN",
    "EMPLOYER", "REMUNERATION",
}
CENTRELINK_WORDS = {
    "CENTRELINK", "PENSION", "ALLOWANCE", "FAMILY TAX BENEFIT", "FTB",
    "YOUTH ALLOWANCE", "AUSTUDY", "CARER PAYMENT", "NEWSTART",
}
STRUCTURAL_ENGINES = {"income", "liability", "transfer"}
META_KEYS = {"reason", "rationale", "note"}
# Tokens that make a phrase a poor merchant keyword.  They are common in
# transaction descriptions but do not identify who was paid.
GENERIC_KEYWORD_TOKENS = {
    "DIRECT", "DEBIT", "CREDIT", "PAYMENT", "PAYWAY", "TRANSFER", "VALUE",
    "DATE", "CARD", "FROM", "TO", "AU", "PTY", "LTD", "THE", "AND", "FOR",
    "REF", "REFERENCE", "ONLINE", "PURCHASE", "INV", "BILL", "ACCOUNT",
}
# These suffix/boilerplate tokens say nothing about the merchant and should
# never be the reason a longer phrase wins.
STRONG_GENERIC_TOKENS = {"AU", "PTY", "LTD", "THE", "AND", "FOR", "REF", "INV"}


# ── io ──────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def load_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


def load_scope(review_dir: Path) -> set[str]:
    path = review_dir / "target_scope.json"
    if not path.exists():
        return set()
    payload = load_json(path)
    selected: set[str] = set()
    for group in payload.get("selected_groups") or []:
        selected.update(str(value) for value in group.get("cluster_ids") or [])
    return selected


def _text(value: Any) -> str:
    return "" if value is None else str(value)


# ── initial KB access (streamed; merchant_kb is ~874k rows) ──────────────────

class InitialKB:
    """Stream merchant_kb rows by merchant name without loading the file."""

    def __init__(self, repo: Path):
        self.path = repo / "initial_engine" / "merchant_kb.csv"
        self._cache: dict[str, list[dict[str, str]]] = {}

    def prefetch(self, merchant_names: Iterable[str]) -> None:
        """Collect several merchant rows in one scan.

        A review with many initial candidates used to scan the 874k-row KB once
        per merchant.  Callers know the names before proposal generation, so
        one pass is enough; ``find`` still falls back to a scan for names that
        were not prefetched.
        """
        wanted = {
            _text(name).strip()
            for name in merchant_names
            if _text(name).strip()
        } - self._cache.keys()
        if not wanted or not self.path.exists():
            return
        found: dict[str, list[dict[str, str]]] = {name: [] for name in wanted}
        with self.path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for line, row in enumerate(reader, start=2):
                name = _text(row.get("merchant_name")).strip()
                if name not in wanted:
                    continue
                found[name].append(
                    {
                        "line": line,
                        "merchant_name": name,
                        "keywords": _text(row.get("keywords")),
                        "category": _text(row.get("category")),
                    }
                )
        self._cache.update(found)

    def find(self, merchant_name: str) -> list[dict[str, str]]:
        wanted = _text(merchant_name).strip()
        if wanted in self._cache:
            return self._cache[wanted]
        self.prefetch([wanted])
        return self._cache.get(wanted, [])


# ── rule location ───────────────────────────────────────────────────────────

def _first_sample_text(cluster: dict[str, Any]) -> str:
    samples = cluster.get("samples") or []
    return _text(samples[0].get("text")) if samples else ""


def _dr_cr(cluster: dict[str, Any]) -> str:
    values = {
        _text(sample.get("dr_cr")).strip().lower()
        for sample in cluster.get("samples") or []
        if _text(sample.get("dr_cr")).strip()
    }
    return values.pop() if len(values) == 1 else ""


def _keyword_score(value: str) -> tuple[float, bool, int, int]:
    words = value.split()
    alpha = sum(1 for word in words if any(char.isalpha() for char in word))
    numeric = sum(1 for word in words if any(char.isdigit() for char in word))
    generic = sum(
        1 for word in words
        if word in GENERIC_KEYWORD_TOKENS and word not in STRONG_GENERIC_TOKENS
    )
    strong_generic = sum(1 for word in words if word in STRONG_GENERIC_TOKENS)
    return (
        alpha - 1.5 * numeric - 0.5 * generic - 2.0 * strong_generic,
        2 <= len(words) <= 4,
        -len(words),
        len(value),
    )


def _pick_keyword(cluster: dict[str, Any]) -> str:
    hints = [str(value).strip() for value in cluster.get("keyword_hints") or []]
    hints = [
        value for value in hints
        if value and not value.isdigit() and value not in common.STOPWORDS
        and not all(token in common.STOPWORDS for token in value.split())
        and 3 <= len(value) <= 60
    ]
    if not hints:
        hints = common.candidate_keywords(_first_sample_text(cluster))
    if not hints:
        return ""
    return max(hints, key=_keyword_score)


def _merchant_guess(cluster: dict[str, Any]) -> str:
    keyword = _pick_keyword(cluster)
    if keyword:
        return " ".join(keyword.split()[:4])
    cleaned = common.clean_text(_first_sample_text(cluster))
    return " ".join(cleaned.split()[:4])


def _locate_rule(
    repo: Path,
    engine: str,
    hit: dict[str, Any],
    initial_kb: InitialKB,
) -> tuple[common.RuleFileSpec, common.RuleRow] | None:
    file_name = _text(hit.get("file"))
    spec = common.spec_for(engine, file_name)
    if spec is None or not spec.is_matcher:
        return None
    if spec.match_mode == "initial_variants":
        merchant = _text(hit.get("rule_id")).strip()
        if not merchant:
            keyword = _text(hit.get("pattern")).strip()
            for candidate in common.initial_keyword_hits(repo, keyword):
                if _text(candidate.get("keyword")).strip().upper() == keyword.upper():
                    merchant = _text(candidate.get("merchant")).strip()
                    if merchant:
                        break
        found = initial_kb.find(merchant)
        if len(found) != 1:
            return None
        row = found[0]
        return spec, common.RuleRow(
            engine=engine,
            relative_path=spec.relative_path,
            line_number=int(row.get("line", 0)),
            values={
                "merchant_name": row["merchant_name"],
                "keywords": row["keywords"],
                "category": row["category"],
            },
        )

    rows = common.read_rule_rows(repo, spec)
    wanted_id = _text(hit.get("rule_id")).strip()
    wanted_pattern = _text(hit.get("pattern")).strip()
    # ``rule_id_column`` is sometimes a category-ish column (dishonour's
    # rule_type, income's pattern_group) where the value repeats.  Only trust
    # it when it identifies exactly one row; otherwise fall back to the
    # pattern, which is the evidence's actual match.
    if wanted_id and spec.rule_id_column:
        by_id = [
            row for row in rows
            if row.get(spec.rule_id_column).strip() == wanted_id
        ]
        if len(by_id) == 1:
            return spec, by_id[0]
    if wanted_pattern:
        by_pattern = [row for row in rows if row.pattern.strip() == wanted_pattern]
        if len(by_pattern) == 1:
            return spec, by_pattern[0]
    return None


def _unique_match_key(
    repo: Path,
    spec: common.RuleFileSpec,
    row: common.RuleRow,
    initial_kb: InitialKB,
) -> dict[str, str] | None:
    if spec.match_mode == "initial_variants":
        name = row.get("merchant_name").strip()
        if name and len(initial_kb.find(name)) == 1:
            return {"merchant_name": name}
        return None

    candidates: list[dict[str, str]] = []
    if spec.rule_id_column and row.get(spec.rule_id_column).strip():
        candidates.append({spec.rule_id_column: row.get(spec.rule_id_column)})
    if row.pattern:
        candidates.append({spec.pattern_column: row.pattern})
    if row.get("counterparty") and row.pattern:
        candidates.append({"counterparty": row.get("counterparty"), spec.pattern_column: row.pattern})
    rows = common.read_rule_rows(repo, spec)
    for key in candidates:
        count = sum(
            1 for candidate in rows
            if all(candidate.values.get(name, "") == value for name, value in key.items())
        )
        if count == 1:
            return key
    return None


# ── candidate builders ──────────────────────────────────────────────────────

def _trace_for(
    cluster: dict[str, Any],
    target_file: str,
    method: str,
    matched_rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Draft the evidence -> config-table trace stored on every candidate.

    The agent still has to verify it against the real rule row; this only
    records where the seed came from so a wrong table is visible before
    approval.  ``method`` is one of ``routing.candidate_file_hint``,
    ``matched_rule``, ``matched_merchant``, ``engine_default`` or
    ``suggestion``.
    """
    routing = cluster.get("routing") or {}
    hint = routing.get("candidate_file_hint") or {}
    trace: dict[str, Any] = {
        "cluster_id": _text(cluster.get("cluster_id")),
        "problem_type": _text(
            cluster.get("problem_type") or routing.get("problem_type")
        ),
        "owner_engine": _text(routing.get("owner_engine")),
        "routing_status": _text(routing.get("status")),
        "routing_action": _text(routing.get("action")),
        "candidate_file_hint": _text(hint.get("file")),
        "target_file": target_file,
        "trace_method": method,
    }
    if routing.get("owner_priority") is not None:
        trace["engine_priority"] = routing.get("owner_priority")
    if matched_rule:
        trace["matched_rule"] = matched_rule
    return trace


def _matched_rule(spec: common.RuleFileSpec, row: common.RuleRow) -> dict[str, Any]:
    """Describe the physical rule row an edit/delete is aimed at."""
    return {
        "file": spec.relative_path,
        "line": row.line_number,
        "rule_id": row.get(spec.rule_id_column or ""),
        "pattern": row.pattern,
    }


def _base(
    cluster: dict[str, Any],
    *,
    engine: str,
    target_file: str,
    operation: str,
    category: str,
    reason: str,
    row: dict[str, Any] | None = None,
    match_key: dict[str, Any] | None = None,
    source_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if source_trace is None:
        hint = _text(
            (cluster.get("routing") or {}).get("candidate_file_hint", {}).get("file")
        )
        if operation == "suggestion":
            method = "suggestion"
        elif target_file and hint == target_file:
            method = "routing.candidate_file_hint"
        else:
            method = "engine_default"
        source_trace = _trace_for(cluster, target_file, method)
    candidate: dict[str, Any] = {
        "candidate_id": "",
        "problem_type": cluster.get("problem_type", ""),
        "engine": engine,
        "target_file": target_file,
        "operation": operation,
        "category": category,
        "reason": reason,
        "rationale": reason,
        "evidence_cluster_ids": [cluster.get("cluster_id", "")],
        "source_trace": source_trace,
        "generated_by": "propose_candidates.py",
        "needs_review": True,
        "status": "pending",
    }
    if row is not None:
        candidate["row"] = row
    if match_key:
        candidate["match_key"] = match_key
    return candidate


def _suggestion(
    cluster: dict[str, Any],
    engine: str,
    category: str,
    reason: str,
    target_file: str = "",
) -> dict[str, Any]:
    return _base(
        cluster,
        engine=engine,
        target_file=target_file,
        operation="suggestion",
        category=category,
        reason=reason,
    )


def _append_spec(repo: Path, engine: str, category: str, cluster: dict[str, Any]) -> common.RuleFileSpec | None:
    hint_file = _text((cluster.get("routing") or {}).get("candidate_file_hint", {}).get("file"))
    if hint_file:
        spec = common.spec_for(engine, hint_file)
        if spec and spec.appendable:
            return spec
    if engine == "transfer":
        target = (
            "transfer_engine/resources/transfer_internal_regex_rules.csv"
            if category == "Internal Transfer"
            else "transfer_engine/resources/transfer_external_high_confidence_rules.csv"
        )
        spec = common.spec_for(engine, target)
        return spec if spec and spec.appendable else None
    for spec in common.appendable_specs(engine):
        return spec
    return None


def _max_priority(repo: Path, spec: common.RuleFileSpec, below: int | None = None) -> int:
    values: list[int] = []
    for row in common.read_rule_rows(repo, spec):
        text = row.get("priority").strip()
        if text.isdigit():
            values.append(int(text))
    if below is not None:
        values = [value for value in values if value < below]
    return (max(values) if values else 0) + 10


def _filter_row(repo: Path, spec: common.RuleFileSpec, row: dict[str, Any]) -> dict[str, Any]:
    header = load_header(repo / spec.relative_path)
    return {key: str(value) for key, value in row.items() if key in header}


def propose_append(
    repo: Path,
    cluster: dict[str, Any],
    engine: str,
    category: str,
) -> list[dict[str, Any]]:
    """Append the narrowest rule for a miss/diff owner engine."""
    keyword = _pick_keyword(cluster)
    spec = _append_spec(repo, engine, category, cluster)
    if not keyword:
        return [_suggestion(cluster, engine, category, "没有可用的文本关键词，需人工取证")]
    if spec is None:
        return [_suggestion(cluster, engine, category, f"{engine} 没有可追加的分类器表")]
    if not common.category_allowed(repo, category, engine):
        return [
            _suggestion(
                cluster, engine, category,
                f"分类 {category!r} 不在 {engine} 的目录归属内，需先复核归属",
            )
        ]

    dr_cr = _dr_cr(cluster)
    rule_name = f"propose_{cluster.get('cluster_id', '')}".lower()
    row: dict[str, Any]

    if engine == "catch_all":
        row = {
            "rule_name": rule_name,
            "category": category,
            "pattern": keyword,
            "match_type": "keyword",
            "confidence": "0.6",
        }
        reason = f"漏识别：向 catch_all 追加通用描述词 {keyword!r}；必须确认它不是商户/品牌名"
    elif engine == "all_other_credit":
        row = {"rule_type": "keyword", "pattern": keyword, "required_terms": ""}
        reason = f"入账漏识别：向 all_other_credit 追加 keyword {keyword!r}（该引擎只处理 credit）"
        if dr_cr and dr_cr != "credit":
            return [_suggestion(cluster, engine, category, f"样本 dr_cr={dr_cr!r}，all_other_credit 只处理 credit")]
    elif engine == "dishonour":
        row = {"rule_type": "keyword", "pattern": keyword, "required_terms": ""}
        reason = f"漏识别：向 dishonour 追加 keyword {keyword!r}（大小写不敏感子串）"
    elif engine == "fee":
        priority = _max_priority(repo, spec, below=999)
        row = {
            "priority": str(priority),
            "rule_name": rule_name,
            "category": "fee" if category == "Fees" else category,
            "pattern": "^" + re.escape(keyword),
            "counterparty": "",
            "match_type": "regex",
            "zero_amount_reject": "",
            "description": f"propose from {cluster.get('cluster_id', '')}",
            "unclassified_only": "",
            "dr_cr": dr_cr,
        }
        reason = f"费用漏识别：向 fee 追加锚定 regex ^({keyword})，priority={priority}"
    elif engine == "initial":
        return propose_initial(repo, cluster, category)
    elif engine == "transfer":
        priority = _max_priority(repo, spec)
        row = {
            "priority": str(priority),
            "rule_name": rule_name,
            "pattern": "^" + re.escape(keyword.lower()),
            "dr_cr": dr_cr,
            "description": f"propose from {cluster.get('cluster_id', '')}",
        }
        if "category" in load_header(repo / spec.relative_path):
            row["category"] = "transfer"
        reason = f"转账漏识别：向 {spec.relative_path} 追加小写 regex ^({keyword.lower()})"
    elif engine == "gambling":
        generic = any(
            token in GENERIC_GAMBLING_WORDS
            for token in common.clean_text(_first_sample_text(cluster)).split()
        )
        if generic:
            row = {
                "rule_name": rule_name,
                "category": "Gambling",
                "pattern": keyword,
                "match_type": "keyword",
                "confidence": "0.8",
                "counterparty": "",
                "source": "rule",
            }
            reason = f"博彩通用词漏识别：向 gambling rule 层追加 {keyword!r}"
        else:
            merchant = _merchant_guess(cluster)
            row = {
                "rule_name": rule_name,
                "category": "Gambling",
                "pattern": keyword,
                "match_type": "keyword",
                "confidence": "0.95",
                "counterparty": merchant,
                "source": "institution",
            }
            reason = f"博彩商户漏识别：向 gambling institution 层追加 {merchant!r}（置信度惯例 0.95）"
    elif engine == "rent":
        merchant = _merchant_guess(cluster)
        row = {
            "rule_name": rule_name,
            "category": "Rent",
            "pattern": keyword,
            "match_type": "keyword",
            "confidence": "0.95",
            "counterparty": merchant,
            "source": "institution",
        }
        reason = f"租金商户漏识别：向 rent institution 层追加 {merchant!r}"
    elif engine == "income":
        raw_upper = _first_sample_text(cluster).upper()
        if category == "Centrelink":
            group = "centrelink"
            words = CENTRELINK_WORDS
        elif category == "Wages":
            group = "strong_wage"
            words = STRONG_WAGE_WORDS
        else:
            return [
                _suggestion(
                    cluster, engine, category,
                    f"income 的 {category!r} 不是强关键词可直接生成的分类；需人工设计 pattern_group",
                )
            ]
        found = [
            word for word in words
            if re.search(rf"(?<![A-Z]){re.escape(word)}(?![A-Z])", raw_upper)
        ]
        if found:
            strong = max(found, key=len)
        else:
            return [
                _suggestion(
                    cluster, engine, category,
                    "income 不是纯文本匹配；样本未出现强工资/福利关键词，需人工设计 pattern_group",
                )
            ]
        pattern = rf"\b{re.escape(strong)}\b"
        row = {
            "pattern_group": group,
            "pattern": pattern,
            "match_type": "regex",
            "description": f"propose from {cluster.get('cluster_id', '')}",
        }
        reason = f"收入漏识别：向 income 追加 {group} regex {pattern}（在 seams 文本上匹配）"
    elif engine == "liability":
        return propose_liability(repo, cluster, category)
    else:
        return [_suggestion(cluster, engine, category, f"未知引擎 {engine!r}，需人工归属")]

    return [
        _base(
            cluster,
            engine=engine,
            target_file=spec.relative_path,
            operation="append",
            category=category,
            reason=reason,
            row=_filter_row(repo, spec, row),
        )
    ]


def propose_initial(
    repo: Path,
    cluster: dict[str, Any],
    category: str,
) -> list[dict[str, Any]]:
    keyword = _pick_keyword(cluster)
    if not keyword:
        return [_suggestion(cluster, "initial", category, "没有可用的商户关键词，需人工取证")]

    # Existing merchant hit: add the missing variant instead of creating a
    # duplicate merchant row.
    hits = common.initial_keyword_hits(repo, _first_sample_text(cluster))
    same_category = [
        hit for hit in hits
        if _text(hit.get("category")).strip() == category
        and _text(hit.get("merchant")).strip()
    ]
    for hit in same_category:
        merchant = _text(hit.get("merchant")).strip()
        existing_keyword = _text(hit.get("keyword")).strip()
        if existing_keyword.upper() == keyword.upper():
            continue
        return [
            _base(
                cluster,
                engine="initial",
                target_file="initial_engine/merchant_kb.csv",
                operation="keyword_update",
                category=category,
                reason=(
                    f"漏识别：已有商户 {merchant!r}（分类 {category}）未命中该文本，"
                    f"补充关键词变体 {keyword!r}"
                ),
                row={"add_keywords": [keyword]},
                match_key={"merchant_name": merchant},
                source_trace=_trace_for(
                    cluster,
                    "initial_engine/merchant_kb.csv",
                    "matched_merchant",
                    {
                        "file": "initial_engine/merchant_kb.csv",
                        "rule_id": merchant,
                        "pattern": existing_keyword,
                    },
                ),
            )
        ]
    if same_category:
        return [
            _suggestion(
                cluster, "initial", category,
                "文本已命中正确分类的现有商户，说明漏识别来自后续引擎覆盖/守卫，"
                "不是 initial 规则缺失；不要重复追加关键词",
            )
        ]

    # A keyword already claims the text under another category: do not create
    # a second merchant with the same keyword.
    if hits:
        return [
            _suggestion(
                cluster, "initial", category,
                "文本已命中既有商户关键词但分类不同："
                + "; ".join(
                    f"{hit.get('merchant')}={hit.get('category')}" for hit in hits[:3]
                ),
            )
        ]

    merchant = _merchant_guess(cluster)
    if not merchant:
        return [_suggestion(cluster, "initial", category, "无法推断商户名，需人工录入")]
    row = {"merchant_name": merchant, "keywords": keyword, "category": category}
    return [
        _base(
            cluster,
            engine="initial",
            target_file="initial_engine/merchant_kb.csv",
            operation="append",
            category=category,
            reason=(
                f"漏识别：新建商户 {merchant!r}，关键词 {keyword!r}；"
                "请确认商户名与关键词均来自样本文本"
            ),
            row=row,
        )
    ]


def propose_liability(
    repo: Path,
    cluster: dict[str, Any],
    category: str,
) -> list[dict[str, Any]]:
    keyword = _pick_keyword(cluster)
    merchant = _merchant_guess(cluster)
    hint = (cluster.get("routing") or {}).get("candidate_file_hint") or {}
    target_file = _text(hint.get("file"))
    spec = common.spec_for("liability", target_file) if target_file else None
    if spec is None or not spec.appendable:
        return [
            _suggestion(
                cluster, "liability", category,
                "liability 多表多格式，证据未定位到具体子表；需人工选择 target_file",
            )
        ]
    if not keyword:
        return [_suggestion(cluster, "liability", category, "没有可用关键词，需人工取证")]

    header = load_header(repo / spec.relative_path)
    dr_cr = _dr_cr(cluster) or "credit"
    rule_id = f"PROPOSE_{cluster.get('cluster_id', '')}"
    row: dict[str, Any] = {}
    if "priority" in header:
        row["priority"] = str(_max_priority(repo, spec))
    if "rule_id" in header:
        row["rule_id"] = rule_id
    if "rule_name" in header:
        row["rule_name"] = f"propose_{cluster.get('cluster_id', '')}".lower()
    if "counterparty" in header:
        row["counterparty"] = merchant
    if "product_type" in header:
        # Never copy the category into product_type: liability derives the
        # final bscat from product_type.  Leave it blank for the agent to fill
        # from the table's existing values when the evidence supports a choice.
        row["product_type"] = ""
    if "target_field" in header:
        row["target_field"] = "is_home_loan" if "Home" in category else "is_car_loan"
    if "match_scope" in header:
        row["match_scope"] = "text"
    if "match_type" in header:
        if spec.relative_path.endswith(("credit_card_rules.csv", "overdrawn_rules.csv")):
            row["match_type"] = "regex"
        else:
            row["match_type"] = "keyword"
    if "account_type" in header:
        row["account_type"] = "-" if spec.relative_path.endswith("credit_card_rules.csv") else "*"
    if "bank" in header:
        row["bank"] = "*"
    if "dr_cr" in header:
        row["dr_cr"] = dr_cr
    if "min_prefix_len" in header:
        row["min_prefix_len"] = "0"
    if "exclude_pattern" in header:
        row["exclude_pattern"] = ""
    if "amount_gt" in header:
        row["amount_gt"] = ""
    if "enabled" in header:
        row["enabled"] = "1"
    if "note" in header:
        row["note"] = f"propose from {cluster.get('cluster_id', '')}"
    if "description" in header:
        row["description"] = f"propose from {cluster.get('cluster_id', '')}"
    if "required_terms" in header:
        row["required_terms"] = ""
    if "rule_type" in header:
        row["rule_type"] = "keyword"
    if spec.relative_path.endswith("credit_card_rules.csv"):
        row["keyword"] = re.escape(keyword)
    elif spec.relative_path.endswith("overdrawn_rules.csv"):
        row["pattern"] = re.escape(keyword)
    elif "keyword" in header:
        row["keyword"] = keyword
    elif "pattern" in header:
        row["pattern"] = keyword

    label = "不一致" if _problem_type(cluster) == "diff" else "漏识别"
    candidate = _base(
        cluster,
        engine="liability",
        target_file=spec.relative_path,
        operation="append",
        category=category,
        reason=(
            f"liability {label}：向 {spec.relative_path} 追加 {keyword!r}；"
            "liability 为结构性高风险，必须逐条确认"
        ),
        row=_filter_row(repo, spec, row),
        source_trace=_trace_for(
            cluster,
            spec.relative_path,
            "routing.candidate_file_hint",
        ),
    )
    return [candidate]


# ── diff / extra: change the rule that actually fired ──────────────────────

def _current_hits(cluster: dict[str, Any], engine: str) -> list[dict[str, Any]]:
    hits = list(cluster.get("matching_current_rules") or [])
    if engine == (cluster.get("routing") or {}).get("owner_engine"):
        hits.extend(cluster.get("matching_owner_rules") or [])
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = (
            _text(hit.get("file")),
            _text(hit.get("rule_id")),
            _text(hit.get("pattern")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def propose_rule_change(
    repo: Path,
    cluster: dict[str, Any],
    engine: str,
    hit: dict[str, Any],
    initial_kb: InitialKB,
) -> list[dict[str, Any]]:
    problem_type = _text(cluster.get("problem_type"))
    located = _locate_rule(repo, engine, hit, initial_kb)
    if located is None:
        return [
            _suggestion(
                cluster, engine, _text(cluster.get("current_category")),
                f"已命中 {engine} 规则但无法唯一定位到行，需人工排查: "
                f"{hit.get('file')} / {hit.get('pattern')}",
                _text(hit.get("file")),
            )
        ]
    spec, row = located
    trace = _trace_for(
        cluster,
        spec.relative_path,
        "matched_rule",
        _matched_rule(spec, row),
    )

    if engine == "initial":
        merchant = row.get("merchant_name").strip()
        keywords = common.split_initial_keywords(row.get("keywords"))
        category = row.get("category", "")
        cleaned = common.clean_text(_first_sample_text(cluster))
        matching = [
            keyword for keyword in keywords
            if common.whole_word_anywhere(keyword, cleaned)
        ]
        if not matching:
            matched = _text(hit.get("pattern")).strip()
            if matched in keywords:
                matching = [matched]

        if problem_type == "extra":
            # If every variant on this merchant row matches, deleting only one
            # leaves another variant classifying the same text.  Remove the
            # row instead.
            if matching and len(matching) == len(keywords):
                return [
                    _base(
                        cluster,
                        engine="initial",
                        target_file="initial_engine/merchant_kb.csv",
                        operation="delete",
                        category=category,
                        reason=(
                            f"多识别：商户 {merchant!r} 的全部关键词 "
                            f"{keywords!r} 都命中该文本，删除整个商户行；"
                            "请确认没有合法交易依赖它"
                        ),
                        match_key={"merchant_name": merchant},
                        source_trace=trace,
                    )
                ]
            if matching and len(keywords) > 1:
                return [
                    _base(
                        cluster,
                        engine="initial",
                        target_file="initial_engine/merchant_kb.csv",
                        operation="keyword_update",
                        category=category,
                        reason=(
                            f"多识别：商户 {merchant!r} 的关键词 {matching!r} "
                            "命中了该文本；删除这些变体（保留其他关键词）"
                        ),
                        row={"remove_keywords": matching},
                        match_key={"merchant_name": merchant},
                        source_trace=trace,
                    )
                ]
            return [
                _suggestion(
                    cluster, engine, category,
                    f"商户 {merchant!r} 的关键词无法安全收窄（未能定位命中的变体），"
                    "需人工核对",
                    spec.relative_path,
                )
            ]

        expected = _text(cluster.get("expected_category")).strip()
        if expected and expected != category and common.category_allowed(
            repo, expected, "initial"
        ):
            return [
                _base(
                    cluster,
                    engine="initial",
                    target_file="initial_engine/merchant_kb.csv",
                    operation="edit",
                    category=expected,
                    reason=(
                        f"不一致：商户 {merchant!r} 当前分类 {category!r}，"
                        f"命中关键词 {matching or keywords!r}；"
                        f"把该商户行的 category 改为 {expected!r}。"
                        "这会改变该商户的全部交易，需逐条确认影响面"
                    ),
                    row={"category": expected},
                    match_key={"merchant_name": merchant},
                    source_trace=trace,
                )
            ]
        return [
            _suggestion(
                cluster, engine, category,
                f"商户 {merchant!r} 的期望分类 {expected!r} 不是 initial 可输出分类，"
                "需人工复核归属",
                spec.relative_path,
            )
        ]

    if engine in STRUCTURAL_ENGINES and problem_type != "extra":
        return [
            _suggestion(
                cluster, engine, _text(cluster.get("current_category")),
                f"{engine} 属于结构性引擎（行为阈值/层级优先级/多表），"
                "diff 场景不能自动删除；需人工收窄并逐条确认",
                spec.relative_path,
            )
        ]

    if spec.rule_id_column and row.get(spec.rule_id_column) == "fee_keyword_catchall":
        return [
            _suggestion(
                cluster, engine, _text(cluster.get("current_category")),
                "命中 fee 通用兜底规则 fee_keyword_catchall（unclassified_only）；"
                "直接删除会导致大量费用漏识别，需人工收窄",
                spec.relative_path,
            )
        ]

    match_key = _unique_match_key(repo, spec, row, initial_kb)
    if match_key is None:
        return [
            _suggestion(
                cluster, engine, _text(cluster.get("current_category")),
                f"无法为 {spec.relative_path} 生成唯一 match_key（存在重复行），"
                "需人工指定",
                spec.relative_path,
            )
        ]
    return [
        _base(
            cluster,
            engine=engine,
            target_file=spec.relative_path,
            operation="delete",
            category=_text(cluster.get("current_category")),
            reason=(
                f"{'多识别' if problem_type == 'extra' else '不一致'}："
                f"删除命中规则 {row.pattern!r}；删除后需确认没有同引擎其他规则接管"
            ),
            match_key=match_key,
            source_trace=trace,
        )
    ]


# ── cluster orchestration ───────────────────────────────────────────────────

def _problem_type(cluster: dict[str, Any]) -> str:
    """Return miss/extra/diff, deriving it for evidence written by older runs.

    Current evidence.json carries ``problem_type`` on every cluster.  Probes
    created before that field existed only have expected/current categories, so
    fall back to the same inference the parser uses.
    """
    explicit = _text(
        cluster.get("problem_type")
        or (cluster.get("routing") or {}).get("problem_type")
    ).strip().lower()
    if explicit in {"miss", "extra", "diff"}:
        return explicit

    expected = _text(cluster.get("expected_category")).strip()
    current = _text(cluster.get("current_category")).strip()
    expected_blank = not expected or common.expected_is_unclassified(expected)
    current_blank = (
        not current
        or current.lower() in common.UNCLASSIFIED_VALUES
        or current.lower() in {"unclassified", "unknown"}
    )
    if expected_blank and current_blank:
        return "unknown"
    if expected_blank:
        return "extra"
    if current_blank:
        return "miss"
    if expected != current:
        return "diff"
    return "unknown"


def resolve_engine(engine: str, category: str) -> str:
    """Apply the ownership decision tree's explicit engine preferences."""
    if category == "Gambling":
        return "gambling"
    if category == "Rent":
        return "rent"
    if category == "Fees":
        return "fee"
    if category == "All Other Credits":
        return "all_other_credit"
    return engine


def propose_cluster(
    repo: Path,
    cluster: dict[str, Any],
    initial_kb: InitialKB,
) -> list[dict[str, Any]]:
    problem_type = _problem_type(cluster)
    cluster = {**cluster, "problem_type": problem_type}
    routing = cluster.get("routing") or {}
    expected = _text(cluster.get("expected_category"))
    owner = _text(routing.get("owner_engine"))
    current = _text(cluster.get("current_engine") or routing.get("current_engine"))
    action = _text(routing.get("action")).strip()
    actions = [str(value) for value in routing.get("actions") or []]
    if not actions and action:
        actions = [action]

    if problem_type == "extra":
        engine = current or _text(routing.get("current_engine"))
        if not engine or engine in {"unclassified", "unknown"}:
            return [
                _suggestion(
                    cluster, "", "", "多识别行没有可定位的当前引擎，需人工排查"
                )
            ]
        hits = _current_hits(cluster, engine)
        if not hits:
            return [
                _suggestion(
                    cluster, engine, _text(cluster.get("current_category")),
                    "多识别聚类未定位到命中规则，需人工排查",
                )
            ]
        proposals: list[dict[str, Any]] = []
        for hit in hits[:4]:
            proposals.extend(propose_rule_change(repo, cluster, engine, hit, initial_kb))
        return proposals

    if not expected:
        return [
            _suggestion(cluster, owner, "", "缺少期望分类，无法生成候选")
        ]

    engine = resolve_engine(owner, expected)
    proposals = []

    # diff: only touch the current rule when routing actually asks for an edit
    # or narrowing.  When the owner runs later (feasible_add), adding the owner
    # rule is enough; the later engine overwrites the current claim.
    fix_current = problem_type == "diff" and (
        action in {"edit_current_rule", "narrow_current_rule", "delete_current_rule"}
        or _text(routing.get("status")).strip() == "same_owner_wrong_category"
    )
    if fix_current:
        current_engine = current or owner
        for hit in _current_hits(cluster, current_engine)[:3]:
            proposals.extend(
                propose_rule_change(repo, cluster, current_engine, hit, initial_kb)
            )

    if problem_type == "miss":
        should_append = True
    else:
        should_append = (
            action == "append_owner_rule"
            or _text(routing.get("status")).strip() == "feasible_add"
        )
    if should_append and engine:
        proposals.extend(propose_append(repo, cluster, engine, expected))
    elif not engine:
        proposals.append(
            _suggestion(cluster, "", expected, "目录中没有归属引擎，需先处理 catalog 归属")
        )
    elif problem_type == "diff" and not proposals:
        proposals.append(
            _suggestion(
                cluster, engine, expected,
                "路由结论不允许直接追加归属规则；需先处理当前规则或跨引擎守卫",
            )
        )
    elif not proposals:
        proposals.append(
            _suggestion(
                cluster, engine, expected,
                f"问题类型 {problem_type!r} 无法自动生成候选，需人工判断",
            )
        )
    return proposals


# ── merge / write ───────────────────────────────────────────────────────────

def proposal_key(candidate: dict[str, Any]) -> tuple[str, ...]:
    operation = _text(candidate.get("operation"))
    target = _text(candidate.get("target_file"))
    engine = _text(candidate.get("engine"))
    if operation == "delete":
        return (
            operation, engine, target,
            json.dumps(candidate.get("match_key") or {}, sort_keys=True, default=str),
        )
    if operation in {"edit", "keyword_update"}:
        return (
            operation, engine, target,
            json.dumps(candidate.get("match_key") or {}, sort_keys=True, default=str),
            json.dumps(candidate.get("row") or {}, sort_keys=True, default=str),
        )
    if operation == "append":
        return (
            operation, engine, target,
            _text(candidate.get("category")), common.candidate_pattern(candidate),
        )
    return (
        operation, engine, target, _text(candidate.get("category")),
        _text(candidate.get("reason")),
    )


def _edit_identity(candidate: dict[str, Any]) -> tuple[str, str]:
    return (
        _text(candidate.get("target_file")),
        json.dumps(candidate.get("match_key") or {}, sort_keys=True, default=str),
    )


def coalesce_proposals(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One candidate per physical row change.

    Several evidence hits can point at the same initial merchant (two keyword
    variants) or the same rule (two regex alternatives).  Emitting one
    candidate per hit would trip the validator's cross-candidate collision
    check, so merge them into a single change.
    """
    merged: list[dict[str, Any]] = []
    by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    by_edit: dict[tuple[str, str], dict[str, Any]] = {}
    for proposal in proposals:
        operation = _text(proposal.get("operation"))
        if operation == "edit":
            identity = _edit_identity(proposal)
            target = by_edit.get(identity)
            if target is None:
                by_edit[identity] = proposal
                merged.append(proposal)
            elif (target.get("row") or {}) == (proposal.get("row") or {}):
                for cluster_id in proposal.get("evidence_cluster_ids") or []:
                    clusters = target.setdefault("evidence_cluster_ids", [])
                    if cluster_id and cluster_id not in clusters:
                        clusters.append(cluster_id)
            else:
                merged.append(
                    _as_suggestion(
                        proposal,
                        "同一行出现多个不同的编辑建议；已保留第一版，"
                        f"本条与 {target.get('candidate_id') or '另一条编辑'} 冲突，"
                        "需人工选择后再提交",
                    )
                )
            continue
        # ``edit`` carries a complete replacement row; merging two edits would
        # silently keep only the first one, so it is handled above.
        if operation not in {"delete", "keyword_update"}:
            merged.append(proposal)
            continue
        target_file, key_json = _edit_identity(proposal)
        key = (operation, target_file, key_json)
        target = by_identity.get(key)
        if target is None:
            by_identity[key] = proposal
            merged.append(proposal)
            continue
        for field in ("add_keywords", "remove_keywords"):
            values = (proposal.get("row") or {}).get(field) or []
            if not values:
                continue
            target_rows = target.setdefault("row", {})
            bucket = target_rows.setdefault(field, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
        for cluster_id in proposal.get("evidence_cluster_ids") or []:
            clusters = target.setdefault("evidence_cluster_ids", [])
            if cluster_id and cluster_id not in clusters:
                clusters.append(cluster_id)
        if proposal.get("reason") and proposal["reason"] not in target.get("reason", ""):
            target["reason"] = f"{target.get('reason', '')}；{proposal['reason']}".strip("；")
            target["rationale"] = target["reason"]
    return merged


def _as_suggestion(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    suggestion = {
        key: value for key, value in candidate.items()
        if key not in {"row", "match_key"}
    }
    suggestion["operation"] = "suggestion"
    suggestion["reason"] = reason
    suggestion["rationale"] = reason
    return suggestion


def resolve_keyword_updates(
    proposals: list[dict[str, Any]],
    initial_kb: InitialKB,
) -> list[dict[str, Any]]:
    """Turn no-op / empty-result keyword updates into safe alternatives."""
    resolved: list[dict[str, Any]] = []
    for proposal in proposals:
        if _text(proposal.get("operation")) != "keyword_update":
            resolved.append(proposal)
            continue
        merchant = _text((proposal.get("match_key") or {}).get("merchant_name")).strip()
        rows = initial_kb.find(merchant)
        if len(rows) != 1:
            resolved.append(proposal)
            continue
        try:
            _, change = common.merge_initial_keywords(
                rows[0]["keywords"], proposal.get("row") or {}
            )
        except ValueError as exc:
            if "关键词为空" in str(exc) and proposal.get("problem_type") == "extra":
                delete = {**proposal, "operation": "delete"}
                delete.pop("row", None)
                delete["reason"] = (
                    f"{proposal.get('reason', '')}；删除后关键词为空，改为删除商户行"
                )
                delete["rationale"] = delete["reason"]
                resolved.append(delete)
            else:
                resolved.append(
                    _as_suggestion(
                        proposal,
                        f"关键词更新无法安全执行（{exc}）；需人工处理",
                    )
                )
            continue
        if not change["added"] and not change["removed"]:
            resolved.append(
                _as_suggestion(
                    proposal,
                    "关键词更新没有实际变化（命中的变体可能已被删除）；"
                    "请重新取证，不要重复提交",
                )
            )
            continue
        resolved.append(proposal)
    return resolved


def merge_proposals(
    existing: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Merge by semantic key; never overwrite an existing candidate."""
    by_key = {proposal_key(candidate): candidate for candidate in existing}
    existing_edits: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in existing:
        if _text(candidate.get("operation")) in {"edit", "delete", "keyword_update"}:
            existing_edits.setdefault(_edit_identity(candidate), candidate)

    new: list[dict[str, Any]] = []
    skipped: list[str] = []
    for proposal in proposals:
        key = proposal_key(proposal)
        if key in by_key:
            target = by_key[key]
            clusters = target.setdefault("evidence_cluster_ids", [])
            for cluster_id in proposal.get("evidence_cluster_ids") or []:
                if cluster_id and cluster_id not in clusters:
                    clusters.append(cluster_id)
            skipped.append(
                f"{proposal.get('operation')} {proposal.get('target_file')} "
                f"已存在为 {target.get('candidate_id')}"
            )
            continue
        if _text(proposal.get("operation")) in {"edit", "delete", "keyword_update"}:
            other = existing_edits.get(_edit_identity(proposal))
            if other is not None:
                skipped.append(
                    f"{proposal.get('operation')} {proposal.get('target_file')} "
                    f"与已存在候选 {other.get('candidate_id')} 修改同一行；"
                    "请先 revise 或拒绝旧候选"
                )
                continue
            existing_edits[_edit_identity(proposal)] = proposal
        by_key[key] = proposal
        new.append(proposal)
    return new, skipped


def assign_ids(new: list[dict[str, Any]], existing: list[dict[str, Any]]) -> None:
    used = {_text(candidate.get("candidate_id")) for candidate in existing}
    counter = 1
    for candidate in new:
        while f"P{counter:04d}" in used:
            counter += 1
        candidate["candidate_id"] = f"P{counter:04d}"
        used.add(candidate["candidate_id"])
        counter += 1


# ── main ────────────────────────────────────────────────────────────────────

def _attribution_context(
    review_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """Load the I3 blueprint, its decision section and a resolved lookup."""
    review = common.load_json_if_exists(review_dir / "attribution_review.json")
    section = common.dialog_section(review_dir, "attribution")
    by_cluster = {
        str(item.get("cluster_id", "")): item
        for item in review.get("clusters") or []
        if isinstance(item, dict)
    }
    decisions: dict[str, dict[str, Any]] = {}
    for entry in section.get("decisions") or []:
        if not isinstance(entry, dict):
            continue
        cluster_id = str(entry.get("cluster_id", "")).strip()
        if not cluster_id:
            continue
        resolved = dict(entry)
        cluster = by_cluster.get(cluster_id) or {}
        recommended = cluster.get("recommended") or {}
        selection = str(resolved.get("selection", "") or "").strip()
        if selection == "recommended":
            resolved.setdefault("engine", str(recommended.get("engine", "") or ""))
            resolved.setdefault(
                "target_file", str(recommended.get("target_file", "") or "")
            )
        decisions[cluster_id] = resolved
    return review, section, decisions


def _rescue_attribution_decisions(
    decisions: dict[str, dict[str, Any]],
    proposals: list[dict[str, Any]],
    review: dict[str, Any],
    section: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Keep only decisions that resolve ``engine_default`` drafts.

    When I3 no longer triggers, an old decision must not filter new drafts.
    ``engine_default`` drafts still need an explicit table choice, but that
    rescue decision must match the current blueprint fingerprint; otherwise
    the user has to confirm I3 again.
    """
    rescue_clusters = {
        str(cluster_id)
        for proposal in proposals
        if proposal.get("operation") in common.RULE_OPERATIONS
        and str((proposal.get("source_trace") or {}).get("trace_method", ""))
        == "engine_default"
        for cluster_id in proposal.get("evidence_cluster_ids") or []
    }
    rescued = {
        cluster_id: decision
        for cluster_id, decision in decisions.items()
        if cluster_id in rescue_clusters
    }
    if rescued:
        expected = str(review.get("fingerprint", "") or "").strip()
        actual = str(section.get("fingerprint", "") or "").strip()
        if not expected or actual != expected:
            raise SystemExit(
                "I3 归属蓝图缺失或已更新，旧决策不能用于本轮 engine_default 草稿；"
                "请重新查看 attribution_review.json 并确认目标表"
            )
    return rescued


def _apply_attribution_decisions(
    proposals: list[dict[str, Any]],
    review: dict[str, Any],
    section: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Filter/downgrade drafts according to I3.

    A stopped cluster is dropped.  ``evidence`` keeps the evidence-backed
    draft; when the user's fallback is ``suggestion`` it is downgraded instead
    of writing a table the evidence cannot justify.  ``recommended`` and
    ``alternative`` keep only the chosen physical table.
    """
    fallback = str(section.get("fallback", "") or "").strip()
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    downgraded: list[str] = []
    for proposal in proposals:
        cluster_ids = [
            str(value) for value in proposal.get("evidence_cluster_ids") or []
        ]
        relevant = [
            decisions[cluster_id]
            for cluster_id in cluster_ids
            if cluster_id in decisions
        ]
        if not relevant:
            kept.append(proposal)
            continue
        selections = {
            str(item.get("selection", "") or "").strip()
            for item in relevant
        }
        if "stop" in selections:
            dropped.append(str(proposal.get("candidate_id", "")) or _text(
                proposal.get("reason")
            ))
            continue
        if "evidence" in selections and fallback == "suggestion":
            downgraded.append(str(proposal.get("candidate_id", "")) or _text(
                proposal.get("reason")
            ))
            kept.append(
                _as_suggestion(
                    proposal,
                    "I3 无法确认目标表，已按用户选择降级为 suggestion；"
                    "请补检索/证据后再决定",
                )
            )
            continue
        if "evidence" in selections:
            kept.append(proposal)
            continue
        allowed = {
            (
                str(item.get("engine", "") or ""),
                str(item.get("target_file", "") or ""),
            )
            for item in relevant
            if str(item.get("selection", "") or "").strip()
            in {"recommended", "alternative"}
        }
        if (
            str(proposal.get("engine", "") or ""),
            str(proposal.get("target_file", "") or ""),
        ) in allowed:
            kept.append(proposal)
        else:
            dropped.append(str(proposal.get("candidate_id", "")) or _text(
                proposal.get("reason")
            ))
    return kept, dropped, downgraded


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从证据聚类生成候选规则草稿（不自动确认）。"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument(
        "--problem-type",
        choices=["all", "miss", "extra", "diff"],
        default=None,
        help=(
            "只生成指定类型的候选；不传时读 <review>/intake.json 的 "
            "problem_type，再退回 all。"
        ),
    )
    parser.add_argument(
        "--intake",
        default=None,
        help="I1 入口定位产物；默认 <review>/intake.json。",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写 candidates.json。")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="写完立即运行 bscat.py check（校验 + 渲染 + 对账）。",
    )
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    intake_path = (
        Path(args.intake).expanduser().resolve() if args.intake else None
    )
    if intake_path is not None and not intake_path.exists():
        raise SystemExit(f"--intake 指向的文件不存在: {intake_path}")
    intake = common.load_intake(intake_path or review_dir)
    problem_type = args.problem_type or str(
        intake.get("problem_type", "") or "all"
    )
    if problem_type not in {"all", "miss", "extra", "diff"}:
        raise SystemExit(f"problem_type={problem_type!r} 不合法")
    direction = str(intake.get("direction", "") or "").strip()
    if (
        args.problem_type
        and intake.get("problem_type")
        and args.problem_type != intake["problem_type"]
    ):
        print(
            f"注意：CLI --problem-type={args.problem_type} 覆盖 "
            f"intake.json={intake['problem_type']}"
        )
    if intake:
        print(
            f"入口定位（I1）：problem_type={problem_type}"
            + (f"，direction={direction}" if direction else "")
        )
    gate_errors = common.parse_gate_errors(review_dir)
    gate_errors += common.attribution_gate_errors(review_dir)
    if gate_errors:
        raise SystemExit(
            "条件门禁未完成，拒绝生成候选：\n- " + "\n- ".join(gate_errors)
        )
    evidence_path = review_dir / "evidence.json"
    if not evidence_path.exists():
        raise SystemExit(f"找不到 {evidence_path}；请先运行 prepare.py。")
    clusters = load_json(evidence_path).get("clusters") or []
    scope_ids = load_scope(review_dir)
    selected = [
        cluster for cluster in clusters
        if (not scope_ids or cluster.get("cluster_id") in scope_ids)
        and (
            problem_type == "all"
            or _problem_type(cluster) == problem_type
        )
    ]
    if not selected:
        print("没有可生成的聚类（检查 target_scope.json / --problem-type）。")
        return 0

    initial_kb = InitialKB(repo)
    # One merchant_kb scan for all names this review can look up: the merchant
    # guessed for an append, and the rule_id of every initial rule that fired.
    wanted_merchants = {_merchant_guess(cluster) for cluster in selected}
    for cluster in selected:
        for hit in [
            *(cluster.get("matching_current_rules") or []),
            *(cluster.get("matching_owner_rules") or []),
        ]:
            if _text(hit.get("file")) == "initial_engine/merchant_kb.csv":
                merchant = _text(hit.get("rule_id")).strip()
                if merchant:
                    wanted_merchants.add(merchant)
    # A keyword update targets the merchant the automaton matched on the sample
    # text, which is not necessarily ``_merchant_guess``.  Resolve those names
    # in the same scan; otherwise ``resolve_keyword_updates`` later triggers a
    # second full merchant_kb scan for one row.
    for cluster in selected:
        if _problem_type(cluster) == "extra":
            continue
        routing = cluster.get("routing") or {}
        if resolve_engine(
            _text(routing.get("owner_engine")),
            _text(cluster.get("expected_category")),
        ) != "initial":
            continue
        for hit in common.initial_keyword_hits(repo, _first_sample_text(cluster)):
            merchant = _text(hit.get("merchant")).strip()
            if merchant:
                wanted_merchants.add(merchant)
    initial_kb.prefetch(wanted_merchants)

    proposals: list[dict[str, Any]] = []
    for cluster in selected:
        generated = propose_cluster(repo, cluster, initial_kb)
        proposals.extend(generated)
    proposals = coalesce_proposals(proposals)
    initial_kb.prefetch(
        _text((proposal.get("match_key") or {}).get("merchant_name")).strip()
        for proposal in proposals
        if _text(proposal.get("operation")) == "keyword_update"
    )
    proposals = resolve_keyword_updates(proposals, initial_kb)

    original_proposals = list(proposals)
    review, section, decisions = _attribution_context(review_dir)
    if decisions and not review.get("required"):
        decisions = _rescue_attribution_decisions(
            decisions, proposals, review, section
        )
    dropped: list[str] = []
    downgraded: list[str] = []
    if decisions:
        proposals, dropped, downgraded = _apply_attribution_decisions(
            proposals, review, section, decisions
        )
        if dropped:
            print(f"I3：按归属决策丢弃 {len(dropped)} 条草稿")
            for item in dropped[:8]:
                print(f"  - {item}")
        if downgraded:
            print(f"I3：按 fallback=suggestion 降级 {len(downgraded)} 条草稿")
            for item in downgraded[:8]:
                print(f"  - {item}")
        had_counts = Counter(
            str(cluster_id)
            for proposal in original_proposals
            for cluster_id in proposal.get("evidence_cluster_ids") or []
        )
        kept_counts = Counter(
            str(cluster_id)
            for proposal in proposals
            if proposal.get("operation") in common.RULE_OPERATIONS
            for cluster_id in proposal.get("evidence_cluster_ids") or []
        )
        for cluster_id, decision in decisions.items():
            if str(decision.get("selection", "")).strip() != "alternative":
                continue
            if had_counts[cluster_id] and not kept_counts[cluster_id]:
                raise SystemExit(
                    f"I3 选择了备选表 {decision.get('engine')}/"
                    f"{decision.get('target_file')}，但生成器没有为聚类 "
                    f"{cluster_id} 产出该表的草稿；请重新路由或改选推荐表，"
                    "不要用 revise 改 target_file"
                )
    engine_default_missing = [
        proposal for proposal in proposals
        if proposal.get("operation") in common.RULE_OPERATIONS
        and str((proposal.get("source_trace") or {}).get("trace_method", ""))
        == "engine_default"
        and not any(
            str(cluster_id) in decisions
            for cluster_id in proposal.get("evidence_cluster_ids") or []
        )
    ]
    if engine_default_missing:
        raise SystemExit(
            "以下候选使用 engine_default 兜底表，但对应聚类没有 I3 决策："
            + ", ".join(
                str((proposal.get("source_trace") or {}).get("cluster_id", ""))
                or proposal.get("reason", "")
                for proposal in engine_default_missing[:8]
            )
            + "。请先查看 attribution_review.json，完成 I3 并写 "
            "dialog_decisions.json#attribution"
        )

    candidates_path = review_dir / "candidates.json"
    if candidates_path.exists():
        payload = load_json(candidates_path)
        existing = payload.get("candidates") or []
    else:
        payload = {
            "run_id": f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "candidates": [],
        }
        existing = []

    new, skipped = merge_proposals(existing, proposals)
    assign_ids(new, existing)
    payload["candidates"] = [*existing, *new]
    payload["proposals_generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["proposal_summary"] = {
        "clusters": len(selected),
        "generated": len(proposals),
        "new": len(new),
        "merged_or_skipped": len(skipped),
        "by_operation": dict(Counter(c["operation"] for c in new).most_common()),
        "by_engine": dict(Counter(c["engine"] for c in new).most_common()),
        "by_problem_type": dict(
            Counter(c.get("problem_type", "") for c in new).most_common()
        ),
        "intake": {
            "problem_type": problem_type,
            "direction": direction,
            "scope": str(intake.get("scope", "") or ""),
            "delivery": str(intake.get("delivery", "") or ""),
            "interaction_mode": str(
                intake.get("interaction_mode", "") or ""
            ),
        },
        "dialog": {
            "parse_required": bool(
                (common.load_json_if_exists(review_dir / "parse_review.json"))
                .get("required")
            ) or common.interaction_mode(review_dir) == "guided",
            "attribution_required": bool(review.get("required")),
            "attribution_decisions": len(decisions),
            "attribution_dropped": len(dropped) if decisions else 0,
            "attribution_downgraded": len(downgraded) if decisions else 0,
        },
    }

    print("\n" + "=" * 64)
    print("候选种子生成")
    print("=" * 64)
    print(f"范围聚类: {len(selected)} | 生成: {len(proposals)} | 新增: {len(new)} "
          f"| 已存在: {len(skipped)}")
    for candidate in new:
        print(
            f"  {candidate['candidate_id']} {candidate['problem_type']} "
            f"{candidate['engine']}/{candidate['operation']} -> "
            f"{candidate['target_file']} [{candidate.get('category', '')}]"
        )
        print(f"      {candidate['reason']}")
    if skipped:
        print("\n已存在（未覆盖）:")
        for item in dict.fromkeys(skipped):
            print(f"  - {item}")
    print("=" * 64)

    if args.dry_run:
        print("\n--dry-run：未写入 candidates.json。")
        return 0

    backup_written = None
    if candidates_path.exists():
        backup_written = candidates_path.with_suffix(".json.bak")
        backup_written.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    dump_json(candidates_path, payload)
    print(f"\ncandidates.json -> {candidates_path}")
    if backup_written is not None:
        print(f"备份 -> {backup_written}")

    if args.validate:
        command = [
            sys.executable,
            str(SCRIPT_DIR.parent / "validate.py"),
            "--repo", str(repo),
            "--review", str(review_dir),
        ]
        sys.stdout.flush()
        print("\n$ " + " ".join(command), flush=True)
        return subprocess.run(command).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
