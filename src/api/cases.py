"""Case-level 跨 task API。

把判決精讀結果的組織維度從「task」提升到「case_id」，讓律師過往花 token 換來的
分析可以跨 task 復用：
  - 星標（case_stars）：律師手動收藏的判決，持久化跨 session
  - 歷史精讀：某 case_id 在歷來所有 task / analysis 中的評分、立場、摘要
"""
from fastapi import APIRouter, HTTPException

from src.db import database as db

router = APIRouter(tags=["cases"])


@router.get("/cases/starred")
async def list_starred() -> list[str]:
    """回傳所有已星標的 case_id（newest first）。前端啟動時呼叫一次填 state。"""
    return await db.list_starred_cases()


@router.post("/cases/{case_id}/star", status_code=204)
async def star(case_id: str) -> None:
    """加入星標。冪等：已存在時只刷新 starred_at。"""
    if not case_id.strip():
        raise HTTPException(status_code=400, detail="case_id 不可為空")
    await db.star_case(case_id)


@router.delete("/cases/{case_id}/star", status_code=204)
async def unstar(case_id: str) -> None:
    """取消星標。不存在時 no-op（冪等）。"""
    await db.unstar_case(case_id)


@router.get("/cases/{case_id}/analyses")
async def case_analyses(case_id: str) -> list[dict]:
    """回傳某 case_id 歷來在所有 task / analysis 中的精讀結果（跨 task 聚合）。

    用於 reader 頂部「歷史精讀」tab — 律師打開某判決時看到它過去在哪些研究
    問題下被分析過、評幾分、法院立場摘要等。
    """
    return await db.list_case_analyses(case_id)
