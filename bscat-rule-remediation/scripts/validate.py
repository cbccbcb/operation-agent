#!/usr/bin/env python3
"""阶段四：校验候选、渲染报告，并导入对话审批结果。

主入口是 ``bscat.py check``；本脚本保留为后端实现：

    # 校验并生成报告
    python scripts/bscat.py check --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001

    # 兼容：导入既有 approvals.json（新流程用 bscat.py approve / apply）
    python scripts/bscat.py check --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001 --import reviews/run-001/approvals.json

本脚本不生成网页、不打开浏览器，也不会自动确认任何候选。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Windows pipes default to the active code page; keep Chinese output readable
# in Claude Code and make the same setting apply to internal subprocesses.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
INTERNAL = SCRIPT_DIR / "_internal"
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from cli import ensure_outside_repo  # noqa: E402


def run_internal(script: str, args: list[str]) -> None:
    command = [sys.executable, str(INTERNAL / script), *args]
    print("\n$ " + " ".join(command), flush=True)
    completed = subprocess.run(command)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def print_analysis_report(review_dir: Path) -> None:
    """Print a compact Chinese report after validation."""
    evidence = load_json(review_dir / "evidence.json")
    summary = evidence.get("summary") or {}
    counts = summary.get("problem_type_counts") or {}
    validation = load_json(review_dir / "validation.json")
    vsummary = validation.get("validation_summary") or {}
    validated = validation.get("candidates") or []
    # candidates.json is authoritative for review status; validation.json is
    # generated before approvals are imported and would otherwise print stale
    # pending/confirmed counts.
    candidates = load_json(review_dir / "candidates.json").get("candidates") or validated
    code = (load_json(review_dir / "code_findings.json").get("summary") or {})

    rows = summary.get("rows", "?")
    clusters = summary.get("clusters", "?")
    problem_total = sum(int(value) for value in counts.values() if str(value).isdigit())
    status_counts = {
        status: sum(1 for item in candidates if item.get("status") == status)
        for status in ("pending", "confirmed", "rejected")
    }
    high_pending = [
        item.get("candidate_id", "")
        for item in candidates
        if item.get("risk_level") == "high" and item.get("status") == "pending"
    ]
    medium_pending = [
        item.get("candidate_id", "")
        for item in candidates
        if item.get("risk_level") == "medium" and item.get("status") == "pending"
    ]

    print("\n" + "=" * 64)
    print("分析报告")
    print("=" * 64)
    print(f"样本：{rows} 行")
    print(
        "问题行：{} 行 | 漏识别 miss {} | 多识别 extra {} | 不一致 diff {}".format(
            problem_total,
            counts.get("miss", 0),
            counts.get("extra", 0),
            counts.get("diff", 0),
        )
    )
    print(f"差异聚类：{clusters} 个")
    if vsummary:
        print(
            "候选：{} 条 | 低风险 {} | 中风险 {} | 高风险 {} | 有效 {} | 无效 {}".format(
                vsummary.get("total", 0),
                vsummary.get("low_risk", 0),
                vsummary.get("medium_risk", 0),
                vsummary.get("high_risk", 0),
                vsummary.get("valid", 0),
                vsummary.get("invalid", 0),
            )
        )
        print(
            "审批状态：待审批 {} | 已通过 {} | 已拒绝 {}".format(
                status_counts["pending"],
                status_counts["confirmed"],
                status_counts["rejected"],
            )
        )
    verdicts = code.get("by_verdict") or {}
    if verdicts:
        print(
            "代码诊断（只读）：config_only {} | config_plus_code_review {} | "
            "code_change_suggested {}".format(
                verdicts.get("config_only", 0),
                verdicts.get("config_plus_code_review", 0),
                verdicts.get("code_change_suggested", 0),
            )
        )
    if medium_pending:
        print("待逐条看清的中风险候选：" + ", ".join(medium_pending))
    if high_pending:
        print("待逐条确认的高风险候选：" + ", ".join(high_pending))
    approval_groups = load_json(review_dir / "approval_groups.json")
    groups = approval_groups.get("groups") or []
    if groups:
        print("\n审批弹窗（每张配置表一次，表内高风险→中风险→低风险）：")
        for group in groups:
            counts = group.get("counts") or {}
            plan = group.get("popup_plan") or {}
            suffix = "（需续窗）" if plan.get("continuation") else ""
            print(
                f"  {group.get('order')}. {group.get('target_file')} | "
                f"高 {counts.get('high', 0)} / 中 {counts.get('medium', 0)} / "
                f"低 {counts.get('low', 0)} | 弹窗 {plan.get('popups_needed', 0)}"
                f"{suffix}"
            )
            if group.get("table_note"):
                print(f"     这张表存什么：{group['table_note']}")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="校验候选、打印分析报告并导入对话审批结果。")
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--review", required=True, help="审核目录。")
    parser.add_argument(
        "--import",
        dest="import_path",
        default=None,
        help="导入对话审批记录 approvals.json，并刷新候选状态。",
    )
    args = parser.parse_args()

    review_dir = Path(args.review).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve()
    ensure_outside_repo(repo, review_dir)

    if args.import_path:
        # 快路径：审批导入只改候选 status，规则/冲突/有效性/样本都没变，
        # 跳过重校验、重渲染与影响面对账，直接导入并打印刷新后的状态。
        run_internal(
            "approvals.py",
            ["--review", str(review_dir), "--approvals", str(Path(args.import_path).expanduser().resolve())],
        )
        print_analysis_report(review_dir)
        print("审批结果已导入。下一步：运行 bscat.py apply dry-run；确认后加 --apply 写入。")
        return 0

    validate_args = ["--repo", str(repo), "--review", str(review_dir)]
    run_internal("validate_candidates.py", validate_args)

    run_internal("render_candidates.py", ["--repo", str(repo), "--review", str(review_dir)])

    # 阶段四：影响面/命中数对账（samples+before_report 优先，缺上下文时 simulate.py 自动跳过）
    run_internal("simulate.py", ["reconcile", "--review", str(review_dir)])

    print_analysis_report(review_dir)
    print("下一步：按 approval_groups.json 每张配置表弹一次窗（表内高风险→中风险→低风险），")
    print("用 bscat.py approve --build 生成 approvals.json；然后运行 bscat.py apply dry-run。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
