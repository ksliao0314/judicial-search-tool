"""OR 語法分隔 regex `_OR_SEP_RE` 的行為合約。

支援三種寫法：
  1. 半形 `|` / 全形 `｜`（兩側空白可選）
  2. ` OR ` / ` or `（大小寫不分，**前後需有空白**）
  3. `CJKorCJK` / `CJKORCJK`（兩側皆中日韓字時無空白亦拆）

第 3 條是修 2026-04-19 那天律師輸入 `濫用優勢地位or相對優勢地位or市場優勢地位`
時整串直接送去司法院 Q003 的 bug（中文不加空格是正常打字習慣）。
"""
from src.worker.runner import _parse_or_groups, _flatten_keywords


def test_cjk_adjacent_or_no_space():
    """律師 bug case：CJK 字直接夾 `or`、無空白 → 拆。"""
    assert _parse_or_groups("濫用優勢地位or相對優勢地位or市場優勢地位") == [
        "濫用優勢地位", "相對優勢地位", "市場優勢地位"
    ]


def test_cjk_adjacent_uppercase_or():
    """大小寫不分：`CJKORCJK` 也拆。"""
    assert _parse_or_groups("濫用OR相對") == ["濫用", "相對"]


def test_cjk_adjacent_mixed_case():
    """`Or` / `oR` 也接受。"""
    assert _parse_or_groups("地位Or相對") == ["地位", "相對"]


def test_pipe_halfwidth():
    assert _parse_or_groups("A|B") == ["A", "B"]


def test_pipe_fullwidth():
    """中文 IME 打 `｜`（U+FF5C）也接。"""
    assert _parse_or_groups("A｜B") == ["A", "B"]


def test_or_with_spaces():
    assert _parse_or_groups("A OR B") == ["A", "B"]


def test_or_lowercase_with_spaces():
    assert _parse_or_groups("A or B") == ["A", "B"]


def test_no_or_single_group():
    assert _parse_or_groups("A B") == ["A B"]


def test_multi_and_in_or():
    assert _parse_or_groups("A B | C D") == ["A B", "C D"]


def test_mixed_cjk_and_spaced_or():
    assert _parse_or_groups("濫用or相對 or 市場") == ["濫用", "相對", "市場"]


# ─── Negative：英文字内 'or' 不該拆 ─────────────────────────────
def test_english_word_order_not_split():
    """`work order` 不該被拆成 `work` / `der` — 'or' 是英文字一部份。"""
    assert _parse_or_groups("work order") == ["work order"]


def test_english_word_for_not_split():
    """`for example` 不該因為含 'or' 被拆。"""
    assert _parse_or_groups("for example") == ["for example"]


def test_english_or_without_cjk_context():
    """純英文 `doctorate` 不該因 'or' 被拆（兩側都是 ASCII 字母）。"""
    assert _parse_or_groups("doctorate degree") == ["doctorate degree"]


# ─── Edge cases ───────────────────────────────────────────────────
def test_empty():
    assert _parse_or_groups("") == []


def test_whitespace_only():
    assert _parse_or_groups("   ") == []


def test_flatten_cjk_or():
    """`_flatten_keywords` 也要正確處理 CJK-adjacent OR。"""
    assert _flatten_keywords("濫用or相對") == ["濫用", "相對"]


def test_flatten_preserves_and_in_or_group():
    assert _flatten_keywords("A B | C D") == ["A", "B", "C", "D"]
