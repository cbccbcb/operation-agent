#!/usr/bin/env python3
"""Stage 4/6: match-level impact reconciliation and confirmed-set preview.

Two subcommands, both scoped to the review directory so the callers in
``validate.py`` and ``apply.py`` do not need to pass the repository again:

    python simulate.py reconcile --review reviews/run-001
    python simulate.py preview   --review reviews/run-001

``reconcile`` re-runs the same sample-level match simulation that
``validate_candidates.py`` used and compares it with the effect stored in
``candidates.json``.  A difference means the stored effect is stale (the
candidate row was edited by hand after validation) or the simulator no longer
agrees with the validator; stage 4 must stop and be investigated.

``preview`` aggregates the confirmed candidates before a write.  Append
candidates are re-simulated; edit/delete candidates reuse the effect computed
by the validator because their real takeover behaviour can only be observed by
rerunning the pipeline after the write (``validate_after.py``).

Both commands are intentionally match-level.  They do not model income amount
gates, gambling final claims, or cross-engine overwrites.  That limitation is
written into every report and must be repeated in the chat.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Windows pipes default to the active code page; keep Chinese output readable
# when the script is run directly (the wrappers set this for subprocesses).
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import common  # noqa: E402
import validate_candidates as vc  # noqa: E402

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

RULE_OPERATIONS = {"append", "edit", "delete", "keyword_update"}
CONFIG_OPERATIONS = {"catalog", "priority"}


# ── io / context helpers ────────────────────────────────────────────────────

def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def context_from_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Build the validator's context shape from an evidence.json row.

    ``evidence.json`` rows do not carry amount/account/bank/counterparty, so
    this fallback is only used when before_report.csv + samples.csv are both
    absent.  Reports label the source so a match-level result is never mistaken
    for a full-context simulation.
    """
    return {
        "key": (str(row.get("application_id", "")), str(row.get("transaction_id", ""))),
        "text": str(row.get("text", "") or ""),
        "dr_cr": "",
        "amount": None,
        "account_type": "",
        "bank": "",
        "counterparty": "",
        "expected": str(row.get("expected_category", "") or ""),
        "pipeline_category": str(row.get("pipeline_category", "") or ""),
        "pipeline_engine": str(row.get("pipeline_engine", "") or ""),
        "problem_type": str(row.get("problem_type", "") or ""),
        "cluster_id": str(row.get("cluster_id", "") or ""),
    }


def context_from_sample(row: Any) -> dict[str, Any]:
    """Build a minimal context from samples.csv when no before_report exists."""
    import pandas as pd

    def text(name: str) -> str:
        value = row.get(name)
        return "" if value is None or pd.isna(value) else str(value)

    expected = text("category")
    return {
        "key": (text("application_id"), text("transaction_id")),
        "text": text("text"),
        "dr_cr": text("dr_cr"),
        "amount": row.get("amount"),
        "account_type": text("account_type"),
        "bank": text("bank"),
        "counterparty": text("third_party"),
        "expected": expected,
        # Without a pipeline run there is no reliable before category.  Treat
        # the provided current category as the baseline; the report marks this
        # source as approximate.
        "pipeline_category": text("input_current_category"),
        "pipeline_engine": "",
        "problem_type": text("problem_type"),
        "cluster_id": "",
    }


def load_contexts(review_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Load sample contexts using the richest available source."""
    samples_path = review_dir / "samples.csv"
    before_path = review_dir / "before_report.csv"
    if samples_path.exists() and before_path.exists():
        return vc.load_sample_contexts(review_dir), "samples.csv+before_report.csv"

    evidence_path = review_dir / "evidence.json"
    if evidence_path.exists():
        rows = (load_json(evidence_path).get("rows") or [])
        if rows:
            return [context_from_evidence(row) for row in rows], "evidence.json"

    if samples_path.exists():
        import pandas as pd

        frame = pd.read_csv(
            samples_path,
            encoding="utf-8-sig",
            dtype={"application_id": "string", "transaction_id": "string"},
        )
        return [context_from_sample(row) for _, row in frame.iterrows()], "samples.csv"

    return [], "missing"


# ── simulation ──────────────────────────────────────────────────────────────

def simulate_append(
    candidate: dict[str, Any],
    contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replicate the append branch of validate_candidates.py exactly."""
    engine = str(candidate.get("engine", "")).strip()
    target_file = str(candidate.get("target_file", "")).strip()
    spec = common.spec_for(engine, target_file)
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
        "examples": [],
    }
    if spec is None:
        return {"ok": False, "reason": f"未知规则表 {target_file!r}", "effect": effect}

    values = {str(key): str(value) for key, value in (candidate.get("row") or {}).items()}
    category = vc.resolve_category(candidate, spec, values)
    synthetic = common.RuleRow(
        engine=engine,
        relative_path=target_file,
        line_number=0,
        values=values,
    )
    for context in contexts:
        if not common.rule_matches(spec, synthetic, context):
            continue
        effect["matched_rows"] += 1
        expected = context["expected"]
        pipeline = context["pipeline_category"]
        is_extra = (
            context.get("problem_type") == "extra"
            or (not expected and bool(pipeline))
        )
        if is_extra:
            effect["matched_extra_rows"] += 1
            if category:
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
    return {"ok": True, "reason": "", "effect": effect}


EFFECT_FIELDS = (
    "matched_rows",
    "resolved_rows",
    "conflicts",
    "already_correct",
    "unclassified_gain",
    "matched_extra_rows",
    "conflicts_extra",
    "matched_mismatch_other_category",
)


def compare_effects(
    declared: dict[str, Any],
    simulated: dict[str, Any],
) -> dict[str, Any]:
    differences = {}
    for field in EFFECT_FIELDS:
        old = int(declared.get(field, 0) or 0)
        new = int(simulated.get(field, 0) or 0)
        if old != new:
            differences[field] = {"declared": old, "simulated": new, "delta": new - old}
    return differences


def candidate_effect(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return the stage-4 effect, accepting the old nested location too."""
    effect = candidate.get("expected_effect")
    if isinstance(effect, dict) and effect:
        return effect
    validation = candidate.get("validation") or {}
    effect = validation.get("expected_effect")
    return effect if isinstance(effect, dict) else {}


# ── I6 precheck blueprint ───────────────────────────────────────────────────

def _approvable_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rule/config candidates that could enter stage-five approval."""
    approvable: list[dict[str, Any]] = []
    for candidate in candidates:
        operation = str(candidate.get("operation", ""))
        if operation not in RULE_OPERATIONS | CONFIG_OPERATIONS:
            continue
        if str(candidate.get("status", "pending")) == "rejected":
            continue
        validation = candidate.get("validation") or {}
        if validation.get("valid") is not True:
            continue
        approvable.append(candidate)
    return approvable


def _candidate_warnings(candidate: dict[str, Any]) -> list[str]:
    validation = candidate.get("validation") or {}
    return [str(value) for value in validation.get("warnings") or []]


def _candidate_trace_method(candidate: dict[str, Any]) -> str:
    trace = candidate.get("source_trace") or {}
    return str(trace.get("trace_method", "")) if isinstance(trace, dict) else ""


def build_precheck(
    review_dir: Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Build the I6 blueprint from validation + reconcile results.

    I6 is a conditional gate: all-green append runs skip it.  A mismatch,
    validation warning, shadowed/conflicting match, engine-default table, or
    an effect that can only be observed after writing (edit/delete/config)
    requires the user to see the impact and choose a write-after verify scope.
    """
    candidates = load_json(review_dir / "candidates.json").get("candidates") or []
    approvable = _approvable_candidates(candidates)
    profile = common.load_json_if_exists(review_dir / "task_profile.json")
    tier = str(((profile.get("overall") or {}).get("validation_tier", "")) or "")

    warnings: dict[str, list[str]] = {}
    shadowed: dict[str, int] = {}
    conflicts: dict[str, int] = {}
    engine_default: list[str] = []
    approximate: list[str] = []
    for candidate in approvable:
        candidate_id = str(candidate.get("candidate_id", ""))
        operation = str(candidate.get("operation", ""))
        if operation == "suggestion":
            continue
        item_warnings = _candidate_warnings(candidate)
        if item_warnings:
            warnings[candidate_id] = item_warnings
        effect = candidate_effect(candidate)
        shadowed_rows = int(effect.get("shadowed_rows", 0) or 0)
        if shadowed_rows:
            shadowed[candidate_id] = shadowed_rows
        conflict_rows = int(effect.get("conflicts", 0) or 0)
        if conflict_rows:
            conflicts[candidate_id] = conflict_rows
        if operation in RULE_OPERATIONS and _candidate_trace_method(candidate) == "engine_default":
            engine_default.append(candidate_id)
        if operation in {"edit", "delete", "keyword_update"} or operation in CONFIG_OPERATIONS:
            approximate.append(candidate_id)

    skipped = [item for item in report.get("skipped") or [] if isinstance(item, dict)]
    skipped_append = [
        item for item in skipped
        if str(item.get("operation", "")) == "append"
        and "写入后" not in str(item.get("reason", ""))
    ]
    reconcile_incomplete = bool(
        report.get("skipped_all") or report.get("source") == "missing" or skipped_append
    )

    triggers: list[str] = []
    if report.get("simulate_mismatch"):
        triggers.append("simulate_mismatch")
    if warnings:
        triggers.append("validation_warnings")
    if shadowed:
        triggers.append("shadowed_rows")
    if conflicts:
        triggers.append("conflicts")
    if engine_default:
        triggers.append("engine_default_trace")
    if approximate:
        triggers.append("approximate_effect")
    if reconcile_incomplete:
        triggers.append("reconcile_incomplete")

    if report.get("simulate_mismatch") or conflicts or approximate or tier == "full":
        suggested_scope = "full"
        scope_reason = (
            "存在影响面对账不一致/冲突，或候选真实效果只能写入后观察；"
            "需要完整链路与 baseline"
        )
    elif shadowed or warnings or engine_default or tier == "local":
        suggested_scope = "local"
        scope_reason = "存在遮蔽/告警/兜底归属；需要覆盖局部冲突与同引擎 claim"
    else:
        suggested_scope = "auto"
        scope_reason = "未发现额外风险；沿用 task_profile 的自动档位"

    triggered = bool(triggers)
    guided = common.interaction_mode(review_dir) == "guided" and bool(approvable)
    required = triggered or guided
    if report.get("simulate_mismatch"):
        decision_prompt = (
            f"影响面对账不一致 {int(report.get('mismatch_count', 0) or 0)} 条；"
            "继续会带着未解释的模拟偏差进入审批，必须先决定继续/重新校验/降级/停止"
        )
    elif triggered:
        decision_prompt = (
            f"即将进入阶段五审批；触发项: {', '.join(triggers)}。"
            "确认当前影响面可以接受并继续？"
        )
    else:
        decision_prompt = (
            "guided 模式：即使没有触发项，也确认当前影响面后再进入审批。"
            if guided else "校验与影响面全部通过，无需预审。"
        )
    questions: list[dict[str, Any]] = []
    if required:
        questions.append({
            "header": "I6 校验/影响面预审",
            "prompt": decision_prompt,
            "option_keys": ["continue", "revise", "degrade", "stop"],
        })
        questions.append({
            "header": "影响面处置",
            "prompt": (
                "当前影响面如何处理？accept=接受并进入审批；narrow=收窄范围；"
                "need_samples=先补样本；stop=停止本轮"
            ),
            "option_keys": ["accept", "narrow", "need_samples", "stop"],
        })
        questions.append({
            "header": "写后验证范围",
            "prompt": (
                f"建议 {suggested_scope}（{scope_reason}）。"
                "auto=按 task_profile；local=局部冲突；full=完整链路+baseline；"
                "current=沿用当前命令默认"
            ),
            "option_keys": ["auto", "local", "full", "current"],
        })

    material = {
        "triggers": triggers,
        "suggested_verify_scope": suggested_scope,
        "task_tier": tier,
        "reconcile": {
            "simulate_mismatch": bool(report.get("simulate_mismatch")),
            "mismatch_count": int(report.get("mismatch_count", 0) or 0),
            "source": str(report.get("source", "")),
            "skipped_all": bool(report.get("skipped_all")),
            "mismatch_ids": sorted(
                str(item.get("candidate_id", ""))
                for item in report.get("candidates") or []
                if isinstance(item, dict) and not item.get("match", True)
            ),
        },
        "candidates": [
            {
                "candidate_id": str(candidate.get("candidate_id", "")),
                "operation": str(candidate.get("operation", "")),
                "target_file": str(candidate.get("target_file", "")),
                "risk_level": str(candidate.get("risk_level", "")),
                "warnings": _candidate_warnings(candidate),
                "effect": {
                    field: int(candidate_effect(candidate).get(field, 0) or 0)
                    for field in EFFECT_FIELDS
                },
                "trace_method": _candidate_trace_method(candidate),
            }
            for candidate in sorted(
                approvable,
                key=lambda item: str(item.get("candidate_id", "")),
            )
        ],
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "required": required,
        "triggers": triggers,
        "summary": {
            "approvable": len(approvable),
            "validation_warnings": sorted(warnings),
            "shadowed_rows": shadowed,
            "conflicts": conflicts,
            "engine_default_trace": sorted(engine_default),
            "approximate_effect": sorted(approximate),
            "reconcile_incomplete": reconcile_incomplete,
            "reconcile_source": str(report.get("source", "")),
            "simulate_mismatch": bool(report.get("simulate_mismatch")),
            "mismatch_count": int(report.get("mismatch_count", 0) or 0),
            "task_tier": tier,
        },
        "suggested_verify_scope": suggested_scope,
        "scope_reason": scope_reason,
        "questions": questions,
        "fingerprint": common.sha256_json(material),
    }


def write_precheck(review_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    precheck = build_precheck(review_dir, report)
    path = review_dir / "precheck.json"
    dump_json(path, precheck)
    if precheck["required"]:
        detail = ", ".join(precheck["triggers"]) or "guided 模式"
        print(
            "\nI6 校验/影响面预审：需要（"
            + detail
            + f"）；蓝图 {path}；fingerprint={precheck['fingerprint']}"
        )
        print(
            "建议写后验证范围: "
            f"{precheck['suggested_verify_scope']}（{precheck['scope_reason']}）"
        )
        if "simulate_mismatch" in precheck["triggers"]:
            print(
                "对账不一致是硬触发：必须向用户说明差异；"
                "选择继续时需在决策中写 acknowledge_mismatch=true。"
            )
    else:
        print("\nI6 校验/影响面预审：无异常，可跳过。")
    return precheck


# ── reconcile ───────────────────────────────────────────────────────────────

def reconcile(review_dir: Path) -> int:
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        raise SystemExit(f"未找到 candidates.json: {candidates_path}")

    contexts, source = load_contexts(review_dir)
    candidates = load_json(candidates_path).get("candidates") or []
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "sample_rows": len(contexts),
        "simulate_mismatch": False,
        "mismatch_count": 0,
        "candidates": [],
        "skipped": [],
    }

    if not contexts:
        report["skipped_all"] = True
        report["reason"] = "缺少 samples.csv / before_report.csv / evidence.json rows，跳过影响面对账。"
        dump_json(review_dir / "impact_reconcile.json", report)
        write_precheck(review_dir, report)
        print("影响面对账：缺少样本上下文，已跳过（见 impact_reconcile.json）。")
        return 0

    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", ""))
        operation = str(candidate.get("operation", ""))
        if operation != "append":
            report["skipped"].append(
                {
                    "candidate_id": candidate_id,
                    "operation": operation,
                    "reason": "edit/delete/catalog/priority 的真实效果以写入后 validate_after.py 为准",
                }
            )
            continue

        declared = candidate_effect(candidate)
        if not declared:
            report["skipped"].append(
                {
                    "candidate_id": candidate_id,
                    "operation": operation,
                    "reason": "候选没有阶段四 expected_effect；请先重跑 validate.py",
                }
            )
            continue

        result = simulate_append(candidate, contexts)
        if not result["ok"]:
            report["skipped"].append(
                {
                    "candidate_id": candidate_id,
                    "operation": operation,
                    "reason": result["reason"],
                }
            )
            continue

        simulated = result["effect"]
        differences = compare_effects(declared, simulated)
        mismatch = bool(differences)
        if mismatch:
            report["simulate_mismatch"] = True
            report["mismatch_count"] += 1
        report["candidates"].append(
            {
                "candidate_id": candidate_id,
                "engine": candidate.get("engine", ""),
                "target_file": candidate.get("target_file", ""),
                "category": candidate.get("category", ""),
                "problem_type": candidate.get("problem_type", ""),
                "declared": {field: declared.get(field, 0) for field in EFFECT_FIELDS},
                "simulated": {field: simulated.get(field, 0) for field in EFFECT_FIELDS},
                "differences": differences,
                "match": not mismatch,
                "examples": simulated.get("examples", []),
            }
        )

    dump_json(review_dir / "impact_reconcile.json", report)
    write_precheck(review_dir, report)

    checked = len(report["candidates"])
    print("\n" + "=" * 64)
    print("影响面 / 命中数对账（match-level）")
    print("=" * 64)
    print(f"样本源: {source}（{len(contexts)} 行）")
    print(f"append 候选: {checked} 条 | 一致: {checked - report['mismatch_count']} | "
          f"不一致: {report['mismatch_count']}")
    if report["skipped"]:
        print(f"跳过（以写入后回归为准）: {len(report['skipped'])} 条")
    for item in report["candidates"]:
        if item["match"]:
            continue
        pairs = ", ".join(
            f"{field} 声明 {values['declared']} -> 模拟 {values['simulated']}"
            for field, values in item["differences"].items()
        )
        print(f"  ! {item['candidate_id']}: {pairs}")
    if report["simulate_mismatch"]:
        print("对账不一致：候选的 expected_effect 与当前模拟结果不符。")
        print("请检查候选是否在阶段四之后被手工修改，或模拟器与校验器语义是否漂移；"
              "不要直接降级为零增益。")
    else:
        print("对账一致：append 候选的阶段四效果可复现。")
    print("=" * 64)

    return 1 if report["simulate_mismatch"] else 0


# ── preview ─────────────────────────────────────────────────────────────────

def preview(review_dir: Path) -> int:
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        raise SystemExit(f"未找到 candidates.json: {candidates_path}")

    contexts, source = load_contexts(review_dir)
    candidates = load_json(candidates_path).get("candidates") or []
    confirmed = [
        item for item in candidates
        if item.get("status") == "confirmed"
        and item.get("operation") in RULE_OPERATIONS | CONFIG_OPERATIONS
    ]

    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "sample_rows": len(contexts),
        "confirmed": len(confirmed),
        "candidates": [],
        "totals": {
            "matched_rows": 0,
            "resolved_rows": 0,
            "unclassified_gain": 0,
            "newly_classified": 0,
            "conflicts": 0,
            "conflicts_extra": 0,
            "already_correct": 0,
        },
        "by_problem_type": {},
        "warnings": [],
        "approximate": True,
    }

    if not confirmed:
        report["warnings"].append("没有已确认候选；本次 dry-run 不会写入任何规则。")
        dump_json(review_dir / "preview_report.json", report)
        print("实测预览：没有已确认候选，跳过。")
        return 0

    by_problem: OrderedDict[str, dict[str, int]] = OrderedDict()
    for candidate in confirmed:
        candidate_id = str(candidate.get("candidate_id", ""))
        operation = str(candidate.get("operation", ""))
        problem_type = common.normalise_problem_type(candidate.get("problem_type", "")) or "unknown"
        stored = candidate_effect(candidate)
        effect_source = "validation"
        simulated = None
        if operation == "append":
            if contexts:
                result = simulate_append(candidate, contexts)
                if result["ok"]:
                    simulated = result["effect"]
                    effect_source = "simulated"
                else:
                    report["warnings"].append(
                        f"{candidate_id}: {result['reason']}；回退到阶段四 expected_effect"
                    )
            else:
                report["warnings"].append(
                    f"{candidate_id}: 缺少样本上下文，append 效果回退到阶段四 expected_effect"
                )

        effect = simulated if simulated is not None else stored
        effect_differences = {}
        if simulated is not None and isinstance(stored, dict) and stored:
            effect_differences = compare_effects(stored, simulated)
            if effect_differences:
                pairs = ", ".join(
                    f"{field} 阶段四 {values['declared']} -> 预览 {values['simulated']}"
                    for field, values in effect_differences.items()
                )
                report["warnings"].append(
                    f"{candidate_id}: 预览模拟与阶段四 expected_effect 不一致"
                    f"（候选可能在阶段四后被手工修改）：{pairs}"
                )
        matched = int(effect.get("matched_rows", 0) or 0)
        resolved = int(effect.get("resolved_rows", 0) or 0)
        unclassified = int(effect.get("unclassified_gain", 0) or 0)
        conflicts = int(effect.get("conflicts", 0) or 0)
        conflicts_extra = int(effect.get("conflicts_extra", 0) or 0)
        already = int(effect.get("already_correct", 0) or 0)
        # For extra, "newly classified" is the regression that matters: a row
        # that should stay unclassified receives a category.  For miss/diff it
        # is the previously-unclassified row that now gets the right category.
        newly_classified = conflicts_extra if problem_type == "extra" else unclassified

        report["totals"]["matched_rows"] += matched
        report["totals"]["resolved_rows"] += resolved
        report["totals"]["unclassified_gain"] += unclassified
        report["totals"]["newly_classified"] += newly_classified
        report["totals"]["conflicts"] += conflicts
        report["totals"]["conflicts_extra"] += conflicts_extra
        report["totals"]["already_correct"] += already
        bucket = by_problem.setdefault(
            problem_type, {"candidates": 0, "resolved_rows": 0, "conflicts": 0}
        )
        bucket["candidates"] += 1
        bucket["resolved_rows"] += resolved
        bucket["conflicts"] += conflicts

        item = {
            "candidate_id": candidate_id,
            "engine": candidate.get("engine", ""),
            "target_file": candidate.get("target_file", ""),
            "operation": operation,
            "category": candidate.get("category", ""),
            "problem_type": problem_type,
            "risk_level": candidate.get("risk_level", ""),
            "risk_acknowledged": candidate.get("risk_acknowledged") is True,
            "effect_source": effect_source,
            "effect_differences": effect_differences,
            "matched_rows": matched,
            "resolved_rows": resolved,
            "unclassified_gain": unclassified,
            "newly_classified": newly_classified,
            "conflicts": conflicts,
            "conflicts_extra": conflicts_extra,
            "already_correct": already,
            "examples": effect.get("examples", [])[:5],
        }
        if operation in {"edit", "delete", "keyword_update"}:
            item["approximate"] = True
            item["note"] = (
                "edit/delete/keyword_update 的真实接管以写入后 "
                "validate_after.py 为准"
            )
        if operation in CONFIG_OPERATIONS:
            item["effect_source"] = "config"
            item["note"] = "配置候选不参与样本命中统计"
            report["warnings"].append(
                f"{candidate_id}: 配置变更（{operation}）无法用样本命中预览"
            )
        report["candidates"].append(item)

        if operation == "append" and problem_type == "extra":
            report["warnings"].append(
                f"{candidate_id}: 多识别问题上的 append 可能新增误分类，阶段四应已判高风险"
            )
        if problem_type == "extra" and operation in {"edit", "delete"} and resolved == 0:
            report["warnings"].append(
                f"{candidate_id}: 多识别候选在样本上没有释放任何行"
            )
        if problem_type == "extra" and newly_classified > 0:
            report["warnings"].append(
                f"{candidate_id}: 多识别候选会给 {newly_classified} 行本应未分类的数据新增分类"
            )
        if conflicts:
            report["warnings"].append(
                f"{candidate_id}: 会命中 {conflicts} 行期望为其他分类的数据"
            )
        if resolved == 0 and operation in {"edit", "delete", "keyword_update"}:
            report["warnings"].append(
                f"{candidate_id}: 样本上未观察到修复（resolved_rows=0）"
            )
        if candidate.get("risk_level") == "high" and candidate.get("risk_acknowledged") is not True:
            report["warnings"].append(
                f"{candidate_id}: 高风险候选未记录 risk_acknowledged=true"
            )

    report["by_problem_type"] = dict(by_problem)
    if source != "samples.csv+before_report.csv":
        report["warnings"].append(
            f"样本上下文来自 {source}，金额/账户/对手方字段可能不完整，预览仅供参考"
        )
    report["warnings"].append(
        "预览只做 match-level 模拟：不含 income 金额阈值/行为门控，也不含跨引擎覆盖；"
        "写入后必须以 validate_after.py 的真实流水线回归为准"
    )
    report["warnings"].append(
        "edit/delete 的效果来自阶段四样本模拟，实际接管取决于删除后哪个引擎接手"
    )

    dump_json(review_dir / "preview_report.json", report)

    totals = report["totals"]
    print("\n" + "=" * 64)
    print("confirmed 集实测预览（match-level，未写入）")
    print("=" * 64)
    print(f"样本源: {source}（{len(contexts)} 行） | 已确认候选: {len(confirmed)} 条")
    print(
        "合计：匹配 {matched} | 预期修复 {resolved} | 新增分类 {unclassified} | "
        "冲突 {conflicts} | 已正确 {already}".format(
            matched=totals["matched_rows"],
            resolved=totals["resolved_rows"],
            unclassified=totals["newly_classified"],
            conflicts=totals["conflicts"],
            already=totals["already_correct"],
        )
    )
    for problem_type, bucket in report["by_problem_type"].items():
        print(
            f"  - {common.problem_type_label(problem_type)}: "
            f"{bucket['candidates']} 条候选，修复 {bucket['resolved_rows']} 行，"
            f"冲突 {bucket['conflicts']} 行"
        )
    print("")
    for item in report["candidates"]:
        print(
            f"  {item['candidate_id']} {item['engine']}/{item['operation']} "
            f"[{item['risk_level'] or '?'}] 匹配 {item['matched_rows']} | "
            f"修复 {item['resolved_rows']} | 新增分类 {item['newly_classified']} | "
            f"冲突 {item['conflicts']} | 已正确 {item['already_correct']} "
            f"({item['effect_source']})"
        )
    if report["warnings"]:
        print("\n注意：")
        for warning in dict.fromkeys(report["warnings"]):
            print(f"  - {warning}")
    print("=" * 64)

    return 0


# ── cli ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="match-level 影响面对账与 confirmed 集预览。"
    )
    parser.add_argument("command", choices=["reconcile", "preview"])
    parser.add_argument("--review", required=True, help="审核目录。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review_dir = Path(args.review).expanduser().resolve()
    if not review_dir.exists():
        raise SystemExit(f"审核目录不存在: {review_dir}")
    if args.command == "reconcile":
        return reconcile(review_dir)
    return preview(review_dir)


if __name__ == "__main__":
    raise SystemExit(main())
