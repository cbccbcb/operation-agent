#!/usr/bin/env python3
"""Validate candidate rules against the sample before any human approval.

Checks, in order:
1. schema / target-file existence / operation legality;
2. category ownership (an engine may only emit categories listed for it in
   ``configs/category_catalog.json``, otherwise the whole batch raises);
3. pattern syntax under the engine's real matching mode;
4. duplicate rules in the target table;
5. cross-candidate collisions (same match_key, same appended pattern);
6. sample-level gain / conflict simulation using the engine-accurate matcher in
   ``common.py``;
7. risk tier (``low`` may be batch-approved, ``medium`` and ``high`` need
   per-item review, and ``high`` additionally needs explicit acknowledgement).

The simulation is intentionally sample-scoped: it answers "what would this
rule touch in the rows we have evidence for", not "what will the whole
production batch do".  The post-apply step reruns the real pipeline.

    python validate_candidates.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

# merchant_kb.csv contains very long pipe-separated keyword fields.  Windows
# C long is 32-bit, so sys.maxsize overflows; clamp to the safe 32-bit maximum.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

RULE_OPERATIONS = {"append", "edit", "delete", "keyword_update"}
CONFIG_OPERATIONS = {"catalog", "priority"}
ALL_OPERATIONS = RULE_OPERATIONS | CONFIG_OPERATIONS | {"suggestion"}
SAFE_APPEND_ENGINES = {
    "initial", "dishonour", "gambling", "rent", "catch_all", "fee",
    "all_other_credit",
}
NON_CLASSIFIER_FILES = {
    "transfer_engine/resources/transfer_pairing_exclusions.csv",
    "transfer_engine/resources/transfer_counterparty_rules.csv",
    "transfer_engine/resources/transfer_indicator_patterns.csv",
    "transfer_engine/resources/transfer_group_exclusion_patterns.csv",
    "transfer_engine/resources/transfer_row_exclusion_patterns.csv",
    "liability_engine/resources/bnpl_maximum_limits.csv",
}


# ── io helpers ──────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def read_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


def iter_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            yield line_number, {str(k): str(v) for k, v in row.items()}


def matches_key(row: dict[str, str], match_key: dict[str, Any]) -> bool:
    return all(str(row.get(key, "")) == str(value) for key, value in match_key.items())


# Exact-row lookups on merchant_kb.csv scan 874k rows.  Validation knows all
# match_keys up front, so preload them in one pass instead of one pass per
# candidate.  Values are returned as copies to keep callers from mutating the
# cache.
_ROW_CACHE: dict[
    tuple[str, tuple[str, ...], tuple[str, ...]],
    list[dict[str, Any]],
] = {}


def _row_cache_key(path: Path, match_key: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    columns = tuple(sorted(str(key) for key in match_key))
    values = tuple(str(match_key[column]) for column in columns)
    return str(path), columns, values


def preload_exact_rows(path: Path, match_keys: list[dict[str, Any]]) -> None:
    """Index the requested match_keys in one scan of ``path``.

    Only columns used by some match_key are compared, so a review with 50
    initial edits costs one 874k-row scan instead of 50.
    """
    groups: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for match_key in match_keys:
        if not match_key:
            continue
        columns = tuple(sorted(str(key) for key in match_key))
        groups.setdefault(columns, set()).add(
            tuple(str(match_key[column]) for column in columns)
        )
    if not groups:
        return

    found: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for column_group, wanted in groups.items():
        for values in wanted:
            found[(column_group, values)] = []

    # csv.reader + column indexes avoids building an 874k-element dict stream
    # just to find a handful of rows.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return
        index = {name: position for position, name in enumerate(header)}
        plans: list[tuple[tuple[str, ...], set[tuple[str, ...]], tuple[int, ...]]] = []
        for column_group, wanted in groups.items():
            positions = tuple(index.get(column) for column in column_group)
            if any(position is None for position in positions):
                continue
            plans.append((column_group, wanted, positions))
        for line_number, row in enumerate(reader, start=2):
            for column_group, wanted, positions in plans:
                values = tuple(
                    row[position] if position < len(row) else ""
                    for position in positions
                )
                if values in wanted:
                    found[(column_group, values)].append(
                        {
                            "line": line_number,
                            "row": {
                                name: row[position] if position < len(row) else ""
                                for position, name in enumerate(header)
                            },
                        }
                    )
    for (column_group, values), hits in found.items():
        columns = tuple(sorted(column_group))
        _ROW_CACHE[(str(path), columns, values)] = hits


def find_rows(path: Path, match_key: dict[str, Any]) -> list[dict[str, Any]]:
    key = _row_cache_key(path, match_key)
    if key in _ROW_CACHE:
        return [
            {"line": hit["line"], "row": dict(hit["row"])}
            for hit in _ROW_CACHE[key]
        ]
    hits = []
    for line_number, row in iter_rows(path):
        if matches_key(row, match_key):
            hits.append({"line": line_number, "row": row})
    return hits


# ── sample context ──────────────────────────────────────────────────────────

def _read_context_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "") for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def load_sample_contexts(review_dir: Path):
    """Read the two small sample tables with csv, not pandas.

    Importing pandas costs ~1 s on this stage and the frames are only used as
    lookup dicts here.  ``samples.csv`` and ``before_report.csv`` are written by
    this skill with UTF-8 BOM, which ``utf-8-sig`` handles.
    """
    samples = _read_context_rows(review_dir / "samples.csv")
    before = _read_context_rows(review_dir / "before_report.csv")
    before_lookup = {
        (row.get("application_id", ""), row.get("transaction_id", "")): row
        for row in before
    }

    contexts = []
    for sample in samples:
        key = (
            sample.get("application_id", ""),
            sample.get("transaction_id", ""),
        )
        result = before_lookup.get(key, {})
        contexts.append(
            {
                "key": key,
                "text": sample.get("text", ""),
                "dr_cr": sample.get("dr_cr", ""),
                "amount": sample.get("amount", ""),
                "account_type": sample.get("account_type", ""),
                "bank": sample.get("bank", ""),
                "counterparty": sample.get("third_party", ""),
                "expected": result.get("expected_category", ""),
                "pipeline_category": result.get("pipeline_category", ""),
                "pipeline_engine": result.get("pipeline_engine", ""),
                "problem_type": result.get("problem_type", ""),
                "cluster_id": result.get("cluster_id", ""),
            }
        )
    return contexts


# ── candidate normalisation ────────────────────────────────────────────────

def resolve_category(
    candidate: dict[str, Any],
    spec: common.RuleFileSpec,
    values: dict[str, str],
) -> str:
    declared = str(candidate.get("category") or values.get("category") or "").strip()
    if spec.engine == "income":
        # match_type is ignored and gate-only groups (strong_wage, hard_negative,
        # ...) do not emit a category at all.  Do not let a declared category
        # paper over that.
        return common.income_group_category(values.get("pattern_group", ""))
    if spec.engine == "fee":
        value = values.get("category", "").strip()
        return "Fees" if value.lower() == "fee" else (value or declared)
    if spec.implicit_category:
        return spec.implicit_category
    if spec.engine == "liability" and spec.relative_path.endswith(
        ("credit_card_rules.csv", "counterparty_keyword_rules.csv")
    ):
        return declared
    return declared or values.get("category", "")


def initial_shadow_map(
    repo: Path,
    values: dict[str, Any],
    contexts: list[dict[str, Any]],
    match_key: dict[str, Any] | None = None,
    *,
    operation: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Rows where a different merchant's keyword wins before this one can.

    The initial engine matches whole-word keywords on ``clean_text`` output and
    the longest keyword wins; for a keyword string duplicated across rows the
    last row owns it.  Simulating only the candidate row reports a gain even
    when another merchant shadows it, and the real pipeline never reaches the
    candidate.  ``operation`` selects edit (substitute the matched row) or
    append (end of file, so its keywords win duplicates).

    Existing keywords are read from the prebuilt Aho-Corasick sidecar, which is
    what the engine itself uses.  That turns a per-candidate 874k-row CSV scan
    into a millisecond lookup; a stale or missing sidecar falls back to the
    streaming scan.

    Returns ``{context key: detail}`` for rows the candidate matches textually
    but does not win.
    """
    merchant = str(values.get("merchant_name", "")).strip()
    keywords = [
        keyword
        for keyword in common.split_initial_keywords(values.get("keywords", ""))
        if keyword and keyword not in common.STOPWORDS
    ][: common.INITIAL_MAX_VARIANTS]
    if not merchant or not keywords:
        return {}

    own_merchant = str((match_key or {}).get("merchant_name", "")).strip()
    if (
        common.load_initial_automaton(repo) is not None
        and _initial_automaton_fresh(repo)
        and (operation == "append" or own_merchant)
    ):
        return _initial_shadow_map_automaton(
            repo, merchant, keywords, contexts, own_merchant, operation
        )
    return _initial_shadow_map_scan(
        repo,
        merchant,
        keywords,
        str(values.get("category", "")),
        contexts,
        match_key or {},
        operation,
    )


def _initial_automaton_fresh(repo: Path) -> bool:
    csv_path = repo / "initial_engine" / "merchant_kb.csv"
    pickle_path = Path(str(csv_path) + ".automaton.pickle")
    try:
        return pickle_path.stat().st_mtime >= csv_path.stat().st_mtime
    except OSError:
        return False


def _initial_shadow_map_automaton(
    repo: Path,
    merchant: str,
    keywords: list[str],
    contexts: list[dict[str, Any]],
    own_merchant: str,
    operation: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    shadowed: dict[tuple[str, str], dict[str, Any]] = {}
    for context in contexts:
        cleaned = common.clean_text(context.get("text", ""))
        own_matches = [
            keyword
            for keyword in keywords
            if common.whole_word_anywhere(keyword, cleaned)
        ]
        if not own_matches:
            continue
        own_best = max(len(keyword) for keyword in own_matches)
        own_best_keyword = next(
            keyword for keyword in own_matches if len(keyword) == own_best
        )
        hits = common.initial_keyword_hits(repo, context.get("text", ""))
        if operation != "append":
            # The edited row disappears before the replacement is applied, so
            # its old keywords must not count as competitors.
            hits = [hit for hit in hits if hit["merchant"] != own_merchant]
        if not hits:
            continue
        winner = max(hits, key=lambda hit: len(hit["keyword"]))
        if winner["merchant"] == merchant:
            continue
        longer_shadow = len(winner["keyword"]) > own_best
        duplicate_shadow = (
            operation != "append"
            and len(winner["keyword"]) == own_best
            and winner["keyword"] == own_best_keyword
        )
        if not (longer_shadow or duplicate_shadow):
            continue
        shadowed[tuple(context["key"])] = {
            "application_id": context["key"][0],
            "transaction_id": context["key"][1],
            "text": str(context.get("text", ""))[:160],
            "candidate_keyword": own_best_keyword,
            "winning_keyword": winner["keyword"],
            "winning_merchant": winner["merchant"],
            "winning_category": winner["category"],
        }
    return shadowed


def _initial_shadow_map_scan(
    repo: Path,
    merchant: str,
    keywords: list[str],
    category: str,
    contexts: list[dict[str, Any]],
    match_key: dict[str, Any],
    operation: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    candidate_values = {
        "merchant_name": merchant,
        "keywords": "|".join(keywords),
        "category": category,
    }
    if operation == "append":
        competition = common.initial_keyword_competition(
            repo,
            [str(context.get("text", "")) for context in contexts],
            append={"values": candidate_values},
        )
    else:
        competition = common.initial_keyword_competition(
            repo,
            [str(context.get("text", "")) for context in contexts],
            override={"match_key": match_key, "values": candidate_values},
        )

    shadowed: dict[tuple[str, str], dict[str, Any]] = {}
    for context in contexts:
        cleaned = common.clean_text(context.get("text", ""))
        own_matches = [
            keyword
            for keyword in keywords
            if common.whole_word_anywhere(keyword, cleaned)
        ]
        if not own_matches:
            continue
        own_best = max(len(keyword) for keyword in own_matches)
        own_best_keyword = next(
            keyword for keyword in own_matches if len(keyword) == own_best
        )
        hits = competition.get(cleaned) or []
        if not hits:
            continue
        winner = max(hits, key=lambda hit: len(hit["keyword"]))
        longer_shadow = len(winner["keyword"]) > own_best
        duplicate_shadow = (
            len(winner["keyword"]) == own_best
            and winner["keyword"] == own_best_keyword
        )
        if (
            winner["merchant"] == merchant
            or not (longer_shadow or duplicate_shadow)
        ):
            # Equal-length but different keyword strings depend on the
            # automaton's iteration order; do not guess.
            continue
        shadowed[tuple(context["key"])] = {
            "application_id": context["key"][0],
            "transaction_id": context["key"][1],
            "text": str(context.get("text", ""))[:160],
            "candidate_keyword": own_best_keyword,
            "winning_keyword": winner["keyword"],
            "winning_merchant": winner["merchant"],
            "winning_category": winner["category"],
        }
    return shadowed


def shadow_warning(effect: dict[str, Any]) -> str | None:
    """Explain why a textually matching candidate will not fire."""
    count = int(effect.get("shadowed_rows", 0))
    if not count:
        return None
    examples = effect.get("shadow_examples") or []
    detail = "；".join(
        f"文本 {example['text'][:60]!r} 被商户 "
        f"{example['winning_merchant']!r} 的更长关键词 "
        f"{example['winning_keyword']!r}（分类 {example['winning_category']}）抢先命中"
        for example in examples[:3]
    )
    return (
        f"initial 最长关键词胜出：{count} 行会被其他商户的更长关键词覆盖，"
        f"本候选在这些行上不会生效" + (f"（{detail}）" if detail else "")
    )


def validate_rule_candidate(
    repo: Path,
    candidate: dict[str, Any],
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    engine = str(candidate.get("engine", "")).strip()
    operation = str(candidate.get("operation", "")).strip()
    target_file = str(candidate.get("target_file", "")).strip()

    if not engine:
        errors.append("缺少 engine")
    if not target_file:
        errors.append("缺少 target_file")
    spec = common.spec_for(engine, target_file)
    if spec is None:
        return {
            "valid": False,
            "errors": [f"{target_file!r} 不是引擎 {engine!r} 的已知规则表"],
            "warnings": warnings,
            "risk_level": "high",
            "expected_effect": {},
        }
    target = repo / target_file
    if not target.exists():
        return {
            "valid": False,
            "errors": [f"目标文件不存在: {target}"],
            "warnings": warnings,
            "risk_level": "high",
            "expected_effect": {},
        }

    header = read_header(target)
    provided = {str(k): str(v) for k, v in (candidate.get("row") or {}).items()}
    unknown = sorted(set(provided) - set(header))
    if unknown:
        errors.append(f"以下列不存在于 {target_file}: {unknown}")

    if operation == "append" and not spec.appendable:
        errors.append(
            f"不允许向 {target_file} 追加；该表是排除/配置表，不是分类器"
        )
    target_hits: list[dict[str, Any]] = []
    if operation in {"edit", "delete"}:
        match_key = candidate.get("match_key") or {}
        if not match_key:
            errors.append("edit/delete 需要 match_key")
        else:
            target_hits = find_rows(target, match_key)
            if len(target_hits) != 1:
                errors.append(
                    f"match_key 在 {target_file} 中匹配到 {len(target_hits)} 行"
                    "（必须恰好 1 行）"
                )
    if operation == "append" and not provided:
        errors.append("append 需要 row 内容")
    if operation == "edit" and not provided:
        errors.append("edit 需要在 row 中提供要修改的列")

    if operation == "append":
        values = provided
    elif len(target_hits) == 1:
        values = dict(target_hits[0]["row"])
        if operation == "edit":
            values.update(provided)
    else:
        values = provided
    category = resolve_category(candidate, spec, values)
    pattern = values.get(spec.pattern_column, "")
    match_type = values.get("match_type", "")

    # Every rule change must trace back to the config table the evidence came
    # from, and liability's product_type must already exist in that table.
    trace_error = common.candidate_trace_error(candidate)
    if trace_error:
        errors.append(trace_error)
    warnings.extend(common.candidate_trace_warnings(candidate))
    product_type_error = common.candidate_product_type_error(
        repo, candidate, spec, header, values
    )
    if product_type_error:
        errors.append(product_type_error)

    if operation != "delete":
        if not str(pattern).strip() and not (
            spec.match_mode == "liability_homecar"
            and str(values.get("match_scope", "")).strip().lower() == "all"
        ):
            errors.append(f"候选行中的 {spec.pattern_column!r} 为空")
        else:
            ok, message = common.compile_pattern(spec.match_mode, str(pattern), match_type)
            if not ok:
                errors.append(message)

    if operation in {"append", "edit"}:
        if not category:
            errors.append(
                "无法确定最终产出的分类；请设置 candidate.category "
                "（例如 income 的纯门控 pattern_group、liability 的 product_type 规则等）"
            )
        elif not common.category_allowed(repo, category, engine):
            owners = common.category_owner_map(repo).get(category, [])
            errors.append(
                f"分类 {category!r} 不归引擎 {engine!r} 所有 "
                f"（目录中的归属引擎: {owners or '无'}）；orchestrator 会在校验时抛错"
            )
        if spec.implicit_category and candidate.get("category") not in (None, "", spec.implicit_category):
            errors.append(
                f"{engine} 固定产出 {spec.implicit_category!r}；"
                f"候选却声明为 {candidate.get('category')!r}"
            )

    # Duplicate detection.  An edit may legitimately keep the same pattern;
    # exclude the row being edited.
    duplicates = []
    if operation in {"append", "edit"} and str(pattern).strip():
        duplicates = common.duplicate_rules(repo, engine, str(pattern), category)
        if operation == "edit" and len(target_hits) == 1:
            duplicates = [
                hit for hit in duplicates
                if hit.get("line") != target_hits[0]["line"]
            ]
        if duplicates:
            errors.append(
                "已存在完全相同的规则: "
                + "; ".join(
                    f"{hit.get('file')}:{hit.get('line')} ({hit.get('category')})"
                    for hit in duplicates[:5]
                )
            )

    # Sample simulation.
    declared_problem_type = common.normalise_problem_type(
        candidate.get("problem_type", "")
    )
    effect = {
        "sample_total": len(contexts),
        "matched_rows": 0,
        "resolved_rows": 0,
        "conflicts": 0,
        "already_correct": 0,
        "unclassified_gain": 0,
        "matched_mismatch_other_category": 0,
        "matched_extra_rows": 0,
        "conflicts_extra": 0,
        "shadowed_rows": 0,
        "shadow_examples": [],
        "examples": [],
    }
    if values and operation != "delete":
        synthetic = common.RuleRow(
            engine=engine,
            relative_path=target_file,
            line_number=0,
            values=values,
        )
        shadowed = {}
        if engine == "initial" and operation in {"append", "edit"}:
            shadowed = initial_shadow_map(
                repo,
                values,
                contexts,
                candidate.get("match_key") or {},
                operation=operation,
            )
        for context in contexts:
            if not common.rule_matches(spec, synthetic, context):
                continue
            effect["matched_rows"] += 1
            shadow = shadowed.get(context["key"])
            if shadow:
                effect["shadowed_rows"] += 1
                if len(effect["shadow_examples"]) < 10:
                    effect["shadow_examples"].append(shadow)
                continue
            expected = context["expected"]
            pipeline = context["pipeline_category"]
            is_extra = (
                context.get("problem_type") == "extra"
                or (not expected and bool(pipeline))
            )
            if is_extra:
                effect["matched_extra_rows"] += 1
                if category:
                    # A positive rule on a row that should stay unclassified
                    # is a new false positive.
                    effect["conflicts"] += 1
                    effect["conflicts_extra"] += 1
            elif expected and expected == category:
                effect["resolved_rows"] += 1
                if pipeline == expected:
                    effect["already_correct"] += 1
                elif pipeline == "":
                    effect["unclassified_gain"] += 1
            elif expected and expected != category:
                effect["conflicts"] += 1
                effect["matched_mismatch_other_category"] += 1
            if len(effect["examples"]) < 20:
                effect["examples"].append(
                    {
                        "application_id": context["key"][0],
                        "transaction_id": context["key"][1],
                        "text": context["text"][:160],
                        "expected": expected,
                        "pipeline": pipeline,
                        "problem_type": context.get("problem_type", ""),
                        "pipeline_engine": context["pipeline_engine"],
                        "cluster_id": context["cluster_id"],
                    }
                )

    matched_mode: dict[str, str] = {}
    if operation in {"edit", "delete"} and spec.is_matcher and len(target_hits) == 1:
        old_values = dict(target_hits[0]["row"])
        matched_mode = {
            key: str(old_values.get(key, "") or "")
            for key in ("source", "match_type", "rule_type")
            if old_values.get(key) not in (None, "")
        }
        if old_values:
            old_rule = common.RuleRow(engine=engine, relative_path=target_file,
                                      line_number=0, values=old_values)
            old_hits = sum(
                1 for context in contexts if common.rule_matches(spec, old_rule, context)
            )
            effect["rows_matched_by_old_rule"] = old_hits
            if operation == "delete":
                released = 0
                risky = 0
                released_extra = 0
                blocked_extra = 0
                remaining_examples: list[dict[str, Any]] = []
                target_line = target_hits[0]["line"]
                for context in contexts:
                    if not common.rule_matches(spec, old_rule, context):
                        continue
                    if len(effect["examples"]) < 20:
                        effect["examples"].append(
                            {
                                "application_id": context["key"][0],
                                "transaction_id": context["key"][1],
                                "text": context["text"][:160],
                                "expected": context["expected"],
                                "pipeline": context["pipeline_category"],
                                "problem_type": context.get("problem_type", ""),
                                "pipeline_engine": context["pipeline_engine"],
                                "cluster_id": context["cluster_id"],
                            }
                        )
                    problem_type = context.get("problem_type", "")
                    if problem_type == "extra":
                        if not context["pipeline_category"]:
                            continue
                        # Deleting one rule is not enough if another rule in
                        # the same engine still matches the row.  Do not claim
                        # the row is released until all same-engine matches are
                        # gone.
                        remaining = common.find_matching_rules(repo, engine, context)
                        if engine == "initial":
                            target_rule_id = str(old_values.get("merchant_name", ""))
                            remaining = [
                                hit for hit in remaining
                                if str(hit.get("rule_id", "")) != target_rule_id
                            ]
                        else:
                            remaining = [
                                hit for hit in remaining
                                if not (
                                    hit.get("file") == target_file
                                    and hit.get("line") == target_line
                                )
                            ]
                        if remaining:
                            blocked_extra += 1
                            if len(remaining_examples) < 10:
                                remaining_examples.append(
                                    {
                                        "application_id": context["key"][0],
                                        "transaction_id": context["key"][1],
                                        "remaining_rules": remaining[:5],
                                    }
                                )
                        else:
                            released_extra += 1
                        continue
                    if context["expected"] and context["pipeline_category"] != context["expected"]:
                        released += 1
                    elif context["expected"] and context["pipeline_category"] == context["expected"]:
                        risky += 1
                effect["delete_released_mismatch_rows"] = released
                effect["delete_risked_correct_rows"] = risky
                effect["delete_released_extra_rows"] = released_extra
                effect["delete_blocked_extra_rows"] = blocked_extra
                if remaining_examples:
                    effect["delete_remaining_rule_examples"] = remaining_examples
                if declared_problem_type == "extra":
                    effect["resolved_rows"] = released_extra
                else:
                    effect["resolved_rows"] = released
    elif declared_problem_type == "extra" and operation in {"append", "edit"}:
        # No extra rows matched by this candidate: it cannot fix the problem.
        pass

    message = shadow_warning(effect)
    if message:
        warnings.append(message)

    # Risk tier.  High = structural or demonstrably unsafe on the sample;
    # medium = narrow but still needs the reviewer to look at the impact
    # (notably every edit/delete); low = safe append with no conflicts/gain
    # issues and therefore batchable.
    high_reasons = []
    medium_reasons = []
    if engine in {"income", "liability", "transfer"}:
        high_reasons.append(f"{engine} 规则涉及行为阈值或层级优先级")
    if effect["conflicts"]:
        high_reasons.append(f"命中 {effect['conflicts']} 行期望为其他分类的数据")
    if effect["conflicts_extra"]:
        high_reasons.append(
            f"会给 {effect['conflicts_extra']} 行本应保持未分类的数据新增分类"
        )
    if effect["already_correct"]:
        high_reasons.append(
            f"同时命中 {effect['already_correct']} 行本来已经正确的数据"
        )
    if operation == "append" and effect["resolved_rows"] == 0:
        high_reasons.append("该候选不会修复任何样本行")
    if declared_problem_type == "extra" and operation == "append":
        high_reasons.append("多识别通常需要删除或收窄规则；新增规则可能加重误分类")
    if operation == "delete" and effect.get("delete_risked_correct_rows"):
        high_reasons.append("删除可能影响当前正确的行")
    if (
        operation == "delete"
        and declared_problem_type == "extra"
        and not effect.get("delete_released_extra_rows")
    ):
        high_reasons.append("删除不会修复任何多识别样本行")
    if declared_problem_type == "extra" and effect.get("delete_blocked_extra_rows"):
        high_reasons.append(
            f"删除后仍有其他规则命中 {effect['delete_blocked_extra_rows']} 行多识别数据"
        )
    if spec.relative_path in NON_CLASSIFIER_FILES:
        high_reasons.append("该表不是分类器，无法模拟效果")
    if (
        operation != "delete"
        and declared_problem_type != "extra"
        and category
        and not common.category_allowed(repo, category, engine)
    ):
        high_reasons.append("分类归属不匹配")
    if duplicates:
        high_reasons.append("重复规则")

    # Medium: everything edit/delete that is not already high, plus appends
    # whose sample footprint is broad enough that the reviewer should see the
    # individual rows.  This is the "逐条看清" tier.
    if operation in {"edit", "delete"}:
        medium_reasons.append("修改或删除既有规则，需逐条看清接管与影响面")
    if operation in {"append", "edit"} and effect["matched_rows"] > 5:
        medium_reasons.append(
            f"样本命中 {effect['matched_rows']} 行，影响面较宽，需逐条看清"
        )
    if operation in {"edit", "delete"} and effect.get("rows_matched_by_old_rule", 0) > 5:
        medium_reasons.append(
            f"原规则在样本命中 {effect['rows_matched_by_old_rule']} 行，影响面较宽"
        )
    if operation in {"edit", "delete"} and effect["resolved_rows"] == 0:
        medium_reasons.append("样本上未观察到修复")

    if high_reasons:
        risk = "high"
    elif medium_reasons:
        risk = "medium"
    else:
        risk = "low"

    if operation == "append" and spec.is_matcher:
        warnings.append(
            "样本模拟只验证匹配层面；写入后会重跑真实流水线"
        )
    if operation == "delete":
        warnings.append(
            "删除效果取决于后续哪个引擎接管该行；必须执行写入后验证"
        )
    if declared_problem_type == "extra" and effect.get("delete_blocked_extra_rows"):
        examples = effect.get("delete_remaining_rule_examples") or []
        detail = ""
        if examples:
            hit_names = [
                str(hit.get("rule_id") or hit.get("pattern") or "")
                for hit in examples[0].get("remaining_rules", [])
            ]
            detail = "；示例命中: " + ", ".join(name for name in hit_names if name)
        warnings.append(
            f"删除后同一引擎仍有其他规则命中 {effect['delete_blocked_extra_rows']} "
            f"行多识别数据，仅删除本条不足以修复{detail}"
        )
    if engine == "liability" and spec.relative_path.endswith(
        ("credit_card_rules.csv", "counterparty_keyword_rules.csv")
    ):
        warnings.append(
            "liability 的分类由 product_type/bank/stream 优先级推导；"
            "规则行声明的 category 不会直接成为最终分类"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "risk_level": risk,
        "risk_reasons": high_reasons,
        "medium_reasons": medium_reasons,
        "category": category,
        "pattern": pattern,
        "target_file": target_file,
        "matched_mode": matched_mode,
        "expected_effect": effect,
    }


def validate_keyword_update_candidate(
    repo: Path,
    candidate: dict[str, Any],
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate an initial merchant-keyword update.

    ``keyword_update`` edits one ``merchant_kb.csv`` row without replacing the
    whole row.  It accepts either a full ``keywords`` replacement or
    ``add_keywords`` / ``remove_keywords``.  The result is normalised and
    checked against the initial engine's real constraints (uppercase
    ``[A-Z0-9 ]``, standalone stopwords dropped, 50 variants max), then the old
    and new keyword lists are simulated separately so a removal that loses a
    correct row is visible before approval.
    """
    errors: list[str] = []
    warnings: list[str] = []
    engine = str(candidate.get("engine", "")).strip()
    target_file = str(candidate.get("target_file", "")).strip()
    spec = common.spec_for(engine, target_file)
    if spec is None:
        return {
            "valid": False,
            "errors": [f"{target_file!r} 不是引擎 {engine!r} 的已知规则表"],
            "warnings": warnings,
            "risk_level": "high",
            "expected_effect": {},
        }
    if engine != "initial" or spec.match_mode != "initial_variants":
        errors.append("keyword_update 只允许用于 initial_engine/merchant_kb.csv")

    target = repo / target_file
    if not target.exists():
        return {
            "valid": False,
            "errors": [f"目标文件不存在: {target}"],
            "warnings": warnings,
            "risk_level": "high",
            "expected_effect": {},
        }

    match_key = candidate.get("match_key") or {}
    if not match_key:
        errors.append("keyword_update 需要 match_key（通常为 merchant_name）")
    target_hits = find_rows(target, match_key) if match_key else []
    if match_key and len(target_hits) != 1:
        errors.append(
            f"match_key 在 {target_file} 中匹配到 {len(target_hits)} 行"
            "（必须恰好 1 行）"
        )
    old_values = dict(target_hits[0]["row"]) if len(target_hits) == 1 else {}

    trace_error = common.candidate_trace_error(candidate)
    if trace_error:
        errors.append(trace_error)
    warnings.extend(common.candidate_trace_warnings(candidate))

    update = {str(key): value for key, value in (candidate.get("row") or {}).items()}
    allowed = {"keywords", "add_keywords", "remove_keywords"}
    unknown = sorted(set(update) - allowed)
    if unknown:
        errors.append(
            "keyword_update 的 row 只允许 keywords / add_keywords / "
            f"remove_keywords；收到 {unknown}"
        )

    category = str(
        candidate.get("category") or old_values.get("category", "")
    ).strip()
    if len(target_hits) == 1:
        row_category = str(old_values.get("category", "")).strip()
        if not category:
            errors.append("无法确定商户分类；请设置 candidate.category")
        elif category != row_category:
            errors.append(
                f"keyword_update 不能修改分类：商户当前为 {row_category!r}，"
                f"候选声明为 {category!r}"
            )
        elif not common.category_allowed(repo, category, engine):
            errors.append(
                f"分类 {category!r} 不归引擎 {engine!r} 所有；orchestrator 会拒绝"
            )

    final_keywords = ""
    change: dict[str, Any] = {}
    if len(target_hits) == 1 and not unknown:
        try:
            final_keywords, change = common.merge_initial_keywords(
                old_values.get("keywords", ""), update
            )
        except ValueError as exc:
            errors.append(str(exc))
    if change:
        if not change.get("added") and not change.get("removed"):
            warnings.append("关键词没有变化；该候选是空操作")
        if int(change.get("count_after", 0)) > 40:
            warnings.append(
                f"更新后关键词 {change['count_after']} 个，接近 initial 的 "
                f"{common.INITIAL_MAX_VARIANTS} 个上限"
            )

    effect: dict[str, Any] = {
        "sample_total": len(contexts),
        "matched_rows": 0,
        "resolved_rows": 0,
        "conflicts": 0,
        "already_correct": 0,
        "unclassified_gain": 0,
        "matched_mismatch_other_category": 0,
        "matched_extra_rows": 0,
        "conflicts_extra": 0,
        "rows_matched_by_old_rule": 0,
        "keyword_change": change,
        "shadowed_rows": 0,
        "shadow_examples": [],
        "examples": [],
    }

    if final_keywords and len(target_hits) == 1:
        old_rule = common.RuleRow(
            engine=engine, relative_path=target_file, line_number=0, values=old_values
        )
        new_values = dict(old_values)
        new_values["keywords"] = final_keywords
        new_rule = common.RuleRow(
            engine=engine, relative_path=target_file, line_number=0, values=new_values
        )
        shadowed = initial_shadow_map(
            repo, new_values, contexts, match_key, operation="edit"
        )
        declared_problem_type = common.normalise_problem_type(
            candidate.get("problem_type", "")
        )
        for context in contexts:
            old_match = common.rule_matches(spec, old_rule, context)
            new_match = common.rule_matches(spec, new_rule, context)
            if old_match:
                effect["rows_matched_by_old_rule"] += 1
            expected = context["expected"]
            pipeline = context["pipeline_category"]
            is_extra = (
                context.get("problem_type") == "extra"
                or (not expected and bool(pipeline))
            )
            if old_match and not new_match:
                # Removing a variant can only lose rows.  Rows that were
                # already correct are the dangerous case.
                if expected and expected == category and pipeline == expected:
                    effect["lost_correct_rows"] = effect.get("lost_correct_rows", 0) + 1
                elif is_extra and pipeline:
                    effect["released_extra_rows"] = (
                        effect.get("released_extra_rows", 0) + 1
                    )
                continue
            if not new_match:
                continue
            effect["matched_rows"] += 1
            shadow = shadowed.get(context["key"])
            if shadow and not old_match:
                # The candidate's keyword is shorter than another merchant's
                # matching keyword; it never reaches classification.
                effect["shadowed_rows"] += 1
                if len(effect["shadow_examples"]) < 10:
                    effect["shadow_examples"].append(shadow)
                continue
            if is_extra:
                effect["matched_extra_rows"] += 1
                if not old_match:
                    effect["conflicts"] += 1
                    effect["conflicts_extra"] += 1
            elif expected and expected == category:
                if pipeline == expected:
                    effect["already_correct"] += 1
                elif pipeline == "":
                    effect["unclassified_gain"] += 1
                if not old_match and pipeline != expected:
                    effect["resolved_rows"] += 1
            elif expected and expected != category:
                if not old_match:
                    effect["conflicts"] += 1
                    effect["matched_mismatch_other_category"] += 1
            if len(effect["examples"]) < 20:
                effect["examples"].append(
                    {
                        "application_id": context["key"][0],
                        "transaction_id": context["key"][1],
                        "text": context["text"][:160],
                        "expected": expected,
                        "pipeline": pipeline,
                        "problem_type": context.get("problem_type", ""),
                        "pipeline_engine": context["pipeline_engine"],
                        "cluster_id": context["cluster_id"],
                    }
                )
        if declared_problem_type == "extra":
            effect["resolved_rows"] = effect.get("released_extra_rows", 0)

    message = shadow_warning(effect)
    if message:
        warnings.append(message)

    high_reasons: list[str] = []
    if engine in {"income", "liability", "transfer"}:
        high_reasons.append(f"{engine} 规则涉及行为阈值或层级优先级")
    if effect["conflicts"]:
        high_reasons.append(f"命中 {effect['conflicts']} 行期望为其他分类的数据")
    if effect["conflicts_extra"]:
        high_reasons.append(
            f"会给 {effect['conflicts_extra']} 行本应保持未分类的数据新增分类"
        )
    if effect.get("lost_correct_rows"):
        high_reasons.append(
            f"删除的变体会让 {effect['lost_correct_rows']} 行已正确数据失去分类"
        )

    # A keyword update always edits an existing high-fan-out merchant row; it
    # is never batchable.
    medium_reasons = ["修改 initial 既有商户的关键词变体，需逐条看清 50 变体上限与接管"]
    if change and not change.get("added") and not change.get("removed"):
        medium_reasons.append("关键词没有实际变化（空操作）")
    if declared_problem_type == "extra" and effect.get("resolved_rows", 0) == 0:
        medium_reasons.append("样本上未观察到释放多识别行")

    if errors:
        risk = "high"
    elif high_reasons:
        risk = "high"
    else:
        risk = "medium"

    if len(target_hits) == 1 and target_hits[0]["row"].get("keywords"):
        warnings.append(
            "initial 的 keywords 以 | 分隔且原样加载；已按大写、仅 [A-Z0-9 ]、"
            "去重、停用词和 50 变体上限校验"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "risk_level": risk,
        "risk_reasons": high_reasons,
        "medium_reasons": medium_reasons,
        "category": category,
        "pattern": final_keywords,
        "target_file": target_file,
        "expected_effect": effect,
        "keyword_change": change,
    }


def validate_config_candidate(
    repo: Path,
    candidate: dict[str, Any],
    priorities: dict[str, int],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    operation = candidate.get("operation")
    target_file = candidate.get("target_file", "")
    target = repo / target_file
    if target_file not in {"configs/pipeline.json", "configs/category_catalog.json"}:
        errors.append(
            "配置操作只能指向 configs/pipeline.json 或 "
            "configs/category_catalog.json"
        )
    if not target.exists():
        errors.append(f"目标文件不存在: {target}")

    change = candidate.get("row") or candidate.get("proposed_change") or {}
    if not change:
        errors.append("配置候选需要 row 或 proposed_change")

    if operation == "catalog":
        category = change.get("category") or candidate.get("category")
        owner = change.get("owner_engine_id")
        if not category:
            errors.append("catalog 变更需要 category")
        if owner:
            if isinstance(owner, str):
                engines = [part.strip() for part in owner.split(",") if part.strip()]
            else:
                engines = [str(part).strip() for part in owner]
            unknown = [engine for engine in engines if engine not in priorities]
            if unknown:
                errors.append(f"catalog 归属引擎不在 pipeline 中: {unknown}")
    elif operation == "priority":
        engine_id = change.get("engine_id") or candidate.get("engine")
        if engine_id not in priorities:
            errors.append(f"引擎 {engine_id!r} 不在 pipeline.json 中")
        if "priority" in change:
            try:
                new_priority = int(change["priority"])
            except (TypeError, ValueError):
                errors.append("priority 必须是整数")
            else:
                clashes = [
                    engine for engine, priority in priorities.items()
                    if priority == new_priority and engine != engine_id
                ]
                if clashes:
                    errors.append(
                        f"priority {new_priority} 已被 {clashes} 使用；"
                        "已启用引擎的 priority 必须唯一"
                    )
    else:
        errors.append(f"未知配置操作 {operation!r}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "risk_level": "high",
        "risk_reasons": ["流水线配置变更会影响每一笔交易"],
        "category": candidate.get("category", ""),
        "pattern": "",
        "target_file": target_file,
        "expected_effect": {},
    }


# ── approval grouping (one popup per config table) ──────────────────────────

RISK_ORDER = {"high": 0, "medium": 1, "low": 2}
APPROVAL_OPERATIONS = RULE_OPERATIONS | CONFIG_OPERATIONS


def _approval_item(item: dict[str, Any]) -> dict[str, Any]:
    validation = item.get("validation") or {}
    effect = validation.get("expected_effect") or {}
    reasons = list(validation.get("risk_reasons") or [])
    if not reasons:
        reasons = list(validation.get("medium_reasons") or [])
    trace = item.get("source_trace") or {}
    return {
        "candidate_id": item.get("candidate_id", ""),
        "operation": item.get("operation", ""),
        "engine": item.get("engine", ""),
        "target_file": item.get("target_file", ""),
        "category": validation.get("category") or item.get("category", ""),
        "pattern": validation.get("pattern") or common.candidate_pattern(item),
        "match_key": item.get("match_key") or {},
        "risk_level": item.get("risk_level", "high"),
        "reasons": reasons,
        "warnings": list(validation.get("warnings") or []),
        "expected_effect": {
            "matched_rows": effect.get("matched_rows", 0),
            "resolved_rows": effect.get("resolved_rows", 0),
            "conflicts": effect.get("conflicts", 0),
            "already_correct": effect.get("already_correct", 0),
        },
        "trace_method": trace.get("trace_method", ""),
        "trace_target_file": trace.get("target_file", ""),
        "trace_matched_rule": trace.get("matched_rule") or {},
    }


def _popup_plan(risk_groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Blueprint for one config table's popup(s).

    A question holds at most four options.  High/medium items must be chosen
    one by one, so they are chunked at three candidates plus ``全部拒绝``; low
    risk is one batch question.  More than four questions in total means the
    same table continues in a second popup (never mixed with another table).
    """
    questions: list[dict[str, Any]] = []
    for risk in ("high", "medium", "low"):
        ids = [item["candidate_id"] for item in risk_groups.get(risk) or []]
        if not ids:
            continue
        if risk == "low":
            questions.append(
                {"risk": risk, "kind": "batch", "candidate_ids": ids}
            )
        else:
            for offset in range(0, len(ids), 3):
                questions.append(
                    {
                        "risk": risk,
                        "kind": "multi_select",
                        "candidate_ids": ids[offset:offset + 3],
                    }
                )
    popups = (len(questions) + 3) // 4 if questions else 0
    return {
        "questions": questions,
        "popups_needed": popups,
        "continuation": popups > 1,
    }


def build_approval_groups(payload: dict[str, Any], repo: Path) -> dict[str, Any]:
    """Group approvable candidates by ``target_file`` then by risk tier.

    This is what stage five follows: one ``AskUserQuestion`` per config table,
    with questions ordered high -> medium -> low.  Suggestions, rejected and
    invalid candidates never appear here.
    """
    priorities = common.engine_priorities(repo)
    groups: dict[str, dict[str, Any]] = {}

    for item in payload.get("candidates", []):
        if item.get("operation") not in APPROVAL_OPERATIONS:
            continue
        if item.get("status") != "pending":
            continue
        target_file = str(item.get("target_file", ""))
        validation = item.get("validation") or {}
        if validation.get("valid") is not True:
            continue
        risk = str(item.get("risk_level", "high"))
        if risk not in RISK_ORDER:
            risk = "high"
        group = groups.setdefault(
            target_file,
            {
                "target_file": target_file,
                "engine": item.get("engine", ""),
                "engine_priority": priorities.get(
                    str(item.get("engine", "")), 9999
                ),
                "risk_groups": {"high": [], "medium": [], "low": []},
            },
        )
        group["risk_groups"][risk].append(_approval_item(item))

    ordered: list[dict[str, Any]] = []
    for group in groups.values():
        counts = {
            risk: len(group["risk_groups"][risk])
            for risk in ("high", "medium", "low")
        }
        group["counts"] = counts
        group["popup_plan"] = _popup_plan(group["risk_groups"])
        spec = common.spec_for(group["engine"], group["target_file"])
        group["table_note"] = common.table_role(spec) if spec else ""
        group["pattern_column"] = spec.pattern_column if spec else ""
        group["appendable"] = spec.appendable if spec else None
        ordered.append(group)
    ordered.sort(
        key=lambda group: (
            min(
                (RISK_ORDER[risk] for risk in ("high", "medium", "low")
                 if group["counts"][risk]),
                default=3,
            ),
            int(group.get("engine_priority") or 9999),
            group["target_file"],
        )
    )
    for index, group in enumerate(ordered, start=1):
        group["order"] = index

    return {
        "run_id": payload.get("run_id", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "popup_contract": {
            "per_config_table": True,
            "question_order": ["high", "medium", "low"],
            "max_questions_per_popup": 4,
            "max_options_per_question": 4,
            "low_batch_allowed": True,
            "medium_requires_per_item_choice": True,
            "high_requires_per_item_acknowledgement": True,
            "note": (
                "每张配置表一个弹窗；表内按高风险→中风险→低风险各一个问题。"
                "超过 4 个问题时，同表续一个弹窗，不与别的表混排。"
            ),
        },
        "groups": ordered,
    }


# ── match logic confirmation (I4) ───────────────────────────────────────────

def _match_example(item: dict[str, Any], kind: str) -> dict[str, Any]:
    """Normalise the different example shapes used by the simulator."""
    example: dict[str, Any] = {"kind": kind}
    for key in (
        "application_id", "transaction_id", "text", "expected", "pipeline",
        "problem_type", "cluster_id", "candidate_keyword", "winning_keyword",
        "winning_merchant", "winning_category",
    ):
        value = item.get(key)
        if value not in (None, ""):
            example[key] = value
    remaining = item.get("remaining_rules")
    if isinstance(remaining, list) and remaining:
        names = [
            str(hit.get("rule_id") or hit.get("pattern") or "")
            for hit in remaining[:5]
            if isinstance(hit, dict)
        ]
        example["remaining_rules"] = [name for name in names if name]
    return example


def _option_with_hash(
    item: dict[str, Any], option: dict[str, Any]
) -> dict[str, Any]:
    """Attach the post-patch hash used to lock an I4 decision.

    ``recommended``/``reject`` cover the current row.  Mechanical narrower or
    wider options cover the row after their patch is applied, so the agent can
    write the decision before ``revise`` and the hash still matches afterwards.
    Input-driven options cannot know the final row yet; they carry ``None`` and
    the agent fills the hash after ``revise`` + revalidation.
    """
    option = dict(option)
    row = option.get("row")
    if isinstance(row, dict):
        patched = dict(item)
        patched["row"] = {**(item.get("row") or {}), **row}
        option["row_hash_after"] = common.candidate_row_hash(patched)
    elif option.get("key") in {"recommended", "reject"}:
        option["row_hash_after"] = common.candidate_row_hash(item)
    else:
        option["row_hash_after"] = None
    return option


def _match_question(item: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_id = str(item.get("candidate_id", ""))
    effect = (item.get("validation") or {}).get("expected_effect") or {}
    pattern = str(
        (item.get("validation") or {}).get("pattern")
        or common.candidate_pattern(item)
    )
    return {
        "candidate_id": candidate_id,
        "risk_level": item.get("risk_level", "high"),
        "kind": "single_select",
        "header": f"匹配逻辑 {candidate_id}"[:12],
        "prompt": (
            f"{candidate_id} {item.get('operation', '')} "
            f"{Path(str(item.get('target_file', ''))).name}：建议 `{pattern}`；"
            f"样本命中 {effect.get('matched_rows', 0)} 行、"
            f"修复 {effect.get('resolved_rows', 0)} 行、"
            f"冲突 {effect.get('conflicts', 0)} 行、"
            f"已正确 {effect.get('already_correct', 0)} 行。"
            "是否按这个匹配逻辑执行？"
        ),
        "option_keys": [option["key"] for option in options],
    }


def _match_popup_plan(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Blueprint for one config table's I4 popup(s).

    High/medium candidates each get one single-select question (their four
    options are recommended/narrower/wider/reject or a keyword-safe subset).
    Low-risk candidates share one batch question; choosing "逐条调整" falls
    back to per-candidate questions in the next popup.
    """
    questions: list[dict[str, Any]] = []
    for risk in ("high", "medium"):
        for item in candidates:
            if item["risk_level"] == risk:
                questions.append({
                    "risk": risk,
                    "kind": "single_select",
                    "candidate_ids": [item["candidate_id"]],
                    "header": f"匹配逻辑 {item['candidate_id']}"[:12],
                    "option_keys": [o["key"] for o in item["options"]],
                })
    low = [item for item in candidates if item["risk_level"] == "low"]
    if low:
        questions.append({
            "risk": "low",
            "kind": "batch",
            "candidate_ids": [item["candidate_id"] for item in low],
            "header": "低风险匹配",
            "prompt": f"低风险 {len(low)} 条是否全部按建议匹配？",
            "option_keys": ["batch_recommended", "batch_review", "batch_reject"],
        })
    popups = (len(questions) + 3) // 4 if questions else 0
    return {
        "questions": questions,
        "popups_needed": popups,
        "continuation": popups > 1,
    }


def build_match_review(
    payload: dict[str, Any],
    repo: Path,
    review_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the I4 artifact: what each rule candidate will match, and why.

    Only pending, valid rule candidates appear here.  The agent prints the
    positive/negative examples, asks one popup per target table, then writes
    ``match_review_decisions.json``.  ``approve --build`` later verifies the
    recorded row hash so a post-confirmation edit cannot be approved silently.
    """
    priorities = common.engine_priorities(repo)
    intake = common.load_intake(review_dir) if review_dir else {}
    interaction_mode = str(intake.get("interaction_mode") or "standard")

    groups: dict[str, dict[str, Any]] = {}
    for item in payload.get("candidates", []):
        if item.get("operation") not in RULE_OPERATIONS:
            continue
        if item.get("status") != "pending":
            continue
        validation = item.get("validation") or {}
        if validation.get("valid") is not True:
            continue
        target_file = str(item.get("target_file", ""))
        engine = str(item.get("engine", ""))
        spec = common.spec_for(engine, target_file)
        if spec is None:
            continue
        group = groups.setdefault(
            target_file,
            {
                "target_file": target_file,
                "engine": engine,
                "engine_priority": priorities.get(engine, 9999),
                "match_mode": spec.match_mode,
                "pattern_column": spec.pattern_column,
                "table_note": common.table_role(spec),
                "appendable": spec.appendable,
                "required": interaction_mode != "minimal",
                "candidates": [],
            },
        )
        effect = validation.get("expected_effect") or {}
        options = [
            _option_with_hash(item, option)
            for option in common.match_review_options(spec, item, validation)
        ]
        if not options:
            continue
        positives = [
            _match_example(example, "positive")
            for example in (effect.get("examples") or [])[:5]
        ]
        negatives = [
            _match_example(example, "shadow")
            for example in (effect.get("shadow_examples") or [])[:3]
        ]
        negatives += [
            _match_example(example, "remaining_rule")
            for example in (effect.get("delete_remaining_rule_examples") or [])[:3]
        ]
        risk = str(item.get("risk_level", "high"))
        if risk not in RISK_ORDER:
            risk = "high"
        group["candidates"].append({
            "candidate_id": str(item.get("candidate_id", "")),
            "operation": item.get("operation", ""),
            "problem_type": item.get("problem_type", ""),
            "risk_level": risk,
            "category": validation.get("category") or item.get("category", ""),
            "match_mode": spec.match_mode,
            "matched_mode": validation.get("matched_mode") or {},
            "pattern": validation.get("pattern") or common.candidate_pattern(item),
            "match_key": item.get("match_key") or {},
            "row": item.get("row") or {},
            "row_hash": common.candidate_row_hash(item),
            "recommended": options[0]["key"],
            "options": options,
            "positive_examples": positives,
            "negative_examples": negatives,
            "expected_effect": {
                "matched_rows": effect.get("matched_rows", 0),
                "resolved_rows": effect.get("resolved_rows", 0),
                "conflicts": effect.get("conflicts", 0),
                "already_correct": effect.get("already_correct", 0),
                "shadowed_rows": effect.get("shadowed_rows", 0),
            },
            "warnings": list(validation.get("warnings") or []),
            "source_trace": item.get("source_trace") or {},
            "question": _match_question(item, options),
        })

    ordered: list[dict[str, Any]] = []
    for group in groups.values():
        candidates = group["candidates"]
        if not candidates:
            continue
        candidates.sort(
            key=lambda item: (
                RISK_ORDER.get(item["risk_level"], 0),
                item["candidate_id"],
            )
        )
        group["counts"] = {
            risk: sum(1 for item in candidates if item["risk_level"] == risk)
            for risk in ("high", "medium", "low")
        }
        group["popup_plan"] = _match_popup_plan(candidates)
        ordered.append(group)

    ordered.sort(
        key=lambda group: (
            min(
                (RISK_ORDER[risk] for risk in ("high", "medium", "low")
                 if group["counts"][risk]),
                default=3,
            ),
            int(group.get("engine_priority") or 9999),
            group["target_file"],
        )
    )
    for index, group in enumerate(ordered, start=1):
        group["order"] = index

    return {
        "run_id": payload.get("run_id", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "interaction_mode": interaction_mode,
        "required": interaction_mode != "minimal",
        "problem_type": intake.get("problem_type", ""),
        "direction": intake.get("direction", ""),
        "popup_contract": {
            "per_config_table": True,
            "question_order": ["high", "medium", "low"],
            "max_questions_per_popup": 4,
            "max_options_per_question": 4,
            "low_batch_allowed": True,
            "high_requires_per_item_choice": True,
            "note": (
                "先确认匹配逻辑再审批：每张配置表一个弹窗，高/中风险逐条选择，"
                "低风险可整组按建议；超过 4 个问题时同表续窗。"
            ),
        },
        "groups": ordered,
    }


def render_match_review(match_review: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## 匹配逻辑确认（I4）",
        "",
        "> 先向用户确认每条候选“靠什么匹配、会命中什么、会漏掉/误伤什么”，"
        "再进入阶段五审批。确认结果写入 `match_review_decisions.json`。",
        "",
        "| 顺序 | 配置表 | 引擎 | 高 | 中 | 低 | 弹窗数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for group in match_review.get("groups") or []:
        counts = group["counts"]
        plan = group["popup_plan"]
        lines.append(
            f"| {group['order']} | `{group['target_file']}` | "
            f"{group['engine']} | {counts['high']} | {counts['medium']} | "
            f"{counts['low']} | {plan['popups_needed']}"
            + ("（续）" if plan["continuation"] else "")
            + " |"
        )
    for group in match_review.get("groups") or []:
        lines += ["", f"### {group['order']}. `{group['target_file']}`", ""]
        if group.get("table_note"):
            lines += [f"> {group['table_note']}", ""]
        for item in group["candidates"]:
            option_text = "；".join(
                f"{option['key']}={option['label']}"
                for option in item["options"]
            )
            lines.append(
                f"- **{item['candidate_id']}**（{item['risk_level']}，"
                f"{item['match_mode']}）`{item['pattern']}`"
                f" → {option_text}；hash `{item['row_hash'][:19]}…`"
            )
            if item["positive_examples"]:
                first = item["positive_examples"][0]
                lines.append(
                    f"  - 应命中：`{str(first.get('text', ''))[:80]}`"
                    f"（期望 {first.get('expected', '')}）"
                )
            if item["negative_examples"]:
                first = item["negative_examples"][0]
                detail = (
                    first.get("text")
                    or first.get("candidate_keyword")
                    or ", ".join(first.get("remaining_rules") or [])
                    or ""
                )
                lines.append(f"  - 需避免：`{str(detail)[:80]}`")
    return lines


def render_approval_groups(groups: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## 审批弹窗（按配置表）",
        "",
        "> 每张配置表一个弹窗；表内按高风险 → 中风险 → 低风险分组。"
        "中/高风险逐条选择，低风险可整组通过。",
        "",
        "| 顺序 | 配置表 | 引擎 | 高 | 中 | 低 | 弹窗数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for group in groups.get("groups") or []:
        counts = group["counts"]
        plan = group["popup_plan"]
        lines.append(
            f"| {group['order']} | `{group['target_file']}` | "
            f"{group['engine']} | {counts['high']} | {counts['medium']} | "
            f"{counts['low']} | {plan['popups_needed']}"
            + ("（续）" if plan["continuation"] else "")
            + " |"
        )
    for group in groups.get("groups") or []:
        lines += ["", f"### {group['order']}. `{group['target_file']}`", ""]
        if group.get("table_note"):
            lines += [f"> 这张表存什么：{group['table_note']}", ""]
        for risk, label, how in (
            ("high", "高风险", "逐条确认"),
            ("medium", "中风险", "逐条看清"),
            ("low", "低风险", "可整组通过"),
        ):
            items = group["risk_groups"].get(risk) or []
            if not items:
                continue
            lines.append(
                f"- {label}（{how}）："
                + ", ".join(item["candidate_id"] for item in items)
            )
    return lines


# ── report ──────────────────────────────────────────────────────────────────

def render_report(
    payload: dict[str, Any],
    approval_groups: dict[str, Any] | None = None,
    match_review: dict[str, Any] | None = None,
) -> str:
    candidates = payload["candidates"]
    lines = [
        "# 候选规则校验报告",
        "",
        f"- run_id: `{payload.get('run_id', '')}`",
        f"- 候选总数: {len(candidates)}",
        f"- 通过: {sum(1 for item in candidates if item['validation']['valid'])}",
        f"- 未通过: {sum(1 for item in candidates if not item['validation']['valid'])}",
        f"- 低风险: {sum(1 for item in candidates if item.get('risk_level') == 'low')}",
        f"- 中风险: {sum(1 for item in candidates if item.get('risk_level') == 'medium')}",
        f"- 高风险: {sum(1 for item in candidates if item.get('risk_level') == 'high')}",
        "",
        "> 低风险只代表“在本样本上无冲突且可追加”，不代表生产全量安全。",
        "",
        "## 候选明细",
        "",
        "| ID | 问题类型 | 引擎 | 操作 | 类别 | 风险 | 匹配 | 修复 | 冲突 | 误分类 | 已正确 | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in candidates:
        effect = item["validation"].get("expected_effect") or {}
        status = "通过" if item["validation"]["valid"] else "未通过"
        lines.append(
            f"| {item['candidate_id']} | "
            f"{common.problem_type_label(item.get('problem_type', ''))} | "
            f"{item.get('engine', '')} | "
            f"{item.get('operation', '')} | {item['validation'].get('category', '')} | "
            f"{item.get('risk_level', '')} | {effect.get('matched_rows', '')} | "
            f"{effect.get('resolved_rows', '')} | {effect.get('conflicts', '')} | "
            f"{effect.get('conflicts_extra', '')} | "
            f"{effect.get('already_correct', '')} | {status} |"
        )
    problems = [item for item in candidates if not item["validation"]["valid"]]
    if problems:
        lines += ["", "## 未通过原因", ""]
        for item in problems:
            lines.append(f"### {item['candidate_id']}")
            for error in item["validation"]["errors"]:
                lines.append(f"- {error}")
            lines.append("")

    medium = [item for item in candidates if item.get("risk_level") == "medium"]
    if medium:
        lines += ["", "## 中风险候选（逐条看清）", ""]
        for item in medium:
            reasons = item["validation"].get("medium_reasons", [])
            lines.append(f"- **{item['candidate_id']}**: " + "; ".join(reasons))
        lines.append("")

    high = [item for item in candidates if item.get("risk_level") == "high"]
    if high:
        lines += ["", "## 高风险候选（逐条确认）", ""]
        for item in high:
            reasons = item["validation"].get("risk_reasons", [])
            lines.append(f"- **{item['candidate_id']}**: " + "; ".join(reasons))
        lines.append("")

    if match_review is not None:
        lines += render_match_review(match_review)
    if approval_groups is not None:
        lines += render_approval_groups(approval_groups)
    lines += [
        "",
        "## 下一步",
        "",
        "1. 按 `match_review.json` 每张配置表确认匹配逻辑（I4）：高/中风险逐条，低风险可整组按建议；"
        "结果写入 `match_review_decisions.json`。",
        "2. 如需调整匹配，用 `revise --patch` 改 `row`，重新 `check --only validate`，再确认匹配逻辑。",
        "3. 按 `approval_groups.json` 每张配置表弹一次审批窗：表内先高风险、再中风险、最后低风险。",
        "4. 高风险逐条确认（`risk_acknowledged=true`），中风险逐条选择，低风险可整组通过。",
        "5. 用 `bscat.py approve --build` 生成 `approvals.json`，再运行 `bscat.py apply` dry-run；确认后加 `--apply` 写入并自动验证。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="在样本上校验规则候选。")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--candidates", default=None, help="覆盖 candidates.json 路径。")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    candidates_path = (
        Path(args.candidates).expanduser().resolve()
        if args.candidates else review_dir / "candidates.json"
    )
    if not candidates_path.exists():
        raise SystemExit(f"未找到 candidates.json: {candidates_path}")
    if not (review_dir / "samples.csv").exists():
        raise SystemExit(f"{review_dir} 中未找到 samples.csv")
    if not (review_dir / "before_report.csv").exists():
        raise SystemExit(
            f"{review_dir} 中未找到 before_report.csv。请先运行 collect_evidence.py。"
        )

    payload = load_json(candidates_path)
    candidates = payload.get("candidates", [])
    if not candidates:
        raise SystemExit("candidates.json 中没有候选")

    ids = [str(item.get("candidate_id", "")) for item in candidates]
    if any(not value for value in ids):
        raise SystemExit("每个候选都需要非空的 candidate_id")
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise SystemExit(f"重复的 candidate_id: {duplicates}")

    priorities = common.engine_priorities(repo)
    contexts = load_sample_contexts(review_dir)

    # One merchant_kb scan for every initial exact-row lookup, instead of one
    # scan per edit/delete/keyword_update candidate.
    initial_match_keys = [
        item.get("match_key") or {}
        for item in candidates
        if item.get("operation") in {"edit", "delete", "keyword_update"}
        and str(item.get("target_file", "")) == "initial_engine/merchant_kb.csv"
    ]
    if initial_match_keys:
        preload_exact_rows(repo / "initial_engine" / "merchant_kb.csv", initial_match_keys)

    # Cross-candidate collisions.
    append_keys: Counter[tuple[str, str, str]] = Counter()
    edit_keys: Counter[tuple[str, str]] = Counter()
    for item in candidates:
        operation = item.get("operation")
        if operation == "append":
            row = item.get("row") or {}
            spec = common.spec_for(str(item.get("engine", "")), str(item.get("target_file", "")))
            column = spec.pattern_column if spec else "pattern"
            append_keys[
                (
                    str(item.get("target_file", "")),
                    str(row.get(column, "")),
                    str(item.get("category") or row.get("category", "")),
                )
            ] += 1
        if operation in {"edit", "delete", "keyword_update"}:
            edit_keys[
                (
                    str(item.get("target_file", "")),
                    json.dumps(item.get("match_key") or {}, sort_keys=True, default=str),
                )
            ] += 1

    for item in candidates:
        operation = item.get("operation")
        candidate_id = item.get("candidate_id")
        if operation not in ALL_OPERATIONS:
            item["validation"] = {
                "valid": False,
                "errors": [f"未知操作 {operation!r}"],
                "warnings": [],
                "risk_level": "high",
                "expected_effect": {},
            }
        elif operation in CONFIG_OPERATIONS:
            item["validation"] = validate_config_candidate(repo, item, priorities)
        elif operation == "keyword_update":
            item["validation"] = validate_keyword_update_candidate(repo, item, contexts)
        elif operation == "suggestion":
            item["validation"] = {
                "valid": True,
                "errors": [],
                "warnings": ["仅为建议；apply_rules.py 不会写入"],
                "risk_level": "high",
                "risk_reasons": ["不能自动应用"],
                "category": item.get("category", ""),
                "pattern": "",
                "target_file": item.get("target_file", ""),
                "expected_effect": {},
            }
        else:
            item["validation"] = validate_rule_candidate(repo, item, contexts)

        validation = item["validation"]
        if operation == "append":
            row = item.get("row") or {}
            spec = common.spec_for(str(item.get("engine", "")), str(item.get("target_file", "")))
            column = spec.pattern_column if spec else "pattern"
            key = (
                str(item.get("target_file", "")),
                str(row.get(column, "")),
                str(item.get("category") or row.get("category", "")),
            )
            if append_keys[key] > 1:
                validation["valid"] = False
                validation["errors"].append("另一个候选追加了相同的 pattern/category")
        if operation in {"edit", "delete", "keyword_update"}:
            key = (
                str(item.get("target_file", "")),
                json.dumps(item.get("match_key") or {}, sort_keys=True, default=str),
            )
            if edit_keys[key] > 1:
                validation["valid"] = False
                validation["errors"].append("另一个候选修改/删除了同一行")

        item["risk_level"] = validation.get("risk_level", "high")
        item["expected_effect"] = validation.get("expected_effect", {})
        if item.get("status") not in {"pending", "confirmed", "rejected"}:
            item["status"] = "pending"

    payload["validated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["validation_summary"] = {
        "total": len(candidates),
        "valid": sum(1 for item in candidates if item["validation"]["valid"]),
        "invalid": sum(1 for item in candidates if not item["validation"]["valid"]),
        "low_risk": sum(1 for item in candidates if item["risk_level"] == "low"),
        "medium_risk": sum(1 for item in candidates if item["risk_level"] == "medium"),
        "high_risk": sum(1 for item in candidates if item["risk_level"] == "high"),
    }
    approval_groups = build_approval_groups(payload, repo)
    match_review = build_match_review(payload, repo, review_dir)
    payload["validation_summary"]["approval_groups"] = len(
        approval_groups.get("groups") or []
    )
    payload["validation_summary"]["match_review_groups"] = len(
        match_review.get("groups") or []
    )
    dump_json(candidates_path, payload)
    dump_json(review_dir / "validation.json", payload)
    dump_json(review_dir / "approval_groups.json", approval_groups)
    dump_json(review_dir / common.MATCH_REVIEW_FILENAME, match_review)
    (review_dir / "validation_report.md").write_text(
        render_report(payload, approval_groups, match_review), encoding="utf-8"
    )

    print(json.dumps(payload["validation_summary"], ensure_ascii=False, indent=2))
    print(f"candidates.json  -> {candidates_path}")
    print(f"validation.json  -> {review_dir / 'validation.json'}")
    print(f"approval_groups.json -> {review_dir / 'approval_groups.json'}")
    print(f"match_review.json -> {review_dir / common.MATCH_REVIEW_FILENAME}")
    print(f"validation_report.md -> {review_dir / 'validation_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
