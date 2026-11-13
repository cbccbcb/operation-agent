#!/usr/bin/env python3
"""Normalise an inconsistent-transaction workbook into the CSV shape the
AUS_BS_CAT_Model pipeline expects.

The user-facing workbook carries both the expected category and the currently
assigned category.  The pipeline overwrites ``bscat``, so we preserve the
provided current value under ``input_current_category`` and use ``category``
for the expected label (matching the repository's own sample.csv convention).

    python parse_input.py --input input/discrepancies.xlsx --out reviews/run-001

Outputs: samples.csv, parse_summary.json, samples_manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

import common

# ── column detection ────────────────────────────────────────────────────────

ALIASES: dict[str, list[str]] = {
    "application_id": [
        "application_id", "applicationid", "app_id", "application_no",
        "application_number", "申请编号", "申请id", "申请号",
    ],
    "transaction_id": [
        "transaction_id", "transactionid", "txn_id", "txn_no", "transaction_no",
        "transaction_number", "流水号", "交易编号", "交易id",
    ],
    "text": [
        "text", "description", "narrative", "transaction_description",
        "transaction_text", "交易描述", "摘要", "描述", "交易摘要", "用途",
    ],
    "expected_category": [
        "expected_category", "correct_category", "ground_truth", "label",
        "expected", "expected_bscat", "期望分类", "正确分类", "正确类别",
        "标注分类", "人工分类", "illion_category",
    ],
    "current_category": [
        "current_category", "current_bscat", "bscat", "model_category",
        "bscat_category", "predicted_category", "actual_category", "classification",
        "当前分类", "模型分类", "现有分类", "预测分类", "当前类别",
    ],
    "input_current_engine": [
        "classification_engine", "current_engine", "engine", "当前引擎", "分类引擎",
    ],
    "problem_type": [
        "problem_type", "issue_type", "problem", "error_type", "问题类型",
        "差异类型", "问题分类", "错误类型",
    ],
    "amount": ["amount", "transaction_amount", "amt", "金额", "交易金额"],
    "dr_cr": ["dr_cr", "drcr", "debit_credit", "借贷", "收支", "借贷标志"],
    "transaction_date": ["transaction_date", "txn_date", "date", "交易日期", "日期"],
    "bank_account_id": [
        "bank_account_id", "account_id", "account_no", "account_number",
        "账号", "账户", "银行账号",
    ],
    "account_type": ["account_type", "账户类型"],
    "bank": ["bank", "institution", "银行", "机构"],
    "credit_limit": ["credit_limit", "信用额度"],
    "third_party": ["third_party", "counterparty", "merchant", "交易对手", "商户"],
    "trx_type": ["trx_type", "transaction_type", "交易类型"],
    "balance": ["balance", "balance_after", "余额"],
}

CANONICAL_ORDER = [
    "application_id", "transaction_id", "bank_account_id", "account_type",
    "bank", "credit_limit", "transaction_date", "amount", "trx_type", "dr_cr",
    "balance", "text", "category", "problem_type", "problem_type_source",
    "input_current_category", "input_current_engine", "third_party",
]


def normalise_header(value: Any) -> str:
    text = str(value).strip().lower()
    text = re.sub(r"[^0-9a-z一-鿿]+", "_", text)
    return text.strip("_")


def find_column(columns: list[str], field: str) -> str | None:
    """Exact (normalised) alias match; never substring — 'category' and
    'category_expected' must not silently swap."""
    normalised = {normalise_header(col): col for col in columns}
    for alias in ALIASES.get(field, []):
        key = normalise_header(alias)
        if key in normalised:
            return normalised[key]
    return None


def find_column_candidates(columns: list[str], field: str) -> list[str]:
    """All exact alias matches for one field, in alias-priority order.

    ``find_column`` deliberately returns one column; I2 needs the full list so
    an ambiguous workbook (for example both ``bank`` and ``institution``) is
    visible before the pipeline treats one of them as authoritative.
    """
    normalised = {normalise_header(col): col for col in columns}
    found: list[str] = []
    for alias in ALIASES.get(field, []):
        key = normalise_header(alias)
        column = normalised.get(key)
        if column and column not in found:
            found.append(column)
    return found


def stringify_id(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalise_dr_cr(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().lower()
    if text in {"d", "dr", "debit", "借", "支出", "withdrawal", "out"}:
        return "debit"
    if text in {"c", "cr", "credit", "贷", "收入", "deposit", "in"}:
        return "credit"
    return text


def read_input(path: Path, sheet: str | None) -> tuple[pd.DataFrame, str, list[str]]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        workbook = pd.ExcelFile(path)
        names = list(workbook.sheet_names)
        chosen = sheet or ("transactions" if "transactions" in names else names[0])
        if chosen not in names:
            raise SystemExit(f"未找到工作表 {chosen!r}。可用工作表: {names}")
        frame = pd.read_excel(workbook, sheet_name=chosen)
        return frame, chosen, names
    if suffix == ".csv":
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp1252"):
            try:
                return pd.read_csv(path, encoding=encoding), "", ["csv"]
            except UnicodeDecodeError as exc:  # try the next encoding
                last_error = exc
        raise SystemExit(f"无法解码 CSV（{last_error}）。请提供 UTF-8 导出文件。")
    raise SystemExit(f"不支持的输入类型: {suffix}。请使用 .xlsx、.xlsm 或 .csv。")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把不一致交易明细规范化为 BSCAT 流水线可用的样本。"
    )
    parser.add_argument("--input", required=True, help="待分析的 Excel 或 CSV 文件。")
    parser.add_argument("--out", required=True, help="存放产物的审核目录。")
    parser.add_argument("--sheet", default=None, help="工作表名称（默认 transactions 或第一个）。")
    parser.add_argument(
        "--column-map",
        default=None,
        help='JSON 列映射覆盖，例如 \'{"expected_category": "我期望列", "current_category": "模型列"}\'。',
    )
    parser.add_argument("--app-id", default="SAMPLE", help="输入缺少 application_id 时使用的值。")
    parser.add_argument(
        "--dedupe-keys",
        choices=["fail", "suffix"],
        default="fail",
        help="(application_id, transaction_id) 重复时的处理方式（默认 fail）。",
    )
    parser.add_argument(
        "--problem-type",
        choices=["all", "miss", "extra", "diff"],
        default="all",
        help=(
            "本次要解决的问题类型：miss=漏识别，extra=多识别，"
            "diff=不一致，all=全部（默认）。"
        ),
    )
    parser.add_argument(
        "--blank-expected",
        choices=["unknown", "extra"],
        default="unknown",
        help=(
            "期望分类为空时如何解释：unknown=未标注（默认）；"
            "extra=该行本应保持未分类，用于多识别场景。"
        ),
    )
    parser.add_argument(
        "--drop-missing-expected",
        action="store_true",
        help="丢弃无法确定期望分类、且不属于多识别的行，而不是保留。",
    )
    return parser.parse_args()


def build_parse_review(
    summary: dict[str, Any],
    preview_counts: dict[str, int],
) -> dict[str, Any]:
    """Build the I2 blueprint; it only asks when parsing is genuinely risky."""
    triggers: list[str] = []
    warnings = [str(value) for value in summary.get("warnings") or []]
    informational: list[str] = []
    if summary.get("problem_scope") == "extra":
        extra_rows = int(summary.get("extra_rows", 0) or 0)
        missing_current = int(summary.get("missing_current", 0) or 0)
        if extra_rows:
            informational.append(
                f"{extra_rows} 行按多识别（extra）处理：期望保持未分类，"
                "但流水线可能给出了分类。"
            )
        if missing_current:
            informational.append(
                f"{missing_current} 行没有输入自带的当前分类；"
                "将以流水线实际输出作为当前分类。"
            )
    actionable = [warning for warning in warnings if warning not in informational]
    if actionable:
        triggers.append("parse_warnings")
    if (
        len(summary.get("sheet_names") or []) > 1
        and not summary.get("sheet_explicit")
    ):
        triggers.append("multiple_sheets")
    ambiguous = {
        field: candidates
        for field, candidates in (summary.get("column_candidates") or {}).items()
        if len(candidates) > 1
    }
    if ambiguous:
        triggers.append("column_ambiguity")
    if int(summary.get("unlabeled_expected_rows", 0) or 0) > 0:
        triggers.append("missing_expected")
    if not (summary.get("column_mapping") or {}).get("expected_category"):
        triggers.append("missing_expected_column")
    if int(summary.get("provided_mismatch_count", 0) or 0) > 0:
        triggers.append("label_conflict")
    if int(summary.get("rows_written", 0) or 0) == 0:
        triggers.append("zero_rows")
    scope = str(summary.get("problem_scope", "") or "all")
    other_counts: dict[str, int] = {}
    if scope in {"miss", "diff", "extra"}:
        other_counts = {
            key: int(value)
            for key, value in preview_counts.items()
            if key in {"miss", "diff", "extra"} and key != scope and int(value)
        }
        if other_counts:
            triggers.append("problem_type_mismatch")

    options = [
        {
            "key": "continue",
            "label": "解析正确，继续（推荐）",
            "description": "按当前 sheet、列映射和问题范围生成候选。",
        },
        {
            "key": "change_columns",
            "label": "改列映射",
            "description": "选择后用 Other 输入要覆盖的列名，例如 expected_category=正确列。",
        },
    ]
    if "multiple_sheets" in triggers:
        options.append({
            "key": "change_sheet",
            "label": "换工作表",
            "description": "选择后用 Other 输入 sheet 名称；随后重跑 prepare。",
        })
    if "problem_type_mismatch" in triggers:
        options.append({
            "key": "adjust_problem_type",
            "label": "调整问题类型",
            "description": (
                "选择后用 Other 输入 all/miss/diff/extra；"
                "随后用 --problem-type 重跑 prepare。"
            ),
        })
    options.append({
        "key": "stop",
        "label": "停止本轮",
        "description": "不生成候选，先修输入文件。",
    })

    questions: list[dict[str, Any]] = [
        {
            "header": "I2 解析确认",
            "prompt": (
                f"已写入 {int(summary.get('rows_written', 0) or 0)} 行；"
                f"sheet={summary.get('sheet') or '(CSV)'}；"
                f"问题范围={scope}。请确认解析结果。"
            ),
            "option_keys": [item["key"] for item in options],
            "options": options,
        }
    ]
    if "problem_type_mismatch" in triggers:
        questions.append({
            "header": "问题类型分布",
            "prompt": (
                "入口选择是 "
                f"{scope}，但预览里还有 {other_counts}。按哪一类处理？"
            ),
            "option_keys": ["follow_sample", "keep_intake", "one_type", "stop"],
            "options": [
                {"key": "follow_sample", "label": "按样本实际类型（推荐）",
                 "description": "重跑 prepare --problem-type all，三类都看。"},
                {"key": "keep_intake", "label": "保持入口选择",
                 "description": "只处理当前 problem_type。"},
                {"key": "one_type", "label": "只处理其中一类",
                 "description": "选择后用 Other 写 miss/diff/extra。"},
                {"key": "stop", "label": "停止本轮", "description": "先核对输入。"},
            ],
        })
    if (
        "missing_expected" in triggers
        or int(summary.get("provided_mismatch_count", 0) or 0) > 0
        or not (summary.get("column_mapping") or {}).get("expected_category")
    ):
        questions.append({
            "header": "期望分类权威",
            "prompt": "期望分类应作为权威标签，还是以当前分类为准？",
            "option_keys": ["sample", "current", "manual", "stop"],
            "options": [
                {"key": "sample", "label": "以样本期望为准（推荐）",
                 "description": "保持 expected_category 为权威。"},
                {"key": "current", "label": "以当前分类为准",
                 "description": "需要用 --column-map 交换期望/当前列并重跑。"},
                {"key": "manual", "label": "人工指定列映射",
                 "description": "选择后用 Other 写 column_map，并重跑 prepare。"},
                {"key": "stop", "label": "停止本轮", "description": "先核对标签。"},
            ],
        })

    fingerprint_material = {
        "input_sha256": summary.get("input_sha256"),
        "sheet": summary.get("sheet"),
        "column_mapping": summary.get("column_mapping"),
        "problem_scope": scope,
        "blank_expected_semantics": summary.get("blank_expected_semantics"),
        "rows_written": summary.get("rows_written"),
        "problem_type_preview_counts": preview_counts,
        "warnings": warnings,
        "column_candidates": summary.get("column_candidates"),
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "required": bool(triggers),
        "triggers": triggers,
        "warnings": actionable,
        "informational_warnings": informational,
        "ambiguous_columns": ambiguous,
        "summary": {
            "rows_written": summary.get("rows_written"),
            "sheet": summary.get("sheet"),
            "sheet_names": summary.get("sheet_names"),
            "problem_scope": scope,
            "problem_type_preview_counts": preview_counts,
            "provided_mismatch_count": summary.get("provided_mismatch_count"),
            "unlabeled_expected_rows": summary.get("unlabeled_expected_rows"),
        },
        "questions": questions,
        "fingerprint": common.sha256_json(fingerprint_material),
    }


def main() -> int:
    args = parse_args()
    # When the run is scoped to extra, a blank expected category means "should
    # stay unclassified" rather than "unlabeled".  Make that explicit here so
    # the preview counts and warnings agree with collect_evidence.py.
    if args.problem_type == "extra":
        args.blank_expected = "extra"
    input_path = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"输入文件不存在: {input_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    frame, sheet_name, sheet_names = read_input(input_path, args.sheet)
    columns = [str(col) for col in frame.columns]
    mapping: dict[str, str | None] = {}

    overrides = json.loads(args.column_map) if args.column_map else {}
    for field in ALIASES:
        if overrides.get(field):
            requested = overrides[field]
            if requested not in columns:
                raise SystemExit(f"--column-map 指定的列 {requested!r} 不存在。可用列: {columns}")
            mapping[field] = requested
        else:
            mapping[field] = find_column(columns, field)

    # category is the historical expected-label column.  Only use it when the
    # caller did not name a more specific expected column.
    if mapping.get("expected_category") is None and "category" in columns:
        mapping["expected_category"] = "category"
    # If current is still unknown and `category` exists but is not the expected
    # source, it is a plausible current-label column.
    if mapping.get("current_category") is None and "category" in columns:
        if mapping.get("expected_category") != "category":
            mapping["current_category"] = "category"

    expected_col = mapping.get("expected_category")
    current_col = mapping.get("current_category")
    text_col = mapping.get("text")
    allow_blank_expected = (
        args.blank_expected == "extra" or args.problem_type == "extra"
    )

    problems = []
    if not expected_col and not allow_blank_expected:
        problems.append("expected_category (期望分类/正确分类)")
    if not text_col:
        problems.append("text (交易描述)")
    if problems:
        raise SystemExit(
            "无法识别必需列: " + ", ".join(problems)
            + "。\n可用列: " + ", ".join(columns)
            + "\n请用 --column-map '{\"expected_category\": \"...\"}' 指定。"
            + (
                "\n如果本次只处理多识别（extra），可加 --problem-type extra "
                "或 --blank-expected extra，此时期望分类列可以为空。"
                if not expected_col else ""
            )
        )

    if expected_col and expected_col == current_col:
        raise SystemExit("expected_category 和 current_category 指向同一列。")

    total_rows = len(frame)
    output = pd.DataFrame(index=frame.index)

    # ── identifiers ─────────────────────────────────────────────────────────
    app_source = mapping.get("application_id")
    if app_source:
        raw_app = frame[app_source].map(stringify_id)
        generated_app = int(raw_app.eq("").sum())
        app_values = raw_app.mask(raw_app.eq(""), args.app_id)
    else:
        app_values = pd.Series(args.app_id, index=frame.index)
        generated_app = len(frame)
    output["application_id"] = app_values

    tx_source = mapping.get("transaction_id")
    tx_values = (
        frame[tx_source].map(stringify_id) if tx_source else pd.Series("", index=frame.index)
    )
    generated_tx = 0
    for position in range(len(tx_values)):
        if not tx_values.iat[position]:
            tx_values.iat[position] = f"T{position + 1:07d}"
            generated_tx += 1
    output["transaction_id"] = tx_values

    key_frame = output[["application_id", "transaction_id"]].astype("string").fillna("")
    duplicate_mask = key_frame.duplicated(keep=False)
    duplicate_count = int(duplicate_mask.sum())
    duplicate_pairs = int(key_frame[duplicate_mask].drop_duplicates().shape[0])
    if duplicate_count and args.dedupe_keys == "fail":
        bad = key_frame[duplicate_mask].drop_duplicates().head(10).agg(" / ".join, axis=1)
        raise SystemExit(
            f"重复的 (application_id, transaction_id) 组合: {duplicate_pairs} 组，"
            f"涉及 {duplicate_count} 行。\n"
            + "\n".join(f"  {row}" for row in bad)
            + "\n请提供唯一键，或使用 --dedupe-keys suffix 重新运行。"
        )
    if duplicate_count and args.dedupe_keys == "suffix":
        seen: dict[tuple[str, str], int] = {}
        for position, (app, tx) in enumerate(key_frame.itertuples(index=False, name=None)):
            key = (app, tx)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                output.iat[position, output.columns.get_loc("transaction_id")] = (
                    f"{tx}-{seen[key]}"
                )

    # ── text / labels ───────────────────────────────────────────────────────
    output["text"] = frame[text_col].fillna("").astype(str)
    raw_expected = (
        frame[expected_col].fillna("").astype(str).str.strip()
        if expected_col else pd.Series("", index=frame.index)
    )
    output["category"] = raw_expected.map(common.normalise_expected_category)
    # A non-empty sentinel such as "Unclassified" or "未分类" is an explicit
    # statement that the row should not be classified; carry it as an extra
    # hint instead of silently turning it into a blank label.
    sentinel_expected = raw_expected.map(
        lambda value: bool(value) and common.expected_is_unclassified(value)
    )
    output["input_current_category"] = (
        frame[current_col].fillna("").astype(str).str.strip()
        if current_col else pd.Series("", index=frame.index)
    )

    # ── problem type hint ──────────────────────────────────────────────────
    problem_source = mapping.get("problem_type")
    if problem_source:
        raw_problem = frame[problem_source].fillna("").astype(str).str.strip()
        normalised_problem = raw_problem.map(common.normalise_problem_type)
        unknown_problem = raw_problem.ne("") & normalised_problem.eq("")
        if unknown_problem.any():
            bad_values = sorted(set(raw_problem[unknown_problem]))[:10]
            raise SystemExit(
                "无法识别的问题类型: " + ", ".join(repr(value) for value in bad_values)
                + "。请使用 miss/extra/diff、漏识别/多识别/不一致，或留空由流水线推断。"
            )
        output["problem_type"] = normalised_problem
        output["problem_type_source"] = raw_problem.ne("").map(
            lambda value: "explicit" if value else ""
        )
    else:
        output["problem_type"] = ""
        output["problem_type_source"] = ""

    # Explicit types and sentinel expected labels must agree.
    explicit_extra = output["problem_type"].eq("extra")
    explicit_other = output["problem_type"].isin(["miss", "diff"])
    if (sentinel_expected & explicit_other).any():
        raise SystemExit(
            "以下行的期望分类是“未分类”哨兵值，但问题类型被标记为 miss/diff；"
            "请改为 extra 或留空。"
        )
    output.loc[sentinel_expected & output["problem_type"].eq(""), "problem_type"] = "extra"
    output.loc[sentinel_expected & output["problem_type_source"].eq(""), "problem_type_source"] = "sentinel"

    # In an extra-scoped run (or with --blank-expected extra), a genuinely
    # blank expected label means "this row should stay unclassified".
    if args.blank_expected == "extra":
        blank_untyped = output["category"].eq("") & output["problem_type"].eq("")
        output.loc[blank_untyped, "problem_type"] = "extra"
        output.loc[blank_untyped, "problem_type_source"] = "blank_expected"

    engine_source = mapping.get("input_current_engine")
    if engine_source:
        output["input_current_engine"] = frame[engine_source].fillna("").astype(str).str.strip()
    elif "input_current_engine" in frame.columns:
        output["input_current_engine"] = frame["input_current_engine"].fillna("").astype(str).str.strip()
    else:
        output["input_current_engine"] = ""

    # ── scalar fields the engines read ─────────────────────────────────────
    output["bank_account_id"] = (
        frame[mapping["bank_account_id"]].map(stringify_id)
        if mapping.get("bank_account_id") else ""
    )
    output["account_type"] = (
        frame[mapping["account_type"]].fillna("").astype(str)
        if mapping.get("account_type") else ""
    )
    output["bank"] = (
        frame[mapping["bank"]].fillna("").astype(str)
        if mapping.get("bank") else ""
    )
    output["credit_limit"] = (
        pd.to_numeric(frame[mapping["credit_limit"]], errors="coerce")
        if mapping.get("credit_limit") else float("nan")
    )
    output["amount"] = (
        pd.to_numeric(frame[mapping["amount"]], errors="coerce")
        if mapping.get("amount") else float("nan")
    )
    output["dr_cr"] = (
        frame[mapping["dr_cr"]].map(normalise_dr_cr)
        if mapping.get("dr_cr") else ""
    )
    output["trx_type"] = (
        frame[mapping["trx_type"]].fillna("").astype(str)
        if mapping.get("trx_type") else ""
    )
    output["balance"] = (
        pd.to_numeric(frame[mapping["balance"]], errors="coerce")
        if mapping.get("balance") else float("nan")
    )
    output["third_party"] = (
        frame[mapping["third_party"]].fillna("").astype(str)
        if mapping.get("third_party") else ""
    )

    if mapping.get("transaction_date"):
        raw_dates = frame[mapping["transaction_date"]]
        parsed = pd.to_datetime(raw_dates, errors="coerce")
        if parsed.notna().mean() >= 0.7:
            output["transaction_date"] = parsed.dt.strftime("%Y-%m-%d").fillna("")
        else:
            output["transaction_date"] = raw_dates.fillna("").astype(str)
    else:
        output["transaction_date"] = ""

    # ── carry unrelated columns through untouched ──────────────────────────
    used_sources = {
        source for source in mapping.values() if source
    } | {expected_col, current_col, text_col}
    extras: list[str] = []
    for column in columns:
        if column in used_sources or column in output.columns:
            continue
        if normalise_header(column) in {
            "bscat", "category", "classification_engine", "classification_status",
            "classification_rule_id", "classification_reason",
        }:
            continue
        output[column] = frame[column]
        extras.append(column)

    output = output[[*CANONICAL_ORDER, *extras]]

    missing_expected = int(output["category"].eq("").sum())
    missing_current = int(output["input_current_category"].eq("").sum())
    extra_rows = int(output["problem_type"].eq("extra").sum())
    unlabeled_expected = int(
        (output["category"].eq("") & output["problem_type"].ne("extra")).sum()
    )
    warnings: list[str] = []
    if unlabeled_expected:
        if args.drop_missing_expected:
            output = output[output["category"].ne("") | output["problem_type"].eq("extra")].copy()
            warnings.append(
                f"已丢弃 {unlabeled_expected} 行没有期望分类、且不属于多识别的数据。"
            )
            unlabeled_expected = 0
            missing_expected = int(output["category"].eq("").sum())
        else:
            warnings.append(
                f"{unlabeled_expected} 行没有期望分类，也没有标记为多识别；"
                "这些行会保留作为回归对照，但不会生成候选。"
            )
    if extra_rows:
        warnings.append(
            f"{extra_rows} 行按多识别（extra）处理：期望保持未分类，"
            "但流水线可能给出了分类。"
        )
    if missing_current:
        warnings.append(
            f"{missing_current} 行没有输入自带的当前分类；"
            "将以流水线实际输出作为当前分类。"
        )
    if output["text"].eq("").any():
        warnings.append(f"{int(output['text'].eq('').sum())} 行交易描述为空。")
    if output["amount"].isna().any():
        warnings.append(f"{int(output['amount'].isna().sum())} 行金额不是数值。")
    unknown_dr = sorted(set(output["dr_cr"]) - {"", "debit", "credit"})
    if unknown_dr:
        warnings.append(f"未识别的 dr_cr 值将原样保留: {unknown_dr[:10]}")

    provided_mismatch = int(
        (
            output["input_current_category"].ne("")
            & output["category"].ne("")
            & output["category"].ne(output["input_current_category"])
        ).sum()
    )
    preview_types = pd.Series(
        [
            common.infer_problem_type(
                expected, current, explicit, args.blank_expected
            )
            for expected, current, explicit in zip(
                output["category"],
                output["input_current_category"],
                output["problem_type"],
            )
        ],
        index=output.index,
    )

    samples_path = out_dir / "samples.csv"
    output.to_csv(samples_path, index=False, encoding="utf-8-sig")

    summary = {
        "input": str(input_path),
        "input_sha256": sha256(input_path),
        "sheet": sheet_name,
        "sheet_names": sheet_names,
        "sheet_explicit": bool(args.sheet),
        "total_rows_in_input": total_rows,
        "rows_written": int(len(output)),
        "column_mapping": mapping,
        "column_candidates": {
            field: find_column_candidates(columns, field)
            for field in ALIASES
        },
        "available_columns": columns,
        "extra_columns_carried": extras,
        "categories": {
            str(key): int(value)
            for key, value in output["category"].value_counts().head(50).items()
        },
        "provided_mismatch_count": provided_mismatch,
        "problem_scope": args.problem_type,
        "blank_expected_semantics": args.blank_expected,
        "problem_type_hint_counts": {
            str(key): int(value)
            for key, value in output["problem_type"].value_counts().items()
            if str(key)
        },
        "problem_type_preview_counts": {
            str(key): int(value)
            for key, value in preview_types.value_counts().items()
        },
        "extra_rows": extra_rows,
        "unlabeled_expected_rows": unlabeled_expected,
        "missing_expected": missing_expected,
        "missing_current": missing_current,
        "duplicate_key_rows": duplicate_count,
        "duplicate_key_pairs": duplicate_pairs,
        "generated_application_ids": generated_app,
        "generated_transaction_ids": generated_tx,
        "warnings": warnings,
        "outputs": {
            "samples_csv": str(samples_path),
            "manifest": str(out_dir / "samples_manifest.json"),
        },
    }
    preview_counts = {
        str(key): int(value) for key, value in preview_types.value_counts().items()
    }
    parse_review = build_parse_review(summary, preview_counts)
    (out_dir / "parse_review.json").write_text(
        json.dumps(parse_review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "parse_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "samples_manifest.json").write_text(
        json.dumps(
            {
                "samples_csv": str(samples_path),
                "samples_sha256": sha256(samples_path),
                "input_sha256": summary["input_sha256"],
                "columns": list(output.columns),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"写入行数: {len(output):,} | 输入自带的期望/当前不一致: {provided_mismatch:,}"
        f" | 问题范围: {args.problem_type}"
    )
    print(
        "问题类型预览（以输入当前分类为准，最终以流水线输出为准）: "
        + json.dumps(preview_counts, ensure_ascii=False)
    )
    print(f"列映射: {json.dumps(mapping, ensure_ascii=False)}")
    for warning in warnings:
        print(f"警告: {warning}")
    print(f"samples.csv -> {samples_path}")
    print(f"parse_summary.json -> {out_dir / 'parse_summary.json'}")
    print(
        f"parse_review.json -> {out_dir / 'parse_review.json'} "
        f"(I2 {'需要确认: ' + ', '.join(parse_review['triggers']) if parse_review['required'] else '可跳过'})"
    )
    if parse_review["required"]:
        print(
            "下一步：先按 parse_review.json 完成 I2 解析确认并写 "
            "dialog_decisions.json#parse，再生成候选。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
