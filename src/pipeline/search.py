"""search_judgments 封裝：呼叫 MCP，回傳判決清單骨架。

兩種模式：
  run_search           — 一次拉 max_results 筆後收工（預設）。
  run_search_exhaustive — 以日期為 cursor 反覆下探，直到查無新筆或抵 year_from。
                          用於律師需「窮盡」某關鍵字所有判決的場景。
"""
import asyncio
import logging
from datetime import date, timedelta

from src import mcp_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 法院層級展開
# ---------------------------------------------------------------------------

COURT_TIERS: dict[str, list[str]] = {
    "憲法法庭": ["憲法法庭"],
    "最高法院": ["最高法院"],
    "最高行政法院": ["最高行政法院"],
    "高等行政法院": [
        "臺北高等行政法院",
        "臺中高等行政法院",
        "高雄高等行政法院",
    ],
    # 114 年行政訴訟改制後的地方行政訴訟庭。隸屬高等行政法院但獨立運作、
    # 審行政案件初審。司法院 search 回傳 court 名稱為「臺北高等行政法院 地方庭」
    # （中間有空格）。必須獨立 tier 而非併入 "高等行政法院" —「高等行政法院」tier
    # 的 expand list 走 exact match，不會吞到這些 "地方庭" 案件。
    "高等行政法院地方庭": [
        "臺北高等行政法院 地方庭",
        "臺中高等行政法院 地方庭",
        "高雄高等行政法院 地方庭",
    ],
    "高等法院": [
        "臺灣高等法院",
        "臺灣高等法院臺中分院",
        "臺灣高等法院臺南分院",
        "臺灣高等法院高雄分院",
        "臺灣高等法院花蓮分院",
        "福建高等法院金門分院",
    ],
    "地方法院": [
        "臺灣臺北地方法院", "臺灣士林地方法院", "臺灣新北地方法院",
        "臺灣基隆地方法院", "臺灣宜蘭地方法院", "臺灣桃園地方法院",
        "臺灣新竹地方法院", "臺灣苗栗地方法院", "臺灣臺中地方法院",
        "臺灣彰化地方法院", "臺灣南投地方法院", "臺灣雲林地方法院",
        "臺灣嘉義地方法院", "臺灣臺南地方法院", "臺灣高雄地方法院",
        "臺灣橋頭地方法院", "臺灣屏東地方法院", "臺灣花蓮地方法院",
        "臺灣臺東地方法院", "臺灣澎湖地方法院", "福建金門地方法院",
        "福建連江地方法院", "臺灣高雄少年及家事法院",
    ],
}


def expand_court_tiers(court_tiers: list[str] | None) -> list[str] | None:
    """將 tier 名稱清單展開為實際法院名稱清單。

    None 或空 list → None（全部法院，不限制）。
    """
    if not court_tiers:
        return None
    courts: list[str] = []
    for tier in court_tiers:
        tier_courts = COURT_TIERS.get(tier, [])
        courts.extend(tier_courts)
    return courts or None


# ---------------------------------------------------------------------------

# 司法院網站對單次查詢硬性「只顯示前 500 筆」，超過就截斷；
# 窮盡搜尋碰到 500 必須靠 date cursor 窗口往下切才能拿完。
SITE_MAX_PER_QUERY = 500
EXHAUSTIVE_ROUND_CAP = 500
EXHAUSTIVE_MAX_ROUNDS = 50  # 防呆：極端 keyword 不要讓 worker 跑到天荒地老


# ---------------------------------------------------------------------------
# 民國年 / 西元日期轉換
# ---------------------------------------------------------------------------

def _roc_to_date(roc_str: str) -> date | None:
    """民國日期字串（如 "113-12-31"）→ `datetime.date`（西元）。解析失敗回 None。"""
    try:
        parts = roc_str.split("-")
        if len(parts) != 3:
            return None
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return date(y + 1911, m, d)
    except (ValueError, TypeError):
        return None


def _date_to_roc(d: date) -> tuple[int, int, int]:
    """`datetime.date`（西元）→ (民國年, 月, 日)。"""
    return (d.year - 1911, d.month, d.day)


# ---------------------------------------------------------------------------
# 單輪搜尋（原有行為）
# ---------------------------------------------------------------------------

async def run_search(
    keyword: str,
    court: str | None = None,
    case_type: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    max_results: int = 200,
    single_query: bool = False,
    main_text: str | None = None,
) -> list[dict]:
    """
    呼叫 MCP search_judgments，回傳 list of {case_id, court, date, ...}。

    `single_query=False`（預設、legacy）：把 keyword 用空格 split 後各自查詢合併去重 → OR 語意。
    `single_query=True`（新流程 stage 1）：keyword 整串原樣丟給 MCP → 司法院本身對空格做 AND，
      所以「不當得利 當事人適格」會是「同時含這兩個詞」的判決。律師預期就是 AND。
    """
    if single_query:
        kw = keyword.strip()
        if not kw:
            raise ValueError("關鍵字不得為空")
        logger.info("搜尋（single AND query）：%r (max_results=%d, main_text=%r)",
                    kw, max_results, main_text)
        hits = await mcp_client.search_judgments(
            keyword=kw, court=court, case_type=case_type,
            year_from=year_from, year_to=year_to, max_results=max_results,
            main_text=main_text,
        )
        # 同樣去重（MCP 內部分頁可能有重複）
        seen: set[str] = set()
        results: list[dict] = []
        for item in hits:
            cid = item.get("jid") or item.get("case_id") or ""
            if cid and cid not in seen:
                seen.add(cid); results.append(item)
        logger.info("搜尋完成（single）：%d 筆", len(results))
        return results

    # Legacy 路徑：split + OR 合併
    keywords = [kw.strip() for kw in keyword.split() if kw.strip()]
    if not keywords:
        raise ValueError("關鍵字不得為空")

    seen2: set[str] = set()
    results2: list[dict] = []

    for kw in keywords:
        logger.info("搜尋關鍵字：%s (max_results=%d)", kw, max_results)
        hits = await mcp_client.search_judgments(
            keyword=kw,
            court=court,
            case_type=case_type,
            year_from=year_from,
            year_to=year_to,
            max_results=max_results,
        )
        for item in hits:
            # MCP 回傳同時含 `jid`（結構化，如 "TPTA,113,交,1138,20241231,1"）
            # 與 `case_id`（中文描述，如「臺北高等行政法院 地方庭 113 年度 交 字第 1138 號判決」）。
            # get_judgment 必須吃結構化 jid，否則爬不到頁面 → 這裡去重與後續流程都以 jid 為準。
            cid = item.get("jid") or item.get("case_id") or ""
            if cid and cid not in seen2:
                seen2.add(cid)
                results2.append(item)

    logger.info("搜尋完成（legacy OR），共 %d 筆（去重後）", len(results2))
    return results2


# ---------------------------------------------------------------------------
# 窮盡搜尋（keyset pagination by date cursor）
# ---------------------------------------------------------------------------

async def _exhaustive_single_keyword(
    keyword: str,
    court: str | None,
    case_type: str | None,
    year_from: int | None,
    year_to: int | None,
    max_rounds: int,
    max_total: int | None = None,
    on_round_done = None,    # async callback(new_items, round_num, cumulative)
    main_text: str | None = None,
) -> list[dict]:
    """對單一關鍵字，以「本輪最舊日期 - 1 天」為 cursor 反覆下探，直到：
      a) 本輪 < 500（拿到底）
      b) cursor 跨越 year_from
      c) cursor 未前進（防呆：同一天 >500 筆，無法再細切）
      d) 達 max_rounds（防呆）
      e) 累計 >= max_total（呼叫者指定的 hard cap，避免熱門關鍵字白跑數萬筆）
    """
    seen: set[str] = set()
    all_items: list[dict] = []

    cur_y: int | None = year_to
    cur_m: int | None = None
    cur_d: int | None = None

    for round_num in range(1, max_rounds + 1):
        # Retry on MCP search failure（避免把 MCP 一次性呼叫失敗誤判成「抓到底」提早結束）
        # 三次都失敗 → 累積到的 items 先還給律師、log warning 讓他決定要不要手動重搜
        hits = None
        last_exc = None
        for attempt in range(3):
            try:
                hits = await mcp_client.search_judgments(
                    keyword=keyword,
                    court=court,
                    case_type=case_type,
                    year_from=year_from,
                    year_to=cur_y,
                    month_to=cur_m,
                    day_to=cur_d,
                    max_results=EXHAUSTIVE_ROUND_CAP,
                    main_text=main_text,
                )
                break
            except mcp_client.MCPSearchError as exc:
                last_exc = exc
                backoff = 2 ** attempt  # 1s → 2s → 4s
                logger.warning(
                    "[exhaustive %s] round %d MCP 搜尋失敗 (attempt %d/3)、%ds 後重試：%s",
                    keyword, round_num, attempt + 1, backoff, exc,
                )
                await asyncio.sleep(backoff)
        if hits is None:
            logger.warning(
                "[exhaustive %s] round %d MCP 連續 3 次失敗、中止窮盡（累計 %d 筆）：%s",
                keyword, round_num, len(all_items), last_exc,
            )
            break

        new_items = []
        for item in hits:
            jid = item.get("jid") or item.get("case_id") or ""
            if jid and jid not in seen:
                seen.add(jid)
                new_items.append(item)
        all_items.extend(new_items)
        logger.info(
            "[exhaustive %s] round %d: cursor to=%s-%s-%s, 本輪 %d 筆, "
            "新增 %d, 累計 %d",
            keyword, round_num, cur_y, cur_m, cur_d,
            len(hits), len(new_items), len(all_items),
        )

        # 推進度給呼叫者（worker 用來即時寫 DB + SSE，律師看著數字長）
        if on_round_done and new_items:
            await on_round_done(new_items, round_num, len(all_items))

        # (e) 達到呼叫者指定的 hard cap → 立刻停（避免熱門 keyword 白跑後面幾十輪 MCP）
        if max_total is not None and len(all_items) >= max_total:
            logger.info(
                "[exhaustive %s] 累計 %d 達 max_total=%d，停止 cursor",
                keyword, len(all_items), max_total,
            )
            break

        # (a) 未滿 500 → 抓到底
        if len(hits) < EXHAUSTIVE_ROUND_CAP:
            logger.info("[exhaustive %s] 本輪未滿 %d，結束", keyword, EXHAUSTIVE_ROUND_CAP)
            break

        # 找本輪最舊日期
        oldest: date | None = None
        for h in hits:
            d = _roc_to_date(h.get("date", ""))
            if d and (oldest is None or d < oldest):
                oldest = d
        if oldest is None:
            logger.warning("[exhaustive %s] 本輪回傳無有效日期，中止", keyword)
            break

        # 下一輪 cursor = 本輪最舊 - 1 天
        next_cursor = oldest - timedelta(days=1)
        new_y, new_m, new_d = _date_to_roc(next_cursor)

        # (b) 跨越下界
        if year_from is not None and new_y < year_from:
            logger.info(
                "[exhaustive %s] cursor 跨越 year_from=%d，結束",
                keyword, year_from,
            )
            break

        # (c) cursor 未前進 — 單日 >500 筆，無法再細切（資料極密，放棄續查並警告）
        if (new_y, new_m, new_d) == (cur_y, cur_m, cur_d):
            logger.warning(
                "[exhaustive %s] cursor 停滯於 %s-%s-%s（單日 >%d 筆）；"
                "此日之前的資料未納入",
                keyword, cur_y, cur_m, cur_d, EXHAUSTIVE_ROUND_CAP,
            )
            break

        cur_y, cur_m, cur_d = new_y, new_m, new_d

    else:
        logger.warning(
            "[exhaustive %s] 達 max_rounds=%d 上限強制結束，累計 %d 筆",
            keyword, max_rounds, len(all_items),
        )

    return all_items


async def run_search_exhaustive(
    keyword: str,
    court: str | None = None,
    case_type: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    max_rounds: int = EXHAUSTIVE_MAX_ROUNDS,
    max_total: int | None = None,
    single_query: bool = False,
    on_round_done = None,    # async callback(new_items, round_num, cumulative) — 每 round 觸發
    main_text: str | None = None,
) -> list[dict]:
    """以日期為 cursor 反覆下探，直到該關鍵字在年份範圍內的所有命中都被取得。

    `single_query=False`（預設、legacy）：keyword 用空格 split 各自 exhaustive 後合併 → OR
    `single_query=True`（新流程 stage 1）：keyword 整串視為一次 AND 查詢，cursor 也只跑一次

    `max_total`：累計上限（達到就停 cursor / 不再查下個關鍵字）。
      避免「不當得利」這類熱門關鍵字白跑 50 rounds × 500 = 25000，被呼叫者截斷。
    """
    if single_query:
        kw = keyword.strip()
        if not kw:
            raise ValueError("關鍵字不得為空")
        logger.info("窮盡搜尋（single AND query）：%r", kw)
        items = await _exhaustive_single_keyword(
            keyword=kw,
            court=court, case_type=case_type,
            year_from=year_from, year_to=year_to,
            max_rounds=max_rounds, max_total=max_total,
            on_round_done=on_round_done,
            main_text=main_text,
        )
        # 去重
        seen: set[str] = set()
        results: list[dict] = []
        for item in items:
            cid = item.get("jid") or item.get("case_id") or ""
            if cid and cid not in seen:
                seen.add(cid); results.append(item)
        logger.info("窮盡搜尋完成（single），共 %d 筆", len(results))
        return results

    # Legacy: split + 各自 exhaustive + union
    keywords = [kw.strip() for kw in keyword.split() if kw.strip()]
    if not keywords:
        raise ValueError("關鍵字不得為空")

    seen2: set[str] = set()
    results2: list[dict] = []

    for kw in keywords:
        if max_total is not None and len(results2) >= max_total:
            logger.info(
                "窮盡搜尋已達 max_total=%d，跳過剩餘 keyword（%s）",
                max_total, kw,
            )
            break
        logger.info("窮盡搜尋啟動 keyword=%s（已累計 %d）", kw, len(results2))
        remaining = None if max_total is None else max_total - len(results2)
        items = await _exhaustive_single_keyword(
            keyword=kw,
            court=court,
            case_type=case_type,
            year_from=year_from,
            year_to=year_to,
            max_rounds=max_rounds,
            max_total=remaining,
        )
        for item in items:
            cid = item.get("jid") or item.get("case_id") or ""
            if cid and cid not in seen2:
                seen2.add(cid)
                results2.append(item)

    logger.info("窮盡搜尋完成（legacy OR），共 %d 筆（跨 keyword 去重後）", len(results2))
    return results2
