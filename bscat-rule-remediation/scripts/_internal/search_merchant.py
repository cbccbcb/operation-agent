#!/usr/bin/env python3
"""Search merchant rules before creating an initial-engine candidate.

The initial table is large (merchant_kb.csv is ~74MB / ~874k rows), so this
script streams the file instead of loading it.  It supports two modes:

* default: search ``initial_engine/merchant_kb.csv`` by ``merchant_name`` and
  ``keywords``;
* ``--file``: search any rule table (gambling / rent institution rows, fee,
  catch_all, ...) by the merchant-ish columns it actually has.

``--fuzzy`` adds a ``difflib`` score for near-name matches.  A short prefilter
keeps the fuzzy pass from scoring every row.

    python search_merchant.py --repo D:/Projects/AUS_BS_CAT_Model --query COLES
    python search_merchant.py --repo ... --query "SPORTSBET" --fuzzy --limit 10
    python search_merchant.py --repo ... --query "REAL ESTATE" \
        --file rent_engine/resources/rent_rules.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DEFAULT_FILE = "initial_engine/merchant_kb.csv"
MERCHANT_COLUMNS = (
    "merchant_name", "counterparty", "rule_name",
    "keyword", "keywords", "pattern",
)


def iter_rows(path: Path) -> Iterable[tuple[int, dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line, row in enumerate(reader, start=2):
            yield line, {str(key): str(value or "") for key, value in row.items()}


def _row_text(row: dict[str, str]) -> str:
    # Keep this cheap: it runs once per row over ~874k merchant rows.  The
    # query is already upper-cased, so a raw uppercase join is enough for the
    # substring prefilter.  Fuzzy scoring cleans only the label.
    return " ".join(
        str(row.get(column, "")).upper()
        for column in MERCHANT_COLUMNS
        if row.get(column)
    )


def _label(row: dict[str, str]) -> str:
    for column in ("merchant_name", "counterparty", "rule_name", "rule_id"):
        value = row.get(column, "").strip()
        if value:
            return value
    return ""


def _matched_keywords(row: dict[str, str], query: str) -> list[str]:
    raw = row.get("keywords") or row.get("keyword") or ""
    if not raw:
        return []
    separator = "|" if "|" in raw else ";"
    return [
        part.strip()
        for part in raw.split(separator)
        if part.strip() and query in common.clean_text(part)
    ]


def _score(
    row: dict[str, str],
    query: str,
    query_tokens: list[str],
    fuzzy: bool,
) -> float:
    haystack = _row_text(row)
    if not haystack:
        return 0.0
    exact = bool(query and query in haystack)
    if not fuzzy:
        return 1.0 if exact else 0.0
    label = common.clean_text(_label(row))
    score = SequenceMatcher(None, query, label).ratio() if label else 0.0
    if exact:
        score = max(score, 0.90)
    if query_tokens:
        overlap = sum(1 for token in query_tokens if token in haystack)
        score += 0.05 * min(overlap, 3)
    return min(score, 0.99)


def search(
    repo: Path,
    query: str,
    *,
    fuzzy: bool = False,
    limit: int = 20,
    target_file: str = DEFAULT_FILE,
    min_score: float = 0.55,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` matching rows, best first."""
    path = (repo / target_file).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError as exc:
        raise SystemExit(f"--file 指向仓库之外: {target_file}") from exc
    if not path.exists():
        raise SystemExit(f"找不到规则表: {path}")

    cleaned_query = common.clean_text(query)
    if not cleaned_query:
        raise SystemExit("--query 不能为空")
    query_tokens = [token for token in cleaned_query.split() if len(token) >= 3]
    is_merchant_kb = target_file.endswith("merchant_kb.csv")

    hits: list[dict[str, Any]] = []
    scanned = 0
    for line, row in iter_rows(path):
        scanned += 1
        haystack = _row_text(row)
        if cleaned_query in haystack:
            pass
        elif fuzzy and query_tokens and any(token in haystack for token in query_tokens):
            pass
        else:
            continue
        score = _score(row, cleaned_query, query_tokens, fuzzy)
        if fuzzy and score < min_score:
            continue
        keywords = [part.strip() for part in (row.get("keywords") or "").split("|") if part.strip()]
        hits.append(
            {
                "line": line,
                "merchant": _label(row),
                "category": row.get("category") or row.get("product_type") or "",
                "score": round(score, 3),
                "keyword_count": len(keywords) if is_merchant_kb else None,
                "matched_keywords": _matched_keywords(row, cleaned_query),
                "keywords": keywords[:8],
                "source": row.get("source", ""),
                "file": target_file,
                "row": {
                    key: row.get(key, "")
                    for key in (
                        "merchant_name", "counterparty", "rule_name", "rule_id",
                        "category", "product_type", "source", "match_type",
                    )
                    if key in row
                },
            }
        )
        if len(hits) > max(limit * 50, 500):
            hits.sort(key=lambda item: (-item["score"], item["merchant"]))
            del hits[limit * 10:]

    hits.sort(key=lambda item: (-item["score"], item["merchant"]))
    return hits[:limit]


def print_hits(hits: list[dict[str, Any]], query: str) -> None:
    print(f"\n检索: {query!r} | 命中 {len(hits)} 条")
    if not hits:
        print("没有找到匹配的商户/规则。")
        return
    print("")
    print("| # | 行 | 商户/规则 | 分类 | 分数 | 关键词数 | 命中的关键词 |")
    print("|---|---|---|---|---|---|---|")
    for index, hit in enumerate(hits, start=1):
        keywords = ", ".join(hit["matched_keywords"][:4]) or "-"
        print(
            f"| {index} | {hit['line']} | {hit['merchant']} | "
            f"{hit['category'] or '-'} | {hit['score']} | "
            f"{hit['keyword_count'] if hit['keyword_count'] is not None else '-'} | "
            f"{keywords} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检索 merchant_kb 或任意规则表中的商户/关键词。"
    )
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument("--query", required=True, help="商户名、文本片段或关键词。")
    parser.add_argument(
        "--file",
        default=DEFAULT_FILE,
        help=f"要检索的规则表（相对仓库）；默认 {DEFAULT_FILE}。",
    )
    parser.add_argument("--fuzzy", action="store_true", help="启用 difflib 模糊匹配。")
    parser.add_argument("--min-score", type=float, default=0.55)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="输出 JSON。")
    args = parser.parse_args()

    hits = search(
        Path(args.repo).expanduser().resolve(),
        args.query,
        fuzzy=args.fuzzy,
        limit=args.limit,
        target_file=args.file,
        min_score=args.min_score,
    )
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
    else:
        print_hits(hits, args.query)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
