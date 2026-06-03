"""Case-level 跨 task API。

把判決精讀結果的組織維度從「task」提升到「case_id」，讓律師過往花 token 換來的
分析可以跨 task 復用：
  - 星標（case_stars）：律師手動收藏的判決，持久化跨 session
  - 劃記（case_highlights）：reader 中黃底劃記，跨裝置同步
  - 歷史精讀：某 case_id 在歷來所有 task / analysis 中的評分、立場、摘要
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.db import database as db

router = APIRouter(tags=["cases"])


class HighlightCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    before: str = Field("", max_length=100)
    after: str = Field("", max_length=100)


@router.get("/analyses/{analysis_id}/starred")
async def list_starred(analysis_id: str) -> list[str]:
    """回傳某分析層已星標的 case_id（newest first）。前端切到該分析層時載入填 state。"""
    return await db.list_starred_cases(analysis_id)


@router.post("/analyses/{analysis_id}/cases/{case_id}/star", status_code=204)
async def star(analysis_id: str, case_id: str) -> None:
    """在某分析層加入星標。冪等：已存在時只刷新 starred_at。"""
    if not case_id.strip():
        raise HTTPException(status_code=400, detail="case_id 不可為空")
    await db.star_case(analysis_id, case_id)


@router.delete("/analyses/{analysis_id}/cases/{case_id}/star", status_code=204)
async def unstar(analysis_id: str, case_id: str) -> None:
    """取消某分析層的星標。不存在時 no-op（冪等）。"""
    await db.unstar_case(analysis_id, case_id)


@router.get("/cases/{case_id}/highlights")
async def list_highlights(case_id: str) -> list[dict]:
    """回傳某 case_id 的所有黃底劃記（created_at 升冪）。"""
    return await db.list_case_highlights(case_id)


@router.post("/cases/{case_id}/highlights", status_code=201)
async def add_highlight(case_id: str, body: HighlightCreate) -> dict:
    """新增黃底劃記。回傳 `{id}` 供前端 DOM 節點關聯（取消標記時 DELETE-by-id）。"""
    if not case_id.strip():
        raise HTTPException(status_code=400, detail="case_id 不可為空")
    hl_id = await db.add_case_highlight(
        case_id=case_id,
        text=body.text,
        before_ctx=body.before,
        after_ctx=body.after,
    )
    return {"id": hl_id}


@router.delete("/cases/{case_id}/highlights/{highlight_id}", status_code=204)
async def remove_highlight(case_id: str, highlight_id: int) -> None:
    """取消標記。case_id 只供路徑可讀性、實際刪除靠 highlight_id。"""
    await db.remove_case_highlight(highlight_id)


@router.get("/cases/{case_id}/analyses")
async def case_analyses(case_id: str) -> list[dict]:
    """回傳某 case_id 歷來在所有 task / analysis 中的精讀結果（跨 task 聚合）。

    用於 reader 頂部「歷史精讀」tab — 律師打開某判決時看到它過去在哪些研究
    問題下被分析過、評幾分、法院立場摘要等。
    """
    return await db.list_case_analyses(case_id)
