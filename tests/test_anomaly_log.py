"""anomaly_log 偵測規則測試。

包含：
- 合成 case：每種 anomaly type 對應一個正例 + 一個負例（confirm 規則準確）
- 5 件真實 fixture：跑 check_anomalies 看實際分布（informational，無 assertion）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.utils.anomaly_log import (
    check_anomalies,
    log_judgment,
    _LOG_PATH,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ─── 合成 case：confirm 每條規則 ──────────────────────────────────────

def _normal_reasoning() -> str:
    """產生正常硬斷行的 reasoning（模擬 MCP text-pre 來源每行 ~26 字）。"""
    sentences = [
        "本院按民法第184條之規定，被告應負損害",
        "賠償責任。原告主張被告侵權行為成立，業",
        "據其提出證據資料可考。本院審酌兩造主張",
        "與原審認定，認原告之請求有理由，應予准",
        "許。",
    ]
    return "\n".join(sentences * 20)  # 100 行 ~26 字一行


def test_no_anomaly_for_normal_judgment():
    """正常判決應該無 anomaly。"""
    jud = {
        "case_id": "TEST,113,訴,1,20250101,1",
        "reasoning": _normal_reasoning(),
        "main_text": "被告應給付原告新臺幣壹拾萬元。",
        "facts": "",  # 民事合併段，預期空，不算 anomaly
        "full_text": "TEST FULL",
        "cited_statutes": ["民法第184條", "民法第188條"],
    }
    anomalies, _ = check_anomalies(jud)
    assert anomalies == [], f"normal 判決誤報: {anomalies}"


def test_all_fields_empty():
    """三個欄位都 < 50 字 → all_fields_empty。"""
    jud = {"reasoning": "xx", "main_text": "yy", "facts": "", "cited_statutes": []}
    anomalies, _ = check_anomalies(jud)
    assert "all_fields_empty" in anomalies
    # 已有 all_fields_empty 就不再加 empty_reasoning / empty_main_text 雜訊
    assert "empty_reasoning" not in anomalies
    assert "empty_main_text" not in anomalies


def test_empty_main_text_only():
    """只有 main_text 空 → 只報 empty_main_text，不報 all_fields_empty。"""
    jud = {
        "reasoning": "本院判斷：" + "x" * 1000,
        "main_text": "",
        "facts": "",
        "cited_statutes": [],
    }
    anomalies, _ = check_anomalies(jud)
    assert "empty_main_text" in anomalies
    assert "all_fields_empty" not in anomalies


def test_fullwidth_arabic_l3_marker_recognized():
    """判決書常用全形 `１、` 當 L3（臺北高行 107 全字第 69 號）— 要被
    `_has_outline_marker` 認出，否則長行沒 marker 的 anomaly 會假陽性誤報。"""
    from src.utils.anomaly_log import _has_outline_marker
    assert _has_outline_marker("１、公法上法律關係發生爭執"), \
        "全形 `１、` 應該被視為 L3 outline marker"
    assert _has_outline_marker("２．為防止發生重大之損害"), \
        "全形 `２．` 也應該被視為 L3 outline marker"
    assert _has_outline_marker("1、公法上法律關係"), "半形 `1、` 同樣應該認"
    # 負例：數字後非 L3 分隔符
    assert not _has_outline_marker("１０件判決"), "`１０件` 不是 outline marker"


def test_fullwidth_l3_not_flagged_as_long_line_no_marker():
    """Regression：全形 L3 的長行不該被 long_line_no_marker 誤報。
    臺北高行 107 全字 69 號完整流程："""
    long_l3_line = ("１、公法上法律關係發生爭執：①如爭執之公法上法律關係所由發生的事件已終結"
                    "，即無再為定暫時狀態處分之必要（參照最高行政法院101年度裁定第573號裁定意旨）"
                    "②爭執之公法上法律關係本身須具備訟爭性，如系爭公法上法律關係已經確定，"
                    "而無法再以通常救濟途徑予以變更或撤銷時，自無聲請定暫時狀態處分之餘地。"
                    "③公法上法律關係之爭執應限於假處分所保全之本案行政爭訟標的。" + "x" * 200)
    jud = {
        "reasoning": f"（二）又行政訴訟法第298條第2項規定：...其要件有三：\n{long_l3_line}\n",
        "main_text": "駁回聲請。",
        "cited_statutes": ["行政訴訟法第298條"],
    }
    anomalies, metrics = check_anomalies(jud)
    assert "long_line_no_marker" not in anomalies, \
        f"全形 `１、` 開頭的長行不該被誤判，實際 anomalies={anomalies}"


def test_long_line_no_marker():
    """超過 500 字一行且該行無 outline marker → long_line_no_marker。"""
    long_line = "本院認為此案爭點甚多，茲分析如下。" + "x" * 600
    jud = {
        "reasoning": f"一、爭訟概要\n{long_line}\n二、本院判斷\n本院認為...",
        "main_text": "駁回上訴。",
        "cited_statutes": [],
    }
    anomalies, metrics = check_anomalies(jud)
    assert "long_line_no_marker" in anomalies
    assert metrics["long_lines_no_marker"] == 1


def test_long_line_with_marker_not_anomaly():
    """超 500 字但行首有 outline marker → 不算 anomaly（最高行 .htmlcontent 段落）。"""
    long_line_with_marker = "(一)" + "x" * 600
    jud = {
        "reasoning": f"一、爭訟概要\n{long_line_with_marker}\n",
        "main_text": "駁回上訴。",
        "cited_statutes": [],
    }
    anomalies, metrics = check_anomalies(jud)
    assert "long_line_no_marker" not in anomalies
    assert metrics["long_lines_no_marker"] == 0


def test_quote_unbalanced():
    """引號開關差 > 5 → quote_unbalanced。"""
    jud = {
        "reasoning": "本院認為「" * 10 + "x" * 1000 + "」" * 2,  # 10 開 2 關
        "main_text": "駁回。",
        "cited_statutes": [],
    }
    anomalies, _ = check_anomalies(jud)
    assert "quote_unbalanced" in anomalies


def test_quote_balanced_ok():
    """微小不對稱（≤ 5）不報。"""
    jud = {
        "reasoning": "「a」「b」「c」「d」「e」「f」「g」「h」x" * 30,  # 全平衡
        "main_text": "駁回。",
        "cited_statutes": [],
    }
    anomalies, _ = check_anomalies(jud)
    assert "quote_unbalanced" not in anomalies


def test_cited_statutes_extractor_miss():
    """reasoning 有條號但 cited_statutes 列表空 → cited_statutes_extractor_miss。"""
    jud = {
        "reasoning": "本院按民法第184條規定，駁回。" + "x" * 100,
        "main_text": "駁回。",
        "cited_statutes": [],
    }
    anomalies, _ = check_anomalies(jud)
    assert "cited_statutes_extractor_miss" in anomalies


def test_cited_statutes_present_no_miss():
    """cited_statutes 有東西就不報 miss。"""
    jud = {
        "reasoning": "本院按民法第184條規定，駁回。" + "x" * 100,
        "main_text": "駁回。",
        "cited_statutes": ["民法第184條"],
    }
    anomalies, _ = check_anomalies(jud)
    assert "cited_statutes_extractor_miss" not in anomalies


# ─── outline_number_gap：保守偵測（整層全無 `1`）──────────────────────

def test_outline_gap_L1_no_one_anywhere():
    """整層 L1 沒有 `一` — parser 漏了真正起點的強信號。"""
    reasoning = "\n".join([
        "本院審酌如下。",
        "七、被告之主張不足採。",
        "八、原告之請求應准許。",
        "九、綜上所述。" + "x" * 300,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, metrics = check_anomalies(jud)
    assert "outline_number_gap" in anomalies
    assert metrics["outline_gaps"]["L1"]["issue"] == "no_one"
    assert metrics["outline_gaps"]["L1"]["first"] == 7
    assert metrics["outline_gaps"]["L1"]["total"] == 3


def test_outline_gap_L3_arabic_no_one():
    """L3 arabic 從 `5.` 起一路下去，完全沒 `1.`。"""
    reasoning = "\n".join([
        "規範：",
        "5.擔保物被查封。" + "x" * 100,
        "6.借款人債務不明。" + "x" * 100,
        "7.其他事由。" + "x" * 100,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, metrics = check_anomalies(jud)
    assert "outline_number_gap" in anomalies
    assert metrics["outline_gaps"]["L3"]["issue"] == "no_one"


def test_outline_gap_not_flagged_when_one_exists():
    """律師 `⒎⒏⒐` 邊界案：序列 `[1, 7, 8, 9]`。
    有 `1` 存在，不 flag — 這類由 Pass 2.5 heuristic 負責。"""
    reasoning = "\n".join([
        "規範說明：",
        "1.本金；6.付息（兩項被合併在一起，但起碼有 1）。" + "x" * 100,
        "7.擔保物被查封。" + "x" * 100,
        "8.立約人債務。" + "x" * 100,
        "9.受強制執行。" + "x" * 100,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, _ = check_anomalies(jud)
    assert "outline_number_gap" not in anomalies


def test_outline_gap_multi_section_ok():
    """多 section 判決（事實 + 理由 各從 `一、` 起）— 不 flag。"""
    reasoning = "\n".join([
        "事實：",
        "一、原告主張。" + "x" * 100,
        "二、被告答辯。" + "x" * 100,
        "三、兩造不爭執事項。" + "x" * 100,
        "理由：",
        "一、按民法之規定。" + "x" * 100,
        "二、本院認定。" + "x" * 100,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, _ = check_anomalies(jud)
    assert "outline_number_gap" not in anomalies


def test_outline_gap_single_marker_not_flagged():
    """只有一個 marker — 序列 < 2 不偵測。"""
    reasoning = "\n".join([
        "理由：",
        "一、駁回。" + "x" * 200,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, _ = check_anomalies(jud)
    assert "outline_number_gap" not in anomalies


def test_outline_gap_fullwidth_digit_recognized():
    """全形數字 `１．２．３．` — 有 1 存在不 flag（但全形辨識正確）。"""
    reasoning = "\n".join([
        "規範：",
        "１．本金。" + "x" * 100,
        "２．利息。" + "x" * 100,
        "３．擔保物。" + "x" * 100,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, _ = check_anomalies(jud)
    assert "outline_number_gap" not in anomalies


def test_outline_gap_L2_enclosed_ok():
    """L2 `㈠㈡㈢` 連續從 1 起 — 不該 flag。"""
    reasoning = "\n".join([
        "理由：",
        "㈠事實概要。" + "x" * 100,
        "㈡兩造聲明。" + "x" * 100,
        "㈢爭點。" + "x" * 100,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, _ = check_anomalies(jud)
    assert "outline_number_gap" not in anomalies


def test_outline_gap_cjk_num_二十一_no_one():
    """序列全部從 20 起（沒 `一`）— flag。"""
    reasoning = "\n".join([
        "理由：",
        "二十、第二十項。" + "x" * 100,
        "二十一、第二十一項。" + "x" * 100,
        "二十二、第二十二項。" + "x" * 100,
    ])
    jud = {"reasoning": reasoning, "main_text": "駁回。", "cited_statutes": ["民法"]}
    anomalies, metrics = check_anomalies(jud)
    assert "outline_number_gap" in anomalies
    assert metrics["outline_gaps"]["L1"]["issue"] == "no_one"


def test_metrics_always_returned():
    """無 anomaly 時 metrics 仍應正確填好（供統計用）。"""
    jud = {
        "reasoning": "x" * 1000,
        "main_text": "y" * 50,
        "facts": "",
        "full_text": "z" * 1500,
        "cited_statutes": ["a", "b"],
    }
    _, metrics = check_anomalies(jud)
    assert metrics["reasoning_len"] == 1000
    assert metrics["main_text_len"] == 50
    assert metrics["facts_len"] == 0
    assert metrics["full_text_len"] == 1500
    assert metrics["cited_statutes_count"] == 2


# ─── log_judgment 寫檔測試 ────────────────────────────────────────────

def test_log_judgment_writes_when_anomaly(tmp_path, monkeypatch):
    """有 anomaly → 寫一行 JSONL。"""
    import src.utils.anomaly_log as mod
    log_path = tmp_path / "anomalies.jsonl"
    monkeypatch.setattr(mod, "_LOG_PATH", log_path)

    jud = {
        "case_id": "TEST_AB",
        "court": "Test Court",
        "reasoning": "x",
        "main_text": "y",
        "facts": "",
        "cited_statutes": [],
    }
    wrote = asyncio.run(log_judgment(jud, task_id="t1", jid="JID1"))
    assert wrote is True
    assert log_path.exists()
    line = log_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["case_id"] == "TEST_AB"
    assert record["task_id"] == "t1"
    assert record["jid"] == "JID1"
    assert "all_fields_empty" in record["anomaly_types"]
    assert "metrics" in record


def test_log_judgment_skips_when_no_anomaly(tmp_path, monkeypatch):
    """無 anomaly → 不寫。"""
    import src.utils.anomaly_log as mod
    log_path = tmp_path / "anomalies.jsonl"
    monkeypatch.setattr(mod, "_LOG_PATH", log_path)

    jud = {
        "case_id": "TEST_OK",
        "reasoning": _normal_reasoning(),
        "main_text": "被告應給付原告新臺幣壹拾萬元。",
        "facts": "",
        "cited_statutes": ["民法第184條"],
    }
    wrote = asyncio.run(log_judgment(jud, task_id="t1"))
    assert wrote is False
    assert not log_path.exists()


# ─── 對 5 件真實 fixture 跑（informational） ──────────────────────────

@pytest.mark.parametrize("slug", [
    "tphv_104_jian_shang_98",
    "tpdv_103_jian_401",
    "tcba_106_su_93",
    "tcba_107_su_geng_yi_17",
    "tpaa_110_shang_74",
])
def test_real_fixture_anomaly_distribution(slug, capsys):
    """印出每件 fixture 的 anomaly 分布（無 assertion，純 informational）。"""
    path = FIXTURES_DIR / f"{slug}.json"
    if not path.exists():
        pytest.skip(f"fixture missing: {slug}")
    jud = json.loads(path.read_text(encoding="utf-8"))
    anomalies, metrics = check_anomalies(jud)
    # 用 print 方便 -s 模式時看到
    print(f"\n  {slug}: anomalies={anomalies}")
    print(f"    metrics: reasoning_len={metrics['reasoning_len']} max_line={metrics['max_line_chars']} "
          f"long_lines_no_marker={metrics['long_lines_no_marker']} cited_count={metrics['cited_statutes_count']}")
