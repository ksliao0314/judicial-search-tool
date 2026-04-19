"""針對 validate_5_judgments.py 找出的可疑點做更細的取樣。

聚焦：
1. 最高行政法院 110 上 74：reasoning 66 行 max 2029 字 — 為何 MCP 沒硬斷？
2. 106 訴 93 / 107 訴更一 17：unmatched 」(close quote) 出在哪
3. 104/103 建 案：normal-mode 長引號（>500 字）為何 citation prefix 沒命中
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src import mcp_client  # noqa: E402

CITATION_PREFIXES = re.compile(
    r"(?:判決(?:意旨)?|裁定|解釋|函釋|要旨|意旨|略以|略謂|"
    r"明定|明文規定|揭示|認為|指出|參照|規定|條)$"
)
CASE_NUMBER_RE = re.compile(r"\d+年度?\S{1,20}字第?\d+號")


def show_long_lines(text: str, threshold: int = 100, max_show: int = 5):
    lines = text.split("\n")
    long_ones = [(i, ln) for i, ln in enumerate(lines) if len(ln) > threshold]
    print(f"  超過 {threshold} 字的行：{len(long_ones)} / {len(lines)}")
    for i, ln in long_ones[:max_show]:
        snippet = ln[:80] + "..." + ln[-40:] if len(ln) > 130 else ln
        print(f"    line {i:>4d} ({len(ln)} 字): {snippet}")


def show_long_quotes(text: str, threshold: int = 500, max_show: int = 5):
    """找出 >threshold 字的 "「...」" 並顯示前綴（看 citation mode 為何沒觸發）。"""
    stack = []
    findings = []
    for i, ch in enumerate(text):
        if ch == "「":
            stack.append(i)
        elif ch == "」" and stack:
            start = stack.pop()
            length = i - start - 1
            if length > threshold:
                ctx_before = text[max(0, start - 30):start]
                preview_inside = text[start:start + 60].replace("\n", " ")
                tail_inside = text[i - 30:i + 1].replace("\n", " ")
                is_cit = bool(CITATION_PREFIXES.search(ctx_before[-12:]))
                is_case = bool(CASE_NUMBER_RE.search(ctx_before))
                findings.append({
                    "start": start,
                    "length": length,
                    "ctx_before_30": ctx_before,
                    "head": preview_inside,
                    "tail": tail_inside,
                    "citation_prefix_match": is_cit,
                    "case_number_match": is_case,
                })

    print(f"  >{threshold} 字引號：{len(findings)} 處")
    for f in findings[:max_show]:
        print(f"    ── 位置 {f['start']} 長度 {f['length']} 字")
        print(f"      前 30 字：…{f['ctx_before_30']}")
        print(f"      引號開頭：{f['head']}…")
        print(f"      引號結尾：…{f['tail']}")
        print(f"      citation prefix match? {f['citation_prefix_match']}")
        print(f"      case number match?     {f['case_number_match']}")


def show_unmatched_close_quotes(text: str, max_show: int = 5):
    """找出 unmatched 」 的位置與上下文。"""
    stack = []
    unmatched_close = []
    for i, ch in enumerate(text):
        if ch == "「":
            stack.append(i)
        elif ch == "」":
            if stack:
                stack.pop()
            else:
                ctx_before = text[max(0, i - 40):i].replace("\n", "↵")
                ctx_after = text[i + 1:i + 30].replace("\n", "↵")
                unmatched_close.append((i, ctx_before, ctx_after))

    unmatched_open = stack
    print(f"  Unmatched 」：{len(unmatched_close)} 處；Unmatched 「：{len(unmatched_open)} 處")
    for i, before, after in unmatched_close[:max_show]:
        print(f"    位置 {i}：…{before}【」】{after}…")
    for i in unmatched_open[:max_show]:
        before = text[max(0, i - 30):i].replace("\n", "↵")
        after = text[i + 1:i + 60].replace("\n", "↵")
        print(f"    位置 {i} 開引號未閉合：…{before}【「】{after}…")


CASES = [
    ("TPHV,104,建上,98,20171017,1",  "臺灣高等法院 104 建上 98",     ["long_quote_reasoning"]),
    ("TPDV,103,建,401,20150814,1",  "臺灣臺北地方法院 103 建 401", ["long_quote_reasoning"]),
    ("TCBA,106,訴,93,20170802,1",    "臺中高行 106 訴 93",           ["unmatched_quote_reasoning"]),
    ("TCBA,107,訴更一,17,20201231,1", "臺中高行 107 訴更一 17",      ["unmatched_quote_reasoning"]),
    ("TPAA,110,上,74,20220413,1",    "最高行政法院 110 上 74",        ["long_lines_reasoning", "long_lines_full_text"]),
]


async def main():
    await mcp_client.init_mcp()
    try:
        for jid, label, checks in CASES:
            print("\n" + "=" * 78)
            print(f"【{label}】 {jid}")
            jud = await mcp_client.get_judgment(jid)

            if "long_lines_reasoning" in checks:
                print("\n--- reasoning 超長行檢查 ---")
                show_long_lines(jud.get("reasoning") or "", threshold=80)
            if "long_lines_full_text" in checks:
                print("\n--- full_text 超長行檢查 ---")
                show_long_lines(jud.get("full_text") or "", threshold=80)
            if "long_quote_reasoning" in checks:
                print("\n--- reasoning 長引號 (>500) 檢查 ---")
                show_long_quotes(jud.get("reasoning") or "", threshold=500)
            if "unmatched_quote_reasoning" in checks:
                print("\n--- reasoning unmatched 引號檢查 ---")
                show_unmatched_close_quotes(jud.get("reasoning") or "")

            await asyncio.sleep(0.6)
    finally:
        await mcp_client.close_mcp()


if __name__ == "__main__":
    asyncio.run(main())
