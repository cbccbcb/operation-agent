#!/usr/bin/env python3
"""Read-only diagnosis: can the discrepancy be fixed in configuration, or does
it point at engine code?

The skill never edits engine source.  This script inspects the evidence from
``collect_evidence.py``, the matching rules, and the engine's own source, then
answers one question per cluster:

* ``config_only`` -- a rule/config change can produce the expected outcome;
* ``config_plus_code_review`` -- configuration can work around it, but the
  engine's guard/precedence/mapping should be reviewed;
* ``code_change_suggested`` -- configuration cannot express the fix, or a rule
  that should fire under the documented semantics did not fire.

    python diagnose_code.py --repo D:/Projects/AUS_BS_CAT_Model \
        --review reviews/run-001

Reads:  <review>/evidence.json
Writes: code_findings.json, code_findings.md

This script only reads.  It never writes to the repository.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

# Known engine semantics that change what a configuration row can express.
# Each entry is intentionally conservative: it describes documented behaviour,
# not a defect.  A finding is only raised when the cluster actually touches it.
ENGINE_SEMANTICS: dict[str, dict[str, Any]] = {
    "income": {
        "files": ["income_engine/domain/classification.py"],
        "needles": ["pattern_group", "match_type"],
        "summary": "income 把每条 pattern 都当正则执行，match_type 不参与匹配；"
                   "只有特定 pattern_group 能直接产出 Wages/Centrelink。",
        "detail": "要产出期望分类，候选必须落在可产出分类的 pattern_group；"
                  "如果业务需要新的分组或新的产出分类，就不是配置表能解决的。",
    },
    "all_other_credit": {
        "files": ["all_other_credit_engine/engine.py"],
        "needles": ["rule_type", "keyword"],
        "summary": "all_other_credit 只执行 rule_type=keyword 的行；"
                   "regex 行会被静默忽略，required_terms 也不生效。",
        "detail": "候选必须使用 keyword 行，并且只匹配 credit 方向、"
                  "未被更早引擎声明的行。",
    },
    "fee": {
        "files": ["fee_engine/domain/classification.py"],
        "needles": ["unclassified_only", "zero_amount_reject"],
        "summary": "fee 忽略 match_type，所有行都按正则执行；"
                   "unclassified_only 和 zero_amount_reject 会清掉已匹配结果。",
        "detail": "如果命中后仍没有最终分类，先检查这两个后置过滤器。",
    },
    "liability": {
        "files": [
            "liability_engine/domain/counterparty.py",
            "liability_engine/domain/streams.py",
        ],
        "needles": ["product_type", "stream"],
        "summary": "liability 的最终分类由 product_type/bank/stream 映射推导，"
                   "规则行里的 category 不一定是最终 bscat；"
                   "credit_card_rules 的 match_type/min_prefix_len/exclude_pattern 是死字段。",
        "detail": "如果规则命中了但输出分类仍不符，问题可能在映射层，"
                  "需要代码评审而不是继续改规则行。",
    },
    "initial": {
        "files": ["initial_engine/domain/classification.py"],
        "needles": ["upper", "Financial Institutions"],
        "summary": "initial 的 keywords 会原样插入，并按清洗后的大写文本做整词匹配；"
                   "Financial Institutions 会被排除。",
        "detail": "全小写关键词、带标点的关键词、或属于 Financial Institutions 的商户"
                  "都不会按预期生效。",
    },
    "transfer": {
        "files": [
            "transfer_engine/domain/classification.py",
            "transfer_engine/domain/transfer_counterparty.py",
        ],
        "needles": ["normalize_text", "lower()", "match_type"],
        "summary": "transfer 的外部规则按小写文本做大小写敏感正则，"
                   "counterparty 规则只认 keyword（match_type 仅兼容字段）。",
        "detail": "候选 pattern 必须适配小写文本；PP* 之类前缀是字面量。",
    },
    "gambling": {
        "files": ["gambling_engine/engine.py"],
        "needles": ["source", "confidence"],
        "summary": "gambling 的 institution 层优先于 keyword 层，"
                   "并且会跳过 fee/dishonour 行和更长的 initial 关键词。",
        "detail": "如果候选规则命中却不生效，先检查是否被 institution 层或 initial 声明抢占。",
    },
    "rent": {
        "files": ["rent_engine/engine.py"],
        "needles": ["source", "confidence"],
        "summary": "rent 的 institution 层优先于 keyword 层，"
                   "并会跳过 fee/dishonour 行和更长的 initial 关键词。",
        "detail": "keyword 层可以重新声明已被更早引擎声明的行，但会受上述守卫影响。",
    },
    "catch_all": {
        "files": ["catch_all_engine/engine.py"],
        "needles": ["confidence", "match_type"],
        "summary": "catch_all 只处理没有任何更早声明的行，按 confidence 取胜。",
        "detail": "如果期望分类的行已被其他引擎声明，catch_all 规则不会生效。",
    },
    "dishonour": {
        "files": ["dishonour_engine/engine.py"],
        "needles": ["rule_type", "required_terms"],
        "summary": "dishonour 的 keyword 行是大小写不敏感的字面子串，"
                   "regex 行是 IGNORECASE 搜索。",
        "detail": "keyword 行不要写正则元字符；需要正则时把 rule_type 设为 regex。",
    },
}

GUARD_NEEDLES = {
    "liability": ["already_income_mask", "Wages", "Centrelink"],
    "rent_gambling": ["already_protected_mask", "rent", "gambling"],
    "gambling_commit": ["gambling_owned", "income", "liability"],
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def scan_source(
    repo: Path, relative_files: list[str], needles: list[str], limit: int = 3
) -> list[dict[str, Any]]:
    """First matching source lines, used as citations in the report."""
    hits: list[dict[str, Any]] = []
    for relative in relative_files:
        path = repo / relative
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            if not any(needle in line for needle in needles):
                continue
            hits.append(
                {
                    "file": relative,
                    "line": number,
                    "snippet": line.strip()[:180],
                }
            )
            if len(hits) >= limit:
                return hits
    return hits


def _finding(
    finding_id: str,
    cluster: dict[str, Any],
    *,
    severity: str,
    category: str,
    summary: str,
    detail: str,
    evidence: list[dict[str, Any]] | None = None,
    config_fixable: bool,
    recommended_action: str,
    suggested_code_change: str = "",
    engine: str | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "cluster_id": cluster["cluster_id"],
        "problem_type": cluster.get("problem_type", ""),
        "engine": engine or (
            cluster.get("routing", {}).get("owner_engine")
            or cluster.get("current_engine", "")
        ),
        "current_engine": cluster.get("current_engine", ""),
        "severity": severity,
        "category": category,
        "summary": summary,
        "detail": detail,
        "evidence": evidence or [],
        "config_fixable": config_fixable,
        "recommended_action": recommended_action,
        "suggested_code_change": suggested_code_change,
    }


def diagnose_cluster(
    repo: Path,
    cluster: dict[str, Any],
    counter: list[int],
) -> list[dict[str, Any]]:
    routing = cluster.get("routing", {})
    status = str(routing.get("status", ""))
    problem_type = str(cluster.get("problem_type", ""))
    owner = str(routing.get("owner_engine") or "")
    current_engine = str(cluster.get("current_engine") or "")
    findings: list[dict[str, Any]] = []

    def next_id() -> str:
        counter[0] += 1
        return f"K{counter[0]:04d}"

    # 1. Orchestrator/engine guard explicitly blocks the owner engine.
    if status == "blocked_guard":
        if owner == "liability":
            needles = GUARD_NEEDLES["liability"]
        elif owner in {"rent", "gambling"}:
            needles = GUARD_NEEDLES["rent_gambling"]
        else:
            needles = GUARD_NEEDLES["gambling_commit"]
        findings.append(
            _finding(
                next_id(),
                cluster,
                severity="review",
                category="orchestrator_guard",
                summary="该差异被 orchestrator 的跨引擎守卫拦截，单靠改规则表无法让归属引擎声明该行。",
                detail=str(routing.get("reason", "")),
                evidence=scan_source(
                    repo,
                    ["classification_core/orchestrator.py"],
                    needles,
                ),
                config_fixable=False,
                recommended_action="code_review",
                suggested_code_change=(
                    "先确认守卫是否仍符合业务规则；只有业务确认后才调整 "
                    "classification_core/orchestrator.py 中的候选过滤逻辑。本次不修改代码。"
                ),
            )
        )

    # 2. A later engine overwrites the owner.  Config can work around it, but
    #    the precedence itself is a code/config design decision.
    if status == "overwritten_by_later":
        findings.append(
            _finding(
                next_id(),
                cluster,
                severity="review",
                category="engine_precedence",
                summary="归属引擎比当前引擎更早执行，追加规则会被后面的引擎覆盖。",
                detail=str(routing.get("reason", "")),
                evidence=[],
                config_fixable=True,
                recommended_action="config_plus_code_review",
                suggested_code_change=(
                    "可先通过删除/收窄后执行引擎的规则来修复；"
                    "如果这类覆盖是系统性的，应评审 pipeline.json 的优先级设计。"
                ),
            )
        )

    # 3. A rule that should emit the expected category exists and matches, but
    #    the pipeline did not produce it.  This is the strongest code-bug
    #    signal; keep it as "review", not "confirmed bug".
    if problem_type in {"miss", "diff"} and status in {
        "feasible_add", "same_owner_wrong_category", "blocked_guard"
    }:
        owner_rules = cluster.get("matching_owner_rules") or []
        expected = cluster.get("expected_category", "")
        should_emit = [
            hit for hit in owner_rules
            if hit.get("category") == expected
        ]
        if should_emit and status != "blocked_guard":
            findings.append(
                _finding(
                    next_id(),
                    cluster,
                    severity="review",
                    category="suspected_engine_bug",
                    summary="配置里已有能产出期望分类的规则并命中该行，但流水线没有产出期望分类。",
                    detail=(
                        "这通常意味着归一化、匹配语义、后置过滤或提交层覆盖与文档不一致。"
                        "请对照下方源码证据逐项排查；不要先重复追加同义规则。"
                    ),
                    evidence=[
                        {
                            "file": hit.get("file"),
                            "line": hit.get("line"),
                            "snippet": f"rule_id={hit.get('rule_id')} "
                                       f"pattern={hit.get('pattern')} "
                                       f"category={hit.get('category')}",
                        }
                        for hit in should_emit[:3]
                    ],
                    config_fixable=False,
                    recommended_action="code_review",
                    suggested_code_change=(
                        "在对应引擎的 domain/classification.py 中加日志或单测，"
                        "复现该行从归一化到最终声明的完整路径；确认后再决定是否改代码。"
                    ),
                )
            )

    # 4. Extra: config can delete/narrow only when the responsible rule is
    #    identifiable.  Otherwise the false positive likely comes from a
    #    default/mapping path in code.
    if problem_type == "extra":
        rule_ids = cluster.get("current_rule_ids") or []
        matches = cluster.get("matching_current_rules") or []
        if rule_ids or matches:
            findings.append(
                _finding(
                    next_id(),
                    cluster,
                    severity="info",
                    category="extra_config_fix",
                    summary="多识别可定位到具体规则，配置层面可以删除或收窄该规则。",
                    detail="删除后必须重跑流水线，确认后续引擎没有再次误分类。",
                    evidence=[
                        {
                            "file": hit.get("file"),
                            "line": hit.get("line"),
                            "snippet": f"rule_id={hit.get('rule_id')} "
                                       f"pattern={hit.get('pattern')}",
                        }
                        for hit in matches[:3]
                    ],
                    config_fixable=True,
                    recommended_action="config_only",
                )
            )
        else:
            findings.append(
                _finding(
                    next_id(),
                    cluster,
                    severity="review",
                    category="unlocatable_false_positive",
                    summary="流水线给出了分类，但无法定位到具体规则行；可能来自引擎默认值或映射逻辑。",
                    detail="这种情况通常不是删除一行 CSV 能解决的，需要代码评审。",
                    evidence=scan_source(
                        repo,
                        [f"{current_engine}_engine/engine.py"] if current_engine else [],
                        ["default", "bscat", "product_type"],
                    ),
                    config_fixable=False,
                    recommended_action="code_review",
                    suggested_code_change=(
                        "在引擎的预测/提交路径中定位该行的分类来源，确认是默认值、映射表还是守卫逻辑；"
                        "本次不修改代码。"
                    ),
                )
            )

    # 5. Engine-specific semantic constraints.  Always informational; they tell
    #    the candidate designer what a config row can and cannot express.
    for engine in {owner, current_engine}:
        semantics = ENGINE_SEMANTICS.get(engine)
        if not semantics:
            continue
        # Only surface it when it plausibly matters: the cluster is in this
        # engine's responsibility, or the current engine emitted a category.
        findings.append(
            _finding(
                next_id(),
                cluster,
                severity="info",
                category="engine_semantics",
                summary=semantics["summary"],
                detail=semantics["detail"],
                evidence=scan_source(
                    repo, semantics["files"], semantics["needles"]
                ),
                config_fixable=True,
                # Informational context for candidate design; it does not mean
                # the cluster needs a code review.
                recommended_action="engine_semantics",
                engine=engine,
            )
        )

    # 6. Liability mapping: a rule can match yet the final category comes from
    #    product/stream mapping.  Call this out explicitly.
    if problem_type in {"diff", "extra"} and current_engine == "liability":
        findings.append(
            _finding(
                next_id(),
                cluster,
                severity="review",
                category="liability_mapping",
                summary="liability 的最终分类不是规则行的 category，而是 product_type/bank/stream 映射的结果。",
                detail=(
                    f"当前输出为 {cluster.get('current_category')!r}。"
                    "如果删除/编辑规则后分类仍未变化，问题在映射层。"
                ),
                evidence=scan_source(
                    repo,
                    ENGINE_SEMANTICS["liability"]["files"],
                    ENGINE_SEMANTICS["liability"]["needles"],
                ),
                config_fixable=True,
                recommended_action="config_plus_code_review",
                suggested_code_change=(
                    "确认 product_type/bank/stream 的映射优先级；"
                    "若需要新增映射，属于代码/配置结构变更，需单独评审。"
                ),
                engine="liability",
            )
        )

    return findings


def verdict(findings: list[dict[str, Any]]) -> str:
    """Only review-level findings change the verdict.

    ``info`` findings (engine semantics, locatable extra rules) are context for
    candidate design; they do not mean the cluster needs a code change.
    """
    substantive = [
        item for item in findings
        if item["severity"] in {"review", "likely_bug"}
    ]
    if any(not item["config_fixable"] for item in substantive):
        return "code_change_suggested"
    if substantive:
        return "config_plus_code_review"
    return "config_only"


def render_report(payload: dict[str, Any]) -> str:
    clusters = payload["clusters"]
    findings = payload["findings"]
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for item in findings:
        by_cluster.setdefault(item["cluster_id"], []).append(item)

    verdict_labels = {
        "config_only": "配置可修复",
        "config_plus_code_review": "配置可修复，但建议代码评审",
        "code_change_suggested": "需要代码评审/改动",
    }
    counts = {
        key: sum(1 for item in clusters if item["verdict"] == key)
        for key in verdict_labels
    }

    lines = [
        "# 代码引擎诊断报告（只读）",
        "",
        "> 本报告只回答“这是配置问题还是代码问题”，**不修改任何引擎代码**。",
        "",
        f"- 仓库: `{payload['repo']}`",
        f"- 聚类数: {len(clusters)}",
        f"- 配置可修复: {counts['config_only']}",
        f"- 配置可修复但建议代码评审: {counts['config_plus_code_review']}",
        f"- 需要代码评审/改动: {counts['code_change_suggested']}",
        "",
        "## 按聚类结论",
        "",
        "| 聚类 | 问题类型 | 当前引擎 | 归属引擎 | 结论 | 主要依据 |",
        "|---|---|---|---|---|---|",
    ]
    for cluster in clusters:
        cluster_findings = by_cluster.get(cluster["cluster_id"], [])
        main = next(
            (
                item for item in cluster_findings
                if item["severity"] in {"review", "likely_bug"}
            ),
            cluster_findings[0] if cluster_findings else None,
        )
        summary_text = (main["summary"] if main else "无特殊代码风险").replace("|", "\\|")
        lines.append(
            f"| {cluster['cluster_id']} | {cluster.get('problem_type_label', '')} | "
            f"{cluster.get('current_engine', '')} | "
            f"{cluster.get('routing', {}).get('owner_engine', '') or '-'} | "
            f"{verdict_labels.get(cluster['verdict'], cluster['verdict'])} | "
            f"{summary_text} |"
        )

    lines += [
        "",
        "## 详细发现",
        "",
    ]
    if not findings:
        lines.append("未发现代码层面的阻塞；按候选规则流程处理即可。")
    for item in findings:
        lines += [
            f"### {item['finding_id']} · {item['cluster_id']} · {item['category']}",
            "",
            f"- 严重度: `{item['severity']}`",
            f"- 引擎: `{item['engine'] or item['current_engine']}`",
            f"- 配置能否修复: {'能' if item['config_fixable'] else '不能（需要代码评审）'}",
            f"- 建议动作: `{item['recommended_action']}`",
            "",
            f"**结论**：{item['summary']}",
            "",
            f"**说明**：{item['detail']}",
        ]
        if item["evidence"]:
            lines += ["", "**源码/规则证据**："]
            for evidence in item["evidence"]:
                location = evidence.get("file") or ""
                if evidence.get("line"):
                    location += f":{evidence['line']}"
                lines.append(f"- `{location}` — `{evidence.get('snippet', '')}`")
        if item["suggested_code_change"]:
            lines += [
                "",
                f"**建议的代码方向（未执行）**：{item['suggested_code_change']}",
            ]
        lines.append("")

    lines += [
        "## 边界说明",
        "",
        "- 本报告是静态诊断，不替代写入后的真实流水线验证。",
        "- `suspected_engine_bug` 只表示“按当前配置和文档语义不应发生”，需要人工复现后再定性。",
        "- 任何引擎源码修改都必须由用户单独确认，绝不由本 skill 自动执行。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="只读诊断差异是否来自引擎代码。")
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--review", required=True, help="包含 evidence.json 的审核目录。")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    review_dir = Path(args.review).expanduser().resolve()
    evidence_path = review_dir / "evidence.json"
    if not evidence_path.exists():
        raise SystemExit(f"未找到 evidence.json: {evidence_path}。请先运行 collect_evidence.py。")

    evidence = load_json(evidence_path)
    clusters = evidence.get("clusters", [])
    if not clusters:
        raise SystemExit("evidence.json 中没有聚类；没有需要诊断的差异。")

    counter = [0]
    findings: list[dict[str, Any]] = []
    diagnoses = []
    for cluster in clusters:
        cluster_findings = diagnose_cluster(repo, cluster, counter)
        findings.extend(cluster_findings)
        diagnoses.append(
            {
                "cluster_id": cluster["cluster_id"],
                "problem_type": cluster.get("problem_type", ""),
                "problem_type_label": cluster.get("problem_type_label", ""),
                "current_engine": cluster.get("current_engine", ""),
                "owner_engine": cluster.get("routing", {}).get("owner_engine", ""),
                "expected_category": cluster.get("expected_category", ""),
                "current_category": cluster.get("current_category", ""),
                "verdict": verdict(cluster_findings),
                "findings": [item["finding_id"] for item in cluster_findings],
            }
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "review_dir": str(review_dir),
        "summary": {
            "clusters": len(clusters),
            "findings": len(findings),
            "by_verdict": {
                key: sum(1 for item in diagnoses if item["verdict"] == key)
                for key in ("config_only", "config_plus_code_review",
                            "code_change_suggested")
            },
            "by_severity": {
                key: sum(1 for item in findings if item["severity"] == key)
                for key in ("info", "review", "likely_bug")
            },
        },
        "clusters": diagnoses,
        "findings": findings,
    }
    (review_dir / "code_findings.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (review_dir / "code_findings.md").write_text(
        render_report(payload), encoding="utf-8"
    )

    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"\ncode_findings.json -> {review_dir / 'code_findings.json'}")
    print(f"code_findings.md   -> {review_dir / 'code_findings.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
