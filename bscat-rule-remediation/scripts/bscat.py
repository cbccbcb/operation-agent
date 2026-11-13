#!/usr/bin/env python3
"""BSCAT 规则修复唯一入口。

所有阶段都通过这个脚本调用；prepare.py / validate.py / apply.py 保留为
阶段编排后端。单步重跑（``--only``）直接调用 ``_internal/`` 里的阶段模块，
不再经过额外的 dispatcher。

    python scripts/bscat.py route   --repo <repo> [--input <file> | --category <c> ...]
    # route 后按 I1 弹窗结果写 <review>/intake.json
    python scripts/bscat.py prepare --repo <repo> --input <file> --review <dir>
    # prepare 写 parse_review.json / attribution_review.json；
    # 触发时按 I2/I3 弹窗结果写 <review>/dialog_decisions.json
    python scripts/bscat.py propose --repo <repo> --review <dir>
    # 改候选前先 --dry-run 看 revise_preview.json；触发 I5 时先写 #revise 决策
    python scripts/bscat.py revise  --repo <repo> --review <dir> --patch <patch.json> --dry-run
    # check --only validate 生成 <review>/match_review.json（I4）
    python scripts/bscat.py check   --repo <repo> --review <dir>
    # 按 match_review.json 弹窗后写 match_review_decisions.json
    # check --only reconcile 生成 <review>/precheck.json；触发 I6 时先写 #precheck 决策
    python scripts/bscat.py approve --review <dir> --build --confirmed ...
    python scripts/bscat.py apply   --repo <repo> --review <dir> [--apply]
    # 写后验证成功、按 I8 弹窗后把结果写入 #outcome
    python scripts/bscat.py search  --repo <repo> --query <text> [--fuzzy]

单步重跑：

    python scripts/bscat.py prepare --only sync|parse|evidence|diagnose ...
    python scripts/bscat.py check   --only validate|render|reconcile|preview ...

`apply --report` 会在 dry-run 或写入结束后额外生成 stage_report.md。
未识别的参数原样转发给后端脚本，因此各阶段的 --help 仍然可用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
INTERNAL = SCRIPT_DIR / "_internal"
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from cli import ensure_outside_repo  # noqa: E402

COMMANDS = {
    "route": "入口（I1）：判断目标引擎/表、匹配语义、覆盖风险与验证档位",
    "prepare": "阶段零/一：读取 intake、同步检查、解析、取证、按需诊断（I2/I3 蓝图）",
    "propose": "阶段三：从证据生成候选草稿（读取 intake 类型/方向）",
    "revise": "阶段三：把修订合并回 candidates.json（I5 预览/确认，优先 --patch）",
    "check": "阶段四：校验、生成 match_review（I4）、渲染审阅 CSV、对账（I6 蓝图）",
    "approve": "阶段五：按配置表对话审批；校验 I1–I6 门禁并生成 approvals.json",
    "apply": "阶段六/七：dry-run、写入、写后验证（I8）、回滚、可选报告",
    "search": "辅助：检索 merchant_kb 或任意规则表",
    "catalog": "辅助：列出每张规则表的作用、列与匹配方式",
}

PREPARE_ONLY = {"sync", "parse", "evidence", "diagnose"}
CHECK_ONLY = {"validate", "render", "reconcile", "preview"}


def print_help() -> None:
    print("BSCAT 规则修复唯一入口\n")
    print("用法: python scripts/bscat.py <command> [args...]\n")
    print("命令:")
    for name, description in COMMANDS.items():
        print(f"  {name:<9} {description}")
    print("\n示例:")
    print("  python scripts/bscat.py route --repo D:/Projects/AUS_BS_CAT_Model \\")
    print("      --input sample.csv --problem-type all")
    print("  python scripts/bscat.py route --repo D:/Projects/AUS_BS_CAT_Model \\")
    print("      --category Health --engine initial --problem-type miss")
    print("  python scripts/bscat.py prepare --repo D:/Projects/AUS_BS_CAT_Model \\")
    print("      --input sample.csv --review reviews/run-001")
    print("  # I1 后 prepare/propose 自动读 reviews/run-001/intake.json")
    print("  # prepare 触发 I2/I3 时，先写 reviews/run-001/dialog_decisions.json")
    print("  python scripts/bscat.py check --repo D:/Projects/AUS_BS_CAT_Model \\")
    print("      --review reviews/run-001")
    print("  # validate 后完成 I4；reconcile 生成 precheck.json，触发 I6 时先确认")
    print("  python scripts/bscat.py check --only reconcile \\")
    print("      --review reviews/run-001")
    print("  python scripts/bscat.py catalog --repo D:/Projects/AUS_BS_CAT_Model \\")
    print("      --engine liability")
    print("  python scripts/bscat.py apply --repo D:/Projects/AUS_BS_CAT_Model \\")
    print("      --review reviews/run-001 --approvals reviews/run-001/approvals.json")
    print("\n单步重跑: prepare --only sync|parse|evidence|diagnose")
    print("          check   --only validate|render|reconcile|preview")
    print("\n各命令的详细参数: python scripts/bscat.py <command> --help")


def run(script: Path, args: list[str]) -> int:
    command = [sys.executable, str(script), *args]
    print("\n$ " + " ".join(command), flush=True)
    return subprocess.run(command).returncode


def extract_option(args: list[str], name: str) -> tuple[str | None, list[str]]:
    """Remove ``--name value`` / ``--name=value`` from args."""
    remaining: list[str] = []
    value: str | None = None
    index = 0
    while index < len(args):
        item = args[index]
        if item == name:
            if index + 1 >= len(args):
                raise SystemExit(f"{name} 需要提供值")
            value = args[index + 1]
            index += 2
            continue
        if item.startswith(name + "="):
            value = item.split("=", 1)[1]
            index += 1
            continue
        remaining.append(item)
        index += 1
    return value, remaining


def has_flag(args: list[str], name: str) -> tuple[bool, list[str]]:
    return name in args, [item for item in args if item != name]


def has_option(args: list[str], name: str) -> bool:
    return name in args or any(item.startswith(name + "=") for item in args)


def stage_command(
    stage: str,
    repo: Path | None,
    review_dir: Path,
    extras: list[str],
) -> list[str]:
    """Map one ``--only`` stage to its internal script."""
    if stage == "sync":
        return [
            sys.executable, str(INTERNAL / "sync_status.py"),
            "--repo", str(repo), "--review", str(review_dir), *extras,
        ]
    if stage == "parse":
        args = list(extras)
        if not has_option(args, "--out"):
            args = ["--out", str(review_dir), *args]
        return [sys.executable, str(INTERNAL / "parse_input.py"), *args]
    if stage in {"evidence", "diagnose"}:
        script = "collect_evidence.py" if stage == "evidence" else "diagnose_code.py"
        return [
            sys.executable, str(INTERNAL / script),
            "--repo", str(repo), "--review", str(review_dir), *extras,
        ]
    if stage == "validate":
        return [
            sys.executable, str(INTERNAL / "validate_candidates.py"),
            "--repo", str(repo), "--review", str(review_dir), *extras,
        ]
    if stage == "render":
        return [
            sys.executable, str(INTERNAL / "render_candidates.py"),
            "--repo", str(repo), "--review", str(review_dir), *extras,
        ]
    if stage in {"reconcile", "preview"}:
        return [
            sys.executable, str(INTERNAL / "simulate.py"),
            stage, "--review", str(review_dir), *extras,
        ]
    raise AssertionError(f"未实现的阶段: {stage}")


def run_stage(stage: str, args: list[str]) -> int:
    """Run one internal stage after extracting ``--repo`` / ``--review``."""
    repo_value, args = extract_option(args, "--repo")
    review_value, args = extract_option(args, "--review")
    if not review_value:
        raise SystemExit(f"阶段 {stage!r} 需要 --review。")

    review_dir = Path(review_value).expanduser().resolve()
    repo = Path(repo_value).expanduser().resolve() if repo_value else None
    requires_repo = {"sync", "evidence", "diagnose", "validate", "render"}
    if stage in requires_repo and repo is None:
        raise SystemExit(f"阶段 {stage!r} 需要 --repo。")

    if repo is not None:
        ensure_outside_repo(repo, review_dir)
    review_dir.mkdir(parents=True, exist_ok=True)

    command = stage_command(stage, repo, review_dir, args)
    print("\n$ " + " ".join(command), flush=True)
    return subprocess.run(command).returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print_help()
        return 0

    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        print(f"未知命令: {command}\n", file=sys.stderr)
        print_help()
        return 2

    if command == "route":
        return run(INTERNAL / "rule_index.py", rest)

    if command == "prepare":
        only, rest = extract_option(rest, "--only")
        if only:
            if only not in PREPARE_ONLY:
                raise SystemExit(
                    f"prepare --only 只支持: {', '.join(sorted(PREPARE_ONLY))}"
                )
            return run_stage(only, rest)
        return run(SCRIPT_DIR / "prepare.py", rest)

    if command == "check":
        only, rest = extract_option(rest, "--only")
        if only:
            if only not in CHECK_ONLY:
                raise SystemExit(
                    f"check --only 只支持: {', '.join(sorted(CHECK_ONLY))}"
                )
            return run_stage(only, rest)
        return run(SCRIPT_DIR / "validate.py", rest)

    if command == "propose":
        return run(INTERNAL / "propose_candidates.py", rest)

    if command == "revise":
        return run(INTERNAL / "revise_candidates.py", rest)

    if command == "approve":
        return run(INTERNAL / "approvals.py", rest)

    if command == "search":
        return run(INTERNAL / "search_merchant.py", rest)

    if command == "catalog":
        return run(INTERNAL / "catalog.py", rest)

    if command == "apply":
        report, rest = has_flag(rest, "--report")
        code = run(SCRIPT_DIR / "apply.py", rest)
        if code == 0 and report:
            review, _ = extract_option(rest, "--review")
            if review:
                report_code = run(
                    INTERNAL / "stage_report.py", ["--review", review]
                )
                if report_code:
                    return report_code
            else:
                print("提示：--report 需要 --review；未生成 stage_report.md。")
        return code

    raise AssertionError(f"未处理的命令: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
