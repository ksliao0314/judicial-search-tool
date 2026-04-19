"""Worker 診斷 + kill API。

給律師用來查「現在什麼 task 卡住」+ 砍掉卡住的 inflight work。
"""
from fastapi import APIRouter, HTTPException

from src.db import database as db
from src.worker import runner

router = APIRouter(tags=["workers"])


@router.get("/debug/workers")
async def get_workers_status() -> dict:
    """回傳 sem 狀態 + 活躍 work 清單 + 排隊數量。

    前端 bell 可以 poll 此 endpoint，顯示「排隊中 N 個 task」讓律師知道系統在做事。
    發現 age_sec 異常大（接近 timeout 上限）的 work → 律師可按 kill。
    """
    return runner.get_workers_snapshot()


@router.post("/tasks/{task_id}/kill-worker", status_code=200)
async def kill_worker(task_id: str) -> dict:
    """砍掉某 task 所有 inflight work（不刪 DB row，讓律師能看原因 + 自己重試）。

    流程：
      1. 對每個 active work 的 asyncio.Task 呼叫 .cancel()
      2. CancelledError 傳到 _execute_work → _notify_work_cancelled 做 mark failed + SSE
      3. Sem 自然釋放、排隊中的 task 立刻 unblock

    若 task 不存在 → 404；killed=0 也算成功（代表沒有 inflight work）。
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    killed = await runner.kill_workers_for_task(task_id)
    return {"task_id": task_id, "killed": killed}
