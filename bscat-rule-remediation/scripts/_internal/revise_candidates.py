#!/usr/bin/env python3
"""Stage 4b: apply dialogue revisions to candidates.json.

The normal workflow is dialogue-first: the user reviews and edits candidates
in the conversation, and the agent turns those decisions into a ``--patch``
JSON file.  The JSON file remains the machine source of truth; the per-engine
CSVs are read-only exports for inspection.  For backward compatibility, the
script can still merge edits made directly in the review CSVs.

Safety rules:

* ``--patch`` accepts only rule/row/match_key/reason/rationale changes; approval
  status is deliberately *not* accepted (stage five uses
  ``approvals.py --build`` after the dialogue);
* rule columns are diffed against the row currently in the target table, so a
  no-op edit never becomes a full-row overwrite;
* ``delete`` cannot carry rule-column edits, and ``keyword_update`` only
  accepts ``keywords``/``add_keywords``/``remove_keywords``;
* content changes reset the candidate to ``pending`` and clear its validation,
  forcing a fresh validation and a fresh approval;
* legacy CSV merges go through the same I5 preview and ``patch_sha256`` gate
  as dialogue patches, so direct CSV edits cannot bypass confirmation;
* the JSON is never regenerated from CSV wholesale -- unknown IDs and
  duplicate IDs are hard errors.

    python revise_candidates.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001 --patch dialog_patch.json [--dry-run] [--validate]
    python revise_candidates.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001 [--dry-run] [--validate]   # legacy CSV merge
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402
from render_candidates import METADATA_COLUMNS  # noqa: E402

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

RULE_OPERATIONS = {"append", "edit", "delete", "keyword_update"}
ALLOWED_STATUSES = {"pending", "confirmed", "rejected"}
PATCH_TOP_KEYS = {"changes", "source", "note"}
PATCH_CHANGE_KEYS = {
    "candidate_id", "row", "match_key", "reason", "rationale",
    "source_trace", "operation",
}
KEYWORD_UPDATE_KEYS = {"keywords", "add_keywords", "remove_keywords"}
DOWNGRADE_OPERATIONS = {"suggestion"}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def iter_rows(path: Path) -> Iterable[tuple[int, dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line, row in enumerate(reader, start=2):
            yield line, {str(key): str(value or "") for key, value in row.items()}


def matches_key(row: dict[str, str], match_key: dict[str, Any]) -> bool:
    return all(str(row.get(key, "")) == str(value) for key, value in match_key.items())


def read_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            return next(reader)
        except StopIteration:
            raise SystemExit(f"规则表为空: {path}")


def normalize_match_key(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise SystemExit("patch.match_key 必须是对象且不能为空")
    return {str(key): str(item) for key, item in value.items()}


def normalize_patch_row(value: Any, *, allow_lists: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise SystemExit("patch.row 必须是非空对象")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, list):
            if not allow_lists:
                raise SystemExit(f"patch.row.{key} 不能是数组")
            result[str(key)] = [str(entry) for entry in item]
        elif isinstance(item, (dict, tuple, set)):
            raise SystemExit(f"patch.row.{key} 不能是对象")
        else:
            result[str(key)] = str(item)
    return result


def find_current_row(
    target: Path,
    match_key: dict[str, Any],
    cache: dict[tuple[str, str], dict[str, str]],
) -> dict[str, str]:
    cache_key = (str(target), json.dumps(match_key, sort_keys=True))
    if cache_key in cache:
        return cache[cache_key]
    hits: list[dict[str, str]] = []
    for _, row in iter_rows(target):
        if matches_key(row, match_key):
            hits.append(row)
            if len(hits) > 1:
                break
    if len(hits) != 1:
        raise SystemExit(
            f"match_key 在 {target.name} 中匹配到 {len(hits)} 行（必须恰好 1 行）: "
            f"{json.dumps(match_key, ensure_ascii=False)}"
        )
    cache[cache_key] = hits[0]
    return hits[0]


def rule_header(header: list[str]) -> list[str]:
    return [column for column in header if column not in METADATA_COLUMNS]


def diff_append(
    candidate: dict[str, Any],
    csv_row: dict[str, str],
    header: list[str],
) -> dict[str, Any] | None:
    provided = {str(key): str(value) for key, value in (candidate.get("row") or {}).items()}
    changed = any(csv_row.get(column, "") != provided.get(column, "") for column in header)
    if not changed:
        return None
    return {column: csv_row.get(column, "") for column in header}


def diff_edit(
    candidate: dict[str, Any],
    csv_row: dict[str, str],
    header: list[str],
    current: dict[str, str],
) -> dict[str, Any] | None:
    provided = {str(key): str(value) for key, value in (candidate.get("row") or {}).items()}
    unknown = sorted(set(provided) - set(header))
    if unknown:
        raise SystemExit(
            f"{candidate['candidate_id']}: candidates.json 的 row 含未知列 {unknown}"
        )
    updated: dict[str, str] = {}
    for column in header:
        csv_value = csv_row.get(column, "")
        if csv_value != current.get(column, ""):
            updated[column] = csv_value
    # A field the reviewer reverted to the current value drops out of the
    # patch entirely, keeping the stored candidate minimal.
    return updated or None


def same_update(candidate: dict[str, Any], update: dict[str, Any]) -> bool:
    """True when the CSV still describes exactly the stored change."""
    stored = {
        str(key): value for key, value in (candidate.get("row") or {}).items()
    }

    def normalized(payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, list):
                result[key] = sorted(str(item) for item in value)
            else:
                result[key] = str(value)
        return result

    return normalized(stored) == normalized(update)


def same_final_keywords(candidate: dict[str, Any], final_keywords: list[str]) -> bool:
    """True when a full-``keywords`` candidate already describes the CSV state."""
    stored = (candidate.get("row") or {}).get("keywords")
    if stored is None:
        return False
    stored_keywords = common.split_initial_keywords(str(stored))
    return (
        sorted(keyword.upper() for keyword in stored_keywords)
        == sorted(keyword.upper() for keyword in final_keywords)
    )


def diff_keyword_update(
    candidate: dict[str, Any],
    csv_row: dict[str, str],
    header: list[str],
    current: dict[str, str],
) -> tuple[dict[str, Any], list[str]] | None:
    non_keyword = [column for column in header if column != "keywords"]
    changed_other = [
        column for column in non_keyword
        if csv_row.get(column, "") != current.get(column, "")
    ]
    if changed_other:
        raise SystemExit(
            f"{candidate['candidate_id']}: keyword_update 只能改 keywords 列；"
            f"以下列也被改动: {changed_other}"
        )
    current_keywords = common.split_initial_keywords(current.get("keywords", ""))
    final_keywords = common.split_initial_keywords(csv_row.get("keywords", ""))
    try:
        common.merge_initial_keywords(
            current.get("keywords", ""),
            {"keywords": "|".join(final_keywords)},
        )
    except ValueError as exc:
        raise SystemExit(f"{candidate['candidate_id']}: {exc}") from exc

    added = [value for value in final_keywords if value not in current_keywords]
    removed = [value for value in current_keywords if value not in final_keywords]
    if not added and not removed:
        return None
    update: dict[str, Any] = {}
    if added:
        update["add_keywords"] = added
    if removed:
        update["remove_keywords"] = removed
    return update, final_keywords


def build_revise_preview(
    patch_path: Path,
    patch_hash: str,
    changes: list[dict[str, Any]],
    *,
    force_confirm: bool = False,
) -> dict[str, Any]:
    triggers: list[str] = []
    if any("match_key" in item.get("changed", {}) for item in changes):
        triggers.append("match_key")
    if any("source_trace" in item.get("changed", {}) for item in changes):
        triggers.append("source_trace")
    if len(changes) > 1:
        triggers.append("multiple_changes")
    if any(len(item.get("changed") or {}) > 1 for item in changes):
        triggers.append("multiple_fields")
    material = {
        "patch_sha256": patch_hash,
        "changes": [
            {
                "candidate_id": item.get("candidate_id"),
                "changed": item.get("changed"),
            }
            for item in changes
        ],
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "patch": str(patch_path),
        "patch_sha256": patch_hash,
        "required": bool(triggers) or force_confirm,
        "triggers": triggers,
        "changed": len(changes),
        "candidates": [
            str(item.get("candidate_id", "")) for item in changes
        ],
        "changes": changes,
        "questions": [{
            "header": "I5 修订预览",
            "prompt": (
                f"即将应用 {len(changes)} 处修改；"
                f"触发项: {', '.join(triggers) or '无'}。确认应用？"
            ),
            "option_keys": ["apply", "continue_editing", "cancel"],
        }] if (triggers or force_confirm) else [],
        "fingerprint": common.sha256_json(material),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _print_revise_preview(preview: dict[str, Any], preview_json: Path) -> None:
    print(f"\n修订预览（I5）-> {preview_json}")
    if preview["required"]:
        detail = ", ".join(preview["triggers"]) or "guided 模式"
        print(
            "I5 修订确认：需要（"
            + detail
            + f"）；fingerprint={preview['fingerprint']}"
        )
        print(
            "确认后写入 dialog_decisions.json#revise："
            '{"decision": "apply", '
            f'"patch_sha256": "{preview["patch_sha256"]}", '
            '"decided_at": "<ISO 时间>"}'
        )
    else:
        print("I5 修订确认：未触发（不含 match_key/source_trace 或多字段修改）。")


def revise_gate_errors(
    review_dir: Path,
    preview: dict[str, Any],
    decisions_path: Path | None,
) -> list[str]:
    """Require I5 for match_key/source_trace or multi-field revisions."""
    payload = (
        common.load_dialog_decisions(decisions_path, explicit=True)
        if decisions_path is not None
        else common.load_dialog_decisions(review_dir)
    )
    section = payload.get("revise") or {}
    triggers = [str(value) for value in preview.get("triggers") or []]
    mode = common.interaction_mode(review_dir)
    required = bool(preview.get("required")) or (
        mode == "guided" and bool(preview.get("changes"))
    )
    if mode == "minimal":
        required = any(value in {"match_key", "source_trace"} for value in triggers)
    if not section:
        if not required:
            return []
        return [
            "缺少 I5 修订预览；请先运行带 --dry-run 的 revise，"
            "查看 revise_preview.json 并确认后写入 dialog_decisions.json#revise"
        ]
    errors: list[str] = []
    if not str(section.get("decided_at", "") or "").strip():
        errors.append("I5 决策缺少 decided_at")
    decision = str(section.get("decision", "") or "").strip()
    if decision not in common.REVISE_DECISIONS:
        errors.append(
            f"I5 decision={decision!r} 不合法；"
            f"允许: {sorted(common.REVISE_DECISIONS)}"
        )
        return errors
    if decision != "apply":
        return [*errors, f"用户在 I5 选择 {decision}；未写入 candidates.json"]
    if not required:
        if section.get("patch_sha256") and (
            str(section.get("patch_sha256")) != str(preview.get("patch_sha256"))
        ):
            errors.append("I5 确认的补丁已被修改；请重新预览并确认")
        return errors
    expected_hash = str(preview.get("patch_sha256", "") or "")
    actual_hash = str(section.get("patch_sha256", "") or "")
    if not actual_hash:
        errors.append("I5 决策缺少 patch_sha256；请从 revise_preview.json 读取")
    elif actual_hash != expected_hash:
        errors.append(
            "补丁在 I5 确认后又被修改"
            f"（确认 {actual_hash}，当前 {expected_hash}）；请重新预览并确认"
        )
    if section.get("fingerprint") and (
        str(section.get("fingerprint")) != str(preview.get("fingerprint"))
    ):
        errors.append("I5 决策 fingerprint 已过期；请重新预览并确认")
    return errors


def revise_from_patch(
    repo: Path,
    review_dir: Path,
    patch_path: Path,
    *,
    dry_run: bool = False,
    preview_json: Path | None = None,
    decisions_path: Path | None = None,
) -> dict[str, Any]:
    """Apply rule edits that the user described in the dialogue.

    The patch is an agent-written bridge, not a human editing surface: the user
    only answers/chooses in the conversation, and the agent turns those choices
    into this JSON file.  Approval status is deliberately **not** accepted here
    -- it belongs to stage five and ``approvals.py --build``.
    """
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        raise SystemExit(f"找不到 {candidates_path}")
    if not patch_path.exists():
        raise SystemExit(f"找不到对话修订补丁 {patch_path}")
    patch_hash = common.sha256_file(patch_path)

    candidate_payload = load_json(candidates_path)
    candidates = candidate_payload.get("candidates") or []
    by_id = {str(candidate.get("candidate_id", "")): candidate for candidate in candidates}

    patch_payload = load_json(patch_path)
    if not isinstance(patch_payload, dict):
        raise SystemExit("对话修订补丁必须是 JSON 对象")
    unknown_top = sorted(set(patch_payload) - PATCH_TOP_KEYS)
    if unknown_top:
        raise SystemExit(f"对话修订补丁含未知顶层字段: {unknown_top}")
    changes_in = patch_payload.get("changes")
    if not isinstance(changes_in, list) or not changes_in:
        raise SystemExit("对话修订补丁必须包含非空 changes 数组")

    row_cache: dict[tuple[str, str], dict[str, str]] = {}
    header_cache: dict[str, list[str]] = {}
    changes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, item in enumerate(changes_in, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"changes[{index}] 必须是对象")
        unknown = sorted(set(item) - PATCH_CHANGE_KEYS)
        if unknown:
            raise SystemExit(f"changes[{index}] 含未知字段: {unknown}")
        candidate_id = str(item.get("candidate_id", "")).strip()
        if not candidate_id:
            raise SystemExit(f"changes[{index}] 缺少 candidate_id")
        if candidate_id in seen_ids:
            raise SystemExit(f"{candidate_id}: 在对话修订补丁中重复出现")
        seen_ids.add(candidate_id)
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise SystemExit(f"未知候选 ID {candidate_id}")
        operation = str(candidate.get("operation", ""))
        if operation not in RULE_OPERATIONS:
            raise SystemExit(
                f"{candidate_id}: 操作 {operation!r} 不能在对话修订补丁中修改"
            )

        target_file = str(candidate.get("target_file", ""))
        spec = common.spec_for(str(candidate.get("engine", "")), target_file)
        if spec is None:
            raise SystemExit(
                f"{candidate_id}: 目标文件未命中 RULE_FILES 白名单: {target_file!r}"
            )
        target = repo / target_file
        if not target.exists():
            raise SystemExit(f"{candidate_id}: 目标文件不存在: {target}")
        if target_file not in header_cache:
            header_cache[target_file] = read_header(target)
        header = header_cache[target_file]

        changed: dict[str, Any] = {}
        old_match_key = candidate.get("match_key") or {}
        new_match_key: dict[str, str] | None = None
        if "match_key" in item:
            if operation == "append":
                raise SystemExit(f"{candidate_id}: append 不需要 match_key")
            parsed_key = normalize_match_key(item["match_key"])
            if parsed_key != {str(key): str(value) for key, value in old_match_key.items()}:
                if "row" in item:
                    raise SystemExit(
                        f"{candidate_id}: 修改 match_key 时不能同时修改 row；请分两次修订"
                    )
                find_current_row(target, parsed_key, row_cache)
                new_match_key = parsed_key
                changed["match_key"] = parsed_key
        match_key = new_match_key or {
            str(key): str(value) for key, value in old_match_key.items()
        }
        if operation in {"edit", "delete", "keyword_update"} and not match_key:
            raise SystemExit(f"{candidate_id}: {operation} 需要 match_key")

        new_row: dict[str, Any] | None = None
        if "row" in item:
            if operation == "delete":
                raise SystemExit(f"{candidate_id}: delete 不能修改规则列；请改用 edit")
            if operation == "keyword_update":
                patch_row = normalize_patch_row(item["row"], allow_lists=True)
                unknown_keys = sorted(set(patch_row) - KEYWORD_UPDATE_KEYS)
                if unknown_keys:
                    raise SystemExit(
                        f"{candidate_id}: keyword_update 只允许改 "
                        f"{sorted(KEYWORD_UPDATE_KEYS)}，收到 {unknown_keys}"
                    )
                if "keywords" in patch_row and len(patch_row) > 1:
                    raise SystemExit(
                        f"{candidate_id}: keywords 完整替换不能与 "
                        "add_keywords/remove_keywords 同时使用"
                    )
                current = find_current_row(target, match_key, row_cache)
                try:
                    final_keywords, _ = common.merge_initial_keywords(
                        current.get("keywords", ""), patch_row
                    )
                except ValueError as exc:
                    raise SystemExit(f"{candidate_id}: {exc}") from exc
                current_keywords = common.split_initial_keywords(
                    current.get("keywords", "")
                )
                final_list = common.split_initial_keywords(final_keywords)
                if final_list != current_keywords:
                    new_row = {"keywords": "|".join(final_list)}
            else:
                patch_row = normalize_patch_row(item["row"])
                unknown_columns = sorted(set(patch_row) - set(header))
                if unknown_columns:
                    raise SystemExit(
                        f"{candidate_id}: 以下列不存在于 {target_file}: {unknown_columns}"
                    )
                old_row = {
                    str(key): str(value)
                    for key, value in (candidate.get("row") or {}).items()
                }
                if operation == "append":
                    merged = {**old_row, **patch_row}
                    if merged != old_row:
                        new_row = merged
                elif operation == "edit":
                    current = find_current_row(target, match_key, row_cache)
                    merged = {**old_row, **patch_row}
                    unknown_columns = sorted(set(merged) - set(header))
                    if unknown_columns:
                        raise SystemExit(
                            f"{candidate_id}: candidates.json 的 row 含未知列 "
                            f"{unknown_columns}"
                        )
                    update = {
                        column: value
                        for column, value in merged.items()
                        if str(current.get(column, "")) != str(value)
                    }
                    if update and not same_update(candidate, update):
                        new_row = update
                else:
                    raise SystemExit(
                        f"{candidate_id}: 不支持的操作 {operation!r}"
                    )
            if new_row is not None:
                changed["row"] = new_row

        new_reason: str | None = None
        if "reason" in item:
            if not isinstance(item["reason"], str):
                raise SystemExit(f"{candidate_id}: reason 必须是字符串")
            reason = item["reason"].strip()
            if reason != str(candidate.get("reason", "")).strip():
                new_reason = reason
                changed["reason"] = reason
        new_rationale: str | None = None
        if "rationale" in item:
            if not isinstance(item["rationale"], str):
                raise SystemExit(f"{candidate_id}: rationale 必须是字符串")
            rationale = item["rationale"].strip()
            if rationale != str(candidate.get("rationale", "")).strip():
                new_rationale = rationale
                changed["rationale"] = rationale
        elif new_reason is not None and str(candidate.get("rationale", "")) == str(
            candidate.get("reason", "")
        ):
            new_rationale = new_reason
            changed["rationale"] = new_reason

        if "operation" in item:
            operation_value = str(item["operation"]).strip().lower()
            if operation_value not in DOWNGRADE_OPERATIONS:
                raise SystemExit(
                    f"{candidate_id}: 对话修订只能把候选降级为 suggestion；"
                    f"收到 {operation_value!r}"
                )
            if "row" in item or "match_key" in item:
                raise SystemExit(
                    f"{candidate_id}: 降级为 suggestion 时不能同时修改 row/match_key"
                )
            if operation_value != operation:
                changed["operation"] = operation_value

        if "source_trace" in item:
            trace_value = item["source_trace"]
            if not isinstance(trace_value, dict):
                raise SystemExit(f"{candidate_id}: source_trace 必须是对象")
            traced_file = str(trace_value.get("target_file", "")).strip()
            if traced_file and traced_file != target_file:
                raise SystemExit(
                    f"{candidate_id}: source_trace.target_file="
                    f"{traced_file!r} 与候选 target_file={target_file!r} 不一致"
                )
            if trace_value != (candidate.get("source_trace") or {}):
                changed["source_trace"] = trace_value

        if changed:
            changes.append(
                {
                    "candidate_id": candidate_id,
                    "file": patch_path.name,
                    "line": index,
                    "changed": changed,
                }
            )

    preview = build_revise_preview(
        patch_path,
        patch_hash,
        changes,
        force_confirm=(
            common.interaction_mode(review_dir) == "guided" and bool(changes)
        ),
    )
    if preview_json is not None:
        write_json(preview_json, preview)
        _print_revise_preview(preview, preview_json)

    if not changes:
        print("对话修订补丁与 candidates.json 没有差异。")
        return {"changed": 0, "candidates": []}

    print("\n" + "=" * 64)
    print("对话修订补丁")
    print("=" * 64)
    for item in changes:
        print(f"  {item['candidate_id']} ({item['file']}:{item['line']})")
        for key, value in item["changed"].items():
            print(f"      {key} = {json.dumps(value, ensure_ascii=False, default=str)}")
    print("=" * 64)
    print("变更分布: " + json.dumps(
        dict(Counter(
            key
            for item in changes
            for key in item["changed"]
        ).most_common()),
        ensure_ascii=False,
    ))

    if dry_run:
        print("\n--dry-run：未写入 candidates.json。")
        if preview["required"]:
            print("下一步：按 revise_preview.json 确认后，在对话框取得 I5 决策，再运行不带 --dry-run 的同一条命令。")
        return {
            "changed": len(changes),
            "candidates": [item["candidate_id"] for item in changes],
        }

    gate_errors = revise_gate_errors(review_dir, preview, decisions_path)
    if gate_errors:
        raise SystemExit(
            "I5 修订确认未完成，拒绝写入 candidates.json：\n- "
            + "\n- ".join(gate_errors)
        )

    backup = candidates_path.with_suffix(".json.bak")
    backup.write_text(
        json.dumps(candidate_payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    now = datetime.now().isoformat(timespec="seconds")
    for item in changes:
        candidate = by_id[item["candidate_id"]]
        changed = item["changed"]
        if "row" in changed:
            candidate["row"] = changed["row"]
        if "match_key" in changed:
            candidate["match_key"] = changed["match_key"]
        if "reason" in changed:
            candidate["reason"] = changed["reason"]
        if "rationale" in changed:
            candidate["rationale"] = changed["rationale"]
        if "operation" in changed:
            candidate["operation"] = changed["operation"]
        if "source_trace" in changed:
            candidate["source_trace"] = changed["source_trace"]
        if any(
            key in changed
            for key in ("row", "match_key", "operation", "source_trace")
        ):
            # Rule content or approval evidence changed after review: force a
            # fresh validation and a fresh stage-five approval.
            candidate["status"] = "pending"
            candidate["validation"] = {}
            candidate["risk_acknowledged"] = False
        candidate["revised_via_dialog_at"] = now
        candidate["revised_by"] = "dialog"

    candidate_payload["revised_at"] = now
    dump_json(candidates_path, candidate_payload)
    print(f"\ncandidates.json -> {candidates_path}")
    print(f"备份 -> {backup}")
    print("提示：请运行 bscat.py check 重新校验并渲染审阅 CSV。")
    return {
        "changed": len(changes),
        "candidates": [item["candidate_id"] for item in changes],
    }


def revise(
    repo: Path,
    review_dir: Path,
    *,
    dry_run: bool = False,
    preview_json: Path | None = None,
    decisions_path: Path | None = None,
) -> dict[str, Any]:
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        raise SystemExit(f"找不到 {candidates_path}")
    payload = load_json(candidates_path)
    candidates = payload.get("candidates") or []
    by_id = {str(candidate.get("candidate_id", "")): candidate for candidate in candidates}

    candidate_dir = review_dir / "candidates"
    files = sorted(candidate_dir.glob("*_candidates.csv"))
    if not files:
        raise SystemExit(f"{candidate_dir} 下没有 *_candidates.csv；请先运行 bscat.py check 渲染。")

    row_cache: dict[tuple[str, str], dict[str, str]] = {}
    changes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for path in files:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            if "candidate_id" not in header:
                raise SystemExit(f"{path.name} 缺少 candidate_id 列")
            rule_columns = rule_header(header)
            for line, csv_row in enumerate(reader, start=2):
                candidate_id = str(csv_row.get("candidate_id", "")).strip()
                if not candidate_id:
                    continue
                if candidate_id in seen_ids:
                    raise SystemExit(f"{candidate_id}: 在多个审阅 CSV 中重复出现")
                seen_ids.add(candidate_id)
                candidate = by_id.get(candidate_id)
                if candidate is None:
                    raise SystemExit(f"{path.name}:{line}: 未知候选 ID {candidate_id}")
                operation = str(candidate.get("operation", ""))
                if operation not in RULE_OPERATIONS:
                    raise SystemExit(
                        f"{candidate_id}: 操作 {operation!r} 不能从规则 CSV 反向修订"
                    )

                changed: dict[str, Any] = {}
                target = repo / str(candidate.get("target_file", ""))
                match_key = candidate.get("match_key") or {}
                current: dict[str, str] | None = None

                csv_status = str(csv_row.get("status", "")).strip().lower()
                if csv_status and csv_status not in ALLOWED_STATUSES:
                    raise SystemExit(f"{candidate_id}: 非法 status {csv_status!r}")
                if csv_status and csv_status != candidate.get("status", "pending"):
                    raise SystemExit(
                        f"{candidate_id}: 审阅 CSV 不能修改审批状态；"
                        "请在对话中确认后运行 approvals.py --build"
                    )

                csv_reason = str(csv_row.get("reason", "")).strip()
                if csv_reason and csv_reason != str(candidate.get("reason", "")).strip():
                    changed["reason"] = csv_reason

                retargeted = False
                if operation in {"edit", "delete", "keyword_update"}:
                    original_key = dict(match_key)
                    csv_key = str(csv_row.get("match_key", "")).strip()
                    if csv_key:
                        try:
                            parsed = json.loads(csv_key)
                        except json.JSONDecodeError as exc:
                            raise SystemExit(
                                f"{candidate_id}: match_key 不是合法 JSON: {exc}"
                            ) from exc
                        if not isinstance(parsed, dict) or not parsed:
                            raise SystemExit(
                                f"{candidate_id}: match_key 必须是对象且不能为空"
                            )
                        if parsed != match_key:
                            # Retargeting and editing rule columns in the same
                            # pass is ambiguous: the CSV row still describes
                            # the old row.  Require a separate revision pass.
                            old_current = find_current_row(target, original_key, row_cache)
                            modified = [
                                column for column in rule_columns
                                if csv_row.get(column, "") != old_current.get(column, "")
                            ]
                            if modified:
                                raise SystemExit(
                                    f"{candidate_id}: 修改 match_key 时不能同时修改规则列 "
                                    f"{modified}；请分两次修订"
                                )
                            match_key = parsed
                            changed["match_key"] = parsed
                            retargeted = True
                    if not match_key:
                        raise SystemExit(f"{candidate_id}: {operation} 需要 match_key")
                    current = find_current_row(target, match_key, row_cache)

                if operation == "append":
                    update = diff_append(candidate, csv_row, rule_columns)
                    if update is not None:
                        changed["row"] = update
                elif operation == "edit":
                    update = None if retargeted else diff_edit(
                        candidate, csv_row, rule_columns, current or {}
                    )
                    if update is not None and same_update(candidate, update):
                        update = None
                    if update is not None:
                        changed["row"] = update
                elif operation == "delete":
                    changed_rule_columns = [
                        column for column in rule_columns
                        if csv_row.get(column, "") != (current or {}).get(column, "")
                    ]
                    if changed_rule_columns:
                        raise SystemExit(
                            f"{candidate_id}: delete 不能修改规则列 "
                            f"{changed_rule_columns}；请改用 edit"
                        )
                elif operation == "keyword_update":
                    result = None if retargeted else diff_keyword_update(
                        candidate, csv_row, rule_columns, current or {}
                    )
                    if result is not None:
                        update, final_keywords = result
                        if same_update(candidate, update) or same_final_keywords(
                            candidate, final_keywords
                        ):
                            update = None
                        if update is not None:
                            changed["row"] = update

                if changed:
                    changes.append(
                        {
                            "candidate_id": candidate_id,
                            "file": path.name,
                            "line": line,
                            "changed": changed,
                        }
                    )

    if not changes:
        print("审阅 CSV 与 candidates.json 没有差异。")
        return {"changed": 0, "candidates": []}

    print("\n" + "=" * 64)
    print("候选 CSV 合并（兼容模式）")
    print("=" * 64)
    for item in changes:
        print(f"  {item['candidate_id']} ({item['file']}:{item['line']})")
        for key, value in item["changed"].items():
            print(f"      {key} = {json.dumps(value, ensure_ascii=False, default=str)}")
    print("=" * 64)
    print("变更分布: " + json.dumps(
        dict(Counter(
            key
            for item in changes
            for key in item["changed"]
        ).most_common()),
        ensure_ascii=False,
    ))

    # Legacy CSV edits go through the same I5 preview as dialogue patches, so
    # changing match_key or touching several fields cannot bypass confirmation.
    patch_hash = common.sha256_json(
        [
            {
                "candidate_id": item["candidate_id"],
                "changed": item["changed"],
            }
            for item in changes
        ]
    )
    preview = build_revise_preview(
        review_dir / "candidates",
        patch_hash,
        changes,
        force_confirm=(
            common.interaction_mode(review_dir) == "guided" and bool(changes)
        ),
    )
    if preview_json is not None:
        write_json(preview_json, preview)
        _print_revise_preview(preview, preview_json)

    if dry_run:
        print("\n--dry-run：未写入 candidates.json。")
        if preview["required"]:
            print(
                "下一步：按 revise_preview.json 确认后，在对话框取得 I5 决策，"
                "再运行不带 --dry-run 的同一条命令。"
            )
        return {"changed": len(changes), "candidates": [i["candidate_id"] for i in changes]}

    gate_errors = revise_gate_errors(review_dir, preview, decisions_path)
    if gate_errors:
        raise SystemExit(
            "I5 修订确认未完成，拒绝写入 candidates.json：\n- "
            + "\n- ".join(gate_errors)
        )

    backup = candidates_path.with_suffix(".json.bak")
    backup.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    now = datetime.now().isoformat(timespec="seconds")
    for item in changes:
        candidate = by_id[item["candidate_id"]]
        changed = item["changed"]
        if "row" in changed:
            candidate["row"] = changed["row"]
        if "match_key" in changed:
            candidate["match_key"] = changed["match_key"]
        if any(key in changed for key in ("row", "match_key")):
            # Same rule as the dialogue patch: content changes require a fresh
            # validation and a fresh approval.
            candidate["status"] = "pending"
            candidate["validation"] = {}
            candidate["risk_acknowledged"] = False
        if "reason" in changed:
            old_reason = str(candidate.get("reason", ""))
            candidate["reason"] = changed["reason"]
            if str(candidate.get("rationale", "")) == old_reason:
                candidate["rationale"] = changed["reason"]
        candidate["revised_via_csv_at"] = now

    payload["revised_at"] = now
    dump_json(candidates_path, payload)
    print(f"\ncandidates.json -> {candidates_path}")
    print(f"备份 -> {backup}")
    print("提示：请运行 bscat.py check 重新校验并渲染审阅 CSV。")
    return {"changed": len(changes), "candidates": [i["candidate_id"] for i in changes]}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把对话修订补丁（或兼容模式下的审阅 CSV）合并回 candidates.json。"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument(
        "--patch",
        help=(
            "对话修订补丁 JSON（相对路径按审核目录解析）。提供时只应用补丁，"
            "不读取审阅 CSV。"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--preview-json",
        default=None,
        help="I5 修订预览输出；默认 <review>/revise_preview.json。",
    )
    parser.add_argument(
        "--decisions",
        default=None,
        help="I5 决策文件；默认 <review>/dialog_decisions.json。",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="合并后立即运行 bscat.py check。",
    )
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    preview_json = (
        Path(args.preview_json).expanduser().resolve()
        if args.preview_json
        else review_dir / "revise_preview.json"
    )
    decisions_path = (
        Path(args.decisions).expanduser().resolve()
        if args.decisions else None
    )
    if args.patch:
        patch_path = Path(args.patch).expanduser()
        if not patch_path.is_absolute():
            # 默认相对审核目录；同时兼容用户从仓库根目录传入的
            # ``reviews/run-001/dialog_patch.json``。
            review_candidate = review_dir / patch_path
            cwd_candidate = patch_path.resolve()
            patch_path = (
                review_candidate if review_candidate.exists()
                else cwd_candidate if cwd_candidate.exists()
                else review_candidate
            )
        summary = revise_from_patch(
            repo,
            review_dir,
            patch_path.resolve(),
            dry_run=args.dry_run,
            preview_json=preview_json,
            decisions_path=decisions_path,
        )
    else:
        summary = revise(
            repo,
            review_dir,
            dry_run=args.dry_run,
            preview_json=preview_json,
            decisions_path=decisions_path,
        )
    if args.validate and not args.dry_run and summary["changed"]:
        command = [
            sys.executable,
            str(SCRIPT_DIR.parent / "validate.py"),
            "--repo", str(repo),
            "--review", str(review_dir),
        ]
        sys.stdout.flush()
        print("\n$ " + " ".join(command), flush=True)
        return subprocess.run(command).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
