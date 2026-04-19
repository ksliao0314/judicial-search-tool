"""判決文本 → 結構化法條引用列表（for filter-time 比對）。

為什麼需要這個模組：
  律師搜「民法第179條」，但判決書可能寫「民法第12條及第179條」、「民法第12、179條」、
  「民法第12條…（中略）…同法第179條」。靠 keyword 變體展開（CitationNormalizer 做的事）
  救不了這類複合引用 — 必須把判決書的引用解析成結構化 tuple 再做精確比對。

處理的複合情境：
  - `民法第12、179條`          → [(民法,12), (民法,179)]
  - `第12、179條`              → 繼承上次法名 context → [(民法,12), (民法,179)]
  - `民法第12條及第179條`       → [(民法,12), (民法,179)]
  - `民法第12條、第179條`       → 同上
  - `民法第12至15條`            → [(民法,12),(民法,13),(民法,14),(民法,15)]（範圍展開）
  - `同法第179條`              → 用 last_law context
  - `民法第179條第1項第2款`     → 攜帶項款資訊
  - 多法切換：「民法...刑法...同法」→ 同法指 context 中最近的法

不處理（目前）：
  - `本法` 回指（需結合案件類型判斷）
  - `同條第3項` 回指條號
  - 跨段落的 context（每段重置 context？目前選擇不重置，以提高 recall）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.pipeline.citation_normalizer import Citation, chi_to_int, normalize_law_name


# ---------------------------------------------------------------------------
# 法名識別（judgment 內）：canonical + 所有簡稱都要認
# ---------------------------------------------------------------------------

_LAW_ABBREV_PATH = Path(__file__).resolve().parent.parent / "data" / "law_abbreviations.json"

# 建立「所有可能在判決中出現的法名」pattern，按長度降序以保證 greedy 匹配長的優先
# 例：判決書寫「勞動基準法」時應 match 「勞動基準法」而非「勞動」「基準法」
_LAW_PATTERN: re.Pattern | None = None


def _get_law_pattern() -> re.Pattern:
    global _LAW_PATTERN
    if _LAW_PATTERN is None:
        if _LAW_ABBREV_PATH.exists():
            with _LAW_ABBREV_PATH.open(encoding="utf-8") as f:
                abbrev = json.load(f)
        else:
            abbrev = {}

        all_names: set[str] = set()
        for canonical, variants in abbrev.items():
            if canonical.startswith("_"):
                continue
            all_names.add(canonical)
            all_names.update(variants)

        # 追加「同法」「本法」等 back-reference 關鍵字（不當法名，但要在 sweep 中標記）
        # 這些在 extractor 用另一個 pattern 處理，這裡只準備真實法名。

        # 按長度降序，避免「民」吃掉「民法」
        sorted_names = sorted(all_names, key=len, reverse=True)
        if not sorted_names:
            # 至少要有個不會 match 的 sentinel，避免空 pattern
            sorted_names = ["___NO_LAW___"]
        _LAW_PATTERN = re.compile("(" + "|".join(re.escape(n) for n in sorted_names) + ")")
    return _LAW_PATTERN


# ---------------------------------------------------------------------------
# 條號 group 識別
# ---------------------------------------------------------------------------

# 「第N條」或「第N-M條」或「第N條之M」或「第N、M、K條」或「第N至M條」
# 後接可選的 項/款/目 組。
_NUM = r'(?:\d+|[〇零○０一二兩三四五六七八九十百千萬]+)'

# 一個條號 item 的 tail（三種寫法）：
#   condition + optional 之N： "條之1"、"條"
#   之N + optional 條：        "之1條"、"之1"
#   -N + optional 條：          "-1條"、"-1"
# 用 + 連接避免 implicit-concat 與 + 混用的 SyntaxError
_ARTICLE_TAIL = (
    r'(?:'
    + r'條(?:\s*之\s*' + _NUM + r')?'
    + r'|'
    + r'之\s*' + _NUM + r'\s*條?'
    + r'|'
    + r'-\s*\d+\s*條?'
    + r')'
)

# 條號 span：一或多個條號，由頓號/連詞/範圍連接
# 首個條號可不帶 tail（共享後面的「條」），但整個 span 必須含至少一個「條」
# （由 extract_citations 的 filter 保證，避免誤抓無 tail 的純數字序列如金額）
_CITATION_SPAN_RE = re.compile(
    r'(?P<span>'
    + r'(?:第\s*)?' + _NUM + r'(?:\s*' + _ARTICLE_TAIL + r')?'              # 首個，tail optional
    + r'(?:'
    + r'\s*(?:[、,,]|及|與|或|和|暨|至|到|~|~)\s*'                          # 連詞（含範圍 至/到）
    + r'(?:第\s*)?' + _NUM + r'(?:\s*' + _ARTICLE_TAIL + r')?'
    + r')*'
    + r')'
    + r'(?P<suffix>'
    + r'(?:\s*第?\s*' + _NUM + r'\s*項)?'
    + r'(?:\s*第?\s*' + _NUM + r'\s*款)?'
    + r'(?:\s*第?\s*' + _NUM + r'\s*目)?'
    + r')'
)

# 單一 article atom 用於 scan span：支援三種 tail 寫法
_ATOMIC_ARTICLE_RE = re.compile(
    r'(?:第\s*)?'
    r'(?P<num>' + _NUM + r')'
    r'\s*'
    r'(?:'
        r'條(?:\s*之\s*(?P<s1>' + _NUM + r'))?'                             # 條 | 條之N
        r'|'
        r'之\s*(?P<s2>' + _NUM + r')\s*條?'                                  # 之N | 之N條
        r'|'
        r'-\s*(?P<sd>\d+)\s*條?'                                             # -N | -N條
    r')?'
)

# Range pattern：12 至 15
_RANGE_RE = re.compile(
    r'(?P<a>' + _NUM + r')\s*(?:至|到|~|～)\s*(?P<b>' + _NUM + r')'
)

# 項/款/目從 suffix 取
_PAR_RE = re.compile(r'第?\s*(' + _NUM + r')\s*項')
_ITEM_RE = re.compile(r'第?\s*(' + _NUM + r')\s*款')
_SUBITEM_RE = re.compile(r'第?\s*(' + _NUM + r')\s*目')


# ---------------------------------------------------------------------------
# 主要 extractor
# ---------------------------------------------------------------------------

def extract_citations(text: str) -> list[Citation]:
    """掃過全文，回傳所有引用的 Citation list（去重保序）。

    演算法（單 pass）：
      逐字 sweep，同時追蹤：
        - 最近的法名 context（被 "同法" 回指時使用）
        - 是否遇到 "同法" 關鍵字
      遇到 citation block 時：
        1. 決定這個 block 的法名（block 前的附近法名 > 同法 context > None）
        2. parse article_list，產生多個 Citation（頓號分、範圍展開）
        3. 每個 Citation attach 項/款/目 suffix
    """
    if not text:
        return []

    # 找出所有 "法名 位置" 與 "同法/同條例 位置"
    law_pattern = _get_law_pattern()
    law_positions: list[tuple[int, int, str]] = []   # (start, end, canonical_law)
    for m in law_pattern.finditer(text):
        raw = m.group(1)
        canonical = normalize_law_name(raw)
        if canonical:
            law_positions.append((m.start(), m.end(), canonical))

    # "同法" "同條例" "同規則" "同辦法" 的回指標記
    same_law_re = re.compile(r'同(?:法|條例|規則|辦法|細則)')
    same_law_positions: list[tuple[int, int]] = [(m.start(), m.end()) for m in same_law_re.finditer(text)]

    def find_law_before(pos: int) -> str | None:
        """找距離 pos 最近（且在 pos 之前）的法名 / 同法 context。"""
        candidates = []
        # 真實法名（距離近的優先）
        for start, end, law in law_positions:
            if end <= pos:
                candidates.append((pos - end, law))
        # 同法 → 用 same_law_positions 找到後再往前找真實法名
        for start, end in same_law_positions:
            if end <= pos:
                # 在此 "同法" 位置之前找最近的真實法名
                last_real = None
                for ls, le, law in law_positions:
                    if le <= start:
                        last_real = law
                if last_real:
                    candidates.append((pos - end, last_real))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    results: list[Citation] = []
    seen_tuples: set[tuple] = set()

    for m in _CITATION_SPAN_RE.finditer(text):
        span_start = m.start()
        span_raw = m.group("span")
        suffix_raw = m.group("suffix") or ""

        # span 必須含至少一個「條」字，否則是誤抓（純數字序列）
        if "條" not in span_raw:
            continue

        # 取 span 前最近的法名 context
        law = find_law_before(span_start)

        # parse suffix 的項 / 款 / 目（整個 span 末尾）
        par_m = _PAR_RE.search(suffix_raw)
        item_m = _ITEM_RE.search(suffix_raw)
        sub_m = _SUBITEM_RE.search(suffix_raw)
        paragraph = chi_to_int(par_m.group(1)) if par_m else None
        item = chi_to_int(item_m.group(1)) if item_m else None
        subitem = chi_to_int(sub_m.group(1)) if sub_m else None

        # Span 中擷取所有 atomic article（含範圍展開）
        articles = _extract_articles_from_span(span_raw)

        # Suffix（項/款/目）只套給最後一個 article；前面的視為獨立條號不繼承。
        # 例：「民法第184條第1項前段、第185條」→ 184 有 par=1，185 沒有
        # 這是台灣判決書標準寫法，suffix 附著在「最近的條」上
        for i, (a_num, a_sub) in enumerate(articles):
            is_last = (i == len(articles) - 1)
            cit = Citation(
                law=law,
                article=a_num,
                sub=a_sub,
                paragraph=paragraph if is_last else None,
                item=item if is_last else None,
                subitem=subitem if is_last else None,
            )
            t = cit.as_tuple()
            if t not in seen_tuples:
                seen_tuples.add(t)
                results.append(cit)

    return results


def _extract_articles_from_span(span: str) -> list[tuple[int, int | None]]:
    """掃過一個條號 span，抽出所有 (article, sub) 對，含範圍展開。"""
    if not span:
        return []

    # 先處理範圍：將「12至15」轉為一個事後標記，避免 _ATOMIC_ARTICLE_RE 只抓到 12 和 15
    # 作法：先把 span 用 range 切段：[before] + range + [after]，把 range 展開成「12、13、14、15」

    range_m = _RANGE_RE.search(span)
    if range_m:
        start_num = chi_to_int(range_m.group("a"))
        end_num = chi_to_int(range_m.group("b"))
        if start_num is not None and end_num is not None and 0 < end_num - start_num <= 50:
            expanded = "、".join(str(n) for n in range(start_num, end_num + 1))
            span = span[:range_m.start()] + expanded + span[range_m.end():]

    # 掃過 span 抓每個 atomic
    out: list[tuple[int, int | None]] = []
    for m in _ATOMIC_ARTICLE_RE.finditer(span):
        num_raw = m.group("num")
        if not num_raw:
            continue
        num = chi_to_int(num_raw)
        if num is None:
            continue
        sub: int | None = None
        if m.group("s1"):
            sub = chi_to_int(m.group("s1"))
        elif m.group("s2"):
            sub = chi_to_int(m.group("s2"))
        elif m.group("sd"):
            sub = int(m.group("sd"))
        out.append((num, sub))
    return out
