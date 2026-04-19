"""GET /tasks/{id}/stream — Server-Sent Events 推播任務進度。

前端訂閱後即時收到：
  judgments_ready  — search/filter 完成
  batch_done       — 每批 Claude 分析完成
  analysis_done    — 整層分析完成
  task_done        — 任務全部完成
"""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from src.db import database as db
from src import sse_bus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])


@router.get("/tasks/{task_id}/stream")
async def task_stream(task_id: str):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # 不要因 task.status=='done' 就 early-return —— stage 2.5 深度篩選、stage 3 精讀
    # 都會在 task 已 done 時對它 publish 事件（fetch-judgments / analyses 端點會起新 worker）。
    # 前端打開 stream 就訂閱 sse_bus，直到連線被 client 關閉為止。
    queue = sse_bus.subscribe(task_id)

    async def event_gen():
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # 心跳，避免代理切斷連線
                    yield {"event": "heartbeat", "data": "{}"}
                    continue

                if payload is None:  # sentinel — 任務結束
                    break

                yield {
                    "event": payload["event"],
                    "data": json.dumps(payload["data"], ensure_ascii=False),
                }
        finally:
            sse_bus.unsubscribe(task_id, queue)

    return EventSourceResponse(event_gen())
