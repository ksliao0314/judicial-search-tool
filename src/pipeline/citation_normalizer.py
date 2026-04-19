"""法條引用 normalizer：律師關鍵字 → 結構化 + 變體展開（供 search-time 多變體搜尋用）。

處理的變體空間（see docs memory/judgment_search_variants.md）：
  - 條號數字：阿拉伯 120 / 中文「一百二十」
  - 條之 N 寫法：第N條之M / N-M / N之M條 / 第N條之chi(M)
  - 項/款/目：第1項 / 第一項 / 1項
  - 法名：全名 ↔ 常見簡稱（透過 law_abbreviations.json）
  - 「第」前綴有無
  - 非正式縮寫：「勞基法179.1」= 勞基法第179條第1項

不處理（那是 CitationExtractor 的工作）：
  - 判決書的複合引用「第12、179條」「同法第179條」— 變體展開救不了這類，要做
    結構化抽取再比對。CitationNormalizer 只處理「律師的 keyword 長什麼樣」。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 中文數字 ↔ 阿拉伯數字
# ---------------------------------------------------------------------------

_CHI_DIGITS = {
    '〇': 0, '零': 0, '○': 0, '０': 0,
    '一': 1, '二': 2, '兩': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
}
_CHI_UNITS = {'十': 10, '百': 100, '千': 1000, '萬': 10000}

# match 純中文/全形數字序列（不含「第」「條」等）
_CHI_NUM_RE = re.compile(r'[〇零○０一二兩三四五六七八九十百千萬]+')


def chi_to_int(s: str) -> int | None:
    """把中文數字字串轉阿拉伯整數。無法解析回 None。

    支援：
      一百二十 → 120, 三百二十一 → 321, 十 → 10, 十二 → 12,
      一百 → 100, 三千 → 3000
    """
    if not s:
        return None
    s = s.strip()

    # 純阿拉伯（或混入） — 直接 try
    if s.isdigit():
        return int(s)

    # 全形數字 ０９
    if all('０' <= c <= '９' for c in s):
        return int(s.translate(str.maketrans('０１２３４５６７８９', '0123456789')))

    # 中文數字邏輯：標準分析
    total = 0
    current = 0  # 當前未結算的個位
    for ch in s:
        if ch in _CHI_DIGITS:
            current = _CHI_DIGITS[ch]
        elif ch in _CHI_UNITS:
            unit = _CHI_UNITS[ch]
            # 「十二」開頭 → current 是 0 但隱含 1
            if current == 0:
                current = 1
            total += current * unit
            current = 0
        else:
            return None  # 非法字元
    total += current
    return total if total > 0 or s in ('零', '〇', '○', '０') else None


def int_to_chi(n: int) -> str:
    """阿拉伯整數 → 中文數字字串（小寫，判決書慣用）。

    120 → 一百二十, 321 → 三百二十一, 12 → 十二, 100 → 一百,
    1001 → 一千〇一, 3000 → 三千
    """
    if n == 0:
        return '〇'
    if n < 0:
        return '-' + int_to_chi(-n)

    digits = '〇一二三四五六七八九'
    units = [(1000, '千'), (100, '百'), (10, '十')]

    parts: list[str] = []
    remaining = n
    prev_was_zero = False

    for unit_val, unit_char in units:
        d = remaining // unit_val
        remaining -= d * unit_val
        if d > 0:
            # 「十二」不寫「一十二」— 只在 n < 20 時省略前導一
            if d == 1 and unit_char == '十' and n < 20:
                parts.append(unit_char)
            else:
                parts.append(digits[d] + unit_char)
            prev_was_zero = False
        else:
            # 中間 0 需要標記一次（1001 → 一千〇一），但末尾 0 不標
            if parts and remaining > 0 and not prev_was_zero:
                parts.append('〇')
                prev_was_zero = True

    if remaining > 0:
        parts.append(digits[remaining])

    return ''.join(parts)


# ---------------------------------------------------------------------------
# Citation 結構
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Citation:
    """單一法條引用的結構化表示。"""
    law: str | None       # 法名（canonical 全名，normalize 後）；無法名時 None
    article: int          # 條（必填）
    sub: int | None = None       # 之 N
    paragraph: int | None = None  # 項
    item: int | None = None      # 款
    subitem: int | None = None   # 目

    def as_tuple(self) -> tuple:
        """供 tuple 比對（filter-time 用）。"""
        return (self.law, self.article, self.sub, self.paragraph, self.item, self.subitem)

    def covers(self, other: "Citation") -> bool:
        """
        比對「律師查的 Citation」是否應該命中 other（判決書中的一筆引用）。
        律師可能查得比較粗（只指定條），判決書寫得比較細（含項款），仍應命中。
        反之則不（判決書只有條、律師指定項，不命中因為不夠精確）。
        """
        # law: None 表示律師沒寫法名，視為不限法名（放寬）
        if self.law is not None and other.law != self.law:
            return False
        if other.article != self.article:
            return False
        # sub/paragraph/item/subitem：self 有值則必須相等；self 為 None 則不限
        for s, o in (
            (self.sub, other.sub),
            (self.paragraph, other.paragraph),
            (self.item, other.item),
            (self.subitem, other.subitem),
        ):
            if s is not None and s != o:
                return False
        return True


# ---------------------------------------------------------------------------
# 法名字典
# ---------------------------------------------------------------------------

# canonical 全名 → list of 常見簡稱 / 其他變體
_LAW_ABBREV_PATH = Path(__file__).resolve().parent.parent / "data" / "law_abbreviations.json"
_LAW_ABBREV: dict[str, list[str]] | None = None


def _load_law_abbrev() -> dict[str, list[str]]:
    global _LAW_ABBREV
    if _LAW_ABBREV is None:
        if _LAW_ABBREV_PATH.exists():
            with _LAW_ABBREV_PATH.open(encoding="utf-8") as f:
                _LAW_ABBREV = json.load(f)
        else:
            _LAW_ABBREV = {}
    return _LAW_ABBREV


def normalize_law_name(raw: str) -> str | None:
    """
    把任意法名輸入（可能是簡稱）轉為 canonical 全名。
    找不到對應回傳原字串（視為已是 canonical）。空字串回 None。
    """
    raw = raw.strip() if raw else ""
    if not raw:
        return None
    abbrev = _load_law_abbrev()
    # 正向：canonical 本身就匹配 → return
    if raw in abbrev:
        return raw
    # 反向：raw 是某個 canonical 的簡稱
    for canonical, variants in abbrev.items():
        if raw in variants:
            return canonical
    return raw  # 未知法名，照原樣當 canonical


def law_variants(canonical: str) -> list[str]:
    """取 canonical 法名對應的所有變體（含自己）。"""
    abbrev = _load_law_abbrev()
    variants = abbrev.get(canonical, [])
    # 去重，canonical 在前
    seen = {canonical}
    result = [canonical]
    for v in variants:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def is_law_in_dict(raw: str) -> bool:
    """檢查 raw 是否已收錄於法名字典（作為 canonical 或某 canonical 的 variant）。"""
    if not raw:
        return False
    abbrev = _load_law_abbrev()
    if raw in abbrev:
        return True
    for variants in abbrev.values():
        if raw in variants:
            return True
    return False


# Backwards-compat alias（保留舊名避免 API 層同步要改）
_is_law_in_dict = is_law_in_dict


def _persist_new_law(canonical: str, variants: list[str]) -> None:
    """把新 LLM 展開的法名對 append 到 law_abbreviations.json 並更新 in-memory dict。

    合併策略：若 canonical 已存在，聯集 variants；否則新增 entry。
    """
    abbrev = _load_law_abbrev()
    existing_variants = abbrev.get(canonical, [])
    merged = list(existing_variants)
    for v in variants:
        if v and v != canonical and v not in merged:
            merged.append(v)
    abbrev[canonical] = merged

    # 寫回 JSON（保持 _comment 欄位在最前）
    try:
        _LAW_ABBREV_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LAW_ABBREV_PATH.open("w", encoding="utf-8") as f:
            json.dump(abbrev, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("寫入 law_abbreviations.json 失敗（僅 in-memory 生效）：%s", e)


# ---------------------------------------------------------------------------
# LLM fallback：字典漏的法名首次用到時，用 Claude 展開並持久化
# ---------------------------------------------------------------------------

_LAW_EXPANSION_SYSTEM = """\
你是台灣法律術語助理。使用者輸入一個「法律 / 條例 / 規則 / 辦法 / 細則」名稱。
請判斷它的正式全稱與所有常見簡稱。

**嚴格規則**：
1. 只處理台灣現行有效法律。若不確定該法真實存在，回傳 `{"canonical": "<輸入>", "variants": []}` 即可（不要臆測）。
2. `canonical` 一律為正式全稱，通常以「法 / 條例 / 規則 / 辦法 / 細則」結尾。
3. `variants` 不可包含 canonical 自己，且必須是實際律師或判決書會用的簡寫；你不 100% 確定的簡稱不要列。
4. 不要展開法律意義不同的其他法。
5. 只輸出 JSON，不加任何其他文字。

範例：
輸入「衛廣法」→ {"canonical": "衛星廣播電視法", "variants": ["衛廣法"]}
輸入「勞基法」→ {"canonical": "勞動基準法", "variants": ["勞基法"]}
輸入「民法」→ {"canonical": "民法", "variants": []}
輸入「某不存在法」→ {"canonical": "某不存在法", "variants": []}
"""


async def _expand_law_with_llm(raw: str, api_key: str | None = None) -> dict | None:
    """Call Claude to expand an unknown law name. Returns {canonical, variants} or None on error."""
    import anthropic
    from src.utils.json_parse import extract_json

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            system=[{
                "type": "text",
                "text": _LAW_EXPANSION_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": f"輸入「{raw}」"}],
        )
        data = extract_json(resp.content[0].text)
        if not isinstance(data, dict):
            return None
        canonical = (data.get("canonical") or "").strip()
        variants = [str(v).strip() for v in (data.get("variants") or []) if str(v).strip()]
        if not canonical:
            return None
        return {"canonical": canonical, "variants": variants}
    except Exception as exc:
        logger.warning("_expand_law_with_llm(%r) 失敗：%s", raw, exc)
        return None


async def ensure_law_known(keyword: str, api_key: str | None = None) -> None:
    """若 keyword 的法名不在字典，call Claude 展開並永久化。

    呼叫端應在 parse_keyword / 條號展開 前先 await 此函式。若 keyword 不像
    條號引用（parse_keyword 返回 None），本函式不做任何事。
    """
    if not keyword:
        return
    c = parse_keyword(keyword)
    if c is None or c.law is None:
        return
    if is_law_in_dict(c.law):
        return

    logger.info("未知法名 %r，call Claude 展開", c.law)
    result = await _expand_law_with_llm(c.law, api_key=api_key)
    if not result:
        return
    canonical = result["canonical"]
    variants = result["variants"]
    # 至少要有一個有用的簡稱，且 canonical != raw（否則 Claude 沒真的擴展）
    if not variants and canonical == c.law:
        # 沒任何額外資訊 — 仍寫入空 entry 標記「已查過」避免之後重複 call
        _persist_new_law(canonical, [])
        return
    _persist_new_law(canonical, variants)
    logger.info("新法名入字典：%s → %s", canonical, variants)


# ---------------------------------------------------------------------------
# Parser：律師 keyword → Citation
# ---------------------------------------------------------------------------

# 條號 / 之N / 項 / 款 / 目 的綜合 regex
# 支援的 keyword 格式（律師輸入）：
#   民法第179條   民法第一百七十九條   民法179條   民法第179-1條
#   民法第179條之1 民法第179條之一
#   民法第179條第1項  民法179條1項  民法第179條第一項
#   民法第179條第1項第2款
#   勞基法179.1  勞基法179-1（歧義：可能是 179-1 或 179 第1項，這裡先當 179 之 1）
#
# 也支援無法名（律師只打條號）：
#   第179條  第179-1條  第179條第1項
_NUM_PART = r'(?:\d+|[〇零○０一二兩三四五六七八九十百千萬]+)'
# 法名：1-9 個中文字 + 「法」或「條例」或「規則」或「辦法」或「細則」結尾
# 例：民法、刑法、公司法、勞動基準法、勞基法、公寓大廈管理條例、道路交通安全規則
# 注意不能寫成「{2,10}中文 + 後綴」— 那樣「民法」會被 regex 拒絕（「民」+「法」+ 還要找一個「法」）
_LAW_PART = r'(?:[\u4e00-\u9fff]{1,9}(?:法|條例|規則|辦法|細則))'
_KEYWORD_CITATION_RE = re.compile(
    r'(?P<law>' + _LAW_PART + r')?'                                 # 法名
    r'\s*'
    r'(?:第\s*)?'
    r'(?P<article>' + _NUM_PART + r')'
    r'\s*'
    r'條?'
    r'\s*'
    r'(?:'                                                          # 之 N / -N
        r'(?:之\s*(?P<sub_zhi>' + _NUM_PART + r'))'
        r'|'
        r'(?:-\s*(?P<sub_dash>\d+))'
        r'|'
        r'(?:\.\s*(?P<par_dot>\d+))'                                # 179.1 形式 → 當項
    r')?'
    r'\s*'
    r'(?:第?\s*(?P<paragraph>' + _NUM_PART + r')\s*項)?'
    r'\s*'
    r'(?:第?\s*(?P<item>' + _NUM_PART + r')\s*款)?'
    r'\s*'
    r'(?:第?\s*(?P<subitem>' + _NUM_PART + r')\s*目)?'
)


def parse_keyword(kw: str) -> Citation | None:
    """把律師關鍵字 parse 成 Citation。失敗回 None。

    若 keyword 不像法條引用（沒有條號），直接 return None — 呼叫端可 fallback 走同義詞路徑。
    """
    if not kw:
        return None
    m = _KEYWORD_CITATION_RE.fullmatch(kw.strip())
    if not m:
        return None

    article_raw = m.group("article")
    if not article_raw:
        return None
    article_num = chi_to_int(article_raw)
    if article_num is None:
        return None

    # 之 N：三種來源（之X、-X、.X 其中 .X 另作項處理 below）
    sub = None
    if m.group("sub_zhi"):
        sub = chi_to_int(m.group("sub_zhi"))
    elif m.group("sub_dash"):
        sub = int(m.group("sub_dash"))

    # 項：正式項標記 OR 「179.1」這種 dot 形式
    paragraph = None
    if m.group("paragraph"):
        paragraph = chi_to_int(m.group("paragraph"))
    elif m.group("par_dot"):
        paragraph = int(m.group("par_dot"))

    item = chi_to_int(m.group("item")) if m.group("item") else None
    subitem = chi_to_int(m.group("subitem")) if m.group("subitem") else None

    law_raw = m.group("law")
    law = normalize_law_name(law_raw) if law_raw else None

    return Citation(law=law, article=article_num, sub=sub, paragraph=paragraph,
                    item=item, subitem=subitem)


# ---------------------------------------------------------------------------
# Variant generation：Citation → 判決書可能出現的各種字面寫法
# ---------------------------------------------------------------------------

def generate_article_variants(article: int, sub: int | None) -> list[str]:
    """產生「第 N 條 [之 M]」的所有常見寫法。"""
    chi_a = int_to_chi(article)
    variants: list[str] = []

    if sub is None:
        # 純條號
        for a in [str(article), chi_a]:
            variants.append(f"第{a}條")
            variants.append(f"{a}條")
    else:
        chi_s = int_to_chi(sub)
        for a, s in [(str(article), str(sub)), (chi_a, chi_s),
                     (str(article), chi_s), (chi_a, str(sub))]:
            variants.append(f"第{a}條之{s}")
            variants.append(f"{a}條之{s}")
            variants.append(f"{a}之{s}條")
        # -N 形式（只用阿拉伯）
        variants.append(f"第{article}-{sub}條")
        variants.append(f"{article}-{sub}")
    # 去重保序
    seen = set()
    result = []
    for v in variants:
        if v not in seen:
            seen.add(v); result.append(v)
    return result


def generate_paragraph_suffix(paragraph: int | None, item: int | None, subitem: int | None) -> list[str]:
    """項/款/目的字面變體（可能空）。
    回傳後綴列表，每個都是「第X項」「X項」「第X項第Y款」之類的組合。
    """
    if paragraph is None and item is None and subitem is None:
        return [""]  # 沒有就產生空後綴，主條號獨立

    parts_variants = [[""]]  # 每層可選：不寫、寫

    def _num_forms(n: int) -> list[str]:
        return [str(n), int_to_chi(n)]

    if paragraph is not None:
        forms = []
        for n in _num_forms(paragraph):
            forms += [f"第{n}項", f"{n}項"]
        parts_variants.append(forms)

    if item is not None:
        forms = []
        for n in _num_forms(item):
            forms += [f"第{n}款", f"{n}款"]
        parts_variants.append(forms)

    if subitem is not None:
        forms = []
        for n in _num_forms(subitem):
            forms += [f"第{n}目", f"{n}目"]
        parts_variants.append(forms)

    # 笛卡兒積
    result: list[str] = [""]
    for layer in parts_variants[1:]:
        result = [prev + cur for prev in result for cur in layer]
    # 濾掉空字串（至少要有一個層級）
    result = [s for s in result if s]
    # 加回純條號（無項款目）的空後綴也是合法命中
    result.append("")
    # 去重保序
    seen = set()
    final = []
    for v in result:
        if v not in seen:
            seen.add(v); final.append(v)
    return final


def generate_variants(c: Citation, with_law_prefix: bool = True) -> list[str]:
    """給一個 Citation，產生判決書中可能出現的所有字面寫法。

    with_law_prefix=True：若 c.law 非 None，每個變體前面串上所有法名變體。
                          若 c.law 為 None，產生不含法名的變體（fallback）。
    """
    article_parts = generate_article_variants(c.article, c.sub)
    suffix_parts = generate_paragraph_suffix(c.paragraph, c.item, c.subitem)

    laws: list[str]
    if with_law_prefix and c.law:
        laws = law_variants(c.law)
    else:
        laws = [""]

    results: list[str] = []
    for law in laws:
        for a in article_parts:
            for s in suffix_parts:
                results.append(f"{law}{a}{s}")
    # 去重保序
    seen = set()
    final = []
    for v in results:
        if v not in seen:
            seen.add(v); final.append(v)
    return final


def top_search_variants(c: Citation, limit: int = 5) -> list[str]:
    """選出最該送給 MCP search 的 top N 變體（降低 rate-limit 壓力）。

    優先順序：正式全名 + 第N條 > 簡稱 + 第N條 > 純中文 > 其他
    """
    all_variants = generate_variants(c, with_law_prefix=True)
    # 啟發式排序：
    #   1) 含法名全名（= canonical law_variants[0]） 優先
    #   2) 含「第」前綴 優先
    #   3) 阿拉伯數字 優先（判決書多數這樣寫）
    canonical_law = c.law or ""

    def score(v: str) -> tuple:
        has_full_law = canonical_law and v.startswith(canonical_law)
        has_first_char = "第" in v
        has_digit = any(ch.isdigit() for ch in v)
        return (
            0 if has_full_law else 1,
            0 if has_first_char else 1,
            0 if has_digit else 1,
            len(v),  # 短的優先（寫法越簡潔越常見）
        )

    sorted_variants = sorted(all_variants, key=score)
    return sorted_variants[:limit]
