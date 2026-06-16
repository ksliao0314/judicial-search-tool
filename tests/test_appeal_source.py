"""勞動部訴願 adapter 解析器測試（network-free，對 fixtures）。

fixtures 為真實站台抓下的搜尋結果頁 + 決定書詳情頁（session token 已清）。
測試 search/detail 解析與 ROC 日期轉換；網路/驗證碼流程不在此測（需 live + Claude）。
"""
from pathlib import Path

from src.pipeline.appeal_source import (
    _roc_to_iso,
    parse_detail,
    parse_pagination,
    parse_result_rows,
)

_FIX = Path(__file__).parent / "fixtures"


def _result_html():
    return (_FIX / "appeal_result.html").read_text(encoding="utf-8")


def _detail_html():
    return (_FIX / "appeal_detail.html").read_text(encoding="utf-8")


def test_roc_to_iso():
    assert _roc_to_iso("115/5/18") == "2026-05-18"
    assert _roc_to_iso("99/12/3") == "2010-12-03"
    assert _roc_to_iso("亂") == "亂"  # 解析失敗回原字串


def test_parse_result_rows():
    rows = parse_result_rows(_result_html())
    assert len(rows) == 10
    r = rows[0]
    # 每筆對齊 task_judgments 骨架
    assert r["court"] == "勞動部"
    assert r["case_id"].startswith("勞動法訴")          # 字號為主鍵
    assert r["case_id"].endswith("號")
    assert r["case_no"]                                   # 案號
    assert r["date"].startswith("20") and len(r["date"]) == 10  # ISO
    assert r["main_text"]                                 # 訴願駁回 / 不受理 …
    assert "caseId=" in r["source_url"]                   # 全文 URL 帶 caseId
    assert r["case_id_internal"].isdigit()


def test_result_rows_unique_caseids():
    rows = parse_result_rows(_result_html())
    ids = [r["case_id_internal"] for r in rows]
    assert len(ids) == len(set(ids))                      # 同頁不重複


def test_parse_pagination():
    pg = parse_pagination(_result_html())
    assert pg["page_count"] >= 1                          # 有分頁（≥6 頁）


def test_parse_detail():
    d = parse_detail(_detail_html(), source_url="https://x/?caseId=301147")
    assert d is not None
    assert d["court"] == "勞動部"
    assert d["case_id"].startswith("勞動法訴")            # 字號
    assert d["date"] == "2026-03-27"                      # 中華民國115年3月27日
    assert d["main_text"] == "訴願不受理"
    assert d["facts"].startswith("訴願人因")              # 導言整段
    assert "訴願法第1條" in d["reasoning"]                # 理由實質全文
    assert len(d["reasoning"]) > 200
    assert d["full_text"] and d["source_url"].endswith("caseId=301147")


def test_parse_detail_no_flow_returns_none():
    assert parse_detail("<html><body>no flow div</body></html>") is None
