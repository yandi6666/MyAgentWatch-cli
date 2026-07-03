"""Local inbox/cache storage for myagentwatch-cli daemon."""

from __future__ import annotations

import json
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
INBOX_PATH = DATA_DIR / "inbox.jsonl"
CHAT_CACHE_PATH = DATA_DIR / "chat_cache.jsonl"


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _write_jsonl(path: Path, rows: list[dict]):
    ensure_data_dir()
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _append_unique(path: Path, rows: list[dict], key_fn) -> int:
    ensure_data_dir()
    existing = _read_jsonl(path)
    keys = {key_fn(row) for row in existing}
    new_rows = [row for row in rows if key_fn(row) not in keys]
    if not new_rows:
        return 0
    with path.open("a", encoding="utf-8") as f:
        for row in new_rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(new_rows)


def append_inbox_items(items: list[dict]) -> int:
    normalized = []
    for item in items:
        row = dict(item)
        row["local_type"] = "inbox"
        row["local_is_read"] = bool(row.get("is_read"))
        normalized.append(row)
    return _append_unique(INBOX_PATH, normalized, lambda row: str(row.get("id")))


def append_chat_messages(conv_id: int, messages: list[dict]) -> int:
    normalized = []
    for msg in messages:
        row = dict(msg)
        row["local_type"] = "chat"
        row["conversation_id"] = conv_id
        normalized.append(row)
    return _append_unique(
        CHAT_CACHE_PATH,
        normalized,
        lambda row: f"{row.get('conversation_id')}:{row.get('id')}",
    )


def load_inbox(unread_only: bool = False, limit: int = 50) -> list[dict]:
    rows = _read_jsonl(INBOX_PATH)
    if unread_only:
        rows = [row for row in rows if not row.get("local_is_read") and not row.get("is_read")]
    rows.sort(key=lambda row: int(row.get("created_at") or 0), reverse=True)
    return rows[:limit]


def find_inbox_item(item_id: int) -> dict | None:
    for row in _read_jsonl(INBOX_PATH):
        if int(row.get("id") or 0) == int(item_id):
            return row
    return None


def mark_inbox_read(item_id: int) -> bool:
    rows = _read_jsonl(INBOX_PATH)
    changed = False
    for row in rows:
        if int(row.get("id") or 0) == item_id:
            row["local_is_read"] = True
            row["is_read"] = 1
            changed = True
    if changed:
        _write_jsonl(INBOX_PATH, rows)
    return changed


def unread_count() -> int:
    return len(load_inbox(unread_only=True, limit=1000000))


def max_inbox_id() -> int:
    ids = [int(row.get("id") or 0) for row in _read_jsonl(INBOX_PATH)]
    return max(ids) if ids else 0


def max_chat_id(conv_id: int = 1) -> int:
    ids = [
        int(row.get("id") or 0)
        for row in _read_jsonl(CHAT_CACHE_PATH)
        if int(row.get("conversation_id") or 0) == conv_id
    ]
    return max(ids) if ids else 0


def max_chat_ids() -> dict[str, int]:
    ids: dict[str, int] = {}
    for row in _read_jsonl(CHAT_CACHE_PATH):
        conv_id = str(int(row.get("conversation_id") or 0))
        msg_id = int(row.get("id") or 0)
        if conv_id == "0" or msg_id <= 0:
            continue
        ids[conv_id] = max(ids.get(conv_id, 0), msg_id)
    return ids
