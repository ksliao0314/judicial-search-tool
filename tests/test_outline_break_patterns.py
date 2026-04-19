"""驗證 `_insert_outline_breaks` 的 pattern 容忍度已對齊 frontend。

背景：backend 只有在「句末標點 + outline marker」之間插 `\\n`，讓 frontend 的行首
marker 偵測能夠抓到。若 backend 規則比 frontend 嚴格，judgment 用 frontend-accept
但 backend-reject 的變體格式（e.g. `壹．` 全形句點、`一.` ASCII 句點、全形數字 `１．`）
時，backend 不插斷行 → frontend 收到一長行 → marker 在中段不在行首 → outline 全滅。

本測試鎖這些變體都能正確插入 `\\n`，順帶確保正式格式（`壹、` `一、` `1.`）也沒 regress。
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "mcp-taiwan-legal-db"))

from mcp_server.parsers.judicial_parser import _insert_outline_breaks  # noqa: E402


def _assert_breaks_before(marker: str, text: str):
    out = _insert_outline_breaks(text)
    assert f"\n{marker}" in out, f"expected break before {marker!r} in: {out!r}"


# ─── 基本正式格式（regression guard：不能退步）─────────────────
def test_L0_formal_comma():
    _assert_breaks_before("壹、", "判決如下。壹、本案經受理。")


def test_L1_formal_comma():
    _assert_breaks_before("一、", "判決如下。一、經查本案。")


def test_L2_enclosed():
    _assert_breaks_before("㈠", "判決如下。㈠經查本案。")


def test_L2_paren():
    _assert_breaks_before("(一)", "判決如下。(一)經查本案。")


def test_L3_enclosed():
    _assert_breaks_before("⒈", "判決如下。⒈經查本案。")


def test_L3_arabic_ascii_dot():
    _assert_breaks_before("1.", "判決如下。1.經查本案。")


def test_L4_enclosed():
    _assert_breaks_before("⑴", "判決如下。⑴經查本案。")


def test_L4_paren():
    _assert_breaks_before("(1)", "判決如下。(1)經查本案。")


def test_L5_circled():
    _assert_breaks_before("①", "判決如下。①經查本案。")


# ─── 新增容忍度：L0 / L1 後接變體標點 ─────────────────────────
def test_L0_full_width_period():
    """`壹．` — 全形句點，常見於手打或舊判決"""
    _assert_breaks_before("壹．", "判決如下。壹．本案經受理。")


def test_L0_ascii_period():
    """`壹.` — ASCII 句點"""
    _assert_breaks_before("壹.", "判決如下。壹.本案經受理。")


def test_L0_multi_char():
    """`甲乙、` — 多字 L0 marker"""
    _assert_breaks_before("甲乙、", "判決如下。甲乙、兩造合意。")


def test_L1_full_width_period():
    """`一．` — 全形句點"""
    _assert_breaks_before("一．", "判決如下。一．經查本案。")


def test_L1_ascii_period():
    """`一.` — ASCII 句點"""
    _assert_breaks_before("一.", "判決如下。一.經查本案。")


def test_L1_with_百零():
    """`一百零一、` — 含百零〇字形"""
    _assert_breaks_before("一百零一、", "規定如下：一百零一、第一百零一條規定。")


def test_L2_paren_with_百():
    """`(一百)` — L2 paren 含百"""
    _assert_breaks_before("(一百)", "規定如下：(一百)第百條規定。")


# ─── 新增容忍度：L3 / L4 全形數字 ─────────────────────────────
def test_L3_full_width_digit():
    """`１．` — 全形數字 + 全形句點"""
    _assert_breaks_before("１．", "規定如下：１．第一項規定。")


def test_L3_ascii_full_width_dot():
    """`1．` — ASCII 數字 + 全形句點"""
    _assert_breaks_before("1．", "規定如下：1．第一項規定。")


def test_L3_two_digit():
    """`12.` — 兩位數"""
    _assert_breaks_before("12.", "規定如下：12.第十二項規定。")


def test_L4_full_width_digit():
    """`（１）` — 全形括號 + 全形數字"""
    _assert_breaks_before("（１）", "規定如下：（１）第一項規定。")


def test_L4_mixed():
    """`(１)` — 半形括號 + 全形數字"""
    _assert_breaks_before("(１)", "規定如下：(１)第一項規定。")


# ─── Negative cases：不該誤插的情境 ───────────────────────────
def test_L3_arabic_decimal_not_broken():
    """`1.5` / `1.234` 等小數不應被當 L3 marker"""
    out = _insert_outline_breaks("金額為1.5萬元。")
    assert "\n1." not in out, f"小數不該觸發 break：{out!r}"


def test_L3_decimal_after_punctuation():
    """`。1.5` — 句號後接小數，仍不該當 L3（因為後面接數字）"""
    out = _insert_outline_breaks("總計。1.5萬元")
    assert "\n1." not in out, f"句號後小數不該 break：{out!r}"


def test_midword_marker_not_broken():
    """`民國95年` 中的 `95` 不在 `。：；` 後，不該被 L3 pattern 命中"""
    out = _insert_outline_breaks("於民國95年2月20日受理。")
    # 不應產生 \n95 或 \n2
    assert "\n9" not in out
    assert "\n2" not in out


def test_no_break_before_letter():
    """`。本院` — 句號後接漢字，不該有任何 break"""
    out = _insert_outline_breaks("判決如下。本院認為合法。")
    assert out == "判決如下。本院認為合法。", f"不該修改：{out!r}"


# ─── 組合場景 ─────────────────────────────────────────────────
def test_multiple_markers_in_sequence():
    """連續多個 marker 都要插斷行"""
    text = "規定如下：1.第一項；2.第二項；3.第三項。"
    out = _insert_outline_breaks(text)
    assert out.count("\n1.") == 1
    assert out.count("\n2.") == 1
    assert out.count("\n3.") == 1


def test_idempotent():
    """對已有 \\n 的輸入再跑一次，不會重複插入"""
    text = "判決如下：\n1.已經有斷行\n2.也是"
    out = _insert_outline_breaks(text)
    # 不該有 \n\n（重複 break）
    assert "\n\n" not in out


def test_text_pre_source_no_side_effect():
    """.text-pre 來源已有 \\n，不該觸發額外 break（正向回歸）"""
    text = "判決如下：\n壹、本案。\n一、經查。"
    out = _insert_outline_breaks(text)
    assert out == text, f".text-pre 不該被改動：{out!r}"
