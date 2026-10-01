"""对账流水的哈希链。

对账流水只追加、不改写。每条记录都带前一条的摘要，任一条被事后篡改，链从
那一条起全部对不上，巡检据此发现"历史被动过"。摘要只覆盖业务字段（序号、
类型、时间、负载），与落盘格式解耦。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = "0" * 64


def canonical_payload(record: dict[str, Any]) -> str:
    """把除摘要外的字段按固定键序序列化，供摘要与验链共用。"""

    head = {key: record[key] for key in ("seq", "rec_type", "ts", "payload")}
    return json.dumps(head, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(record: dict[str, Any], prev_hash: str) -> str:
    """计算一条记录的摘要（前挂前一条摘要）。"""

    blob = prev_hash + canonical_payload(record)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verify_chain(records: list[dict[str, Any]]) -> dict:
    """逐条核对哈希链，返回是否完整与第一处断裂位置。"""

    previous = GENESIS
    for index, record in enumerate(records):
        expected = digest_of(record, previous)
        if record.get("digest") != expected:
            return {"ok": False, "broken_at_seq": record.get("seq"), "index": index, "count": len(records)}
        previous = str(record.get("digest"))
    return {"ok": True, "broken_at_seq": None, "index": None, "count": len(records)}
