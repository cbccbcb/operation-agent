#!/usr/bin/env python3
"""阶段六：导入对话审批结果、dry-run、最终确认后写入、写入后验证、回滚。

主入口是 ``bscat.py apply``；本脚本保留为后端实现：

    # 导入对话审批产出的 approvals.json，并 dry-run
    python scripts/bscat.py apply --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001 --approvals reviews/run-001/approvals.json

    # 实际写入（高风险候选还需 --allow-high-risk），随后自动验证并写修复日志
    python scripts/bscat.py apply ... --approvals ... --apply --allow-high-risk \
        --history D:/Projects/operation-agent/repair-history \
        [--verify-scope auto|target|local|full]

    # 回滚：默认 dry-run；加 --apply 才真正恢复
    python scripts/bscat.py apply --review reviews/run-001 --rollback --apply

不提交、不推送、不修改引擎源码。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

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


def run_internal(script: str, args: list[str]) -> int:
    command = [sys.executable, str(INTERNAL / script), *args]
    print("\n$ " + " ".join(command), flush=True)
    completed = subprocess.run(command)
    return completed.returncode


def run_internal_optional(script: str, args: list[str]) -> int:
    """Run an internal module whose failure must not block the write."""
    command = [sys.executable, str(INTERNAL / script), *args]
    print("\n$ " + " ".join(command), flush=True)
    return subprocess.run(command).returncode


def _confirmed_candidates(review_dir: Path, only: str | None) -> list[dict]:
    path = review_dir / "candidates.json"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    candidates = [
        candidate
        for candidate in payload.get("candidates", [])
        if candidate.get("status") == "confirmed"
    ]
    if only:
        wanted = {value.strip() for value in only.split(",") if value.strip()}
        candidates = [
            candidate
            for candidate in candidates
            if candidate.get("candidate_id") in wanted
        ]
    return candidates


def effective_verify_scope(
    args: argparse.Namespace, review_dir: Path
) -> str:
    """Honor I6's verify-scope choice when the CLI leaves it on ``auto``.

    An explicit ``--verify-scope`` on the command line always wins.  I6's
    ``current`` means "keep the current command/default resolution".
    """
    if args.verify_scope != "auto":
        return args.verify_scope
    import common

    section = common.dialog_section(review_dir, "precheck")
    chosen = str(section.get("verify_scope", "") or "").strip()
    if not chosen or chosen == "current":
        return "auto"
    if chosen not in common.PRECHECK_SCOPES:
        raise SystemExit(
            f"I6 verify_scope={chosen!r} 不合法；"
            f"允许: {sorted(common.PRECHECK_SCOPES)}"
        )
    return chosen


def baseline_snapshot_needed(
    args: argparse.Namespace,
    repo: Path,
    review_dir: Path,
    verify_scope: str,
) -> bool:
    """True when post-write verification will run the repository baseline."""
    if not args.apply or args.no_verify:
        return False
    if verify_scope == "full":
        return True
    if verify_scope in {"target", "local"}:
        return False
    try:
        from validate_after import resolve_scope

        evidence_path = review_dir / "evidence.json"
        evidence = (
            json.loads(evidence_path.read_text(encoding="utf-8"))
            if evidence_path.exists()
            else {}
        )
        scope, _ = resolve_scope(
            "auto",
            repo,
            review_dir,
            evidence,
            _confirmed_candidates(review_dir, args.only),
        )
        return scope == "full"
    except Exception as exc:  # noqa: BLE001 - never block the write on this
        print(f"警告：无法解析写后验证范围（{exc}）；跳过写前 baseline 快照。")
        return False


def ensure_high_risk_acknowledged(review_dir: Path) -> None:
    """High-risk candidates only become writable after per-item chat approval."""
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        return
    with candidates_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    missing = [
        candidate.get("candidate_id", "")
        for candidate in payload.get("candidates", [])
        if candidate.get("status") == "confirmed"
        and candidate.get("risk_level") == "high"
        and candidate.get("risk_acknowledged") is not True
    ]
    if missing:
        raise SystemExit(
            "以下高风险候选没有在对话中逐条确认，拒绝执行: "
            + ", ".join(missing)
            + "\n请重新走阶段五：逐条确认后写入 approvals.json 并导入。"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="导入审批、写入规则、验证或回滚。")
    parser.add_argument("--repo", default=None, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--review", required=True, help="审核目录。")
    parser.add_argument("--approvals", default=None, help="对话审批产出的 approvals.json。")
    parser.add_argument(
        "--match-review",
        default=None,
        help=(
            "I4 决策文件；默认 <review>/match_review_decisions.json。"
            "approve --build 用了自定义路径时，apply 必须传同一个文件。"
        ),
    )
    parser.add_argument("--apply", action="store_true", help="实际写入；默认 dry-run。")
    parser.add_argument(
        "--allow-high-risk",
        action="store_true",
        help="确认已逐条审批高风险候选；apply_rules 的强制保护。",
    )
    parser.add_argument("--only", default=None, help="只处理逗号分隔的候选 ID。")
    parser.add_argument("--no-verify", action="store_true", help="写入后跳过真实流水线验证。")
    parser.add_argument(
        "--verify-scope",
        choices=["auto", "target", "local", "full"],
        default="auto",
        help=(
            "写入后验证范围。auto 读取 task_profile.json；"
            "target 目标案例；local 局部冲突；full 完整链路+baseline。默认 auto。"
        ),
    )
    parser.add_argument("--rollback", action="store_true", help="回滚上一次 apply。")
    parser.add_argument("--force", action="store_true", help="回滚时强制覆盖已变化的文件。")
    parser.add_argument(
        "--history",
        default=None,
        help="可选：持久化修复历史目录（仓库外）；缺省只在审核目录写 repair_log.md/jsonl。",
    )
    args = parser.parse_args()

    review_dir = Path(args.review).expanduser().resolve()

    if args.rollback:
        rollback_args = ["--review", str(review_dir)]
        if args.apply:
            rollback_args.append("--apply")
        if args.force:
            rollback_args.append("--force")
        return run_internal("rollback.py", rollback_args)

    if not args.repo:
        raise SystemExit("写入或 dry-run 时必须提供 --repo。")
    repo = Path(args.repo).expanduser().resolve()
    ensure_outside_repo(repo, review_dir)
    if args.history:
        ensure_outside_repo(repo, Path(args.history).expanduser().resolve(), "历史日志目录")

    verify_scope = effective_verify_scope(args, review_dir)
    if verify_scope != args.verify_scope:
        print(
            f"I6 决策：写后验证范围使用 {verify_scope}"
            f"（命令行 --verify-scope={args.verify_scope}）"
        )

    if args.approvals:
        approval_args = [
            "--review", str(review_dir),
            "--approvals", str(Path(args.approvals).expanduser().resolve()),
        ]
        if args.match_review:
            approval_args += [
                "--match-review",
                str(Path(args.match_review).expanduser().resolve()),
            ]
        code = run_internal("approvals.py", approval_args)
        if code:
            return code

    ensure_high_risk_acknowledged(review_dir)

    if baseline_snapshot_needed(args, repo, review_dir, verify_scope):
        print(
            "\n写前 baseline 快照（用于写后差集，避免把既有噪声当回归）...",
            flush=True,
        )
        snapshot_code = run_internal_optional(
            "baseline_guard.py",
            [
                "snapshot", "--repo", str(repo),
                "--review", str(review_dir), "--tag", "before",
            ],
        )
        if snapshot_code:
            print(
                "警告：写前 baseline 快照失败；写后验证将退化为单次 diff，"
                "无法区分既有噪声。"
            )

    apply_args = ["--repo", str(repo), "--review", str(review_dir)]
    if args.apply:
        apply_args.append("--apply")
    if args.allow_high_risk:
        apply_args.append("--allow-high-risk")
    if args.only:
        apply_args += ["--only", args.only]
    code = run_internal("apply_rules.py", apply_args)
    if code:
        return code

    if not args.apply:
        # 阶段六：confirmed 集实测预览（写入授权前；写入后的真实效果由 validate_after 回归负责）
        run_internal("simulate.py", ["preview", "--review", str(review_dir)])
        print("\n以上为 dry-run，未写入任何文件。确认后加 --apply。")
        return 0

    if not args.no_verify:
        code = run_internal(
            "validate_after.py",
            [
                "--repo", str(repo), "--review", str(review_dir),
                "--scope", verify_scope,
            ],
        )
        if code:
            print("\n写入后验证失败。请查看 validation_after.json；如需撤销，运行：")
            print(f"  python scripts/bscat.py apply --review {review_dir} --rollback --apply")
            print(
                "不进入 I8 写后下一步：先按失败/回滚路径处理；不要向用户询问接受或继续。"
            )
            return code

    log_args = ["--repo", str(repo), "--review", str(review_dir)]
    if args.history:
        log_args += ["--history", str(Path(args.history).expanduser().resolve())]
    run_internal("repair_log.py", log_args)

    print("\n写入完成。仓库未 commit / push；请自行检查 diff 后决定是否提交。")
    if not args.no_verify:
        print(
            "\nI8 写后下一步：先展示 validation_after.json 的关键结果，"
            "再用 AskUserQuestion 确认下一步（accept / rollback / "
            "full_then_decide / export_report）。"
        )
        print(
            "确认后写入 dialog_decisions.json#outcome："
            '{"decision": "...", "remaining": "continue|end|export_pending", '
            '"decided_at": "<ISO 时间>"}'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
