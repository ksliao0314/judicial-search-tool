"""MCP get_judgment / get_interpretation 的單點入口 + 全域 rate limit。

歷史：本檔曾含 `fetch_and_filter` 批次 pipeline（每批 20 筆、批間
1.5-3.5 秒 sleep）與 `_matches_filter` 字串過濾，在 2026-04 架構升級改走
`runner._run_stage25_fetch`（並行 + 跨 task cache）後已無 caller。
2026-04-18 session 清理時整個刪除，若需再引入批次過濾邏輯請參考 git history。

現在只保留兩個公開項目：
  _fetch_one       — Stage 2.5 / Stage 3 / reasoning prefilter 共用的 MCP 入口
  _mcp_fetch_bucket — 全域 MCP get_judgment rate limit（見下方說明）
"""
import logging

from src import mcp_client
from src.utils.rate_limiter import judicial_bucket
from src.utils.retry import with_retry

logger = logging.getLogger(__name__)

# MCP get_judgment 的全域 rate limit（跨 task 共用）。
# MCP fork 只對 search 限流（judicial_search.py _rate_limit），get_judgment 無限流 →
# 多個 task 並行（_stage_sem=5 + Stage 3 fetch_sem=5 = 峰值 25 路）可能瞬間爆打
# 司法院。此 bucket 在 app 層做保底限流、保護司法院 + MCP subprocess。
# 60 req/min、burst 30：允許短時 burst 後 1 req/sec 持續抓，平均對應舊 BATCH_DELAY
# 的「每批 20 筆、批間 1.5-3.5 秒」節奏（~40 req/min）但更寬鬆。
# cache hit（app 層 find_cached_judgment / MCP 端 file cache）不走 _fetch_one 就不耗 token。
# cons get_interpretation 走本機 JSON，也不經此 bucket。
# 2026-07-16 起改為「全 app 對司法院共用的單一預算」的別名（見 rate_limiter.judicial_bucket）：
# PDF 直抓等其他路徑也分食同一個桶，F5 看的是來源 IP 的總速率，各路徑各開桶會疊加超標。
_mcp_fetch_bucket = judicial_bucket

async def _fetch_one(jid: str, *, source_url: str | None = None) -> dict:
    """取得單筆判決 / 訴願決定書全文，帶重試。

    dispatch：
    - source_url 為 appealweb（勞動部訴願）→ get_appeal_decision（詳情頁無驗證碼、
      不經 MCP / 司法院 bucket）
    - jid 為 釋字第N號 / 年憲判字第N號（容忍「司法院」prefix）→ cons get_interpretation
    - 其他 → FJUD get_judgment
    """
    # 勞動部訴願：詳情頁公開、用 source_url（含 caseId）抓全文
    if source_url and "appealweb.mol.gov.tw" in source_url:
        from src.pipeline import appeal_source
        d = await with_retry(
            appeal_source.get_appeal_decision,
            source_url,
            delays=(3.0, 8.0, 20.0),
            label=f"get_appeal_decision(…{source_url[-24:]})",
        )
        if d is None:
            raise RuntimeError(f"訴願決定書解析失敗：{source_url}")
        # 詳情頁的字號偶爾被遮蔽（如法律扶助案顯示「…字第號」缺號碼），但搜尋列的字號
        # （= jid，由 search_appeals 抓自結果表格）是完整的 → 回填，避免 case_id 空：
        # 空字號會讓 task_judgments 存空鍵，且多筆遮蔽案都空鍵會撞 UNIQUE(task_id,case_id)
        # 被 INSERT OR IGNORE 互相吃掉、只活一筆。
        if not (d.get("case_id") or "").strip() and jid:
            d["case_id"] = jid
        return d
    from src.pipeline.cons_normalizer import (
        is_interpretation_case_id, normalize_cons_judgment, strip_cons_prefix,
    )
    if is_interpretation_case_id(jid):
        # MCP 端只認不含 prefix 的格式、剝「司法院」再送
        api_case_id = strip_cons_prefix(jid)
        raw = await with_retry(
            mcp_client.get_interpretation,
            api_case_id,
            delays=(2.0, 5.0, 10.0),   # cons 本機 JSON 通常秒回，短 backoff 即可
            label=f"get_interpretation({api_case_id})",
        )
        return normalize_cons_judgment(raw)
    # FJUD get_judgment：走全域 token bucket 限流（見模組頂部 _mcp_fetch_bucket 說明）
    await _mcp_fetch_bucket.acquire(1)
    return await with_retry(
        mcp_client.get_judgment,
        jid,
        delays=(5.0, 15.0, 45.0),
        label=f"get_judgment({jid})",
    )
