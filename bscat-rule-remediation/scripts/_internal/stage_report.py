#!/usr/bin/env python3
"""阶段七：把本次审核目录里的产物汇总成 ``stage_report.md``。

由 ``bscat.py apply ... --report`` 调用，也可以单独运行：

    python scripts/_internal/stage_report.py --review reviews/run-001

只读审核目录，绝不修改目标仓库。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def write_report(review_dir: Path) -> Path:
    evidence = load_json(review_dir / "evidence.json")
    summary = evidence.get("summary") or {}
    candidates = load_json(review_dir / "candidates.json").get("candidates") or []
    validation = load_json(review_dir / "validation.json")
    validation_summary = validation.get("validation_summary") or {}
    apply_report = load_json(review_dir / "apply_report.json")
    after = load_json(review_dir / "validation_after.json")
    sync = load_json(review_dir / "git_sync.json")

    problem_counts = summary.get("problem_type_counts") or {}
    status = Counter(str(item.get("status", "pending")) for item in candidates)
    risk = Counter(str(item.get("risk_level", "high")) for item in candidates)
    operation = Counter(str(item.get("operation", "")) for item in candidates)
    invalid = [
        item.get("candidate_id", "")
        for item in candidates
        if (item.get("validation") or {}).get("valid") is not True
    ]
    pending_high = [
        item.get("candidate_id", "")
        for item in candidates
        if item.get("risk_level") == "high" and item.get("status") == "pending"
    ]

    lines = [
        "# 阶段报告",
        "",
        f"- 审核目录: `{review_dir}`",
        "",
        "## 阶段零：同步",
        "",
    ]
    if sync:
        lines += [
            f"- 分支: `{sync.get('branch', '')}` -> `{sync.get('upstream', '')}`",
            f"- ahead/behind: {sync.get('ahead', '?')}/{sync.get('behind', '?')}",
            f"- HEAD: `{str(sync.get('head', ''))[:12]}`",
        ]
    else:
        lines.append("- 没有 git_sync.json（尚未运行阶段零或已跳过）。")

    lines += [
        "",
        "## 阶段一：样本与证据",
        "",
        f"- 样本行: {summary.get('rows', '?')}",
        f"- 差异聚类: {summary.get('clusters', '?')}",
        "- 问题类型: " + json.dumps(problem_counts, ensure_ascii=False),
        "",
        "## 阶段三/四：候选",
        "",
        f"- 候选总数: {len(candidates)}",
        "- 操作: " + json.dumps(dict(operation.most_common()), ensure_ascii=False),
        "- 风险: " + json.dumps(dict(risk.most_common()), ensure_ascii=False),
        "- 状态: " + json.dumps(dict(status.most_common()), ensure_ascii=False),
    ]
    if validation_summary:
        lines.append(
            "- 校验: 有效 {} | 无效 {}".format(
                validation_summary.get("valid", 0),
                validation_summary.get("invalid", 0),
            )
        )
    if invalid:
        lines.append("- 未通过校验的候选: " + ", ".join(invalid))
    if pending_high:
        lines.append("- 待逐条确认的高风险候选: " + ", ".join(pending_high))

    lines += ["", "## 阶段六：写入与写后验证", ""]
    if apply_report:
        lines += [
            f"- apply 模式: {apply_report.get('mode', '?')}",
            f"- 写入文件数: {len(apply_report.get('files') or [])}",
            f"- 候选: {', '.join(apply_report.get('candidate_ids') or []) or '-'}",
        ]
        if after:
            lines.append(
                "- 写后回归: {}".format(
                    "通过" if after.get("ok") is True else "失败/需检查"
                )
            )
    else:
        lines.append("- 尚未实际写入（没有 apply_report.json）。")

    lines += [
        "",
        "## 下一步",
        "",
    ]
    if not candidates:
        lines.append("- 运行 `bscat.py propose` 生成候选。")
    elif invalid:
        lines.append("- 先修正未通过校验的候选，再运行 `bscat.py check`。")
    elif status.get("pending"):
        lines.append("- 按 `approval_groups.json` 每张配置表弹一次窗完成审批，然后运行 `bscat.py approve --build`。")
    elif not apply_report:
        lines.append("- 运行 `bscat.py apply` dry-run，确认后加 `--apply` 写入。")
    else:
        lines.append(
            "- 检查 `validation_after.json`，然后按需 "
            "`bscat.py apply --rollback --apply`。"
        )

    path = review_dir / "stage_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成阶段报告 stage_report.md。")
    parser.add_argument("--review", required=True, help="审核目录（必须在目标仓库外）。")
    args = parser.parse_args()
    review_dir = Path(args.review).expanduser().resolve()
    path = write_report(review_dir)
    print(f"stage_report.md -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
