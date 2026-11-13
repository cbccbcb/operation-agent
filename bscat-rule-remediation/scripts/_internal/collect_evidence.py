#!/usr/bin/env python3
"""Run AUS_BS_CAT_Model on the normalised sample and collect remediation evidence.

This is the bridge between the raw workbook and rule candidates.  It executes
the repository's real orchestrator (not a reimplementation), records what every
engine claimed, isolates the rows whose final category differs from the
expected category, and clusters those rows by transaction text so the skill can
reason about a handful of patterns instead of hundreds of rows.

    python collect_evidence.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001

Reads:  <review>/samples.csv
Writes: before_report.csv, claims.csv, mismatches.csv, clusters.csv,
        evidence.json, gap_summary.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402


# ── pipeline execution ──────────────────────────────────────────────────────

def import_pipeline(repo: Path):
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from classification_core.config import (  # noqa: WPS433
        load_category_owners,
        load_pipeline_config,
    )
    from classification_core.orchestrator import ClassificationOrchestrator  # noqa: WPS433

    return load_pipeline_config, load_category_owners, ClassificationOrchestrator


def run_pipeline(repo: Path, samples_path: Path):
    import pandas as pd

    load_pipeline_config, load_category_owners, orchestrator_cls = import_pipeline(repo)
    samples = pd.read_csv(
        samples_path,
        encoding="utf-8-sig",
        dtype={"application_id": "string", "transaction_id": "string"},
    )
    orchestrator = orchestrator_cls(
        config=load_pipeline_config(repo / "configs" / "pipeline.json"),
        category_owners=load_category_owners(repo / "configs" / "category_catalog.json"),
    )
    return samples, orchestrator.run(samples)


def claims_frame(run) -> "Any":
    import pandas as pd

    frames = []
    for execution in run.executions:
        claims = execution.claims
        if claims is None or claims.empty:
            continue
        frame = claims.copy()
        frame["engine_id"] = execution.engine_id
        frame["engine_version"] = execution.engine_version
        frame["engine_priority"] = execution.priority
        frame["engine_accepted_count"] = execution.accepted_count
        frames.append(frame)
    if not frames:
        return pd.DataFrame(
            columns=[
                "application_id", "transaction_id", "bscat", "counterparty",
                "classification_rule_id", "classification_reason", "stream_id",
                "priority", "engine_id", "engine_version", "engine_priority",
                "engine_accepted_count",
            ]
        )
    return pd.concat(frames, ignore_index=True, sort=False)


# ── routing assessment ──────────────────────────────────────────────────────

def _norm(value: Any) -> str:
    return "" if common.is_missing(value) else str(value).strip()


def _guard_block(
    owner: str,
    current_engine: str,
    current_category: str,
    current_rule_id: str,
    context: dict[str, Any],
) -> str:
    """Return the orchestrator/engine guard that blocks ``owner``, if any."""
    if owner == "liability" and current_category in {"Wages", "Centrelink"}:
        return (
            "orchestrator 会在 liability 引擎运行前，把 Wages/Centrelink 行从候选集中剔除"
        )
    if owner in {"rent", "gambling"} and current_engine in {"income", "liability"}:
        return (
            "orchestrator 会把已被 income/liability 声明的行从 rent/gambling 候选集中剔除"
        )
    if owner in {"income", "liability"} and current_engine == "gambling":
        return (
            "orchestrator 会在提交时丢弃 gambling 已声明行上的 income/liability 预测"
        )
    if owner == "all_other_credit" and owner != current_engine:
        if _norm(context.get("dr_cr")).lower() != "credit":
            return "all_other_credit 只处理 dr_cr=credit 的行"
        if current_engine != "unclassified" and current_category != "External Transfers":
            return (
                "all_other_credit 排除所有已有声明，只有 External Transfers 例外"
            )
    if owner == "catch_all" and owner != current_engine and current_engine != "unclassified":
        return "catch_all 只匹配尚无任何声明的行"
    return ""


def _candidate_file_hint(owner: str, expected: str) -> dict[str, str]:
    if owner == "initial":
        return {"file": "initial_engine/merchant_kb.csv", "pattern_column": "keywords"}
    if owner == "transfer":
        if expected == "Internal Transfer":
            return {
                "file": "transfer_engine/resources/transfer_internal_regex_rules.csv",
                "pattern_column": "pattern",
            }
        return {
            "file": "transfer_engine/resources/transfer_external_high_confidence_rules.csv",
            "pattern_column": "pattern",
        }
    if owner == "dishonour":
        return {"file": "dishonour_engine/resources/dishonour_rules.csv", "pattern_column": "pattern"}
    if owner == "gambling":
        return {"file": "gambling_engine/resources/gambling_rules.csv", "pattern_column": "pattern"}
    if owner == "income":
        group = "centrelink" if expected == "Centrelink" else "salary_packaging"
        return {
            "file": "income_engine/resources/income_pattern_rules.csv",
            "pattern_column": "pattern",
            "pattern_group": group,
        }
    if owner == "liability":
        mapping = {
            "Debt Collection": (
                "liability_engine/resources/debt_collection_rules.csv", "keyword"
            ),
            "Debt Consolidation": (
                "liability_engine/resources/debt_consolidation_rules.csv", "keyword"
            ),
            "Overdrawn": ("liability_engine/resources/overdrawn_rules.csv", "pattern"),
            "Dishonours": ("liability_engine/resources/dishonours_rules.csv", "pattern"),
            "Credit Card Repayments": (
                "liability_engine/resources/credit_card_rules.csv", "keyword"
            ),
        }
        if expected in mapping:
            path, column = mapping[expected]
            return {"file": path, "pattern_column": column}
        return {
            "file": "liability_engine/resources/counterparty_keyword_rules.csv",
            "pattern_column": "keyword",
        }
    if owner == "all_other_credit":
        return {
            "file": "all_other_credit_engine/resources/all_other_credit_rules.csv",
            "pattern_column": "pattern",
        }
    if owner == "fee":
        return {"file": "fee_engine/resources/fee_classification_rules.csv", "pattern_column": "pattern"}
    if owner == "rent":
        return {"file": "rent_engine/resources/rent_rules.csv", "pattern_column": "pattern"}
    if owner == "catch_all":
        return {"file": "catch_all_engine/resources/catch_all_rules.csv", "pattern_column": "pattern"}
    return {}


def assess_extra_routing(
    current_engine: str,
    current_category: str,
    current_rule_id: str,
    priorities: dict[str, int],
) -> dict[str, Any]:
    """Routing for a false positive: the row should have stayed unclassified.

    Extra has no owner category to add.  The only config-level fix is to
    delete or narrow the rule that claimed the row.  Code-level alternatives
    (guards, normalisation, precedence) belong in the code diagnosis report,
    not in an automatic code edit.
    """
    if not current_category:
        return {
            "owner_engine": "",
            "owner_priority": None,
            "status": "extra_already_clean",
            "action": "none",
            "actions": [],
            "risk_hint": "low",
            "reason": "该行当前未分类，多识别问题已不存在",
            "candidate_file_hint": {},
        }
    return {
        "owner_engine": "",
        "owner_priority": None,
        "current_engine": current_engine,
        "current_priority": priorities.get(current_engine),
        "current_rule_id": current_rule_id,
        "status": "extra_current_rule",
        "action": "delete_or_narrow_current_rule",
        "actions": ["delete_current_rule", "narrow_current_rule"],
        "risk_hint": "high",
        "reason": (
            f"该行本应保持未分类，但 {current_engine or '未知引擎'} 给出了 "
            f"{current_category!r}；需要删除或收窄命中的规则，并确认删除后"
            "不会被后续引擎再次误分类"
        ),
        "candidate_file_hint": {},
    }


def _assess_owner(
    owner: str,
    *,
    expected: str,
    current_engine: str,
    current_category: str,
    current_rule_id: str,
    context: dict[str, Any],
    priorities: dict[str, int],
    enabled: set[str],
) -> dict[str, Any]:
    owner_priority = priorities.get(owner, 9999)
    current_priority = priorities.get(current_engine, 0)
    hint = _candidate_file_hint(owner, expected)

    if owner not in enabled:
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "owner_disabled",
            "action": "priority_change",
            "risk_hint": "high",
            "reason": "目录中的归属引擎在 pipeline.json 中被禁用",
            "candidate_file_hint": hint,
        }

    guard = _guard_block(owner, current_engine, current_category, current_rule_id, context)
    if guard:
        if owner == current_engine:
            action = "edit_current_rule"
        else:
            action = "delete_or_edit_current_rule" if current_engine != "unclassified" else "manual_review"
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "blocked_guard",
            "action": action,
            "risk_hint": "high",
            "reason": guard,
            "candidate_file_hint": hint,
        }

    if current_engine == "unclassified":
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "feasible_add",
            "action": "append_owner_rule",
            "risk_hint": "high" if owner in {"income", "liability", "transfer"} else "low",
            "reason": "该行当前未分类；归属引擎的规则可以直接声明它",
            "candidate_file_hint": hint,
        }

    if owner == current_engine:
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "same_owner_wrong_category",
            "action": "edit_current_rule",
            "risk_hint": "high",
            "reason": (
                "该行已被归属引擎声明，但分类错误；修改胜出的规则，"
                "或增加优先级更高的规则"
            ),
            "candidate_file_hint": hint,
        }

    if owner_priority > current_priority:
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "feasible_add",
            "action": "append_owner_rule",
            "risk_hint": "high" if owner in {"income", "liability", "transfer"} else "low",
            "reason": (
                f"归属引擎在 {current_engine} 之后执行（priority {owner_priority} > "
                f"{current_priority}），它的声明会覆盖当前声明"
            ),
            "candidate_file_hint": hint,
        }

    # Owner runs earlier than the engine that produced the current category.
    if current_engine == "catch_all":
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "feasible_add",
            "action": "append_owner_rule",
            "risk_hint": "high" if owner in {"income", "liability", "transfer"} else "low",
            "reason": (
                "catch_all 只匹配未分类行，因此更早的归属声明会让 catch_all 无法命中"
            ),
            "candidate_file_hint": hint,
        }
    if current_engine == "all_other_credit" and current_category != "External Transfers":
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "feasible_add",
            "action": "append_owner_rule",
            "risk_hint": "high" if owner in {"income", "liability", "transfer"} else "low",
            "reason": (
                "all_other_credit 排除已有声明，因此更早的归属声明会阻止它再次声明该行"
            ),
            "candidate_file_hint": hint,
        }
    if current_engine == "fee" and current_rule_id == "fee_keyword_catchall":
        return {
            "owner_engine": owner,
            "owner_priority": owner_priority,
            "status": "feasible_add",
            "action": "append_owner_rule",
            "risk_hint": "high" if owner in {"income", "liability", "transfer"} else "low",
            "reason": (
                "当前 fee 声明来自 fee_keyword_catchall（unclassified_only=true）；"
                "更早的归属声明会阻止它"
            ),
            "candidate_file_hint": hint,
        }

    return {
        "owner_engine": owner,
        "owner_priority": owner_priority,
        "status": "overwritten_by_later",
        "action": "delete_or_edit_current_rule",
        "risk_hint": "high",
        "reason": (
            f"归属引擎在 {current_engine} 之前执行（priority {owner_priority} < "
            f"{current_priority}）；仅追加归属规则会被覆盖"
        ),
        "candidate_file_hint": hint,
    }


def assess_routing(
    expected: str,
    current_engine: str,
    current_category: str,
    current_rule_id: str,
    context: dict[str, Any],
    owners: dict[str, list[str]],
    priorities: dict[str, int],
    enabled: set[str],
    problem_type: str = "diff",
) -> dict[str, Any]:
    if not expected:
        return {
            "status": "no_expected_category",
            "action": "manual_review",
            "risk_hint": "high",
            "reason": "样本行没有期望分类",
        }
    if expected == current_category:
        return {
            "status": "already_correct",
            "action": "none",
            "risk_hint": "low",
            "reason": "流水线当前的分类已符合期望",
        }

    owner_list = owners.get(expected, [])
    if not owner_list:
        return {
            "status": "catalog_missing",
            "action": "catalog_change",
            "risk_hint": "high",
            "reason": f"分类 {expected!r} 在 category_catalog.json 中没有归属引擎",
            "owner_engines": [],
        }

    assessments = [
        _assess_owner(
            owner,
            expected=expected,
            current_engine=current_engine,
            current_category=current_category,
            current_rule_id=current_rule_id,
            context=context,
            priorities=priorities,
            enabled=enabled,
        )
        for owner in owner_list
    ]
    order = {"feasible_add": 0, "same_owner_wrong_category": 1,
             "overwritten_by_later": 2, "blocked_guard": 3, "owner_disabled": 4}
    assessments.sort(
        key=lambda item: (order.get(item["status"], 9), -item.get("owner_priority", 0))
    )
    chosen = dict(assessments[0])
    chosen["owner_engines"] = owner_list
    chosen["alternatives"] = assessments[1:]
    chosen["current_engine"] = current_engine
    chosen["current_priority"] = priorities.get(current_engine)
    chosen["problem_type"] = problem_type
    chosen["actions"] = _routing_actions(chosen, problem_type, priorities)
    return chosen


def _routing_actions(
    routing: dict[str, Any],
    problem_type: str,
    priorities: dict[str, int],
) -> list[str]:
    """Config actions that can realise this routing decision.

    ``action`` stays the single primary label for backward compatibility;
    ``actions`` is ordered and may contain both a delete and an append when a
    rule must be removed before the owner engine can claim the row.
    """
    action = str(routing.get("action", "") or "")
    status = str(routing.get("status", "") or "")

    if problem_type == "extra":
        return [] if status == "extra_already_clean" else [
            "delete_current_rule", "narrow_current_rule"
        ]
    if problem_type == "miss":
        return [] if action in {"", "none"} else [action]

    actions: list[str] = []
    if action and action != "none":
        actions.append(action)
    owner_priority = routing.get("owner_priority")
    current_priority = routing.get("current_priority")
    if status == "overwritten_by_later":
        actions = ["delete_current_rule", "append_owner_rule"]
    elif status == "feasible_add" and owner_priority is not None and current_priority is not None:
        if owner_priority > current_priority:
            actions = ["append_owner_rule"]
    elif status == "same_owner_wrong_category":
        actions = ["edit_current_rule"]
    elif status == "blocked_guard" and routing.get("owner_engine") != routing.get("current_engine"):
        actions = ["delete_or_edit_current_rule", "append_owner_rule"]
    # Preserve order, drop blanks and duplicates.
    seen: set[str] = set()
    return [
        item for item in actions
        if item and not (item in seen or seen.add(item))
    ]


# ── clustering ──────────────────────────────────────────────────────────────

def build_rows(
    samples,
    output,
    owners: dict[str, list[str]],
    priorities: dict[str, int],
    enabled: set[str],
    scope: str = "all",
    blank_expected: str = "unknown",
):
    import pandas as pd

    frame = output.copy()
    expected = samples["category"].fillna("").astype(str).str.strip()
    provided = (
        samples["input_current_category"].fillna("").astype(str).str.strip()
        if "input_current_category" in samples.columns
        else pd.Series("", index=samples.index)
    )
    explicit_types = (
        samples["problem_type"].fillna("").astype(str).str.strip()
        if "problem_type" in samples.columns
        else pd.Series("", index=samples.index)
    )
    explicit_sources = (
        samples["problem_type_source"].fillna("").astype(str).str.strip()
        if "problem_type_source" in samples.columns
        else pd.Series("", index=samples.index)
    )
    pipeline_category = frame["bscat"].fillna("").astype(str)
    pipeline_engine = frame["classification_engine"].fillna("").astype(str)
    pipeline_engine = pipeline_engine.mask(pipeline_engine.eq(""), "unclassified")
    pipeline_rule_id = frame["classification_rule_id"].fillna("").astype(str)
    pipeline_reason = frame["classification_reason"].fillna("").astype(str)
    pipeline_priority = frame["classification_priority"]
    scope_types = common.problem_scope_types(scope)

    rows = []
    for position in range(len(frame)):
        expected_value = expected.iat[position]
        current_value = pipeline_category.iat[position]
        explicit = explicit_types.iat[position]
        source = explicit_sources.iat[position]
        problem_type = common.infer_problem_type(
            expected_value, current_value, explicit, blank_expected
        )
        # An explicit miss/diff without an expected label cannot be routed;
        # keep the row as evidence but do not treat it as actionable.
        if explicit in {"miss", "diff"} and not expected_value:
            problem_type = "unknown"
        is_problem = _is_problem_row(problem_type, expected_value, current_value)
        selected = is_problem and problem_type in scope_types
        raw_text = samples["text"].iat[position] if "text" in samples.columns else ""
        context = {
            "text": "" if pd.isna(raw_text) else str(raw_text),
            "dr_cr": samples["dr_cr"].iat[position] if "dr_cr" in samples.columns else "",
            "amount": samples["amount"].iat[position] if "amount" in samples.columns else None,
            "account_type": samples["account_type"].iat[position] if "account_type" in samples.columns else "",
            "bank": samples["bank"].iat[position] if "bank" in samples.columns else "",
            "counterparty": samples["third_party"].iat[position] if "third_party" in samples.columns else "",
        }
        routing = {
            "status": "already_correct",
            "action": "none",
            "actions": [],
            "risk_hint": "low",
            "reason": "",
            "problem_type": problem_type,
        }
        cluster_id = ""
        if selected:
            if problem_type == "extra":
                routing = assess_extra_routing(
                    pipeline_engine.iat[position],
                    current_value,
                    pipeline_rule_id.iat[position],
                    priorities,
                )
                routing["problem_type"] = problem_type
            else:
                routing = assess_routing(
                    expected_value,
                    pipeline_engine.iat[position],
                    current_value,
                    pipeline_rule_id.iat[position],
                    context,
                    owners,
                    priorities,
                    enabled,
                    problem_type=problem_type,
                )
            cluster_id = "|".join(
                [
                    problem_type,
                    expected_value,
                    pipeline_engine.iat[position],
                    current_value,
                    common.cluster_text(context["text"]),
                ]
            )
        rows.append(
            {
                "position": position,
                "application_id": samples["application_id"].iat[position],
                "transaction_id": samples["transaction_id"].iat[position],
                "text": context["text"],
                "expected_category": expected_value,
                "provided_current_category": provided.iat[position],
                "pipeline_category": current_value,
                "pipeline_engine": pipeline_engine.iat[position],
                "pipeline_rule_id": pipeline_rule_id.iat[position],
                "pipeline_reason": pipeline_reason.iat[position],
                "pipeline_priority": pipeline_priority.iat[position],
                "problem_type": problem_type,
                "problem_type_source": source or "inferred",
                "is_problem": is_problem,
                "scope_selected": selected,
                "provided_mismatch": bool(
                    expected_value and provided.iat[position] and provided.iat[position] != expected_value
                ),
                "is_mismatch": selected,
                "cluster_key": cluster_id,
                "routing": routing,
                "context": context,
            }
        )
    return rows


def _is_problem_row(problem_type: str, expected: str, pipeline: str) -> bool:
    if problem_type == "miss":
        return bool(expected) and pipeline != expected
    if problem_type == "extra":
        return bool(pipeline)
    if problem_type == "diff":
        return bool(expected) and pipeline != expected
    return False


def build_clusters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        if row["is_mismatch"]:
            grouped.setdefault(row["cluster_key"], []).append(row)

    clusters = []
    for index, (key, members) in enumerate(grouped.items(), start=1):
        first = members[0]
        expected = first["expected_category"]
        current_engine = first["pipeline_engine"]
        current_category = first["pipeline_category"]
        cluster_id = f"C{index:04d}"
        for member in members:
            member["cluster_id"] = cluster_id
        cluster = {
            "cluster_id": cluster_id,
            "cluster_key": key,
            "problem_type": first["problem_type"],
            "problem_type_label": common.problem_type_label(first["problem_type"]),
            "count": len(members),
            "expected_category": expected,
            "current_category": current_category,
            "current_engine": current_engine,
            "current_rule_ids": sorted(
                {member["pipeline_rule_id"] for member in members if member["pipeline_rule_id"]}
            ),
            "routing": first["routing"],
            "keyword_hints": common.candidate_keywords(first["text"]),
            "clustered_text": common.cluster_text(first["text"]),
            "samples": [
                {
                    "application_id": member["application_id"],
                    "transaction_id": member["transaction_id"],
                    "text": member["text"],
                    "amount": member["context"].get("amount"),
                    "dr_cr": member["context"].get("dr_cr"),
                }
                for member in members[:5]
            ],
            "row_keys": [
                [member["application_id"], member["transaction_id"]]
                for member in members
            ],
            "matching_owner_rules": [],
            "matching_current_rules": [],
        }
        clusters.append(cluster)
    return clusters


def attach_rule_matches(
    repo: Path,
    clusters: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    priorities: dict[str, int],
) -> None:
    cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_key = {row["cluster_key"]: row for row in rows if row["is_mismatch"]}

    def lookup(engine: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        text = str(context.get("text", ""))
        key = (engine, text)
        if key not in cache:
            cache[key] = common.find_matching_rules(repo, engine, context, limit=10)
        return cache[key]

    for cluster in clusters:
        row = by_key.get(cluster.get("cluster_key", ""))
        if row is None:
            continue
        context = row["context"]
        owner = cluster["routing"].get("owner_engine")
        if owner:
            cluster["matching_owner_rules"] = lookup(owner, context)
        if cluster["current_engine"] not in {owner, "unclassified"}:
            cluster["matching_current_rules"] = lookup(cluster["current_engine"], context)
        # Extra has no owner; point the candidate at the table that actually
        # produced the false positive, preferring the rule id the pipeline
        # reported.
        if cluster.get("problem_type") == "extra":
            hint = cluster["routing"].get("candidate_file_hint") or {}
            if not hint:
                matches = cluster["matching_current_rules"]
                chosen = next(
                    (
                        hit for hit in matches
                        if hit.get("rule_id")
                        and hit["rule_id"] == row["pipeline_rule_id"]
                    ),
                    matches[0] if matches else None,
                )
                if chosen:
                    hint = {
                        "file": chosen["file"],
                        "pattern_column": common.spec_for(
                            cluster["current_engine"], chosen["file"]
                        ).pattern_column if common.spec_for(
                            cluster["current_engine"], chosen["file"]
                        ) else "pattern",
                        "rule_id": chosen.get("rule_id", ""),
                        "line": chosen.get("line"),
                    }
                cluster["routing"]["candidate_file_hint"] = hint


# ── artefacts ───────────────────────────────────────────────────────────────

def write_auto_scope(
    review_dir: Path,
    clusters: list[dict[str, Any]],
    scope: str,
) -> dict[str, Any] | None:
    """Write target_scope.json automatically for a single-type fast path.

    When the caller already chose ``--problem-type miss|extra|diff``, stage 2
    has nothing left to ask: every cluster of that type is in scope.  The file
    is only written when absent so a reviewer can still narrow the scope by
    hand without collect_evidence overwriting the edit on a rerun.
    """
    if scope not in {"miss", "extra", "diff"} or not clusters:
        return None
    path = review_dir / "target_scope.json"
    if path.exists():
        print("target_scope.json 已存在，保留现有范围（不自动覆盖）。")
        return None

    groups: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
    deferred: list[dict[str, Any]] = []
    for cluster in clusters:
        routing = cluster.get("routing") or {}
        problem_type = str(cluster.get("problem_type", "") or "").strip()
        # For extra, there is no expected category, so the catalog has no
        # owner engine.  The engine that over-classified the row is the one
        # whose rule must be deleted/narrowed: use current_engine as the scope
        # engine.  miss/diff still use the expected category's owner.
        engine = str(routing.get("owner_engine", "") or "").strip()
        scope_basis = "owner_engine"
        if not engine and problem_type == "extra":
            engine = str(
                cluster.get("current_engine")
                or routing.get("current_engine")
                or ""
            ).strip()
            scope_basis = "current_engine"
        item = {
            "cluster_id": cluster.get("cluster_id", ""),
            "problem_type": problem_type,
            "row_count": int(cluster.get("count", 0) or 0),
            "owner_engine": engine,
            "scope_basis": scope_basis,
        }
        if not engine or engine.lower() in {"unknown", "none"}:
            item["reason"] = "owner_engine/current_engine 为空/unknown，需人工归类"
            deferred.append(item)
            continue
        key = (engine, problem_type)
        group = groups.get(key)
        if group is None:
            group = {
                "engine": engine,
                "scope_basis": scope_basis,
                "problem_types": [problem_type],
                "cluster_ids": [],
                "row_count": 0,
            }
            groups[key] = group
        if problem_type not in group["problem_types"]:
            group["problem_types"].append(problem_type)
        group["cluster_ids"].append(cluster.get("cluster_id", ""))
        group["row_count"] += item["row_count"]

    payload = {
        "selected_at": datetime.now().isoformat(timespec="seconds"),
        "source": "user_prespecified",
        "selected_groups": list(groups.values()),
        "deferred_groups": deferred,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        f"target_scope.json -> {path}"
        f"（--problem-type {scope} 自动选择 {len(groups)} 组，{len(deferred)} 个聚类待人工归类）"
    )
    return payload


def write_outputs(
    repo: Path,
    review_dir: Path,
    samples_path: Path,
    samples,
    run,
    rows: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    priorities: dict[str, int],
    enabled: set[str],
    owners: dict[str, list[str]],
    scope: str = "all",
    blank_expected: str = "unknown",
) -> dict[str, Any]:
    import pandas as pd

    report = run.transactions.copy()
    report["expected_category"] = [row["expected_category"] for row in rows]
    report["provided_current_category"] = [row["provided_current_category"] for row in rows]
    report["pipeline_category"] = [row["pipeline_category"] for row in rows]
    report["pipeline_engine"] = [row["pipeline_engine"] for row in rows]
    report["pipeline_rule_id"] = [row["pipeline_rule_id"] for row in rows]
    report["pipeline_reason"] = [row["pipeline_reason"] for row in rows]
    report["problem_type"] = [row["problem_type"] for row in rows]
    report["problem_type_label"] = [
        common.problem_type_label(row["problem_type"]) for row in rows
    ]
    report["problem_type_source"] = [row["problem_type_source"] for row in rows]
    report["is_problem"] = [row["is_problem"] for row in rows]
    report["scope_selected"] = [row["scope_selected"] for row in rows]
    report["is_mismatch"] = [row["is_mismatch"] for row in rows]
    report["provided_mismatch"] = [row["provided_mismatch"] for row in rows]
    report["cluster_id"] = [row.get("cluster_id", "") for row in rows]
    report["routing_status"] = [row["routing"].get("status", "") for row in rows]
    report["routing_action"] = [row["routing"].get("action", "") for row in rows]
    report["routing_actions"] = [
        " / ".join(row["routing"].get("actions", [])) for row in rows
    ]
    report["routing_reason"] = [row["routing"].get("reason", "") for row in rows]
    report["owner_engine"] = [row["routing"].get("owner_engine", "") for row in rows]
    report["suggested_keywords"] = [
        " | ".join(common.candidate_keywords(row["text"])[:8]) for row in rows
    ]
    report.to_csv(review_dir / "before_report.csv", index=False, encoding="utf-8-sig")

    mismatches = report[report["is_mismatch"]].copy()
    mismatches.to_csv(review_dir / "mismatches.csv", index=False, encoding="utf-8-sig")

    claims = claims_frame(run)
    claims.to_csv(review_dir / "claims.csv", index=False, encoding="utf-8-sig")

    cluster_rows = []
    for cluster in clusters:
        routing = cluster["routing"]
        cluster_rows.append(
            {
                "cluster_id": cluster["cluster_id"],
                "problem_type": cluster["problem_type"],
                "problem_type_label": cluster["problem_type_label"],
                "count": cluster["count"],
                "expected_category": cluster["expected_category"],
                "current_category": cluster["current_category"],
                "current_engine": cluster["current_engine"],
                "routing_status": routing.get("status", ""),
                "routing_action": routing.get("action", ""),
                "routing_actions": " / ".join(routing.get("actions", [])),
                "risk_hint": routing.get("risk_hint", ""),
                "owner_engine": routing.get("owner_engine", ""),
                "reason": routing.get("reason", ""),
                "candidate_file": routing.get("candidate_file_hint", {}).get("file", ""),
                "keyword_hints": " | ".join(cluster["keyword_hints"][:8]),
                "sample_text": cluster["samples"][0]["text"] if cluster["samples"] else "",
            }
        )
    pd.DataFrame(cluster_rows).to_csv(
        review_dir / "clusters.csv", index=False, encoding="utf-8-sig"
    )
    auto_scope = write_auto_scope(review_dir, clusters, scope)

    selected_rows = [row for row in rows if row["is_mismatch"]]
    expected_counts = Counter(
        row["expected_category"] for row in selected_rows if row["expected_category"]
    )
    status_counts = Counter(row["routing"].get("status", "") for row in selected_rows)
    action_counts = Counter(row["routing"].get("action", "") for row in selected_rows)
    problem_counts = Counter(row["problem_type"] for row in selected_rows)
    all_problem_counts = Counter(row["problem_type"] for row in rows)
    category_pairs = Counter(
        (row["expected_category"], row["pipeline_category"])
        for row in selected_rows
    )

    summary = {
        "rows": len(rows),
        "expected_rows": sum(1 for row in rows if row["expected_category"]),
        "problems": len(selected_rows),
        "mismatches": len(selected_rows),
        "provided_mismatches": sum(1 for row in rows if row["provided_mismatch"]),
        "clusters": len(clusters),
        "problem_type_counts": dict(problem_counts.most_common()),
        "all_problem_type_counts": dict(all_problem_counts.most_common()),
        "expected_category_counts": dict(expected_counts.most_common()),
        "routing_status_counts": dict(status_counts.most_common()),
        "routing_action_counts": dict(action_counts.most_common()),
        "top_category_pairs": [
            {"expected": expected, "pipeline": pipeline, "count": count}
            for (expected, pipeline), count in category_pairs.most_common(20)
        ],
    }
    summary["problem_scope"] = scope
    summary["blank_expected_semantics"] = blank_expected
    summary["target_scope_auto"] = auto_scope is not None

    engines = [
        {
            "engine_id": execution.engine_id,
            "version": execution.engine_version,
            "priority": execution.priority,
            "candidate_count": execution.candidate_count,
            "prediction_count": execution.prediction_count,
            "accepted_count": execution.accepted_count,
            "duration_seconds": round(execution.duration_seconds, 3),
            "enabled": execution.engine_id in enabled,
        }
        for execution in run.executions
    ]

    evidence = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "run_id": run.run_id,
        "samples": {
            "path": str(samples_path),
            "sha256": hashlib.sha256(samples_path.read_bytes()).hexdigest(),
            "rows": len(samples),
        },
        "pipeline": {
            "engines": engines,
            "priorities": priorities,
            "owners": owners,
        },
        "summary": summary,
        "clusters": clusters,
        "rows": [
            {
                "application_id": row["application_id"],
                "transaction_id": row["transaction_id"],
                "text": row["text"],
                "expected_category": row["expected_category"],
                "provided_current_category": row["provided_current_category"],
                "pipeline_category": row["pipeline_category"],
                "pipeline_engine": row["pipeline_engine"],
                "pipeline_rule_id": row["pipeline_rule_id"],
                "problem_type": row["problem_type"],
                "problem_type_source": row["problem_type_source"],
                "is_problem": row["is_problem"],
                "scope_selected": row["scope_selected"],
                "is_mismatch": row["is_mismatch"],
                "provided_mismatch": row["provided_mismatch"],
                "cluster_id": row.get("cluster_id", ""),
                "routing": row["routing"],
            }
            for row in rows
        ],
    }
    (review_dir / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (review_dir / "gap_summary.md").write_text(
        render_gap_summary(summary, engines, clusters, priorities, scope),
        encoding="utf-8",
    )
    return {"summary": summary, "engines": engines}


def render_gap_summary(
    summary: dict[str, Any],
    engines: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    priorities: dict[str, int],
    scope: str = "all",
) -> str:
    scope_labels = {
        "all": "全部（漏识别 + 多识别 + 不一致）",
        "miss": "仅漏识别（miss）",
        "extra": "仅多识别（extra）",
        "diff": "仅不一致（diff）",
    }
    lines = [
        "# BSCAT 差异取证报告",
        "",
        f"- 本次问题范围: {scope_labels.get(scope, scope)}",
        f"- 样本行数: {summary['rows']:,}",
        f"- 有期望分类的行: {summary['expected_rows']:,}",
        f"- 本次纳入处理的问题行: {summary.get('problems', summary['mismatches']):,}",
        f"- 输入自带的期望/当前不一致: {summary['provided_mismatches']:,}",
        f"- 聚类数: {summary['clusters']:,}",
    ]
    problem_counts = summary.get("problem_type_counts", {})
    if problem_counts:
        lines.append(
            "- 问题类型分布: "
            + "，".join(
                f"{common.problem_type_label(key)} {value:,}"
                for key, value in problem_counts.items()
            )
        )
    lines += [
        "",
        "## 引擎执行概况",
        "",
        "| 引擎 | 优先级 | 候选 | 预测 | 接受 | 耗时(s) |",
        "|---|---|---|---|---|---|",
    ]
    for engine in engines:
        lines.append(
            f"| {engine['engine_id']} | {engine['priority']} | "
            f"{engine['candidate_count']:,} | {engine['prediction_count']:,} | "
            f"{engine['accepted_count']:,} | {engine['duration_seconds']} |"
        )

    lines += ["", "## 路由结论", ""]
    for status, count in summary["routing_status_counts"].items():
        lines.append(f"- `{status}`: {count:,}")
    if summary["routing_action_counts"]:
        lines.append("")
        for action, count in summary["routing_action_counts"].items():
            lines.append(f"- 建议动作 `{action}`: {count:,}")

    lines += [
        "",
        "## 主要差异类别",
        "",
        "| 期望分类 | 当前输出 | 行数 |",
        "|---|---|---|",
    ]
    for pair in summary["top_category_pairs"]:
        lines.append(f"| {pair['expected']} | {pair['pipeline']} | {pair['count']:,} |")

    lines += [
        "",
        "## 聚类明细（按行数排序，最多 30 个）",
        "",
        "| 聚类 | 问题类型 | 行数 | 期望 | 当前 | 当前引擎 | 路由状态 | 建议动作 | 关键词线索 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cluster in sorted(clusters, key=lambda item: -item["count"])[:30]:
        routing = cluster["routing"]
        sample = cluster["samples"][0]["text"] if cluster["samples"] else ""
        hints = " / ".join(cluster["keyword_hints"][:4]).replace("|", "\\|")
        lines.append(
            f"| {cluster['cluster_id']} | {cluster.get('problem_type_label', '')} | "
            f"{cluster['count']} | {cluster['expected_category'] or '（应未分类）'} | "
            f"{cluster['current_category']} | {cluster['current_engine']} | "
            f"{routing.get('status', '')} | "
            f"{' / '.join(routing.get('actions', [])) or routing.get('action', '')} | "
            f"`{hints}` |"
        )
        if sample:
            sample_display = sample[:90].replace("|", "\\|")
            lines.append(
                f"|  |  |  |  |  |  |  |  | 示例: `{sample_display}` |"
            )

    extra_clusters = [c for c in clusters if c.get("problem_type") == "extra"]
    if extra_clusters:
        lines += [
            "",
            "## 多识别（extra）需要处理的误分类规则",
            "",
            "> 多识别没有“期望分类”可补充，配置层面的修复方向是删除或收窄命中的规则；",
            "> 删除后是否会由后续引擎再次误分类，必须在写入后验证。",
            "",
            "| 聚类 | 当前引擎 | 当前分类 | 命中的规则 | 候选文件 |",
            "|---|---|---|---|---|",
        ]
        for cluster in extra_clusters[:30]:
            matches = cluster.get("matching_current_rules") or []
            rules = "; ".join(
                f"{hit.get('rule_id') or '(' + str(hit.get('line')) + ')'}"
                for hit in matches[:3]
            ) or cluster.get("current_rule_ids") or "(未定位)"
            hint = cluster["routing"].get("candidate_file_hint", {}).get("file", "")
            lines.append(
                f"| {cluster['cluster_id']} | {cluster['current_engine']} | "
                f"{cluster['current_category']} | {rules} | {hint} |"
            )

    lines += [
        "",
        "## 使用说明",
        "",
        "- 本报告只做取证与路由判断，不修改任何规则文件。",
        "- `mismatches.csv` 是逐行证据；`clusters.csv` 是聚合视图；`claims.csv` "
        "记录每个引擎实际接受的声明。",
        "- `problem_type` 表示差异性质：漏识别需要补充规则，多识别需要删除/收窄规则，"
        "不一致可能需要同时做两件事。",
        "- 下一步：基于聚类生成 `candidates.json`，再运行 `validate_candidates.py`。",
        "- 高优先级变更（编辑/删除既有规则、优先级、catalog、收入/负债/转账规则）"
        "必须逐条人工确认。",
    ]
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="收集规则修复所需的流水线证据。")
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--review", required=True, help="包含 samples.csv 的审核目录。")
    parser.add_argument("--samples", default=None, help="覆盖 samples.csv 路径。")
    parser.add_argument(
        "--problem-type",
        choices=["all", "miss", "extra", "diff"],
        default=None,
        help=(
            "本次要解决的问题类型；默认读取 parse_summary.json 中的 problem_scope。"
            "miss=漏识别，extra=多识别，diff=不一致，all=全部。"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    samples_path = (
        Path(args.samples).expanduser().resolve()
        if args.samples else review_dir / "samples.csv"
    )
    if not samples_path.exists():
        raise SystemExit(f"未找到 samples.csv: {samples_path}。请先运行 parse_input.py。")
    if not (repo / "configs" / "pipeline.json").exists():
        raise SystemExit(f"不是有效的 AUS_BS_CAT_Model 仓库: {repo}")

    owners = common.category_owner_map(repo)
    priorities = common.engine_priorities(repo)
    enabled = common.enabled_engines(repo)

    parse_summary_path = review_dir / "parse_summary.json"
    parse_summary = (
        json.loads(parse_summary_path.read_text(encoding="utf-8"))
        if parse_summary_path.exists() else {}
    )
    scope = args.problem_type or parse_summary.get("problem_scope", "all")
    blank_expected = parse_summary.get("blank_expected_semantics", "unknown")
    if scope == "extra":
        blank_expected = "extra"

    samples, run = run_pipeline(repo, samples_path)
    rows = build_rows(
        samples, run.transactions, owners, priorities, enabled,
        scope=scope, blank_expected=blank_expected,
    )
    clusters = build_clusters(rows)
    attach_rule_matches(repo, clusters, rows, priorities)

    result = write_outputs(
        repo, review_dir, samples_path, samples, run, rows, clusters,
        priorities, enabled, owners, scope=scope, blank_expected=blank_expected,
    )

    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"\nbefore_report.csv -> {review_dir / 'before_report.csv'}")
    print(f"evidence.json   -> {review_dir / 'evidence.json'}")
    print(f"gap_summary.md  -> {review_dir / 'gap_summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
