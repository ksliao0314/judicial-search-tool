"""把 cons.judicial.gov.tw MCP tool 的回傳 normalize 成 FJUD-shape，
讓既有的 task_search_hits / task_judgments / Reader UI 能直接用同一個資料結構。

映射表：
  cons 欄位          → task_judgments 欄位
  ─────────────────   ───────────────────
  case_id            → case_id (同名：「釋字第748號」/「111年憲判字第1號」)
  date (含中文)       → date (民國-月-日 格式、對齊 FJUD)
  issues             → facts (爭點視為「事實脈絡」)
  issue_summary (新制) → cause (案由 pill)
  main_text           → main_text (解釋文 = 結論)
  reasoning           → reasoning (理由書 = 推論；只在 include_reasoning=True 時有)
  related_statutes    → cited_statutes (直接對齊)
  petitioner (新制)   → parties = {"聲請人": [petitioner]}
  source_url          → source_url
  —                   → court = "憲法法庭" (固定)
"""
from __future__ import annotations

import re

# ─── case_id 格式偵測 ───────────────────────────────────────────────
# 律師慣用全稱「司法院釋字第N號」；cons MCP tool 只吃「釋字第N號」不吃 prefix。
# 我們 DB 存全稱（律師看得到），呼叫 MCP 前用 strip_cons_prefix 剝 prefix。
#
# 釋字第N號 / 司法院釋字第N號（含 N 和 第 號的 optional spacing / 省略）
_RE_OLD_INTERP = re.compile(r"^\s*(?:司法院\s*)?釋字\s*第?\s*\d+\s*號?\s*$")
# 111年憲判字第1號 / 憲法法庭 111 年憲判字第 1 號 / 111憲判1 等（暫不加 prefix）
_RE_NEW_INTERP = re.compile(r"^\s*(?:憲法法庭\s*)?\d+\s*年?\s*憲判(字)?\s*第?\s*\d+\s*號?\s*$")

_PREFIX_OLD = "司法院"


def is_old_interpretation(case_id: str) -> bool:
    """case_id 是否為舊制釋字（釋字第1號至813號）；"司法院" prefix 有無皆可。"""
    return bool(case_id and _RE_OLD_INTERP.match(case_id))


def add_display_prefix(case_id: str) -> str:
    """對舊制釋字加「司法院」prefix 作為 DB 存值（律師慣用全稱）。
    新制憲判字目前暫不加 prefix（上游 MCP 測試需要，前端也已習慣不加）。
    """
    if not case_id:
        return case_id
    s = case_id.strip()
    if _RE_OLD_INTERP.match(s) and not s.startswith(_PREFIX_OLD):
        return _PREFIX_OLD + s
    return s


def strip_cons_prefix(case_id: str) -> str:
    """對 cons case_id 剝掉顯示 prefix（送 MCP 前呼叫）。
    「司法院釋字第445號」→「釋字第445號」。
    非釋字格式一律原樣返回。
    """
    if not case_id:
        return case_id
    s = case_id.strip()
    if s.startswith(_PREFIX_OLD):
        rest = s[len(_PREFIX_OLD):].lstrip()
        if _RE_OLD_INTERP.match(rest):
            return rest
    return s


def is_interpretation_case_id(case_id: str) -> bool:
    """判斷 case_id 是否為釋字 / 憲判字（需要走 cons 路徑）。
    判決 case_id 格式如「臺北高等行政法院 107 年度全字第 69 號」— 不會命中。"""
    if not case_id:
        return False
    return bool(_RE_OLD_INTERP.match(case_id) or _RE_NEW_INTERP.match(case_id))


# ─── Date 正規化 ─────────────────────────────────────────────────────
# 舊制：「中華民國 52年05月22日」
# 新制：「111年05月13日」
# 目標：民國-月-日 如 "52-05-22"
_RE_DATE = re.compile(r"(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")


def normalize_cons_date(date_str: str) -> str:
    """釋字/憲判字日期字串 → 民國-月-日 格式（對齊 FJUD）。
    解析失敗回空字串。"""
    if not date_str:
        return ""
    m = _RE_DATE.search(date_str)
    if not m:
        return ""
    year, month, day = m.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def extract_year_from_cons_date(date_str: str) -> int | None:
    """從 cons date 取民國年度（用於 year_from/year_to client-side filter）。"""
    if not date_str:
        return None
    m = _RE_DATE.search(date_str)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, TypeError):
        return None


# ─── 舊制釋字理由書尾端抽大法官名單 ────────────────────────────────
#
# 舊釋字 reasoning 尾巴一律以下列其中一種 pattern 結尾：
#   「大法官會議主席　院　長　XXX\n大法官　AAA　BBB...」（標準）
#   「大法官會議主席　副院長　XXX\n大法官　AAA　BBB...」
#   「大法官會議　主　席　XXX\n大法官　AAA　BBB...」（主席 label 分字）
#   「大法官會議主席（代理院長）　大法官　XXX\n大法官　AAA　BBB...」
# 統計 813 筆，有 reasoning 的 734 筆全部命中（100% recall）。
#
# 新制憲判字 reasoning 不含名單（metadata 也沒有）、此切分只對舊制 apply。
_JUDGES_CUT_RE = re.compile(
    r'\n\s*大法官會議[\s\u3000]*主[\s\u3000]*席'
    r'(?:[\s\u3000]*（[^）]*）)?'
    r'[\s\u3000]*(?:院[\s\u3000]*長|副院長|大法官)?'
    r'[\s\u3000]*[\u4e00-\u9fff]'
)
# 名單區塊的 label 文字（抽名字前要移除）
_JUDGES_LABEL_RE = re.compile(
    r'大法官會議|主[\s\u3000]*席|院[\s\u3000]*長|副院長|大法官|（[^）]*）'
)


def split_old_interpretation_reasoning(reasoning: str) -> tuple[str, list[str]]:
    """舊釋字：把 reasoning 尾端的大法官名單切出來。

    回傳 (cleaned_reasoning, judges_list)：
    - cleaned_reasoning：不含名單的理由書主體
    - judges_list：大法官姓名（含主席），順序保留、去重

    若偵測不到名單、reasoning 原樣返回、judges_list 為空。
    """
    if not reasoning:
        return reasoning, []
    m = _JUDGES_CUT_RE.search(reasoning)
    if not m:
        return reasoning, []

    cleaned = reasoning[:m.start()].rstrip()
    judges_section = reasoning[m.start():]

    # 移除 label 文字（大法官、主席、院長、括號備註等），剩下的就是名字 + 分隔符
    stripped = _JUDGES_LABEL_RE.sub('', judges_section)

    # Split by 任意空白（含全形空格 U+3000、換行）
    raw = [p.strip() for p in re.split(r'[\s\u3000]+', stripped) if p.strip()]

    # Merge 連續單字：如「吳」+「庚」→「吳庚」（司法院排版常把 2 字名拆開填充空白對齊）
    merged: list[str] = []
    i = 0
    while i < len(raw):
        cur = raw[i]
        if len(cur) == 1 and i + 1 < len(raw) and len(raw[i + 1]) == 1:
            merged.append(cur + raw[i + 1])
            i += 2
        else:
            merged.append(cur)
            i += 1

    # Dedup 保序
    seen: set[str] = set()
    out: list[str] = []
    for n in merged:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return cleaned, out


# ─── Hit (search_interpretations 結果) → task_search_hits shape ──────
def normalize_cons_hit(hit: dict) -> dict:
    """search_interpretations 單筆結果 → task_search_hits row 格式。

    注意：search_interpretations 本身不回 date 欄位，要走 get_interpretation
    才拿得到。但 Stage 1 寫 task_search_hits 時**還沒抓全文**；此時只填 case_id /
    type 資訊，date 先空字串、year filter 由 Stage 2 client-side 做時用 case_id
    對照 cons 本機 JSON 再取 date（或 fallback 當下不做 year filter）。

    case_id 以「司法院釋字第N號」全稱存進 DB；jid 保留 cons API 格式（不含 prefix）
    供 Stage 2.5 呼叫 get_interpretation 時用。
    """
    raw = hit.get("case_id") or hit.get("title") or ""
    display = add_display_prefix(raw)
    # jid 與 case_id 都存 display 格式；task_search_hits.case_id 會優先取 jid
    # （既有邏輯），這樣 DB 裡看到的就是律師認得的全稱。Stage 2.5 呼叫 MCP 前
    # 用 strip_cons_prefix 剝 prefix，`is_interpretation_case_id` regex 容忍兩種格式。
    return {
        "jid": display,
        "case_id": display,
        "court": "憲法法庭",
        "date": "",                       # Stage 1 沒 date、交給 Stage 2.5 補
        "source_url": "",                 # 同上
        "cause": hit.get("issues", ""),   # 爭點當案由，Stage 2 案由 filter 用
    }


# ─── Full (get_interpretation 結果) → task_judgments shape ───────────
def normalize_cons_judgment(cons_data: dict) -> dict:
    """get_interpretation 回傳 → task_judgments row dict 格式。

    假設呼叫時帶了 include_reasoning=True、include_opinions=False。
    `opinions` 欄位即使有也不納入（意見書不精讀、不占 context）。
    """
    # DB 存顯示用全稱（「司法院釋字第445號」）；is_old_interpretation 辨認此格式
    raw_case_id = cons_data.get("case_id") or cons_data.get("case_number") or ""
    case_id = add_display_prefix(raw_case_id)
    date = normalize_cons_date(cons_data.get("date", ""))

    # 案由：新制 `issue_summary` 優先（較長、完整）；舊制 fallback `issues`
    cause = cons_data.get("issue_summary") or cons_data.get("issues") or ""

    # parties：新制有 `petitioner`（聲請人）；舊制無
    parties: dict = {}
    petitioner = cons_data.get("petitioner")
    if petitioner:
        parties["聲請人"] = [petitioner] if isinstance(petitioner, str) else list(petitioner)

    # related_statutes 是字串（含多條法規、換行分隔）
    # 保留原字串當 cited_statutes 清單的 single entry — 既有判決也允許字串 list
    # 真正從 full_text 抽 tuple 會在 extract_citations 那步做
    related = cons_data.get("related_statutes") or ""
    if related:
        cited_statutes = [line.strip() for line in related.split("\n") if line.strip()]
    else:
        cited_statutes = []

    # full_text：把 main_text + reasoning 合併作為「完整判決原文」
    # （既有 full_text 語意：MCP parser 分段不準時的穩健備援、用於 citation 抽取）
    main_text = cons_data.get("main_text") or ""
    reasoning = cons_data.get("reasoning") or ""

    # 舊制釋字：reasoning 尾巴有大法官名單、切出來填 judges 欄位（避免污染精讀 context）
    judges: list[str] | None = None
    if is_old_interpretation(case_id):
        reasoning, judges_list = split_old_interpretation_reasoning(reasoning)
        if judges_list:
            judges = judges_list

    # full_text 用「切過的 reasoning」拼（前端高亮、citation 抽取都不該包括名單）
    full_text = "\n\n".join(p for p in [main_text, reasoning] if p)

    return {
        "case_id": case_id,
        "court": "憲法法庭",
        "date": date,
        "source_url": cons_data.get("source_url", ""),
        "reasoning": reasoning,
        "main_text": main_text,
        "facts": cons_data.get("issues", ""),   # 爭點存 facts 供前端 Reader 顯示
        "cited_statutes": cited_statutes,
        "full_text": full_text,
        "judges": judges,                        # 舊釋字有名單、新憲判字 None
        "parties": parties if parties else None,
        "cause": cause,
    }
