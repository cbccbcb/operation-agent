#!/usr/bin/env python3
"""恢复 ``apply_rules.py`` 备份的规则/配置文件。

    python rollback.py --review reviews/run-001            # dry-run
    python rollback.py --review reviews/run-001 --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="回滚上一次规则写入。")
    parser.add_argument("--review", required=True, help="包含 apply_report.json 的审核目录。")
    parser.add_argument("--apply", action="store_true", help="实际恢复；默认只做 dry-run。")
    parser.add_argument(
        "--force",
        action="store_true",
        help="当前文件已不再等于写入后的哈希时，仍然强制恢复。",
    )
    args = parser.parse_args()

    review_dir = Path(args.review).resolve()
    report_path = review_dir / "apply_report.json"
    if not report_path.exists():
        raise SystemExit(f"未找到 apply_report.json: {report_path}")

    with report_path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    repo = Path(report["repo"]).resolve()

    entries = report.get("entries", [])
    if not entries:
        raise SystemExit("apply_report.json 中没有可恢复的记录")

    conflicts = []
    for entry in entries:
        backup = Path(entry["backup"])
        target = repo / entry["target_file"]
        if not backup.exists():
            raise SystemExit(f"备份文件缺失: {backup}")
        expected = entry.get("sha256_after")
        if target.exists() and expected and sha256(target) != expected:
            conflicts.append(str(target))
        status = "恢复" if args.apply else "将恢复"
        print(f"{status}: {target} <- {backup}")

    if conflicts and not args.force:
        raise SystemExit(
            "以下文件在写入后又发生了变化，拒绝覆盖（确认后可加 --force）:\n"
            + "\n".join(f"  {path}" for path in conflicts)
        )

    if not args.apply:
        print("\n仅 dry-run。确认后添加 --apply 才会恢复。")
        return 0

    # 逆序恢复，使重叠文件回到最早的状态。
    for entry in reversed(entries):
        backup = Path(entry["backup"])
        target = repo / entry["target_file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
        expected = entry.get("sha256_before")
        if expected and sha256(target) != expected:
            print(f"警告: 恢复后的哈希与写入前记录不一致: {target}")

    print(f"\n已恢复 {len(entries)} 个文件。未执行 commit 或 push。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
