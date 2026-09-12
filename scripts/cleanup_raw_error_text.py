"""就地清理历史失败文本中的供应商原始响应片段。

默认仅预览（dry-run），加 `--apply` 才写回。只替换文本内容，不删除任何记录。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\cleanup_raw_error_text.py
    .\\.venv\\Scripts\\python.exe scripts\\cleanup_raw_error_text.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 直接以脚本路径运行时，保证项目根目录可导入 app 包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.services.model_errors import sanitize_failure_text  # noqa: E402
from app.storage.database import engine  # noqa: E402


# (表, 列, 附加 WHERE 条件)；失败原因可能落在界面可见的任意一处。
TEXT_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("collection_runs", "error_message", ""),
    ("chat_agent_runs", "summary", ""),
    ("chat_agent_runs", "error_message", ""),
    ("chat_messages", "content", "role = 'assistant'"),
    ("chat_agent_events", "detail", ""),
    ("notifications", "title", ""),
    ("notifications", "detail", ""),
)
SAMPLE_LIMIT = 3
BEFORE_PREVIEW_CHARS = 80

def _clean_json(value: object) -> object:
    """递归清理 JSONB 审计里的字符串，保留结构与数值类型。"""
    if isinstance(value, str):
        return sanitize_failure_text(value)
    if isinstance(value, dict):
        return {key: _clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_json(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="清理历史失败文本中的供应商原始响应")
    parser.add_argument("--apply", action="store_true", help="写回数据库；缺省仅预览")
    parser.add_argument("--samples", type=int, default=SAMPLE_LIMIT, help="预览时最多打印多少条前后对照")
    args = parser.parse_args()

    pending: list[tuple[str, str, str, str, str]] = []
    with engine.begin() as connection:
        for table, column, condition in TEXT_TARGETS:
            where = f" WHERE {condition}" if condition else ""
            rows = connection.execute(
                text(f"SELECT id, {column} AS value FROM {table}{where}")
            ).mappings()
            for row in rows:
                original = row["value"]
                if not isinstance(original, str):
                    continue
                cleaned = sanitize_failure_text(original)
                if cleaned != original.strip():
                    pending.append((table, column, row["id"], original, cleaned))

        rows = connection.execute(text("SELECT id, metadata_json FROM chat_agent_events")).mappings()
        for row in rows:
            original = row["metadata_json"]
            cleaned = _clean_json(original)
            if cleaned != original:
                pending.append(
                    (
                        "chat_agent_events",
                        "metadata_json",
                        row["id"],
                        json.dumps(original, ensure_ascii=False),
                        json.dumps(cleaned, ensure_ascii=False),
                    )
                )

        print(f"待清理记录数：{len(pending)}")
        counts: dict[str, int] = {}
        for index, (table, column, row_id, before, after) in enumerate(pending):
            counts[f"{table}.{column}"] = counts.get(f"{table}.{column}", 0) + 1
            if index < args.samples:
                preview = before[:BEFORE_PREVIEW_CHARS] + ("…" if len(before) > BEFORE_PREVIEW_CHARS else "")
                print(f"  - {table}.{column} id={row_id}\n    旧：{preview}\n    新：{after}")
        for target, count in sorted(counts.items()):
            print(f"  {target}: {count}")

        if not args.apply:
            print("预览模式：未写入。确认后加 --apply 重跑。")
            return 0

        for table, column, row_id, _before, after in pending:
            if column == "metadata_json":
                connection.execute(
                    text(f"UPDATE {table} SET {column} = CAST(:value AS jsonb) WHERE id = :id"),
                    {"value": after, "id": row_id},
                )
            else:
                connection.execute(
                    text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
                    {"value": after, "id": row_id},
                )

    print(f"已写回 {len(pending)} 条记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
