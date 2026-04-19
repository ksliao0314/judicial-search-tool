"""GET /tasks/{id}/judgments — 多分析層 JOIN 清單查詢。

查詢參數：
  primary_analysis_id    必填，主排序依據
  secondary_analysis_id  選填，副標籤
  min_score              選填，score 門檻（≥）
  court                  選填，法院名稱模糊搜尋
  year_from / year_to    選填，西元年（資料庫存西元）
"""
import io
import zipfile

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from src.db import database as db
from src.pipeline.pdf_generator import generate_judgment_pdf

router = APIRouter(tags=["judgments"])


@router.get("/tasks/{task_id}/judgments")
async def list_judgments(
    task_id: str,
    primary_analysis_id: str = Query(..., description="主分析層 ID"),
    secondary_analysis_id: str | None = Query(None, description="副分析層 ID"),
    min_score: int | None = Query(None, ge=1, le=10, description="最低 score"),
    court: str | None = Query(None, description="法院名稱（模糊）"),
    year_from: int | None = Query(None, description="起始西元年"),
    year_to: int | None = Query(None, description="結束西元年"),
    limit: int | None = Query(None, ge=1, le=500, description="分頁筆數"),
    offset: int | None = Query(None, ge=0, description="分頁偏移"),
):
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")

    return await db.get_judgments_with_analyses(
        task_id=task_id,
        primary_analysis_id=primary_analysis_id,
        secondary_analysis_id=secondary_analysis_id,
        min_score=min_score,
        court_filter=court,
        year_from=year_from,
        year_to=year_to,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/{task_id}/judgments/{case_id}")
async def get_judgment_detail(task_id: str, case_id: str) -> dict:
    """取得單筆判決全文（供閱讀器頁使用）。"""
    judgments = await db.get_task_judgments(task_id)
    for j in judgments:
        if j["case_id"] == case_id:
            return j
    raise HTTPException(status_code=404, detail="Judgment not found")


@router.get("/tasks/{task_id}/judgments/{case_id}/pdf")
async def download_judgment_pdf(task_id: str, case_id: str):
    """下載單筆判決 PDF。"""
    judgments = await db.get_task_judgments(task_id)
    judgment = None
    for j in judgments:
        if j["case_id"] == case_id:
            judgment = j
            break
    if not judgment:
        raise HTTPException(status_code=404, detail="Judgment not found")

    pdf_bytes = generate_judgment_pdf(judgment)
    # 用字號做檔名（去掉全形空白）
    safe_name = (case_id or "judgment").replace('\u3000', '').replace(' ', '')[:50]
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.pdf"'},
    )


class BulkPdfRequest(BaseModel):
    case_ids: list[str]


@router.post("/tasks/{task_id}/judgments/bulk-pdf")
async def download_bulk_pdf(task_id: str, body: BulkPdfRequest):
    """批次下載多筆判決 PDF（zip）。"""
    judgments = await db.get_task_judgments(task_id)
    judgment_map = {j["case_id"]: j for j in judgments}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cid in body.case_ids:
            j = judgment_map.get(cid)
            if not j:
                continue
            pdf_bytes = generate_judgment_pdf(j)
            safe_name = (cid or "judgment").replace('\u3000', '').replace(' ', '')[:50]
            zf.writestr(f"{safe_name}.pdf", pdf_bytes)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="judgments.zip"'},
    )
