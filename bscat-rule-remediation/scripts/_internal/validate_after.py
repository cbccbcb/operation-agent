#!/usr/bin/env python3
"""Re-run the real pipeline after applying candidates and compare before/after.

Two comparisons matter:

* **row level** -- did the sample row move from wrong to right, and did any
  previously-correct row move to wrong?  This is the user-visible outcome.
* **claim level** -- did any engine change what it claimed, even if a later
  engine overwrote the row?  A hidden claim regression is still a regression
  and mirrors what ``baseline.py diff`` reports.

    python validate_after.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001 --scope auto

``--scope`` controls how much of the sample is compared:

* ``target`` -- the target clusters plus rows matching changed patterns;
* ``local``  -- target plus the affected engines / expected categories;
* ``full``   -- every row, every engine claim, and ``baseline.py diff``.

Even in ``target``/``local`` a global row-level regression guard remains: a row
outside the chosen scope that regresses or loses its classification still forces
a rollback recommendation.  Out-of-scope claim changes are reported for review
but do not by themselves force rollback.

Writes after_report.csv, claims_after.csv, validation_after.json and
validation_report.md.  Exits 1 when a row regression is found so a caller can
trigger rollback quickly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import baseline_guard  # noqa: E402
import collect_evidence as ce  # noqa: E402
import common  # noqa: E402
import rule_index  # noqa: E402


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _key(row) -> tuple[str, str]:
    return str(row["application_id"]), str(row["transaction_id"])


def _value(row) -> str:
    import pandas as pd

    value = row.get("bscat")
    return "" if value is None or pd.isna(value) else str(value)


def _expected(row) -> str:
    import pandas as pd

    value = row.get("expected_category")
    return "" if value is None or pd.isna(value) else str(value)


def _problem_type(row) -> str:
    import pandas as pd

    value = row.get("problem_type")
    if value is None or pd.isna(value):
        return ""
    return common.normalise_problem_type(value) or str(value)


def compare_rows(
    before,
    after,
    scope_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    import pandas as pd

    before_lookup = {_key(row): row for _, row in before.iterrows()}
    after_lookup = {_key(row): row for _, row in after.iterrows()}
    all_keys = set(before_lookup) | set(after_lookup)
    keys = sorted(all_keys if scope_keys is None else (set(scope_keys) & all_keys))

    resolved = []
    regressed = []
    still_wrong = []
    still_correct = []
    newly_classified = []
    lost_classification = []
    changed_rows = []
    by_problem_type: dict[str, dict[str, int]] = {}

    for key in keys:
        before_row = before_lookup.get(key)
        after_row = after_lookup.get(key)
        before_category = _value(before_row) if before_row is not None else ""
        after_category = _value(after_row) if after_row is not None else ""
        expected = _expected(before_row) if before_row is not None else (
            _expected(after_row) if after_row is not None else ""
        )
        problem_type = (
            _problem_type(before_row) if before_row is not None
            else (_problem_type(after_row) if after_row is not None else "")
        )
        item = {
            "application_id": key[0],
            "transaction_id": key[1],
            "problem_type": problem_type,
            "expected": expected,
            "before": before_category,
            "after": after_category,
            "text": str(before_row.get("text", "")) if before_row is not None else "",
            "cluster_id": (
                str(before_row.get("cluster_id", ""))
                if before_row is not None and pd.notna(before_row.get("cluster_id"))
                else ""
            ),
        }
        if problem_type == "extra":
            # The desired outcome is "no category at all".
            if before_category and not after_category:
                resolved.append(item)
            elif not before_category and after_category:
                regressed.append(item)
            elif before_category and after_category:
                still_wrong.append(item)
            else:
                still_correct.append(item)
        elif expected and before_category != expected and after_category == expected:
            resolved.append(item)
        elif expected and before_category == expected and after_category != expected:
            regressed.append(item)
        elif expected and before_category != expected and after_category != expected:
            still_wrong.append(item)
        elif expected and before_category == expected and after_category == expected:
            still_correct.append(item)

        # Lost classification is only a loss when the row was supposed to be
        # classified.  For an extra row, losing the unwanted category is the fix.
        if not before_category and after_category:
            newly_classified.append(item)
        if before_category and not after_category and problem_type != "extra":
            lost_classification.append(item)
        if before_category != after_category:
            changed_rows.append(item)

        bucket = by_problem_type.setdefault(
            problem_type or "unknown",
            {"rows": 0, "resolved": 0, "regressed": 0,
             "still_wrong": 0, "still_correct": 0},
        )
        bucket["rows"] += 1
        for name, collection in (
            ("resolved", resolved),
            ("regressed", regressed),
            ("still_wrong", still_wrong),
            ("still_correct", still_correct),
        ):
            if collection and collection[-1] is item:
                bucket[name] += 1
                break

    return {
        "resolved": resolved,
        "regressed": regressed,
        "still_wrong": still_wrong,
        "still_correct": still_correct,
        "newly_classified": newly_classified,
        "lost_classification": lost_classification,
        "changed_rows": changed_rows,
        "by_problem_type": by_problem_type,
        "counts": {
            "rows": len(keys),
            "resolved": len(resolved),
            "regressed": len(regressed),
            "still_wrong": len(still_wrong),
            "still_correct": len(still_correct),
            "newly_classified": len(newly_classified),
            "lost_classification": len(lost_classification),
            "changed": len(changed_rows),
        },
    }


def compare_claims(
    before,
    after,
    scope_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    def index(frame):
        result = {}
        for _, row in frame.iterrows():
            row_key = (
                str(row.get("application_id", "")),
                str(row.get("transaction_id", "")),
            )
            if scope_keys is not None and row_key not in scope_keys:
                continue
            key = (*row_key, str(row.get("engine_id", "")))
            result[key] = (
                str(row.get("bscat", "")),
                str(row.get("classification_rule_id", "")),
                str(row.get("counterparty", "")),
            )
        return result

    before_index = index(before)
    after_index = index(after)
    keys = set(before_index) | set(after_index)
    added = []
    removed = []
    changed = []
    by_engine: dict[str, Counter] = defaultdict(Counter)
    for key in sorted(keys):
        old = before_index.get(key)
        new = after_index.get(key)
        engine = key[2]
        if old is None and new is not None:
            added.append({"key": key, "after": new})
            by_engine[engine]["added"] += 1
        elif old is not None and new is None:
            removed.append({"key": key, "before": old})
            by_engine[engine]["removed"] += 1
        elif old != new:
            changed.append({"key": key, "before": old, "after": new})
            by_engine[engine]["changed"] += 1
        if new is not None:
            by_engine[engine]["after_total"] += 1
        if old is not None:
            by_engine[engine]["before_total"] += 1
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "by_engine": {engine: dict(counter) for engine, counter in by_engine.items()},
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }


def _frame_keys(frame) -> set[tuple[str, str]]:
    return {_key(row) for _, row in frame.iterrows()}


def _item_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("application_id", "")), str(item.get("transaction_id", ""))


def _cluster_keys(evidence: dict[str, Any]) -> dict[str, set[tuple[str, str]]]:
    result: dict[str, set[tuple[str, str]]] = {}
    for cluster in evidence.get("clusters") or []:
        cluster_id = str(cluster.get("cluster_id", ""))
        keys = set()
        for pair in cluster.get("row_keys") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                keys.add((str(pair[0]), str(pair[1])))
        result[cluster_id] = keys
    return result


def _candidate_min_tier(candidates: list[dict[str, Any]]) -> str:
    """Lowest validation tier implied by the applied operations themselves."""
    tier = "target"
    for candidate in candidates:
        operation = str(candidate.get("operation", "")).strip().lower()
        if operation == "suggestion":
            continue
        problem_type = common.normalise_problem_type(candidate.get("problem_type", ""))
        engine = str(candidate.get("engine", "")).strip()
        if engine in rule_index.STRUCTURAL_ENGINES or operation in rule_index.CONFIG_OPERATIONS:
            return "full"
        if operation in {"edit", "delete", "keyword_update"} or problem_type in {"diff", "extra"}:
            tier = "local"
    return tier


def _candidate_target_keys(
    candidates: list[dict[str, Any]],
    evidence: dict[str, Any],
    samples,
) -> set[tuple[str, str]]:
    """Target clusters plus every sample row matched by a changed pattern."""
    keys: set[tuple[str, str]] = set()
    clusters = _cluster_keys(evidence)
    for candidate in candidates:
        for cluster_id in candidate.get("evidence_cluster_ids") or []:
            keys |= clusters.get(str(cluster_id), set())
        for pair in candidate.get("sample_row_keys") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                keys.add((str(pair[0]), str(pair[1])))

        engine = str(candidate.get("engine", ""))
        target_file = str(candidate.get("target_file", ""))
        spec = common.spec_for(engine, target_file)
        pattern = common.candidate_pattern(candidate)
        if spec is None or not pattern or samples is None:
            continue
        synthetic = common.RuleRow(
            engine=engine,
            relative_path=target_file,
            line_number=0,
            values={
                str(key): str(value)
                for key, value in (candidate.get("row") or {}).items()
            },
        )
        for _, row in samples.iterrows():
            if common.rule_matches(
                spec,
                synthetic,
                {
                    "text": str(row.get("text", "")),
                    "dr_cr": str(row.get("dr_cr", "")),
                    "amount": row.get("amount"),
                },
            ):
                keys.add(_key(row))
    return keys


def _local_scope_keys(
    target_keys: set[tuple[str, str]],
    candidates: list[dict[str, Any]],
    evidence: dict[str, Any],
    before,
    after,
    before_claims,
    after_claims,
) -> tuple[set[tuple[str, str]], set[str], set[str]]:
    """Target plus the affected engines and expected categories."""
    keys = set(target_keys)
    engines = {
        str(candidate.get("engine", "")).strip()
        for candidate in candidates
        if candidate.get("engine")
    }
    categories = {
        str(candidate.get("category", "")).strip()
        for candidate in candidates
        if candidate.get("category")
    }
    clusters = _cluster_keys(evidence)
    for cluster in evidence.get("clusters") or []:
        owner = str((cluster.get("routing") or {}).get("owner_engine", ""))
        if owner in engines:
            keys |= clusters.get(str(cluster.get("cluster_id", "")), set())

    for frame in (before, after):
        for _, row in frame.iterrows():
            if _expected(row) in categories or _value(row) in categories:
                keys.add(_key(row))

    for frame in (before_claims, after_claims):
        for _, row in frame.iterrows():
            if str(row.get("engine_id", "")) in engines:
                keys.add(
                    (
                        str(row.get("application_id", "")),
                        str(row.get("transaction_id", "")),
                    )
                )
    return keys, engines, categories


def resolve_scope(
    requested: str,
    repo: Path,
    review_dir: Path,
    evidence: dict[str, Any],
    applied_candidates: list[dict[str, Any]],
) -> tuple[str, str]:
    """Resolve ``auto`` to target/local/full and explain the choice."""
    if requested in {"target", "local", "full"}:
        return requested, f"显式指定 --scope {requested}"

    profile_tier = ""
    source = "无 task_profile.json"
    profile_path = review_dir / "task_profile.json"
    if profile_path.exists():
        try:
            profile_tier = str(
                (load_json(profile_path).get("overall") or {}).get(
                    "validation_tier", ""
                )
            )
            source = "task_profile.json"
        except (OSError, ValueError):
            profile_tier = ""
    elif evidence:
        try:
            profile_tier = str(
                (rule_index.build_task_profile(repo, evidence).get("overall") or {})
                .get("validation_tier", "")
            )
            source = "evidence.json 即时推导"
        except (OSError, ValueError):
            profile_tier = ""

    candidate_tier = _candidate_min_tier(applied_candidates)
    tiers = [
        tier for tier in (profile_tier, candidate_tier)
        if tier in rule_index.TIER_ORDER
    ]
    if not tiers:
        return "local", "无法确定档位，默认 local"
    scope = max(tiers, key=lambda tier: rule_index.TIER_ORDER[tier])
    return scope, (
        f"auto: profile={profile_tier or '-'}, candidates={candidate_tier} ({source})"
    )


def build_scope_keys(
    scope: str,
    repo: Path,
    review_dir: Path,
    evidence: dict[str, Any],
    candidates: list[dict[str, Any]],
    samples,
    before,
    after,
    before_claims,
    after_claims,
) -> tuple[set[tuple[str, str]], dict[str, Any]]:
    all_keys = _frame_keys(before) | _frame_keys(after)
    target_fallback = ""
    target_keys = _candidate_target_keys(candidates, evidence, samples)
    if not target_keys:
        target_keys = {
            _key(row) for _, row in before.iterrows()
            if _problem_type(row) in {"miss", "extra", "diff"}
        }
        target_fallback = "problem_rows"
    if not target_keys:
        target_keys = set(all_keys)
        target_fallback = "all_rows"

    if scope == "full":
        keys = set(all_keys)
        engines: set[str] = set()
        categories: set[str] = set()
    elif scope == "target":
        keys = target_keys & all_keys
        engines = set()
        categories = set()
    else:
        keys, engines, categories = _local_scope_keys(
            target_keys, candidates, evidence, before, after,
            before_claims, after_claims,
        )
        keys &= all_keys

    return keys, {
        "scope": scope,
        "scope_keys": len(keys),
        "total_rows": len(all_keys),
        "target_keys": len(target_keys),
        "target_fallback": target_fallback,
        "engines": sorted(engines),
        "categories": sorted(categories),
    }


def attribute_rows(
    repo: Path,
    review_dir: Path,
    row_comparison: dict[str, Any],
    applied_ids: set[str],
    samples_path: Path,
) -> list[dict[str, Any]]:
    """Best-effort attribution of changed rows to applied candidates."""
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        return []
    candidates = load_json(candidates_path).get("candidates", [])
    applied = [
        candidate for candidate in candidates
        if candidate.get("candidate_id") in applied_ids
    ]

    # Build a text lookup for pattern-level attribution.
    import pandas as pd

    samples = pd.read_csv(
        samples_path, encoding="utf-8-sig",
        dtype={"application_id": "string", "transaction_id": "string"},
    )
    text_lookup = {
        (str(row["application_id"]), str(row["transaction_id"])): str(row.get("text", ""))
        for _, row in samples.iterrows()
    }

    results = []
    for candidate in applied:
        category = str(candidate.get("category", ""))
        engine = str(candidate.get("engine", ""))
        spec = common.spec_for(engine, str(candidate.get("target_file", "")))
        pattern = common.candidate_pattern(candidate)
        cluster_ids = set(candidate.get("evidence_cluster_ids") or candidate.get("samples") or [])
        row_keys = {
            (str(item[0]), str(item[1]))
            for item in (candidate.get("sample_row_keys") or [])
            if isinstance(item, (list, tuple)) and len(item) == 2
        }

        def attribute(rows):
            selected = []
            for row in rows:
                key = (row["application_id"], row["transaction_id"])
                if key in row_keys:
                    selected.append(row)
                    continue
                if cluster_ids and row.get("cluster_id") in cluster_ids:
                    selected.append(row)
                    continue
                if category and row["expected"] == category:
                    text = text_lookup.get(key, "")
                    if pattern and spec is not None:
                        synthetic = common.RuleRow(
                            engine=engine,
                            relative_path=str(candidate.get("target_file", "")),
                            line_number=0,
                            values={str(k): str(v) for k, v in (candidate.get("row") or {}).items()},
                        )
                        if common.rule_matches(
                            spec,
                            synthetic,
                            {"text": text, "dr_cr": "", "amount": None},
                        ):
                            selected.append(row)
            return selected

        resolved = attribute(row_comparison["resolved"])
        regressed = attribute(row_comparison["regressed"])
        results.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "engine": engine,
                "category": category,
                "operation": candidate.get("operation"),
                "actual_resolved": len(resolved),
                "actual_regressed": len(regressed),
                "expected_resolved": (candidate.get("expected_effect") or {}).get("resolved_rows"),
                "resolved_examples": resolved[:10],
                "regressed_examples": regressed[:10],
            }
        )
    return results


def run_repo_baseline(
    repo: Path,
    review_dir: Path,
    output: Path,
    touched_files: list[str] | None = None,
) -> dict[str, Any]:
    """Run ``baseline.py diff`` and compare it with the pre-write snapshot.

    The repository's own ``diff`` command cannot answer "did this write add a
    new difference": every rule edit invalidates a run_meta fingerprint,
    pre-existing drift also exits 1, and stdout always contains
    ``=== Summary ===``. So we compare two runs as multisets of report lines
    and attribute new lines to the files apply says it wrote.
    """
    result = baseline_guard.run_baseline(repo)
    if not result.get("ran"):
        return {
            "ran": False,
            "error": result.get("error", "baseline.py 未运行"),
            "interpreter": result.get("interpreter", ""),
        }
    text = (result.get("stdout") or "") + (result.get("stderr") or "")
    after = baseline_guard.make_snapshot(repo, result, "after")
    baseline_guard.write_snapshot(review_dir, after, text)

    touched = list(touched_files or [])
    expected = baseline_guard.expected_rule_hashes(repo)
    before = baseline_guard.load_snapshot(review_dir, "before")
    if before and before.get("repo") != str(repo.resolve()):
        # A snapshot from another repo cannot serve as a reference.
        before = None
    if before:
        comparison = baseline_guard.compare_snapshots(
            before, after, touched, expected
        )
    else:
        comparison = baseline_guard.summarise_single(after, touched, expected)
    comparison["output"] = str(output)
    comparison["snapshot_after"] = str(
        baseline_guard.snapshot_path(review_dir, "after")
    )
    return comparison


def render_baseline_lines(baseline: dict[str, Any] | None) -> list[str]:
    """Render the repository-level baseline section of validation_report.md."""
    lines = ["", "## 仓库级 baseline", ""]
    if not baseline:
        lines.append(
            "- 未运行（scope 非 full；需要仓库级基线时用 `--scope full` "
            "或显式 `--run-baseline`）"
        )
        return lines
    if not baseline.get("ran"):
        lines.append(f"- baseline 未运行: {baseline.get('error')}")
        return lines

    counts = baseline.get("counts") or {}
    files = baseline.get("files") or {}
    if baseline.get("before_snapshot"):
        lines.append(
            f"- 写前快照: `{baseline.get('before_generated_at')}`"
            f"（baseline exit {baseline.get('before_exit_code')}）"
        )
        lines.append(
            f"- 写后运行: baseline exit {baseline.get('exit_code')}，"
            f"耗时 {baseline.get('duration_s')}s"
        )
        lines.append(
            f"- **新增语义差异（本次写入引起）: "
            f"{counts.get('semantic', 0)}**"
        )
        lines.append(f"- 无法归因的新差异: {counts.get('unexpected', 0)}")
        lines.append(
            f"- 已解释的预期变化（指纹/配置）: {counts.get('expected', 0)}"
        )
        if counts.get("removed_total"):
            lines.append(
                f"- 写前存在、写后消失的差异行: "
                f"{counts['removed_total']}（既有噪声被本次写入改变）"
            )
        drift = baseline.get("drift_before") or {}
        drift_changed = drift.get("changed") or []
        lines.append(
            f"- 写前已存在的基线漂移: {len(drift_changed)} 个文件"
            "（不计入本次回归）"
        )
        for path in drift_changed[:10]:
            lines.append(f"  - {path}")
    else:
        lines.append(
            "- 未找到写前快照；以下只是写后单次 diff，"
            "**无法区分既有噪声**。"
        )
        lines.append(
            f"- baseline exit code: {baseline.get('exit_code')}，"
            f"耗时 {baseline.get('duration_s')}s"
        )
        lines.append(
            f"- 报告中的语义差异行: {counts.get('semantic', 0)}"
            "（可能包含写前既有噪声）"
        )

    changed = files.get("changed_by_write") or []
    if changed:
        lines.append(f"- 本次写入改变的文件: {len(changed)}")
        for path in changed[:20]:
            lines.append(f"  - {path}")
    unexpected_files = files.get("unexpected_changes") or []
    if unexpected_files:
        lines.append(
            f"- **意外变化的文件（不在本次写入范围）: "
            f"{len(unexpected_files)}**"
        )
        for path in unexpected_files[:20]:
            lines.append(f"  - {path}")
    untouched = files.get("touched_but_unchanged") or []
    if untouched:
        lines.append(
            f"- 写入声明但指纹未变化的文件: {len(untouched)}"
            "（写入可能是空操作，需确认）"
        )
        for path in untouched[:20]:
            lines.append(f"  - {path}")
    if files.get("input_changed"):
        lines.append("- **输入文件 sample.csv 在两次快照之间发生变化**")
    if files.get("run_meta_changed"):
        lines.append("- **baseline/run_meta.json 在两次快照之间发生变化**")
    if baseline.get("output"):
        lines.append(f"- 写后原始输出: `{baseline.get('output')}`")

    semantic = baseline.get("semantic_lines") or []
    if semantic:
        heading = (
            "### 新增语义差异（最多 50 条）"
            if baseline.get("before_snapshot")
            else "### 报告中的语义差异（最多 50 条，无法区分既有噪声）"
        )
        lines += ["", heading, ""]
        for line in semantic[:50]:
            lines.append(f"- `{line.strip()}`")
    unexpected = baseline.get("unexpected_lines") or []
    if unexpected:
        lines += ["", "### 无法归因的新差异（最多 20 条）", ""]
        for line in unexpected[:20]:
            lines.append(f"- `{line.strip()}`")
    return lines


def render_report(
    row_comparison: dict[str, Any],
    claim_comparison: dict[str, Any],
    attribution: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    applied_ids: list[str],
    applied_types: set[str] | None = None,
    scope_info: dict[str, Any] | None = None,
    out_of_scope: dict[str, Any] | None = None,
) -> str:
    counts = row_comparison["counts"]
    scope_info = scope_info or {}
    out_of_scope = out_of_scope or {}
    recommendation = "keep"
    reasons = []
    if counts["regressed"]:
        recommendation = "rollback"
        reasons.append(f"{counts['regressed']} 行原本正确，写入后发生回归")
    if counts["lost_classification"]:
        recommendation = "rollback"
        reasons.append(f"{counts['lost_classification']} 行失去了原有分类")
    if out_of_scope.get("regressed"):
        recommendation = "rollback"
        reasons.append(
            f"范围外有 {len(out_of_scope['regressed'])} 行发生回归；"
            "即使 scope 较小也不能忽略"
        )
    if out_of_scope.get("lost_classification"):
        recommendation = "rollback"
        reasons.append(
            f"范围外有 {len(out_of_scope['lost_classification'])} 行失去原有分类"
        )
    if counts["resolved"] == 0 and applied_ids:
        recommendation = "review"
        reasons.append("没有任何样本行被修复；该改动可能无效")
    by_type = row_comparison.get("by_problem_type", {})
    for problem_type in sorted(applied_types or set()):
        if by_type.get(problem_type, {}).get("resolved", 0) == 0:
            if recommendation != "rollback":
                recommendation = "review"
            reasons.append(
                f"已应用 {common.problem_type_label(problem_type)} 候选，"
                "但该问题类型没有任何样本行被修复"
            )
    if claim_comparison["counts"]["changed"] or claim_comparison["counts"]["removed"]:
        reasons.append(
            f"范围内 claim-level changes: +{claim_comparison['counts']['added']} / "
            f"-{claim_comparison['counts']['removed']} / "
            f"~{claim_comparison['counts']['changed']}"
        )
    if out_of_scope.get("claim_changes") and recommendation == "keep":
        recommendation = "review"
        reasons.append(
            f"范围外有 {len(out_of_scope['claim_changes'])} 条 claim 变化；"
            "确认是否为该规则的预期影响，必要时改用 --scope full"
        )
    baseline_files = (baseline or {}).get("files") or {}
    if baseline_files.get("unexpected_changes") and recommendation == "keep":
        recommendation = "review"
        reasons.append(
            f"逐文件指纹显示本次未写入的 "
            f"{len(baseline_files['unexpected_changes'])} 个文件也发生了变化；"
            "需人工确认是否为外部改动"
        )
    if baseline and baseline.get("has_differences"):
        baseline_counts = baseline.get("counts") or {}
        new_count = baseline_counts.get("semantic", 0) + baseline_counts.get(
            "unexpected", 0
        )
        if baseline.get("before_snapshot"):
            reasons.append(
                f"仓库级 baseline 相对写前新增 {new_count} 条差异；"
                "明细见 validation_report.md 的 baseline 段落"
            )
        else:
            reasons.append(
                "仓库级 baseline.py diff 报告了差异；"
                "没有写前快照，无法区分既有噪声"
            )

    lines = [
        "# 应用后验证报告",
        "",
        f"- 已应用候选: {', '.join(applied_ids) if applied_ids else '(未记录)'}",
        f"- 验证范围: **{scope_info.get('scope', 'full')}** "
        f"({scope_info.get('scope_keys', counts['rows'])}/"
        f"{scope_info.get('total_rows', counts['rows'])} 行)"
        + (f" — {scope_info['reason']}" if scope_info.get("reason") else ""),
        f"- 结论: **{recommendation}**",
    ]
    if scope_info.get("target_fallback"):
        lines.append(
            f"- 注意: 目标聚类行缺失，target 范围回退为 {scope_info['target_fallback']}"
        )
    for reason in reasons:
        lines.append(f"- 原因: {reason}")
    lines += [
        "",
        "## 行级结果",
        "",
        "| 指标 | 行数 |",
        "|---|---|",
        f"| 修复 | {counts['resolved']} |",
        f"| 回归 | {counts['regressed']} |",
        f"| 仍未修复 | {counts['still_wrong']} |",
        f"| 仍然正确 | {counts['still_correct']} |",
        f"| 新分类（漏识别/不一致为改善；多识别为回归） | {counts['newly_classified']} |",
        f"| 丢失分类（多识别的丢失视为修复） | {counts['lost_classification']} |",
    ]
    by_type = row_comparison.get("by_problem_type", {})
    if by_type:
        lines += [
            "",
            "## 按问题类型统计",
            "",
            "| 问题类型 | 行数 | 修复 | 回归 | 仍未修复 | 仍然正确 |",
            "|---|---|---|---|---|---|",
        ]
        for problem_type in ("miss", "extra", "diff", "correct", "unknown"):
            stats = by_type.get(problem_type)
            if not stats:
                continue
            lines.append(
                f"| {common.problem_type_label(problem_type)} | {stats['rows']} | "
                f"{stats['resolved']} | {stats['regressed']} | "
                f"{stats['still_wrong']} | {stats['still_correct']} |"
            )

    lines += [
        "",
        "## 声明级变化（范围内的所有引擎）",
        "",
        "| 引擎 | 前 | 后 | 新增 | 删除 | 变更 |",
        "|---|---|---|---|---|---|",
    ]
    for engine in sorted(claim_comparison["by_engine"]):
        stats = claim_comparison["by_engine"][engine]
        lines.append(
            f"| {engine} | {stats.get('before_total', 0)} | "
            f"{stats.get('after_total', 0)} | {stats.get('added', 0)} | "
            f"{stats.get('removed', 0)} | {stats.get('changed', 0)} |"
        )

    if attribution:
        lines += [
            "",
            "## 候选效果",
            "",
            "| 候选 | 类别 | 操作 | 预期修复 | 实际修复 | 实际回归 |",
            "|---|---|---|---|---|---|",
        ]
        for item in attribution:
            lines.append(
                f"| {item['candidate_id']} | {item['category']} | {item['operation']} | "
                f"{item.get('expected_resolved', '')} | {item['actual_resolved']} | "
                f"{item['actual_regressed']} |"
            )

    if row_comparison["regressed"]:
        lines += ["", "## 回归明细（最多 50 条）", ""]
        for row in row_comparison["regressed"][:50]:
            lines.append(
                f"- `{row['application_id']}/{row['transaction_id']}`: "
                f"{row['expected']} -> {row['after']} (before: {row['before']}) "
                f"| {row['text'][:120]}"
            )
    if row_comparison["still_wrong"]:
        lines += ["", "## 仍未修复（最多 30 条）", ""]
        for row in row_comparison["still_wrong"][:30]:
            expectation = (
                "应保持未分类" if row.get("problem_type") == "extra"
                else f"期望 {row['expected']}"
            )
            lines.append(
                f"- `{row['application_id']}/{row['transaction_id']}`: "
                f"{expectation}, 实际 {row['after'] or '（未分类）'} "
                f"| {row['text'][:120]}"
            )

    if out_of_scope:
        lines += ["", "## 范围外守卫", ""]
        lines.append(
            f"- 范围外回归: {len(out_of_scope.get('regressed') or [])} 行"
        )
        lines.append(
            f"- 范围外丢失分类: {len(out_of_scope.get('lost_classification') or [])} 行"
        )
        lines.append(
            f"- 范围外 claim 变化: {len(out_of_scope.get('claim_changes') or [])} 条"
        )
        for row in (out_of_scope.get("regressed") or [])[:20]:
            lines.append(
                f"- 回归 `{row.get('application_id')}/{row.get('transaction_id')}`: "
                f"{row.get('expected')} -> {row.get('after')} "
                f"(before: {row.get('before')}) | {row.get('text', '')[:100]}"
            )
        for row in (out_of_scope.get("lost_classification") or [])[:20]:
            lines.append(
                f"- 丢分类 `{row.get('application_id')}/{row.get('transaction_id')}`: "
                f"{row.get('before')} -> （未分类） | {row.get('text', '')[:100]}"
            )
        if out_of_scope.get("claim_changes"):
            lines.append(
                "- 范围外 claim 变化默认只要求复核；若确认非预期，改用 `--scope full` 重跑。"
            )

    lines += render_baseline_lines(baseline)

    lines += [
        "",
        "## 建议",
        "",
    ]
    if recommendation == "rollback":
        lines += [
            "1. 立即运行 `scripts/bscat.py apply --review <review> --rollback --apply` 恢复 apply 前的文件。",
            "2. 重新运行本脚本确认回归消失。",
            "3. 回到候选生成阶段，缩小模式或改为编辑既有规则。",
        ]
    elif recommendation == "review":
        lines += [
            "1. 检查候选是否命中样本（`mismatches.csv` / `validation.json`）。",
            "2. 检查引擎优先级或 guard 是否阻止了候选生效。",
            "3. 不要把“未生效”当成“安全”；调整后再验证。",
        ]
    else:
        lines += [
            "1. 样本验证通过；仍需人工审阅 `git diff`。",
            "2. 如需扩大到全量，请另跑 `baseline.py diff` 并比较 claim 级差异。",
            "3. 不要自动提交；由用户决定 commit/push。",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="写入候选后验证真实流水线。")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--samples", default=None)
    parser.add_argument(
        "--scope",
        choices=["auto", "target", "local", "full"],
        default="auto",
        help=(
            "验证范围。auto: 读取 task_profile.json 与已应用候选取较高档位；"
            "target: 目标聚类+命中新/旧 pattern 的行；local: 再加同引擎/同分类行；"
            "full: 全量行+全部 claim+baseline。默认 auto。"
        ),
    )
    parser.add_argument(
        "--run-baseline",
        action="store_true",
        help="同时运行仓库的 baseline.py diff（较慢，仅供参考）。scope=full 时自动启用。",
    )
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    samples_path = (
        Path(args.samples).expanduser().resolve()
        if args.samples else review_dir / "samples.csv"
    )
    for required in ("samples.csv", "before_report.csv", "claims.csv"):
        if not (review_dir / required).exists():
            raise SystemExit(f"{required} 不存在于 {review_dir}")

    # Rerun the real pipeline.
    try:
        samples, run = ce.run_pipeline(repo, samples_path)
    except Exception as exc:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"写入后流水线失败: {exc}",
            "recommendation": "rollback",
        }
        dump_json(review_dir / "validation_after.json", report)
        (review_dir / "validation_report.md").write_text(
            "# 应用后验证报告\n\n流水线在应用候选后失败，建议立即回滚。\n\n"
            f"```\n{exc}\n```\n",
            encoding="utf-8",
        )
        print(f"流水线失败: {exc}")
        return 2

    import pandas as pd

    after = run.transactions.copy()
    after["expected_category"] = samples["category"].fillna("").astype(str).str.strip().values
    after["provided_current_category"] = (
        samples["input_current_category"].fillna("").astype(str).str.strip().values
        if "input_current_category" in samples.columns else ""
    )
    after["pipeline_category"] = after["bscat"].fillna("").astype(str)
    after["pipeline_engine"] = (
        after["classification_engine"].fillna("").astype(str).replace("", "unclassified")
    )
    if "problem_type" in samples.columns:
        after["problem_type"] = samples["problem_type"].fillna("").astype(str).values
    after.to_csv(review_dir / "after_report.csv", index=False, encoding="utf-8-sig")
    claims_after = ce.claims_frame(run)
    claims_after.to_csv(review_dir / "claims_after.csv", index=False, encoding="utf-8-sig")

    before = pd.read_csv(
        review_dir / "before_report.csv", encoding="utf-8-sig",
        dtype={"application_id": "string", "transaction_id": "string"},
    )
    before_claims = pd.read_csv(
        review_dir / "claims.csv", encoding="utf-8-sig",
        dtype={"application_id": "string", "transaction_id": "string"},
    )

    apply_report_path = review_dir / "apply_report.json"
    applied_ids: list[str] = []
    touched_files: list[str] = []
    if apply_report_path.exists():
        apply_report = load_json(apply_report_path)
        applied_ids = [
            str(value) for value in apply_report.get("candidate_ids", [])
        ]
        touched_files = [
            str(entry.get("target_file", "")).replace("\\", "/")
            for entry in apply_report.get("entries", [])
            if entry.get("target_file")
        ]

    candidates_path = review_dir / "candidates.json"
    all_candidates = (
        load_json(candidates_path).get("candidates", [])
        if candidates_path.exists() else []
    )
    by_id = {
        str(candidate.get("candidate_id")): candidate
        for candidate in all_candidates
    }
    applied_candidates = [
        by_id[cid] for cid in applied_ids if cid in by_id
    ]
    applied_types = {
        common.normalise_problem_type(candidate.get("problem_type", ""))
        for candidate in applied_candidates
    } - {""}

    evidence_path = review_dir / "evidence.json"
    evidence = load_json(evidence_path) if evidence_path.exists() else {}

    scope, scope_reason = resolve_scope(
        args.scope, repo, review_dir, evidence, applied_candidates
    )
    scope_keys, scope_meta = build_scope_keys(
        scope, repo, review_dir, evidence, applied_candidates,
        samples, before, after, before_claims, claims_after,
    )
    scope_meta["requested"] = args.scope
    scope_meta["reason"] = scope_reason

    row_comparison = compare_rows(before, after, scope_keys)
    claim_comparison = compare_claims(before_claims, claims_after, scope_keys)
    full_row_comparison = compare_rows(before, after)
    full_claim_comparison = compare_claims(before_claims, claims_after)

    out_of_scope = {
        "regressed": [
            row for row in full_row_comparison["regressed"]
            if _item_key(row) not in scope_keys
        ],
        "lost_classification": [
            row for row in full_row_comparison["lost_classification"]
            if _item_key(row) not in scope_keys
        ],
        "claim_changes": [
            item
            for item in (
                full_claim_comparison["changed"] + full_claim_comparison["removed"]
            )
            if tuple(item["key"][:2]) not in scope_keys
        ],
    }
    scope_meta["out_of_scope_regressed"] = len(out_of_scope["regressed"])
    scope_meta["out_of_scope_lost_classification"] = len(
        out_of_scope["lost_classification"]
    )
    scope_meta["out_of_scope_claim_changes"] = len(out_of_scope["claim_changes"])

    attribution = attribute_rows(
        repo, review_dir, row_comparison, set(applied_ids), samples_path
    )

    baseline = None
    if args.run_baseline or scope == "full":
        baseline = run_repo_baseline(
            repo,
            review_dir,
            review_dir / "baseline_after.txt",
            touched_files,
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run.run_id,
        "applied_candidates": applied_ids,
        "applied_problem_types": sorted(applied_types),
        "scope": scope_meta,
        "row_comparison": {
            "counts": row_comparison["counts"],
            "by_problem_type": row_comparison.get("by_problem_type", {}),
            "resolved_examples": row_comparison["resolved"][:50],
            "regressed_examples": row_comparison["regressed"][:50],
            "still_wrong_examples": row_comparison["still_wrong"][:50],
        },
        "claim_comparison": {
            "counts": claim_comparison["counts"],
            "by_engine": claim_comparison["by_engine"],
            "changed_examples": claim_comparison["changed"][:50],
        },
        "out_of_scope": {
            "counts": {
                "regressed": len(out_of_scope["regressed"]),
                "lost_classification": len(out_of_scope["lost_classification"]),
                "claim_changes": len(out_of_scope["claim_changes"]),
            },
            "regressed_examples": out_of_scope["regressed"][:20],
            "lost_classification_examples": out_of_scope["lost_classification"][:20],
            "claim_change_examples": out_of_scope["claim_changes"][:20],
        },
        "candidate_attribution": attribution,
        "baseline": baseline,
    }
    recommendation = "keep"
    if row_comparison["counts"]["regressed"] or row_comparison["counts"]["lost_classification"]:
        recommendation = "rollback"
    elif out_of_scope["regressed"] or out_of_scope["lost_classification"]:
        recommendation = "rollback"
    elif applied_ids and row_comparison["counts"]["resolved"] == 0:
        recommendation = "review"
    elif any(
        row_comparison["by_problem_type"].get(problem_type, {}).get("resolved", 0) == 0
        for problem_type in applied_types
    ):
        recommendation = "review"
    elif out_of_scope["claim_changes"]:
        recommendation = "review"
    elif baseline and (baseline.get("files") or {}).get("unexpected_changes"):
        recommendation = "review"
    payload["recommendation"] = recommendation
    dump_json(review_dir / "validation_after.json", payload)
    (review_dir / "validation_report.md").write_text(
        render_report(
            row_comparison, claim_comparison, attribution, baseline,
            applied_ids, applied_types, scope_meta, out_of_scope,
        ),
        encoding="utf-8",
    )

    print(json.dumps(
        {
            "recommendation": recommendation,
            "scope": {
                "tier": scope,
                "rows": scope_meta["scope_keys"],
                "total_rows": scope_meta["total_rows"],
                "reason": scope_reason,
            },
            "rows": row_comparison["counts"],
            "claims": claim_comparison["counts"],
            "out_of_scope": payload["out_of_scope"]["counts"],
            "baseline": None if not baseline else {
                "before_snapshot": bool(baseline.get("before_snapshot")),
                "semantic": (baseline.get("counts") or {}).get("semantic", 0),
                "unexpected": (baseline.get("counts") or {}).get(
                    "unexpected", 0
                ),
                "expected": (baseline.get("counts") or {}).get("expected", 0),
                "drift_before": len(
                    (baseline.get("drift_before") or {}).get("changed") or []
                ),
                "unexpected_files": len(
                    (baseline.get("files") or {}).get("unexpected_changes")
                    or []
                ),
            },
        },
        ensure_ascii=False,
        indent=2,
    ))
    print(f"after_report.csv -> {review_dir / 'after_report.csv'}")
    print(f"validation_after.json -> {review_dir / 'validation_after.json'}")
    print(f"validation_report.md -> {review_dir / 'validation_report.md'}")
    return 1 if recommendation == "rollback" else 0


if __name__ == "__main__":
    raise SystemExit(main())
