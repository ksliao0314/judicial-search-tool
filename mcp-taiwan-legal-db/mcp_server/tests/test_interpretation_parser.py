"""舊制大法官解釋（釋字 1-813）結構解析器 — 每 era 3 則 golden 案例測試。

用真實釋字文本當 fixture、不 mock。
Fixtures 從 mcp_server/data/old_cases.json 載入（lazy）— 測試執行環境必須
有此檔案（dev / release 皆內建）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server.parsers.interpretation_parser import (
    _era_of,
    parse_interpretation,
)

# ─── Fixture loader ─────────────────────────────
_DATA = Path(__file__).resolve().parents[1] / "data" / "old_cases.json"


@pytest.fixture(scope="session")
def old_cases() -> dict:
    with open(_DATA) as f:
        return json.load(f)


def _parse(old_cases, cid: int):
    d = old_cases[str(cid)]
    return parse_interpretation(
        cid=cid,
        main_text=d.get("main_text"),
        reasoning=d.get("reasoning"),
        issues=d.get("issues"),
    )


def _roles(result) -> list[str]:
    return [s["role"] for s in result["sections"]]


# ─── Era 分層 ─────────────────────────────
class TestEraBoundaries:
    def test_era_a(self):
        assert _era_of(1) == "A"
        assert _era_of(50) == "A"
        assert _era_of(100) == "A"

    def test_era_b(self):
        assert _era_of(101) == "B"
        assert _era_of(200) == "B"
        assert _era_of(300) == "B"

    def test_era_c(self):
        assert _era_of(301) == "C"
        assert _era_of(600) == "C"

    def test_era_d(self):
        assert _era_of(601) == "D"
        assert _era_of(800) == "D"

    def test_era_e(self):
        assert _era_of(801) == "E"
        assert _era_of(813) == "E"


# ─── Era A：無理由書（79 則）+ 有理由書（21 則） ────────
class TestEraA:
    """Era A (釋字 1-100)：79 則無理由書、應回空 sections；其餘 21 則走一般流程。"""

    def test_cid_1_no_reasoning(self, old_cases):
        """釋字第1號（中華民國 38 年）— 最早期、無獨立理由書。"""
        r = _parse(old_cases, 1)
        assert r["era"] == "A"
        assert r["summary"]  # 有解釋文
        assert r["sections"] == []  # 無理由書、sections 全空

    def test_cid_50_no_reasoning(self, old_cases):
        """釋字第50號 — 確認早期無理由書特例。"""
        r = _parse(old_cases, 50)
        assert r["era"] == "A"
        assert r["sections"] == []

    def test_cid_82_with_reasoning(self, old_cases):
        """釋字第82號（民國 48 年）— Era A 少數有理由書的案例。"""
        r = _parse(old_cases, 82)
        assert r["era"] == "A"
        # Era A 的 21 則有理由書案例應至少切出 court_reasoning + signatures
        roles = _roles(r)
        assert "court_reasoning" in roles
        assert "signatures" in roles


# ─── Era B：短札期（101-300） ─────────────────────
class TestEraB:
    def test_cid_150_full_four_sections(self, old_cases):
        """釋字第150號 — 教科書級 4-section 結構（聲請/見解/結論/署名）。"""
        r = _parse(old_cases, 150)
        assert r["era"] == "B"
        assert _roles(r) == ["petitioner_claim", "court_reasoning", "conclusion", "signatures"]
        sec = {s["role"]: s for s in r["sections"]}
        assert "本件聲請人" in sec["petitioner_claim"]["text"]
        assert "依上說明" in sec["conclusion"]["text"]

    def test_cid_250_reasoning_only(self, old_cases):
        """釋字第250號 — Era B 常見型：無獨立聲請段、reasoning + signatures。"""
        r = _parse(old_cases, 250)
        assert r["era"] == "B"
        assert "court_reasoning" in _roles(r)
        assert "signatures" in _roles(r)

    def test_cid_200_has_signatures(self, old_cases):
        """任何有理由書的 Era B case 都應切出 signatures。"""
        r = _parse(old_cases, 200)
        assert r["era"] == "B"
        assert "signatures" in _roles(r)


# ─── Era C：成長期（301-600） ─────────────────────
class TestEraC:
    def test_cid_450_reasoning_only(self, old_cases):
        """釋字第450號 — 單段 reasoning + signatures。"""
        r = _parse(old_cases, 450)
        assert r["era"] == "C"
        assert "court_reasoning" in _roles(r)
        assert "signatures" in _roles(r)

    def test_cid_550_long_reasoning(self, old_cases):
        """釋字第550號 — 較長 reasoning（社福分擔案）。"""
        r = _parse(old_cases, 550)
        assert r["era"] == "C"
        assert "court_reasoning" in _roles(r)
        sec = next(s for s in r["sections"] if s["role"] == "court_reasoning")
        assert len(sec["text"]) > 1000  # 該案 reasoning 確實長

    def test_cid_500_has_signatures(self, old_cases):
        r = _parse(old_cases, 500)
        assert r["era"] == "C"
        assert "signatures" in _roles(r)

    def test_cid_419_enumeration_cases(self, old_cases):
        """釋字第419號 — 4 件聲請合併、聲請段用「一、二、三、四、立法委員X」enumeration。

        petitioner 應含 P1-P17（包括 4 案聲請列表 + 聲請人主張 + 關係機關主張 +
        「本件斟酌...作成本解釋，其理由如左：」hard boundary meta 段）。
        """
        r = _parse(old_cases, 419)
        assert r["era"] == "C"
        sec = {s["role"]: s for s in r["sections"]}
        assert "petitioner_claim" in sec
        pet_text = sec["petitioner_claim"]["text"]
        # 4 案聲請 enumeration（立法委員 X 等 Y 人）都應在內
        assert "立法委員郝龍斌" in pet_text
        assert "立法委員張俊雄" in pet_text
        assert "立法委員馮定國" in pet_text
        assert "立法委員饒穎奇" in pet_text
        # 聲請人之主張 transition 段
        assert "本件前述第一案至第三案聲請人之主張略稱" in pet_text
        # 關係機關行政院主張
        assert "關係機關行政院" in pet_text
        # Hard boundary 段「本件斟酌全辯論意旨，作成本解釋，其理由如左」屬 petitioner meta
        assert "本件斟酌" in pet_text
        # Reasoning 不該含任何聲請 meta / 關係機關主張
        court_text = sec["court_reasoning"]["text"]
        assert "立法委員" not in court_text or "立法委員郝龍斌" not in court_text

    def test_cid_520_agency_as_petitioner(self, old_cases):
        """釋字第520號 — 行政院為聲請人（本件行政院為決議…）、權力分立案件。"""
        r = _parse(old_cases, 520)
        assert r["era"] == "C"
        sec = {s["role"]: s for s in r["sections"]}
        assert "petitioner_claim" in sec
        pet_text = sec["petitioner_claim"]["text"]
        assert pet_text.startswith("本件行政院")
        assert "停止興建核能第四電廠" in pet_text

    def test_cid_601_dazheng_outline_with_procedural(self, old_cases):
        """釋字第601號 — 壹、受理程序 / 貳、… 結構、需 split 出 procedural_ruling。

        壹、受理程序 裡除了聲請意旨、還有法院對迴避 / 受理要件的論述；
        後者語意屬本院認定、應獨立為 procedural_ruling role、供 LLM 精讀參考。
        """
        r = _parse(old_cases, 601)
        sec = {s["role"]: s for s in r["sections"]}
        # petitioner 短、只含 opener + 本件聲請意旨 chunk
        assert "petitioner_claim" in sec
        pet_text = sec["petitioner_claim"]["text"]
        assert pet_text.startswith("壹、受理程序")
        assert "本件聲請意旨" in pet_text
        # 受理程序 法院論述獨立為 procedural_ruling
        assert "procedural_ruling" in sec
        proc_text = sec["procedural_ruling"]["text"]
        assert "迴避制度" in proc_text
        assert "司法院大法官" in proc_text or "大法官" in proc_text
        # reasoning 從貳、起
        court_text = sec["court_reasoning"]["text"]
        assert court_text.startswith("貳、")

    def test_cid_445_multi_agency_petitioner(self, old_cases):
        """釋字第445號 — 多機關主張案（次查聲請人 + 4 機關主張）。

        petitioner_claim 應含 P1-P6（6037字左右）、court_reasoning 從「司法院解釋
        憲法，並有統一解釋法律及命令之權」開始。
        """
        r = _parse(old_cases, 445)
        assert r["era"] == "C"
        sec = {s["role"]: s for s in r["sections"]}
        assert "petitioner_claim" in sec
        pet_text = sec["petitioner_claim"]["text"]
        # 聲請人 + 4 個機關陳述都該在 petitioner
        assert "次查聲請人主張" in pet_text
        assert "相關機關行政院則主張" in pet_text
        assert "法務部" in pet_text and "兼代行政院" in pet_text
        assert "內政部及內政部警政署之主張" in pet_text
        assert "交通部提出書狀主張" in pet_text
        # 總長度 > 5000 字確保全部機關 claim 都收進來
        assert len(pet_text) > 5000, f"petitioner 長度過短 ({len(pet_text)})"
        # court_reasoning 不應再含機關主張 opener
        court_text = sec["court_reasoning"]["text"]
        assert "交通部提出書狀主張" not in court_text
        assert court_text.startswith("司法院解釋憲法")


# ─── Era D：爆量期（601-800） ─────────────────────
class TestEraD:
    def test_cid_603_complex_petitioner(self, old_cases):
        """釋字第603號（指紋案）— 多段 petitioner + 關係機關陳述、應全入 petitioner_claim。"""
        r = _parse(old_cases, 603)
        assert r["era"] == "D"
        roles = _roles(r)
        assert "petitioner_claim" in roles
        assert "court_reasoning" in roles
        # petitioner 含關係機關陳述 + 「本院斟酌...理由如下：」meta 段
        sec = {s["role"]: s for s in r["sections"]}
        pet_text = sec["petitioner_claim"]["text"]
        assert "立法委員" in pet_text
        assert "關係機關" in pet_text
        assert "理由如下" in pet_text  # hard boundary 段仍歸 petitioner

    def test_cid_750_subsections(self, old_cases):
        """釋字第750號 — 有「一、二、三」sub-header、應切 subsections。"""
        r = _parse(old_cases, 750)
        assert r["era"] == "D"
        sec = next(s for s in r["sections"] if s["role"] == "court_reasoning")
        subs = sec.get("subsections", [])
        assert len(subs) > 1  # 至少 2 個 sub-section
        # 至少其中一個 title 含「系爭規定」
        assert any("系爭規定" in (s.get("title") or "") for s in subs)

    def test_cid_650_no_subheader(self, old_cases):
        """釋字第650號 — Era D 無 sub-header 的案例、subsections 應為空或單一。"""
        r = _parse(old_cases, 650)
        assert r["era"] == "D"
        sec = next(s for s in r["sections"] if s["role"] == "court_reasoning")
        subs = sec.get("subsections", [])
        # 無 sub-header → 要麼根本無 subsections field、要麼單一 subsection
        assert len(subs) <= 1


# ─── Era E：過渡期（801-813） ─────────────────────
class TestEraE:
    def test_cid_802_petitioner_with_court(self, old_cases):
        """釋字第802號 — 「聲請人臺灣...法官」開頭、需認出 petitioner + subsections。"""
        r = _parse(old_cases, 802)
        assert r["era"] == "E"
        roles = _roles(r)
        assert "petitioner_claim" in roles
        assert "court_reasoning" in roles
        sec = next(s for s in r["sections"] if s["role"] == "court_reasoning")
        assert len(sec.get("subsections", [])) > 1

    def test_cid_813_yuan_prefix(self, old_cases):
        """釋字第813號（歷史建築案）— 「緣聲請人慈祐宮...」首段、需認出 petitioner + 結論。"""
        r = _parse(old_cases, 813)
        assert r["era"] == "E"
        roles = _roles(r)
        assert "petitioner_claim" in roles
        assert "court_reasoning" in roles
        assert "conclusion" in roles
        sec = {s["role"]: s for s in r["sections"]}
        assert sec["conclusion"]["text"].startswith("綜上")

    def test_cid_801_basic_structure(self, old_cases):
        r = _parse(old_cases, 801)
        assert r["era"] == "E"
        roles = _roles(r)
        assert "court_reasoning" in roles
        assert "signatures" in roles


# ─── 防呆 / 邊界條件 ─────────────────────────────
class TestEdgeCases:
    def test_empty_inputs(self):
        r = parse_interpretation(cid=1, main_text="", reasoning="")
        assert r["summary"] == ""
        assert r["sections"] == []

    def test_none_inputs(self):
        r = parse_interpretation(cid=1, main_text=None, reasoning=None)
        assert r["summary"] == ""
        assert r["sections"] == []

    def test_issues_passthrough(self, old_cases):
        d = old_cases["500"]
        r = parse_interpretation(
            cid=500, main_text=d.get("main_text"),
            reasoning=d.get("reasoning"), issues=d.get("issues"),
        )
        assert r["issues"] == (d.get("issues") or "").replace("\xa0", " ").replace("\u3000", " ").strip()

    def test_signatures_always_last(self, old_cases):
        """signatures 永遠是最後一個 section（若存在）。"""
        for cid in [150, 250, 450, 603, 750, 802, 813]:
            r = _parse(old_cases, cid)
            if "signatures" in _roles(r):
                assert r["sections"][-1]["role"] == "signatures"

    def test_role_schema_consistency(self, old_cases):
        """所有 section 都有 role + title + text、role ∈ 已定義集合。"""
        valid_roles = {"petitioner_claim", "court_reasoning", "conclusion", "signatures"}
        for cid in [1, 150, 450, 603, 750, 813]:
            r = _parse(old_cases, cid)
            for sec in r["sections"]:
                assert sec["role"] in valid_roles
                assert "title" in sec
                assert "text" in sec


# ─── Full-scale 健康檢查（cheap regression）────
class TestFullScaleHealth:
    """跑全部 813 則、檢查統計分佈不會大幅偏離 Phase 1 勘查結論。"""

    def test_era_a_no_section_count(self, old_cases):
        """Era A 應有 ~79 則無 sections（無理由書）、允許 ±2 波動。"""
        no_sect = 0
        for cid in range(1, 101):
            r = _parse(old_cases, cid)
            if not r["sections"]:
                no_sect += 1
        assert 77 <= no_sect <= 81, f"Era A no-section count = {no_sect}、偏離勘查預期 79"

    def test_era_b_c_d_e_all_have_signatures(self, old_cases):
        """Era B-E 所有釋字都有理由書、應 100% 切出 signatures。"""
        missing = []
        for cid in range(101, 814):
            r = _parse(old_cases, cid)
            if "signatures" not in _roles(r):
                missing.append(cid)
        assert not missing, f"Era B-E 共 {len(missing)} 則 signatures miss: {missing[:10]}"

    def test_no_parse_crashes(self, old_cases):
        """全 813 則都應 parse 成功不 raise。"""
        errors = []
        for cid in range(1, 814):
            try:
                _parse(old_cases, cid)
            except Exception as e:
                errors.append((cid, str(e)))
        assert not errors, f"Parse 失敗 {len(errors)} 則：{errors[:5]}"
