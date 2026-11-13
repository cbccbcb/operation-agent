#!/usr/bin/env python3
"""Apply *confirmed* candidates to the working-branch rule tables.

Safety model:
- dry-run unless ``--apply`` is passed;
- only ``status == confirmed`` and validation-passing candidates;
- high-risk candidates additionally require ``--allow-high-risk`` (the skill
  passes it only after per-item human confirmation);
- every touched file is backed up outside the repository before writing;
- writes are atomic (temp file + os.replace) and the repository is never
  committed or pushed.

    python apply_rules.py --repo ... --review reviews/run-001            # dry run
    python apply_rules.py --repo ... --review reviews/run-001 --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

# merchant_kb.csv contains very long pipe-separated keyword fields.  Windows
# C long is 32-bit, so sys.maxsize overflows; clamp to the safe 32-bit maximum.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

RULE_OPERATIONS = {"append", "edit", "delete", "keyword_update"}
CONFIG_OPERATIONS = {"catalog", "priority"}
CONFIG_TARGETS = {
    "catalog": "configs/category_catalog.json",
    "priority": "configs/pipeline.json",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def has_bom(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(3) == b"\xef\xbb\xbf"


def detect_newline(path: Path) -> str:
    with path.open("rb") as handle:
        sample = handle.read(65536)
    return "\r\n" if b"\r\n" in sample else "\n"


def sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matches(row: dict[str, str], match_key: dict[str, Any]) -> bool:
    return all(str(row.get(key, "")) == str(value) for key, value in match_key.items())


def read_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SystemExit(f"规则表为空: {path}")
        return list(reader.fieldnames), list(reader)


def write_table(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    encoding = "utf-8-sig" if has_bom(path) else "utf-8"
    newline = detect_newline(path)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("w", encoding=encoding, newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=header, extrasaction="ignore", lineterminator=newline
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in header})
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_rows(path: Path, rows: list[dict[str, str]]) -> None:
    """Append without rewriting the 874k-row merchant KB."""
    header, _ = read_table(path)
    encoding = "utf-8" if has_bom(path) else "utf-8"
    newline = detect_newline(path)
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 1))
        needs_newline = handle.read(1) not in (b"\n", b"\r")
    with path.open("a", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=header, extrasaction="ignore", lineterminator=newline
        )
        if needs_newline:
            handle.write(newline)
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in header})


def apply_initial_streaming(
    target: Path,
    group: list[dict[str, Any]],
    header: list[str],
) -> tuple[int, int]:
    """Apply initial-engine changes without loading the 874k-row KB.

    ``merchant_kb.csv`` is large enough that materialising every row as a dict
    is wasteful.  This streaming rewrite handles append / edit / delete /
    keyword_update in one pass, verifies that every edit-like candidate matched
    exactly one row, and atomically replaces the file.
    """
    encoding = "utf-8-sig" if has_bom(target) else "utf-8"
    newline = detect_newline(target)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    # Appends are verified by the duplicate-row check below, not by a row
    # match.  Seed them as satisfied so a mixed append + keyword_update batch
    # does not fail the final "exactly one row" audit.
    matched = {
        candidate["candidate_id"]: (1 if candidate["operation"] == "append" else 0)
        for candidate in group
    }
    append_rows_list = [
        {column: str((candidate.get("row") or {}).get(column, "")) for column in header}
        for candidate in group
        if candidate["operation"] == "append"
    ]
    append_keys = {tuple(row.get(column, "") for column in header) for row in append_rows_list}
    rows_before = 0
    rows_after = 0
    try:
        with target.open(encoding="utf-8-sig", newline="") as source, temp_path.open(
            "w", encoding=encoding, newline=""
        ) as destination:
            reader = csv.reader(source)
            writer = csv.writer(destination, lineterminator=newline)
            source_header = next(reader)
            if source_header != header:
                raise SystemExit(
                    f"{target}: 表头在读取后发生变化，拒绝写入"
                )
            writer.writerow(source_header)
            for values in reader:
                rows_before += 1
                row = {
                    name: values[index] if index < len(values) else ""
                    for index, name in enumerate(source_header)
                }
                hits = [
                    candidate for candidate in group
                    if candidate["operation"] != "append"
                    and matches(row, candidate.get("match_key") or {})
                ]
                if len(hits) > 1:
                    raise SystemExit(
                        "多个候选同时命中 merchant_kb 的同一行: "
                        + ", ".join(candidate["candidate_id"] for candidate in hits)
                    )
                if hits:
                    candidate = hits[0]
                    matched[candidate["candidate_id"]] += 1
                    if candidate["operation"] == "delete":
                        continue
                    if candidate["operation"] == "edit":
                        update = {
                            key: str(value)
                            for key, value in (candidate.get("row") or {}).items()
                        }
                        unknown = sorted(set(update) - set(source_header))
                        if unknown:
                            raise SystemExit(
                                f"{candidate['candidate_id']}: 以下列不存在于 "
                                f"{target.name}: {unknown}"
                            )
                        row.update(update)
                    elif candidate["operation"] == "keyword_update":
                        try:
                            final_keywords, _ = common.merge_initial_keywords(
                                row.get("keywords", ""), candidate.get("row") or {}
                            )
                        except ValueError as exc:
                            raise SystemExit(
                                f"{candidate['candidate_id']}: {exc}"
                            ) from exc
                        row["keywords"] = final_keywords
                    else:
                        raise SystemExit(
                            f"{candidate['candidate_id']}: 不支持的操作 "
                            f"{candidate['operation']}"
                        )
                key = tuple(row.get(column, "") for column in source_header)
                if key in append_keys:
                    raise SystemExit(
                        "追加的行已存在于 merchant_kb: " + " | ".join(key)
                    )
                writer.writerow([row.get(column, "") for column in source_header])
                rows_after += 1
            for row in append_rows_list:
                writer.writerow([row.get(column, "") for column in source_header])
                rows_after += 1
        bad = [
            candidate_id for candidate_id, count in matched.items() if count != 1
        ]
        if bad:
            raise SystemExit(
                "以下候选在 merchant_kb 中没有恰好匹配 1 行: " + ", ".join(bad)
            )
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return rows_before, rows_after


def ensure_under(path: Path, repo: Path, allowed: tuple[str, ...]) -> Path:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(repo.resolve())
    except ValueError as exc:
        raise SystemExit(f"拒绝写入仓库之外的路径: {resolved}") from exc
    if not any(str(relative).startswith(prefix) for prefix in allowed):
        raise SystemExit(f"拒绝写入非预期的仓库路径: {relative}")
    return resolved


def backup(path: Path, backup_root: Path, repo: Path) -> Path:
    relative = path.resolve().relative_to(repo.resolve())
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def apply_rule_candidates(
    repo: Path,
    candidates: list[dict[str, Any]],
    backup_root: Path,
    attempted: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_file: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_file.setdefault(candidate["target_file"], []).append(candidate)

    for target_file, group in sorted(by_file.items()):
        target = ensure_under(
            repo / target_file, repo, ("transfer_engine", "initial_engine",
                                       "dishonour_engine", "gambling_engine",
                                       "income_engine", "liability_engine",
                                       "all_other_credit_engine", "fee_engine",
                                       "rent_engine", "catch_all_engine")
        )
        backup_path = backup(target, backup_root, repo)
        entry: dict[str, Any] = {
            "target_file": target_file,
            "backup": str(backup_path),
        }
        attempted.append(entry)
        # The merchant KB is 74MB / ~874k rows.  Every initial batch is
        # applied by streaming so the file is never materialised in memory.
        if target_file == "initial_engine/merchant_kb.csv":
            before_sha = sha256(target)
            with target.open(encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle))
            rows_before, rows_after = apply_initial_streaming(target, group, header)
            entry.update(
                {
                    "sha256_before": before_sha,
                    "sha256_after": sha256(target),
                    "rows_before": rows_before,
                    "rows_after": rows_after,
                    "candidates": [
                        candidate["candidate_id"] for candidate in group
                    ],
                }
            )
            continue

        before_sha = sha256(target)
        header, rows = read_table(target)
        before_count = len(rows)
        appended: list[dict[str, str]] = []

        for candidate in group:
            operation = candidate["operation"]
            if operation == "append":
                row = {column: str((candidate.get("row") or {}).get(column, "")) for column in header}
                key = tuple(row.get(column, "") for column in header)
                if any(tuple(existing.get(column, "") for column in header) == key for existing in rows):
                    raise SystemExit(
                        f"{candidate['candidate_id']}: 相同行已存在于 {target_file}"
                    )
                rows.append(row)
                appended.append(row)
            elif operation in {"edit", "delete"}:
                match_key = candidate.get("match_key") or {}
                hits = [index for index, row in enumerate(rows) if matches(row, match_key)]
                if len(hits) != 1:
                    raise SystemExit(
                        f"{candidate['candidate_id']}: match_key 在 {target_file} 中匹配到 {len(hits)} 行（必须恰好 1 行）"
                    )
                index = hits[0]
                if operation == "delete":
                    rows.pop(index)
                else:
                    update = candidate.get("row") or {}
                    unknown = sorted(set(update) - set(header))
                    if unknown:
                        raise SystemExit(
                            f"{candidate['candidate_id']}: 以下列不存在于 {target_file}: {unknown}"
                        )
                    rows[index].update({key: str(value) for key, value in update.items()})
            else:
                raise SystemExit(f"{candidate['candidate_id']}: 不支持的操作 {operation}")

        if appended and not any(candidate["operation"] != "append" for candidate in group):
            append_rows(target, appended)
        else:
            write_table(target, header, rows)

        entry.update(
            {
                "sha256_before": before_sha,
                "sha256_after": sha256(target),
                "rows_before": before_count,
                "rows_after": len(rows),
                "candidates": [candidate["candidate_id"] for candidate in group],
            }
        )
    return attempted


def apply_config_candidates(
    repo: Path,
    candidates: list[dict[str, Any]],
    backup_root: Path,
    attempted: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for candidate in candidates:
        target_file = candidate["target_file"]
        operation = candidate["operation"]
        expected_target = CONFIG_TARGETS.get(operation)
        if expected_target and target_file != expected_target:
            raise SystemExit(
                f"{candidate['candidate_id']}: {operation} 只允许写 "
                f"{expected_target!r}，拒绝 {target_file!r}"
            )
        target = ensure_under(repo / target_file, repo, ("configs",))
        backup_path = backup(target, backup_root, repo)
        attempt_entry: dict[str, Any] = {
            "target_file": target_file,
            "backup": str(backup_path),
        }
        attempted.append(attempt_entry)
        before_sha = sha256(target)
        payload = load_json(target)
        change = candidate.get("row") or candidate.get("proposed_change") or {}

        if operation == "catalog":
            category = change.get("category") or candidate.get("category")
            if not category:
                raise SystemExit(f"{candidate['candidate_id']}: catalog 变更需要 category")
            categories = payload.setdefault("categories", {})
            catalog_entry = categories.setdefault(category, {})
            for key in ("owner_engine_id", "active"):
                if key in change:
                    catalog_entry[key] = change[key]
        elif operation == "priority":
            engine_id = change.get("engine_id") or candidate.get("engine")
            engines = payload.get("pipeline", [])
            hits = [engine for engine in engines if engine.get("engine_id") == engine_id]
            if len(hits) != 1:
                raise SystemExit(
                    f"{candidate['candidate_id']}: 引擎 {engine_id!r} 未找到或匹配到多行"
                )
            for key in ("priority", "enabled"):
                if key in change:
                    hits[0][key] = change[key]
            new_priority = int(hits[0].get("priority"))
            clashes = [
                engine.get("engine_id")
                for engine in engines
                if engine.get("engine_id") != engine_id
                and engine.get("enabled", True)
                and int(engine.get("priority", 0)) == new_priority
            ]
            if clashes:
                raise SystemExit(
                    f"{candidate['candidate_id']}: priority {new_priority} 与已启用引擎 "
                    f"{clashes} 冲突"
                )
        else:
            raise SystemExit(f"{candidate['candidate_id']}: 不支持的配置操作 {operation}")

        dump_json(target, payload)
        attempt_entry.update(
            {
                "sha256_before": before_sha,
                "sha256_after": sha256(target),
                "candidates": [candidate["candidate_id"]],
            }
        )
    return attempted


def main() -> int:
    parser = argparse.ArgumentParser(description="写入已确认的规则候选。")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--apply", action="store_true", help="实际写入；默认只做 dry-run。")
    parser.add_argument(
        "--allow-high-risk",
        action="store_true",
        help="存在已确认的高风险候选时必须添加。",
    )
    parser.add_argument("--only", default=None, help="逗号分隔的候选 ID。")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    review_dir = Path(args.review).resolve()
    payload = load_json(review_dir / "candidates.json")
    candidates = payload.get("candidates", [])

    if args.only:
        wanted = {value.strip() for value in args.only.split(",") if value.strip()}
        candidates = [candidate for candidate in candidates if candidate["candidate_id"] in wanted]
        missing = wanted - {candidate["candidate_id"] for candidate in candidates}
        if missing:
            raise SystemExit(f"未知候选 ID: {sorted(missing)}")

    confirmed = [candidate for candidate in candidates if candidate.get("status") == "confirmed"]
    if not confirmed:
        raise SystemExit("没有已确认的候选。人工审批后请把 status 设为 confirmed。")

    invalid = [
        candidate["candidate_id"]
        for candidate in confirmed
        if (candidate.get("validation") or {}).get("valid") is False
    ]
    if invalid:
        raise SystemExit(f"以下候选已确认但校验无效: {invalid}")

    high_risk = [
        candidate["candidate_id"]
        for candidate in confirmed
        if candidate.get("risk_level") == "high"
    ]
    if high_risk and not args.allow_high_risk:
        raise SystemExit(
            "高风险候选需要逐条审批后添加 --allow-high-risk: "
            + ", ".join(high_risk)
        )

    rule_candidates = [c for c in confirmed if c["operation"] in RULE_OPERATIONS]
    config_candidates = [c for c in confirmed if c["operation"] in CONFIG_OPERATIONS]
    unsupported = [
        c["candidate_id"] for c in confirmed
        if c["operation"] not in RULE_OPERATIONS | CONFIG_OPERATIONS
    ]
    if unsupported:
        raise SystemExit(f"以下建议不能自动写入: {unsupported}")

    # Defence in depth: never let a candidate target a non-classifier table for
    # an append, or emit a category its engine does not own.
    for candidate in rule_candidates:
        spec = common.spec_for(
            str(candidate.get("engine", "")), str(candidate.get("target_file", ""))
        )
        if spec is None:
            raise SystemExit(
                f"{candidate['candidate_id']}: 引擎 {candidate.get('engine')!r} 的规则表未知 "
                f"{candidate.get('target_file')!r}"
            )
        if candidate["operation"] == "append" and not spec.appendable:
            raise SystemExit(
                f"{candidate['candidate_id']}: {candidate['target_file']} 不是可追加的分类器表"
            )
        if candidate["operation"] == "keyword_update" and (
            candidate.get("engine") != "initial"
            or candidate.get("target_file") != "initial_engine/merchant_kb.csv"
        ):
            raise SystemExit(
                f"{candidate['candidate_id']}: keyword_update 只允许写 "
                "initial_engine/merchant_kb.csv"
            )
        if candidate["operation"] == "append":
            category = candidate.get("category") or (candidate.get("row") or {}).get("category")
            if category and not common.category_allowed(
                repo, str(category), str(candidate.get("engine", ""))
            ):
                raise SystemExit(
                    f"{candidate['candidate_id']}: 引擎 "
                    f"{candidate.get('engine')!r} 不拥有分类 {category!r}"
                )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = review_dir / "backups" / timestamp

    print(f"已确认候选: {len(confirmed)}")
    for candidate in confirmed:
        print(
            f"  {candidate['candidate_id']}: "
            f"{common.problem_type_label(candidate.get('problem_type', '')) or '未标注'} / "
            f"{candidate['operation']} {candidate.get('target_file', '')} "
            f"({candidate.get('risk_level', 'high')})"
        )
    if not args.apply:
        print("\n仅 dry-run。确认无误后添加 --apply 才会写入。")
        return 0

    entries: list[dict[str, Any]] = []
    attempted: list[dict[str, Any]] = []
    try:
        if rule_candidates:
            apply_rule_candidates(repo, rule_candidates, backup_root, attempted)
        if config_candidates:
            apply_config_candidates(repo, config_candidates, backup_root, attempted)
        entries = attempted
    except BaseException:
        # Restore every backup created in this run, including the file whose
        # write failed (its entry was registered before the write started).
        # Almost every refusal below raises SystemExit, which inherits from
        # BaseException rather than Exception -- catching Exception here left
        # files already written in the batch modified after a later failure.
        for entry in attempted:
            backup_path = Path(entry["backup"])
            if backup_path.exists():
                shutil.copy2(backup_path, repo / entry["target_file"])
        raise

    report = {
        "applied_at": timestamp,
        "repo": str(repo),
        "review_dir": str(review_dir),
        "candidate_ids": [candidate["candidate_id"] for candidate in confirmed],
        "candidates": [
            {
                "candidate_id": candidate["candidate_id"],
                "problem_type": candidate.get("problem_type", ""),
                "operation": candidate["operation"],
                "target_file": candidate.get("target_file", ""),
            }
            for candidate in confirmed
        ],
        "entries": entries,
    }
    dump_json(review_dir / "apply_report.json", report)

    print("\n已写入:")
    for entry in entries:
        print(
            f"  {entry['target_file']}: rows {entry.get('rows_before', '?')} -> "
            f"{entry.get('rows_after', '?')} | backup {entry['backup']}"
        )
    print(f"\napply_report.json -> {review_dir / 'apply_report.json'}")

    try:
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--stat"],
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.stdout.strip():
            print("\ngit diff --stat:\n" + diff.stdout.strip())
        else:
            print("\ngit diff --stat: （未检测到变化）")
    except FileNotFoundError:
        print("\n未找到 git；请手工检查变更。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
