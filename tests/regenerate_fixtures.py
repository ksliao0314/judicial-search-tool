"""產生 5 件指定判決的 parsed JSON fixture，存到 tests/fixtures/。

只在需要更新 fixture 時手動跑（例如 MCP parser 改動後想重建 baseline）。
平常 regression 測試不會跑這個。

跑法：
    cd judgment-search
    .venv/bin/python tests/regenerate_fixtures.py

實作上**繞過 MCP cache**：直接 fetch HTML + 跑 parse_judgment_page，確保 fixture
反映當下 parser 的真實輸出。否則 MCP 30 天 cache 會把舊輸出鎖進 fixture，未來
parser 改動的 regression 偵測就失準。

跑完會在 tests/fixtures/ 下產生：
    tphv_104_jian_shang_98.json
    tpdv_103_jian_401.json
    tcba_106_su_93.json
    tcba_107_su_geng_yi_17.json
    tpaa_110_shang_74.json
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 與 MCP 相同的 SSL trust（macOS/Windows 用 OS-native 信任庫）
import truststore
truststore.inject_into_ssl()

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "mcp-taiwan-legal-db"))

from bs4 import BeautifulSoup  # noqa: E402
from mcp_server.parsers.judicial_parser import parse_judgment_page  # noqa: E402

FIXTURES_DIR = _PROJECT_ROOT / "tests" / "fixtures"

CASES = [
    ("TPHV,104,建上,98,20171017,1",     "tphv_104_jian_shang_98"),
    ("TPDV,103,建,401,20150814,1",     "tpdv_103_jian_401"),
    ("TCBA,106,訴,93,20170802,1",       "tcba_106_su_93"),
    ("TCBA,107,訴更一,17,20201231,1",  "tcba_107_su_geng_yi_17"),
    ("TPAA,110,上,74,20220413,1",       "tpaa_110_shang_74"),
]


async def fetch_and_parse(client: httpx.AsyncClient, jid: str) -> dict:
    """直接打司法院 + 跑 parser，繞過 MCP cache。

    補上 source_url（在 MCP wrapper 才會塞，但下游 reader 依賴此欄）。
    """
    url = f"https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id={jid}"
    resp = await client.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    jud = soup.select_one("#jud")
    if not jud:
        raise ValueError(f"no #jud element in HTML for {jid}")
    parsed = parse_judgment_page(f"<html><body>{jud}</body></html>")
    parsed.setdefault("source_url", url)
    return parsed


async def main():
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Fixture 目錄：{FIXTURES_DIR}")

    async with httpx.AsyncClient(
        timeout=60.0,
        headers={"User-Agent": "TaiwanLegalMCP/1.0"},
    ) as client:
        for jid, slug in CASES:
            print(f"  抓取並解析 {jid}")
            try:
                jud = await fetch_and_parse(client, jid)
            except Exception as exc:
                print(f"    ✗ 失敗：{exc}")
                continue
            out_path = FIXTURES_DIR / f"{slug}.json"
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(jud, f, ensure_ascii=False, indent=2, sort_keys=True)
            print(f"    → {out_path.name} ({out_path.stat().st_size:,} bytes)")
            await asyncio.sleep(0.6)

    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
