"""_apply_narrow court_tier exact-match 回歸測試。

Bug 背景：114 年行政訴訟改制後司法院回傳 court 含「臺北高等行政法院 地方庭」
（中間空格）。舊版 _apply_narrow 用 substring match，tier "高等行政法院" 會把
"臺北高等行政法院 地方庭" 也吞進來；律師想分開兩類時無法分開。

Fix：
1. 加獨立 tier "高等行政法院地方庭"
2. _apply_narrow 改 exact match，依賴 COURT_TIERS 完整列舉分庭名稱
"""
from __future__ import annotations

from src.pipeline.search import COURT_TIERS, expand_court_tiers
from src.worker.runner import _apply_narrow


def _hit(court: str, date: str = "113-01-01", case_id: str = "X"):
    return {"case_id": case_id, "court": court, "date": date, "source_url": ""}


# ─── 新 tier 存在且內容正確 ─────────────────────────────────────────────

def test_new_tier_registered():
    assert "高等行政法院地方庭" in COURT_TIERS
    courts = COURT_TIERS["高等行政法院地方庭"]
    # 三個本院各自對應的地方庭，空格格式與司法院 court 字串一致
    assert "臺北高等行政法院 地方庭" in courts
    assert "臺中高等行政法院 地方庭" in courts
    assert "高雄高等行政法院 地方庭" in courts


def test_expand_new_tier():
    assert expand_court_tiers(["高等行政法院地方庭"]) == [
        "臺北高等行政法院 地方庭",
        "臺中高等行政法院 地方庭",
        "高雄高等行政法院 地方庭",
    ]


# ─── _apply_narrow exact match：核心迴歸 ───────────────────────────────

def test_high_admin_court_tier_does_not_swallow_local_branches():
    """tier '高等行政法院' 不應吞到 '臺北高等行政法院 地方庭'（原本 substring match 的 bug）。"""
    hits = [
        _hit("臺北高等行政法院", case_id="base"),
        _hit("臺北高等行政法院 地方庭", case_id="local"),
    ]
    narrowed = _apply_narrow(hits, {"court_tiers": ["高等行政法院"]})
    ids = [h["case_id"] for h in narrowed]
    assert ids == ["base"], f"expected only base, got {ids}"


def test_local_branch_tier_returns_only_local_branches():
    """tier '高等行政法院地方庭' 只回地方庭，不含本院。"""
    hits = [
        _hit("臺北高等行政法院", case_id="base"),
        _hit("臺北高等行政法院 地方庭", case_id="local"),
        _hit("高雄高等行政法院 地方庭", case_id="local_ks"),
    ]
    narrowed = _apply_narrow(hits, {"court_tiers": ["高等行政法院地方庭"]})
    ids = sorted(h["case_id"] for h in narrowed)
    assert ids == ["local", "local_ks"]


def test_both_tiers_selected_union():
    """兩個 tier 都勾 → 全部本院 + 全部地方庭。"""
    hits = [
        _hit("臺北高等行政法院", case_id="base"),
        _hit("臺北高等行政法院 地方庭", case_id="local"),
        _hit("最高行政法院", case_id="supreme"),  # 不在任一 tier，應排除
    ]
    narrowed = _apply_narrow(hits, {
        "court_tiers": ["高等行政法院", "高等行政法院地方庭"]
    })
    ids = sorted(h["case_id"] for h in narrowed)
    assert ids == ["base", "local"]


def test_local_court_tier_still_works():
    """tier '地方法院' 只含傳統地方法院，不會誤抓地方庭（避免用戶混淆名詞造成的 silent bug）。"""
    hits = [
        _hit("臺灣臺北地方法院", case_id="real"),
        _hit("臺北高等行政法院 地方庭", case_id="admin_local"),
    ]
    narrowed = _apply_narrow(hits, {"court_tiers": ["地方法院"]})
    ids = [h["case_id"] for h in narrowed]
    assert ids == ["real"]


# ─── 既有 tier 不 regress ──────────────────────────────────────────────

def test_high_court_tier_includes_branches():
    """tier '高等法院' 應含本院 + 分院（原本 substring match 能做到，改 exact 後也要保持）。"""
    hits = [
        _hit("臺灣高等法院", case_id="main"),
        _hit("臺灣高等法院臺中分院", case_id="tc"),
        _hit("臺灣高等法院高雄分院", case_id="ks"),
    ]
    narrowed = _apply_narrow(hits, {"court_tiers": ["高等法院"]})
    ids = sorted(h["case_id"] for h in narrowed)
    assert ids == ["ks", "main", "tc"]


def test_year_filter_still_works():
    """year_from/year_to 與 court_tier 疊加應 AND 正確。"""
    hits = [
        _hit("臺北高等行政法院 地方庭", date="113-05-01", case_id="old"),
        _hit("臺北高等行政法院 地方庭", date="114-01-15", case_id="new114"),
        _hit("臺北高等行政法院 地方庭", date="115-06-30", case_id="new115"),
    ]
    narrowed = _apply_narrow(hits, {
        "court_tiers": ["高等行政法院地方庭"],
        "year_from": 114,
        "year_to": 115,
    })
    ids = sorted(h["case_id"] for h in narrowed)
    assert ids == ["new114", "new115"]


def test_empty_narrow_returns_all():
    hits = [_hit("臺北高等行政法院"), _hit("X")]
    assert _apply_narrow(hits, {}) == hits
    assert _apply_narrow(hits, None) == hits
