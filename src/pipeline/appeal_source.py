"""勞動部訴願決定書 來源 adapter（app 端，非 MCP）。

來源：勞工行政救濟案件資訊整合平台 https://appealweb.mol.gov.tw
- 搜尋 `/Appeal/AppealCaseDecisionResult`：有圖形驗證碼（5 碼數字）+ session token → 用 Claude vision 解。
- 全文詳情 `/Appeal/AppealCaseDecisionContent?caseId=`：**公開、無驗證碼**、可並行抓。

對外提供與司法院來源對齊的兩個動作，讓既有 pipeline（過濾 / AI 精讀 / synthesis /
reader / 匯出）完全重用：
- search_appeals(...)        ≈ search_judgments：回命中清單（字號 / 案號 / 日期 / 主文 / source_url）
- get_appeal_decision(...)   ≈ get_judgment：回結構化全文（main_text / facts / reasoning / full_text）

驗證碼只擋搜尋、不擋全文 → 一次搜尋解一張驗證碼，後續逐筆全文免驗證碼。
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_BASE = "https://appealweb.mol.gov.tw"
_SEARCH_PAGE = f"{_BASE}/Appeal/AppealCaseDecision"
_RESULT_URL = f"{_BASE}/Appeal/AppealCaseDecisionResult"
_CAPTCHA_URL = f"{_BASE}/Appeal/GetValidateCode"
_DETAIL_URL = f"{_BASE}/Appeal/AppealCaseDecisionContent"
_COURT = "勞動部"

# 決定結果（rulingBookResultType）：站台 select 的代碼 → 中文。供 UI 給使用者選。
RESULT_TYPES = {"DENY": "駁回", "REVOKE": "撤銷", "NOT_ACCEPTED": "不受理"}

# 單一 OR group 搜尋的硬上限（翻頁累積到此即停）。runner 用它偵測「結果被截斷」→
# 推 truncated 旗標 + warning，避免律師誤以為已涵蓋全部（無聲偽陰性）。
APPEAL_SEARCH_CAP = 500
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 結果頁表格欄位序：序號 / 案號 / 發文日期 / 決定文號(字號) / 決定書主文 / 決定書連結
_COL_CASE_NO = 1
_COL_DATE = 2
_COL_WEN_HAO = 3
_COL_MAIN = 4
_COL_LINK = 5


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _roc_to_iso(s: str) -> str:
    """民國日期『115/5/18』→ ISO『2026-05-18』。解析失敗回原字串。"""
    m = re.match(r"\s*(\d{2,3})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})", s or "")
    if not m:
        return (s or "").strip()
    y = int(m.group(1)) + 1911
    return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _clean_lines(raw: str) -> list[str]:
    """逐行去除前後（含全形）空白、丟空行。"""
    out = []
    for ln in raw.split("\n"):
        ln = ln.replace("　", " ").strip()
        if ln:
            out.append(ln)
    return out


def _extract_token(html: str) -> str | None:
    # 用 BeautifulSoup 取值、不對屬性順序 / 引號樣式敏感（ASP.NET 慣用 name-first 雙引號，
    # 但改版風險高；舊 regex 寫死順序+雙引號會在改版時取不到 token 而整個搜尋失敗）。
    el = BeautifulSoup(html, "html.parser").find("input", attrs={"name": "token"})
    return el.get("value") if el else None


def _result_is_bounced(html: str) -> bool:
    """搜尋失敗（驗證碼錯）會被打回搜尋表單 → 表單上會再出現驗證碼輸入框。

    只用正向訊號「請輸入下方圖片」（驗證碼輸入框 placeholder）判定，不用「無決定書連結」
    當代理 —— 合法 0 筆結果頁也沒有決定書連結、會被誤判為 bounced 而連續 retry→失敗。
    """
    return "請輸入下方圖片" in html


# ---------------------------------------------------------------------------
# 解析器（純函式，可離線測）
# ---------------------------------------------------------------------------

def parse_result_rows(html: str) -> list[dict]:
    """解析搜尋結果頁 → 命中清單。每筆對齊 task_judgments 骨架。

    健壯化（防站台改欄序時整頁靜默丟失）：
    - 連結/caseId 欄用「含 caseId 的 <a>」在任一 td 動態定位，不依賴寫死 index。
    - 其餘欄（案號/發文日期/決定文號/主文）優先用表頭 th 文字對應；無表頭才退回寫死序位 _COL_*。
    """
    soup = BeautifulSoup(html, "html.parser")
    # 表頭 th 文字 → 欄 index（站台調整欄序時用；th[i] 與 td[i] 對齊）
    hdr: dict[str, int] = {}
    for tr in soup.select("table tr"):
        ths = tr.find_all("th")
        if ths:
            for i, th in enumerate(ths):
                t = th.get_text(strip=True)
                for key in ("案號", "日期", "文號", "主文"):
                    if key in t and key not in hdr:
                        hdr[key] = i
            break  # 只取第一個含 th 的列當表頭

    def _col(tds, key, fallback_idx):
        idx = hdr.get(key, fallback_idx)
        return tds[idx].get_text(strip=True) if 0 <= idx < len(tds) else ""

    rows: list[dict] = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        # 動態找含 caseId 連結的欄（不依賴寫死 _COL_LINK）
        case_id_internal = ""
        for td in tds:
            a = td.find("a", href=True)
            if a:
                m = re.search(r"caseId=(\d+)", a["href"])
                if m:
                    case_id_internal = m.group(1)
                    break
        if not case_id_internal:
            continue
        case_no = _col(tds, "案號", _COL_CASE_NO)               # 11500586041W05
        wen_hao = _col(tds, "文號", _COL_WEN_HAO)               # 勞動法訴一字第1150007704號
        rows.append({
            "case_id": wen_hao or case_no,                     # 字號為主鍵（律師引用用）
            "case_no": case_no,
            "court": _COURT,
            "date": _roc_to_iso(_col(tds, "日期", _COL_DATE)),
            "main_text": _col(tds, "主文", _COL_MAIN),          # 訴願駁回 / 不受理 / 撤銷…
            "source_url": f"{_DETAIL_URL}?caseId={case_id_internal}",
            "case_id_internal": case_id_internal,
        })
    return rows


def to_search_hit(row: dict) -> dict:
    """search_appeals 單筆 → task_search_hits row 格式（對齊 bulk_insert_task_search_hits）。

    source_url 帶 caseId、Stage 1 就持久化進 task_search_hits，Stage 2.5 fetch
    用它走 get_appeal_decision 抓全文（免驗證碼）。
    """
    return {
        "jid": row["case_id"],                       # 字號為 case_id（律師引用用）
        "case_id": row["case_id"],
        "court": row.get("court", _COURT),
        "date": row.get("date", ""),
        "url": row.get("source_url", ""),            # 詳情 URL（含 caseId）
        "cause": "",
        "summary": row.get("main_text", ""),         # 主文當預覽（訴願駁回/不受理/撤銷）
    }


def parse_pagination(html: str) -> dict:
    """偵測結果頁分頁資訊（提示用；search loop 不依賴它）→ {total, page_count}。

    分頁連結是 javascript:void(0)、頁碼在 .pagination 的文字裡（非 URL 參數），
    故 page_count 取「目前可見的最大頁碼」（受分頁視窗限制、未必是總頁數）。
    """
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    m = re.search(r"共\s*([\d,]+)\s*筆", html)
    if m:
        total = int(m.group(1).replace(",", ""))
    page_count = 0
    for el in soup.select(".pagination .page-link, .pagination a, .pagination li"):
        t = el.get_text(strip=True)
        if t.isdigit():
            page_count = max(page_count, int(t))
    return {"total": total, "page_count": page_count}


def parse_detail(html: str, source_url: str = "") -> dict | None:
    """解析訴願決定書詳情頁 → 結構化欄位（對齊 get_judgment 回傳）。

    結構：勞動部訴願決定書 / 字號 / 訴願人:.. / 導言(本部決定如下) / 主文 / 理由 /
          據上論結 / 訴願審議委員會 委員… / 中華民國…日 / 救濟教示。
    映射：main_text←主文、reasoning←理由、facts←導言段、full_text←整段。
    """
    soup = BeautifulSoup(html, "html.parser")
    flow = soup.select_one(".con-flow")
    if not flow:
        return None
    lines = _clean_lines(flow.get_text("\n"))
    if not lines:
        return None
    full = "\n".join(lines)

    # 字號：開頭數行中含「訴…字第…號」、但不是標題「勞動部訴願決定書」那行
    wen_hao = ""
    for ln in lines[:6]:
        if re.search(r"字第\s*\d+\s*號", ln) and "決定書" not in ln:
            wen_hao = ln
            break

    main_text = _slice(full, "\n主文\n", ["\n理由\n", "\n事實\n"])
    reasoning = _slice(full, "\n理由\n", ["據上論結", "\n訴願審議委員會", "\n中華民國"])
    # 導言（事實）：開頭「訴願人因…不服…提起訴願，本部決定如下：」整段。
    # 訴願書常把事實併進理由，故 facts 放這段導言、實質分析在 reasoning。
    facts = _lead_paragraph(lines)

    # 理由解析失敗（標題非標準如「事實及理由」「核其理由」、或整段版面異常）→ 退回 full_text
    # 當 reasoning，避免 run_analysis_v2 收到空理由而靜默評 0（漏判）。只要 reasoning 空就退，
    # 不再要求 main_text 也空 —— 主文有解析出來但理由沒有，是常見的部分失敗，仍須補 reasoning。
    if not reasoning and full:
        reasoning = full

    return {
        "case_id": wen_hao,
        "court": _COURT,
        "date": _extract_doc_date(full),
        "main_text": main_text,
        "facts": facts,
        "reasoning": reasoning,
        "cited_statutes": None,
        "full_text": full,
        "source_url": source_url,
    }


def _slice(full: str, start: str, stops: list[str]) -> str:
    si = full.find(start)
    if si < 0:
        return ""
    si += len(start)
    ei = len(full)
    for st in stops:
        p = full.find(st, si)
        if p >= 0:
            ei = min(ei, p)
    return full[si:ei].strip("\n 　").strip()


def _lead_paragraph(lines: list[str]) -> str:
    for ln in lines:
        if ln.startswith("訴願人因") or ("不服" in ln and "提起訴願" in ln):
            return ln
    return ""


def _extract_doc_date(full: str) -> str:
    # 取「最後一個」中華民國日期：落款的訴願決定日在文末，前面出現的多是引用他機關
    # 處分日 / 法院裁判日（如「不服…中華民國115年1月7日…處分」），取第一個會抓到引用日。
    ms = list(re.finditer(r"中華民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", full))
    if not ms:
        return ""
    m = ms[-1]
    y = int(m.group(1)) + 1911
    return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


# ---------------------------------------------------------------------------
# 驗證碼（Claude vision）
# ---------------------------------------------------------------------------

async def solve_captcha_via_claude(client, image_bytes: bytes, *, model: str) -> str:
    """用 Claude vision 讀 5 碼數字驗證碼。client 為 anthropic.AsyncAnthropic。"""
    import base64
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = await client.messages.create(
        model=model,
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text":
                    "這是驗證碼圖片，內含 5 個阿拉伯數字（紅色），可能有干擾線。"
                    "只回傳那 5 個數字、不要任何其他文字或標點。"},
            ],
        }],
    )
    text = (resp.content[0].text if resp.content else "").strip()
    digits = re.sub(r"\D", "", text)
    return digits


# ---------------------------------------------------------------------------
# 對外：搜尋 + 抓全文
# ---------------------------------------------------------------------------

def _build_search_params(keywords: list[str], case_year: str | None,
                         result_type: str | None, token: str, valid_code: str,
                         law_name: str | None = None) -> dict:
    kws = [k for k in (keywords or []) if k][:3]
    # operator 必須用站台代碼 "0"(且含/AND) / "1"(或含/OR)，不可送 "and" 字串 —— 實測
    # 送無效值會讓 appealweb 後端忽略 lawName + rulingBookResultType 篩選、回傳未篩選的
    # 全部關鍵字命中（420 vs 官方 12）。群內多關鍵字一律 AND（OR 由上層拆 group 各搜），故用 "0"。
    params = {
        "outgoingWordNum": "", "lawName": law_name or "", "caseSerialNumber": "",
        "contentPublic1": kws[0] if len(kws) > 0 else "",
        "operator1": "0",
        "contentPublic2": kws[1] if len(kws) > 1 else "",
        "operator2": "0",
        "contentPublic3": kws[2] if len(kws) > 2 else "",
        "caseYear": case_year or "",
        "molCaseDisposalWordNum": "", "molCaseReviewResultsWordNum": "",
        "rulingBookResultType": result_type or "",
        "validCode": valid_code, "token": token,
    }
    return params


# 案件類別（lawName）選項由站台 AJAX `/Appeal/AjaxGetLawList` 動態提供（28 項、偶有增修），
# 故 app 端不寫死、改 proxy 即時取得，UI select 與原站「完全比照」且自動同步。
_LAW_LIST_URL = f"{_BASE}/Appeal/AjaxGetLawList"


async def get_appeal_categories(client: httpx.AsyncClient | None = None) -> list[str]:
    """回傳訴願「案件類別」清單（lawName 字串、依站台 sort 排序）。

    對應搜尋表單的 `<select name=lawName>`（label「案件類別」），選項 value = lawName 字串
    （站台 JS：`<option value=data[i].lawName>`），故搜尋時把選定字串直接當 lawName 送出。
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"User-Agent": _UA}, follow_redirects=True,
                                   timeout=30.0)
    try:
        r = await client.get(_LAW_LIST_URL, headers={"X-Requested-With": "XMLHttpRequest"})
        if r.status_code != 200:
            return []
        data = r.json()
        items = [d for d in data if isinstance(d, dict) and d.get("lawName")]
        items.sort(key=lambda d: d.get("sort", 9999))
        # 去重保序（站台理論上不重，但防呆）
        seen, out = set(), []
        for d in items:
            name = str(d["lawName"]).strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out
    except Exception as exc:
        logger.warning("取訴願案件類別清單失敗：%s", exc)
        return []
    finally:
        if own:
            await client.aclose()


async def search_appeals(
    keywords: list[str],
    *,
    solve_captcha,                 # async callable(image_bytes) -> str（5 碼）
    case_year: str | None = None,
    result_type: str | None = None,
    law_name: str | None = None,   # 案件類別（lawName 字串），None=全部
    max_captcha_retries: int = 4,
    max_results: int = APPEAL_SEARCH_CAP,
    max_pages: int = 80,
    page_delay: float = 0.8,       # 翻頁間禮貌延遲（秒）
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """關鍵字搜尋訴願決定書 → 回命中清單。

    一次解驗證碼搜出 page 1，後續翻頁帶 pageNumber + 接力 token（免驗證碼，實測確認）。
    去重防最後一頁回捲、max_results/max_pages 雙重截斷、page_delay 禮貌節流。
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"User-Agent": _UA}, follow_redirects=True,
                                   timeout=45.0)
    try:
        main = await client.get(_SEARCH_PAGE)
        token = _extract_token(main.text)
        if not token:
            raise RuntimeError("訴願搜尋頁取不到 token")

        # ── page 1：解驗證碼 + 搜尋（含 retry 換驗證碼）──
        page1_html: str | None = None
        base_params: dict | None = None
        for attempt in range(max_captcha_retries):
            try:
                img = (await client.get(_CAPTCHA_URL)).content
                code = await solve_captcha(img)
                if len(code) != 5:
                    logger.info("appeal captcha 非 5 碼（%r）換一張重試", code)
                    continue
                params = _build_search_params(keywords, case_year, result_type, token, code,
                                              law_name=law_name)
                # 搜尋請求也納入 try：連線逾時 / 503 / WAF 擋時換驗證碼重試，
                # 不可讓 transient 失敗直接穿出、害整個 OR group 中止。
                r = await client.get(_RESULT_URL, params=params)
            except Exception as exc:
                logger.info("appeal 取/解驗證碼或搜尋請求失敗（attempt %d）：%s，重試",
                            attempt + 1, exc)
                continue
            if _result_is_bounced(r.text):
                logger.info("appeal 搜尋被打回（驗證碼錯 attempt %d）", attempt + 1)
                continue
            page1_html = r.text
            base_params = {k: v for k, v in params.items() if k != "validCode"}
            break
        if page1_html is None:
            raise RuntimeError(f"訴願搜尋驗證碼連續 {max_captcha_retries} 次失敗")

        results = parse_result_rows(page1_html)
        seen = {x["case_id_internal"] for x in results}
        next_token = _extract_token(page1_html)
        page = 2
        # ── page 2..N：免驗證碼翻頁 ──
        while (results and next_token and len(results) < max_results
               and page <= max_pages):
            await asyncio.sleep(page_delay)
            pg = dict(base_params)
            pg["token"] = next_token
            pg["pageNumber"] = str(page)
            pg["pageSize"] = "10"
            r = await client.get(_RESULT_URL, params=pg)
            if _result_is_bounced(r.text):
                break
            rows = parse_result_rows(r.text)
            new = [x for x in rows if x["case_id_internal"] not in seen]
            if not new:                       # 回捲到看過的批次 → 到底了
                break
            for x in new:
                seen.add(x["case_id_internal"])
            results.extend(new)
            next_token = _extract_token(r.text)
            page += 1

        logger.info("appeal 搜尋『%s』命中 %d 筆（%d 頁）", " ".join(keywords or []),
                    len(results), page - 1)
        return results[:max_results]
    finally:
        if own:
            await client.aclose()


async def get_appeal_decision(source_url_or_case_id: str, *,
                              client: httpx.AsyncClient | None = None) -> dict | None:
    """抓單筆訴願決定書全文（無驗證碼）。可傳 source_url 或內部 caseId。"""
    if source_url_or_case_id.startswith("http"):
        url = source_url_or_case_id
    else:
        url = f"{_DETAIL_URL}?caseId={source_url_or_case_id}"
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"User-Agent": _UA}, follow_redirects=True,
                                   timeout=45.0)
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        return parse_detail(r.text, source_url=url)
    finally:
        if own:
            await client.aclose()
