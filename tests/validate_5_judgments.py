"""驗證 5 則指定判決的 MCP 欄位完整性與解析器 / 排版相關結構訊號。

跑法（在 judgment-search/ 目錄下）：
    .venv/bin/python tests/validate_5_judgments.py

報告內容對應 CLAUDE.md 與 memory 中的解析器知識：
- MCP 欄位完整性（reasoning / main_text / facts / cited_statutes / full_text）
- L0-L5 階層 marker 分布（含 PUA 延伸）
- Boundary check edge cases（函號、之X、第X條 等可能的誤判）
- 引號統計（normal / citation mode 雙閾值風險）
- MCP 硬斷行特徵（行長分布、% mid-sentence）
- ASCII 表格偵測
- Informal L1 短語偵測
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

# 讓 src.* import 找得到
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src import mcp_client  # noqa: E402

# ---------------------------------------------------------------------------
# 待驗證的 5 則判決
# ---------------------------------------------------------------------------
CASES = [
    # (顯示名, search keyword, court hint)
    ("臺灣高等法院 104 建上 98",     "104年度建上字第98號",     "臺灣高等法院"),
    ("臺灣臺北地方法院 103 建 401", "103年度建字第401號",     "臺灣臺北地方法院"),
    ("臺中高等行政法院 106 訴 93",   "106年度訴字第93號",       "臺中高等行政法院"),
    ("臺中高等行政法院 107 訴更一 17", "107年度訴更一字第17號", "臺中高等行政法院"),
    ("最高行政法院 110 上 74",       "110年度上字第74號",       "最高行政法院"),
]

# ---------------------------------------------------------------------------
# 解析器規則的 Python 鏡像（與 JS parseJudgmentParagraphs 對應）
# ---------------------------------------------------------------------------
BOUNDARY_CHARS = set("。；：？！?!.\n\r\t \u3000」』）)")

L0_CHARS = set("壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸")
# L1: 中文數字 + 「、」
L1_PATTERN = re.compile(r"^([一二三四五六七八九十]{1,4})、")
# L2 unicode: U+3220–3229 ㈠–㈩
L2_UNICODE = set(chr(c) for c in range(0x3220, 0x322A))
# L2 paren: （一） 或 (一)
L2_PAREN = re.compile(r"^[（(]([一二三四五六七八九十]{1,3})[）)]")
# L3 unicode: U+2488–249B ⒈–⒛
L3_UNICODE = set(chr(c) for c in range(0x2488, 0x249C))
# L3 arabic: 1. 2. 但要 boundary + 後接非數字
L3_ARABIC = re.compile(r"^(\d{1,2})\.(?!\d)")
# L4 unicode: U+2474–2487 ⑴–⒇
L4_UNICODE = set(chr(c) for c in range(0x2474, 0x2488))
# L4 paren: (1) (2) (10)
L4_PAREN = re.compile(r"^[（(](\d{1,2})[）)]")
# L5: U+2460–2473 ①–⑳
L5_UNICODE = set(chr(c) for c in range(0x2460, 0x2474))
# PUA 延伸 ⑪⑫... (司法院自定，U+E000–F8FF)
PUA_RANGE = (0xE000, 0xF8FF)

# Citation prefix patterns（引號雙閾值偵測用）
CITATION_PREFIXES = re.compile(
    r"(?:判決(?:意旨)?|裁定|解釋|函釋|要旨|意旨|略以|略謂|"
    r"明定|明文規定|揭示|認為|指出|參照|規定|條)$"
)
CASE_NUMBER_RE = re.compile(r"\d+年度?\S{1,20}字第?\d+號")

# 已知會被誤判為 outline marker 的 false positive pattern
FP_PATTERNS = {
    "函號／字號內含 ㈠–㈩": re.compile(
        r"[\u4e00-\u9fff]{1,4}[\u3220-\u3229][\u4e00-\u9fff]{0,3}字第"
    ),
    "之 + L2/L3/L4 接續": re.compile(
        r"之[\u3220-\u3229\u2488-\u249B\u2474-\u2487\u2460-\u2473（(]"
    ),
    "第 + 阿拉伯數字 + 條／項／款": re.compile(r"第\d{1,3}[條項款]"),
    "小數點數字（疑似 L3 誤判風險）": re.compile(r"(?<!\d)(\d{1,2})\.(\d)"),
}

# Informal L1 短語（無正式 L0/L1 marker 時 promotion 用）
INFORMAL_L1_PATTERNS = [
    re.compile(r"上訴人(?:起訴|聲請)?(?:主張|陳稱|辯稱|則以|則稱|略謂|略以)"),
    re.compile(r"被上訴人(?:則以|答辯|辯稱|則稱|抗辯|略以|略謂)"),
    re.compile(r"原告(?:起訴)?(?:主張|陳稱|聲明|略稱|略謂)"),
    re.compile(r"被告(?:則以|答辯|辯稱|抗辯|略以)"),
    re.compile(r"原審(?:法院)?(?:認|以為|認定|略以|略謂)"),
    re.compile(r"原判決(?:認|以|略|認定|論述)"),
    re.compile(r"本院(?:按|認為|之判斷|查|審酌|以為)"),
]

TABLE_CHARS = set("┌┐└┘├┤┬┴┼│─")
SENTENCE_TERMINATORS = set("。！？!?")


# ---------------------------------------------------------------------------
# MCP 互動：找 JID
# ---------------------------------------------------------------------------

async def find_jid(keyword: str, court: str) -> dict | None:
    """用 case number 當 keyword 搜尋，回傳第一筆吻合 court 的判決。"""
    try:
        results = await mcp_client.search_judgments(
            keyword=keyword,
            court=court,
            max_results=20,
        )
    except Exception as exc:
        print(f"  [search 失敗] {exc}")
        return None

    if not results:
        # fallback 不指定法院再試一次
        try:
            results = await mcp_client.search_judgments(keyword=keyword, max_results=20)
        except Exception as exc:
            print(f"  [search fallback 失敗] {exc}")
            return None

    # 找完全 match keyword（去空白）的第一筆
    target_compact = keyword.replace(" ", "")
    for r in results:
        case_id = (r.get("case_id") or r.get("title") or "").replace(" ", "")
        if target_compact in case_id:
            return r
    # 沒有精確 match 就回第一筆
    return results[0] if results else None


# ---------------------------------------------------------------------------
# 分析函式
# ---------------------------------------------------------------------------

def field_completeness(jud: dict) -> dict:
    """欄位完整性：長度與 sanity check。"""
    fields = ("main_text", "facts", "reasoning", "full_text")
    rep = {}
    for f in fields:
        v = jud.get(f) or ""
        rep[f] = {
            "length": len(v),
            "lines": v.count("\n") + 1 if v else 0,
            "empty": not v.strip(),
        }
    rep["cited_statutes"] = {
        "count": len(jud.get("cited_statutes") or []),
    }
    # 已知 bug：reasoning 空但 facts 巨大 → MCP parser 把合併段歸錯
    rep["_warn_reasoning_empty"] = (
        rep["reasoning"]["empty"] and rep["facts"]["length"] > 1000
    )
    return rep


def marker_scan(text: str) -> dict:
    """掃 L0-L5 marker 出現次數（無 boundary check，純頻率）。"""
    if not text:
        return {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0, "PUA": 0}

    counts = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0, "PUA": 0}

    for line in text.split("\n"):
        line = line.lstrip()
        if not line:
            continue

        first = line[0]
        if first in L0_CHARS and len(line) > 1 and line[1] in "、，":
            counts["L0"] += 1
        elif L1_PATTERN.match(line):
            counts["L1"] += 1
        elif first in L2_UNICODE or L2_PAREN.match(line):
            counts["L2"] += 1
        elif first in L3_UNICODE or L3_ARABIC.match(line):
            counts["L3"] += 1
        elif first in L4_UNICODE or L4_PAREN.match(line):
            counts["L4"] += 1
        elif first in L5_UNICODE:
            counts["L5"] += 1
        elif PUA_RANGE[0] <= ord(first) <= PUA_RANGE[1]:
            counts["PUA"] += 1

    return counts


def boundary_false_positives(text: str) -> dict:
    """掃 known false positive patterns 出現次數。"""
    if not text:
        return {}
    return {name: len(pat.findall(text)) for name, pat in FP_PATTERNS.items()}


def quote_audit(text: str) -> dict:
    """引號審計：成對統計、orphan、長度分布、是否帶 citation prefix。"""
    if not text:
        return {"open": 0, "close": 0, "balanced": True}
    n_open_corner = text.count("「")
    n_close_corner = text.count("」")
    n_open_double = text.count("『")
    n_close_double = text.count("』")

    # 配對找最大 quote 長度（簡化版：依序配對）
    quote_lengths = []
    suspect_orphans = []
    citation_mode_quotes = 0

    stack = []  # (open_index, has_citation_prefix)
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "「":
            # 看前 8 字判斷是否 citation mode
            ctx = text[max(0, i - 8):i]
            is_citation = bool(CITATION_PREFIXES.search(ctx) or CASE_NUMBER_RE.search(ctx))
            stack.append((i, is_citation))
        elif ch == "」" and stack:
            start, is_citation = stack.pop()
            length = i - start - 1
            quote_lengths.append(length)
            if is_citation:
                citation_mode_quotes += 1
            # normal mode 超 500 字 → orphan 風險
            if not is_citation and length > 500:
                suspect_orphans.append({"start": start, "length": length})
        i += 1

    # 還在 stack 上的就是 unmatched open
    unmatched_open = len(stack)

    return {
        "open_corner": n_open_corner,
        "close_corner": n_close_corner,
        "open_double": n_open_double,
        "close_double": n_close_double,
        "balanced_corner": n_open_corner == n_close_corner,
        "max_quote_length": max(quote_lengths) if quote_lengths else 0,
        "p99_quote_length": _p99(quote_lengths) if quote_lengths else 0,
        "median_quote_length": _median(quote_lengths) if quote_lengths else 0,
        "n_quotes": len(quote_lengths),
        "citation_mode_quotes": citation_mode_quotes,
        "unmatched_open_corner": unmatched_open,
        "suspect_orphan_normal_mode": len(suspect_orphans),
    }


def _median(arr):
    arr = sorted(arr)
    n = len(arr)
    return arr[n // 2] if n else 0


def _p99(arr):
    arr = sorted(arr)
    if not arr:
        return 0
    idx = max(0, int(len(arr) * 0.99) - 1)
    return arr[idx]


def hard_break_profile(text: str) -> dict:
    """MCP 硬斷行特徵：行長分布、行末 % 為句末標點。"""
    if not text:
        return {}
    lines = [ln for ln in text.split("\n") if ln.strip()]
    lengths = [len(ln) for ln in lines]
    if not lengths:
        return {}

    # CJK 字元數（粗估：非 ASCII 與非全形空白）
    def cjk_count(s):
        return sum(1 for c in s if ord(c) > 127 and c != "\u3000")

    cjk_lengths = [cjk_count(ln) for ln in lines]
    end_with_terminator = sum(
        1 for ln in lines
        if ln and (ln.rstrip()[-1] if ln.rstrip() else "") in SENTENCE_TERMINATORS
        or ln.rstrip().endswith(("」", "』"))
        and len(ln.rstrip()) >= 2
        and ln.rstrip()[-2] in SENTENCE_TERMINATORS
    )

    counter = Counter(cjk_lengths)
    return {
        "line_count": len(lines),
        "median_cjk_length": _median(cjk_lengths),
        "max_cjk_length": max(cjk_lengths),
        "min_cjk_length": min(cjk_lengths),
        "top3_cjk_lengths": counter.most_common(3),
        "pct_end_terminator": round(100 * end_with_terminator / len(lines), 1),
        "pct_mid_sentence": round(100 * (len(lines) - end_with_terminator) / len(lines), 1),
    }


def table_check(text: str) -> dict:
    """ASCII box-drawing 表格偵測。"""
    if not text:
        return {"table_lines": 0, "table_chars_present": False}
    table_lines = 0
    for ln in text.split("\n"):
        ln = ln.lstrip()
        if ln and ln[0] in TABLE_CHARS:
            table_lines += 1
    return {
        "table_lines": table_lines,
        "table_chars_present": any(c in text for c in TABLE_CHARS),
    }


def informal_l1_scan(text: str) -> dict:
    """Informal L1 短語偵測（會在無正式 L0/L1 時 promotion）。"""
    if not text:
        return {"hits": 0, "would_promote": False}
    hits = sum(len(pat.findall(text)) for pat in INFORMAL_L1_PATTERNS)
    return {"hits": hits}


def section_header_leakage(text: str) -> list[str]:
    """檢查內文是否殘留 section header 文字（MCP 切錯時可見）。"""
    leak = []
    for header in ("主文", "事實", "理由", "事實及理由", "事實與理由", "犯罪事實及理由"):
        # 內文裡是否出現獨立一行的 section header
        if re.search(rf"^\s*{header}\s*$", text or "", flags=re.MULTILINE):
            leak.append(header)
    return leak


# ---------------------------------------------------------------------------
# 報告渲染
# ---------------------------------------------------------------------------

def render_report(case_label: str, jud: dict) -> str:
    out = []
    out.append("=" * 78)
    out.append(f"【{case_label}】")
    out.append(f"  case_id : {jud.get('case_id', '?')}")
    out.append(f"  court   : {jud.get('court', '?')}")
    out.append(f"  date    : {jud.get('date', '?')}")
    out.append(f"  cause   : {jud.get('cause', '?')}")
    out.append(f"  judges  : {jud.get('judges') or '?'}")

    # 1. 欄位完整性
    fc = field_completeness(jud)
    out.append("\n  ── 欄位完整性 ──")
    for f in ("main_text", "facts", "reasoning", "full_text"):
        info = fc[f]
        flag = " ⚠️ EMPTY" if info["empty"] else ""
        out.append(f"    {f:11s}: {info['length']:>8d} chars / {info['lines']:>4d} lines{flag}")
    out.append(f"    cited_statutes: {fc['cited_statutes']['count']} 條")
    if fc["_warn_reasoning_empty"]:
        out.append("    ⚠️ reasoning 空但 facts 龐大 → 疑似 MCP 切段 bug（合併段歸錯）")

    # 2. 各欄位的結構分析
    for field in ("main_text", "reasoning", "full_text"):
        text = jud.get(field) or ""
        if not text.strip():
            continue
        out.append(f"\n  ── 欄位『{field}』結構 ──")

        markers = marker_scan(text)
        marker_str = " ".join(f"{k}={v}" for k, v in markers.items() if v)
        out.append(f"    Marker：{marker_str or '(無)'}")
        if markers["PUA"]:
            out.append(f"    ⚠️ 偵測到 {markers['PUA']} 個 PUA 字元 — 須由 context 判斷是 L2 延伸還是罕用漢字")

        fps = boundary_false_positives(text)
        fp_hits = {k: v for k, v in fps.items() if v}
        if fp_hits:
            out.append("    Boundary FP 候選：")
            for k, v in fp_hits.items():
                out.append(f"      {k}: {v}")

        qa = quote_audit(text)
        out.append(
            f"    引號：開={qa['open_corner']} 關={qa['close_corner']} "
            f"配對={'✓' if qa['balanced_corner'] else '✗'} "
            f"中位數={qa['median_quote_length']}字 "
            f"P99={qa['p99_quote_length']}字 "
            f"最大={qa['max_quote_length']}字"
        )
        if qa.get("citation_mode_quotes"):
            out.append(f"      其中 citation mode 引號：{qa['citation_mode_quotes']}")
        if qa.get("unmatched_open_corner"):
            out.append(f"      ⚠️ 未配對開引號：{qa['unmatched_open_corner']} 個")
        if qa.get("suspect_orphan_normal_mode"):
            out.append(f"      ⚠️ Normal mode 超長引號（>500 字）：{qa['suspect_orphan_normal_mode']} 處")

        hb = hard_break_profile(text)
        if hb:
            out.append(
                f"    硬斷行：{hb['line_count']}行 "
                f"中位 CJK={hb['median_cjk_length']}字 "
                f"max={hb['max_cjk_length']}字 "
                f"行末句末標點={hb['pct_end_terminator']}% "
                f"行末 mid-sentence={hb['pct_mid_sentence']}%"
            )
            top3 = ", ".join(f"{l}({c}次)" for l, c in hb["top3_cjk_lengths"])
            out.append(f"      top3 行長：{top3}")

        tc = table_check(text)
        if tc["table_chars_present"]:
            out.append(f"    ⚠️ 偵測到 ASCII 表格（{tc['table_lines']} 表格行）")

        il1 = informal_l1_scan(text)
        if markers["L0"] == 0 and markers["L1"] == 0 and il1["hits"] >= 2:
            out.append(f"    ⚠️ 無正式 L0/L1，但有 {il1['hits']} 個 informal L1 短語 → 會啟動 promotion")

        leak = section_header_leakage(text)
        if leak:
            out.append(f"    ⚠️ 內文殘留 section header：{leak}")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def main():
    print("初始化 MCP（mcp-taiwan-legal-db）...")
    await mcp_client.init_mcp()

    try:
        print("\n搜尋 5 則判決的 JID...")
        resolved = []
        for label, kw, court in CASES:
            print(f"  搜尋：{label}")
            r = await find_jid(kw, court)
            if not r:
                print(f"    ✗ 找不到")
                resolved.append((label, None))
                continue
            jid = r.get("jid") or r.get("id") or r.get("case_id")
            print(f"    ✓ jid = {jid}")
            resolved.append((label, jid))
            await asyncio.sleep(0.5)

        print("\n抓取全文 + 分析...")
        reports = []
        for label, jid in resolved:
            if not jid:
                reports.append(f"\n{'=' * 78}\n【{label}】\n  ✗ 找不到 JID，跳過\n")
                continue
            print(f"  抓取：{label} ({jid})")
            try:
                jud = await mcp_client.get_judgment(jid)
                reports.append(render_report(label, jud))
            except Exception as exc:
                reports.append(f"\n{'=' * 78}\n【{label}】\n  ✗ get_judgment 失敗：{exc}\n")
            await asyncio.sleep(1.0)

        print("\n" + "=" * 78)
        print("驗證報告")
        print("=" * 78)
        for r in reports:
            print(r)
            print()

    finally:
        await mcp_client.close_mcp()


if __name__ == "__main__":
    asyncio.run(main())
