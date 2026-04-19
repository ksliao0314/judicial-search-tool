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
from src.utils.rate_limiter import TokenBucket
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
_mcp_fetch_bucket = TokenBucket(rate_per_minute=60, capacity=30)

async def _fetch_one(jid: str) -> dict:
    """取得單筆判決，帶重試。

    依 jid（= case_id）格式 dispatch：
    - 釋字第N號 / 年憲判字第N號（容忍「司法院」prefix）→ 走 cons get_interpretation
    - 其他 → 走 FJUD get_judgment
    """
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
