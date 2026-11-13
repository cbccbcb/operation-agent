#!/usr/bin/env python3
"""Stage 0: verify that HEAD and its upstream are exactly in sync.

This module is shared by ``prepare.py`` and the stage dispatcher.  It only runs
``git fetch`` plus read-only git commands; it never pulls, merges, rebases,
checks out, stashes or resets.  On a non-0/0 result it fails closed.

A clean fetch result (0/0) is cached outside the target repository.  With
``--sync-max-age SECONDS`` a later run may reuse it when the repository,
remote, branch, upstream and local HEAD are unchanged; the working tree status
is always recomputed.  The default is 0, i.e. always fetch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_VERSION = 1


def ensure_review_outside_repo(repo: Path, review_dir: Path) -> None:
    """Review artifacts must never pollute the target repository."""
    try:
        review_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"审核目录不能位于目标仓库内: {review_dir}\n"
        "请改到仓库之外的目录，避免 review 产物/备份被当成规则修改。"
    )


def ensure_cache_outside_repo(repo: Path, cache_path: Path) -> None:
    """The sync cache is runtime state and must stay out of the repo."""
    try:
        cache_path.resolve().relative_to(repo.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"同步缓存不能位于目标仓库内: {cache_path}\n"
        "请改到仓库之外的路径，避免运行期文件混入规则修改。"
    )


def default_cache_path(repo: Path) -> Path:
    """Per-repository cache in the OS temp directory."""
    key = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "bscat-sync-cache" / f"{key}.json"


def load_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"提示：同步缓存不可读（{exc}），忽略并重新 fetch。")
        return None
    if not isinstance(payload, dict):
        print(f"提示：同步缓存格式不正确，忽略并重新 fetch：{path}")
        return None
    return payload


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write the cache; failure only costs a future fetch."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    except OSError as exc:
        print(f"警告：无法写入同步缓存（{exc}）；本次结果不受影响。")
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def discard_cache(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cache_reuse_reason(
    cache: dict[str, Any],
    *,
    repo: Path,
    branch: str,
    upstream: str,
    remote: str,
    remote_url: str,
    head: str,
    max_age: int,
    now: float,
) -> tuple[bool, float, str]:
    """Return ``(usable, age_seconds, why_not)`` for a cached fetch result."""
    if cache.get("version") != CACHE_VERSION:
        return False, 0.0, "缓存版本不一致"
    if cache.get("repo") != str(repo):
        return False, 0.0, "仓库路径不一致"
    if cache.get("branch") != branch:
        return False, 0.0, "分支不一致"
    if cache.get("upstream") != upstream:
        return False, 0.0, "upstream 不一致"
    if cache.get("remote") != remote:
        return False, 0.0, "remote 不一致"
    cached_url = str(cache.get("remote_url") or "")
    if cached_url and remote_url and cached_url != remote_url:
        return False, 0.0, "remote URL 不一致"
    if cache.get("head") != head:
        return False, 0.0, "本地 HEAD 不一致"
    if cache.get("ahead") != 0 or cache.get("behind") != 0:
        return False, 0.0, "缓存不是干净的 0/0"
    fetched = cache.get("fetched_at_epoch")
    if not isinstance(fetched, (int, float)):
        return False, 0.0, "缓存缺少 fetched_at_epoch"
    age = now - float(fetched)
    if age < 0:
        return False, age, "缓存时间在未来"
    if age > max_age:
        return False, age, f"缓存已过期（{age:.0f}s > {max_age}s）"
    return True, age, ""


def write_sync_payload(review_dir: Path, payload: dict[str, Any]) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "git_sync.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def check_sync(
    repo: Path,
    review_dir: Path,
    *,
    max_age: int = 0,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Fail closed unless HEAD and its upstream are exactly in sync."""
    if max_age < 0:
        raise SystemExit("--sync-max-age 不能为负数。")
    cache_file = (cache_path or default_cache_path(repo)).expanduser().resolve()
    ensure_cache_outside_repo(repo, cache_file)
    if cache_file.exists() and cache_file.is_dir():
        raise SystemExit(f"同步缓存路径必须是文件，不能是目录: {cache_file}")

    branch_result = git(repo, "branch", "--show-current")
    branch = branch_result.stdout.strip()
    if branch_result.returncode or not branch:
        raise SystemExit("无法确定当前分支；请在目标仓库中手动检查后重试。")

    upstream_result = git(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    if upstream_result.returncode:
        raise SystemExit(
            f"分支 {branch!r} 没有 upstream。请先手动设置远程跟踪分支，"
            "再重新运行。本 skill 不会自动设置、pull、merge 或 rebase。"
        )
    upstream = upstream_result.stdout.strip()
    remote = upstream.split("/", 1)[0] if "/" in upstream else "origin"

    remote_url_result = git(repo, "remote", "get-url", remote)
    remote_url = (
        remote_url_result.stdout.strip() if remote_url_result.returncode == 0 else ""
    )

    head_result = git(repo, "rev-parse", "HEAD")
    head = head_result.stdout.strip()
    if head_result.returncode or not head:
        raise SystemExit("无法确定本地 HEAD；请在目标仓库中手动检查后重试。")

    # A cached 0/0 fetch may be reused only while the identity of the checkout
    # is unchanged.  The working tree is always re-read instead of cached.
    if max_age > 0:
        cache = load_cache(cache_file)
        if cache:
            usable, age, why_not = cache_reuse_reason(
                cache,
                repo=repo,
                branch=branch,
                upstream=upstream,
                remote=remote,
                remote_url=remote_url,
                head=head,
                max_age=max_age,
                now=time.time(),
            )
            if usable:
                status = git(repo, "status", "--short").stdout.strip()
                age_seconds = max(0, int(round(age)))
                payload = {
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "cache_reused": True,
                    "cache_age_seconds": age_seconds,
                    "fetched_at": str(cache.get("fetched_at") or ""),
                    "branch": branch,
                    "upstream": upstream,
                    "ahead": 0,
                    "behind": 0,
                    "head": head,
                    "upstream_head": str(cache.get("upstream_head") or ""),
                    "working_tree_status": status.splitlines() if status else [],
                }
                write_sync_payload(review_dir, payload)
                print(
                    f"同步检查通过（复用 {age_seconds}s 前的 fetch）: "
                    f"{branch} == {upstream} (0/0)"
                )
                if status:
                    print("提示：工作区存在未提交改动；分析会以当前工作区内容为准。")
                return payload
            if why_not:
                print(f"同步缓存未复用：{why_not}；重新 fetch。")

    # fetch only updates remote-tracking refs; it never touches the working tree.
    fetch = git(repo, "fetch", remote)
    if fetch.returncode:
        raise SystemExit(
            f"git fetch {remote} 失败，无法确认远程状态；请检查网络/凭据后重试。\n"
            + (fetch.stderr or "").strip()
        )

    counts = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if counts.returncode:
        raise SystemExit("无法比较本地与远程分支；请手动检查后重试。")
    parts = counts.stdout.split()
    if len(parts) != 2:
        raise SystemExit("无法解析本地与远程分支的差异计数；请手动检查后重试。")
    ahead, behind = int(parts[0]), int(parts[1])
    remote_head = git(repo, "rev-parse", "@{upstream}").stdout.strip()
    status = git(repo, "status", "--short").stdout.strip()
    fetched_at_epoch = time.time()
    fetched_at = datetime.fromtimestamp(fetched_at_epoch).isoformat(timespec="seconds")

    payload = {
        "checked_at": fetched_at,
        "cache_reused": False,
        "cache_age_seconds": 0,
        "fetched_at": fetched_at,
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "head": head,
        "upstream_head": remote_head,
        "working_tree_status": status.splitlines() if status else [],
    }
    write_sync_payload(review_dir, payload)

    if ahead or behind:
        # A dirty sync state must never become a reusable cache entry.
        discard_cache(cache_file)
        raise SystemExit(
            f"本地分支 {branch} 与 {upstream} 不同步（ahead={ahead}, behind={behind}）。\n"
            "请先手动同步（pull/merge/rebase 由你决定并执行），确认 0/0 后再运行。\n"
            "本 skill 不会自动同步，也不会 stash/checkout/reset。"
        )

    write_cache(
        cache_file,
        {
            "version": CACHE_VERSION,
            "repo": str(repo),
            "branch": branch,
            "upstream": upstream,
            "remote": remote,
            "remote_url": remote_url,
            "head": head,
            "upstream_head": remote_head,
            "ahead": 0,
            "behind": 0,
            "fetched_at": fetched_at,
            "fetched_at_epoch": fetched_at_epoch,
        },
    )

    print(f"同步检查通过: {branch} == {upstream} (0/0)")
    if status:
        print("提示：工作区存在未提交改动；分析会以当前工作区内容为准。")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段零：git 同步检查（只读）。")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument(
        "--sync-max-age",
        type=int,
        default=0,
        metavar="SECONDS",
        help=(
            "复用最近一次干净 fetch 结果的秒数；0=总是 fetch（默认）。"
            "仅在仓库/remote/分支/upstream/本地 HEAD 未变且缓存为 0/0 时复用。"
        ),
    )
    parser.add_argument(
        "--sync-cache",
        default=None,
        help="同步缓存文件；默认系统临时目录，且必须在目标仓库外。",
    )
    parser.add_argument("--json", action="store_true", help="输出检查结果 JSON。")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    ensure_review_outside_repo(repo, review_dir)
    cache_path = (
        Path(args.sync_cache).expanduser().resolve() if args.sync_cache else None
    )
    payload = check_sync(
        repo, review_dir, max_age=args.sync_max_age, cache_path=cache_path
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
