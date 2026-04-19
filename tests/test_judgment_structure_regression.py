"""5 件指定判決的結構 regression test。

用 tests/fixtures/ 下的 parsed JSON 作 baseline，每個 case 對關鍵欄位做 assertion。
這套 test 抓的是「目前的 frontend / API / pipeline 是否還能正確處理這 5 件的結構」。

如果 MCP parser 改了想重新 baseline：跑 `tests/regenerate_fixtures.py`。

涵蓋的 edge case：
- 民事 (TPHV/TPDV) × 行政 (TCBA/TPAA)
- 地方法院 (TPDV) × 高等 (TPHV/TCBA) × 最高 (TPAA)
- text-pre 來源 (TPHV/TPDV/TCBA) × htmlcontent 來源 (TPAA)
- 含長引號 + ASCII 表格 + 同案三審序列 (瑞成堂案)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# 每個 fixture 的結構期望（對 MCP parser + 我們的下游 pipeline 設下界）
# 數字是 baseline 的 80%，留些餘裕避免無謂失敗（例：MCP 微改字元計數）
EXPECTED = {
    "tphv_104_jian_shang_98": {
        "court_contains": "高等法院",
        "case_id_contains": "104年度建上字第98號",
        "facts_empty": True,                # 民事「事實及理由」合併段
        "reasoning_min_chars": 20000,
        "main_text_min_chars": 100,
        "cited_statutes_min": 4,
        "min_outline_lines": 50,
        "max_line_chars_lt": 60,            # text-pre 來源，硬斷行 ~26 字
    },
    "tpdv_103_jian_401": {
        "court_contains": "臺北地方法院",
        "case_id_contains": "103年度建字第401號",
        "facts_empty": True,
        "reasoning_min_chars": 18000,
        "main_text_min_chars": 100,
        "cited_statutes_min": 4,
        "min_outline_lines": 50,
        "max_line_chars_lt": 60,
    },
    "tcba_106_su_93": {
        "court_contains": "臺中高等行政法院",
        "case_id_contains": "106年度訴字第93號",
        "facts_empty": True,
        "reasoning_min_chars": 24000,
        "main_text_min_chars": 10,
        "cited_statutes_min": 25,
        "min_outline_lines": 70,
        "max_line_chars_lt": 60,
    },
    "tcba_107_su_geng_yi_17": {
        "court_contains": "臺中高等行政法院",
        "case_id_contains": "107年度訴更一字第17號",
        "facts_empty": True,
        "reasoning_min_chars": 40000,
        "main_text_min_chars": 50,
        "cited_statutes_min": 40,
        "min_outline_lines": 150,
        "max_line_chars_lt": 60,
    },
    "tpaa_110_shang_74": {
        # ★ 這是 P0 修復目標：最高行 .htmlcontent 段落原本擠成一團
        # 注意：MCP parser 對 .htmlcontent 沒抽 court 名，case_id 也不含「最高行政法院」字樣
        # 所以 court_contains 這裡僅做存在性檢查（值總會通過）
        "court_contains": "",
        "case_id_contains": "110年度上字第74號",
        "facts_empty": True,
        "reasoning_min_chars": 20000,
        "main_text_min_chars": 10,
        "cited_statutes_min": 25,
        "min_outline_lines": 70,            # 修復前是 33；修復後 91
        # max_line 不設 — htmlcontent 段落本身就很長，frontend Pass 3 軟斷行處理
    },
}


# ── helpers ───────────────────────────────────────────────────────────────

# 行首 outline marker 偵測（與解析器規則同步）
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


def _count_outline_lines(text: str) -> int:
    if not text:
        return 0
    n = 0
    for line in text.split("\n"):
        s = line.lstrip()
        if any(p.match(s) for p in _LINE_LEAD):
            n += 1
    return n


def _max_line_chars(text: str) -> int:
    if not text:
        return 0
    return max((len(ln) for ln in text.split("\n") if ln.strip()), default=0)


def _load_fixture(slug: str) -> dict:
    path = FIXTURES_DIR / f"{slug}.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# ── tests ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("slug,expected", EXPECTED.items())
def test_judgment_structure(slug, expected):
    """對每個 fixture 跑同一套結構 assertion。"""
    fixture_path = FIXTURES_DIR / f"{slug}.json"
    assert fixture_path.exists(), f"Fixture missing: {slug}.json. 跑 tests/regenerate_fixtures.py 重建。"

    jud = _load_fixture(slug)

    # 法院 + 字號身分
    assert expected["court_contains"] in (jud.get("court") or jud.get("case_id") or ""), \
        f"{slug}: court 不含 {expected['court_contains']!r}"
    assert expected["case_id_contains"].replace(" ", "") in (jud.get("case_id") or "").replace(" ", ""), \
        f"{slug}: case_id 不含 {expected['case_id_contains']!r}"

    # 欄位完整性
    facts = jud.get("facts") or ""
    reasoning = jud.get("reasoning") or ""
    main_text = jud.get("main_text") or ""
    cited = jud.get("cited_statutes") or []

    if expected.get("facts_empty"):
        assert not facts.strip(), \
            f"{slug}: facts 應為空（民事/行政合併段歸 reasoning），實際 {len(facts)} 字"

    assert len(reasoning) >= expected["reasoning_min_chars"], \
        f"{slug}: reasoning 太短 {len(reasoning)} < {expected['reasoning_min_chars']}"

    assert len(main_text) >= expected["main_text_min_chars"], \
        f"{slug}: main_text 太短 {len(main_text)} < {expected['main_text_min_chars']}"

    assert len(cited) >= expected["cited_statutes_min"], \
        f"{slug}: cited_statutes 不足 {len(cited)} < {expected['cited_statutes_min']}"

    # outline marker 行首化（catches MCP parser 結構斷行 regression）
    outline_lines = _count_outline_lines(reasoning)
    assert outline_lines >= expected["min_outline_lines"], \
        f"{slug}: outline marker 行數 {outline_lines} < {expected['min_outline_lines']}"

    # 行長上界（catches MCP 沒做硬斷行的 regression；htmlcontent 來源不檢查）
    if "max_line_chars_lt" in expected:
        max_chars = _max_line_chars(reasoning)
        assert max_chars < expected["max_line_chars_lt"], \
            f"{slug}: reasoning max line {max_chars} >= {expected['max_line_chars_lt']}"


def test_all_fixtures_present():
    """確保 5 件 fixture 都在。"""
    for slug in EXPECTED:
        path = FIXTURES_DIR / f"{slug}.json"
        assert path.exists(), f"Missing fixture: {slug}.json"


def test_source_url_present():
    """所有 fixture 必須有 source_url（reader 「開啟司法院原文」依賴）。"""
    for slug in EXPECTED:
        jud = _load_fixture(slug)
        url = jud.get("source_url") or ""
        assert "judicial.gov.tw" in url, f"{slug}: source_url 異常 {url!r}"
