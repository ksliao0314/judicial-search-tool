"""POST /tasks/{id}/analyses — 新增分析層（Stage 3 v2）。

律師只需填 NL 問題 + 可選 read_facts toggle + 可選 narrow 條件。
後端自動：依 narrow 篩子集 → 抓全文 → per-judgment Claude 評分（score=論述詳細度）→ synthesis 總結。
"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.db import database as db
from src.worker.runner import (
    Stage3AnalyzeWork, ReasoningPreFilterWork,
    _run_reasoning_prefilter, TaskCancelledError, dispatch_work, _get_stage_sem,
    register_worker, unregister_worker, WORK_TIMEOUT_SEC,
    request_finalize_preliminary, request_graceful_abort, _clear_graceful_abort,
    _fire_abort_partial_synthesis,
    start_retry_skipped, _is_retry_skipped_inflight,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analyses"])


class NarrowSpec(BaseModel):
    """Stage 3 narrow 條件。空欄位代表不限。"""
    court_tiers: list[str] | None = None
    year_from: int | None = None
    year_to: int | None = None
    case_types: list[str] | None = None  # 後端 ignore；前端 client filter


class AddAnalysisRequest(BaseModel):
    question: str = Field(min_length=1, description="律師研究的 NL 問題")
    read_facts: bool = Field(
        default=False,
        description="是否同時讀「事實」段（成本較高；僅「有沒有 X 情形」類問題需要）",
    )
    narrow: NarrowSpec | None = Field(
        default=None,
        description="Stage 3 narrow 條件。省略 = 不 narrow 分析全部 hits",
    )
    prefilter_case_ids: list[str] | None = Field(
        default=None,
        description="理由預篩後的 case_id 清單。有值時跳過 fetch、只分析這些 case_ids",
    )
    reasoning_filter: bool = Field(
        default=False,
        description="在 fetch 過程中即時比對 reasoning 含關鍵字，只精讀命中的",
    )


class PreFilterRequest(BaseModel):
    narrow: NarrowSpec | None = None


class AddAnalysisResponse(BaseModel):
    analysis_id: str
    status: str
    flow: str  # 'stage3' | 'legacy'


@router.post("/tasks/{task_id}/analyses", response_model=AddAnalysisResponse, status_code=201)
async def add_analysis(
    task_id: str,
    body: AddAnalysisRequest,
    x_api_key: str | None = Header(default=None),
) -> AddAnalysisResponse:
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in ("running", "done"):
        raise HTTPException(
            status_code=409,
            detail="任務尚未完成 search 階段，無法追問",
        )

    hits_count = await db.count_task_search_hits(task_id)
    if hits_count == 0:
        # 無 task_search_hits = Stage 1 沒跑或 0-hit task（後者前端會 auto-delete）
        raise HTTPException(
            status_code=409,
            detail="任務尚未完成搜尋階段或搜尋 0 筆，無法精讀",
        )

    analysis_id = str(uuid.uuid4())
    # 結構化段落全部讀取（facts 預設 always-on，budget 由 analyze.py 控制）
    narrow_dict = body.narrow.model_dump(exclude_none=True) if body.narrow else {}
    ai_read_fields_str = "reasoning,main_text,facts,cited_statutes"

    await db.create_analysis(
        analysis_id=analysis_id,
        task_id=task_id,
        question=body.question,
        ai_read_field=ai_read_fields_str,
        filter_fields=None,           # v2 不用 string pre-filter
        narrow_state=narrow_dict,
        status="pending",
    )

    work = Stage3AnalyzeWork(
        task_id=task_id,
        analysis_id=analysis_id,
        question=body.question,
        read_facts=body.read_facts,
        narrow=narrow_dict,
        api_key=x_api_key,
        prefilter_case_ids=body.prefilter_case_ids,
        reasoning_filter=body.reasoning_filter,
    )

    # Bypass task_queue — 併發執行，dispatch_work 內部 semaphore(5) 控上限。
    # LLM 共享 token bucket，所以兩 task 並行 LLM 不會爆 API quota。
    asyncio.create_task(dispatch_work(work))
    return AddAnalysisResponse(analysis_id=analysis_id, status="pending", flow="stage3")


@router.post("/tasks/{task_id}/reasoning-prefilter", status_code=200)
async def start_reasoning_prefilter(
    task_id: str,
    body: PreFilterRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """啟動理由預篩：抓全文 + 比對 reasoning 含關鍵字。

    改為 asyncio.create_task 並行執行（不走 worker queue），
    因為預篩只打 MCP 抓全文 + 字串比對，不佔 Claude API quota。
    """
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    narrow_dict = body.narrow.model_dump(exclude_none=True) if body.narrow else {}

    # 預先計算 narrow 後的 total 筆數，給 DB row 用
    from src.worker.runner import _apply_narrow
    all_hits = await db.get_task_search_hits(task_id)
    narrowed = _apply_narrow(all_hits, narrow_dict)
    total = len(narrowed)

    # 律師手動啟動 → INSERT OR REPLACE 整筆（recovery_attempts 重置為 0、matched 清空）
    import json as _json
    narrow_json = _json.dumps(narrow_dict, sort_keys=True, ensure_ascii=False)
    await db.init_prefilter_result(task_id, narrow_json, total)

    work = ReasoningPreFilterWork(
        task_id=task_id,
        narrow=narrow_dict,
        api_key=x_api_key,
    )

    # reasoning_prefilter 不佔 Claude 額度、但要走 _stage_sem(5) 全域併發上限
    # 防止多個 task 同時啟動預篩把 MCP 撐爆（每 task 內部會順序 fetch 全文、
    # 全域多 task 併發仍需限制）
    async def _bg_prefilter():
        async with _get_stage_sem():
            work_id = register_worker(task_id, "reasoning_prefilter")
            try:
                await asyncio.wait_for(_run_reasoning_prefilter(work), timeout=WORK_TIMEOUT_SEC)
            except TaskCancelledError:
                logger.info("Prefilter task %s 已被刪除，中止", task_id)
            except asyncio.TimeoutError:
                logger.error("Prefilter task %s 超時 %d 秒，強制中止", task_id, WORK_TIMEOUT_SEC)
                try:
                    cur = await db.get_prefilter_result(task_id)
                    if cur and cur["narrow"] == narrow_json:
                        await db.mark_prefilter_cancelled(task_id)
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("Prefilter task %s 失敗：%s", task_id, exc)
                # 非 ownership-lost 的 exception → mark cancelled（若仍是當前 owner）
                try:
                    cur = await db.get_prefilter_result(task_id)
                    if cur and cur["narrow"] == narrow_json:
                        await db.mark_prefilter_cancelled(task_id)
                except Exception:
                    pass
            finally:
                unregister_worker(work_id)

    asyncio.create_task(_bg_prefilter())
    return {"ok": True, "task_id": task_id, "total": total}


@router.get("/tasks/{task_id}/prefilter-result")
async def get_prefilter_result(task_id: str) -> dict | None:
    """前端 openTask 時呼叫：回傳當前預篩狀態（若有）。

    回傳 row 本身（narrow / matched_case_ids 解 JSON 後）或 null。
    """
    r = await db.get_prefilter_result(task_id)
    if not r:
        return None
    import json as _json
    try:
        r["narrow"] = _json.loads(r["narrow"])
    except Exception:
        r["narrow"] = {}
    try:
        r["matched_case_ids"] = _json.loads(r["matched_case_ids"])
    except Exception:
        r["matched_case_ids"] = []
    return r


@router.delete("/tasks/{task_id}/prefilter-result", status_code=204)
async def clear_prefilter_result(task_id: str) -> None:
    """律師按「清除」時刪掉預篩結果 row。"""
    await db.delete_prefilter_result(task_id)


@router.get("/tasks/{task_id}/analyses")
async def list_analyses(task_id: str) -> list[dict]:
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.list_analyses(task_id)


@router.post("/tasks/{task_id}/analyses/{analysis_id}/finalize", status_code=200)
async def finalize_preliminary(task_id: str, analysis_id: str) -> dict:
    """律師按「就用現在結果定稿」→ preliminary 升格 final。

    三條路徑：
    1. **有 preliminary synthesis（含 running）→ 立即 DB 升格**（0 延遲路徑）
       - running 時額外設 graceful_abort 旗標讓 scoring 停下（避免背景繼續燒 token）
       - 直接寫 status=done、is_preliminary=0、推 stage3_synthesis_done is_final=True
       - 回 is_final=True、FE 立即 re-render（不等 SSE）
    2. status='running' 但**還沒有 preliminary**（罕見、scoring 剛開始）→ set finalize 旗標、
       retry loop 邊界 check 後升格（舊路徑、FE 等 SSE + 5 秒 fallback）
    3. 其他 → 409（沒 preliminary 可升格、也不在跑）
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    analysis = await db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")

    has_preliminary = bool(analysis.get("synthesis_is_preliminary")) and analysis.get("synthesis")
    current_status = analysis.get("status")

    # 路徑 1：有 preliminary → 立即升格（含 running 情境）
    if has_preliminary:
        from src import sse_bus
        # 若正在跑、set abort 旗標讓 scoring 合作停下（避免背景 workers 繼續燒 token）
        # 也 set finalize 旗標當 belt-and-suspenders（retry loop 讀到會 early break）
        if current_status == "running":
            request_graceful_abort(analysis_id)
            request_finalize_preliminary(analysis_id)
        try:
            existing_synthesis = json.loads(analysis.get("synthesis") or "{}")
        except (json.JSONDecodeError, TypeError):
            existing_synthesis = {}
        await db.update_analysis(
            analysis_id, status="done", synthesis_is_preliminary=0,
        )
        # P1-6：SSE publish 包 try/except — DB 已寫成功、SSE 失敗不該讓整個 request 爆 500
        # FE 已有 5 秒 fallback check（見 handleFinalizePreliminary）會自己 reload 回正確狀態
        try:
            await sse_bus.publish(task_id, "stage3_synthesis_done", {
                "task_id": task_id, "analysis_id": analysis_id,
                "synthesis": existing_synthesis, "is_final": True,
            })
            await sse_bus.publish(task_id, "analysis_done", {
                "task_id": task_id, "analysis_id": analysis_id,
                "match_count": analysis.get("match_count", 0),
            })
        except Exception as sse_e:
            logger.warning("[%s] /finalize SSE publish 失敗（FE 靠 5 秒 fallback 收尾）：%s",
                           task_id, sse_e)
        logger.info("[%s] /finalize 即時升格（原 status=%s）→ done",
                    task_id, current_status)
        return {
            "analysis_id": analysis_id,
            "finalize_requested": True,
            "current_status": "done",
            "has_preliminary": False,
            "is_final": True,
        }

    # 路徑 2：running 但還沒 preliminary → set 旗標、retry loop 升格
    if current_status == "running":
        request_finalize_preliminary(analysis_id)
        return {
            "analysis_id": analysis_id,
            "finalize_requested": True,
            "current_status": "running",
            "has_preliminary": False,
            "is_final": False,
        }

    # 路徑 3：沒 preliminary + 不在跑 → 409
    raise HTTPException(status_code=409, detail="無 preliminary synthesis 可升格")


@router.post("/tasks/{task_id}/analyses/{analysis_id}/abort", status_code=200)
async def abort_analysis(
    task_id: str,
    analysis_id: str,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """律師按「中止」→ set graceful abort 旗標、依命中數決定走 fast / slow path。

    Fast path（命中 ≥ 3）：
      立即 fire-and-forget 背景跑 partial synthesis（發 stage3_synthesis_start、
      run_synthesis、寫 DB、發 stage3_partial_done）。律師按下中止後 5-15 秒看到結果。
      Scoring 那邊的 in-flight Claude call 繼續跑、最終 scoring-end 的 graceful_abort
      分支偵測 synthesis 已存在 → skip 重複 synthesis。

    Slow path（命中 < 3）：
      只 set 旗標、等 scoring cooperative 結束、scoring-end graceful_abort 分支
      發 stage3_cancelled（partial synthesis 沒有價值、不跑）。
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    analysis = await db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    if analysis.get("status") not in {"pending", "running"}:
        raise HTTPException(
            status_code=409,
            detail=f"只能中止 pending / running 的分析（當前 {analysis.get('status')}）",
        )
    request_graceful_abort(analysis_id)
    match_count = int(analysis.get("match_count", 0) or 0)
    fast_path = match_count >= 3
    if fast_path:
        asyncio.create_task(_fire_abort_partial_synthesis(
            analysis_id=analysis_id,
            task_id=task_id,
            question=analysis.get("question", ""),
            api_key=x_api_key,
        ))
    return {
        "analysis_id": analysis_id,
        "abort_requested": True,
        "current_status": analysis.get("status"),
        "match_count": match_count,
        "fast_synthesis": fast_path,
    }


@router.get("/tasks/{task_id}/analyses/{analysis_id}/results-feed")
async def list_analysis_results_feed(task_id: str, analysis_id: str) -> dict:
    """給 card live feed 用：律師中途打開卡片時 backfill 已精讀結果。

    回 {items: 最新 N 筆顯示列（case_id/score/match）, total: 真實總筆數}。total 讓前端的
    「即時回傳筆數」不會被顯示用的 limit 砍而倒退。
    """
    if not await db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    if not await db.get_analysis(analysis_id):
        raise HTTPException(status_code=404, detail="Analysis not found")
    return await db.list_analysis_results_feed(analysis_id)


@router.post("/tasks/{task_id}/analyses/{analysis_id}/retry", status_code=200)
async def retry_analysis(
    task_id: str,
    analysis_id: str,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """重試失敗的 analysis：清除舊 results → 重置狀態 → 重新入佇列。"""
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    analysis = await db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    if analysis["status"] != "failed":
        raise HTTPException(status_code=409, detail="只能重試失敗的分析")

    # 清除舊的 analysis_results
    await db.delete_analysis_results(analysis_id)
    # 重置 analysis 狀態
    await db.update_analysis(
        analysis_id, status="pending", completed=0, match_count=0, synthesis=None,
    )
    # 恢復 task 狀態（如果 task 也被標 failed 的話）
    if task["status"] == "failed":
        await db.update_task(task_id, status="done")

    # 重新 dispatch Stage 3（0-hit task 不會走到這裡因為前端會 auto-delete）
    try:
        narrow = json.loads(analysis.get("narrow_state") or "{}")
    except (json.JSONDecodeError, TypeError):
        narrow = {}
    ai_read = (analysis.get("ai_read_field") or "").split(",")
    read_facts = "facts" in ai_read
    work = Stage3AnalyzeWork(
        task_id=task_id,
        analysis_id=analysis_id,
        question=analysis["question"],
        read_facts=read_facts,
        narrow=narrow,
        api_key=x_api_key,
    )

    # Bypass task_queue — 併發執行，dispatch_work 內部 semaphore(5) 控上限。
    # LLM 共享 token bucket，所以兩 task 並行 LLM 不會爆 API quota。
    asyncio.create_task(dispatch_work(work))
    return {"analysis_id": analysis_id, "status": "pending"}


@router.post("/tasks/{task_id}/analyses/{analysis_id}/resume", status_code=200)
async def resume_analysis(
    task_id: str,
    analysis_id: str,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """律師按「繼續未完成的分析」→ 從 partial 狀態續跑剩餘判決。

    與 retry 不同：保留 completed / match_count / analysis_results（已分析的不重跑）。
    run_analysis_v2 的 already_done_ids 機制會自動 skip 已寫 DB 的 case。
    status=partial → running；runner 完成後若正常 → 升格 final（覆蓋 partial synthesis）；
    若律師續跑中再中止（graceful_abort）→ synthesis_is_preliminary 為 True 時 was_resumed=True、
    升格 done + is_final=True（見 runner._run_stage3_analyze 的 graceful abort 分支）。
    """
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    analysis = await db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    if analysis["status"] != "partial":
        raise HTTPException(
            status_code=409,
            detail=f"只能續跑 partial 的分析（當前 {analysis['status']}）",
        )

    # P0-3：Atomic check-and-set、防 double-click race
    # 若律師瞬間 double-click、兩個 request 同時到這、只有一個 UPDATE 成功、
    # 另一個 rowcount=0 → 回 409（避免兩個 worker 同時跑同 analysis 造成資料 race）
    # 不 reset completed / match_count / analysis_results — 續跑重點就是保留已分析結果
    acquired = await db.update_analysis_if_status(
        analysis_id, expected_status="partial", status="running",
    )
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail="此分析已在續跑中或狀態已變（避免 double-click 造成資料重覆）",
        )
    # 清掉 graceful abort 旗標（從先前 /abort 留下的）：
    # 若不清，新 worker 起來時 _run_and_report 會 check 到旗標 → 全部 early-return aborted
    # → scoring loop 瞬間結束 → 進 graceful_abort 分支 → was_resumed=True → 誤標 done
    # → 實際只多跑 0-1 筆
    _clear_graceful_abort(analysis_id)

    try:
        narrow = json.loads(analysis.get("narrow_state") or "{}")
    except (json.JSONDecodeError, TypeError):
        narrow = {}
    ai_read = (analysis.get("ai_read_field") or "").split(",")
    read_facts = "facts" in ai_read
    work = Stage3AnalyzeWork(
        task_id=task_id,
        analysis_id=analysis_id,
        question=analysis["question"],
        read_facts=read_facts,
        narrow=narrow,
        api_key=x_api_key,
    )
    asyncio.create_task(dispatch_work(work))
    return {"analysis_id": analysis_id, "status": "running"}


@router.post("/tasks/{task_id}/analyses/{analysis_id}/retry-skipped", status_code=202)
async def retry_skipped_cases(
    task_id: str,
    analysis_id: str,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """律師按「重試 N 筆下載失敗」→ 背景對 stage2.5 fetch 失敗的 case_ids 重抓 + scoring。

    流程（非同步、立即回 202）：
    1. 讀 analyses.skipped_case_ids
    2. 逐筆 _fetch_one：成功寫 task_judgments、失敗加回 still_skipped
    3. 對救回的筆跑 run_analysis_v2(case_id_filter=...) 跑 scoring
    4. 更新 skipped_case_ids（留下仍失敗的、全成功則 NULL）
    5. SSE retry_skipped_{start|progress|done} 通知 UI

    不重跑 synthesis — 新救回的 match 會出現在結果列表、cluster/summary 維持原樣。
    律師若要含在 synthesis、可按「重新總結」或發新追問。

    Race：_retry_skipped_inflight guard 防 double-click；既有 retry 在跑就回 409。
    """
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    analysis = await db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    raw = analysis.get("skipped_case_ids")
    if not raw:
        raise HTTPException(status_code=404, detail="此分析無下載失敗記錄可重試")
    try:
        skipped = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        skipped = []
    if not skipped:
        raise HTTPException(status_code=404, detail="待重試清單為空")

    if _is_retry_skipped_inflight(analysis_id):
        raise HTTPException(status_code=409, detail="此分析已在重試中")

    started = start_retry_skipped(task_id, analysis_id, x_api_key)
    if not started:
        raise HTTPException(status_code=409, detail="重試啟動失敗（可能已在跑）")

    return {
        "analysis_id": analysis_id,
        "status": "retrying",
        "total": len(skipped),
    }


class QuickFollowupRequest(BaseModel):
    source_analysis_id: str = Field(description="基於哪個 analysis 的精讀結果")
    question: str = Field(min_length=1, description="追問內容")


@router.post("/tasks/{task_id}/quick-followup", status_code=200)
async def quick_followup(
    task_id: str,
    body: QuickFollowupRequest,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """快速追問：基於既有精讀摘要回答（1 次 Claude call，~5 秒）。

    不重跑 per-judgment 精讀，直接從 source_analysis 的 results 送 Claude。
    結果存為新 analysis（flow='quick'），顯示在 State C。
    """
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    source = await db.get_analysis(body.source_analysis_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source analysis not found")

    analysis_id = str(uuid.uuid4())
    await db.create_analysis(
        analysis_id=analysis_id,
        task_id=task_id,
        question=body.question,
        ai_read_field="quick_followup",
        status="running",
    )

    from src.pipeline.analyze import run_quick_followup
    synthesis = await run_quick_followup(
        analysis_id=analysis_id,
        source_analysis_id=body.source_analysis_id,
        question=body.question,
        original_question=source["question"],
        api_key=x_api_key,
    )
    await db.update_analysis(analysis_id, status="done",
                             match_count=len(synthesis.get("relevant_case_ids", [])))

    return {
        "analysis_id": analysis_id,
        "flow": "quick",
        "synthesis": synthesis,
    }


@router.post("/tasks/{task_id}/analyses/{analysis_id}/re-synthesis", status_code=200)
async def re_synthesis(
    task_id: str,
    analysis_id: str,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """只重跑 synthesis（不重跑精讀）— 精讀結果全部保留，只花 1 次 Claude call。

    用途：synthesis 因 JSONDecodeError 失敗時，律師點「重新生成摘要」。
    """
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    analysis = await db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")

    from src.pipeline.analyze import run_synthesis
    from src import sse_bus

    # 同步執行（synthesis 只要 1 次 Claude call，幾秒完成）
    synthesis = await run_synthesis(
        analysis_id=analysis_id,
        question=analysis["question"],
        api_key=x_api_key,
    )

    # 補 _usage：累積的 scoring tokens（DB）+ 這次 synthesis tokens。
    # run_synthesis 內部會 set_analysis_synthesis 寫入、但沒帶 _usage；這裡覆寫補完。
    syn_in = int(synthesis.get("_synth_input", 0) or 0)
    syn_out = int(synthesis.get("_synth_output", 0) or 0)
    a_now = await db.get_analysis(analysis_id) or {}
    s_in = int(a_now.get("scoring_input_tokens", 0) or 0)
    s_out = int(a_now.get("scoring_output_tokens", 0) or 0)
    cost_usd = (s_in * 0.80 + s_out * 4.00 + syn_in * 3.00 + syn_out * 15.00) / 1_000_000
    synthesis["_usage"] = {
        "scoring_input": s_in, "scoring_output": s_out,
        "synthesis_input": syn_in, "synthesis_output": syn_out,
        "total_cost_usd": round(cost_usd, 4),
    }
    # 保留 is_preliminary 原本的值（re-synthesis 常發生在 partial/done，不改 flag）
    await db.set_analysis_synthesis(
        analysis_id, synthesis,
        is_preliminary=bool(a_now.get("synthesis_is_preliminary", 0)),
    )

    # 推 SSE 通知前端刷新
    await sse_bus.publish(task_id, "stage3_synthesis_done", {
        "task_id": task_id,
        "analysis_id": analysis_id,
        "synthesis": synthesis,
    })

    return {"analysis_id": analysis_id, "synthesis": synthesis}
