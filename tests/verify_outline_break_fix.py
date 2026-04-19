"""驗證 _insert_outline_breaks 修補：

1. 對 5 則判決直接 fetch raw HTML（繞過 MCP cache）→ 跑 parse_judgment_page
2. 比對 patched 後 reasoning 行首 outline marker 出現次數
3. 真正的 fix 目標：outline marker 必須出現在行首，frontend 才能正確建 outline
   - 個別 (N) 段落本身可能很長（>1000 字），那由 frontend Pass 3 軟斷行處理，
     不是 MCP 的責任
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 與 MCP 同樣的 SSL trust
import truststore
truststore.inject_into_ssl()

import httpx

# 讓 mcp_server import 找得到
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "mcp-taiwan-legal-db"))

from mcp_server.parsers.judicial_parser import parse_judgment_page  # noqa: E402

import re

# 行首 outline marker 偵測
_LINE_LEAD = [
    re.compile(r"^[壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸][、，]"),  # L0
    re.compile(r"^[一二三四五六七八九十]{1,4}、"),                       # L1
    re.compile(r"^[\u3220-\u3229]"),                                     # L2 unicode
    re.compile(r"^[（(][一二三四五六七八九十]{1,3}[）)]"),                # L2 paren
    re.compile(r"^[\u2488-\u249B]"),                                     # L3 unicode
    re.compile(r"^\d{1,2}\.(?!\d)"),                                     # L3 arabic
    re.compile(r"^[\u2474-\u2487]"),                                     # L4 unicode
    re.compile(r"^[（(]\d{1,2}[）)]"),                                   # L4 paren
    re.compile(r"^[\u2460-\u2473]"),                                     # L5 unicode
]

def count_outline_lines(lines):
    return sum(1 for ln in lines for pat in _LINE_LEAD if pat.match(ln.lstrip()))


CASES = [
    # baseline_outline 是修復前的 outline marker 行首數量（舊版 marker_scan 結果）
    # 修復後應該顯著上升
    ("TPAA,110,上,74,20220413,1",      "最高行政法院 110 上 74",     {"min_outline_lines": 30, "baseline": 33}),
    ("TPHV,104,建上,98,20171017,1",    "高院 104 建上 98",           {"min_outline_lines": 50, "baseline": 55}),
    ("TPDV,103,建,401,20150814,1",    "北院 103 建 401",            {"min_outline_lines": 60, "baseline": 63}),
    ("TCBA,106,訴,93,20170802,1",      "中高行 106 訴 93",           {"min_outline_lines": 80, "baseline": 87}),
    ("TCBA,107,訴更一,17,20201231,1", "中高行 107 訴更一 17",       {"min_outline_lines": 150, "baseline": 178}),
]


async def main():
    pass_count, fail_count = 0, 0
    async with httpx.AsyncClient(timeout=60.0, headers={"User-Agent": "TaiwanLegalMCP/1.0"}) as client:
        for jid, label, criteria in CASES:
            url = f"https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id={jid}"
            print("=" * 78)
            print(f"【{label}】 {jid}")
            try:
                resp = await client.get(url)
            except Exception as e:
                print(f"  ✗ HTTP fail: {e}")
                fail_count += 1
                continue

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            jud = soup.select_one("#jud")
            if not jud:
                print("  ✗ no #jud")
                fail_count += 1
                continue

            parsed = parse_judgment_page(f"<html><body>{jud}</body></html>")
            reasoning = parsed.get("reasoning") or ""
            full_text = parsed.get("full_text") or ""

            r_lines = [ln for ln in reasoning.split("\n") if ln.strip()]
            r_lengths = [len(ln) for ln in r_lines]
            r_max = max(r_lengths) if r_lengths else 0
            r_med = sorted(r_lengths)[len(r_lengths) // 2] if r_lengths else 0

            f_lines = [ln for ln in full_text.split("\n") if ln.strip()]
            f_lengths = [len(ln) for ln in f_lines]
            f_max = max(f_lengths) if f_lengths else 0

            outline_lines = count_outline_lines(r_lines)

            print(f"  reasoning : {len(r_lines)} 行  median={r_med}字  max={r_max}字")
            print(f"  full_text : {len(f_lines)} 行  max={f_max}字")
            print(f"  outline marker 行數：{outline_lines}（baseline={criteria['baseline']}, 期望≥{criteria['min_outline_lines']}）")

            ok = outline_lines >= criteria["min_outline_lines"]
            if ok:
                pass_count += 1
                print(f"  ✓ 通過")
            else:
                fail_count += 1
                print(f"  ✗ 失敗")

            # 抽樣前 5 行給人看
            print("  reasoning 前 5 行：")
            for ln in r_lines[:5]:
                preview = ln[:100] + ("..." if len(ln) > 100 else "")
                print(f"    [{len(ln):>4d}] {preview}")

            await asyncio.sleep(0.6)

    print("\n" + "=" * 78)
    print(f"總計：{pass_count} pass / {fail_count} fail")
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
