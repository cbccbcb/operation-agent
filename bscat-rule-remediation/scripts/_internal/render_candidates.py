#!/usr/bin/env python3
"""Render ``candidates.json`` into human-review CSVs and a Markdown report.

The JSON file is the single source of truth.  Writing candidate rows straight
into rule CSVs is forbidden: CSV quoting (regex quantifiers contain commas) and
column-order mistakes are the two most common ways a rule change corrupts a
table.  This script owns all rendering; ``apply_rules.py`` owns writing.

    python render_candidates.py --repo D:\\Projects\\AUS_BS_CAT_Model --review reviews/run-001
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

# merchant_kb.csv contains very long pipe-separated keyword fields.  Windows
# C long is 32-bit, so sys.maxsize overflows; clamp to the safe 32-bit maximum.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# These names must not collide with any rule-table header; the per-engine CSVs
# use ``[rule columns..., metadata columns...]`` as one field list.  ``category``
# and ``pattern`` are therefore deliberately absent here (they already exist in
# most rule tables); ``candidates_all.csv`` carries them separately.
METADATA_COLUMNS = [
    "candidate_id", "problem_type", "status", "risk_level", "operation",
    "target_file", "reason", "expected_resolved", "expected_conflicts",
    "match_key", "change", "needs_review",
]
RULE_OPERATIONS = {"append", "edit", "delete", "keyword_update"}
CONFIG_OPERATIONS = {"catalog", "priority"}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return next(reader)


def find_rows(path: Path, match_key: dict[str, Any]) -> list[dict[str, str]]:
    """Return matching rows, using a preloaded index when available.

    merchant_kb.csv has ~874k rows; materialising it just to resolve one
    match_key is both slow and memory-hungry.  ``preload_exact_rows`` resolves
    all known initial match_keys in one scan before rendering starts.
    """
    key = _row_cache_key(path, match_key)
    if key in _ROW_CACHE:
        return _ROW_CACHE[key]
    hits: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if matches(row, match_key):
                hits.append(row)
                if len(hits) > 1:
                    break
    return hits


def matches(row: dict[str, str], match_key: dict[str, Any]) -> bool:
    return all(str(row.get(key, "")) == str(value) for key, value in match_key.items())


_ROW_CACHE: dict[
    tuple[str, tuple[str, ...], tuple[str, ...]],
    list[dict[str, str]],
] = {}


def _row_cache_key(
    path: Path, match_key: dict[str, Any]
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    columns = tuple(sorted(str(key) for key in match_key))
    values = tuple(str(match_key[column]) for column in columns)
    return str(path), columns, values


def preload_exact_rows(path: Path, match_keys: list[dict[str, Any]]) -> None:
    """Index requested match_keys in one pass (at most two hits per key)."""
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

    found: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for column_group, wanted in groups.items():
        for values in wanted:
            found[(column_group, values)] = []
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
        for row in reader:
            for column_group, wanted, positions in plans:
                values = tuple(
                    row[position] if position < len(row) else ""
                    for position in positions
                )
                if values not in wanted:
                    continue
                hits = found[(column_group, values)]
                if len(hits) < 2:
                    hits.append(
                        {
                            name: row[position] if position < len(row) else ""
                            for position, name in enumerate(header)
                        }
                    )
    for (column_group, values), hits in found.items():
        _ROW_CACHE[(str(path), column_group, values)] = hits


def safe_target(repo: Path, target_file: str) -> Path:
    path = (repo / target_file).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError as exc:
        raise SystemExit(f"target_file 指向仓库之外的路径: {target_file}") from exc
    if not path.exists():
        raise SystemExit(f"target_file 不存在: {path}")
    return path


def build_review_row(
    repo: Path,
    candidate: dict[str, Any],
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Render one candidate as a rule-table row plus a ``change`` summary."""
    target = safe_target(repo, candidate["target_file"])
    header = load_header(target)
    operation = candidate["operation"]
    provided = dict(candidate.get("row") or {})
    change: dict[str, Any] = {}

    if operation == "append":
        row = {column: provided.get(column, "") for column in header}
    elif operation in {"edit", "delete", "keyword_update"}:
        match_key = candidate.get("match_key") or {}
        if not match_key:
            raise SystemExit(
                f"{candidate['candidate_id']}: {operation} 需要 match_key"
            )
        hits = find_rows(target, match_key)
        if len(hits) != 1:
            raise SystemExit(
                f"{candidate['candidate_id']}: match_key 在 {target} 中匹配到 {len(hits)} 行"
                "（必须恰好 1 行）"
            )
        row = {column: hits[0].get(column, "") for column in header}
        if operation == "edit":
            unknown = sorted(set(provided) - set(header))
            if unknown:
                raise SystemExit(
                    f"{candidate['candidate_id']}: 以下列不存在于 {target.name}: {unknown}"
                )
            row.update({key: value for key, value in provided.items()})
        elif operation == "keyword_update":
            try:
                final_keywords, change = common.merge_initial_keywords(
                    row.get("keywords", ""), provided
                )
            except ValueError as exc:
                raise SystemExit(f"{candidate['candidate_id']}: {exc}") from exc
            row["keywords"] = final_keywords
    else:
        raise SystemExit(f"{candidate['candidate_id']}: 不是规则操作")

    return header, row, change


def pattern_of(candidate: dict[str, Any]) -> str:
    row = candidate.get("row") or {}
    for key in ("pattern", "keywords", "keyword"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def write_csv(path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def render(repo: Path, review_dir: Path) -> dict[str, Any]:
    payload = load_json(review_dir / "candidates.json")
    candidates = payload.get("candidates", [])
    if not candidates:
        raise SystemExit("candidates.json 中没有候选")

    # duplicate ids are a hard error — the approval and apply phases key on them
    ids = [candidate["candidate_id"] for candidate in candidates]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise SystemExit(f"重复的 candidate_id: {duplicates}")

    by_engine: dict[str, list[dict[str, Any]]] = {}
    unified: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []

    initial_match_keys = [
        candidate.get("match_key") or {}
        for candidate in candidates
        if candidate.get("operation") in {"edit", "delete", "keyword_update"}
        and str(candidate.get("target_file", "")) == "initial_engine/merchant_kb.csv"
    ]
    if initial_match_keys:
        preload_exact_rows(
            repo / "initial_engine" / "merchant_kb.csv", initial_match_keys
        )

    for candidate in candidates:
        operation = candidate.get("operation", "append")
        if operation in RULE_OPERATIONS:
            header, row, change = build_review_row(repo, candidate)
            meta = {
                "candidate_id": candidate["candidate_id"],
                "problem_type": candidate.get("problem_type", ""),
                "status": candidate.get("status", "pending"),
                "risk_level": candidate.get("risk_level", "high"),
                "operation": operation,
                "target_file": candidate["target_file"],
                "reason": candidate.get("reason", ""),
                "expected_resolved": (candidate.get("expected_effect") or {}).get("resolved_rows", ""),
                "expected_conflicts": (candidate.get("expected_effect") or {}).get("conflicts", ""),
                "match_key": json.dumps(
                    candidate.get("match_key") or {}, ensure_ascii=False, sort_keys=True
                ),
                "change": json.dumps(change, ensure_ascii=False, sort_keys=True),
                "needs_review": "true" if candidate.get("needs_review") else "false",
            }
            rendered = {**row, **meta}
            by_engine.setdefault(candidate["engine"], []).append(rendered)
            unified.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "problem_type": candidate.get("problem_type", ""),
                    "engine": candidate["engine"],
                    "operation": operation,
                    "target_file": candidate["target_file"],
                    "risk_level": candidate.get("risk_level", "high"),
                    "status": candidate.get("status", "pending"),
                    "category": candidate.get("category", ""),
                    "pattern": pattern_of(candidate),
                    "reason": candidate.get("reason", ""),
                    "expected_resolved": meta["expected_resolved"],
                    "expected_conflicts": meta["expected_conflicts"],
                    "match_key": meta["match_key"],
                    "change": meta["change"],
                    "needs_review": meta["needs_review"],
                    "evidence_cluster_ids": ",".join(
                        str(value)
                        for value in candidate.get("evidence_cluster_ids") or []
                    ),
                }
            )
        elif operation in CONFIG_OPERATIONS or operation == "suggestion":
            suggestions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "engine": candidate.get("engine", ""),
                    "operation": operation,
                    "target_file": candidate.get("target_file", ""),
                    "risk_level": candidate.get("risk_level", "high"),
                    "status": candidate.get("status", "pending"),
                    "category": candidate.get("category", ""),
                    "reason": candidate.get("reason", ""),
                    "proposed_change": json.dumps(
                        candidate.get("row") or candidate.get("proposed_change") or {},
                        ensure_ascii=False,
                    ),
                }
            )
        else:
            raise SystemExit(
                f"{candidate['candidate_id']}: 未知操作 {operation!r}"
            )

    candidate_dir = review_dir / "candidates"
    for engine, rows in by_engine.items():
        # A candidate set may target more than one file in the same engine
        # (liability has eight).  Split by target_file when that happens.
        for target_file in sorted({row["target_file"] for row in rows}):
            subset = [row for row in rows if row["target_file"] == target_file]
            file_header = load_header(safe_target(repo, target_file))
            write_csv(
                candidate_dir / f"{engine}__{Path(target_file).stem}_candidates.csv",
                [*file_header, *METADATA_COLUMNS],
                subset,
            )

    unified_header = [
        "candidate_id", "problem_type", "engine", "operation", "target_file",
        "risk_level", "status", "category", "pattern", "reason",
        "expected_resolved", "expected_conflicts", "match_key", "change",
        "needs_review", "evidence_cluster_ids",
    ]
    write_csv(review_dir / "candidates_all.csv", unified_header, unified)
    write_csv(
        review_dir / "config_suggestions.csv",
        ["candidate_id", "engine", "operation", "target_file", "risk_level",
         "status", "category", "reason", "proposed_change"],
        suggestions,
    )

    markdown = render_markdown(payload, unified, suggestions)
    (review_dir / "candidate_report.md").write_text(markdown, encoding="utf-8")

    return {
        "candidates": len(candidates),
        "rule_candidates": len(unified),
        "suggestions": len(suggestions),
        "candidate_dir": str(candidate_dir),
        "report": str(review_dir / "candidate_report.md"),
    }


def render_markdown(
    payload: dict[str, Any],
    unified: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
) -> str:
    lines = [
        "# 候选规则评审报告",
        "",
        f"- run_id: `{payload.get('run_id', '')}`",
        f"- 候选总数: {len(unified) + len(suggestions)}",
        f"- 规则候选: {len(unified)}",
        f"- 配置/工程建议: {len(suggestions)}",
        "",
        "## 规则候选",
        "",
        "| ID | 问题类型 | 引擎 | 操作 | 类别 | 模式 | 风险 | 预期修复 | 预期冲突 | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in unified:
        pattern = str(row["pattern"]).replace("|", "\\|")[:60]
        lines.append(
            f"| {row['candidate_id']} | {row.get('problem_type', '')} | "
            f"{row['engine']} | {row['operation']} | "
            f"{row['category'] or '（未分类）'} | `{pattern}` | {row['risk_level']} | "
            f"{row['expected_resolved']} | {row['expected_conflicts']} | {row['status']} |"
        )
    if suggestions:
        lines += ["", "## 配置/工程建议（不直接写规则表）", "",
                  "| ID | 引擎 | 操作 | 类别 | 原因 |", "|---|---|---|---|---|"]
        for row in suggestions:
            lines.append(
                f"| {row['candidate_id']} | {row['engine']} | {row['operation']} | "
                f"{row['category']} | {row['reason']} |"
            )
    lines += [
        "",
        "## 审批规则",
        "",
        "- 弹窗按 `target_file`（配置表）分组：每张表一个弹窗，表内先高风险、再中风险、最后低风险。",
        "- `low`：可在该配置表的弹窗里整组通过；仍需用户明确同意。",
        "- `medium`：必须逐条选择（编辑/删除既有规则、样本命中面较宽、零增益或接管不明），`risk_acknowledged` 保持 `false`。",
        "- `high`：必须逐条确认并记录风险确认（income/liability/transfer、优先级、catalog、冲突、误伤已正确行或删除后仍被命中），`risk_acknowledged=true`。",
        "- 每条规则候选的 `source_trace` 必须指向证据命中的配置表；缺失或不一致不得审批。",
        "- 审批结果写入 `approvals.json`；只有 `status=confirmed` 且校验通过的候选才会被 `bscat.py apply` 写入。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染候选规则评审产物。")
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--review", required=True, help="包含 candidates.json 的审核目录。")
    args = parser.parse_args()

    summary = render(Path(args.repo).resolve(), Path(args.review).resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
