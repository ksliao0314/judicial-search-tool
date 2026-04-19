"""cons (憲法解釋) normalizer 測試，特別覆蓋舊制釋字大法官名單抽取。

舊釋字 reasoning 尾巴一律有「大法官會議主席...大法官 A B C...」格式，
4 種格式變體統計 813 筆、有 reasoning 的 734 筆全部命中（100% recall）。
"""
from __future__ import annotations

from src.pipeline.cons_normalizer import (
    is_interpretation_case_id,
    is_old_interpretation,
    add_display_prefix,
    strip_cons_prefix,
    normalize_cons_date,
    extract_year_from_cons_date,
    split_old_interpretation_reasoning,
    normalize_cons_judgment,
    normalize_cons_hit,
)


# ─── case_id 分流 ──────────────────────────────────────────────────

def test_is_interpretation_case_id_recognizes_old():
    assert is_interpretation_case_id("釋字第748號")
    assert is_interpretation_case_id("釋字 748")
    assert is_interpretation_case_id("釋字748")
    # 含「司法院」prefix 也要認（DB 存的全稱）
    assert is_interpretation_case_id("司法院釋字第748號")
    assert is_old_interpretation("司法院釋字第748號")


# ─── Display prefix / strip ───────────────────────────────────────

def test_add_display_prefix_old():
    assert add_display_prefix("釋字第748號") == "司法院釋字第748號"
    assert add_display_prefix("釋字748") == "司法院釋字748"


def test_add_display_prefix_idempotent():
    """已有 prefix 不重複加"""
    assert add_display_prefix("司法院釋字第748號") == "司法院釋字第748號"


def test_add_display_prefix_not_old_unchanged():
    """新制憲判字不加 prefix；一般判決原樣"""
    assert add_display_prefix("111年憲判字第1號") == "111年憲判字第1號"
    assert add_display_prefix("臺北高等行政法院 107 年度全字第 69 號") == "臺北高等行政法院 107 年度全字第 69 號"


def test_strip_cons_prefix_old():
    assert strip_cons_prefix("司法院釋字第748號") == "釋字第748號"


def test_strip_cons_prefix_non_old_unchanged():
    assert strip_cons_prefix("釋字第748號") == "釋字第748號"
    assert strip_cons_prefix("111年憲判字第1號") == "111年憲判字第1號"
    assert strip_cons_prefix("臺北高等行政法院 107 號") == "臺北高等行政法院 107 號"


def test_normalize_cons_hit_applies_prefix():
    """search_interpretations hit 經 normalize 後、jid 與 case_id 皆為「司法院釋字」全稱。
    Stage 2.5 呼叫 MCP 前 `_fetch_one` 會用 strip_cons_prefix 剝 prefix。"""
    hit = {"type": "釋字", "case_id": "釋字第748號", "title": "釋字第748號", "issues": "同性婚姻"}
    out = normalize_cons_hit(hit)
    assert out["case_id"] == "司法院釋字第748號"
    assert out["jid"] == "司法院釋字第748號"
    # Stage 2.5 的 _fetch_one 再用 strip_cons_prefix 剝回「釋字第748號」送 MCP
    assert strip_cons_prefix(out["jid"]) == "釋字第748號"


def test_is_interpretation_case_id_recognizes_new():
    assert is_interpretation_case_id("111年憲判字第1號")
    assert is_interpretation_case_id("111憲判1")


def test_is_interpretation_case_id_rejects_regular_judgment():
    assert not is_interpretation_case_id("臺北高等行政法院 107 年度全字第 69 號")
    assert not is_interpretation_case_id("109 年度訴字第 1234 號")


def test_is_old_interpretation_only_matches_old():
    assert is_old_interpretation("釋字第748號")
    assert not is_old_interpretation("111年憲判字第1號")
    assert not is_old_interpretation("臺北高等行政法院 107 年度全字第 69 號")


# ─── 日期正規化 ────────────────────────────────────────────────────

def test_normalize_cons_date_old():
    assert normalize_cons_date("中華民國 52年05月22日") == "52-05-22"


def test_normalize_cons_date_new():
    assert normalize_cons_date("111年05月13日") == "111-05-13"


def test_normalize_cons_date_empty():
    assert normalize_cons_date("") == ""
    assert normalize_cons_date("invalid") == ""


def test_extract_year():
    assert extract_year_from_cons_date("中華民國 52年05月22日") == 52
    assert extract_year_from_cons_date("111年05月13日") == 111
    assert extract_year_from_cons_date("invalid") is None


# ─── 舊釋字 reasoning 尾端大法官名單抽取 ────────────────────────────

def test_split_standard_format_院長():
    """最常見：大法官會議主席　院　長　XXX"""
    reasoning = "一、本院認為... 末段結論。\n\n大法官會議主席　院　長　施啟揚\n\n大法官　翁岳生　劉鐵錚　吳　庚　王和雄\n\n王澤鑑　林永謀"
    cleaned, judges = split_old_interpretation_reasoning(reasoning)
    assert "大法官會議" not in cleaned
    assert "施啟揚" not in cleaned
    assert judges == ["施啟揚", "翁岳生", "劉鐵錚", "吳庚", "王和雄", "王澤鑑", "林永謀"]


def test_split_副院長_format():
    """第二常見：大法官會議主席　副院長　XXX"""
    reasoning = "結論文。\n\n大法官會議主席　副院長　汪道淵\n\n大法官　翁岳生　翟紹先"
    cleaned, judges = split_old_interpretation_reasoning(reasoning)
    assert "汪道淵" in judges
    assert "汪道淵" not in cleaned


def test_split_spaced_主_席_format():
    """偶有：大法官會議　主　席　XXX（主席 label 分字）"""
    reasoning = "結論。\n\n大法官會議　主　席　翁岳生\n\n大法官　劉鐵錚　王和雄"
    cleaned, judges = split_old_interpretation_reasoning(reasoning)
    assert judges == ["翁岳生", "劉鐵錚", "王和雄"]
    assert "翁岳生" not in cleaned


def test_split_代理院長_format():
    """括號備註：大法官會議主席（代理院長）　大法官　XXX"""
    reasoning = "結論。\n\n大法官會議主席（代理院長）　大法官　謝在全\n\n大法官　賴英照　林子儀"
    cleaned, judges = split_old_interpretation_reasoning(reasoning)
    assert judges == ["謝在全", "賴英照", "林子儀"]
    assert "謝在全" not in cleaned
    assert "代理院長" not in cleaned


def test_split_merges_single_char_names():
    """2 字名如「吳庚」在司法院排版中會拆成「吳　庚」，normalizer 必須 merge 回去。"""
    reasoning = "結論。\n\n大法官會議主席　院　長　施啟揚\n\n大法官　吳　庚　王和雄　呂　生"
    _, judges = split_old_interpretation_reasoning(reasoning)
    assert "吳庚" in judges
    assert "吳" not in judges  # 不該有單字殘留
    assert "呂生" in judges


def test_split_dedups_names():
    """同名字不重複出現（理論上不會但保險）。"""
    reasoning = "結論。\n\n大法官會議主席　院　長　施啟揚\n\n大法官　施啟揚　翁岳生"
    _, judges = split_old_interpretation_reasoning(reasoning)
    assert judges.count("施啟揚") == 1


def test_split_no_marker_returns_empty():
    """reasoning 無大法官名單標記時原樣返回、judges 空。"""
    reasoning = "單純的理由書，沒有名單區塊結尾。"
    cleaned, judges = split_old_interpretation_reasoning(reasoning)
    assert cleaned == reasoning
    assert judges == []


# ─── 整合：normalize_cons_judgment 舊釋字判決 ──────────────────────

def test_normalize_cons_judgment_old_interpretation_extracts_judges():
    cons_data = {
        "case_id": "釋字第381號",
        "case_number": "釋字第381號",
        "date": "中華民國 84年06月09日",
        "issues": "國大修憲一讀會開議人數標準如何？",
        "main_text": "憲法第一百七十四條第一款...",
        "reasoning": "本案爭點... 結論部分。\n\n大法官會議主席　院　長　施啟揚\n\n大法官　翁岳生　劉鐵錚　吳　庚　王和雄",
        "related_statutes": "憲法第174條第1款",
        "source_url": "https://cons.judicial.gov.tw/...",
    }
    result = normalize_cons_judgment(cons_data)
    # DB 存全稱（加「司法院」prefix）
    assert result["case_id"] == "司法院釋字第381號"
    assert result["date"] == "84-06-09"
    assert result["court"] == "憲法法庭"
    assert result["judges"] == ["施啟揚", "翁岳生", "劉鐵錚", "吳庚", "王和雄"]
    # reasoning 已切除名單
    assert "大法官會議" not in result["reasoning"]
    assert "施啟揚" not in result["reasoning"]
    assert result["reasoning"].endswith("結論部分。")
    # full_text 應用切過的 reasoning（不含名單）
    assert "大法官會議" not in result["full_text"]


def test_normalize_cons_judgment_new_ruling_keeps_reasoning_as_is():
    """新制憲判字 reasoning 不含名單、normalizer 不切、judges 為 None。"""
    cons_data = {
        "case_id": "111年憲判字第1號",
        "case_number": "111年憲判字第1號【肇事駕駛人受強制抽血檢測酒精濃度案】",
        "date": "111年05月13日",
        "issue_summary": "本案的憲法爭點...",
        "main_text": "主文：系爭規定違憲。",
        "reasoning": "壹、案件事實與聲請意旨【1】\n\n一、原因案件【2】\n\n聲請人主張...",
        "related_statutes": "道路交通管理處罰條例第35條",
        "petitioner": "聲請人A",
        "source_url": "https://cons.judicial.gov.tw/...",
    }
    result = normalize_cons_judgment(cons_data)
    assert result["date"] == "111-05-13"
    assert result["judges"] is None  # 新制無名單
    # reasoning 沒被切
    assert "聲請人主張" in result["reasoning"]
    # cause 用 issue_summary（新制）
    assert result["cause"] == "本案的憲法爭點..."
    # parties 有聲請人
    assert result["parties"] == {"聲請人": ["聲請人A"]}
