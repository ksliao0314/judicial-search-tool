"""CitationNormalizer + CitationExtractor 單元測試。

涵蓋使用者提過的所有變體。加入新 case 時請寫成新的 test，勿刪舊 test。
"""
import sys
from pathlib import Path

# pytest 如果跑不到模組，讓它 fallback 用 sys.path（解 macOS iCloud UF_HIDDEN 干擾 .pth）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

from src.pipeline.citation_normalizer import (
    Citation,
    chi_to_int,
    int_to_chi,
    parse_keyword,
    generate_variants,
    top_search_variants,
    normalize_law_name,
)
from src.pipeline.citation_extractor import extract_citations


# ---------------------------------------------------------------------------
# 中阿數轉換
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chi,expected", [
    ("一", 1), ("十", 10), ("十二", 12), ("二十", 20), ("二十一", 21),
    ("一百", 100), ("一百二十", 120), ("三百二十一", 321),
    ("一千", 1000), ("三千", 3000),
    ("〇", 0),
])
def test_chi_to_int(chi, expected):
    assert chi_to_int(chi) == expected


@pytest.mark.parametrize("n,expected", [
    (1, "一"), (10, "十"), (12, "十二"), (20, "二十"), (100, "一百"),
    (120, "一百二十"), (321, "三百二十一"), (179, "一百七十九"),
])
def test_int_to_chi(n, expected):
    assert int_to_chi(n) == expected


def test_chi_int_roundtrip():
    for n in [1, 10, 12, 20, 100, 179, 321, 999, 1000]:
        assert chi_to_int(int_to_chi(n)) == n


# ---------------------------------------------------------------------------
# 法名正規化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,canonical", [
    ("勞基法", "勞動基準法"),
    ("勞動基準法", "勞動基準法"),
    ("水保法", "水土保持法"),
    ("民訴", "民事訴訟法"),
    ("民訴法", "民事訴訟法"),
    ("民法", "民法"),
    ("未知法", "未知法"),  # fallback
])
def test_normalize_law_name(raw, canonical):
    assert normalize_law_name(raw) == canonical


# ---------------------------------------------------------------------------
# parse_keyword
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw,expected", [
    ("民法第179條", Citation("民法", 179)),
    ("民法第一百七十九條", Citation("民法", 179)),
    ("民法179條", Citation("民法", 179)),
    ("第179條", Citation(None, 179)),
    ("民法第320條之1", Citation("民法", 320, sub=1)),
    ("刑法第三百二十條之一", Citation("刑法", 320, sub=1)),
    ("刑法320-1", Citation("刑法", 320, sub=1)),
    ("民法第179條第1項", Citation("民法", 179, paragraph=1)),
    ("民法第179條第一項", Citation("民法", 179, paragraph=1)),
    ("勞基法179.1", Citation("勞動基準法", 179, paragraph=1)),
    ("公司法第8條第3項第2款", Citation("公司法", 8, paragraph=3, item=2)),
])
def test_parse_keyword(kw, expected):
    assert parse_keyword(kw) == expected


def test_parse_keyword_non_citation():
    """非 citation 格式 keyword 應回 None"""
    for kw in ["信賴保護原則", "", "   ", "abc"]:
        assert parse_keyword(kw) is None


# ---------------------------------------------------------------------------
# variant generation
# ---------------------------------------------------------------------------

def test_generate_variants_includes_all_forms():
    """勞基法179.1 的變體列表應同時含常見格式"""
    c = parse_keyword("勞基法179.1")
    variants = generate_variants(c)
    joined = "\n".join(variants)
    # 必須含這些關鍵形式
    assert "勞動基準法第179條第1項" in joined
    assert "勞動基準法第一百七十九條第一項" in joined
    assert "勞基法第179條第1項" in joined
    assert "勞動基準法179條第1項" in joined


def test_top_search_variants_limit():
    c = parse_keyword("勞基法179.1")
    top = top_search_variants(c, limit=5)
    assert len(top) == 5
    # 第一個應該是最 canonical 的
    assert top[0].startswith("勞動基準法")


# ---------------------------------------------------------------------------
# Citation.covers 語意
# ---------------------------------------------------------------------------

def test_covers_wildcard_matches_specific():
    """律師查粗（只指定條），命中判決裡寫細的（條+項款）"""
    query = Citation("民法", 179)
    specific = Citation("民法", 179, paragraph=1, item=2)
    assert query.covers(specific)


def test_covers_specific_does_not_match_generic():
    """律師查細（指定項），判決寫粗（只條）→ 不命中"""
    query = Citation("民法", 179, paragraph=1)
    generic = Citation("民法", 179)
    assert not query.covers(generic)


def test_covers_law_none_means_any_law():
    """律師沒指定法名 → 視為不限法名"""
    query = Citation(None, 179)
    assert query.covers(Citation("民法", 179))
    assert query.covers(Citation("刑法", 179))


def test_covers_different_article():
    query = Citation("民法", 179)
    assert not query.covers(Citation("民法", 180))


# ---------------------------------------------------------------------------
# CitationExtractor — 判決文本
# ---------------------------------------------------------------------------

def _cits(text):
    """簡寫：取 (law, article, sub, paragraph, item) tuple set"""
    return {(c.law, c.article, c.sub, c.paragraph, c.item)
            for c in extract_citations(text)}


def test_extract_simple():
    assert (("民法", 179, None, None, None),) == tuple(_cits("本案適用民法第179條規定"))


def test_extract_compound_and():
    """民法第12條及第179條"""
    assert _cits("民法第12條及第179條所定") == {
        ("民法", 12, None, None, None),
        ("民法", 179, None, None, None),
    }


def test_extract_compound_comma_shared_tiao():
    """民法第12、179條 — 共享「條」字"""
    assert _cits("依民法第12、179條規定") == {
        ("民法", 12, None, None, None),
        ("民法", 179, None, None, None),
    }


def test_extract_range():
    """第12至15條 — 範圍展開"""
    assert _cits("按民法第12至15條均規定") == {
        ("民法", 12, None, None, None),
        ("民法", 13, None, None, None),
        ("民法", 14, None, None, None),
        ("民法", 15, None, None, None),
    }


def test_extract_same_law_backref():
    """同法第X條 → 回指最近的法名"""
    text = "民法第12條規定如前述，同法第179條亦有規定。"
    assert _cits(text) == {
        ("民法", 12, None, None, None),
        ("民法", 179, None, None, None),
    }


def test_extract_multi_law_context_switch():
    """多法交錯 + 同法回指"""
    text = "民法第184條為侵權，刑法第320條規範竊盜，同法第321條為加重竊盜"
    cits = _cits(text)
    assert ("民法", 184, None, None, None) in cits
    assert ("刑法", 320, None, None, None) in cits
    assert ("刑法", 321, None, None, None) in cits  # 同法指最近的刑法


def test_extract_par_item():
    assert _cits("民法第179條第1項第2款之規定") == {
        ("民法", 179, None, 1, 2),
    }


def test_extract_chinese_numerals():
    assert _cits("民法第一百七十九條第一項") == {
        ("民法", 179, None, 1, None),
    }


def test_extract_sub_article():
    """保險法第123條之1、第123條之2、第129條之1及第132條之1"""
    text = "保險法第123條之1、第123條之2、第129條之1及第132條之1條文"
    assert _cits(text) == {
        ("保險法", 123, 1, None, None),
        ("保險法", 123, 2, None, None),
        ("保險法", 129, 1, None, None),
        ("保險法", 132, 1, None, None),
    }


def test_extract_dash_form():
    """第130、148-3、167-1、171-1 條"""
    text = "第130、148-3、167-1、171-1 條條文"
    assert _cits(text) == {
        (None, 130, None, None, None),
        (None, 148, 3, None, None),
        (None, 167, 1, None, None),
        (None, 171, 1, None, None),
    }


def test_extract_suffix_only_applies_to_last():
    """民法第184條第1項前段、第185條 — 第1項只屬 184"""
    text = "民法第184條第1項前段、第185條"
    assert _cits(text) == {
        ("民法", 184, None, 1, None),
        ("民法", 185, None, None, None),
    }


def test_extract_ignores_non_citation_numbers():
    """金額、頁碼等純數字不該被當引用"""
    assert _cits("原告受傷送醫，支付醫療費用新臺幣12、345元整") == set()
    assert _cits("判決書第3頁共10頁") == set()


def test_extract_empty():
    assert extract_citations("") == []
    assert extract_citations("本判決與法條無關") == []
