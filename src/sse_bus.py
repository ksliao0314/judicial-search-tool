"""SSE 事件匯流排：worker 推事件，stream API 訂閱事件。

每個 task_id 可以有多個訂閱者（多個瀏覽器分頁）。
每個訂閱者有獨立的 asyncio.Queue，publish 時廣播到所有訂閱者。
"""
import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# task_id -> list of subscriber queues
_subscribers: dict[str, list[asyncio.Queue[dict | None]]] = defaultdict(list)

_SENTINEL = None  # 放入 queue 表示任務結束，訂閱者應停止等待


def subscribe(task_id: str) -> asyncio.Queue[dict | None]:
    """建立訂閱，回傳專屬 queue。呼叫方負責在結束後呼叫 unsubscribe。"""
    q: asyncio.Queue[dict | None] = asyncio.Queue()
    _subscribers[task_id].append(q)
    return q


def unsubscribe(task_id: str, q: asyncio.Queue[dict | None]) -> None:
    """移除訂閱。"""
    try:
        _subscribers[task_id].remove(q)
    except ValueError:
        pass
    if not _subscribers[task_id]:
        _subscribers.pop(task_id, None)


async def publish(task_id: str, event_type: str, data: dict[str, Any]) -> None:
    """廣播事件給所有訂閱者。"""
    payload = {"event": event_type, "data": data}
    for q in list(_subscribers.get(task_id, [])):
        await q.put(payload)


async def publish_done(task_id: str) -> None:
    """推送 sentinel，通知訂閱者任務串流結束。"""
    for q in list(_subscribers.get(task_id, [])):
        await q.put(_SENTINEL)


def format_sse(event_type: str, data: dict[str, Any]) -> str:
    """將事件格式化為 SSE wire format（供 stream.py 使用）。"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
