"""兩階段搜尋的 task endpoints：

  POST   /tasks                建立 task + 觸發 stage 1（純廣搜，寫 task_search_hits）
  GET    /tasks                任務清單（附 hits_total）
  GET    /tasks/{id}           單筆任務 + analyses
  GET    /tasks/{id}/hits      Stage 1 結果清單（給前端做 stage 2 client filter）
  GET    /tasks/{id}/hits/{case_id}
                                Reader 即時讀單筆全文（代理 MCP get_judgment，不寫 DB）
  DELETE /tasks/{id}           刪除任務（含 cancel 中執行任務）

POST /tasks 走非同步：立刻回 task_id；前端訂閱 SSE 等 stage1_done。
"""
import asyncio
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src import mcp_client
from src.db import database as db
from src.worker.runner import (
    Stage1SearchWork, Stage25FetchWork, request_cancel_deep_fetch,
    _run_stage1_search, TaskCancelledError, dispatch_work, _get_stage_sem,
    register_worker, unregister_worker, WORK_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])


# 合法欄位白名單（同 pipeline/filter.py 與 analyze.py 的 FIELD_LABELS 鍵集合）。
# Stage 3 analyses 端點驗證 filter_fields / ai_read_fields 用，留在這方便 import。
ALLOWED_FIELDS = {"reasoning", "main_text", "facts", "cited_statutes", "full_text"}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    keyword: str = Field(min_length=1, description="搜尋關鍵字（空格分隔多個 = AND）")
    search_domain: str = Field(
        default="judgment",
        description="搜尋分流：'judgment'（走 FJUD、最多 500 筆、支援 exhaustive）"
                    "或 'interpretation'（走 cons 釋字+憲判字、本機離線索引）",
    )
    expand_keywords: bool = Field(
        default=True,
        description="是否在 stage 1 search 前對 keyword 做 citation/synonym 展開。"
                    "UI 使用者透過展開預覽送出時應傳 false 避免後端重複展開。"
                    "interpretation mode 自動忽略（cons 離線索引無需展開）",
    )
    exhaustive: bool = Field(
        default=True,
        description="Stage 1 預設窮盡（律師看到的「找到 N 筆」必須準確才能決定要不要 narrow）。"
                    "可關閉走快速 path（但 N 可能少報）。interpretation mode 自動忽略",
    )
    main_text: str | None = Field(
        default=None,
        description="主文內含字串（對應司法院「裁判主文」欄位 jud_jmain）— search server-side 篩選，"
                    "比抓回來再 client filter 快很多。例：「被告應給付」「撤銷原處分」。"
                    "理由欄司法院 search 不支援，需 stage 2.5 深度篩選。"
                    "interpretation mode 無此欄位（釋字無主文結構）",
    )
    year_from: int | None = Field(
        default=None,
        description="搜尋年度下限（民國年）— 對 MCP search 壓 server-side 篩選、有效減少 Stage 1 結果。"
                    "interpretation mode 忽略（cons 離線索引無此篩選）。",
    )
    year_to: int | None = Field(
        default=None,
        description="搜尋年度上限（民國年）。",
    )
    original_keyword: str | None = Field(
        default=None,
        description="使用者原始輸入（展開前），用於 UI 顯示。未提供時 fallback 到 keyword。",
    )


class CreateTaskResponse(BaseModel):
    task_id: str
    status: str  # 'pending' — 前端訂閱 SSE 等 stage1_done


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/tasks", response_model=CreateTaskResponse, status_code=201)
async def create_task(
    body: CreateTaskRequest,
    x_api_key: str | None = Header(default=None),
) -> CreateTaskResponse:
    task_id = str(uuid.uuid4())

    # filter_fields 留空字串（schema NOT NULL 限制）— 新流程不在 task 層級使用，
    # 移到 analyses 端點。mode 預設 'keyword'，semantic 之後再決定怎麼接。
    if body.search_domain not in ("judgment", "interpretation"):
        raise HTTPException(status_code=400, detail="search_domain 必須是 'judgment' 或 'interpretation'")
    await db.create_task(
        task_id=task_id,
        mode="keyword",
        search_domain=body.search_domain,
        keyword=body.keyword,
        filter_fields="",
        status="pending",
        search_params={
            "expand_keywords": body.expand_keywords,
            "exhaustive": body.exhaustive,
            "main_text": body.main_text,
            "year_from": body.year_from,
            "year_to": body.year_to,
            "original_keyword": body.original_keyword,
            "search_domain": body.search_domain,   # 冗餘存一份給 recovery 用
        },
    )

    work = Stage1SearchWork(
        task_id=task_id,
        keyword=body.keyword,
        expand_keywords=body.expand_keywords,
        exhaustive=body.exhaustive,
        main_text=body.main_text,
        year_from=body.year_from,
        year_to=body.year_to,
        api_key=x_api_key,
        search_domain=body.search_domain,
    )

    # Stage 1 不走 task_queue — 直接開 asyncio task 並行跑（只打 MCP 拿 metadata）。
    # 走 _stage_sem(5) 全域併發上限；Stage 1 雖短，但避免律師一次 fire 20 個搜尋
    # 把 MCP subprocess 撐爆（Playwright fallback 尤其吃資源）。
    # 保留獨立 try/except：stage1_failed 是專屬 SSE 事件、前端有對應 handler，不能走
    # _execute_work 的通用 analysis_failed 路徑。
    async def _bg_stage1():
        async with _get_stage_sem():
            work_id = register_worker(task_id, "stage1_search")
            try:
                await asyncio.wait_for(_run_stage1_search(work), timeout=WORK_TIMEOUT_SEC)
            except TaskCancelledError:
                logger.info("Stage 1 task %s 已被刪除，中止", task_id)
            except asyncio.TimeoutError:
                logger.error("Stage 1 task %s 超時 %d 秒，強制中止", task_id, WORK_TIMEOUT_SEC)
                try:
                    await db.update_task(task_id, status="failed")
                    from src import sse_bus
                    await sse_bus.publish(task_id, "stage1_failed", {
                        "task_id": task_id, "error": f"搜尋超時 {WORK_TIMEOUT_SEC}s，請重試",
                    })
                    await sse_bus.publish_done(task_id)
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("Stage 1 task %s 失敗：%s", task_id, exc)
                try:
                    await db.update_task(task_id, status="failed")
                    # 推 SSE 通知前端搜尋失敗（避免律師看到永久 spinner）
                    from src import sse_bus
                    await sse_bus.publish(task_id, "stage1_failed", {
                        "task_id": task_id,
                        "error": str(exc)[:200],
                    })
                    await sse_bus.publish_done(task_id)
                except Exception as inner:
                    logger.error("Stage 1 task %s 標記失敗也失敗：%s", task_id, inner)
            finally:
                unregister_worker(work_id)

    asyncio.create_task(_bg_stage1())

    return CreateTaskResponse(task_id=task_id, status="pending")


@router.get("/tasks")
async def list_tasks() -> list[dict]:
    """任務清單。每筆附 `hits_total`（task_search_hits 筆數），新流程的首頁用來顯示「N 筆」。
    Legacy 任務 hits_total 為 0（資料在 task_judgments 而非 task_search_hits）。
    """
    tasks = await db.list_tasks()
    for t in tasks:
        t["hits_total"] = await db.count_task_search_hits(t["id"])
        # 附 analyses 摘要，讓首頁 getTaskPhase 能正確判斷精讀狀態
        t["analyses"] = await db.list_analyses(t["id"])
    return tasks


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    analyses = await db.list_analyses(task_id)
    hits_total = await db.count_task_search_hits(task_id)
    facts_coverage = await db.get_facts_coverage(task_id)
    return {
        **task,
        "analyses": analyses,
        "hits_total": hits_total,
        "facts_coverage": facts_coverage,
    }


@router.get("/tasks/{task_id}/hits")
async def list_task_hits(task_id: str) -> list[dict]:
    """Stage 1 結果清單。前端拿到後做 stage 2 client filter（court/year/cause）。"""
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.get_task_search_hits(task_id)


@router.get("/tasks/{task_id}/hits/{case_id:path}")
async def get_task_hit_full(task_id: str, case_id: str) -> dict:
    """Reader 即時讀取單筆判決全文 — 代理 MCP get_judgment，不寫 task_judgments。

    case_id 用 `:path` 後綴是因為 jid 含逗號（如 `TPAA,113,訴,1234,...`），
    若用一般 path param 會被 URL 解析誤切。
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    hit = await db.get_task_search_hit(task_id, case_id)
    if not hit:
        raise HTTPException(status_code=404, detail="Hit not found in this task")
    judgment = await mcp_client.get_judgment(case_id)
    # 把 stage 1 的 cause / summary 也帶回（reader 顯示時用得到）
    return {**judgment, "cause": hit.get("cause"), "summary": hit.get("summary")}


class FetchJudgmentsRequest(BaseModel):
    """Stage 2.5 深度篩選 request：律師指定要抓全文的 case_ids 子集。"""
    case_ids: list[str] = Field(min_length=1, description="要抓全文的 case_id 清單（jid 結構化）")


@router.post("/tasks/{task_id}/fetch-judgments", status_code=202)
async def fetch_judgments(
    task_id: str,
    body: FetchJudgmentsRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Stage 2.5：對指定 case_ids 抓全文（非同步背景執行）。

    抓完寫入 task_judgments，前端訂閱 SSE 收 `stage25_progress` / `stage25_done`，
    完成後可在 stage 2 view 用「內文細篩」對主文/理由/事實做字串過濾，零 Claude 成本。
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    work = Stage25FetchWork(
        task_id=task_id,
        case_ids=body.case_ids,
        api_key=x_api_key,
    )
    # Bypass task_queue — 並行與其他 task 的 Stage 2.5 / Stage 3。semaphore(5) 控上限。
    # 同時記錄 stage25_inflight 供 server 重啟 recovery 用。
    await db.record_stage25_inflight(task_id, body.case_ids)
    asyncio.create_task(dispatch_work(work))
    return {"task_id": task_id, "queued": len(body.case_ids), "status": "queued"}


@router.delete("/tasks/{task_id}/fetch-judgments", status_code=200)
async def cancel_fetch_judgments(task_id: str) -> dict:
    """取消進行中的 stage 2.5 深度篩選。worker 會在下一筆 fetch 邊界中止並推
    `stage25_cancelled` SSE。已抓到的 task_judgments 保留不刪（仍可用於內文細篩）。
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    request_cancel_deep_fetch(task_id)
    return {"task_id": task_id, "cancel_requested": True}


@router.get("/tasks/{task_id}/judgments-bulk")
async def list_judgments_bulk(task_id: str) -> list[dict]:
    """回傳 task 中所有已抓全文的 task_judgments（無 analysis JOIN）。

    給 stage 2「內文細篩」用 — 前端拿到後在 client 跑 substring 比對。
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.get_task_judgments(task_id)


@router.delete("/tasks/{task_id}", status_code=200)
async def delete_task(task_id: str) -> dict:
    """刪除任務及其全部附屬資料（hits / judgments / analyses / results / search_hits）。

    執行中 / pending 任務也允許刪除：執行中的 work 會在主要 pipeline 邊界檢查
    task row 是否仍存在（runner.py 的 `_check_task_alive`），偵測到被刪就 raise
    TaskCancelledError，讓 `_execute_work` 的 except 攔下並中止該 work。
    最壞情況：work 還在某個長 await（例如 MCP 搜尋）內，會浪費那次呼叫的成本；
    但 FK violations 全被 `_execute_work` 的 generic except 攔下，不會影響其他 work。
    """
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    stats = await db.delete_task(task_id)
    return {
        "task_id": task_id,
        "deleted": stats,
        "was_running": task["status"] in ("pending", "running"),
    }
