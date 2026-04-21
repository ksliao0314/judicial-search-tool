"""GET /tasks/{id}/judgments — 多分析層 JOIN 清單查詢。

查詢參數：
  primary_analysis_id    必填，主排序依據
  secondary_analysis_id  選填，副標籤
  min_score              選填，score 門檻（≥）
  court                  選填，法院名稱模糊搜尋
  year_from / year_to    選填，西元年（資料庫存西元）
"""
import asyncio
import datetime
import io
import logging
import re as _re_std
import zipfile
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from src.db import database as db

logger = logging.getLogger(__name__)


# 司法院一般判決（judgment.judicial.gov.tw）有三種 PDF URL 型態：
#   A. /FILES/{PREFIX}/{rest}.pdf           — 靜態 pre-generated PDF，只有 ispdf=1 判決可用
#   B. /PdfWithLine/GetPDF.ashx?jrecno=...  — 動態產生加行號 PDF，大多數判決適用、不需 session
#   C. /EXPORTFILE/ExportToPdf.aspx?...     — 動態產生原格式 PDF，直打一律 302 需 session，略
#
# 策略：A 為主（最輕量、新判決幾乎都有）、B 為 fallback（A 不存在時、大多能救回）、
#       都不行才 fallback 到 data.aspx 原文頁讓律師手動處理
def _extract_judicial_jid(judgment: dict) -> tuple[str, str] | None:
    """回 (court_prefix, rest_with_commas) 或 None。rest 保留原 commas 未編碼。"""
    source_url = judgment.get("source_url") or ""
    if source_url and "judgment.judicial.gov.tw" not in source_url:
        return None
    jid = None
    m = _re_std.search(r"[?&]id=([^&]+)", source_url)
    if m:
        try:
            from urllib.parse import unquote
            jid = unquote(m.group(1))
        except Exception:
            jid = m.group(1)
    if not jid:
        cid = judgment.get("case_id") or ""
        if _re_std.match(r"^[A-Z]+,", cid):
            jid = cid
    if not jid:
        return None
    parts = jid.split(",")
    if len(parts) < 2:
        return None
    return parts[0], ",".join(parts[1:])


def _judicial_pdf_url(judgment: dict) -> str | None:
    """Pattern A：靜態 /FILES/*.pdf (pre-generated)"""
    jid = _extract_judicial_jid(judgment)
    if not jid:
        return None
    prefix, rest = jid
    return f"https://judgment.judicial.gov.tw/FILES/{prefix}/{quote(rest, safe='')}.pdf"


def _pdfwithline_url(judgment: dict) -> str | None:
    """Pattern B：動態 /PdfWithLine/GetPDF.ashx（加行號 PDF、大多數判決適用）"""
    jid = _extract_judicial_jid(judgment)
    if not jid:
        return None
    prefix, rest = jid
    return (
        "https://judgment.judicial.gov.tw/PdfWithLine/GetPDF.ashx"
        f"?jrecno={quote(rest, safe='')}&tablename={prefix}"
    )


# cons.judicial.gov.tw PDF 解析：憲判字 / 釋字的 PDF 檔不是 URL 模板、
# internal_id 散落在 docdata.aspx 頁 HTML 中、<a title="下載文件「XXX.pdf」" href="/download/download.aspx?id=NNNNN">
#
# 司法院舊釋字的 PDF 生態系很亂（實證 416/436/500/585/700/813 等多樣本）：
#   · 新制憲判字：必有「判決_OCR.pdf」（律師要的全文）
#   · 舊制釋字：真正解釋文是「抄本{N}.pdf」(N = 釋字編號)、常常沒上傳
#     ‒ 釋字 700/813/400 0-10 筆、完全沒抄本 N
#     ‒ 釋字 500/585 有抄本但 585 缺失
#     ‒ 釋字 436 只有「其他公開之卷內文書」（不是解釋文）
#     ‒ 釋字 700 列的抄本是 698 的（資料錯誤、絕不能取）
#   · 意見書 / 聲請書 / 答辯書 / 立場表 / 卷內文書 / 案情摘要 都不是律師要的主文
#
# 策略：用 positive anchor（4 個夠具體的 pattern）按優先序選、找不到就 None（不 guess）
#   釋字 500 的 title 是「抄本500(含蘇大法官俊雄部分不同意見書).pdf」含「意見書」字串、
#   所以不能用 blacklist 預過濾、會誤殺真正想要的文件
_INTERP_NUM_RE = _re_std.compile(r"釋字第\s*(\d+)\s*號")


async def _cons_pdf_url(
    client: httpx.AsyncClient, source_url: str, case_id: str | None = None
) -> str | None:
    if "cons.judicial.gov.tw" not in source_url:
        return None
    try:
        resp = await client.get(source_url, timeout=_PDF_FETCH_TIMEOUT)
        if resp.status_code != 200:
            logger.debug("cons docdata %d for %s", resp.status_code, source_url)
            return None
        html = resp.text
    except Exception as e:
        logger.debug("cons docdata fetch err: %s", e)
        return None

    # 司法院 <a> 屬性順序不一致：憲判字頁是 title 先 / href 後、舊釋字頁是 href 先 / title 後。
    # 先抽 <a> 標籤、再從其 attr 字串中獨立找 href + title，順序無關
    pairs: list[tuple[str, str]] = []
    _HREF_RE = _re_std.compile(r'href="(/download/download\.aspx\?id=\d+)"')
    _TITLE_RE = _re_std.compile(r'title="下載文件「([^」]+\.pdf)」"')
    for m in _re_std.finditer(r"<a\s+([^>]+)>", html):
        attrs = m.group(1)
        href_m = _HREF_RE.search(attrs)
        title_m = _TITLE_RE.search(attrs)
        if href_m and title_m:
            pairs.append((title_m.group(1), href_m.group(1)))
    if not pairs:
        return None

    # 從 case_id 抽釋字編號（僅舊制）、用於驗抄本號對得上
    interp_num: str | None = None
    if case_id:
        m = _INTERP_NUM_RE.search(case_id)
        if m:
            interp_num = m.group(1)

    def pick_first(predicate) -> str | None:
        for t, h in pairs:
            if predicate(t):
                return h
        return None

    # 1. 憲判字主文全文（最常見命中）
    h = pick_first(lambda t: "判決_OCR" in t)
    if h: return f"https://cons.judicial.gov.tw{h}"

    # 2. 舊釋字：抄本{N} 必須編號對得上、避免釋字 700 頁面誤抓「抄本698」
    if interp_num:
        pat = f"抄本{interp_num}"
        h = pick_first(lambda t: pat in t)
        if h: return f"https://cons.judicial.gov.tw{h}"

    # 3. 未來若司法院補上「解釋文 / 解釋理由書」PDF — 目前 0 筆命中、保險留著
    h = pick_first(lambda t: "解釋文" in t or "解釋理由書" in t)
    if h: return f"https://cons.judicial.gov.tw{h}"

    # 4. 憲判字摘要 fallback（律師點了就是想看內容、摘要總比完全 404 好）
    h = pick_first(lambda t: "判決摘要" in t)
    if h: return f"https://cons.judicial.gov.tw{h}"

    # 5. 沒有對得上的主體文件 → 不 guess、告訴前端此判決沒 PDF
    return None

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
    """取得單筆判決全文（供閱讀器頁使用）。

    舊制釋字（釋字第1-813號）：額外 inject `sections` 結構（本院見解 / 聲請意旨 /
    結論 / 大法官署名）供 Reader UI 顯示 sub-header。新制憲判字 / 一般判決不動。
    """
    judgments = await db.get_task_judgments(task_id)
    for j in judgments:
        if j["case_id"] == case_id:
            _maybe_attach_interp_sections(j)
            return j
    raise HTTPException(status_code=404, detail="Judgment not found")


# 舊制釋字 reader sections 注入 — 失敗 fail-safe（不存在 sections 欄位、Reader fallback 純文字渲染）
# Path manipulation：iCloud UF_HIDDEN 讓 .pth 被 skip（見 CLAUDE.md / tech gotchas 記憶）、
# 改用手動 sys.path 補救、不依賴 editable install 的 finder。
import re as _re
import sys as _sys
from pathlib import Path as _Path
_MCP_FORK = _Path(__file__).resolve().parents[2] / "mcp-taiwan-legal-db"
if _MCP_FORK.exists() and str(_MCP_FORK) not in _sys.path:
    _sys.path.insert(0, str(_MCP_FORK))
try:
    from mcp_server.parsers.interpretation_parser import parse_interpretation as _parse_interp
except ImportError:
    _parse_interp = None  # type: ignore[assignment]

from src.pipeline.cons_normalizer import is_old_interpretation as _is_old_interp
_OLD_CID_RE = _re.compile(r"釋字第?\s*(\d+)\s*號?")


def _maybe_attach_interp_sections(judgment: dict) -> None:
    """舊制釋字專用 — inject `interp_sections` 欄位到 judgment dict。

    新制憲判字、一般判決：函式 no-op、直接返回（不新增任何欄位）。
    Parser 失敗或無 reasoning：靜默略過、Reader 走 fallback 純文字路徑。
    """
    if _parse_interp is None:
        return
    case_id = judgment.get("case_id") or ""
    if not _is_old_interp(case_id):
        return
    m = _OLD_CID_RE.search(case_id)
    if not m:
        return
    reasoning = judgment.get("reasoning") or ""
    if not reasoning.strip():
        return
    try:
        parsed = _parse_interp(
            cid=int(m.group(1)),
            main_text=judgment.get("main_text") or "",
            reasoning=reasoning,
            issues=judgment.get("facts") or "",
        )
        sections = parsed.get("sections") or []
        if sections:
            judgment["interp_sections"] = sections
            judgment["interp_era"] = parsed.get("era", "")
    except Exception:
        return


# 舊的 GET /tasks/{id}/judgments/{cid}/pdf endpoint（用 reportlab 自產 PDF）已移除：
# v1.0.3 起所有單筆 PDF 下載改走 /api/pdf-url 取司法院原版 URL（見下）、前端用 window.open。
# reportlab 依賴一併拿掉、省掉 ~12 MB 冷依賴。


@router.get("/pdf-url")
async def resolve_pdf_url(
    source_url: str = Query(..., description="判決 source_url"),
    case_id: str | None = Query(None, description="判決字號（舊制釋字用來比對抄本編號）"),
):
    """解析 source_url 對應的 PDF 直接下載連結。

    · judgment.judicial.gov.tw：URL 模板組 + HEAD 檢查 Content-Type；若 server 回 HTML
      （ispdf=0、舊判決無 pre-generated PDF）→ fallback 回 source_url 本身，讓前端開原文頁、
      律師手動點司法院「轉存PDF」按鈕動態產生（ExportToPdf.aspx 直接打一律 302 → Errorpage，需瀏覽器 session）
    · cons.judicial.gov.tw：抓 docdata 頁 HTML、按優先序找主體文件的 download.aspx URL

    回傳 `{url, kind: "direct"|"fallback", detail?}`；完全不可用才回 404。
    """
    if not source_url or not source_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="invalid source_url")

    # 一般判決：依序嘗試 /FILES/*.pdf → /PdfWithLine/GetPDF.ashx → fallback data.aspx
    if "judgment.judicial.gov.tw" in source_url:
        candidates = []
        files_url = _judicial_pdf_url({"source_url": source_url})
        if files_url:
            candidates.append(("files", files_url))
        withline_url = _pdfwithline_url({"source_url": source_url})
        if withline_url:
            candidates.append(("withline", withline_url))
        if not candidates:
            raise HTTPException(status_code=404, detail="無法從 source_url 解析 JID")

        async with httpx.AsyncClient(
            headers={"User-Agent": _PDF_USER_AGENT},
            follow_redirects=True,
        ) as client:
            for label, cand in candidates:
                try:
                    resp = await client.head(cand, timeout=15.0)
                    ct = resp.headers.get("content-type", "").lower()
                    if resp.status_code == 200 and "pdf" in ct:
                        return {"url": cand, "kind": "direct"}
                except Exception as e:
                    logger.debug("HEAD %s err: %s", label, e)

        # 兩條路都失敗 → fallback 開原文頁、律師手動轉存
        return {
            "url": source_url,
            "kind": "fallback",
            "detail": "此判決司法院直接下載連結失效、請於開啟的原文頁點「轉存PDF」按鈕手動產生",
        }

    # 憲判字 / 釋字：抓 docdata 頁解析
    if "cons.judicial.gov.tw" in source_url:
        async with httpx.AsyncClient(
            headers={"User-Agent": _PDF_USER_AGENT},
            follow_redirects=True,
        ) as client:
            url = await _cons_pdf_url(client, source_url, case_id=case_id)
        if not url:
            raise HTTPException(status_code=404, detail="司法院未提供此判決 PDF")
        return {"url": url, "kind": "direct"}

    raise HTTPException(status_code=404, detail="unsupported host")


class BulkPdfRequest(BaseModel):
    case_ids: list[str]


# 司法院 PDF 批次抓取：concurrency 限到 2 避免觸發 WAF rate-limit
# 每個 request 45s timeout + 3 次指數退避重試（1s / 3s / 9s）
# 實測司法院對 /FILES/*.pdf 有 ReadError-style 斷線；retry 幾乎都能救回
_PDF_FETCH_CONCURRENCY = 2
_PDF_FETCH_TIMEOUT = 45.0
_PDF_FETCH_RETRIES = 3
_PDF_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _fetch_one_pdf(
    client: httpx.AsyncClient, judgment: dict, sem: asyncio.Semaphore
) -> tuple[str, bytes | None, str | None]:
    """抓一筆司法院原版 PDF。return (case_id, pdf_bytes | None, error | None)

    兩種來源：
      · judgment.judicial.gov.tw 一般判決：用 _judicial_pdf_url URL 模板直抓
      · cons.judicial.gov.tw 憲判字 / 釋字：先抓 docdata.aspx 解析出 download.aspx URL 再下載

    WAF 對 /FILES/*.pdf 偶有 TCP 層斷線（httpx → httpcore.ReadError）；
    固定退避重試 3 次覆蓋絕大多數 transient 失敗。4xx/5xx 直接回不重試。
    """
    case_id = judgment.get("case_id") or ""
    source_url = judgment.get("source_url") or ""

    # Resolve PDF URL candidates：一般判決有兩條可試、cons 只有一條
    candidates: list[str] = []
    files_url = _judicial_pdf_url(judgment)
    if files_url:
        candidates.append(files_url)
    withline_url = _pdfwithline_url(judgment)
    if withline_url:
        candidates.append(withline_url)
    if not candidates and "cons.judicial.gov.tw" in source_url:
        cons_url = await _cons_pdf_url(client, source_url, case_id=case_id)
        if cons_url:
            candidates.append(cons_url)
    if not candidates:
        return case_id, None, f"司法院未提供此判決 PDF（請至原文頁 {source_url} 手動轉存）"

    last_err = None
    async with sem:
        for candidate_url in candidates:
            for attempt in range(_PDF_FETCH_RETRIES):
                try:
                    resp = await client.get(candidate_url, timeout=_PDF_FETCH_TIMEOUT)
                    if resp.status_code == 404:
                        last_err = "HTTP 404"
                        break  # 404 不是 transient、換下一個 candidate
                    if resp.status_code != 200:
                        last_err = f"HTTP {resp.status_code}"
                        if resp.status_code < 500:
                            break  # 4xx 不重試、換下一個 candidate
                    else:
                        ct = resp.headers.get("content-type", "")
                        if "pdf" not in ct.lower():
                            # 非 PDF（通常是 HTML 錯誤頁）→ 換下一個 candidate
                            last_err = f"非 PDF content-type: {ct}"
                            break
                        return case_id, resp.content, None
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                    last_err = f"{type(e).__name__}: {e}"
                    logger.debug("PDF fetch attempt %d failed: %s", attempt + 1, last_err)
                if attempt < _PDF_FETCH_RETRIES - 1:
                    await asyncio.sleep(3 ** attempt)

    return case_id, None, (
        f"司法院所有下載管道皆失效（{last_err}）、"
        f"請至原文頁 {source_url} 手動點「轉存PDF」"
    )


@router.post("/tasks/{task_id}/judgments/bulk-pdf")
async def download_bulk_pdf(task_id: str, body: BulkPdfRequest):
    """批次抓司法院原版 PDF、打包為 zip 回傳。

    比起先前版本（reportlab 自產仿司法院格式 PDF），這裡直抓 /FILES/*.pdf 原版、
    結果是完全相同於律師在司法院網頁點「轉存PDF」的 PDF 檔。

    Fallback：URL 組不出（舊制釋字 case_id 沒 JID）或抓失敗的判決不入 zip，
    同一包裡附 `_失敗清單.txt` 記錄跳過的項目供律師手動下載。
    """
    if not body.case_ids:
        raise HTTPException(status_code=400, detail="case_ids 不可為空")

    judgments_all = await db.get_task_judgments(task_id)
    judgment_map = {j["case_id"]: j for j in judgments_all}

    # 過濾出有對應 task_judgments row 的 case_ids（不存在的略過）
    targets = [judgment_map[cid] for cid in body.case_ids if cid in judgment_map]
    if not targets:
        raise HTTPException(status_code=404, detail="無符合的判決")

    sem = asyncio.Semaphore(_PDF_FETCH_CONCURRENCY)
    async with httpx.AsyncClient(
        headers={"User-Agent": _PDF_USER_AGENT},
        follow_redirects=True,
    ) as client:
        results = await asyncio.gather(
            *(_fetch_one_pdf(client, j, sem) for j in targets)
        )

    buf = io.BytesIO()
    success = 0
    failures: list[tuple[str, str]] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for case_id, pdf_bytes, err in results:
            if pdf_bytes is None:
                failures.append((case_id, err or "unknown"))
                continue
            safe_name = (case_id or "judgment").replace("\u3000", "").replace(" ", "")[:80]
            zf.writestr(f"{safe_name}.pdf", pdf_bytes)
            success += 1
        if failures:
            lines = [
                f"以下 {len(failures)} 筆判決 PDF 抓取失敗，請至司法院網頁手動下載：",
                "",
            ]
            for cid, reason in failures:
                lines.append(f"  · {cid}  —  {reason}")
            zf.writestr("_失敗清單.txt", "\n".join(lines).encode("utf-8"))

    if success == 0:
        raise HTTPException(
            status_code=502,
            detail=f"全部 {len(targets)} 筆判決抓取失敗：{failures[0][1] if failures else 'unknown'}",
        )

    today = datetime.date.today().isoformat()
    filename = f"judicial_pdfs_{today}.zip"
    logger.info(
        "bulk-pdf: task=%s requested=%d success=%d failed=%d",
        task_id, len(body.case_ids), success, len(failures),
    )
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

