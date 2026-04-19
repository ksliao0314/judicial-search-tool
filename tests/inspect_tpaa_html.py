"""抓 TPAA,110,上,74 的 raw HTML，比對 TCBA / TPHV 看 markup 差異。

目的：弄清為何 MCP parser 對最高行的 reasoning 沒做硬斷行。
hypothesis：HTML 結構不同（單一巨型 <div> vs. 多個 <p>/<br>）。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# 與 MCP 相同的 SSL trust 設定（macOS/Windows 用 OS-native 信任庫）
import truststore
truststore.inject_into_ssl()

import httpx
from bs4 import BeautifulSoup

CASES = [
    ("TPAA,110,上,74,20220413,1",      "最高行政法院"),
    ("TCBA,107,訴更一,17,20201231,1", "中高行（對照組）"),
    ("TPHV,104,建上,98,20171017,1",    "高院（對照組）"),
]


async def main():
    async with httpx.AsyncClient(timeout=60.0, headers={"User-Agent": "TaiwanLegalMCP/1.0"}) as client:
        for jid, label in CASES:
            url = f"https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id={jid}"
            print("=" * 78)
            print(f"【{label}】 {jid}")
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "lxml")

            jud = soup.select_one("#jud")
            if not jud:
                print("  no #jud found")
                continue

            # 各層 selector 命中情況
            for sel in (".htmlcontent", ".text-pre", ".jud_content"):
                el = jud.select_one(sel)
                if el is None:
                    print(f"  {sel}: NONE")
                else:
                    txt_len = len(el.get_text(strip=True))
                    print(f"  {sel}: 文字 {txt_len} 字, children={len(list(el.children))}")

            # body_el 選擇邏輯複製：
            _MIN = 50
            _hc = jud.select_one(".htmlcontent")
            if _hc and len(_hc.get_text(strip=True)) >= _MIN:
                body_el = _hc
                chosen = ".htmlcontent"
            else:
                _tp = jud.select_one(".text-pre")
                if _tp and len(_tp.get_text(strip=True)) >= _MIN:
                    body_el = _tp
                    chosen = ".text-pre"
                else:
                    _jc = jud.select_one(".jud_content")
                    if _jc and len(_jc.get_text(strip=True)) >= _MIN:
                        body_el = _jc
                        chosen = ".jud_content"
                    else:
                        body_el = jud
                        chosen = "#jud (fallback)"
            print(f"  → MCP 採用 selector: {chosen}")

            # 統計 body_el 內各種 tag 數量
            tag_counter = {}
            for el in body_el.find_all(True):
                tag_counter[el.name] = tag_counter.get(el.name, 0) + 1
            top_tags = sorted(tag_counter.items(), key=lambda x: -x[1])[:8]
            print(f"  body_el 內主要 tag：{top_tags}")

            # 模擬 MCP 的 get_text("\n") 並計算行長分布
            raw_text = body_el.get_text("\n", strip=False)
            cleaned = re.sub(r'\n{3,}', '\n\n', raw_text).strip()
            lines = cleaned.split("\n")
            non_empty = [ln for ln in lines if ln.strip()]
            print(f"  get_text('\\n') 後：{len(lines)} 行（非空 {len(non_empty)}）")
            if non_empty:
                lengths = [len(ln) for ln in non_empty]
                lengths.sort()
                print(f"    行長：min={lengths[0]} median={lengths[len(lengths)//2]} max={lengths[-1]}")
                # 超過 200 字的行
                long_ones = [ln for ln in non_empty if len(ln) > 200]
                print(f"    超 200 字的行：{len(long_ones)} / {len(non_empty)}")

            # 看 body_el 的前 600 字 raw HTML 結構
            html_snippet = str(body_el)[:600].replace("\n", "↵")
            print(f"  body_el HTML 前 600 字：\n    {html_snippet}")

            print()
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
