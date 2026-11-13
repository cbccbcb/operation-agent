#!/usr/bin/env python3
"""Merge the conversation approval's ``approvals.json`` into ``candidates.json``.

Only explicit ``confirmed`` / ``rejected`` entries are accepted.  A candidate
whose validation failed can never be confirmed through this path.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

ALLOWED_STATUSES = {"confirmed", "rejected"}
MATCH_REVIEW_SELECTIONS = {
    "recommended", "narrower", "wider", "adjust", "custom", "reject",
}


def approval_gate_errors(
    candidate: dict[str, Any], review_dir: Path
) -> list[str]:
    """Block confirmation when the config-table trace is missing or stale."""
    errors: list[str] = []
    trace_error = common.candidate_trace_error(candidate)
    if trace_error:
        errors.append(trace_error)
    errors.extend(attribution_match_errors(candidate, review_dir))
    return errors


def review_gate_errors(review_dir: Path) -> list[str]:
    """I2/I3 gates that apply to the whole review before stage-five approval."""
    errors = common.parse_gate_errors(review_dir)
    errors += common.attribution_gate_errors(review_dir)
    return errors


def attribution_match_errors(
    candidate: dict[str, Any], review_dir: Path
) -> list[str]:
    """Ensure a confirmed rule candidate still matches its I3 target table."""
    if str(candidate.get("operation", "")).strip() not in common.RULE_OPERATIONS:
        return []
    review = common.load_json_if_exists(review_dir / "attribution_review.json")
    if not review or not review.get("required"):
        return []
    section = common.dialog_section(review_dir, "attribution")
    decisions = {
        str(entry.get("cluster_id", "")).strip(): entry
        for entry in section.get("decisions") or []
        if isinstance(entry, dict)
    }
    cluster_id = str(
        (candidate.get("source_trace") or {}).get("cluster_id", "")
    ).strip()
    cluster = next(
        (
            item for item in review.get("clusters") or []
            if isinstance(item, dict)
            and str(item.get("cluster_id", "")).strip() == cluster_id
        ),
        None,
    )
    if cluster is None or not cluster.get("triggers"):
        return []
    decision = decisions.get(cluster_id)
    if decision is None:
        return [
            f"I3 缺少聚类 {cluster_id} 的目标表决策；"
            "请重新查看 attribution_review.json 并确认"
        ]
    selection = str(decision.get("selection", "")).strip()
    if selection == "stop":
        return [f"I3 已停止聚类 {cluster_id}；该候选不能确认"]
    if selection not in {"recommended", "alternative"}:
        return []
    recommended = cluster.get("recommended") or {}
    engine = str(
        decision.get("engine") or recommended.get("engine", "")
    ).strip()
    target_file = str(
        decision.get("target_file") or recommended.get("target_file", "")
    ).strip()
    actual_engine = str(candidate.get("engine", "")).strip()
    actual_target = str(candidate.get("target_file", "")).strip()
    if (engine and actual_engine != engine) or (
        target_file and actual_target != target_file
    ):
        return [
            f"I3 确认的目标表为 {engine}/{target_file}，"
            f"但候选写入 {actual_engine}/{actual_target}；请重新生成候选或更新 I3"
        ]
    return []


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_match_review_decisions(
    review_dir: Path, path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Read ``match_review_decisions.json``; missing file returns ``{}``.

    A present but malformed file is fatal.  Silently treating it as "no
    decisions" would let an I4 confirmation disappear between the popup and
    the approval.
    """
    decisions_path = (
        path if path is not None
        else review_dir / common.MATCH_REVIEW_DECISIONS_FILENAME
    )
    if not decisions_path.exists():
        if path is not None:
            raise SystemExit(f"找不到匹配逻辑决策文件: {decisions_path}")
        return {}
    payload = load_json(decisions_path)
    if not isinstance(payload, dict):
        raise SystemExit(f"{decisions_path} 必须是 JSON 对象")
    entries = payload.get("decisions")
    if not isinstance(entries, list):
        raise SystemExit(f"{decisions_path} 缺少 decisions 列表")

    decisions: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit(f"{decisions_path} 的 decisions 项必须是对象")
        candidate_id = str(entry.get("candidate_id", "")).strip()
        if not candidate_id:
            raise SystemExit(f"{decisions_path} 存在缺少 candidate_id 的决策项")
        if candidate_id in decisions:
            raise SystemExit(
                f"{decisions_path} 中 {candidate_id} 重复出现；请合并为一条决策"
            )
        selected = str(entry.get("selected", "")).strip()
        if selected not in MATCH_REVIEW_SELECTIONS:
            raise SystemExit(
                f"{candidate_id}: selected={selected!r} 不合法；"
                f"允许: {sorted(MATCH_REVIEW_SELECTIONS)}"
            )
        row_hash = str(entry.get("row_hash", "")).strip()
        if selected != "reject":
            if not row_hash:
                raise SystemExit(
                    f"{candidate_id}: selected={selected} 必须带 row_hash；"
                    "请在 revise + 重新校验后从 match_review.json 读取 row_hash_after"
                )
            if not row_hash.startswith("sha256:"):
                raise SystemExit(f"{candidate_id}: row_hash 必须以 sha256: 开头")
        decisions[candidate_id] = dict(entry)
    return decisions


def match_review_context(
    review_dir: Path, match_review_path: Path | None = None
) -> dict[str, Any]:
    """Decide whether I4 is required and load its decisions once per run."""
    meta_path = review_dir / common.MATCH_REVIEW_FILENAME
    meta: dict[str, Any] = {}
    if meta_path.exists():
        loaded = load_json(meta_path)
        if not isinstance(loaded, dict):
            raise SystemExit(f"{meta_path} 必须是 JSON 对象")
        meta = loaded
    decisions = load_match_review_decisions(review_dir, match_review_path)
    intake = common.load_intake(review_dir)
    interaction_mode = str(
        intake.get("interaction_mode")
        or meta.get("interaction_mode")
        or "standard"
    )
    enforce = meta_path.exists() or bool(decisions)
    if interaction_mode == "minimal" or meta.get("required") is False:
        enforce = False
    return {
        "enforce": enforce,
        "interaction_mode": interaction_mode,
        "decisions": decisions,
        "match_review_path": str(meta_path) if meta_path.exists() else "",
        "decisions_path": (
            str(match_review_path)
            if match_review_path is not None
            else str(review_dir / common.MATCH_REVIEW_DECISIONS_FILENAME)
        ),
    }


def match_review_gate_errors(
    candidate: dict[str, Any], context: dict[str, Any]
) -> list[str]:
    """Block confirmation when I4 is required but missing or stale."""
    if not context.get("enforce"):
        return []
    if str(candidate.get("operation", "")).strip() not in common.RULE_OPERATIONS:
        return []
    candidate_id = str(candidate.get("candidate_id", "")).strip()
    decisions = context.get("decisions") or {}
    decision = decisions.get(candidate_id)
    if decision is None:
        return [
            "缺少匹配逻辑确认（I4）；请先完成 match_review_decisions.json"
        ]
    selected = str(decision.get("selected", "")).strip()
    if selected == "reject":
        return ["I4 已拒绝该匹配逻辑，不能确认"]
    expected_hash = str(decision.get("row_hash", "")).strip()
    actual_hash = common.candidate_row_hash(candidate)
    if expected_hash != actual_hash:
        return [
            "匹配逻辑在 I4 确认后又被修改"
            f"（确认 {expected_hash or '(空)'}，当前 {actual_hash}）；"
            "请重新确认匹配逻辑"
        ]
    return []


def import_approvals(
    review_dir: Path,
    approvals_path: Path,
    match_review_path: Path | None = None,
) -> dict[str, int]:
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        raise SystemExit(f"找不到 {candidates_path}；请先生成并校验候选。")
    if not approvals_path.exists():
        raise SystemExit(f"找不到审批文件 {approvals_path}。")

    payload = load_json(candidates_path)
    approvals = load_json(approvals_path)
    match_context = match_review_context(review_dir, match_review_path)
    candidates = payload.get("candidates", [])
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}

    requested = approvals.get("candidates")
    if isinstance(requested, list) and any(
        isinstance(entry, dict)
        and str(entry.get("status", "")).strip().lower() == "confirmed"
        for entry in requested
    ):
        gate_errors = review_gate_errors(review_dir)
        gate_errors += common.precheck_gate_errors(review_dir)
        if gate_errors:
            raise SystemExit(
                "审批前置门禁未完成，拒绝导入审批：\n- "
                + "\n- ".join(gate_errors)
            )

    payload_run = str(payload.get("run_id", ""))
    approval_run = str(approvals.get("run_id", ""))
    if payload_run and approval_run and payload_run != approval_run:
        raise SystemExit(
            f"审批文件的 run_id ({approval_run}) 与候选 ({payload_run}) 不一致；"
            "请重新走最新一轮对话审批，避免套用过期的审批结果。"
        )

    entries = approvals.get("candidates")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("审批文件中没有 candidates 列表，或列表为空。")

    errors: list[str] = []
    unknown: list[str] = []
    counted = {"confirmed": 0, "rejected": 0}
    seen: set[str] = set()

    for entry in entries:
        candidate_id = str(entry.get("candidate_id", "")).strip()
        status = str(entry.get("status", "")).strip().lower()
        if not candidate_id:
            errors.append("存在缺少 candidate_id 的审批项")
            continue
        if candidate_id in seen:
            errors.append(f"{candidate_id}: 审批文件中重复出现")
            continue
        seen.add(candidate_id)
        candidate = by_id.get(candidate_id)
        if candidate is None:
            unknown.append(candidate_id)
            continue
        if status not in ALLOWED_STATUSES:
            errors.append(f"{candidate_id}: 非法状态 {status!r}（只允许 confirmed/rejected）")
            continue
        validation = candidate.get("validation") or {}
        if status == "confirmed" and validation.get("valid") is not True:
            errors.append(f"{candidate_id}: 尚未通过校验，不能确认")
            continue
        if status == "confirmed":
            gate_errors = approval_gate_errors(candidate, review_dir)
            gate_errors += match_review_gate_errors(candidate, match_context)
            if gate_errors:
                errors.append(f"{candidate_id}: " + "; ".join(gate_errors))
                continue
        if (
            status == "confirmed"
            and candidate.get("risk_level") == "high"
            and entry.get("risk_acknowledged") is not True
        ):
            errors.append(f"{candidate_id}: 高风险候选必须在对话中逐条确认（risk_acknowledged=true）")
            continue
        candidate["status"] = status
        candidate["review_note"] = str(entry.get("note", "") or "")
        candidate["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
        if status == "confirmed":
            candidate["match_logic_confirmed"] = (
                str(candidate.get("operation", "")) in common.RULE_OPERATIONS
                and candidate_id in match_context["decisions"]
            )
        else:
            candidate["match_logic_confirmed"] = False
        if candidate.get("risk_level") == "high" and status == "confirmed":
            candidate["risk_acknowledged"] = True
        counted[status] += 1

    if unknown:
        errors.append("未知候选 ID: " + ", ".join(sorted(unknown)))
    if errors:
        raise SystemExit("审批导入失败：\n- " + "\n- ".join(errors))

    payload["approvals_imported_at"] = datetime.now().isoformat(timespec="seconds")
    payload["approvals_source"] = str(approvals_path)
    dump_json(candidates_path, payload)

    pending = sum(1 for candidate in candidates if candidate.get("status") == "pending")
    print(
        f"审批结果已导入: confirmed={counted['confirmed']}, "
        f"rejected={counted['rejected']}, pending={pending}"
    )
    return {"confirmed": counted["confirmed"], "rejected": counted["rejected"], "pending": pending}


def build_approvals(
    review_dir: Path,
    confirmed_ids: list[str],
    rejected_ids: list[str],
    acknowledged_ids: list[str],
    out_path: Path,
    note: str = "",
    match_review_path: Path | None = None,
) -> dict[str, Any]:
    """Build ``approvals.json`` from explicit candidate-ID lists.

    High-risk candidates must also appear in ``acknowledged_ids``.  This does
    not replace the chat confirmation; it only removes the hand-written JSON
    step and keeps the ``risk_acknowledged`` rule in one place.
    """
    candidates_path = review_dir / "candidates.json"
    if not candidates_path.exists():
        raise SystemExit(f"找不到 {candidates_path}；请先生成并校验候选。")
    payload = load_json(candidates_path)
    match_context = match_review_context(review_dir, match_review_path)
    by_id = {
        str(candidate.get("candidate_id", "")): candidate
        for candidate in payload.get("candidates", [])
    }
    confirmed = [value for value in confirmed_ids if value]
    rejected = [value for value in rejected_ids if value]
    acknowledged = set(acknowledged_ids)
    if confirmed:
        gate_errors = review_gate_errors(review_dir)
        gate_errors += common.precheck_gate_errors(review_dir)
        if gate_errors:
            raise SystemExit(
                "审批前置门禁未完成，拒绝生成 approvals.json：\n- "
                + "\n- ".join(gate_errors)
            )
    overlap = sorted(set(confirmed) & set(rejected))
    if overlap:
        raise SystemExit(f"候选不能同时确认和拒绝: {overlap}")
    if not confirmed and not rejected:
        raise SystemExit("至少提供 --confirmed 或 --rejected")

    errors: list[str] = []
    entries: list[dict[str, Any]] = []
    for status, ids in (("confirmed", confirmed), ("rejected", rejected)):
        for candidate_id in ids:
            candidate = by_id.get(candidate_id)
            if candidate is None:
                errors.append(f"{candidate_id}: 未知候选 ID")
                continue
            validation = candidate.get("validation") or {}
            if status == "confirmed" and validation.get("valid") is not True:
                errors.append(f"{candidate_id}: 尚未通过校验，不能确认")
                continue
            if status == "confirmed":
                gate_errors = approval_gate_errors(candidate, review_dir)
                gate_errors += match_review_gate_errors(candidate, match_context)
                if gate_errors:
                    errors.append(f"{candidate_id}: " + "; ".join(gate_errors))
                    continue
            risk = str(candidate.get("risk_level", ""))
            if status == "confirmed" and risk == "high" and candidate_id not in acknowledged:
                errors.append(
                    f"{candidate_id}: 高风险候选必须列入 --risk-acknowledged"
                )
                continue
            entries.append(
                {
                    "candidate_id": candidate_id,
                    "status": status,
                    "risk_acknowledged": risk == "high" and status == "confirmed",
                    "match_logic_confirmed": (
                        status == "confirmed"
                        and str(candidate.get("operation", "")) in common.RULE_OPERATIONS
                        and candidate_id in match_context["decisions"]
                    ),
                    "note": note,
                }
            )
    if errors:
        raise SystemExit("审批构建失败：\n- " + "\n- ".join(errors))
    if not entries:
        raise SystemExit("没有可写入的审批项")

    unknown_ack = sorted(acknowledged - set(confirmed))
    if unknown_ack:
        print(f"提示：--risk-acknowledged 中未确认的候选已忽略: {unknown_ack}")

    approvals = {
        "run_id": str(payload.get("run_id", "")),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "dialogue",
        "candidates": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(approvals, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(out_path)
    print(
        f"approvals.json -> {out_path} "
        f"(confirmed={len(confirmed)}, rejected={len(rejected)}, "
        f"risk_acknowledged={len(acknowledged)})"
    )
    return approvals


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="构建或导入对话审批产出的 approvals.json。"
    )
    parser.add_argument("--review", required=True)
    parser.add_argument("--approvals", default=None, help="要导入的 approvals.json。")
    parser.add_argument(
        "--build",
        action="store_true",
        help="根据 --confirmed/--rejected/--risk-acknowledged 生成 approvals.json。",
    )
    parser.add_argument("--confirmed", default="", help="逗号分隔的候选 ID。")
    parser.add_argument("--rejected", default="", help="逗号分隔的候选 ID。")
    parser.add_argument(
        "--risk-acknowledged",
        default="",
        help="逗号分隔的高风险候选 ID；必须与 --confirmed 对应。",
    )
    parser.add_argument("--out", default=None, help="输出路径；默认 <review>/approvals.json。")
    parser.add_argument("--note", default="", help="写入每条审批项的备注。")
    parser.add_argument(
        "--match-review",
        default=None,
        help=(
            "匹配逻辑确认文件；默认 <review>/match_review_decisions.json。"
            "match_review.json 存在时，confirmed 的规则候选必须带最新 row_hash。"
        ),
    )
    args = parser.parse_args()

    review_dir = Path(args.review).expanduser().resolve()
    match_review_path = (
        Path(args.match_review).expanduser().resolve()
        if args.match_review else None
    )
    if args.build:
        split = lambda value: [part.strip() for part in str(value).split(",") if part.strip()]
        out_path = (
            Path(args.out).expanduser().resolve()
            if args.out else review_dir / "approvals.json"
        )
        build_approvals(
            review_dir,
            split(args.confirmed),
            split(args.rejected),
            split(args.risk_acknowledged),
            out_path,
            args.note,
            match_review_path,
        )
        return 0

    if not args.approvals:
        raise SystemExit("请提供 --approvals 导入，或使用 --build 生成。")
    import_approvals(
        review_dir,
        Path(args.approvals).expanduser().resolve(),
        match_review_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
