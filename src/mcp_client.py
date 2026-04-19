"""持久化 MCP 客戶端：連接 mcp-taiwan-legal-db，提供 search_judgments / get_judgment。

在 FastAPI lifespan 中呼叫 init_mcp() / close_mcp()，
保持整個 server 生命週期內只有一個 subprocess + ClientSession。
"""
import json
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


class MCPSearchError(RuntimeError):
    """MCP server 搜尋回傳 success=False 時丟出。呼叫端決定 retry 或 fallback。"""

# Cross-task cache invalidation key（見 db.find_cached_judgment）。
# 會變動 task_judgments 內容的 parser 邏輯改動時都要 bump：
#   1. mcp-taiwan-legal-db/mcp_server/parsers/judicial_parser.py
#   2. src/pipeline/citation_extractor.py
#   3. worker/runner.py _run_stage25_fetch 寫入 task_judgments 的欄位集合（新增欄位時）
# 流程：改 parser → tests/regenerate_fixtures.py → pytest 通過 → bump → commit。
# 舊 row 以 'v0' backfill（遷移 7），永遠不會被跨 task cache 命中，漸進汰換。
# v1 → v2（2026-04-18）：upstream 合併（移除 Playwright、改用 httpx + F5 WAF bypass、
#   合併憲法法庭工具）。HTML 抓取路徑變 → 可能 micro-diff in parsed 結果，保守 bump。
# v2 → v3（2026-04-18）：舊制釋字 normalizer 從 reasoning 尾端切出大法官名單
#   到 judges 欄位。既有 v2 cache 的釋字 reasoning 還含名單尾巴、要失效重抓。
# v3 → v4（2026-04-18）：舊制釋字 case_id 加「司法院」prefix。v3 cache 存的是
#   「釋字第N號」、v4 存「司法院釋字第N號」，cross-task cache 以 case_id 當 key、
#   不同格式視為不同資料、自然 miss 重抓。
PARSER_VERSION = "v4"

_session: ClientSession | None = None
_exit_stack_close = None  # 用來保存需要呼叫的 cleanup callable


async def init_mcp() -> None:
    """啟動 mcp-taiwan-legal-db subprocess 並建立 ClientSession。

    若 MCP server 無法啟動（例如尚未安裝），僅記錄警告，不中斷 server 啟動。

    env 處理：我們用 editable install 把 clone 下來的 mcp-taiwan-legal-db
    裝進 venv，但 macOS 有時會把 site-packages 下的 .pth 檔標上 UF_HIDDEN
    flag（iCloud/Time Machine/某些同步工具會這樣做），一旦被標 hidden，
    Python 3.12+ 的 site.py 就會直接跳過該 .pth，導致 editable install 失效、
    subprocess import 不到 mcp_server。
    這裡明確把 clone 的根目錄加進 PYTHONPATH，即使 .pth 失效也能 import。
    """
    global _session, _exit_stack_close

    import os
    import sys
    from contextlib import AsyncExitStack
    from pathlib import Path

    # judgment-search/src/mcp_client.py → judgment-search → mcp-taiwan-legal-db
    mcp_src_root = Path(__file__).resolve().parents[1] / "mcp-taiwan-legal-db"
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{mcp_src_root}{os.pathsep}{existing_pp}" if existing_pp else str(mcp_src_root)
    )

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        env=env,
    )

    stack = AsyncExitStack()
    try:
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        _session = session
        _exit_stack_close = stack.aclose
        logger.info("MCP mcp-taiwan-legal-db 連線成功")
    except Exception as exc:
        logger.warning("MCP 初始化失敗（%s）：搜尋功能將不可用", exc)
        # stdio_client 若已啟動 subprocess（即使後續 initialize 失敗），必須 aclose
        # 清掉；否則 zombie subprocess 會累積
        try:
            await stack.aclose()
        except Exception:
            pass


async def close_mcp() -> None:
    """關閉 ClientSession 及 subprocess。"""
    global _session, _exit_stack_close
    if _exit_stack_close:
        await _exit_stack_close()
        _session = None
        _exit_stack_close = None
        logger.info("MCP 連線已關閉")


def _get_session() -> ClientSession:
    if _session is None:
        raise RuntimeError("MCP server 未連線。請確認 mcp-taiwan-legal-db 已安裝（npm install -g mcp-taiwan-legal-db）。")
    return _session


def _parse_tool_result(result: Any) -> Any:
    """從 CallToolResult 取出第一個 text content 並 JSON 解析。"""
    content = getattr(result, "content", None)
    if not content:
        raise ValueError("MCP 工具回傳空 content")
    text = getattr(content[0], "text", None)
    if text is None:
        raise ValueError("MCP 工具回傳非文字 content")
    return json.loads(text)


# ---------------------------------------------------------------------------
# 工具封裝
# ---------------------------------------------------------------------------

async def search_judgments(
    keyword: str,
    court: str | None = None,
    case_type: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    month_from: int | None = None,
    day_from: int | None = None,
    month_to: int | None = None,
    day_to: int | None = None,
    max_results: int = 200,
    main_text: str | None = None,
) -> list[dict]:
    """呼叫 search_judgments，回傳判決清單（字號 / 法院 / 日期）。

    max_results：MCP 硬上限 500（對應司法院網站單次查詢顯示上限），預設 200。
    month/day 參數為民國曆，配合 year_from/year_to 做細粒度窗口切分 —
    窮盡搜尋模式用這些參數做 keyset pagination。
    main_text：對應司法院「裁判主文」欄位（jud_jmain）— search server-side filter，
    比拿到結果再 client-filter 快很多（律師最常用：「被告應給付」「撤銷原處分」等）。
    """
    args: dict[str, Any] = {"keyword": keyword, "max_results": max_results}
    if court:
        args["court"] = court
    if case_type:
        args["case_type"] = case_type
    if year_from is not None:
        args["year_from"] = year_from
    if year_to is not None:
        args["year_to"] = year_to
    if month_from is not None:
        args["month_from"] = month_from
    if day_from is not None:
        args["day_from"] = day_from
    if month_to is not None:
        args["month_to"] = month_to
    if day_to is not None:
        args["day_to"] = day_to
    if main_text:
        args["main_text"] = main_text

    session = _get_session()
    result = await session.call_tool("search_judgments", arguments=args)
    data = _parse_tool_result(result)
    # 回傳形式可能是 {"judgments": [...]} 或直接 [...]
    if isinstance(data, dict):
        # MCP server 搜尋失敗時會回 {"success": False, "error": "..."}（無 judgments key）
        # 必須 raise、而不是吞成空 list — 不然窮盡搜尋會把「失敗」誤判成「抓到底」提早結束
        if data.get("success") is False:
            raise MCPSearchError(str(data.get("error") or "search_judgments 失敗"))
        return data.get("judgments", data.get("results", []))
    return data


async def count_judgments(keyword: str) -> int | None:
    """估算某 keyword 在司法院的命中筆數（用於 synonym tier 分級）。

    MCP 的 total_count 其實是 `len(results)`（即分頁抓了幾筆），不是司法院全站真總數。
    所以我們用 max_results=50 探頂：若回傳滿 50 → 該 keyword 至少有 50 筆，為 confirmed tier；
    若 < 50 → 就是真實筆數。對分級 (0 / 1-5 / 6-49 / ≥50) 的邏輯已足夠。
    """
    session = _get_session()
    args = {"keyword": keyword, "max_results": 50}
    try:
        result = await session.call_tool("search_judgments", arguments=args)
        data = _parse_tool_result(result)
        if isinstance(data, dict):
            results = data.get("results", data.get("judgments", []))
            return len(results)
        return len(data) if isinstance(data, list) else None
    except Exception:
        return None


async def get_judgment(jid: str) -> dict:
    """呼叫 get_judgment，回傳單筆結構化判決。"""
    session = _get_session()
    result = await session.call_tool("get_judgment", arguments={"jid": jid})
    return _parse_tool_result(result)


# ---------------------------------------------------------------------------
# 憲法解釋相關（cons.judicial.gov.tw 資料，2026-04 upstream 新增）
# ---------------------------------------------------------------------------
#
# 範圍：釋字第 1-813 號 + 111 年起憲判字 ~70 筆（共 868 筆，本機 JSON 離線索引）。
# 與 FJUD search_judgments 分流：律師在首頁選「憲法解釋」mode 時走這條路徑。

async def search_interpretations(
    keyword: str = "",
    include_old: bool = True,
    include_new: bool = True,
    max_results: int = 1000,
) -> list[dict]:
    """搜尋大法官解釋 / 憲法法庭裁判。

    keyword: 空字串 → 回所有；非空 → 標題/字號/爭點/理由書子字串匹配。
    max_results 預設 1000（含括全部 868 筆）。

    回傳形式：
      [{"type": "釋字" | "憲判字", "case_id": "釋字第748號", "year": ..., "number": ..., "title": ..., "issues": ...}, ...]

    年度篩選由呼叫端 client-side 做（cons 的 year 參數只對新制有效）。
    """
    session = _get_session()
    args: dict[str, Any] = {
        "keyword": keyword,
        "include_old": include_old,
        "include_new": include_new,
        "max_results": max_results,
    }
    result = await session.call_tool("search_interpretations", arguments=args)
    data = _parse_tool_result(result)
    if isinstance(data, dict):
        return data.get("results", [])
    return data if isinstance(data, list) else []


async def get_interpretation(
    case_id: str,
    include_reasoning: bool = True,
    include_opinions: bool = False,
) -> dict:
    """取單篇釋字 / 憲判字。

    預設 include_reasoning=True（律師要看完整推論）、include_opinions=False
    （意見書不具拘束力、占 context 大量空間、不納入精讀）。
    """
    session = _get_session()
    args: dict[str, Any] = {
        "case_id": case_id,
        "include_reasoning": include_reasoning,
        "include_opinions": include_opinions,
    }
    result = await session.call_tool("get_interpretation", arguments=args)
    return _parse_tool_result(result)
