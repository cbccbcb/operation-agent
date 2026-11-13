#!/usr/bin/env python3
"""写前/写后 baseline 快照与差集。

背景（P0-B）：``baseline.py diff`` 的退出码对「本次写入是否引入新差异」
没有区分力——

* 任何规则文件改动都会让 ``run_meta.json`` 的指纹比对失败 → exit 1；
* 写前就存在的漂移（worktree 领先/落后 baseline）同样 exit 1；
* stdout 一定包含 ``=== Summary ===``，所以 ``bool(stdout)`` 恒为真。

本模块把 baseline 从「一次性布尔信号」变成「两次快照的集合差」：

1. 写入前运行一次 ``baseline.py diff``，保存输出行和全部规则文件的
   CRLF 归一化 SHA-256；
2. 写入后再运行一次，按多重集合做差（同一条既有噪声不会被重复计数）；
3. 新出现的行分成四类：可归因于本次写入（指纹/配置）、派生（标题、结论、
   汇总行）、语义（claim、行级分类、summary 金额）、无法归因；
4. 同时比较两次快照的逐文件指纹，列出被写入改变的文件、意外变化的文件
   和写入声明但未变化的文件。

规则文件集合与哈希算法刻意与仓库的 ``baseline.py`` 保持一致
（``_rule_files`` / ``_sha256``），否则逐文件比对会与 run_meta 对不上。

用法（内部命令，由 apply.py 在写前调用）::

    python baseline_guard.py snapshot --repo <repo> --review <dir> [--tag before]
    python baseline_guard.py status   --repo <repo> [--review <dir>] [--tag before]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

RUN_META_RELATIVE = Path("baseline") / "run_meta.json"
BASELINE_INPUT_RELATIVE = Path("sample.csv")

_SUMMARY_LINE = re.compile(r"^\d+ differences \| engine versions: ")
_EXPECTED_IF_CONFIG_TOUCHED = (
    "Engine configuration changed",
    "on_engine_error changed",
)
_ALWAYS_UNEXPECTED = (
    "Input file fingerprint changed",
    "Engine version changed",
    "Baseline format changed",
    "Baseline is missing",
    "Error:",
)


# ── fingerprints (must match AUS_BS_CAT_Model/baseline.py) ─────────────────


def _sha256(path: Path) -> str:
    """SHA-256 of the file contents with CRLF normalised to LF.

    Same algorithm as the repository's ``baseline.py``: line endings are a
    checkout artifact and must not change the fingerprint.
    """
    digest = hashlib.sha256()
    with path.open("rb") as file:
        pending = b""
        for block in iter(lambda: file.read(1024 * 1024), b""):
            data = pending + block
            if data.endswith(b"\r"):
                data, pending = data[:-1], b"\r"
            else:
                pending = b""
            digest.update(data.replace(b"\r\n", b"\n"))
        if pending:
            digest.update(pending)
    return digest.hexdigest()


def _sha256_if_exists(path: Path) -> str:
    return _sha256(path) if path.is_file() else ""


def rule_file_paths(repo: Path) -> dict[str, Path]:
    """Return ``{repo-relative posix path: absolute path}`` for rule files.

    Mirrors ``baseline.py::_rule_files``: pipeline config, category catalog,
    every ``*_engine/resources/**/*.csv`` and ``initial_engine/merchant_kb.csv``.
    """
    repo = repo.resolve()
    candidates = [
        repo / "configs" / "pipeline.json",
        repo / "configs" / "category_catalog.json",
        *sorted(repo.glob("*_engine/resources/**/*.csv")),
        repo / "initial_engine" / "merchant_kb.csv",
    ]
    result: dict[str, Path] = {}
    for path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(repo).as_posix()
        except ValueError:
            continue
        result[relative] = resolved
    return result


def current_rule_hashes(repo: Path) -> dict[str, str]:
    return {
        relative: _sha256(path)
        for relative, path in rule_file_paths(repo).items()
    }


def run_meta_path(repo: Path) -> Path:
    return repo.resolve() / RUN_META_RELATIVE


def load_run_meta(repo: Path) -> dict[str, Any]:
    path = run_meta_path(repo)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def expected_rule_hashes(repo: Path) -> dict[str, str]:
    meta = load_run_meta(repo)
    fingerprints = meta.get("fingerprints") or {}
    return fingerprints.get("rule_files") or {}


def fingerprint_state(
    repo: Path, hashes: dict[str, str] | None = None
) -> dict[str, Any]:
    """Compare current (or given) rule hashes with the baseline's run_meta."""
    expected = expected_rule_hashes(repo)
    current = current_rule_hashes(repo) if hashes is None else hashes
    changed = [
        {
            "path": path,
            "expected": expected[path],
            "actual": current[path],
        }
        for path in sorted(set(expected) & set(current))
        if expected[path] != current[path]
    ]
    return {
        "expected_count": len(expected),
        "current_count": len(current),
        "changed": changed,
        "missing": sorted(set(expected) - set(current)),
        "new": sorted(set(current) - set(expected)),
    }


# ── baseline run and snapshot ───────────────────────────────────────────────


def run_baseline(repo: Path, timeout: int = 3600) -> dict[str, Any]:
    """Run ``python baseline.py diff`` with cwd=repo and capture the output."""
    command = [sys.executable, "baseline.py", "diff"]
    environment = os.environ.copy()
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ran": False,
            "error": str(exc),
            "command": command,
            "interpreter": sys.executable,
            "duration_s": round(time.monotonic() - started, 3),
        }
    return {
        "ran": True,
        "exit_code": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "command": command,
        "interpreter": sys.executable,
        "duration_s": round(time.monotonic() - started, 3),
    }


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def make_snapshot(repo: Path, result: dict[str, Any], tag: str) -> dict[str, Any]:
    """Build the JSON snapshot for one baseline run."""
    text = (result.get("stdout") or "") + (result.get("stderr") or "")
    return {
        "tag": tag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo.resolve()),
        "interpreter": result.get("interpreter", ""),
        "command": result.get("command", []),
        "ran": bool(result.get("ran")),
        "exit_code": result.get("exit_code"),
        "duration_s": result.get("duration_s"),
        "error": result.get("error", ""),
        "lines": _lines(text),
        "rule_hashes": current_rule_hashes(repo),
        "run_meta_sha256": _sha256_if_exists(run_meta_path(repo)),
        "input_sha256": _sha256_if_exists(repo.resolve() / BASELINE_INPUT_RELATIVE),
    }


def snapshot_path(review_dir: Path, tag: str) -> Path:
    return review_dir / f"baseline_{tag}.json"


def write_snapshot(
    review_dir: Path, snapshot: dict[str, Any], text: str
) -> Path:
    path = snapshot_path(review_dir, snapshot["tag"])
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (review_dir / f"baseline_{snapshot['tag']}.txt").write_text(
        text, encoding="utf-8"
    )
    return path


def load_snapshot(review_dir: Path, tag: str) -> dict[str, Any] | None:
    path = snapshot_path(review_dir, tag)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def snapshot_is_fresh(snapshot: dict[str, Any], repo: Path) -> bool:
    """True when the snapshot still describes this repo's current rule files."""
    if not snapshot:
        return False
    if snapshot.get("repo") != str(repo.resolve()):
        return False
    return (
        snapshot.get("rule_hashes") == current_rule_hashes(repo)
        and snapshot.get("run_meta_sha256")
        == _sha256_if_exists(run_meta_path(repo))
        and snapshot.get("input_sha256")
        == _sha256_if_exists(repo.resolve() / BASELINE_INPUT_RELATIVE)
    )


def ensure_snapshot(
    repo: Path,
    review_dir: Path,
    tag: str = "before",
    force: bool = False,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Reuse a fresh snapshot; otherwise run baseline and write a new one."""
    existing = load_snapshot(review_dir, tag)
    if existing and not force and snapshot_is_fresh(existing, repo):
        existing["reused"] = True
        return existing
    result = run_baseline(repo, timeout=timeout)
    snapshot = make_snapshot(repo, result, tag)
    write_snapshot(
        review_dir,
        snapshot,
        (result.get("stdout") or "") + (result.get("stderr") or ""),
    )
    snapshot["reused"] = False
    return snapshot


# ── comparison ──────────────────────────────────────────────────────────────


def _classify_line(line: str, touched_files: set[str]) -> str:
    """Classify one newly-appeared baseline line.

    ``expected``   -- explained by the files this apply wrote;
    ``derived``    -- headers, conclusion, summary count line, baseline notes;
    ``semantic``   -- claim/row/summary changes caused by the write;
    ``unexpected`` -- cannot be attributed to the write.
    """
    text = line.strip()
    if not text:
        return "derived"
    if text.startswith("=== ") and text.endswith(" ==="):
        return "derived"
    if text.startswith("Conclusion:"):
        return "derived"
    if _SUMMARY_LINE.match(text):
        return "derived"
    if text.startswith("Note:"):
        return "derived"
    if text == "Rule file fingerprint changed":
        return "expected" if touched_files else "unexpected"
    if text.startswith(_EXPECTED_IF_CONFIG_TOUCHED):
        return (
            "expected"
            if "configs/pipeline.json" in touched_files
            else "unexpected"
        )
    if text.startswith(_ALWAYS_UNEXPECTED):
        return "unexpected"
    return "semantic"


def _drift(hashes: dict[str, str], expected: dict[str, str]) -> dict[str, Any]:
    return {
        "changed": [
            path
            for path in sorted(set(expected) & set(hashes))
            if expected[path] != hashes[path]
        ],
        "missing": sorted(set(expected) - set(hashes)),
        "new": sorted(set(hashes) - set(expected)),
    }


def _file_delta(
    before: dict[str, Any], after: dict[str, Any], touched_files: set[str]
) -> dict[str, Any]:
    before_hashes = before.get("rule_hashes") or {}
    after_hashes = after.get("rule_hashes") or {}
    changed = [
        path
        for path in sorted(set(before_hashes) | set(after_hashes))
        if before_hashes.get(path) != after_hashes.get(path)
    ]
    return {
        "touched": sorted(touched_files),
        "changed_by_write": changed,
        "unexpected_changes": [
            path for path in changed if path not in touched_files
        ],
        "touched_but_unchanged": [
            path for path in sorted(touched_files) if path not in changed
        ],
        "added": sorted(set(after_hashes) - set(before_hashes)),
        "removed": sorted(set(before_hashes) - set(after_hashes)),
        "input_changed": before.get("input_sha256")
        != after.get("input_sha256"),
        "run_meta_changed": before.get("run_meta_sha256")
        != after.get("run_meta_sha256"),
    }


def compare_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
    touched_files: list[str],
    expected_hashes: dict[str, str],
) -> dict[str, Any]:
    """Multiset difference between two baseline snapshots plus file deltas."""
    touched = {str(path).replace("\\", "/") for path in touched_files}
    before_lines = Counter(before.get("lines") or [])
    after_lines = Counter(after.get("lines") or [])
    new_lines = list((after_lines - before_lines).elements())
    removed_lines = list((before_lines - after_lines).elements())

    buckets: dict[str, list[str]] = {
        "expected": [],
        "derived": [],
        "semantic": [],
        "unexpected": [],
    }
    for line in new_lines:
        buckets[_classify_line(line, touched)].append(line)

    counts = {key: len(value) for key, value in buckets.items()}
    counts["new_total"] = len(new_lines)
    counts["removed_total"] = len(removed_lines)
    files = _file_delta(before, after, touched)

    return {
        "ran": True,
        "before_snapshot": True,
        "before_generated_at": before.get("generated_at"),
        "after_generated_at": after.get("generated_at"),
        "before_exit_code": before.get("exit_code"),
        "exit_code": after.get("exit_code"),
        "duration_s": after.get("duration_s"),
        "interpreter": after.get("interpreter", ""),
        "counts": counts,
        "expected_lines": buckets["expected"],
        "derived_lines": buckets["derived"],
        "semantic_lines": buckets["semantic"],
        "unexpected_lines": buckets["unexpected"],
        "removed_lines": removed_lines,
        "files": files,
        "drift_before": _drift(before.get("rule_hashes") or {}, expected_hashes),
        "drift_after": _drift(after.get("rule_hashes") or {}, expected_hashes),
        "has_differences": bool(
            buckets["semantic"]
            or buckets["unexpected"]
            or files["unexpected_changes"]
        ),
    }


def summarise_single(
    snapshot: dict[str, Any],
    touched_files: list[str],
    expected_hashes: dict[str, str],
) -> dict[str, Any]:
    """Fallback when no pre-write snapshot exists: classify one report only.

    Pre-existing noise cannot be separated in this mode; the counts are
    reported as-is and the caller must say so.
    """
    touched = {str(path).replace("\\", "/") for path in touched_files}
    buckets: dict[str, list[str]] = {
        "expected": [],
        "derived": [],
        "semantic": [],
        "unexpected": [],
    }
    for line in snapshot.get("lines") or []:
        buckets[_classify_line(line, touched)].append(line)

    counts = {key: len(value) for key, value in buckets.items()}
    counts["new_total"] = len(snapshot.get("lines") or [])
    counts["removed_total"] = 0
    return {
        "ran": True,
        "before_snapshot": False,
        "before_generated_at": None,
        "after_generated_at": snapshot.get("generated_at"),
        "before_exit_code": None,
        "exit_code": snapshot.get("exit_code"),
        "duration_s": snapshot.get("duration_s"),
        "interpreter": snapshot.get("interpreter", ""),
        "counts": counts,
        "expected_lines": buckets["expected"],
        "derived_lines": buckets["derived"],
        "semantic_lines": buckets["semantic"],
        "unexpected_lines": buckets["unexpected"],
        "removed_lines": [],
        "files": {
            "touched": sorted(touched),
            "changed_by_write": [],
            "unexpected_changes": [],
            "touched_but_unchanged": [],
            "added": [],
            "removed": [],
            "input_changed": False,
            "run_meta_changed": False,
        },
        "drift_before": {"changed": [], "missing": [], "new": []},
        "drift_after": _drift(
            snapshot.get("rule_hashes") or {}, expected_hashes
        ),
        "has_differences": bool(buckets["semantic"] or buckets["unexpected"]),
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


def _print_status(repo: Path, review_dir: Path | None, tag: str) -> int:
    state = fingerprint_state(repo)
    print(f"仓库: {repo}")
    print(
        f"规则文件: 当前 {state['current_count']} 个 / "
        f"run_meta {state['expected_count']} 个"
    )
    print(
        f"相对 run_meta: changed {len(state['changed'])} / "
        f"missing {len(state['missing'])} / new {len(state['new'])}"
    )
    for item in state["changed"]:
        print(f"  changed: {item['path']}")
    for path in state["missing"]:
        print(f"  missing: {path}")
    for path in state["new"]:
        print(f"  new: {path}")
    if review_dir is not None:
        snapshot = load_snapshot(review_dir, tag)
        if snapshot is None:
            print(f"写前快照: 未找到（{snapshot_path(review_dir, tag)}）")
        else:
            fresh = snapshot_is_fresh(snapshot, repo)
            print(
                f"写前快照: {snapshot_path(review_dir, tag)} "
                f"({snapshot.get('generated_at')}, "
                f"{'与当前仓库一致' if fresh else '已过期，需要重跑'})"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="写前 baseline 快照、逐文件指纹与写后差集。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser(
        "snapshot", help="运行 baseline.py diff 并把输出/指纹保存为快照。"
    )
    snapshot.add_argument("--repo", required=True)
    snapshot.add_argument("--review", required=True)
    snapshot.add_argument("--tag", default="before")
    snapshot.add_argument("--force", action="store_true")
    snapshot.add_argument("--timeout", type=int, default=3600)

    status = subparsers.add_parser(
        "status", help="只读查看逐文件指纹与既有漂移，不运行流水线。"
    )
    status.add_argument("--repo", required=True)
    status.add_argument("--review", default=None)
    status.add_argument("--tag", default="before")

    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    if args.command == "status":
        review = (
            Path(args.review).expanduser().resolve() if args.review else None
        )
        return _print_status(repo, review, args.tag)

    review_dir = Path(args.review).expanduser().resolve()
    review_dir.mkdir(parents=True, exist_ok=True)
    snapshot = ensure_snapshot(
        repo, review_dir, tag=args.tag, force=args.force, timeout=args.timeout
    )
    print(
        f"baseline 快照: {snapshot_path(review_dir, args.tag)} "
        f"({'复用已有快照' if snapshot.get('reused') else '新运行'})"
    )
    print(
        f"  baseline.py exit code: {snapshot.get('exit_code')} | "
        f"耗时 {snapshot.get('duration_s')}s | "
        f"输出行 {len(snapshot.get('lines') or [])}"
    )
    state = fingerprint_state(repo, snapshot.get("rule_hashes") or {})
    print(
        f"  规则文件 {state['current_count']} 个；"
        f"相对 run_meta 漂移 {len(state['changed'])} 个（写前既有，不计入本次回归）"
    )
    for item in state["changed"]:
        print(f"    drift: {item['path']}")
    if snapshot.get("error"):
        print(f"  错误: {snapshot['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
