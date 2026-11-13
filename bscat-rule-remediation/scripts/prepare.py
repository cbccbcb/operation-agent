#!/usr/bin/env python3
"""阶段零/一：同步检查、解析输入、真实流水线取证、按需代码诊断。

主入口是 ``bscat.py prepare``；本脚本保留为后端实现：

    python scripts/bscat.py prepare --repo D:/Projects/AUS_BS_CAT_Model \
        --input sample.xlsx --review reviews/run-001 --problem-type all

准备阶段始终生成两份机器可读产物：

* ``rule_index.json``：引擎/表/分类/覆盖关系的运行时索引；
* ``task_profile.json``：由证据聚类推导的逐任务验证档位。

``--problem-type`` 不传时读 ``<review>/intake.json``（I1 入口定位产物），
再退回 ``all``；``--intake`` 可覆盖该文件。

``--diagnose auto``（默认）只在 diff/extra、结构性引擎、跨引擎守卫或路由异常
时运行只读代码诊断；干净的 miss 追加不再强制探索源码。
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

import common  # noqa: E402
import rule_index  # noqa: E402
from cli import ensure_outside_repo  # noqa: E402
from sync_status import check_sync  # noqa: E402


def run_internal(script: str, args: list[str]) -> None:
    """Run an internal module; its stdout/stderr stay visible."""
    command = [sys.executable, str(INTERNAL / script), *args]
    print("\n$ " + " ".join(command), flush=True)
    completed = subprocess.run(command)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="分析前准备：同步、解析、取证、写 rule_index/task_profile、按需代码诊断。"
    )
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--input", required=True, help="Excel/CSV 输入文件。")
    parser.add_argument("--review", required=True, help="审核目录。")
    parser.add_argument(
        "--problem-type",
        choices=["all", "miss", "extra", "diff"],
        default=None,
        help=(
            "本次要解决的问题类型。不传时读 <review>/intake.json 的 problem_type，"
            "再退回 all。"
        ),
    )
    parser.add_argument(
        "--intake",
        default=None,
        help="I1 入口定位产物；默认 <review>/intake.json。",
    )
    parser.add_argument(
        "--blank-expected",
        choices=["unknown", "extra"],
        default="unknown",
        help="期望分类为空时的解释；extra 表示应保持未分类。",
    )
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--column-map", default=None)
    parser.add_argument("--app-id", default=None)
    parser.add_argument(
        "--dedupe-keys", choices=["error", "suffix"], default="error"
    )
    parser.add_argument("--drop-missing-expected", action="store_true")
    parser.add_argument(
        "--diagnose",
        choices=["auto", "always", "never"],
        default="auto",
        help=(
            "只读代码诊断策略。auto: 仅 diff/extra、结构性引擎、跨引擎守卫或路由异常时运行；"
            "always: 总是运行；never: 跳过。默认 auto。"
        ),
    )
    parser.add_argument(
        "--sync-max-age",
        type=int,
        default=0,
        metavar="SECONDS",
        help=(
            "复用最近一次干净 git fetch 结果的秒数；0=每次 fetch（默认）。"
            "仅当仓库/remote/分支/upstream/本地 HEAD 未变且缓存为 0/0 时复用。"
        ),
    )
    parser.add_argument(
        "--sync-cache",
        default=None,
        help="同步缓存文件；默认系统临时目录，且必须在目标仓库外。",
    )
    parser.add_argument(
        "--skip-sync-check",
        action="store_true",
        help="仅限离线/测试；正式分析不要使用。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    ensure_outside_repo(repo, review_dir)
    review_dir.mkdir(parents=True, exist_ok=True)

    intake_path = (
        Path(args.intake).expanduser().resolve() if args.intake else None
    )
    if intake_path is not None and not intake_path.exists():
        raise SystemExit(f"--intake 指向的文件不存在: {intake_path}")
    intake = common.load_intake(intake_path or review_dir)
    problem_type = args.problem_type or str(
        intake.get("problem_type", "") or "all"
    )
    if problem_type not in {"all", "miss", "extra", "diff"}:
        raise SystemExit(f"problem_type={problem_type!r} 不合法")
    if (
        args.problem_type
        and intake.get("problem_type")
        and args.problem_type != intake["problem_type"]
    ):
        print(
            f"注意：CLI --problem-type={args.problem_type} 覆盖 "
            f"intake.json={intake['problem_type']}"
        )
    if intake:
        bits = [f"problem_type={problem_type}"]
        for field in (
            "direction", "scope", "delivery", "interaction_mode",
        ):
            value = str(intake.get(field, "") or "").strip()
            if value:
                bits.append(f"{field}={value}")
        print("入口定位（I1）：" + "，".join(bits))
    else:
        print(
            f"入口定位（I1）：未找到 intake.json；problem_type={problem_type}。"
            "如未与用户确认问题类型，先补 I1 弹窗并写入 intake.json。"
        )

    if not args.skip_sync_check:
        if args.sync_max_age < 0:
            raise SystemExit("--sync-max-age 不能为负数。")
        cache_path = (
            Path(args.sync_cache).expanduser().resolve()
            if args.sync_cache else None
        )
        if args.sync_max_age > 0:
            print(
                f"同步缓存：复用窗口 {args.sync_max_age}s"
                "（仅复用干净 0/0 fetch；准备写入前建议 0）。"
            )
        check_sync(
            repo, review_dir, max_age=args.sync_max_age, cache_path=cache_path
        )
    else:
        if args.sync_max_age > 0:
            print("警告：--skip-sync-check 优先，--sync-max-age 不生效。")
        print("警告：已跳过同步检查（--skip-sync-check）。")

    index = rule_index.build_index(repo)
    index_path = rule_index.write_json(review_dir / "rule_index.json", index)
    print(f"rule_index.json -> {index_path}")
    if index.get("index_warnings"):
        print(f"索引漂移警告：{len(index['index_warnings'])} 条（见 rule_index.json）")

    parse_args_list = [
        "--input", str(Path(args.input).expanduser().resolve()),
        "--out", str(review_dir),
        "--problem-type", problem_type,
        "--blank-expected", args.blank_expected,
    ]
    if args.sheet:
        parse_args_list += ["--sheet", args.sheet]
    if args.column_map:
        parse_args_list += ["--column-map", args.column_map]
    if args.app_id:
        parse_args_list += ["--app-id", args.app_id]
    if args.dedupe_keys != "error":
        parse_args_list += ["--dedupe-keys", args.dedupe_keys]
    if args.drop_missing_expected:
        parse_args_list.append("--drop-missing-expected")

    run_internal("parse_input.py", parse_args_list)
    parse_review_path = review_dir / "parse_review.json"
    mode = common.interaction_mode(review_dir)
    if parse_review_path.exists():
        parse_review = json.loads(parse_review_path.read_text(encoding="utf-8"))
        if parse_review.get("required") or mode == "guided":
            detail = ", ".join(parse_review.get("triggers") or []) or "guided 模式"
            print(
                "I2 解析确认：需要（"
                + detail
                + f"）；蓝图 {parse_review_path}；"
                f"fingerprint={str(parse_review.get('fingerprint', ''))[:19]}…"
            )
        else:
            print("I2 解析确认：无异常，可跳过。")
    run_internal("collect_evidence.py", ["--repo", str(repo), "--review", str(review_dir)])

    evidence_path = review_dir / "evidence.json"
    profile = None
    if evidence_path.exists():
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        profile = rule_index.build_task_profile(repo, evidence, index)
        profile_path = rule_index.write_json(
            review_dir / "task_profile.json", profile
        )
        overall = profile.get("overall") or {}
        print(
            f"task_profile.json -> {profile_path} "
            f"({overall.get('tier_label', overall.get('validation_tier', ''))})"
        )
        attribution_review = rule_index.build_attribution_review(
            profile, interaction_mode=mode
        )
        attribution_path = rule_index.write_json(
            review_dir / "attribution_review.json", attribution_review
        )
        if attribution_review.get("required"):
            detail = (
                ", ".join(attribution_review.get("triggers") or [])
                or "guided 模式"
            )
            print(
                "I3 归属/目标表确认：需要（"
                + detail
                + f"）；蓝图 {attribution_path}；"
                f"fingerprint={str(attribution_review.get('fingerprint', ''))[:19]}…"
            )
        else:
            print("I3 归属/目标表确认：路由唯一且无归属风险，可跳过。")

    run_diagnosis = args.diagnose == "always"
    if args.diagnose == "auto":
        overall = (profile or {}).get("overall") or {}
        run_diagnosis = bool(overall.get("needs_code_diagnosis"))
        diagnosis_reason = (
            "存在 diff/extra、结构性引擎、跨引擎守卫或路由异常"
            if run_diagnosis else "没有必须读源码的触发条件（diff/extra、结构性引擎、跨引擎守卫或路由异常）"
        )
    elif args.diagnose == "never":
        diagnosis_reason = "用户指定 --diagnose never"
    else:
        diagnosis_reason = "用户指定 --diagnose always"

    if run_diagnosis:
        print(f"代码诊断：运行（{diagnosis_reason}）")
        run_internal(
            "diagnose_code.py", ["--repo", str(repo), "--review", str(review_dir)]
        )
    else:
        print(f"代码诊断：跳过（{diagnosis_reason}）")
        if args.diagnose == "never" and profile:
            overall = profile.get("overall") or {}
            if overall.get("needs_code_diagnosis"):
                print(
                    "警告：任务档位本身要求完整诊断；propose 前必须人工确认匹配语义。"
                )

    print("\n准备完成。下一步：运行 bscat.py propose 生成候选，再运行 bscat.py check。")
    if problem_type != "all":
        print(
            f"范围快路径：本轮只处理 {problem_type}；"
            "collect_evidence 已按该类型自动写入 target_scope.json（若不存在）。"
        )
    print(f"审核目录: {review_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
