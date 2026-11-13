#!/usr/bin/env python3
"""生成并追加一次成功写入的“修复历史日志”。

只记录“这次实际改了什么”，不复制候选、校验、审批的完整评审内容。
运行时仍会在 review 目录保留中间文件；是否长期保留由使用人决定。

    python repair_log.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001 --history D:/Projects/repair-history

写入：
  <review>/repair_log.md
  <review>/repair_log.jsonl
  <history>/repair_log.jsonl          （追加，按 run_id 去重）
  <history>/runs/<run_id>.md          （每次一份，便于归档）
  <history>/runs/<run_id>/apply_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Windows pipes default to the active code page; keep Chinese output readable.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure_outside_repo(repo: Path, target: Path) -> None:
    try:
        target.resolve().relative_to(repo.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"历史日志目录不能位于目标仓库内: {target}\n"
        "请改到仓库之外，避免日志污染 git diff。"
    )


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned or "run"


def candidate_match(candidate: dict[str, Any]) -> str:
    row = candidate.get("row") or {}
    for key in ("pattern", "keywords", "keyword", "merchant_name"):
        value = row.get(key)
        if value not in (None, ""):
            return f"{key}={value}"
    match_key = candidate.get("match_key")
    if match_key:
        return json.dumps(match_key, ensure_ascii=False)
    return ""


def markdown_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "").replace("|", "\\|")


def build_record(repo: Path, review_dir: Path) -> dict[str, Any]:
    candidates_payload = load_json(review_dir / "candidates.json")
    apply_report = load_json(review_dir / "apply_report.json")
    validation_after = load_json(review_dir / "validation_after.json")
    parse_summary = load_json(review_dir / "parse_summary.json")
    target_scope = load_json(review_dir / "target_scope.json")
    git_sync = load_json(review_dir / "git_sync.json")

    if not apply_report:
        raise SystemExit(f"找不到 {review_dir / 'apply_report.json'}；只有实际写入后才生成修复日志。")

    candidates = candidates_payload.get("candidates") or []
    by_id = {str(candidate.get("candidate_id", "")): candidate for candidate in candidates}
    applied_ids = [str(value) for value in (apply_report.get("candidate_ids") or [])]

    applied = []
    for candidate_id in applied_ids:
        candidate = by_id.get(candidate_id, {})
        applied.append(
            {
                "candidate_id": candidate_id,
                "problem_type": candidate.get("problem_type", ""),
                "operation": candidate.get("operation", ""),
                "engine": candidate.get("engine", ""),
                "target_file": candidate.get("target_file", ""),
                "category": candidate.get("category", ""),
                "match": candidate_match(candidate),
                "reason": candidate.get("rationale") or candidate.get("reason", ""),
            }
        )

    changes = []
    for entry in apply_report.get("entries") or []:
        changes.append(
            {
                "target_file": entry.get("target_file", ""),
                "rows_before": entry.get("rows_before"),
                "rows_after": entry.get("rows_after"),
                "candidates": entry.get("candidates") or [],
                "backup": entry.get("backup", ""),
                "sha256_before": entry.get("sha256_before", ""),
                "sha256_after": entry.get("sha256_after", ""),
            }
        )

    row_comparison = validation_after.get("row_comparison") or {}
    record = {
        "run_id": candidates_payload.get("run_id") or apply_report.get("run_id") or "",
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "branch": git_sync.get("branch", ""),
        "head": git_sync.get("head", ""),
        "upstream": git_sync.get("upstream", ""),
        "input": parse_summary.get("input", ""),
        "input_sha256": parse_summary.get("input_sha256", ""),
        "problem_scope": parse_summary.get("problem_scope", ""),
        "selected_groups": target_scope.get("selected_groups", []),
        "applied_candidates": applied,
        "changes": changes,
        "validation": {
            "recommendation": validation_after.get("recommendation", ""),
            "counts": row_comparison.get("counts", {}),
            "by_problem_type": row_comparison.get("by_problem_type", {}),
        },
        "review_dir": str(review_dir),
        "backup_root": str(review_dir / "backups"),
        "apply_report": str(review_dir / "apply_report.json"),
        "committed": False,
    }
    return record


def render_markdown(record: dict[str, Any]) -> str:
    lines = [
        "# BSCAT 修复历史记录",
        "",
        f"- run_id: `{record.get('run_id', '')}`",
        f"- 时间: {record.get('logged_at', '')}",
        f"- 仓库: `{record.get('repo', '')}`",
        f"- 分支: `{record.get('branch', '')}`",
        f"- HEAD: `{record.get('head', '')}`",
        f"- 输入样本: `{record.get('input', '')}`",
        f"- 输入 SHA-256: `{record.get('input_sha256', '')}`",
        f"- 问题范围: `{record.get('problem_scope', '')}`",
        f"- 应用候选数: {len(record.get('applied_candidates') or [])}",
        f"- 变更文件数: {len(record.get('changes') or [])}",
        f"- 写入后建议: `{record.get('validation', {}).get('recommendation', '')}`",
        "- 提交状态: 未提交（由使用人检查 diff 后自行提交）",
        "",
        "## 应用的候选",
        "",
        "| ID | 问题类型 | 操作 | 引擎 | 目标文件 | 分类 | 模式/匹配键 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in record.get("applied_candidates") or []:
        lines.append(
            "| {candidate_id} | {problem_type} | {operation} | {engine} | "
            "{target_file} | {category} | `{match}` |".format(
                candidate_id=markdown_cell(item.get("candidate_id")),
                problem_type=markdown_cell(item.get("problem_type")),
                operation=markdown_cell(item.get("operation")),
                engine=markdown_cell(item.get("engine")),
                target_file=markdown_cell(item.get("target_file")),
                category=markdown_cell(item.get("category")) or "（未分类）",
                match=markdown_cell(item.get("match")),
            )
        )

    lines += [
        "",
        "## 变更文件",
        "",
        "| 文件 | 行数变化 | 候选 | 备份 |",
        "|---|---|---|---|",
    ]
    for item in record.get("changes") or []:
        before = item.get("rows_before")
        after = item.get("rows_after")
        rows = f"{before} → {after}" if before is not None or after is not None else ""
        lines.append(
            "| {target_file} | {rows} | {candidates} | `{backup}` |".format(
                target_file=markdown_cell(item.get("target_file")),
                rows=markdown_cell(rows),
                candidates=markdown_cell(", ".join(item.get("candidates") or [])),
                backup=markdown_cell(item.get("backup")),
            )
        )

    counts = record.get("validation", {}).get("counts", {})
    lines += [
        "",
        "## 写入后验证",
        "",
        f"- resolved: {counts.get('resolved', '')}",
        f"- regressed: {counts.get('regressed', '')}",
        f"- lost_classification: {counts.get('lost_classification', '')}",
        f"- newly_classified: {counts.get('newly_classified', '')}",
        f"- recommendation: `{record.get('validation', {}).get('recommendation', '')}`",
        "",
        "## 回滚",
        "",
        "```bash",
        f'python scripts/bscat.py apply --review "{record.get("review_dir", "")}" --rollback',
        f'python scripts/bscat.py apply --review "{record.get("review_dir", "")}" --rollback --apply',
        "```",
        "",
        "回滚依赖 `apply_report.json` 和 `backups/`；如果清理审核目录，请一并保留这两项。",
        "",
    ]
    return "\n".join(lines)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_id = str(record.get("run_id", ""))
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    existing = json.loads(line)
                except ValueError:
                    continue
                if run_id and str(existing.get("run_id", "")) == run_id:
                    return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成修复历史日志。")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument(
        "--history",
        default=None,
        help="可选：持久化历史目录；缺省只在审核目录写 repair_log.md/jsonl。",
    )
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    record = build_record(repo, review_dir)

    markdown = render_markdown(record)
    (review_dir / "repair_log.md").write_text(markdown, encoding="utf-8")
    append_jsonl(review_dir / "repair_log.jsonl", record)

    history_paths = []
    if args.history:
        history = Path(args.history).expanduser().resolve()
        ensure_outside_repo(repo, history)
        run_dir = history / "runs" / safe_name(str(record.get("run_id", "")))
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "repair_log.md").write_text(markdown, encoding="utf-8")
        apply_report = review_dir / "apply_report.json"
        if apply_report.exists():
            shutil.copy2(apply_report, run_dir / "apply_report.json")
        append_jsonl(history / "repair_log.jsonl", record)
        history_paths = [
            str(history / "repair_log.jsonl"),
            str(run_dir / "repair_log.md"),
            str(run_dir / "apply_report.json"),
        ]

    print(f"修复历史日志 -> {review_dir / 'repair_log.md'}")
    for path in history_paths:
        print(f"历史归档       -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
