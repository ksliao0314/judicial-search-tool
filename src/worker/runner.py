"""背景 worker：透過 `asyncio.create_task(dispatch_work(...))` 分派 work items、
推 SSE 事件。

Work item 類型（見 Stage*Work dataclasses）：
  stage1_search       — 純 MCP 廣搜，寫 task_search_hits
  stage25_fetch       — 律師指定 case_ids 抓全文
  reasoning_prefilter — 預抓全文比對 reasoning 關鍵字
  stage3_analyze      — fetch + Claude 精讀 + synthesis

併發控制：_stage_sem(5) 全域上限。Claude LLM 額外由 analyze.py 的 token bucket
（ITPM/RPM）跨 task 限流。
Server 重啟後，pending/running 任務 + stage25_inflight 自動恢復。
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.db import database as db
from src.pipeline import analyze as analyze_pipeline
from src.pipeline import filter as filter_pipeline
from src.pipeline import search as search_pipeline
from src.pipeline import synonym_expander
from src.pipeline.citation_normalizer import (
    Citation, parse_keyword, top_search_variants, generate_variants,
    ensure_law_known, _load_law_abbrev,
)
from src.utils import anomaly_log
from src import sse_bus, mcp_client
from src.mcp_client import PARSER_VERSION

logger = logging.getLogger(__name__)


async def sync_law_abbreviations_to_synonyms() -> None:
    """啟動時把 law_abbreviations.json 同步到 synonym_dictionary。

    每個法名全名 ↔ 簡稱寫成 confirmed synonym group，
    讓組合詞展開能自動處理「公平法第20條」→「公平交易法第20條」。
    """
    abbrev = _load_law_abbrev()
    if not abbrev:
        return
    count = 0
    for canonical, variants in abbrev.items():
        if not variants:
            continue
        # 全名 + 所有簡稱都是同一組的 variants（排除單字簡稱）
        all_variants = [canonical] + [v for v in variants if v != canonical and len(v) > 1]
        if len(all_variants) <= 1:
            continue  # 只有全名自己，沒有有效簡稱
        await db.upsert_synonyms(
            canonical=canonical,
            variants=all_variants,
            source="law_abbreviations",
        )
        # 確保都是 confirmed tier（手動設定 accept_count 到閾值）
        for v in all_variants:
            existing = await db.get_synonyms(canonical, min_tier="confirmed")
            already_confirmed = any(r["variant"] == v for r in existing)
            if not already_confirmed:
                # 連續 accept 3 次升 confirmed
                for _ in range(3):
                    await db.record_synonym_feedback(canonical, v, accepted=True)
        count += 1
    logger.info("法規簡稱同步完成：%d 組寫入同義詞庫", count)


async def sync_synonym_seed_to_dict() -> None:
    """啟動時把 `src/data/synonym_seed.json` 的非法名同義組同步進 synonym_dictionary。

    這些是專案預設的事務所確認同義字庫（tier=confirmed），新裝機就有。
    與 `sync_law_abbreviations_to_synonyms` 並列；法名簡稱走那條、一般法律用語
    （如「假處分 / 暫時處分」、「僱傭 / 雇傭」）走這條，避免污染法名字典。
    """
    from pathlib import Path
    seed_path = Path(__file__).resolve().parent.parent / "data" / "synonym_seed.json"
    if not seed_path.exists():
        return
    try:
        with seed_path.open(encoding="utf-8") as f:
            seed = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("synonym_seed.json 讀取失敗：%s", exc)
        return

    count = 0
    for canonical, variants in seed.items():
        if canonical.startswith("_"):  # 略過 _comment 等 metadata
            continue
        if not variants:
            continue
        all_variants = [canonical] + [v for v in variants if v != canonical and len(v) > 1]
        if len(all_variants) <= 1:
            continue
        await db.upsert_synonyms(
            canonical=canonical,
            variants=all_variants,
            source="synonym_seed",
        )
        for v in all_variants:
            existing = await db.get_synonyms(canonical, min_tier="confirmed")
            already_confirmed = any(r["variant"] == v for r in existing)
            if not already_confirmed:
                for _ in range(3):
                    await db.record_synonym_feedback(canonical, v, accepted=True)
        count += 1
    logger.info("同義詞種子同步完成：%d 組寫入同義詞庫", count)


# ---------------------------------------------------------------------------
# 兩階段搜尋 work items（新流程，見 SEARCH_REDESIGN.md）
# Legacy FullTaskWork / NewAnalysisWork 已移除（2026-04）— 0 筆 legacy task 在 DB 中。
# ---------------------------------------------------------------------------

@dataclass
class Stage1SearchWork:
    """Stage 1：純做 MCP search 寫 task_search_hits，不抓全文、不精讀。

    court / case_type 不接受（Stage 2 互動篩選）、但 **year_from / year_to 接受**：
    常見關鍵字（例如「詐欺」）若不限年度會撈到 5000+ 筆、MCP 500 上限切多輪仍很慢、
    律師其實不需要 30 年前的判決。首頁進階篩選新增年度 slider 允許壓在 Stage 1 server-side。

    main_text：律師若已知主文措辭（如「被告應給付」「撤銷原處分」），可在此先壓 server-side
    篩選，比拿全部回來再 client filter 快很多。理由欄司法院 search 不支援，需 stage 2.5 深篩。

    search_domain：'judgment'（FJUD 判決）或 'interpretation'（釋字+憲判字）。後者走
    不同 pipeline（`_run_stage1_search_interpretation`）—不展同義詞、不窮盡、不 main_text、不 year。
    """
    type: str = field(default="stage1_search", init=False)
    task_id: str = ""
    keyword: str = ""
    expand_keywords: bool = True
    exhaustive: bool = True
    main_text: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    api_key: str | None = None
    search_domain: str = "judgment"


@dataclass
class Stage3AnalyzeWork:
    """Stage 3 v2：對 narrow 後的 hits 子集做 fetch + per-judgment Claude 評分 + synthesis。

    v2 redesign (2026-04)：取消獨立 stage 2.5「深度篩選」步驟、拿掉 filter_fields/ai_read_fields
    picker。律師只需要輸入 NL 問題 + 是否讀事實。後端自動：
      1. 從 task_search_hits 依 narrow 篩出子集
      2. 對每筆 get_judgment 抓全文寫 task_judgments（含重用 cache）
      3. Claude 評分（score = 論述詳細度，0-10），寫入 analysis_results
      4. Synthesis：對 score>0 的跑一次 Claude 總結，寫入 analyses.synthesis

    narrow 目前支援 court_tiers / year_from / year_to。
    """
    type: str = field(default="stage3_analyze", init=False)
    task_id: str = ""
    analysis_id: str = ""
    question: str = ""
    read_facts: bool = False
    narrow: dict = field(default_factory=dict)
    api_key: str | None = None
    prefilter_case_ids: list[str] | None = None  # 理由預篩後的 case_ids
    reasoning_filter: bool = False  # 在 fetch loop 中即時比對 reasoning 含關鍵字


@dataclass
class Stage25FetchWork:
    """Stage 2.5：律師按「深度篩選」時，對指定 case_ids 抓全文寫入 task_judgments，
    不做字串過濾、不跑 Claude。前端拿到後可離線做內文細篩。

    fetch 失敗的 case_id 跳過（不影響後續流程，前端對缺漏判決保留 UI 提示）。
    已存在於 task_judgments 的 case_id 自動跳過（INSERT OR IGNORE）。
    """
    type: str = field(default="stage25_fetch", init=False)
    task_id: str = ""
    case_ids: list[str] = field(default_factory=list)
    api_key: str | None = None


@dataclass
class ReasoningPreFilterWork:
    """理由預篩：抓全文 + 比對 reasoning 欄位是否含關鍵字。
    勾選後立刻啟動，律師可同時輸入 NL 問題。"""
    type: str = field(default="reasoning_prefilter", init=False)
    task_id: str = ""
    narrow: dict = field(default_factory=dict)
    api_key: str | None = None


WorkItem = (
    Stage1SearchWork | Stage25FetchWork | Stage3AnalyzeWork | ReasoningPreFilterWork
)


# Stage 1 hard cap：超過此數律師應回頭加條件再搜，不該硬塞整個資料庫
# 5000 考量：Claude 總成本控制（~US$80 上限 worst case）、Stage 2.5 fetch 時間
# （60 req/min MCP bucket、5000 筆要 ~80 分鐘）、司法院 WAF 容忍度（連續 ~10 rounds MCP 搜尋）
STAGE1_HARD_CAP = 5000


# ---------------------------------------------------------------------------
# 任務取消（cooperative cancellation）
# ---------------------------------------------------------------------------

class TaskCancelledError(Exception):
    """律師中途刪除任務時 raise 此例外，由 _execute_work 的 except 攔下，
    跟一般失敗區分（不要再去 update_task(status='failed')，因為 row 已被刪）。
    """
    def __init__(self, task_id: str) -> None:
        super().__init__(f"task {task_id} cancelled")
        self.task_id = task_id


async def _check_task_alive(task_id: str) -> None:
    """檢查 task row 是否仍存在；若已被刪除，raise TaskCancelledError。

    在 pipeline 的主要邊界呼叫（search 前後、filter 前、analyze 前），
    讓 worker 在律師按下刪除後最遲於下一個邊界中止，避免：
      - 浪費更多 MCP / Claude API 呼叫
      - 對已刪除 task 的 task_judgments 做 INSERT 觸發 FK violation
    """
    if not await db.get_task(task_id):
        raise TaskCancelledError(task_id)


# Stage 2.5 深度篩選取消旗標：律師按「停止」時 API 加入 set，worker 每筆 fetch 前檢查。
# 跟 TaskCancelledError（砍整個 task）不同，這個只中止當下的 deep fetch，task 本身保留。
_cancelled_deep_fetches: set[str] = set()

def request_cancel_deep_fetch(task_id: str) -> None:
    _cancelled_deep_fetches.add(task_id)

def _is_deep_fetch_cancelled(task_id: str) -> bool:
    return task_id in _cancelled_deep_fetches

def _clear_deep_fetch_cancel(task_id: str) -> None:
    _cancelled_deep_fetches.discard(task_id)


# Preliminary synthesis finalize 旗標：律師按「就用現在結果定稿」→ API 加入 set。
# retry loop 每輪邊界檢查、檢到就中止剩餘 retry、把 preliminary 升格為 final。
_finalize_requested: set[str] = set()

def request_finalize_preliminary(analysis_id: str) -> None:
    _finalize_requested.add(analysis_id)

def _is_finalize_requested(analysis_id: str) -> bool:
    return analysis_id in _finalize_requested

def _clear_finalize_requested(analysis_id: str) -> None:
    _finalize_requested.discard(analysis_id)


# Graceful abort 旗標：律師按「中止並查看目前結果」→ API 加入 set。
# 生效點：
#   1) analyze.py _run_and_report 每筆進入前 check → return aborted dict、不跑 Claude
#   2) retry loop 每輪邊界 check → break（與 finalize 同機制）
# 中止後若 rows ≥ 3，跑 partial synthesis、status='partial'、is_preliminary=1、可 /resume
# rows < 3 則 status='cancelled'（維持舊語意）。兩者互斥於 finalize 的升格邏輯。
_graceful_abort_requested: set[str] = set()

def request_graceful_abort(analysis_id: str) -> None:
    _graceful_abort_requested.add(analysis_id)

def _is_graceful_abort(analysis_id: str) -> bool:
    return analysis_id in _graceful_abort_requested

def _clear_graceful_abort(analysis_id: str) -> None:
    _graceful_abort_requested.discard(analysis_id)


# Retry-skipped in-flight guard：防 double-click 同時 fire 兩個 retry worker
# 造成同 case 被重覆 fetch / scoring 寫 DB 衝突（UNIQUE(analysis_id, case_id) 會擋、
# 但先擋在 endpoint 層更乾淨）
_retry_skipped_inflight: set[str] = set()

def _is_retry_skipped_inflight(analysis_id: str) -> bool:
    return analysis_id in _retry_skipped_inflight


# 剩餘筆數門檻：達標時觸發 preliminary synthesis（total < 30 不觸發，太少沒收斂的價值）
def _preliminary_remaining_threshold(total: int) -> int | None:
    if total < 30:
        return None
    if total <= 150:
        return 10
    if total <= 500:
        return 20
    return 30


async def _fire_preliminary_synthesis(
    analysis_id: str,
    task_id: str,
    question: str,
    api_key: str | None,
    completed: int,
    total: int,
) -> None:
    """跑 preliminary synthesis、寫 DB with is_preliminary=1、推 SSE。失敗 swallow 不中斷主流程。

    此時 scoring 還在繼續跑，synthesis 只用當下的 analysis_results（score>0 的 row）。
    後續 main flow 跑完 retry 會再做一次 final synthesis 覆蓋。
    """
    logger.info("[%s] 觸發 preliminary synthesis (analysis=%s, completed=%d/%d)",
                task_id, analysis_id, completed, total)
    try:
        await sse_bus.publish(task_id, "stage3_synthesis_start", {
            "task_id": task_id, "analysis_id": analysis_id,
            "preliminary": True,
        })
        synthesis = await analyze_pipeline.run_synthesis(
            analysis_id=analysis_id, question=question, api_key=api_key,
        )
        # Preliminary usage 含「目前為止累積的 scoring tokens」（DB 讀、非 in-memory）
        # + synthesis 自己這趟。雖然 scoring 可能還在跑、律師看到的成本至少反映
        # 當下實際花費，而非僅 synthesis 那段。
        syn_in = synthesis.get("_synth_input", 0)
        syn_out = synthesis.get("_synth_output", 0)
        a_now = await db.get_analysis(analysis_id) or {}
        s_in = int(a_now.get("scoring_input_tokens", 0) or 0)
        s_out = int(a_now.get("scoring_output_tokens", 0) or 0)
        # Haiku pricing: $0.80/MTok in, $4.00/MTok out
        # Sonnet pricing: $3.00/MTok in, $15.00/MTok out
        cost_usd = (s_in * 0.80 + s_out * 4.00 + syn_in * 3.00 + syn_out * 15.00) / 1_000_000
        synthesis["_usage"] = {
            "scoring_input": s_in, "scoring_output": s_out,
            "synthesis_input": syn_in, "synthesis_output": syn_out,
            "total_cost_usd": round(cost_usd, 4),
        }
        await db.set_analysis_synthesis(analysis_id, synthesis, is_preliminary=True)
        await sse_bus.publish(task_id, "preliminary_synthesis_done", {
            "task_id": task_id, "analysis_id": analysis_id,
            "synthesis": synthesis,
            "completed": completed, "total": total,
        })
    except Exception as e:
        logger.warning("[%s] preliminary synthesis 失敗（不影響主流程）：%s", task_id, e)


async def _fire_abort_partial_synthesis(
    analysis_id: str,
    task_id: str,
    question: str,
    api_key: str | None,
) -> None:
    """律師按中止（命中 ≥ 3）→ 立即在背景跑 partial synthesis、不等 scoring in-flight 結束。

    律師 UX：按下中止 → 立刻看到「AI 綜合分析中」→ 5-15 秒後進 State C。
    Scoring 那邊的 8 個 Claude call 仍繼續跑（cooperative abort 擋不住已 in-flight）、
    但對律師透明 — SSE 觸發切換 State C 後、後續 scoring-end 的 graceful_abort 分支會
    detect synthesis 已寫 → skip 重複 synthesis。
    """
    try:
        analysis = await db.get_analysis(analysis_id) or {}
        match_count = int(analysis.get("match_count", 0) or 0)
        rows_done = await db.count_analysis_results(analysis_id)
        total = int(analysis.get("total", 0) or 0)

        await sse_bus.publish(task_id, "stage3_synthesis_start", {
            "task_id": task_id, "analysis_id": analysis_id, "preliminary": True,
        })
        synthesis = await analyze_pipeline.run_synthesis(
            analysis_id=analysis_id, question=question, api_key=api_key,
        )
        # P1-5：寫 DB 前再 check flag、若 /resume 在 synthesis 跑 5-15 秒期間清了旗、
        # 表示律師已經不要 partial、放棄 DB 升格（避免把 /resume 設的 running 覆寫成 partial）
        if not _is_graceful_abort(analysis_id):
            logger.info("[%s] abort fast synthesis 完成但 graceful_abort 旗已清（被 /resume 取消）、跳過 DB 升格", task_id)
            return
        # Token usage 累積：DB scoring tokens（scoring 可能還在跑、值偏低但律師看到的
        # 是當下真實花費）+ synthesis 自己這趟
        a_now = await db.get_analysis(analysis_id) or {}
        s_in  = int(a_now.get("scoring_input_tokens", 0)  or 0)
        s_out = int(a_now.get("scoring_output_tokens", 0) or 0)
        syn_in  = int(synthesis.get("_synth_input", 0)  or 0)
        syn_out = int(synthesis.get("_synth_output", 0) or 0)
        cost_usd = (s_in * 0.80 + s_out * 4.00 + syn_in * 3.00 + syn_out * 15.00) / 1_000_000
        synthesis["_usage"] = {
            "scoring_input": s_in, "scoring_output": s_out,
            "synthesis_input": syn_in, "synthesis_output": syn_out,
            "total_cost_usd": round(cost_usd, 4),
        }
        # 設計決策（2026-04-19）：任何中止都寫 partial，不論是否為 resume 中的再次中止。
        # 實測發現「resume 中再中止=升格 done」的原設計反直覺（律師以為還能繼續）。
        # 升格 done 只有兩條路：自然跑完 / 按「就用現在結果定稿」按鈕。
        await db.set_analysis_synthesis(analysis_id, synthesis, is_preliminary=True)
        await db.update_analysis(analysis_id, status="partial")
        await sse_bus.publish(task_id, "stage3_partial_done", {
            "task_id": task_id, "analysis_id": analysis_id,
            "done": rows_done, "total": total,
            "match_count": match_count, "is_final": False,
            "synthesis": synthesis,
        })
        logger.info("[%s] abort fast partial synthesis 完成 → partial（命中 %d/%d）",
                    task_id, match_count, rows_done)
    except Exception as e:
        logger.warning("[%s] abort fast partial synthesis 失敗：%s", task_id, e)
        # P0-2：synthesis 失敗 → 寫 fallback synthesis + 發 SSE 解除 FE「AI 綜合分析中…」卡死
        # 律師仍可 /resume（資料保留）、或關卡重開。status 仍寫 partial、不寫 failed、讓律師有路徑續跑。
        try:
            a_recover = await db.get_analysis(analysis_id) or {}
            mc = int(a_recover.get("match_count", 0) or 0)
            tot = int(a_recover.get("total", 0) or 0)
            rows = await db.count_analysis_results(analysis_id)
            fallback_syn = {
                "total_relevant": mc,
                "consensus": "不足",
                "summary": f"綜合分析產出失敗（{type(e).__name__}）。已分析 {rows} 筆結果保留、可選「繼續未完成的分析」或關閉後重開任務。",
                "clusters": [],
                "_fallback": True,
            }
            await db.set_analysis_synthesis(analysis_id, fallback_syn, is_preliminary=True)
            await db.update_analysis(analysis_id, status="partial")
            await sse_bus.publish(task_id, "stage3_partial_done", {
                "task_id": task_id, "analysis_id": analysis_id,
                "done": rows, "total": tot,
                "match_count": mc, "is_final": False,
                "synthesis": fallback_syn,
            })
        except Exception as recovery_e:
            logger.error("[%s] abort fast synthesis recovery 也失敗：%s", task_id, recovery_e)


# ---------------------------------------------------------------------------
# Worker 啟動
# ---------------------------------------------------------------------------

async def start_worker() -> None:
    """Server 啟動時跑：同步法規簡稱、恢復被中斷的任務（Stage 1/2.5/3）。
    Recovery 全走 `asyncio.create_task(dispatch_work(work))` — 與正常 API handler
    路徑一致、共用 _stage_sem(5) 全域併發上限、fire-and-forget（lifespan 不用等）。
    """
    await sync_law_abbreviations_to_synonyms()
    await sync_synonym_seed_to_dict()
    await _recover_pending_tasks()
    await _recover_stage25_inflight()
    await _recover_prefilter_inflight()


async def _recover_stage25_inflight() -> None:
    """Server 重啟時重跑被中斷的 Stage 2.5 fetch。

    INSERT OR IGNORE 讓重跑 idempotent（已抓好的 case_id 不會重複）。但會再打
    MCP 一次（走 MCP fork 30 天 file cache、通常命中、無司法院 HTTP 成本）。

    走 dispatch_work（不經 queue），與正常 API handler 流程一致、受同一組
    semaphore 約束。不會為了 recovery 爆 5 path 限制。
    """
    inflight = await db.list_stage25_inflight()
    if not inflight:
        return
    for rec in inflight:
        work = Stage25FetchWork(
            task_id=rec["task_id"],
            case_ids=rec["case_ids"],
            api_key=None,  # Stage 2.5 不呼叫 LLM、不需要 API key
        )
        asyncio.create_task(dispatch_work(work))
        logger.info(
            "恢復 Stage 2.5 fetch task=%s case_ids=%d 筆（started=%s）",
            rec["task_id"], len(rec["case_ids"]), rec["started_at"],
        )


async def _recover_prefilter_inflight() -> None:
    """Server 重啟時恢復被中斷的理由預篩。

    流程：
    - 掃 task_prefilter_results status='running' 的 row
    - 每筆 +1 recovery_attempts
    - 達 MAX_PREFILTER_RECOVERY_ATTEMPTS 就 mark 'cancelled'（律師介入）
    - 否則重派 ReasoningPreFilterWork，narrow 從 row 讀回

    Partial matched_case_ids 丟棄（由新 work 覆蓋 update_prefilter_progress）。
    """
    rows = await db.list_running_prefilters()
    if not rows:
        return
    for rec in rows:
        task_id = rec["task_id"]
        new_attempts = await db.increment_prefilter_attempts(task_id)

        if new_attempts >= db.MAX_PREFILTER_RECOVERY_ATTEMPTS:
            await db.mark_prefilter_cancelled(task_id)
            logger.warning(
                "[%s] prefilter recovery 達上限 %d，標記 cancelled（律師介入）",
                task_id, db.MAX_PREFILTER_RECOVERY_ATTEMPTS,
            )
            continue

        try:
            narrow_dict = json.loads(rec["narrow"]) if rec["narrow"] else {}
        except json.JSONDecodeError:
            narrow_dict = {}
        work = ReasoningPreFilterWork(
            task_id=task_id,
            narrow=narrow_dict,
            api_key=None,
        )
        asyncio.create_task(dispatch_work(work))
        logger.info(
            "[%s] 恢復 prefilter（第 %d/%d 次 recovery）",
            task_id, new_attempts, db.MAX_PREFILTER_RECOVERY_ATTEMPTS,
        )


async def _recover_pending_tasks() -> None:
    """Server 重啟後重新 dispatch pending/running 任務。

    走 asyncio.create_task(dispatch_work)，與正常 API handler 一致享受
    _stage_sem(5) 全域併發上限（recovery 10 個任務可並行消化，不會序列化慢跑）。

    task_search_hits 為空 → 重跑 Stage1SearchWork（stage 1 還沒完成）
    task_search_hits 已有 → 對每個 pending/running analysis 依 env var 決定
                             重跑 Stage3AnalyzeWork 或 mark failed
    """
    pending = await db.get_pending_tasks()
    for task in pending:
        try:
            sp = json.loads(task.get("search_params") or "{}")
        except (json.JSONDecodeError, TypeError):
            sp = {}
        await _recover_new_task(task, sp)


async def _recover_new_task(task: dict, sp: dict) -> None:
    """新流程 recovery：依 task_search_hits 是否已填判斷重啟 stage 1 還是 stage 3。"""
    hits_count = await db.count_task_search_hits(task["id"])

    if hits_count == 0:
        # Stage 1 還沒完成（或剛開始就被中斷）→ 重新跑 stage 1
        work: WorkItem = Stage1SearchWork(
            task_id=task["id"],
            keyword=task["keyword"],
            expand_keywords=sp.get("expand_keywords", True),
            exhaustive=sp.get("exhaustive", True),
            main_text=sp.get("main_text"),
            year_from=sp.get("year_from"),
            year_to=sp.get("year_to"),
        )
        asyncio.create_task(dispatch_work(work))
        logger.info("恢復新任務 %s stage 1", task["id"])
        return

    # Stage 1 已完成；對每個 pending/running analysis 嘗試恢復。
    # v2 後只需要 question + narrow + read_facts，filter_fields/ai_read_fields 已 deprecated
    #
    # Recovery 策略（2026-04 起）：
    #   1. 若 env var ANTHROPIC_API_KEY 存在 → 用它 re-queue（Production systemd /
    #      docker 部署會設這個 env var、恢復完全自動化、律師不用做事）
    #   2. 若無 env var → mark failed（開發環境常見）；前端 loadHomeTasks 會偵測
    #      最近失敗 + localStorage 有 key → 自動打 /retry 恢復，使用者也不用做事
    #
    # 只有「env var 沒設 + 瀏覽器沒開 + localStorage 沒 key」三件事同時才真的
    # 需要律師手動點。
    import os as _os
    env_api_key = _os.environ.get("ANTHROPIC_API_KEY")
    analyses = await db.list_analyses(task["id"])
    for analysis in analyses:
        if analysis["status"] not in ("pending", "running"):
            continue
        if env_api_key:
            try:
                narrow = json.loads(analysis.get("narrow_state") or "{}")
            except (json.JSONDecodeError, TypeError):
                narrow = {}
            ai_read = (analysis.get("ai_read_field") or "").split(",")
            read_facts = "facts" in ai_read
            work = Stage3AnalyzeWork(
                task_id=task["id"],
                analysis_id=analysis["id"],
                question=analysis["question"],
                read_facts=read_facts,
                narrow=narrow,
                api_key=env_api_key,
            )
            asyncio.create_task(dispatch_work(work))
            logger.info(
                "[%s] 從 ANTHROPIC_API_KEY env var 恢復 stage 3 分析 %s",
                task["id"], analysis["id"],
            )
        else:
            await db.update_analysis(analysis["id"], status="failed")
            logger.warning(
                "[%s] stage 3 分析 %s recovery 略過（env var 未設 + 無 per-request "
                "key）→ mark failed。前端偵測 localStorage 有 key 時會自動 /retry；"
                "否則請從 UI 手動重觸發",
                task["id"], analysis["id"],
            )


# ---------------------------------------------------------------------------
# Dispatch（併發執行，取代 task_queue + worker_loop 序列化架構）
# ---------------------------------------------------------------------------
#
# 設計原則：
#   - 所有路徑（API handler + recovery）走 `asyncio.create_task(dispatch_work)`
#   - `_stage_sem(3)` 控制同時跑的 work 數上限（MCP 實際 serialized，3 slot 足夠且
#     更容易診斷；太大會讓律師更難辨識哪個 work 卡住）
#   - LLM 額外由 analyze.py 的 token bucket（ITPM/RPM）跨 task 限流
#
# 好處：LLM 不會因為並行而爆量、fetch/cache 可平行加速、單 task 死亡不卡其他 task。
_STAGE_CONCURRENCY = 3
_stage_sem: asyncio.Semaphore | None = None

# Worker timeout：4 小時（律師最大 task ~1000 筆精讀需 40-90 分、多任務競爭 rate limit
# 可能拉到 2-3 小時、4 hr 是保守 ceiling 防真 stuck 占 slot）。
# Stage3 timeout 時會走 graceful abort path（見 dispatch_work 的 TimeoutError handler）、
# 產 partial synthesis 給律師救回已分析結果；其他 stage timeout 維持原 mark failed 行為。
_WORK_TIMEOUT_SEC = 4 * 60 * 60

# 活躍 work registry — 供 /api/debug/workers 查詢、kill-worker endpoint 找 target。
# Key = 自動生成的 work_id (uuid)；Value = {task_id, type, started_at, task_obj}。
# task_obj 是 asyncio.Task，kill-worker 直接對它 cancel()。
# 進 _execute_work 前 register、finally unregister（保證 clean）。
_active_workers: dict[str, dict] = {}


def _get_stage_sem() -> asyncio.Semaphore:
    """延遲初始化 semaphore — asyncio.Semaphore 在 no running loop 時建立會 bind
    到錯的 loop，所以模組 import 時不能建。"""
    global _stage_sem
    if _stage_sem is None:
        _stage_sem = asyncio.Semaphore(_STAGE_CONCURRENCY)
    return _stage_sem


def get_workers_snapshot() -> dict:
    """供 /api/debug/workers 用。回傳 sem 狀態 + 活躍 work 清單。

    sem._value 是 CPython 內部屬性，正式 API 沒有 expose。用 try/except 防 version 變動。
    sem._waiters 是 deque of Future，len 等於排隊中的 acquire call 數量。
    """
    sem = _stage_sem
    try:
        sem_value = sem._value if sem else _STAGE_CONCURRENCY  # type: ignore[union-attr]
        waiters = len(sem._waiters) if sem and hasattr(sem, "_waiters") and sem._waiters else 0  # type: ignore[union-attr]
    except Exception:
        sem_value, waiters = _STAGE_CONCURRENCY, 0
    return {
        "sem_capacity": _STAGE_CONCURRENCY,
        "sem_available": sem_value,
        "waiters": waiters,
        "work_timeout_sec": _WORK_TIMEOUT_SEC,
        "active": [
            {
                "work_id": wid,
                "task_id": info["task_id"],
                "type": info["type"],
                "started_at": info["started_at"],
                "age_sec": int((datetime.now(timezone.utc) - datetime.fromisoformat(info["started_at"])).total_seconds()),
            }
            for wid, info in _active_workers.items()
        ],
    }


def register_worker(task_id: str, work_type: str) -> str:
    """給 _bg_stage1 / _bg_prefilter 這類不走 dispatch_work 的特殊路徑用。
    回傳 work_id；呼叫端記得 finally 時 unregister_worker(work_id)。

    用法：
        work_id = register_worker(task_id, "stage1_search")
        try:
            await asyncio.wait_for(..., timeout=_WORK_TIMEOUT_SEC)
        finally:
            unregister_worker(work_id)
    """
    import uuid
    work_id = uuid.uuid4().hex[:12]
    _active_workers[work_id] = {
        "task_id": task_id,
        "type": work_type,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "task_obj": asyncio.current_task(),
    }
    return work_id


def unregister_worker(work_id: str) -> None:
    _active_workers.pop(work_id, None)


WORK_TIMEOUT_SEC = _WORK_TIMEOUT_SEC  # 對外 export 供 API layer 用


async def kill_workers_for_task(task_id: str) -> int:
    """砍掉某 task 所有 inflight work，回傳被砍的 work 數量。

    每個 work 的 asyncio.Task.cancel() 會觸發 CancelledError →
    _execute_work 的 except 走 _notify_work_cancelled（mark failed + SSE）。
    Sem 由 finally release、後續 task 自然 unblock。
    """
    killed = 0
    for wid, info in list(_active_workers.items()):
        if info["task_id"] != task_id:
            continue
        task_obj = info.get("task_obj")
        if task_obj and not task_obj.done():
            task_obj.cancel()
            killed += 1
            logger.warning("[%s] kill-worker 取消 work %s (%s)", task_id, wid, info["type"])
    return killed


async def _notify_work_cancelled(task_id: str, analysis_id: str | None) -> None:
    """CancelledError 路徑的 best-effort 清理：
    analysis 轉 failed、推 analysis_failed SSE。
    DB / SSE 任一步異常都只 log，不往上拋（呼叫端馬上要 re-raise cancel）。

    不推 publish_done — task 下可能還有其他 analysis 在跑，
    sentinel 會誤關整個 SSE 訂閱通道。
    """
    try:
        if analysis_id:
            await db.update_analysis(analysis_id, status="failed")
        if task_id:
            await sse_bus.publish(task_id, "analysis_failed", {
                "task_id": task_id,
                "analysis_id": analysis_id or "",
                "error": "分析已中止（server 重啟或 task cancel）",
            })
    except Exception:
        logger.exception("[%s] CancelledError 通知失敗", task_id)


async def _execute_work(work: "WorkItem") -> None:
    """Dispatch 單一 work item 到對應 _run_* 函式，含統一錯誤處理。

    Semaphore 由 dispatch_work 外層負責，這個函式本身不拿 sem。
    """
    try:
        if work.type == "stage1_search":
            await _run_stage1_search(work)  # type: ignore[arg-type]
        elif work.type == "stage25_fetch":
            await _run_stage25_fetch(work)  # type: ignore[arg-type]
        elif work.type == "reasoning_prefilter":
            await _run_reasoning_prefilter(work)  # type: ignore[arg-type]
        elif work.type == "stage3_analyze":
            await _run_stage3_analyze(work)  # type: ignore[arg-type]
        else:
            logger.error("未知 work 類型：%r，跳過", work.type)
    except asyncio.CancelledError:
        # cancel 前 best-effort 通知前端 + 標 analysis failed。
        # 用 shield 讓 cleanup task 不會被外層 cancel 再打斷；shield 自身被 cancel
        # 時 cleanup 仍會在背景完成（若 event loop 還活著）。
        analysis_id = getattr(work, "analysis_id", None)
        task_id = getattr(work, "task_id", None)
        if task_id:
            try:
                await asyncio.shield(_notify_work_cancelled(task_id, analysis_id))
            except asyncio.CancelledError:
                pass
        raise
    except TaskCancelledError as exc:
        # 律師主動刪除任務 → 不要再 update_task / update_analysis
        # （row 已不存在；UPDATE 雖然 no-op 但 SSE 也已沒人聽）。直接跳到下一筆。
        logger.info("任務 %s 已被律師刪除，中止當前 work item", exc.task_id)
    except Exception as exc:
        logger.exception("工作項目執行失敗：%s", exc)
        # 標記對應 analysis 為 failed + 推 SSE 通知前端
        try:
            analysis_id = getattr(work, "analysis_id", None)
            task_id = getattr(work, "task_id", None)
            error_msg = str(exc)[:200]
            if analysis_id:
                await db.update_analysis(analysis_id, status="failed")
            if task_id:
                # 只有在沒有其他 running/pending analysis 時才標 task failed
                # （避免一個 analysis 失敗連帶影響其他 analysis）
                analyses = await db.list_analyses(task_id)
                other_active = any(
                    a["id"] != analysis_id and a["status"] in ("pending", "running")
                    for a in analyses
                )
                if not other_active:
                    await db.update_task(task_id, status="failed")
                # 推 SSE 讓前端即時顯示失敗狀態。
                # 不推 publish_done — task 下可能還有其他 analysis/fetch 在跑，
                # sentinel 會誤關整個 SSE 訂閱通道。前端 analysis_failed handler
                # 不會主動 close SSE，讓連線繼續收同 task 其他事件。
                await sse_bus.publish(task_id, "analysis_failed", {
                    "task_id": task_id,
                    "analysis_id": analysis_id or "",
                    "error": error_msg,
                })
        except Exception:
            pass


async def dispatch_work(work: "WorkItem") -> None:
    """API handler 用這個繞開 task_queue。拿 semaphore 保證全域併發上限。

    用法：`asyncio.create_task(runner.dispatch_work(work))` — 立刻返回給 HTTP
    response，work 在背景 sem 控制下執行。

    Registry + timeout 機制：
      - 拿到 sem 後先註冊到 _active_workers（GET /api/debug/workers 可見）
      - 用 asyncio.wait_for 包 _WORK_TIMEOUT_SEC，超時自動 cancel
      - Finally 保證 unregister，不論 work 正常結束、timeout、還是被 kill-worker cancel
    """
    import uuid
    work_id = uuid.uuid4().hex[:12]
    current = asyncio.current_task()
    async with _get_stage_sem():
        _active_workers[work_id] = {
            "task_id": getattr(work, "task_id", "") or "",
            "type": work.type,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "task_obj": current,
        }
        try:
            await asyncio.wait_for(_execute_work(work), timeout=_WORK_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            # Timeout 觸發 inner cancel → _execute_work 的 CancelledError handler
            # 已做 notify_work_cancelled（mark failed + SSE）。
            task_id = getattr(work, "task_id", "?")
            logger.error(
                "[%s] work %s (%s) 超時 %d 秒，已強制中止",
                task_id, work_id, work.type, _WORK_TIMEOUT_SEC,
            )
            # Stage3 timeout 特殊處理：已有 ≥3 match 的話、把 status=failed 改寫成 partial、
            # 並 fire partial synthesis；律師還能看到已分析的結果、可以 /resume 補齊或 /finalize
            # （見設計決策：timeout 不應該讓律師白白失去已分析的資料）
            if isinstance(work, Stage3AnalyzeWork):
                try:
                    a_now = await db.get_analysis(work.analysis_id) or {}
                    match_count = int(a_now.get("match_count", 0) or 0)
                    if match_count >= 3:
                        logger.info("[%s] Stage3 timeout recovery: match=%d ≥ 3、fire partial synthesis",
                                    task_id, match_count)
                        request_graceful_abort(work.analysis_id)  # _fire_abort_partial_synthesis 會 self-check
                        asyncio.create_task(_fire_abort_partial_synthesis(
                            analysis_id=work.analysis_id,
                            task_id=work.task_id,
                            question=work.question,
                            api_key=work.api_key,
                        ))
                except Exception as exc:
                    logger.warning("[%s] Stage3 timeout recovery 失敗：%s", task_id, exc)
        finally:
            _active_workers.pop(work_id, None)


# _worker_loop 已移除（2026-04 refactor）：所有路徑（API handler + recovery）
# 改走 `asyncio.create_task(dispatch_work(work))`，統一 _stage_sem 控併發。
# task_queue + worker_loop 變成 dead code、整個刪除。


# ---------------------------------------------------------------------------
# Keyword 展開輔助：決定走 citation 路徑還是 synonym 路徑
# ---------------------------------------------------------------------------

async def _expand_compound_synonyms(kw: str) -> list[str]:
    """組合詞同義詞展開：掃描 kw 中的子字串，替換為 confirmed 同義詞群組的變體。

    範例：
      詞庫有 僱傭 → [僱傭, 雇傭, 僱庸]
      輸入「雇傭契約」→ 回傳 [「雇傭契約」,「僱傭契約」,「僱庸契約」]

    設計原則：
      1. 最長匹配優先（避免「公平交易法」被拆成「公平」+「交易法」）
      2. 同一位置只匹配一次（不重疊）
      3. 多處匹配做 cartesian product，但上限 MAX_COMPOUND_VARIANTS 筆
      4. 原始輸入永遠在回傳列表第一位
    """
    MAX_COMPOUND_VARIANTS = 10

    groups = await db.get_confirmed_synonym_groups()
    if not groups:
        return [kw]

    # 建反向索引：每個 variant 文字 → 它所屬的同義詞群組（所有替換選項）
    # 含 canonical 本身也是可被匹配的 variant
    variant_to_group: dict[str, list[str]] = {}
    for canon, variants in groups.items():
        for v in variants:
            variant_to_group[v] = variants

    # 收集所有可匹配的 variant 文字，按長度降序（最長優先匹配）
    all_variants = sorted(variant_to_group.keys(), key=len, reverse=True)

    # 掃描 kw，找出所有不重疊的匹配位置
    # 每個匹配：(start, end, group_variants)
    matches = []
    used = [False] * len(kw)  # 標記已被匹配的字元位置

    for v in all_variants:
        if len(v) < 2:
            continue  # 跳過單字（太容易誤匹配，例如「民」）
        start = 0
        while True:
            pos = kw.find(v, start)
            if pos == -1:
                break
            end = pos + len(v)
            # 檢查這段位置是否已被更長的匹配佔用
            if not any(used[pos:end]):
                # 確認不是匹配到自己（群組裡只有自己一個就不用展開）
                group = variant_to_group[v]
                if len(group) > 1:
                    matches.append((pos, end, group))
                    for i in range(pos, end):
                        used[i] = True
            start = pos + 1

    if not matches:
        return [kw]

    # 依位置排序
    matches.sort(key=lambda m: m[0])

    # 把 kw 拆成固定段 + 可替換段，做 cartesian product
    # 例：「雇傭契約糾紛」 matches=[(0,2,['僱傭','雇傭','僱庸'])]
    # segments = [可替換['僱傭','雇傭','僱庸'], 固定'契約糾紛']
    segments = []  # list of (str | list[str])
    last_end = 0
    for start, end, group in matches:
        if start > last_end:
            segments.append(kw[last_end:start])  # 固定段
        segments.append(group)  # 可替換段
        last_end = end
    if last_end < len(kw):
        segments.append(kw[last_end:])  # 尾部固定段

    # Cartesian product
    import itertools
    replaceable_indices = [i for i, s in enumerate(segments) if isinstance(s, list)]
    replaceable_options = [segments[i] for i in replaceable_indices]

    # 計算組合數，超限就截斷每組的選項
    total_combos = 1
    for opts in replaceable_options:
        total_combos *= len(opts)
    if total_combos > MAX_COMPOUND_VARIANTS:
        # 每組等比例截斷
        import math
        max_per = max(2, int(math.pow(MAX_COMPOUND_VARIANTS, 1 / len(replaceable_options))))
        replaceable_options = [opts[:max_per] for opts in replaceable_options]

    results = []
    for combo in itertools.product(*replaceable_options):
        parts = list(segments)
        for idx, replacement in zip(replaceable_indices, combo):
            parts[idx] = replacement
        results.append(''.join(parts))

    # 確保原始輸入在最前
    if kw in results:
        results.remove(kw)
    results.insert(0, kw)

    return results[:MAX_COMPOUND_VARIANTS]


async def _expand_keyword(
    kw: str, api_key: str | None,
) -> tuple[list[str], list[str], Citation | None]:
    """
    回傳 (search_variants, filter_variants, citation_query)

    決策邏輯（兩層展開）：
      1. 組合詞同義詞展開：掃描子字串替換 confirmed 同義詞
         例：「公平法第20條」→ [「公平法第20條」,「公平交易法第20條」]
      2. 對每個結果嘗試法條解析：成功則生成條號變體
         例：「公平交易法第20條」→ [「第20條」,「第二十條」, ...]
      3. 合併去重
    """
    # 法規簡稱已在啟動時同步到同義詞庫（sync_law_abbreviations_to_synonyms），
    # 不再需要 ensure_law_known 的 LLM fallback。

    # Step 1: 組合詞同義詞展開
    compound_variants = await _expand_compound_synonyms(kw)

    # Step 2: 對每個 variant 嘗試法條解析
    all_search: list[str] = []
    all_filter: list[str] = []
    first_citation: Citation | None = None

    for variant in compound_variants:
        citation = parse_keyword(variant)
        if citation is not None:
            sv = top_search_variants(citation, limit=5)
            fv = generate_variants(citation, with_law_prefix=True)
            if citation.law is not None:
                fv = fv + generate_variants(citation, with_law_prefix=False)
            all_search.extend(sv)
            all_filter.extend(fv)
            if first_citation is None:
                first_citation = citation
        else:
            all_search.append(variant)
            all_filter.append(variant)

    # 去重（保留順序）
    seen_s: set[str] = set()
    search_v = [v for v in all_search if v not in seen_s and not seen_s.add(v)]
    seen_f: set[str] = set()
    filter_v = [v for v in all_filter if v not in seen_f and not seen_f.add(v)]

    # 搜尋變體限制數量（rate-limit 敏感）
    search_v = search_v[:8]

    return search_v, filter_v, first_citation


# ---------------------------------------------------------------------------
# 兩階段搜尋 — Stage 1（純 MCP 廣搜，寫 task_search_hits）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 兩階段搜尋 — Stage 2.5（律師按「深度篩選」抓全文，無字串過濾、無 Claude）
# ---------------------------------------------------------------------------

async def _try_reuse_cached_judgment(task_id: str, case_id: str) -> dict | None:
    """跨 task cache 複用：若其他 task 已抓過同 case_id 且 parser_version + TTL 都符合，
    複製欄位寫一筆到當前 task，回傳 judgment-shaped dict（與 mcp_client.get_judgment 同 shape）。
    否則回傳 None，呼叫端自行打 MCP。

    - 省 MCP subprocess IPC + re-parse + extract_citations + anomaly_log，~0.1-0.5s/筆
    - cache hit 不呼叫 anomaly_log：parser 結果相同，重複寫只會壞 jq 統計
    - extracted_citations 從 DB 讀既存 tuples、不重跑 extract_citations
    """
    cached = await db.find_cached_judgment(case_id, PARSER_VERSION)
    if cached is None:
        return None

    def _safe_json_load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    cached_cited = _safe_json_load(cached["cited_statutes"], [])
    cached_citations = _safe_json_load(cached["extracted_citations"], [])
    cached_judges = _safe_json_load(cached["judges"], None) if cached["judges"] else None
    cached_parties = _safe_json_load(cached["parties"], None) if cached["parties"] else None

    await db.create_task_judgment(
        task_id=task_id,
        case_id=cached["case_id"],
        court=cached["court"],
        date=cached["date"],
        source_url=cached["source_url"],
        reasoning=cached["reasoning"],
        main_text=cached["main_text"],
        facts=cached["facts"],
        cited_statutes=cached_cited,
        full_text=cached["full_text"],
        extracted_citations=cached_citations,
        judges=cached_judges,
        parties=cached_parties,
        cause=cached["cause"],
        parser_version=PARSER_VERSION,
    )
    # 回傳 judgment-shaped dict（mcp_client.get_judgment 同 shape）
    return {
        "case_id": cached["case_id"],
        "court": cached["court"],
        "date": cached["date"],
        "source_url": cached["source_url"],
        "reasoning": cached["reasoning"],
        "main_text": cached["main_text"],
        "facts": cached["facts"],
        "cited_statutes": cached_cited,
        "full_text": cached["full_text"],
        "judges": cached_judges,
        "parties": cached_parties,
        "cause": cached["cause"],
    }


async def _run_stage25_fetch(work: Stage25FetchWork) -> None:
    """對指定 case_ids 抓全文 + 寫 task_judgments，給律師做內文細篩用。

    - 每筆 fetch 前先查跨 task cache（_try_reuse_cached_judgment）：同 case_id + 同
      parser_version + 30 天內曾抓過 → 直接複製欄位到當前 task，跳過 MCP / re-parse /
      citation extraction / anomaly log。cache miss 才走原本流程。
    - 每筆 fetch 前檢查 cancel 旗標（律師按「停止」時最遲 1-2s 內中止）
    - 每 3 筆（或 total 小時每筆）publish 一次 stage25_progress，前端畫進度條
    - 完成推 stage25_done；被取消推 stage25_cancelled
    """
    from src.pipeline.citation_extractor import extract_citations
    from src.pipeline.filter import _fetch_one

    task_id = work.task_id
    await _check_task_alive(task_id)
    _clear_deep_fetch_cancel(task_id)   # 清掉前次可能留下的 stale flag

    total = len(work.case_ids)
    if total == 0:
        await sse_bus.publish(task_id, "stage25_done", {
            "task_id": task_id, "fetched": 0, "total": 0, "skipped": 0, "reused": 0,
        })
        return

    logger.info("[%s] stage25 deep fetch %d 筆", task_id, total)
    # 小 task 每筆都推，大 task 每 5 筆推（減流量，保留順暢進度條）
    PROGRESS_EVERY = 1 if total <= 30 else 5
    fetched = 0
    skipped = 0
    reused = 0     # 跨 task cache 命中計數（本次 task 直接複用既存 task_judgments row）
    cancelled = False
    task_cancelled_exc: TaskCancelledError | None = None   # 任務整個被刪 → gather 後 re-raise

    # 並行抓取（原為序列逐筆）。global _mcp_fetch_bucket(60/min) 仍序列化真正的 MCP
    # 呼叫，並行主要讓 cache-hit 與司法院回應延遲重疊 → cold-fetch 從「逐筆 latency-bound」
    # 拉到「bucket 上限 bound」（典型 ~3-4x）。並發 create_task_judgment 寫入由 _conn 的
    # busy_timeout 護住。
    FETCH25_CONCURRENCY = 5
    fetch_sem = asyncio.Semaphore(FETCH25_CONCURRENCY)

    async def _emit_progress() -> None:
        # 完成順序非確定但 fetched 單調遞增；最後一筆（fetched==total）必觸發
        if fetched % PROGRESS_EVERY == 0 or fetched == total:
            await sse_bus.publish(task_id, "stage25_progress", {
                "task_id": task_id, "fetched": fetched, "total": total, "reused": reused,
            })

    async def _fetch_one_case(case_id: str) -> None:
        nonlocal fetched, skipped, reused, cancelled, task_cancelled_exc
        if cancelled or _is_deep_fetch_cancelled(task_id):
            cancelled = True
            return
        async with fetch_sem:
            if cancelled or _is_deep_fetch_cancelled(task_id):
                cancelled = True
                return
            try:
                await _check_task_alive(task_id)
            except TaskCancelledError as exc:
                task_cancelled_exc = exc    # 任務整個被刪 → gather 後 re-raise 交 worker loop
                cancelled = True
                return

            # === 跨 task cache lookup ===
            # cache 複用內含 create_task_judgment 寫入；並發下若撞 SQLITE_BUSY/UNIQUE/
            # 磁碟錯，gather(return_exceptions=True) 會靜默吞掉 → 該筆判決無聲漏（序列版
            # 會大聲 crash）。比照 cold-fetch 寫入失敗：記 skipped、續跑成可重試的 skip。
            try:
                cached_judgment = await _try_reuse_cached_judgment(task_id, case_id)
            except Exception as exc:
                logger.warning("[%s] stage25 cache 複用 %s 失敗：%s", task_id, case_id, exc)
                skipped += 1
                fetched += 1
                await _emit_progress()
                return
            if cached_judgment is not None:
                reused += 1
                fetched += 1
                await _emit_progress()
                return

            try:
                judgment = await _fetch_one(case_id)
            except Exception as exc:
                logger.warning("[%s] stage25 跳過 %s：%s", task_id, case_id, exc)
                skipped += 1
                fetched += 1
                await _emit_progress()
                return

            try:
                ft = judgment.get("full_text") or ""
                extracted = extract_citations(ft) if ft else []
                ec_serialized = [list(c.as_tuple()) for c in extracted] if extracted else None

                cited = judgment.get("cited_statutes")
                if isinstance(cited, str):
                    try:
                        cited = json.loads(cited)
                    except json.JSONDecodeError:
                        cited = [cited] if cited else []

                await db.create_task_judgment(
                    task_id=task_id,
                    case_id=judgment.get("case_id", case_id),
                    court=judgment.get("court", ""),
                    date=judgment.get("date", ""),
                    source_url=judgment.get("source_url", ""),
                    reasoning=judgment.get("reasoning"),
                    main_text=judgment.get("main_text"),
                    facts=judgment.get("facts"),
                    cited_statutes=cited,
                    full_text=judgment.get("full_text"),
                    extracted_citations=ec_serialized,
                    judges=judgment.get("judges"),
                    parties=judgment.get("parties"),
                    cause=judgment.get("cause"),
                    parser_version=PARSER_VERSION,
                )
                # 記錄 parse 結構異常（不阻塞 fetch；無 anomaly 不寫）
                await anomaly_log.log_judgment(judgment, task_id=task_id, jid=case_id)
            except Exception as exc:
                logger.warning("[%s] stage25 寫入 %s 失敗：%s", task_id, case_id, exc)
                skipped += 1
                fetched += 1
                await _emit_progress()
                return

            fetched += 1
            await _emit_progress()

    await asyncio.gather(
        *[_fetch_one_case(cid) for cid in work.case_ids],
        return_exceptions=True,
    )
    if task_cancelled_exc is not None:
        raise task_cancelled_exc   # 任務被刪：交給 worker loop 的 except 處理

    _clear_deep_fetch_cancel(task_id)
    # 清除 inflight 紀錄（無論 cancel / done）：標示「這個 task 的 fetch 不需要再
    # 被 server 重啟 recovery 重跑」。若本函式 raise 出錯沒走到這行、inflight
    # row 會留著 → 下次啟動會重新 dispatch（idempotent 因為 INSERT OR IGNORE）。
    await db.clear_stage25_inflight(task_id)

    if cancelled:
        await sse_bus.publish(task_id, "stage25_cancelled", {
            "task_id": task_id,
            "fetched": fetched,
            "total": total,
            "skipped": skipped,
            "reused": reused,
        })
        logger.info("[%s] stage25 已取消，完成 %d / %d（跳過 %d，重用 %d）",
                    task_id, fetched, total, skipped, reused)
        return

    await sse_bus.publish(task_id, "stage25_done", {
        "task_id": task_id,
        "fetched": fetched,
        "total": total,
        "skipped": skipped,
        "reused": reused,
    })
    logger.info("[%s] stage25 完成 %d / %d（跳過 %d，重用 %d）",
                task_id, fetched, total, skipped, reused)


# ---------------------------------------------------------------------------
# 兩階段搜尋 — Stage 1（純 MCP 廣搜，寫 task_search_hits）
# ---------------------------------------------------------------------------

# OR 語法分隔：接受三種寫法
#   - 半形 `|` / 全形 `｜`(U+FF5C)：可選空白
#   - ` OR ` / ` or `（前後有空白）
#   - `CJKorCJK` / `CJKORCJK`（兩側皆為中日韓字、無空白亦視為分隔；這對應律師
#     中文輸入習慣，打 `地位or相對` 不加空格時仍需拆開）
# separator 純前端語法、絕不送 MCP
import re as _re_orsep
_OR_SEP_RE = _re_orsep.compile(
    r'\s*[|｜]\s*'
    r'|\s+(?:[Oo][Rr])\s+'
    r'|(?<=[\u4e00-\u9fff])(?:[Oo][Rr])(?=[\u4e00-\u9fff])'
)

def _parse_or_groups(query: str) -> list[str]:
    """把 user 輸入切成 OR 群組。
    例：
      「A B」            → ["A B"]
      「A|B」「A OR B」  → ["A", "B"]
      「A B | C D」      → ["A B", "C D"]
    各 group 內部空格保留 → 後續 .split() 當 AND keyword 處理。
    """
    if not query:
        return []
    groups = [g.strip() for g in _OR_SEP_RE.split(query) if g.strip()]
    return groups


def _flatten_keywords(query: str) -> list[str]:
    """抽出 user 輸入的所有實際 keyword（剝掉 OR/| separator）。
    「A B | C D」→ ["A", "B", "C", "D"]
    下游 filter / reader highlight 用這個取代 .split()，避免把 "OR" / "|" 誤當 keyword。
    """
    groups = _parse_or_groups(query)
    if not groups:
        return []
    out: list[str] = []
    for g in groups:
        out.extend(kw.strip() for kw in g.split() if kw.strip())
    return out


async def _run_stage1_search(work: Stage1SearchWork) -> None:
    task_id = work.task_id

    # Dispatch by search_domain — 憲法解釋模式走獨立 pipeline
    if work.search_domain == "interpretation":
        await _run_stage1_search_interpretation(work)
        return

    started = datetime.now(timezone.utc).isoformat()

    await _check_task_alive(task_id)
    await db.update_task(task_id, status="running", started_at=started)

    # Step 1: keyword 展開
    # 語法：
    #   「A B」             → AND：A 且 B 都要出現
    #   「A|B」「A OR B」   → OR：各自獨立當一個 group、結果 union（分隔線前端指令、不送 MCP）
    #   「A B | C D」       → (A AND B) OR (C AND D) — 群內 AND、群間 OR
    # 同義/法條變體展開：每個 keyword 內部變體是 OR（同義詞），跨 keyword 是 AND。
    # 各 OR group 各自做 AND-of-OR cartesian product；合併所有 combo 一起跑查詢。
    or_groups_raw = _parse_or_groups(work.keyword)
    if not or_groups_raw:
        or_groups_raw = [work.keyword.strip()]

    # 每個 OR group 獨立展開，產出自己的 combo 列表
    all_combos: list[tuple[str, ...]] = []
    # 跨 group 合計的 raw keyword（扁平）— 用於 UI 顯示
    raw_keywords: list[str] = []
    # 所有展開後的變體（扁平、去重）— 存進 task.search_params 供 reader 高亮使用
    all_expanded_variants: list[str] = []
    _seen_variants: set[str] = set()
    MAX_AND_QUERIES = 20
    combo_truncated = False

    for group_query in or_groups_raw:
        group_raw_keywords = [kw.strip() for kw in group_query.split() if kw.strip()]
        if not group_raw_keywords:
            continue
        raw_keywords.extend(group_raw_keywords)

        variant_groups: list[list[str]] = []
        if work.expand_keywords:
            n_kw = len(group_raw_keywords)
            import math
            max_per_kw = max(2, min(8, int(math.pow(20, 1 / max(n_kw, 1)))))
            for kw in group_raw_keywords:
                sv, _, _ = await _expand_keyword(kw, api_key=work.api_key)
                variants = sv if sv else [kw]
                variant_groups.append(variants[:max_per_kw])
        else:
            variant_groups = [[kw] for kw in group_raw_keywords]

        # 扁平收集這 group 的所有變體（reader 高亮會用）
        for vg in variant_groups:
            for v in vg:
                if v and v not in _seen_variants:
                    _seen_variants.add(v)
                    all_expanded_variants.append(v)

        import itertools
        combos = list(itertools.product(*variant_groups))
        all_combos.extend(combos)

    if len(all_combos) > MAX_AND_QUERIES:
        logger.warning(
            "[%s] stage1 變體組合 %d 超過上限 %d，截斷",
            task_id, len(all_combos), MAX_AND_QUERIES,
        )
        combo_truncated = True
        all_combos = all_combos[:MAX_AND_QUERIES]

    # 為了相容下游（combo 變數名）
    combos = all_combos

    logger.info(
        "[%s] stage1 keyword 展開：%d OR group、%d 原始 keyword、%d 展開變體 → %d 個 AND 查詢",
        task_id, len(or_groups_raw), len(raw_keywords), len(all_expanded_variants), len(combos),
    )

    # 把展開後的變體存進 task.search_params — reader 高亮會讀來補進關鍵字 regex
    # 這樣律師搜「僱傭」時，reader 不只標「僱傭」也標「雇用 / 雇傭」等展開出來的變體
    if all_expanded_variants:
        try:
            cur_task = await db.get_task(task_id)
            cur_sp_str = cur_task.get("search_params") if cur_task else None
            cur_sp = json.loads(cur_sp_str) if cur_sp_str else {}
            cur_sp["expanded_variants"] = all_expanded_variants
            await db.update_task(task_id, search_params=json.dumps(cur_sp, ensure_ascii=False))
        except Exception as exc:
            # 存失敗只 log，不影響搜尋本體
            logger.warning("[%s] 存 expanded_variants 失敗：%s", task_id, exc)

    # Step 2: MCP search — 對每個 AND 查詢執行
    # 每 round 完成（一個 cursor 區段，~500 筆）就立刻寫 DB + 推 SSE 進度，
    # 律師看到 stage 2 列表筆數秒秒往上跳，而不是等全部 round 完才一次顯示。
    await _check_task_alive(task_id)

    async def on_round(new_items: list[dict], round_num: int, cumulative: int) -> None:
        """search 的 callback：把這 round 新增的判決即時寫入 task_search_hits 並推 SSE。
        cumulative = 該 combo 內 keyword 的 in-memory 累積；DB count 才是跨 combo 的真值。
        """
        if not new_items:
            return
        await db.bulk_insert_task_search_hits(task_id, new_items)
        total_now = await db.count_task_search_hits(task_id)
        await sse_bus.publish(task_id, "stage1_progress", {
            "task_id": task_id,
            "hits_total": total_now,
            "round": round_num,
            "new_hits": new_items,    # 前端直接 append，不必 refetch
        })
        logger.info("[%s] stage1 round %d 寫入 %d，DB 累計 %d", task_id, round_num, len(new_items), total_now)

    # Early termination：連續 2 個 combo delta=0 → 剩下都是 subset、提早停。
    # Variants 之間常高度重疊（例：「勞務契約」「承攬契約」都命中「契約」類判決），
    # 8 個 combo 跑完最後 3 個可能都是 0 delta，各多花 10–20s 只為了確認 0 收穫。
    # 阈值 2（不是 1）避免誤停：有時某 combo 臨時 0 命中（法院 cache miss）下一個又恢復。
    zero_delta_combos = 0
    EARLY_STOP_THRESHOLD = 2

    for combo_idx, combo in enumerate(combos, start=1):
        cur_total = await db.count_task_search_hits(task_id)
        if cur_total >= STAGE1_HARD_CAP:
            logger.info("[%s] stage1 已達 hard cap %d，停止後續 combo", task_id, STAGE1_HARD_CAP)
            break
        await _check_task_alive(task_id)
        and_query = " ".join(combo)
        remaining = STAGE1_HARD_CAP - cur_total

        # 先通知前端目前跑到第幾個變體 — State A header 會顯示「變體 X/Y」
        await sse_bus.publish(task_id, "stage1_combo_progress", {
            "task_id": task_id,
            "combo_idx": combo_idx,
            "total_combos": len(combos),
            "query": and_query,
        })

        prev_total = cur_total

        if work.exhaustive:
            await search_pipeline.run_search_exhaustive(
                keyword=and_query,
                court=None, case_type=None,
                year_from=work.year_from, year_to=work.year_to,  # 首頁進階篩選壓 server-side
                max_total=remaining,
                single_query=True,         # 整串原樣 → MCP/司法院 做 AND
                on_round_done=on_round,    # 每 cursor round 完成立刻通知
                main_text=work.main_text,  # 主文 server-side 篩選
            )
        else:
            # 非 exhaustive：一次回最多 500，沒有 round 概念，直接呼叫 callback 一次
            hits = await search_pipeline.run_search(
                keyword=and_query,
                court=None, case_type=None,
                year_from=work.year_from, year_to=work.year_to,
                max_results=min(search_pipeline.SITE_MAX_PER_QUERY, remaining),
                single_query=True,
                main_text=work.main_text,
            )
            await on_round(hits, 1, len(hits))

        # combo 完成：檢查這一輪有沒有新增 hit
        new_total = await db.count_task_search_hits(task_id)
        delta = new_total - prev_total
        if delta == 0:
            zero_delta_combos += 1
            remaining_combos = len(combos) - combo_idx
            if zero_delta_combos >= EARLY_STOP_THRESHOLD and remaining_combos > 0:
                logger.info(
                    "[%s] stage1 連續 %d 個變體無新 hit，提前終止（省 %d 個剩餘變體）",
                    task_id, zero_delta_combos, remaining_combos,
                )
                break
        else:
            zero_delta_combos = 0

    # 收尾
    final_total = await db.count_task_search_hits(task_id)
    truncated = work.exhaustive and final_total >= STAGE1_HARD_CAP

    finished = datetime.now(timezone.utc).isoformat()
    elapsed = _elapsed_sec(started, finished)
    await db.update_task(task_id, status="done", finished_at=finished)

    # 組 warnings 清單：讓前端知道搜尋有被截斷
    warnings = []
    if truncated:
        warnings.append(f"結果已達上限 {STAGE1_HARD_CAP} 筆，可能有更多判決未納入。建議縮小搜尋範圍。")
    if combo_truncated:
        warnings.append("關鍵字變體組合過多，部分組合未搜尋。建議減少關鍵字數量。")

    await sse_bus.publish(task_id, "stage1_done", {
        "task_id": task_id,
        "hits_total": final_total,
        "truncated": truncated,
        "elapsed_sec": elapsed,
        "warnings": warnings,
    })
    # 保留 task_done 事件給通用 listener（任務 timeline / 歷史卡片更新）
    await sse_bus.publish(task_id, "task_done", {
        "task_id": task_id,
        "elapsed_sec": elapsed,
    })
    await sse_bus.publish_done(task_id)
    logger.info("[%s] stage1 完成，%d hits（truncated=%s），耗時 %ds",
                task_id, final_total, truncated, elapsed)


# ---------------------------------------------------------------------------
# Stage 1 interpretation：走 cons.judicial.gov.tw 釋字 + 憲判字
# ---------------------------------------------------------------------------

async def _run_stage1_search_interpretation(work: Stage1SearchWork) -> None:
    """憲法解釋模式 Stage 1：call search_interpretations、normalize 成 task_search_hits。

    與 FJUD 模式的差異：
    - 資料本機離線（868 筆釋字+憲判字），秒回
    - 不做 synonym expansion、cartesian product（母體小、keyword substring 夠用）
    - 不做窮盡 pagination（無 500 筆上限）
    - 不支援 main_text filter（釋字無主文結構）
    - OR 語法仍支援：`A | B` → 兩次搜尋合併
    """
    from src.pipeline.cons_normalizer import normalize_cons_hit

    task_id = work.task_id
    started = datetime.now(timezone.utc).isoformat()

    await _check_task_alive(task_id)
    await db.update_task(task_id, status="running", started_at=started)

    # 1. 切 OR groups（keyword 每個 group 獨立查、結果 union）
    or_groups = _parse_or_groups(work.keyword)
    if not or_groups:
        or_groups = [work.keyword.strip()]

    await sse_bus.publish(task_id, "stage1_progress", {
        "task_id": task_id,
        "phase": "搜尋憲法解釋中",
        "hits_so_far": 0,
    })

    # 2. 對每個 group 呼叫 search_interpretations，合併 + 去重
    seen_case_ids: set[str] = set()
    all_hits: list[dict] = []
    for kw in or_groups:
        try:
            results = await mcp_client.search_interpretations(
                keyword=kw,
                include_old=True,
                include_new=True,
                max_results=1000,
            )
        except Exception as exc:
            logger.warning("[%s] search_interpretations(%r) 失敗：%s", task_id, kw, exc)
            continue
        for hit in results:
            cid = hit.get("case_id") or hit.get("title") or ""
            if not cid or cid in seen_case_ids:
                continue
            seen_case_ids.add(cid)
            all_hits.append(normalize_cons_hit(hit))

    logger.info("[%s] stage1 interpretation: %d groups → %d unique hits",
                task_id, len(or_groups), len(all_hits))

    # 3. 寫 task_search_hits（批次 INSERT）
    await db.bulk_insert_task_search_hits(task_id, all_hits)

    # 4. task_done + stage1_done
    finished = datetime.now(timezone.utc).isoformat()
    await db.update_task(task_id, status="done", finished_at=finished)
    elapsed = _elapsed_sec(started, finished)

    # 存 expanded_variants（用原 OR groups 當「變體」供 reader 高亮）
    sp = await db.get_task(task_id)
    try:
        sp_dict = json.loads(sp.get("search_params") or "{}") if sp else {}
    except (json.JSONDecodeError, TypeError):
        sp_dict = {}
    sp_dict["expanded_variants"] = list(or_groups)
    # update_task 沒替 dict 做 JSON 序列化、這邊手動轉字串
    await db.update_task(task_id, search_params=json.dumps(sp_dict, ensure_ascii=False))

    # 0-hit task auto-delete 語意：搜 0 筆等於律師關鍵字無匹配、下面流程沒意義、
    # 但不在 backend 自動刪（前端 `closeSearchCard` 會偵測後刪）
    await sse_bus.publish(task_id, "stage1_done", {
        "task_id": task_id,
        "hits_total": len(all_hits),
        "truncated": False,
        "elapsed_sec": elapsed,
        "warnings": [],
    })
    await sse_bus.publish(task_id, "task_done", {
        "task_id": task_id,
        "elapsed_sec": elapsed,
    })
    await sse_bus.publish_done(task_id)
    logger.info("[%s] stage1 interpretation 完成，%d hits，耗時 %ds",
                task_id, len(all_hits), elapsed)


# ---------------------------------------------------------------------------
# 兩階段搜尋 — Stage 3（narrow + fetch + 字串過濾 + Claude 精讀）
# ---------------------------------------------------------------------------

def _apply_narrow(hits: list[dict], narrow: dict) -> list[dict]:
    """依 narrow 條件篩出子集；空 narrow 則回傳全部。

    支援欄位：
      court_tiers   — list[str]，e.g. ['最高行政法院', '高等行政法院']
                      會展開成實際法院名後做 **exact match**（含分庭 suffix 必須
                      顯式列在 expand list，避免 "高等行政法院" tier 吞到
                      "臺北高等行政法院 地方庭" 之類 114 年後才存在的獨立法院）
      year_from / year_to — 民國年（int），對 hit['date'] 第一段比對
      case_types    — 後端 ignore（前端 client filter）
    """
    if not narrow:
        return hits

    court_tiers = narrow.get("court_tiers")
    year_from = narrow.get("year_from")
    year_to = narrow.get("year_to")

    target_courts = search_pipeline.expand_court_tiers(court_tiers) if court_tiers else None
    target_courts_set = set(target_courts) if target_courts else None

    def matches(h: dict) -> bool:
        if target_courts_set is not None:
            court = h.get("court", "")
            # Exact match：司法院 court 字串已經 normalize（如「臺北高等行政法院 地方庭」
            # 中間空格固定），直接 set membership 即可。expand_court_tiers 負責把各種
            # 分庭 / 分院名稱完整列入 tier 清單。
            if court not in target_courts_set:
                return False
        if year_from is not None or year_to is not None:
            try:
                yr = int((h.get("date") or "").split("-")[0])
                if year_from is not None and yr < year_from:
                    return False
                if year_to is not None and yr > year_to:
                    return False
            except (ValueError, IndexError):
                # 解析失敗的判決保守保留（避免錯殺）
                pass
        return True

    return [h for h in hits if matches(h)]


async def _run_reasoning_prefilter(work: ReasoningPreFilterWork) -> None:
    """抓全文 + 比對 reasoning 是否含關鍵字，即時推 SSE + 持久化到 DB。

    持久化機制（task_prefilter_results）：
    - 啟動時 init_prefilter_result（律師手動）或由 recovery 呼叫前 attempts++
    - Ownership check：用 narrow JSON 當 key。DB row 的 narrow 若跟自己啟動時不同，
      代表律師改了 filter 且新 work 已覆蓋 row → 自己 graceful abort 不寫 DB
    - 正常完成 mark_prefilter_done；非 ownership 的 exception mark_prefilter_cancelled
    """
    task_id = work.task_id
    task = await db.get_task(task_id)
    if not task:
        return

    from src.pipeline.filter import _fetch_one
    from src.pipeline.citation_extractor import extract_citations

    # 取 narrow 後的 hits
    all_hits = await db.get_task_search_hits(task_id)
    narrowed = _apply_narrow(all_hits, work.narrow)
    fetch_total = len(narrowed)

    # Ownership key：正規化 narrow JSON（sort_keys 確保 {A, B} 和 {B, A} 產生相同字串）
    own_narrow_json = json.dumps(work.narrow or {}, sort_keys=True, ensure_ascii=False)

    # 若是律師手動觸發 —— init_prefilter_result 由 API 層在 POST 時已呼叫；
    # 這邊只有 recovery 呼叫可能跳過（recovery 用 increment_prefilter_attempts 維持 row）。
    # 為了 idempotent，這裡不再 init — 相信 caller 已經設好 row。

    async def _ownership_valid() -> bool:
        cur = await db.get_prefilter_result(task_id)
        return cur is not None and cur["narrow"] == own_narrow_json

    # 準備關鍵字（原始 keyword split + 展開變體）
    raw_keywords = _flatten_keywords(task["keyword"])

    matched_ids = []
    fetched = 0
    reused = 0    # 跨 task cache 命中數（含在 fetched 內）

    # 一次載入已有的 task_judgments（雙 key：格式化名稱 + JID）
    _pf_existing = await db.get_task_judgments(task_id)
    _prefilter_existing = _build_judgment_map(_pf_existing)

    for idx, hit in enumerate(narrowed):
        if _is_deep_fetch_cancelled(task_id):
            _clear_deep_fetch_cancel(task_id)
            if await _ownership_valid():
                await db.mark_prefilter_cancelled(task_id)
            await sse_bus.publish(task_id, "reasoning_prefilter_cancelled", {
                "task_id": task_id, "fetched": fetched, "matched": len(matched_ids),
            })
            return

        await _check_task_alive(task_id)

        case_id = hit.get("jid") or hit.get("case_id") or ""
        if not case_id:
            fetched += 1
            continue

        # 抓全文（當前 task 已抓過的跳過；其他 task 抓過的複用）
        judgment = _prefilter_existing.get(case_id)

        if not judgment:
            # === 跨 task cache lookup（其他 task 相同 case_id + parser_version） ===
            # cache 複用含 create_task_judgment 寫入；序列迴圈中若拋例外會中止整個
            # prefilter 後續判決。比照 cold-fetch 失敗：記 skip、continue 下一筆。
            try:
                cached_judgment = await _try_reuse_cached_judgment(task_id, case_id)
            except Exception as exc:
                logger.warning("[%s] prefilter cache 複用跳過 %s：%s", task_id, case_id, exc)
                fetched += 1
                continue
            if cached_judgment is not None:
                _prefilter_existing[cached_judgment["case_id"]] = cached_judgment
                judgment = cached_judgment
                reused += 1
            else:
                try:
                    raw = await _fetch_one(case_id)
                    ft = raw.get("full_text") or ""
                    extracted = extract_citations(ft) if ft else []
                    ec_serialized = [list(c.as_tuple()) for c in extracted] if extracted else None
                    cited = raw.get("cited_statutes")
                    if isinstance(cited, str):
                        try:
                            cited = json.loads(cited)
                        except json.JSONDecodeError:
                            cited = [cited] if cited else []

                    await db.create_task_judgment(
                        task_id=task_id, case_id=raw.get("case_id", case_id),
                        court=raw.get("court", ""), date=raw.get("date", ""),
                        source_url=raw.get("source_url", ""),
                        reasoning=raw.get("reasoning"), main_text=raw.get("main_text"),
                        facts=raw.get("facts"), cited_statutes=cited,
                        full_text=raw.get("full_text"), extracted_citations=ec_serialized,
                        judges=raw.get("judges"), parties=raw.get("parties"), cause=raw.get("cause"),
                        parser_version=PARSER_VERSION,
                    )
                    _prefilter_existing[raw.get("case_id", case_id)] = raw
                    judgment = raw
                except Exception as exc:
                    logger.warning("[%s] prefilter fetch 跳過 %s：%s", task_id, case_id, exc)
                    fetched += 1
                    continue

        fetched += 1

        # 比對 reasoning 是否含任一關鍵字
        reasoning = judgment.get("reasoning") or judgment.get("full_text") or ""
        if any(kw in reasoning for kw in raw_keywords):
            jid = judgment.get("case_id") or case_id
            matched_ids.append(jid)

        # 每 5 筆推一次進度
        if (fetched % 5 == 0) or (idx == fetch_total - 1):
            # Ownership check — DB 的 narrow 若被新 work 覆蓋，我自己 abort
            if not await _ownership_valid():
                logger.info("[%s] prefilter ownership lost（narrow 已變更），abort", task_id)
                return
            matched_json = json.dumps(matched_ids, ensure_ascii=False)
            await db.update_prefilter_progress(task_id, matched_json, len(matched_ids))
            await sse_bus.publish(task_id, "reasoning_prefilter_progress", {
                "task_id": task_id,
                "fetched": fetched,
                "total": fetch_total,
                "matched": len(matched_ids),
                "matched_case_ids": matched_ids[:],
                "reused": reused,
            })

    # Final: 仍是當前 owner 才寫 done（避免 race）
    if await _ownership_valid():
        await db.mark_prefilter_done(
            task_id, json.dumps(matched_ids, ensure_ascii=False), len(matched_ids),
        )
    await sse_bus.publish(task_id, "reasoning_prefilter_done", {
        "task_id": task_id,
        "total": fetch_total,
        "matched": len(matched_ids),
        "matched_case_ids": matched_ids,
        "reused": reused,
    })
    logger.info("[%s] 理由預篩完成：%d/%d 筆命中（重用 %d）",
                task_id, len(matched_ids), fetch_total, reused)


async def _run_stage3_analyze(work: Stage3AnalyzeWork) -> None:
    task_id = work.task_id
    analysis_id = work.analysis_id

    await _check_task_alive(task_id)
    await db.update_analysis(analysis_id, status="running")

    task = await db.get_task(task_id)
    if not task:
        raise TaskCancelledError(task_id)

    # search_domain 決定 Claude prompt（interpretation 不評 direction）
    search_domain = task.get("search_domain") or "judgment"

    # 從 task_search_hits 拉全部 hits → apply narrow（或用理由預篩結果）
    if work.prefilter_case_ids:
        # 理由預篩已完成 — 全文已在 task_judgments，只分析這些 case_ids
        narrowed = [{"jid": cid, "case_id": cid} for cid in work.prefilter_case_ids]
        logger.info(
            "[%s] stage3 analysis=%s 使用理由預篩結果 %d 筆",
            task_id, analysis_id, len(narrowed),
        )
    else:
        all_hits = await db.get_task_search_hits(task_id)
        narrowed = _apply_narrow(all_hits, work.narrow)
        logger.info(
            "[%s] stage3 analysis=%s narrow %d → %d 筆",
            task_id, analysis_id, len(all_hits), len(narrowed),
        )

    if not narrowed:
        await db.update_analysis(analysis_id, status="done", total=0, match_count=0)
        await sse_bus.publish(task_id, "analysis_done", {
            "task_id": task_id,
            "analysis_id": analysis_id,
            "match_count": 0,
        })
        # 不推 publish_done — 律師可能追問或再次 narrow，SSE 通道應保持 open
        return

    # Step 1: 並行 fetch 全文 + 可選理由篩選
    # reasoning_filter=True 時：fetch 每筆 → 比對 reasoning → 只有命中的才進 Claude 精讀
    await _check_task_alive(task_id)
    _clear_deep_fetch_cancel(task_id)
    from src.pipeline.citation_extractor import extract_citations
    from src.pipeline.filter import _fetch_one

    FETCH_CONCURRENCY = 5  # MCP 並行（HTTP I/O bound，5 路加速 cold start）
    fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    fetch_total = len(narrowed)
    _fetched = 0           # atomic counter（CPython GIL 保護）
    _actually_fetched = 0
    _reused = 0            # 跨 task cache 命中數（推給 SSE 供前端顯示）
    reasoning_matched_ids: list[str] = []
    skipped_cases: list[dict] = []  # 優化 3：收集 fetch 失敗的 case_ids
    raw_keywords = _flatten_keywords(task["keyword"])
    # 合併 Stage 1 展開出的 variants（法條格式變體、同義詞）供 smart_truncate 定位長判決
    # 中的相關段落 — 律師搜「民法第184條之1」但判決寫「民法第184-1條」時，視窗不會錯位
    _sp_raw = task.get("search_params") or "{}"
    try:
        _sp = json.loads(_sp_raw) if isinstance(_sp_raw, str) else _sp_raw
        _exp = _sp.get("expanded_variants") if isinstance(_sp, dict) else None
    except (json.JSONDecodeError, TypeError):
        _exp = None
    truncation_keywords = list(raw_keywords)
    if isinstance(_exp, list):
        _seen = set(truncation_keywords)
        for v in _exp:
            if isinstance(v, str) and v and v not in _seen:
                _seen.add(v)
                truncation_keywords.append(v)
    _cancelled = False

    # 一次載入已有的 task_judgments（雙 key：格式化名稱 + JID）
    existing_judgments = await db.get_task_judgments(task_id)
    existing_map = _build_judgment_map(existing_judgments)

    # Recovery-aware：已分析過的 case_ids（新分析時為空集合）
    _existing_results = await db.get_analysis_results(analysis_id)
    already_done_ids: set[str] = {r["case_id"] for r in _existing_results}

    # Producer-consumer 模式：fetch 每完成一筆（cache 或新抓）就 put 進 queue，
    # Claude consumer 持續消費，消除兩輪制空窗期。reasoning_filter=True 不走此路。
    judgment_queue: asyncio.Queue = asyncio.Queue() if not work.reasoning_filter else None  # type: ignore[assignment]

    async def _fetch_one_hit(hit: dict) -> None:
        nonlocal _fetched, _actually_fetched, _reused, _cancelled
        if _cancelled or _is_deep_fetch_cancelled(task_id):
            _cancelled = True
            return
        try:
            await _check_task_alive(task_id)
        except TaskCancelledError:
            _cancelled = True
            raise

        case_id = hit.get("jid") or hit.get("case_id") or ""
        if not case_id:
            _fetched += 1
            return

        # 已在當前 task cache → 跳過 fetch（recovery 場景）
        judgment = existing_map.get(case_id)
        if judgment:
            _fetched += 1
        else:
            # === 跨 task cache lookup（在 sem 外，hits 走全平行、不佔 5-way 槽位） ===
            # cache 複用含 create_task_judgment 寫入；並發下若拋例外，外層 gather
            # (return_exceptions=True) 會靜默吞掉 → 該筆無聲漏。比照 cold-fetch 失敗：
            # 記 skipped_cases、續跑成可重試的 skip。
            try:
                cached_judgment = await _try_reuse_cached_judgment(task_id, case_id)
            except Exception as exc:
                logger.warning("[%s] stage3 cache 複用跳過 %s：%s", task_id, case_id, exc)
                skipped_cases.append({"case_id": case_id, "error": str(exc)[:100]})
                _fetched += 1
                return
            if cached_judgment is not None:
                judgment = cached_judgment
                existing_map[cached_judgment["case_id"]] = cached_judgment
                _reused += 1
                _fetched += 1
            else:
                async with fetch_sem:
                    try:
                        judgment = await _fetch_one(case_id)
                    except Exception as exc:
                        logger.warning("[%s] stage3 fetch 跳過 %s：%s", task_id, case_id, exc)
                        skipped_cases.append({"case_id": case_id, "error": str(exc)[:100]})
                        _fetched += 1
                        return

                    ft = judgment.get("full_text") or ""
                    extracted = extract_citations(ft) if ft else []
                    ec_serialized = [list(c.as_tuple()) for c in extracted] if extracted else None
                    cited = judgment.get("cited_statutes")
                    if isinstance(cited, str):
                        try: cited = json.loads(cited)
                        except json.JSONDecodeError: cited = [cited] if cited else []

                    await db.create_task_judgment(
                        task_id=task_id, case_id=judgment.get("case_id", case_id),
                        court=judgment.get("court", ""), date=judgment.get("date", ""),
                        source_url=judgment.get("source_url", ""),
                        reasoning=judgment.get("reasoning"), main_text=judgment.get("main_text"),
                        facts=judgment.get("facts"), cited_statutes=cited,
                        full_text=judgment.get("full_text"), extracted_citations=ec_serialized,
                        judges=judgment.get("judges"), parties=judgment.get("parties"),
                        cause=judgment.get("cause"),
                        parser_version=PARSER_VERSION,
                    )
                    existing_map[judgment.get("case_id", case_id)] = judgment
                    _actually_fetched += 1
                    _fetched += 1

        # 交錯模式：把 judgment 丟到 queue 讓 Claude consumer 立即消費
        # 略過 reasoning_filter（走序列）與 recovery 已分析過的判決
        if judgment_queue is not None and judgment is not None:
            formatted_id = judgment.get("case_id", case_id)
            if formatted_id not in already_done_ids:
                await judgment_queue.put(judgment)

        # 理由篩選：比對 reasoning 是否含任一關鍵字
        if work.reasoning_filter and judgment:
            reasoning = judgment.get("reasoning") or judgment.get("full_text") or ""
            if any(kw in reasoning for kw in raw_keywords):
                jid = judgment.get("case_id") or case_id
                reasoning_matched_ids.append(jid)

    # 啟動並行 fetch + SSE 進度推送
    async def _progress_reporter():
        """每 2 秒推一次 fetch 進度，直到全部完成。"""
        last_reported = 0
        while last_reported < fetch_total and not _cancelled:
            await asyncio.sleep(2)
            if _fetched != last_reported:
                last_reported = _fetched
                progress_data = {
                    "task_id": task_id, "fetched": _fetched, "total": fetch_total,
                    "reused": _reused,
                }
                if work.reasoning_filter:
                    progress_data["reasoning_matched"] = len(reasoning_matched_ids)
                await sse_bus.publish(task_id, "stage25_progress", progress_data)

    # ── Fetch + Claude 交錯邏輯 ──
    # reasoning_filter=True → 必須先完成全部 fetch（需要完整 matched_ids），序列
    # reasoning_filter=False → fetch 在背景跑，Claude 等首批 judgment 到 DB 後就開始
    #   run_analysis_v2 的 recovery 邏輯（already_done check）保證第二輪不會重複分析

    discovery_keyword = raw_keywords[0] if raw_keywords else None
    case_id_filter = None
    if work.prefilter_case_ids:
        case_id_filter = work.prefilter_case_ids

    # `_scoring_persisted` 追蹤「本次 run_analysis_v2 call 內已寫入 DB 的 scoring tokens」，
    # 每次 on_batch_done 算 delta 來 increment DB，避免單 run 內重複累加。
    # Retry iteration 會另起 run_analysis_v2、local counter 重置，但 DB 用增量寫入、
    # 不會丟前一 run 的累積。
    _scoring_persisted = {"input": 0, "output": 0}

    async def on_batch_done(batch_results_data: list[dict], usage: dict | None = None) -> None:
        a_now = await db.get_analysis(analysis_id) or {}
        completed_now = a_now.get("completed", 0)
        total_now = a_now.get("total", fetch_total)
        match_count_now = a_now.get("match_count", 0)
        event = {
            "task_id": task_id, "analysis_id": analysis_id,
            "completed": completed_now, "total": total_now,
            "match_count": match_count_now,
            "results": batch_results_data,
        }
        if usage:
            # 先把本 run 這批新增的 scoring tokens 以 delta 形式寫入 DB，
            # 供 preliminary / final / 手動升格 讀取總成本
            try:
                cur_in  = int(usage.get("scoring_input", 0) or 0)
                cur_out = int(usage.get("scoring_output", 0) or 0)
                delta_in  = max(0, cur_in  - _scoring_persisted["input"])
                delta_out = max(0, cur_out - _scoring_persisted["output"])
                if delta_in or delta_out:
                    await db.increment_scoring_tokens(analysis_id, delta_in, delta_out)
                    _scoring_persisted["input"]  = cur_in
                    _scoring_persisted["output"] = cur_out
            except Exception as exc:
                logger.warning("[%s] increment_scoring_tokens 失敗（不影響主流程）：%s", task_id, exc)
            # SSE payload 的 usage 用 DB 累積值、不是 local run counter。
            # 原因：resume 時新 run_analysis_v2 invocation 的 _total_input_tokens 從 0 起算、
            # 若 SSE 回傳 local 值、前端 ticker 會看到「跨 resume token 歸零」。
            # 用 DB 累積值就能忠實顯示「從一開始到現在總共花了多少」。
            try:
                a_updated = await db.get_analysis(analysis_id) or {}
                event["usage"] = {
                    "scoring_input":  int(a_updated.get("scoring_input_tokens",  0) or 0),
                    "scoring_output": int(a_updated.get("scoring_output_tokens", 0) or 0),
                }
            except Exception:
                event["usage"] = usage  # fallback 用 local（仍比 None 好）
        await sse_bus.publish(task_id, "batch_done", event)

    async def _do_fetch():
        """並行 fetch 全部，完成後設 event。"""
        progress_task = asyncio.create_task(_progress_reporter())
        try:
            results = await asyncio.gather(
                *[_fetch_one_hit(h) for h in narrowed], return_exceptions=True,
            )
            for r in results:
                if isinstance(r, TaskCancelledError):
                    raise r
        finally:
            progress_task.cancel()
            try: await progress_task
            except asyncio.CancelledError: pass
        # 最終進度推送（含全 cache 命中的場景）
        if _fetched > 0:
            await sse_bus.publish(task_id, "stage25_progress", {
                "task_id": task_id, "fetched": _fetched, "total": fetch_total,
                "reused": _reused,
            })

    async def _do_claude_pass(pass_label: str = ""):
        """讀 DB 中已有的 judgment → Claude 精讀。回傳 usage dict。"""
        return await analyze_pipeline.run_analysis_v2(
            analysis_id=analysis_id, task_id=task_id,
            question=work.question, read_facts=work.read_facts,
            on_batch_done=on_batch_done, api_key=work.api_key,
            discovery_keyword=discovery_keyword,
            case_id_filter=case_id_filter if work.reasoning_filter else None,
            search_keywords=truncation_keywords,
            search_domain=search_domain,
        ) or {}

    scoring_usage = {}

    # ── Preliminary watcher：scoring 期間背景監控剩餘筆數 ──
    # 剩餘 ≤ threshold（依 total 規模 10/20/30）→ 先跑 synthesis 出初步結果，flag=1
    # 主流程接著會跑 retry + final synthesis 覆蓋（flag=0）
    # 先等 2 分鐘再開始判斷，避免極短任務剛啟動就被誤觸發
    _preliminary_fired = {"value": False}

    async def _preliminary_watcher() -> None:
        try:
            await asyncio.sleep(120)
        except asyncio.CancelledError:
            return
        while True:
            try:
                if _preliminary_fired["value"]:
                    return
                a = await db.get_analysis(analysis_id)
                if not a or a.get("synthesis_is_preliminary"):
                    return
                completed_now = a.get("completed") or 0
                total_now = a.get("total") or 0
                if total_now <= 0:
                    await asyncio.sleep(30)
                    continue
                if completed_now >= total_now:
                    return  # scoring 已完，由主流程接 final
                th = _preliminary_remaining_threshold(total_now)
                if th is None:
                    return  # total < 30，不值得 preliminary
                if (total_now - completed_now) <= th:
                    _preliminary_fired["value"] = True
                    await _fire_preliminary_synthesis(
                        analysis_id, task_id, work.question, work.api_key,
                        completed_now, total_now,
                    )
                    return
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[%s] preliminary watcher 錯誤：%s", task_id, e)
                return

    preliminary_task = asyncio.create_task(_preliminary_watcher())

    if work.reasoning_filter:
        # ── 序列模式：先 fetch 全部（需要完整 reasoning_matched_ids），再 Claude ──
        await _do_fetch()
        if _cancelled:
            _clear_deep_fetch_cancel(task_id)
            preliminary_task.cancel()
            await db.update_analysis(analysis_id, status="failed")
            await sse_bus.publish(task_id, "stage3_cancelled", {
                "task_id": task_id, "analysis_id": analysis_id,
                "phase": "fetch", "fetched": _fetched, "total": fetch_total,
            })
            # 不推 publish_done — 律師仍可能發起新精讀或 fetch
            return

        case_id_filter = reasoning_matched_ids
        after_filter = len(reasoning_matched_ids)
        logger.info("[%s] 理由篩選：%d/%d 筆命中", task_id, after_filter, fetch_total)

        await _check_task_alive(task_id)
        await sse_bus.publish(task_id, "judgments_ready", {
            "task_id": task_id, "total_search": fetch_total,
            "after_filter": after_filter, "skipped": len(skipped_cases),
        })
        scoring_usage = await _do_claude_pass("sequential")
    else:
        # ── Producer-consumer 交錯模式 ──
        # fetch worker 每完成一筆（cache 或新抓）就 put 到 judgment_queue；
        # Claude consumer（CONCURRENCY 個 worker）持續從 queue 消費並分析，
        # fetch 結束後 put N 個 None sentinel 通知 consumer 收工。
        # 消除舊設計「第一輪 Claude 跑完 → 等 fetch → catch-up」的空窗期。
        #
        # expected_total：narrow 後實際要分析的筆數（已扣除 recovery 中 already_done）
        expected_total = 0
        for _hit in narrowed:
            _cid = _hit.get("jid") or _hit.get("case_id") or ""
            if not _cid:
                continue
            _j = existing_map.get(_cid)
            # cache 命中且已分析 → 不計入；其餘（要抓的 or cache 未分析的）都計入
            if _j is not None and _j.get("case_id") in already_done_ids:
                continue
            expected_total += 1

        after_filter = fetch_total if not work.prefilter_case_ids else len(work.prefilter_case_ids)
        await sse_bus.publish(task_id, "judgments_ready", {
            "task_id": task_id, "total_search": fetch_total,
            "after_filter": after_filter, "skipped": 0,
        })

        # 同時啟動 fetch producer 與 Claude consumer
        fetch_task = asyncio.create_task(_do_fetch())
        claude_task = asyncio.create_task(
            analyze_pipeline.run_analysis_v2(
                analysis_id=analysis_id, task_id=task_id,
                question=work.question, read_facts=work.read_facts,
                on_batch_done=on_batch_done, api_key=work.api_key,
                discovery_keyword=discovery_keyword,
                search_keywords=truncation_keywords,
                judgment_queue=judgment_queue,
                expected_total=expected_total,
                search_domain=search_domain,
            )
        )

        # 等 fetch 完成
        try:
            await fetch_task
        except TaskCancelledError:
            # 取消 claude_task 並讓 worker loop 接住
            claude_task.cancel()
            try:
                await claude_task
            except (asyncio.CancelledError, Exception):
                pass
            raise
        except Exception as exc:
            logger.warning("[%s] fetch 背景任務異常：%s", task_id, exc)

        if _cancelled:
            _clear_deep_fetch_cancel(task_id)
            preliminary_task.cancel()
            # 解鎖 consumer workers：put 足量 sentinel，然後 cancel 收尾
            for _ in range(analyze_pipeline.CONCURRENCY):
                judgment_queue.put_nowait(None)
            try:
                await asyncio.wait_for(claude_task, timeout=10)
            except (asyncio.TimeoutError, Exception):
                claude_task.cancel()
                try:
                    await claude_task
                except (asyncio.CancelledError, Exception):
                    pass
            await db.update_analysis(analysis_id, status="failed")
            await sse_bus.publish(task_id, "stage3_cancelled", {
                "task_id": task_id, "analysis_id": analysis_id,
                "phase": "fetch", "fetched": _fetched, "total": fetch_total,
            })
            # 不推 publish_done — 律師仍可能發起新精讀或 fetch
            return

        # Fetch 正常結束：put N 個 sentinel 讓 consumer 收工
        for _ in range(analyze_pipeline.CONCURRENCY):
            await judgment_queue.put(None)

        try:
            scoring_usage = await claude_task or {}
        except Exception as exc:
            logger.warning("[%s] Claude consumer 異常：%s", task_id, exc)
            scoring_usage = {}

        # 更新 skipped 數量（fetch 完成後才知道最終值）
        await sse_bus.publish(task_id, "judgments_ready", {
            "task_id": task_id, "total_search": fetch_total,
            "after_filter": after_filter, "skipped": len(skipped_cases),
        })

    # Scoring 主迴圈結束 → 停掉 preliminary watcher（若還沒 fire 的話）
    preliminary_task.cancel()
    try:
        await preliminary_task
    except (asyncio.CancelledError, Exception):
        pass

    # Persist skipped_cases 到 DB — 律師可在 UI 按「全部重試」叫 retry endpoint 重抓
    # （暫時性 MCP/司法院 WAF 失敗佔大宗、下次重試通常救得回）。存 case_id 陣列、
    # 不存 error 訊息（retry 時反正要重跑、原錯誤訊息保留意義不大）。
    if skipped_cases:
        try:
            await db.update_analysis(
                analysis_id,
                skipped_case_ids=json.dumps(
                    [c["case_id"] for c in skipped_cases], ensure_ascii=False,
                ),
            )
        except Exception as exc:
            # 寫入失敗只 log、不影響主流程（UI 仍可看到 skipped 數、只是無法 retry）
            logger.warning("[%s] 寫入 skipped_case_ids 失敗：%s", task_id, exc)

    # ── Phase 3.5：retry missing judgments ──
    # Scoring 主迴圈結束後，task_judgments 可能有些 case 完全沒被評分（server 重啟中斷 /
    # queue 漏抓）。用 list_missing_judgments 找出來，用 run_analysis_v2 加 case_id_filter
    # 重跑一輪，最多 3 輪；每輪若 missing 集合沒變化（全部失敗），提早結束。
    # 每輪邊界 check _is_finalize_requested — 律師按「定稿」就中止剩餘 retry。
    MAX_RETRY_ITERATIONS = 3
    retry_iteration = 0
    finalize_intercepted = False
    prev_missing_set: set[str] = set()

    while retry_iteration < MAX_RETRY_ITERATIONS:
        if _is_graceful_abort(analysis_id):
            logger.info("[%s] 律師按「中止」→ 中止 retry loop（已跑 %d 輪）",
                        task_id, retry_iteration)
            break
        if _is_finalize_requested(analysis_id):
            finalize_intercepted = True
            logger.info("[%s] 律師按「定稿」→ 中止 retry loop（已跑 %d 輪）",
                        task_id, retry_iteration)
            break
        missing = await db.list_missing_judgments(task_id, analysis_id)
        if not missing:
            break
        missing_set = set(missing)
        if missing_set == prev_missing_set:
            logger.info("[%s] retry 無進展（missing 集合不變）→ 結束", task_id)
            break
        prev_missing_set = missing_set
        logger.info("[%s] retry iteration %d：%d 筆 missing",
                    task_id, retry_iteration + 1, len(missing))
        try:
            retry_usage = await analyze_pipeline.run_analysis_v2(
                analysis_id=analysis_id, task_id=task_id,
                question=work.question, read_facts=work.read_facts,
                on_batch_done=on_batch_done, api_key=work.api_key,
                discovery_keyword=discovery_keyword,
                case_id_filter=missing,
                search_keywords=truncation_keywords,
                search_domain=search_domain,
            ) or {}
            scoring_usage["scoring_input"] = (
                scoring_usage.get("scoring_input", 0) + retry_usage.get("scoring_input", 0)
            )
            scoring_usage["scoring_output"] = (
                scoring_usage.get("scoring_output", 0) + retry_usage.get("scoring_output", 0)
            )
        except Exception as e:
            logger.warning("[%s] retry iteration %d 異常：%s",
                           task_id, retry_iteration + 1, e)
        retry_iteration += 1

    analysis = await db.get_analysis(analysis_id)
    match_count = (analysis or {}).get("match_count", 0)

    # ── Graceful abort 分支（優先於 finalize：律師明確按「中止」， finalize 旗標忽略）──
    # 判準用 match_count 而非 rows_done：沒有命中的判決再多也 synthesis 不出東西。
    # match_count ≥ 3：/abort endpoint 已 fire-and-forget 跑 _fire_abort_partial_synthesis
    #                  fast path → 這裡只要 detect partial/done 已寫、skip 重複 synthesis
    # match_count < 3：status='cancelled'、不跑 synthesis
    if _is_graceful_abort(analysis_id):
        rows_done = await db.count_analysis_results(analysis_id)
        _clear_graceful_abort(analysis_id)
        _clear_finalize_requested(analysis_id)  # 兩旗並存時 abort 覆蓋
        # Fast path 已跑完：analysis.status 會是 partial
        # 重抓 analysis 確認最新狀態（fast path async 寫的）
        a_latest = await db.get_analysis(analysis_id) or {}
        if a_latest.get("status") in {"partial", "done"} and a_latest.get("synthesis"):
            logger.info("[%s] graceful abort scoring-end：fast path 已完成 synthesis，skip",
                        task_id)
            return
        if match_count >= 3:
            # 任何中止都寫 partial（2026-04-19 決策：移除 was_resumed→done 誤 trap 設計）
            await sse_bus.publish(task_id, "stage3_synthesis_start", {
                "task_id": task_id, "analysis_id": analysis_id, "preliminary": True,
            })
            try:
                partial_synthesis = await analyze_pipeline.run_synthesis(
                    analysis_id=analysis_id, question=work.question, api_key=work.api_key,
                )
            except Exception as exc:
                logger.warning("[%s] partial synthesis 失敗：%s", task_id, exc)
                partial_synthesis = {}
            # Token usage 累積（同 final synthesis 的 pricing 邏輯）
            a_now = await db.get_analysis(analysis_id) or {}
            s_in  = int(a_now.get("scoring_input_tokens", 0)  or 0) or scoring_usage.get("scoring_input", 0)
            s_out = int(a_now.get("scoring_output_tokens", 0) or 0) or scoring_usage.get("scoring_output", 0)
            syn_in  = int(partial_synthesis.get("_synth_input", 0)  or 0)
            syn_out = int(partial_synthesis.get("_synth_output", 0) or 0)
            cost_usd = (s_in * 0.80 + s_out * 4.00 + syn_in * 3.00 + syn_out * 15.00) / 1_000_000
            partial_synthesis["_usage"] = {
                "scoring_input": s_in, "scoring_output": s_out,
                "synthesis_input": syn_in, "synthesis_output": syn_out,
                "total_cost_usd": round(cost_usd, 4),
            }
            await db.set_analysis_synthesis(analysis_id, partial_synthesis, is_preliminary=True)
            await db.update_analysis(analysis_id, status="partial")
            await sse_bus.publish(task_id, "stage3_partial_done", {
                "task_id": task_id, "analysis_id": analysis_id,
                "done": rows_done, "total": after_filter,
                "match_count": match_count, "is_final": False,
                "synthesis": partial_synthesis,
            })
            logger.info("[%s] stage3 graceful abort → partial（%d/%d，命中 %d）",
                        task_id, rows_done, after_filter, match_count)
        else:
            await db.update_analysis(analysis_id, status="cancelled")
            await sse_bus.publish(task_id, "stage3_cancelled", {
                "task_id": task_id, "analysis_id": analysis_id,
                "done": rows_done, "total": after_filter, "match_count": match_count,
            })
            logger.info("[%s] stage3 graceful abort → cancelled（命中 %d 筆 < 3、rows=%d，無 synthesis 價值）",
                        task_id, match_count, rows_done)
        return

    # ── Final synthesis 決策 ──
    # 3 個分支：
    #   A. finalize_intercepted + 有 preliminary → 直接升格（flag → 0），省掉最後 synthesis call
    #   B. finalize_intercepted 但無 preliminary（極端 edge case，律師在極短任務按定稿）→ 走正常 final
    #   C. 正常完成 → 跑 final synthesis 覆蓋任何 preliminary，flag=0 mark done
    if finalize_intercepted and analysis and analysis.get("synthesis_is_preliminary") \
            and analysis.get("synthesis"):
        logger.info("[%s] finalize 定稿：preliminary 升格 final（省 synthesis call）", task_id)
        _clear_finalize_requested(analysis_id)
        await db.update_analysis(analysis_id, synthesis_is_preliminary=0, status="done")
        try:
            existing_synthesis = json.loads(analysis["synthesis"])
        except (json.JSONDecodeError, TypeError):
            existing_synthesis = {}
        await sse_bus.publish(task_id, "analysis_done", {
            "task_id": task_id, "analysis_id": analysis_id, "match_count": match_count,
        })
        await sse_bus.publish(task_id, "stage3_synthesis_done", {
            "task_id": task_id, "analysis_id": analysis_id,
            "synthesis": existing_synthesis, "is_final": True,
        })
        logger.info("[%s] stage3 v2 finalize（preliminary 升格），相關 %d/%d 筆",
                    task_id, match_count, after_filter)
        return

    # 走到這裡 = 正常 final synthesis（case B / case C）
    _clear_finalize_requested(analysis_id)  # B case 下也清掉旗標
    await _check_task_alive(task_id)
    await sse_bus.publish(task_id, "stage3_synthesis_start", {
        "task_id": task_id, "analysis_id": analysis_id, "preliminary": False,
    })
    synthesis = await analyze_pipeline.run_synthesis(
        analysis_id=analysis_id, question=work.question, api_key=work.api_key,
    )

    # 合併 token usage 到 synthesis → 存 DB
    # Haiku pricing: $0.80/MTok input, $4.00/MTok output
    # Sonnet pricing: $3.00/MTok input, $15.00/MTok output
    # 從 DB 讀累積 scoring tokens（on_batch_done 持續增量寫入），比 local scoring_usage
    # 更可靠 — 尤其 retry iteration 的 local counter 各自獨立、DB 有完整累積。
    a_final = await db.get_analysis(analysis_id) or {}
    s_in = int(a_final.get("scoring_input_tokens", 0) or 0) or scoring_usage.get("scoring_input", 0)
    s_out = int(a_final.get("scoring_output_tokens", 0) or 0) or scoring_usage.get("scoring_output", 0)
    syn_in = synthesis.get("_synth_input", 0)
    syn_out = synthesis.get("_synth_output", 0)
    cost_usd = (s_in * 0.80 + s_out * 4.00 + syn_in * 3.00 + syn_out * 15.00) / 1_000_000
    synthesis["_usage"] = {
        "scoring_input": s_in, "scoring_output": s_out,
        "synthesis_input": syn_in, "synthesis_output": syn_out,
        "total_cost_usd": round(cost_usd, 4),
    }
    await db.set_analysis_synthesis(analysis_id, synthesis, is_preliminary=False)
    await db.update_analysis(analysis_id, status="done")

    await sse_bus.publish(task_id, "analysis_done", {
        "task_id": task_id, "analysis_id": analysis_id, "match_count": match_count,
    })
    await sse_bus.publish(task_id, "stage3_synthesis_done", {
        "task_id": task_id, "analysis_id": analysis_id,
        "synthesis": synthesis, "is_final": True,
    })
    # 不推 publish_done — task 整體沒有「結束」概念，律師隨時可能追問或 fetch
    # 更多判決。sentinel 會誤關 SSE 通道，後續追問 analysis 的事件就收不到。
    # SSE 在前端 tab 關閉時由 stream.py 的 finally 自動 unsubscribe。
    logger.info("[%s] stage3 v2 完成 analysis=%s，相關 %d/%d 筆，consensus=%s",
                task_id, analysis_id, match_count, after_filter, synthesis.get("consensus"))


# ---------------------------------------------------------------------------
# Retry-skipped：對 stage2.5 fetch 失敗的 case_id 重試
# ---------------------------------------------------------------------------
# 背景：Stage3 fetch 時 MCP 可能回空（司法院 WAF throttle、MCP subprocess 臨時卡、
# 或 cookie 失效），3-retry 耗盡後當筆就 skip、警告「N 筆下載失敗未分析」。實測
# 大多數失敗是暫時性的（手動再抓成功率高）。這 endpoint 讓律師一鍵重試。
#
# 流程：
#   1. 讀 analyses.skipped_case_ids（stage3 結束時寫入）
#   2. 逐筆 _fetch_one：成功 → create_task_judgment；失敗 → 加回 still_skipped
#   3. 全部抓完後、對成功筆用 run_analysis_v2(case_id_filter=...) 跑 scoring
#   4. 更新 analyses.skipped_case_ids（留 still_skipped；全成功則 NULL）
#   5. SSE 通知 UI refresh 結果列表
# 不重跑 synthesis — synthesis 的 summary / clusters 可能會略遺漏新救回的 match、
# 但律師按「重新總結」或發新追問可再生。簡化複雜度、省成本。

async def _run_retry_skipped(
    task_id: str,
    analysis_id: str,
    api_key: str | None = None,
) -> None:
    """背景執行：retry stage2.5 fetch 失敗的 case_ids。

    呼叫 pattern：API endpoint 檢查 inflight guard 後 asyncio.create_task() 這個函式。
    完成/失敗都負責清 inflight guard。
    """
    from src.pipeline.filter import _fetch_one
    from src.pipeline.citation_extractor import extract_citations
    from src.pipeline import analyze as analyze_pipeline
    from src.mcp_client import PARSER_VERSION

    try:
        analysis = await db.get_analysis(analysis_id)
        task = await db.get_task(task_id)
        if not analysis or not task:
            logger.warning("[%s] retry-skipped abort: analysis or task missing", task_id)
            return

        raw = analysis.get("skipped_case_ids") or "[]"
        try:
            skipped = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            skipped = []
        if not skipped:
            logger.info("[%s] retry-skipped no-op: skipped_case_ids empty", task_id)
            return

        logger.info("[%s] retry-skipped 開始：%d 筆", task_id, len(skipped))
        await sse_bus.publish(task_id, "retry_skipped_start", {
            "task_id": task_id, "analysis_id": analysis_id, "total": len(skipped),
        })

        # Phase 1：逐筆 fetch（平行但受 MCP bucket 限流）
        still_skipped: list[str] = []
        recovered_case_ids: list[str] = []   # MCP 回傳的人類可讀 case_id（寫 task_judgments 用）
        FETCH_CONCURRENCY = 3  # retry 用較保守的並行數、避免再次觸發司法院 WAF
        fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _retry_fetch_one(jid: str, idx: int) -> None:
            async with fetch_sem:
                try:
                    judgment = await _fetch_one(jid)
                except Exception as exc:
                    logger.warning("[%s] retry-skipped fetch 仍失敗 %s：%s",
                                   task_id, jid, str(exc)[:100])
                    still_skipped.append(jid)
                    await sse_bus.publish(task_id, "retry_skipped_progress", {
                        "task_id": task_id, "analysis_id": analysis_id,
                        "current": idx, "total": len(skipped),
                        "case_id": jid, "status": "failed",
                    })
                    return

                # 寫入 task_judgments — 跟 stage3 的 fetch 路徑保持一致
                ft = judgment.get("full_text") or ""
                extracted = extract_citations(ft) if ft else []
                ec_serialized = [list(c.as_tuple()) for c in extracted] if extracted else None
                cited = judgment.get("cited_statutes")
                if isinstance(cited, str):
                    try: cited = json.loads(cited)
                    except json.JSONDecodeError: cited = [cited] if cited else []

                real_case_id = judgment.get("case_id", jid)
                try:
                    await db.create_task_judgment(
                        task_id=task_id, case_id=real_case_id,
                        court=judgment.get("court", ""), date=judgment.get("date", ""),
                        source_url=judgment.get("source_url", ""),
                        reasoning=judgment.get("reasoning"), main_text=judgment.get("main_text"),
                        facts=judgment.get("facts"), cited_statutes=cited,
                        full_text=judgment.get("full_text"), extracted_citations=ec_serialized,
                        judges=judgment.get("judges"), parties=judgment.get("parties"),
                        cause=judgment.get("cause"),
                        parser_version=PARSER_VERSION,
                    )
                except Exception as exc:
                    # 可能已存在（極少數 race）、忽略；continue 跑 scoring
                    logger.info("[%s] retry-skipped task_judgments 寫入跳過 %s：%s",
                                task_id, real_case_id, str(exc)[:80])

                recovered_case_ids.append(real_case_id)
                await sse_bus.publish(task_id, "retry_skipped_progress", {
                    "task_id": task_id, "analysis_id": analysis_id,
                    "current": idx, "total": len(skipped),
                    "case_id": jid, "status": "fetched",
                })

        await asyncio.gather(*[_retry_fetch_one(jid, i) for i, jid in enumerate(skipped, start=1)])

        # Phase 2：對救回的 case 跑 scoring（若有救回）
        new_matches = 0
        if recovered_case_ids:
            logger.info("[%s] retry-skipped 進入 scoring：%d 筆救回、%d 筆仍失敗",
                        task_id, len(recovered_case_ids), len(still_skipped))
            ai_read = (analysis.get("ai_read_field") or "").split(",")
            read_facts = "facts" in ai_read
            search_domain = task.get("search_domain") or "judgment"

            # 從 task.search_params 取 expanded_variants（smart_truncate 用）
            sp_raw = task.get("search_params") or "{}"
            try:
                sp = json.loads(sp_raw) if isinstance(sp_raw, str) else sp_raw
                truncation_keywords = sp.get("expanded_variants") if isinstance(sp, dict) else None
            except (json.JSONDecodeError, TypeError):
                truncation_keywords = None

            discovery_keyword = (task.get("keyword") or "").split()[0] if task.get("keyword") else None

            # 記 scoring 前的 match_count → 跑 → 比 diff（省得一筆筆查 DB、且自動跟
            # run_analysis_v2 的 increment_analysis_progress 計算口徑一致）
            a_before = await db.get_analysis(analysis_id)
            match_count_before = (a_before or {}).get("match_count", 0) or 0

            # 對新救回的 case 跑 scoring（run_analysis_v2 的 case_id_filter 機制）
            # match_count 由 increment_analysis_progress 更新、completed 同步更新
            try:
                await analyze_pipeline.run_analysis_v2(
                    analysis_id=analysis_id, task_id=task_id,
                    question=analysis["question"], read_facts=read_facts,
                    api_key=api_key,
                    case_id_filter=recovered_case_ids,
                    discovery_keyword=discovery_keyword,
                    search_keywords=truncation_keywords,
                    search_domain=search_domain,
                )
            except Exception as exc:
                logger.warning("[%s] retry-skipped scoring 錯誤：%s", task_id, exc)

            a_after = await db.get_analysis(analysis_id)
            match_count_after = (a_after or {}).get("match_count", 0) or 0
            new_matches = max(0, match_count_after - match_count_before)

        # Phase 3：更新 skipped_case_ids — 留下仍失敗的、全成功則清 NULL
        # 同時恢復 status — run_analysis_v2 進 scoring 時會把 status 改 running、
        # retry 結束需改回原本的終態（done / partial）；依 synthesis_is_preliminary 判斷
        a_final = await db.get_analysis(analysis_id) or {}
        restore_status = (
            "partial" if a_final.get("synthesis_is_preliminary")
            else "done"
        )
        await db.update_analysis(
            analysis_id,
            status=restore_status,
            skipped_case_ids=(
                json.dumps(still_skipped, ensure_ascii=False) if still_skipped else None
            ),
        )

        await sse_bus.publish(task_id, "retry_skipped_done", {
            "task_id": task_id, "analysis_id": analysis_id,
            "total": len(skipped),
            "recovered": len(recovered_case_ids),
            "still_failed": len(still_skipped),
            "new_matches": new_matches,
        })
        logger.info("[%s] retry-skipped 完成：%d 救回（%d 新 match）、%d 仍失敗",
                    task_id, len(recovered_case_ids), new_matches, len(still_skipped))
    except Exception as exc:
        logger.exception("[%s] retry-skipped 未捕獲異常：%s", task_id, exc)
        await sse_bus.publish(task_id, "retry_skipped_done", {
            "task_id": task_id, "analysis_id": analysis_id,
            "total": 0, "recovered": 0, "still_failed": 0, "new_matches": 0,
            "error": str(exc)[:200],
        })
    finally:
        _retry_skipped_inflight.discard(analysis_id)


def start_retry_skipped(task_id: str, analysis_id: str, api_key: str | None = None) -> bool:
    """API 入口：檢查 inflight guard、true = 啟動成功、false = 已在跑"""
    if analysis_id in _retry_skipped_inflight:
        return False
    _retry_skipped_inflight.add(analysis_id)
    asyncio.create_task(_run_retry_skipped(task_id, analysis_id, api_key))
    return True


# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------

def _build_judgment_map(judgments: list[dict]) -> dict[str, dict]:
    """建立 case_id 雙 key 查找表：格式化名稱 + source_url 中的 JID。

    task_search_hits 用 JID（如 TPAA,113,訴,1234,...），
    task_judgments.case_id 用格式化名稱（如 臺北高等行政法院113年度訴字第1234號）。
    雙 key 確保兩邊都能查到。
    """
    mapping: dict[str, dict] = {}
    for j in judgments:
        mapping[j["case_id"]] = j
        url = j.get("source_url") or ""
        if "id=" in url:
            jid = url.split("id=")[-1]
            mapping[jid] = j
    return mapping


def _elapsed_sec(start_iso: str, end_iso: str) -> int:
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        return int((end - start).total_seconds())
    except Exception:
        return 0
