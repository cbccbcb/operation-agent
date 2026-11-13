#!/usr/bin/env python3
"""Print what every rule table stores, from RULE_FILES plus the real header.

Tracing a candidate to a config table needs two facts that are easy to get
wrong: the table's role (``common.table_role``) and its actual columns.  This
command exposes both at runtime, so the agent never has to guess from a file
name or trust a stale document.

    python catalog.py --repo D:\\Projects\\AUS_BS_CAT_Model
    python catalog.py --repo D:\\Projects\\AUS_BS_CAT_Model --engine liability
    python catalog.py --repo D:\\Projects\\AUS_BS_CAT_Model --json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402


def read_header(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle), [])


def table_entry(repo: Path, spec: common.RuleFileSpec, priority: int) -> dict[str, Any]:
    path = repo / spec.relative_path
    return {
        "engine": spec.engine,
        "priority": priority,
        "target_file": spec.relative_path,
        "exists": path.exists(),
        "columns": read_header(path),
        "pattern_column": spec.pattern_column,
        "match_mode": spec.match_mode,
        "category_column": spec.category_column or "",
        "rule_id_column": spec.rule_id_column or "",
        "implicit_category": spec.implicit_category or "",
        "row_mode_column": spec.row_mode_column or "",
        "appendable": spec.appendable,
        "is_matcher": spec.is_matcher,
        "note": common.table_role(spec),
    }


def render_text(tables: list[dict[str, Any]]) -> str:
    lines = [
        "配置表目录（来源：RULE_FILES + 实际表头）",
        "=" * 64,
    ]
    current = None
    for table in tables:
        if table["engine"] != current:
            current = table["engine"]
            lines += [
                "",
                f"## {current}（priority {table['priority']}）",
            ]
        write_mode = "可追加" if table["appendable"] else "只读"
        role = "分类规则" if table["is_matcher"] else "配置/辅助表"
        note = table["note"] or "（未登记说明）"
        columns = ", ".join(table["columns"]) or "（文件不存在或表头为空）"
        lines += [
            "",
            f"- {table['target_file']}",
            f"  作用: {note}",
            f"  列: {columns}",
            f"  匹配: {table['pattern_column']} / {table['match_mode']}"
            + (
                f" | 分类列 {table['category_column']}"
                if table["category_column"] else ""
            )
            + (
                f" | 行类型列 {table['row_mode_column']}"
                if table["row_mode_column"] else ""
            )
            + (
                f" | 隐含分类 {table['implicit_category']}"
                if table["implicit_category"] else ""
            ),
            f"  写入: {write_mode}（{role}）",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="列出规则表的作用、列与匹配方式。")
    parser.add_argument("--repo", required=True, help="AUS_BS_CAT_Model 仓库根目录。")
    parser.add_argument(
        "--engine",
        default="",
        help="只列指定引擎，可用逗号分隔，例如 liability,initial。",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON。")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise SystemExit(f"仓库目录不存在: {repo}")
    wanted = {
        part.strip() for part in str(args.engine).split(",") if part.strip()
    }
    priorities = common.engine_priorities(repo)
    tables = [
        table_entry(repo, spec, priorities.get(spec.engine, 9999))
        for spec in common.RULE_FILES
        if not wanted or spec.engine in wanted
    ]
    if not tables:
        raise SystemExit(f"没有匹配的规则表: {sorted(wanted)}")

    if args.json:
        payload = {"repo": str(repo), "tables": tables}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(tables), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
