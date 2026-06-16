"""bulk-pdf 端點「一同打包 AI 評價/綜合彙整 .md」行為測試（network-free）。

mock 掉 db.get_task_judgments 與 _fetch_one_pdf，驗證：
  1. 前端傳入的 markdown 會被 writestr 進同一個 zip、內容與檔名正確
  2. 全部 PDF 失敗但有 markdown → 仍回傳 zip（不 502），且含失敗清單
  3. 沒有 markdown 且全部失敗 → 維持原本 502 行為（回歸守門）
  4. PDF 成功 + markdown → 同一個 zip 內 PDF 與 .md 並存
"""
import io
import zipfile

import pytest
from fastapi import HTTPException

from src.api import judgments as J
from src.api.judgments import BulkPdfRequest, download_bulk_pdf

_CASE = "TPTA,113,訴,1,20240101,1"


async def _fake_get_task_judgments(task_id):
    return [{
        "case_id": _CASE, "court": "臺北高等行政法院", "date": "2024-01-01",
        "source_url": "https://judgment.judicial.gov.tw/x", "reasoning": "",
        "main_text": "", "facts": "", "cited_statutes": None, "full_text": "",
    }]


def _zip_names(resp) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(resp.body))
    return {n: zf.read(n) for n in zf.namelist()}


def _patch(monkeypatch, pdf_result):
    monkeypatch.setattr(J.db, "get_task_judgments", _fake_get_task_judgments)

    async def _fake_fetch(client, judgment, sem):
        cid = judgment["case_id"]
        return (cid, *pdf_result)  # pdf_result = (bytes|None, err|None)

    monkeypatch.setattr(J, "_fetch_one_pdf", _fake_fetch)


async def test_md_packed_when_all_pdfs_fail(monkeypatch):
    _patch(monkeypatch, (None, "mock fail"))
    md = "# 判決研究匯出：測試\n\n## AI 綜合彙整\n結論文字"
    resp = await download_bulk_pdf(
        "t1", BulkPdfRequest(case_ids=[_CASE], markdown=md,
                             md_filename="AI評價與綜合彙整_測試.md"))
    names = _zip_names(resp)
    assert "AI評價與綜合彙整_測試.md" in names
    assert names["AI評價與綜合彙整_測試.md"].decode("utf-8") == md
    assert "_失敗清單.txt" in names  # 全失敗仍記錄、但不 502


async def test_no_md_all_fail_still_502(monkeypatch):
    _patch(monkeypatch, (None, "mock fail"))
    with pytest.raises(HTTPException) as ei:
        await download_bulk_pdf("t1", BulkPdfRequest(case_ids=[_CASE]))
    assert ei.value.status_code == 502


async def test_md_alongside_successful_pdf(monkeypatch):
    _patch(monkeypatch, (b"%PDF-1.4 fake", None))
    resp = await download_bulk_pdf(
        "t1", BulkPdfRequest(case_ids=[_CASE], markdown="# x"))
    names = _zip_names(resp)
    assert any(n.endswith(".pdf") for n in names)
    assert any(n.endswith(".md") for n in names)


async def test_md_filename_gets_md_extension(monkeypatch):
    _patch(monkeypatch, (b"%PDF fake", None))
    resp = await download_bulk_pdf(
        "t1", BulkPdfRequest(case_ids=[_CASE], markdown="# x", md_filename="摘要"))
    names = _zip_names(resp)
    assert "摘要.md" in names  # 自動補 .md 副檔名
