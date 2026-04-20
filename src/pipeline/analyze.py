"""Claude API 批次精讀：對 task_judgments 指定欄位執行 AI 分析，寫入 analysis_results。

CONCURRENCY 並行（asyncio.Semaphore，預設 8）+ per-model token bucket 控流。
Scoring 走 Haiku bucket、synthesis / quick_followup 走 Sonnet bucket。
Claude API 失敗最多重試 3 次（MAX_CLAUDE_RETRIES），仍失敗記 error，不阻塞。
使用 prompt caching 降低重複 system prompt token 費用。
"""
import asyncio
import json
import logging
import re
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import anthropic

from src.db import database as db
from src.pipeline.cons_normalizer import is_old_interpretation
from src.utils.json_parse import extract_json
from src.utils.rate_limiter import TokenBucket, estimate_prompt_tokens

# 舊制釋字結構解析 — 只用於 _get_field_text 的 reasoning 過濾路徑；對新制憲判字 / 一般
# 判決完全不動。Parser 自 mcp fork import（shared library 模式、避免重複維護）。
# Path manipulation：iCloud UF_HIDDEN 讓 editable .pth 被 skip（見 CLAUDE.md tech gotchas），
# 改用手動 sys.path 補救、不依賴 editable install 的 finder。
import sys as _sys
from pathlib import Path as _Path
_MCP_FORK = _Path(__file__).resolve().parents[2] / "mcp-taiwan-legal-db"
if _MCP_FORK.exists() and str(_MCP_FORK) not in _sys.path:
    _sys.path.insert(0, str(_MCP_FORK))
try:
    from mcp_server.parsers.interpretation_parser import parse_interpretation
except ImportError:  # 極罕見：fork 未安裝時讓 analyze 仍能跑（reasoning 走 fallback）
    parse_interpretation = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 模型選擇：per-judgment 評分用 Haiku（快、便宜、Tier 1 ITPM 高）；
# synthesis 總結用 Sonnet（只跑 1 次，品質要求高）。
MODEL_SCORING = "claude-haiku-4-5-20251001"   # per-judgment 0-10 評分
MODEL_SYNTHESIS = "claude-sonnet-4-6"          # synthesis 總結

# Token buckets — per-model，因為 Anthropic 後端 rate limit 是 model 獨立的，
# 共用同一個 bucket 會讓 Sonnet 的 quota 被 Haiku 吃掉、或反之。
#
# Haiku Tier 1: ITPM 50K、RPM 50 → -20% safety margin
# Sonnet Tier 1: ITPM 30K、RPM 50 → -20% safety margin
# 升 tier 或換 model 時調這幾個常數即可。
HAIKU_ITPM_LIMIT = 40_000
HAIKU_RPM_LIMIT = 40
SONNET_ITPM_LIMIT = 24_000
SONNET_RPM_LIMIT = 40

# 保留 ITPM_LIMIT / RPM_LIMIT 別名（Haiku），供外部 import（如 UI cost estimation）使用
ITPM_LIMIT = HAIKU_ITPM_LIMIT
RPM_LIMIT = HAIKU_RPM_LIMIT

CONCURRENCY = 8
MAX_CLAUDE_RETRIES = 3

# Scoring 走 Haiku
_haiku_itpm_bucket = TokenBucket(rate_per_minute=HAIKU_ITPM_LIMIT)
_haiku_rpm_bucket = TokenBucket(rate_per_minute=HAIKU_RPM_LIMIT)
# Synthesis + quick_followup 走 Sonnet
_sonnet_itpm_bucket = TokenBucket(rate_per_minute=SONNET_ITPM_LIMIT)
_sonnet_rpm_bucket = TokenBucket(rate_per_minute=SONNET_RPM_LIMIT)


FIELD_LABELS: dict[str, str] = {
    "reasoning": "理由",
    "main_text": "主文",
    "facts": "事實",
    "cited_statutes": "引用法條",
    "full_text": "全文",
}

SYSTEM_PROMPT = (
    "你是一位台灣法律研究助理，專門協助律師分析法院判決。"
    "回答時請嚴格遵照指示格式，只輸出 JSON，不加任何其他文字。"
)

# variant_candidates 發現規則（v1 / v2 共用的 variant 說明段）
_VARIANT_RULES = """
關於 `variant_candidates`：若判決中使用了「{keyword}」的**異體字、錯字、簡寫**，列出最多 3 個。**嚴格要求**：

✓ **可以列**（同一法律概念、純粹寫法差異）：
  - 異體字：僱 ↔ 雇、傭 ↔ 佣 ↔ 庸、蹟 ↔ 績、複 ↔ 覆
  - 簡寫：勞動基準法 ↔ 勞基法、建築物 ↔ 建物
  - 錯字：古蹟 ↔ 古績、重複 ↔ 重覆

✗ **絕對不要列**（這是最常見的錯誤）：
  - 相關概念不同詞：「僱傭」的 variant **不是**「雇主」「受僱人」「僱傭契約」「勞務契約」（這些是相關概念但不等於僱傭）
  - 上位下位：「契約」的 variant **不是**「買賣契約」「租賃契約」
  - 近義非同義：「撤銷」的 variant **不是**「廢止」「失效」

測試：如果你列的詞跟「{keyword}」替換後，**句子意思會變**，那就不是 variant，不要列。

若沒發現就填 []。"""

# v1 尾段（legacy prompt 用 — 保留原格式）
_V1_SUFFIX = _VARIANT_RULES + """

{field_label}內容如下：
{field_text}"""

# v2 尾段（結構化 prompt 用 — 判決以【】section headers 呈現）
_V2_SUFFIX = _VARIANT_RULES + """

判決內容如下（各段以【】標示段落類型）：
{field_text}"""


# v1 prompt（legacy，NewAnalysisWork 用）— 保留原 match/score/reason 三件組
ANALYSIS_PROMPT = ("""\
以下是一份法院判決的{field_label}段落。
請判斷這份判決的{field_label}，是否符合以下條件：

{question}

請只回傳 JSON，不要任何其他文字：
{{
  "match": "yes" | "no" | "partial",
  "score": 1到10的整數,
  "excerpt": "命中的關鍵段落，限200字以內，若不符合則留空字串",
  "reason": "你的判斷理由，限80字以內",
  "variant_candidates": []
}}""" + _V1_SUFFIX)


# Interpretation mode prompt：釋字 / 憲判字（大法官解釋），不評 direction。
# 解釋是抽象法律意見、沒有「支持/反對」的立場分布概念（不像判決有當事人兩造）。
# 給律師：只要 score（關聯性）+ position（解釋立場/重點）+ excerpt（原文摘錄）。
ANALYSIS_PROMPT_CONS = ("""\
以下是一份大法官解釋 / 憲法法庭判決的結構化內容。
律師研究問題：
{question}

請閱讀各段落（以【】標示段落類型），回答：
此解釋 / 判決對研究問題的**關聯性**（score 0-10）。

**各段落的角色**：
- 【理由】大法官的法律分析、憲法詮釋與論證
- 【爭點】本案處理的憲法爭議或聲請爭點
- 【主文】解釋文 / 判決結論（宣告合憲/違憲/合憲限縮等）
- 【引用法條】本案援引的法律條文清單

**score 評分標準（0-10）**：
  0 = 完全無關
  1-3 = 邊緣提及
  4-6 = 有處理但非核心
  7-10 = 核心議題，直接解釋律師問題

**不要評 direction**：憲法解釋是抽象規範意見、無「兩造立場」概念。

請只回傳 JSON，不要任何其他文字：
{{
  "score": 0到10的整數,
  "position": "大法官的核心論點或宣告意旨，60字內；若 score=0 留空字串",
  "excerpt": "命中的核心段落原文（必須從原文逐字複製，不得改寫或摘要），200字內；若 score=0 留空字串",
  "found_in": "excerpt 所在的段落類型：reasoning / facts / main_text / cited_statutes",
  "variant_candidates": []
}}""" + _V2_SUFFIX)


# v2 prompt：結構化段落 + score + direction（立場方向）
ANALYSIS_PROMPT_V2 = ("""\
以下是一份法院判決的結構化內容。
律師研究問題：
{question}

請閱讀各段落（以【】標示段落類型），回答兩個問題：
1. 此判決對研究問題的**論述詳細度**（score 0-10）
2. 法院的**立場方向**是否與律師的問題方向一致（direction）

**各段落的角色**：
- 【理由】法院的法律分析、法條解釋與認定
- 【事實】犯罪事實、案件背景、當事人主張
- 【主文】判決結果（刑期、罰金、撤銷、駁回等）
- 【引用法條】本案引用的法條清單

**根據律師問題判斷哪些段落重要**：
- 法律見解、法條解釋類問題 → 以【理由】為主，事實僅供脈絡
- 量刑、刑度類問題 → 【主文】（刑期）＋【事實】（犯罪類型）＋【理由】（量刑理由）都重要
- 情境、事實類問題（「有沒有XX情形」）→ 【事實】為主＋【理由】中的事實認定
- 結果類問題（「是否免罰」「有無撤銷」）→ 【主文】＋【理由】

**score 評分標準（0-10）**：
  0 = 完全沒論述或無關
  1-3 = 邊緣提及
  4-6 = 有處理但非核心爭點
  7-10 = 核心爭點，有詳細論述與認定

**direction 判斷規則**：
  分析律師問題是否隱含特定立場或方向。
  - 若問題問「法院認定 X 可以/成立/適用的理由」→ 律師想找支持 X 的判決
    · 法院認定 X 成立/適用 → "支持"
    · 法院認定 X 不成立/不適用 → "反對"
  - 若問題是中性探索（「法院如何認定 X」「X 的判斷標準」）→ 不分方向
    · 填 "中性"
  - 若無法判斷 → "中性"

**excerpt 選取規則（分層優先、絕對禁止必須遵守）**：

**第一優先**：從**法院自己的判斷/論理段落**挑（「本院認為/本院查/本院見解/本院認定/本院判斷/本院核閱」等由法院說話的段落）。

**Fallback**（只在第一優先找不到時）：若判決結構不清、沒明確「本院…」標頭（如簡易裁定、結構混雜的老判決、極短裁判書）、可退一步取 reasoning 中「最能支撐 position 的段落」— 但仍需同時滿足下方**絕對禁止**。

**絕對禁止來源**（三層規則都必須遵守、即使該段文字看起來很命中）：
  - 【主文】— 只是判決結果、律師已知、無新訊息
  - **任何程序當事人的主張 / 抗辯 / 答辯 / 陳述 / 聲明 / 略以 / 指稱 / 訴稱**，
    包含但不限於：原告、被告、上訴人、被上訴人、抗告人、聲請人、相對人、
    參加人、再審原告、再審被告、自訴人、告訴人、反訴原告/被告、選定當事人等
  - 證人證述、鑑定報告引文 — 證據不是法院的判斷
  - 法院**複述**當事人論點的句子（即使所在段落以「本院查」開頭、若內容在重述當事人、仍不算）

**「按…規定」「經查…」開頭詞不可當判準** — 當事人陳述也常這樣寫。
判準是「這段話**所在的 section 是不是法院在說話、且法院在表達自己的見解**」。

**「固非無見」特別 pattern**（律師研究極重要）：
若判決中出現「原審…固非無見」「上訴意旨固非無據」「被告所辯尚非無據」等句式、
這是**法院承認對方有理但即將反駁**的信號。excerpt **必須取此句之後**的法院反駁段落
（通常以「然」「然則」「然查」「惟」「但」銜接）— 上級審推翻下級審、或法院反駁
當事人論點的核心推理、律師最在意的內容。

若連 reasoning 裡都找不到任何非主文、非當事人主張的段落（極罕見）、excerpt 留空字串。

請只回傳 JSON，不要任何其他文字：
{{
  "score": 0到10的整數,
  "direction": "支持" | "反對" | "中性",
  "position": "法院的立場/認定，60字內；若 score=0 留空字串",
  "excerpt": "依上述分層優先選取的段落原文（必須從判決原文逐字複製，不得改寫或摘要），200字內；若 score=0 或真的找不到任何可用段落才留空字串",
  "found_in": "excerpt 所在的段落類型：reasoning / facts / main_text / cited_statutes",
  "variant_candidates": []
}}""" + _V2_SUFFIX)


_default_client: anthropic.AsyncAnthropic | None = None


def _get_client(api_key: str | None = None) -> anthropic.AsyncAnthropic:
    """回傳 Anthropic 客戶端。提供 api_key 時建立獨立實例，否則用全域快取（讀 env var）。"""
    if api_key:
        return anthropic.AsyncAnthropic(api_key=api_key)
    global _default_client
    if _default_client is None:
        _default_client = anthropic.AsyncAnthropic()
    return _default_client


# ---------------------------------------------------------------------------
# 智慧截取：reasoning 超長時，定位與問題/關鍵字相關的段落
# ---------------------------------------------------------------------------

# 總預算（chars）— Sonnet 4.6 有 200K context，但 token bucket 限流，
# 12K chars ≈ 4K tokens，比原本 8K 大 50%，但不會顯著拖慢吞吐。
FIELD_BUDGET_TOTAL = 12000
# 兩階段評分：Round 1 用小預算快速篩，Round 2 只對 score>0 的用完整預算
SCREENING_BUDGET = 3000
# 判決數門檻：低於此數直接跑完整預算（兩階段的 overhead 不划算）
TWO_PASS_THRESHOLD = 20
# 非 reasoning 欄位的預算上限（各自獨立，通常遠低於此）
_SECONDARY_BUDGET = {
    "main_text": 800,
    "facts": 1200,           # 預設 always-on，降預算控成本
    "cited_statutes": 400,
    "full_text": 3000,
}

# 結構化段落 headers — 語意角色標示（重要性由 prompt 根據問題類型動態指引）
_SECTION_HEADERS: dict[str, str] = {
    "reasoning":      "【理由】",
    "main_text":      "【主文】",
    "facts":          "【事實】",
    "cited_statutes": "【引用法條】",
    "full_text":      "【全文】",
}
# keyword hit 前後各取多少 chars 作為 context window
_KEYWORD_WINDOW = 2000

# NL 問題拆詞用：標點 + 常見虛詞切分
_Q_PUNCT_RE = re.compile(r'[，。？！、；：「」（）【】《》〈〉\u201c\u201d\u2018\u2019,.:;!?()\[\]\s]+')
# 保守切分：只用不太會切斷複合詞的虛詞
# 避免：為（行為）、是（但是）、有（所有）、在（存在）、中（其中）、以（以上）
_Q_PARTICLE_RE = re.compile(
    r'(?:是否|如何|有無|因此|因而|但是|然而|是否有|惟|的|了|而|與|或|及|和|之)'
)


def _extract_question_terms(question: str) -> list[str]:
    """從律師的 NL 問題中提取關鍵詞組，用於 reasoning 智慧截取。

    策略：先拆標點成子句，再拆虛詞成詞組，保留 ≥2 字的片段。
    例：「法院如何認定行為人無故意過失而免罰？」
      → 子句 [「法院如何認定行為人無故意過失而免罰」]
      → 拆虛詞 [「法院」「認定行為人無故意過失」「免罰」]
      → 長片段再拆 [「認定」「行為人」「無故意過失」]
      結果：[「法院」「認定行為人無故意過失」「免罰」「認定」「行為人」「無故意過失」]
    """
    if not question:
        return []
    # 拆標點成子句
    clauses = [c.strip() for c in _Q_PUNCT_RE.split(question) if len(c.strip()) >= 2]

    terms: list[str] = []
    for clause in clauses:
        # 拆虛詞成詞組
        parts = [p.strip() for p in _Q_PARTICLE_RE.split(clause) if len(p.strip()) >= 2]
        terms.extend(parts)
        # 長詞組（>6 字）再嘗試拆分一次（抓更細的概念）
        for part in parts:
            if len(part) > 6:
                sub = [s.strip() for s in _Q_PARTICLE_RE.split(part) if len(s.strip()) >= 2]
                if len(sub) > 1:
                    terms.extend(sub)

    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _smart_truncate(text: str, budget: int, keywords: list[str] | None = None) -> str:
    """智慧截取：短文直接回傳；超長文用 keywords 定位相關段落。

    策略：
      1. 文字 <= budget → 全文回傳
      2. 有 keywords 且在文中命中 → 取每個 hit 前後 _KEYWORD_WINDOW，合併重疊視窗
         a. 合併後 <= budget → 回傳
         b. 仍然超過 → 截斷到 budget
      3. 無 keywords 或沒命中 → 取前段 + 後段（法院判決的結論常在理由段末尾）
    """
    if len(text) <= budget:
        return text

    # 嘗試 keyword-aware 截取
    if keywords:
        # 收集 (position, keyword_length) 以便計算精確 window
        hits: list[tuple[int, int]] = []
        for kw in keywords:
            start = 0
            while True:
                idx = text.find(kw, start)
                if idx == -1:
                    break
                hits.append((idx, len(kw)))
                start = idx + 1

        if hits:
            # 建立 windows，合併重疊
            windows: list[tuple[int, int]] = []
            for pos, kw_len in sorted(set(hits)):
                ws = max(0, pos - _KEYWORD_WINDOW)
                we = min(len(text), pos + kw_len + _KEYWORD_WINDOW)
                windows.append((ws, we))

            merged: list[list[int]] = [list(windows[0])]
            for s, e in windows[1:]:
                if s <= merged[-1][1] + 200:  # 200 chars gap 也合併，避免碎片
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])

            # 組合文字
            parts: list[str] = []
            if merged[0][0] > 200:
                # 保留開頭 context（法院判決理由通常先交代法條依據）
                parts.append(text[:300])
                parts.append("\n[…略…]\n")
            for i, (ws, we) in enumerate(merged):
                parts.append(text[ws:we])
                if i < len(merged) - 1:
                    parts.append("\n[…略…]\n")
            if merged[-1][1] < len(text) - 200:
                parts.append("\n[…略…]\n")
                # 保留結尾 context（結論/認定常在最後）
                parts.append(text[-300:])

            result = "".join(parts)
            if len(result) <= budget:
                return result
            # 仍超過 → 截斷（保留前面 keyword 段落，因為是 score 依據）
            return result[:budget]

    # Fallback：無 keyword / 沒命中 → 頭尾各半
    head = budget * 2 // 3
    tail = budget - head - 20
    return text[:head] + "\n\n[…中略…]\n\n" + text[-tail:]


# ─── 舊制釋字 reasoning 過濾（AI 精讀專用） ────
_OLD_INTERP_CID_RE = re.compile(r"釋字第?\s*(\d+)\s*號?")


def _old_interp_filtered_reasoning(judgment: dict) -> str | None:
    """若 judgment 為舊制釋字（1-813）、回傳過濾成「本院認定」的 reasoning。

    - 丟棄 petitioner_claim（聲請意旨、當事人 / 關係機關主張）
    - 丟棄 signatures（大法官署名、只是名單）
    - 保留 court_reasoning + conclusion → 核心法律論述
    - 保留 procedural_ruling → 601 型受理程序法院認定段（仍屬本院認定）

    回 None 代表不適用（非舊制釋字 / parser 失敗 / 無可用 sections）、
    由 caller 用原始 reasoning（no-op）。**不影響新制憲判字或一般判決**。
    """
    if parse_interpretation is None:
        return None
    case_id = judgment.get("case_id") or ""
    if not is_old_interpretation(case_id):
        return None
    m = _OLD_INTERP_CID_RE.search(case_id)
    if not m:
        return None
    reasoning = judgment.get("reasoning") or ""
    if not reasoning.strip():
        return None
    try:
        parsed = parse_interpretation(
            cid=int(m.group(1)),
            main_text=judgment.get("main_text") or "",
            reasoning=reasoning,
        )
        wanted = [
            s["text"] for s in (parsed.get("sections") or [])
            if s.get("role") in ("procedural_ruling", "court_reasoning", "conclusion")
        ]
        if not wanted:
            return None  # parser 切不到本院見解段 → 保守回原文
        return "\n\n".join(wanted)
    except Exception:
        return None


def _get_field_text(
    judgment: dict,
    ai_read_fields: list[str],
    search_keywords: list[str] | None = None,
    question: str | None = None,
    budget_override: int | None = None,
) -> tuple[str, str]:
    """取得送給 Claude 精讀的文字及對應 label。

    智慧預算分配：非 reasoning 欄位先取實際長度（有上限），
    剩餘預算全部給 reasoning，超長時用 keyword + question terms 定位相關段落。

    budget_override：覆蓋 FIELD_BUDGET_TOTAL（兩階段評分第一輪用 SCREENING_BUDGET）。
    """
    total_budget = budget_override or FIELD_BUDGET_TOTAL
    # 舊制釋字：reasoning 預先過濾到「本院見解 + 結論」、丟棄聲請意旨 / 大法官署名。
    # 新制憲判字與一般判決此值為 None、下方照原樣讀 reasoning 欄位（不影響）。
    _old_interp_reasoning_filtered = _old_interp_filtered_reasoning(judgment)

    # 第一遍：收集所有欄位的原始文字
    raw_fields: list[tuple[str, str, str]] = []  # (field_name, label, text)
    for field in ai_read_fields:
        value = judgment.get(field)
        if field == "reasoning" and _old_interp_reasoning_filtered is not None:
            # 舊制釋字 + parser 成功 → 用過濾後 reasoning 取代原值
            value = _old_interp_reasoning_filtered
        if not value:
            continue
        label = FIELD_LABELS.get(field, field)
        if field == "cited_statutes":
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = [value]
            raw_fields.append((field, label, "、".join(str(v) for v in value)))
        else:
            raw_fields.append((field, label, str(value)))

    if not raw_fields:
        return "（無內容）", "內容"

    # 合併搜尋關鍵字 + NL 問題關鍵詞組，作為截取的定位依據
    truncation_terms = list(search_keywords or [])
    question_terms = _extract_question_terms(question)
    for qt in question_terms:
        if qt not in truncation_terms:
            truncation_terms.append(qt)

    # 第二遍：分配預算 — secondary fields 先取，reasoning 拿剩餘
    # Screening 模式（budget_override < FIELD_BUDGET_TOTAL）時等比縮小 secondary budgets
    budget_ratio = total_budget / FIELD_BUDGET_TOTAL if total_budget < FIELD_BUDGET_TOTAL else 1.0

    secondary_used = 0
    field_texts: dict[str, str] = {}
    for fname, _label, text in raw_fields:
        if fname == "reasoning":
            continue  # reasoning 最後處理
        cap = int(_SECONDARY_BUDGET.get(fname, 1000) * budget_ratio)
        # facts 和 full_text 也用 smart truncate（法院可能在事實段論述）
        if fname in ("facts", "full_text"):
            field_texts[fname] = _smart_truncate(text, cap, truncation_terms or None)
        else:
            field_texts[fname] = text[:cap] if len(text) > cap else text
        secondary_used += len(field_texts[fname])

    # reasoning 拿到剩餘預算
    reasoning_budget = max(1000, total_budget - secondary_used)
    for fname, _label, text in raw_fields:
        if fname == "reasoning":
            field_texts[fname] = _smart_truncate(text, reasoning_budget, truncation_terms or None)
            break

    # 組合輸出：每個欄位帶【】section header
    sections: list[str] = []
    for fname, _label, _raw in raw_fields:
        if fname not in field_texts:
            continue
        header = _SECTION_HEADERS.get(fname, f"【{_label}】")
        sections.append(f"{header}\n{field_texts[fname]}")

    combined_text = "\n\n".join(sections) if sections else "（無內容）"
    return combined_text, "判決"


async def _call_claude(
    case_id: str,
    field_text: str,
    field_label: str,
    question: str,
    keyword: str,
    api_key: str | None = None,
) -> dict:
    """呼叫 Claude API，回傳解析後的結果 dict。失敗最多重試 2 次。

    keyword：供 variant_candidates discovery prompt 用 — 告訴 Claude 它要找哪個詞的變體。
    """
    client = _get_client(api_key)
    prompt = ANALYSIS_PROMPT.format(
        field_label=field_label,
        question=question,
        keyword=keyword or question,  # 退化：無 keyword 時用 question 當提示
        field_text=field_text,  # 截取已由 _get_field_text 的 _smart_truncate 處理
    )

    # 估算 input tokens（含 system prompt）並向 bucket 預先請求額度。
    # 估 > 實際 → bucket 會過度節流但安全；估 < 實際 → 可能觸發 429 由 retry 兜底。
    estimated_tokens = estimate_prompt_tokens(SYSTEM_PROMPT) + estimate_prompt_tokens(prompt)

    last_exc: Exception | None = None
    for attempt in range(MAX_CLAUDE_RETRIES + 1):
        # 每次嘗試都經過 bucket（重試也計額度，因為真的會打 API）
        await _haiku_itpm_bucket.acquire(estimated_tokens)
        await _haiku_rpm_bucket.acquire(1)

        try:
            response = await client.messages.create(
                model=MODEL_SCORING,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            return extract_json(response.content[0].text)
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_CLAUDE_RETRIES:
                # 429 rate limit 要長 backoff 才有意義（tokens-per-minute 需等 60s 滾動視窗）
                # 其他錯誤用短 backoff。優先看回傳 header 的 retry-after，否則用保守預設。
                is_429 = _is_rate_limit_error(exc)
                retry_after = _extract_retry_after(exc)
                if is_429:
                    delay = retry_after if retry_after is not None else 30 * (attempt + 1)
                else:
                    delay = 2 ** attempt
                logger.warning(
                    "Claude 分析 %s 第 %d 次失敗（%s）：sleep %.1fs 重試",
                    case_id, attempt + 1,
                    "rate_limit" if is_429 else type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

    logger.error("Claude 分析 %s 全部失敗：%s", case_id, last_exc)
    raise last_exc  # type: ignore[misc]


def _is_rate_limit_error(exc: Exception) -> bool:
    """Anthropic SDK 的 RateLimitError 或含 429 狀態碼都視為 rate limit。"""
    import anthropic
    if isinstance(exc, anthropic.RateLimitError):
        return True
    status = getattr(exc, "status_code", None)
    return status == 429


def _extract_retry_after(exc: Exception) -> float | None:
    """從 APIError 的 response headers 取 retry-after（秒）；取不到回 None。"""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# v2 Claude 呼叫：用新 prompt (score = 論述詳細度)，回 {score, position, excerpt}
# ---------------------------------------------------------------------------

async def _call_claude_v2(
    case_id: str,
    field_text: str,
    field_label: str,
    question: str,
    keyword: str,
    api_key: str | None = None,
    search_domain: str = "judgment",
) -> dict:
    """v2 prompt 呼叫。用 Haiku 做 per-judgment 評分（快、省）。
    search_domain='interpretation' 時用 CONS prompt（無 direction 評分）。
    """
    client = _get_client(api_key)
    prompt_template = (
        ANALYSIS_PROMPT_CONS if search_domain == "interpretation"
        else ANALYSIS_PROMPT_V2
    )
    prompt = prompt_template.format(
        field_label=field_label,
        question=question,
        keyword=keyword or question,
        field_text=field_text,  # 截取已由 _get_field_text 的 _smart_truncate 處理
    )
    estimated_tokens = estimate_prompt_tokens(SYSTEM_PROMPT) + estimate_prompt_tokens(prompt)

    last_exc: Exception | None = None
    for attempt in range(MAX_CLAUDE_RETRIES + 1):
        await _haiku_itpm_bucket.acquire(estimated_tokens)
        await _haiku_rpm_bucket.acquire(1)
        try:
            response = await client.messages.create(
                model=MODEL_SCORING,
                max_tokens=512,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text
            stop_reason = getattr(response, "stop_reason", None)
            # Anthropic 實際 stop_reason：end_turn（自然完）/ max_tokens（截斷）/ stop_sequence / tool_use
            # 誤寫成 "end_of_turn" 會把每筆成功回應都當截斷 → log 噪音
            if stop_reason and stop_reason != "end_turn":
                logger.warning("Claude(v2) %s 回應截斷 (stop=%s), text[:80]=%s",
                               case_id, stop_reason, raw_text[:80])
            result = extract_json(raw_text)
            # 附帶 usage 資訊供呼叫方累積
            usage = getattr(response, "usage", None)
            if usage:
                result["_usage_input"] = getattr(usage, "input_tokens", 0)
                result["_usage_output"] = getattr(usage, "output_tokens", 0)
            return result
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_CLAUDE_RETRIES:
                is_429 = _is_rate_limit_error(exc)
                retry_after = _extract_retry_after(exc)
                delay = retry_after if (is_429 and retry_after is not None) else (
                    30 * (attempt + 1) if is_429 else 2 ** attempt
                )
                logger.warning("Claude(v2) %s 第 %d 次失敗（%s）：sleep %.1fs 重試",
                               case_id, attempt + 1,
                               "rate_limit" if is_429 else type(exc).__name__, delay)
                await asyncio.sleep(delay)
    logger.error("Claude(v2) %s 全部失敗：%s", case_id, last_exc)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Synthesis prompt — stage 3 v2 全部精讀完後跑一次，產生 analysis.synthesis JSON
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """\
你是律師的法律研究助理。律師提出了一個研究問題，系統已從司法院判決資料庫中找到相關判決並逐筆精讀。現在請你根據精讀結果，**直接回答律師的問題**。

律師研究問題：
{question}

共 {n} 份判決有論述此問題。以下列出 score 最高的 {listed} 份，每筆含法院立場（[支持]/[反對]/[中性]）和法院認定摘要：
{items}

請直接回答律師的問題。**direction_counts 是內部統計，律師不會看到、也不該影響你的 answer 結構**。

只回傳 JSON，不要任何其他文字：
{{
  "total_relevant": {n},
  "answer": "300字內。直接回答律師問的問題、不要覆述問題本身。內容依問題類型決定：",
  "direction_counts": {{"支持": 0, "反對": 0, "中性": 0}},
  "clusters": []
}}

answer 內容指引（依問題類型自動套用，**不要在 answer 文字中提到「支持/反對/中性」這些方向標籤**）：

A. 探索型問題（律師問「法院如何判斷 X / X 的要件 / X 怎麼認定」，items 多為 [中性]）：
   answer 重點放在：
   - 法院常見的判斷要件、標準、判斷流程
   - 主流見解與例外情境
   - 律師在實務上如何套用（援引條文 + 主張結構 + 應注意的反面論述）
   不要寫「對立見解」「對手可能主張」這種強行對立的框架。

B. 立場型問題（律師問「找出法院認定 X 成立 / 不成立的判決」，items 有明顯 [支持]/[反對] 分布）：
   answer 重點放在：
   - 主流結論（多數法院怎麼判）
   - 認定成立 / 不成立的具體理由
   - 對立見解的核心主張、律師需注意的反方論點

clusters 規則（**律師看到的真正分類就在這裡，比 direction 重要**）：
  依**內容本質**分群 — 問什麼就按什麼分：
  - 探索型 → 按法院的**判斷要件或標準**分群（如「主觀要件」「客觀要件」「程序要件」）
  - 立場型「找出 X 的案件」→ 按法院准許/認定的**理由類型**分群（如「急迫危險」「保全必要」）
  - 量刑型「X 判幾年」→ 按**刑度區間**或**量刑因素**分群
  - 若無法有意義地分群 → 填 []
  格式：[{{"label":"10字內","case_ids":["id1","id2"]}}]，每群最多 5 個代表性 case_id。
  探索型問題下、clusters 應該越精準越好（律師會用這個當思考骨架）。
"""


# ---------------------------------------------------------------------------
# 快速追問 prompt — 基於既有精讀摘要回答新問題（1 次 Claude call）
# ---------------------------------------------------------------------------

QUICK_FOLLOWUP_PROMPT = """\
律師先前的研究問題：{original_question}
以下是 {n} 筆精讀結果摘要（每筆含 score、方向、法院立場、核心段落）：
{items}

律師追問：{followup_question}

請基於以上既有摘要回答律師的追問。只回傳 JSON，不要任何其他文字：
{{
  "summary": "200字內回答律師追問",
  "direction_summary": "支持 X 筆、反對 Y 筆（若追問涉及方向判斷）",
  "relevant_case_ids": ["與追問直接相關的 case_id，最多 10 個"],
  "clusters": []
}}

clusters：若回答涉及不同立場或分類，依見解分 2-3 群，格式 [{{"label":"10字內","case_ids":["id1"]}}]，每群最多 5 個。不需分類時填 []。
"""

_QUICK_FOLLOWUP_MAX_ITEMS = 80  # 快速追問帶更多 items（因為不是 synthesis 那麼長的回應）


def should_quick_followup(new_question: str, existing_results: list[dict]) -> tuple[bool, float]:
    """判斷追問能否基於既有摘要回答。回傳 (是否快速模式, 命中率)。"""
    terms = _extract_question_terms(new_question)
    if not terms:
        return False, 0.0
    corpus = " ".join(
        (r.get("reason", "") or "") + " " + (r.get("excerpt", "") or "")
        for r in existing_results
    )
    hits = sum(1 for t in terms if t in corpus)
    ratio = hits / len(terms) if terms else 0.0
    return ratio >= 0.3, ratio


async def run_quick_followup(
    analysis_id: str,
    source_analysis_id: str,
    question: str,
    original_question: str,
    api_key: str | None = None,
) -> dict:
    """基於既有精讀摘要回答追問 — 1 次 Claude call。

    讀取 source_analysis_id 的 analysis_results，送給 Claude 配追問。
    結果存入 analysis_id 的 synthesis 欄位。
    """
    results = await db.get_analysis_results_scored(source_analysis_id)
    if not results:
        synthesis = {
            "total_relevant": 0,
            "summary": "來源分析沒有相關判決，無法回答追問。",
            "clusters": [],
            "_quick": True,
        }
        await db.set_analysis_synthesis(analysis_id, synthesis)
        return synthesis

    # Items 重排：優先 prompt 中那些跟「追問」最相關的判決，而非單純 score top
    # smart_truncate 在 raw reasoning 用 keyword window；這裡 reason+excerpt 已是預摘要，
    # 改用「追問 question 關鍵詞命中率」當主排序，score 當 tiebreaker
    followup_terms = _extract_question_terms(question)
    def _followup_relevance(r: dict) -> tuple[int, int]:
        if not followup_terms:
            return (0, r.get("score", 0))
        text = (r.get("reason") or "") + " " + (r.get("excerpt") or "")
        hits = sum(1 for t in followup_terms if t in text)
        return (hits, r.get("score", 0))
    sorted_results = sorted(results, key=_followup_relevance, reverse=True)

    # 組 items
    items_lines = []
    for idx, r in enumerate(sorted_results[:_QUICK_FOLLOWUP_MAX_ITEMS], 1):
        court = r.get("court", "")
        case_id = r.get("case_id", "")
        score = r.get("score", 0)
        reason = (r.get("reason") or "").strip()[:80]
        excerpt = (r.get("excerpt") or "").strip()[:100]
        items_lines.append(f'{idx}. {court} {case_id} (score={score}): {reason} | {excerpt}')
    items_text = "\n".join(items_lines)

    prompt = QUICK_FOLLOWUP_PROMPT.format(
        original_question=original_question,
        n=len(results),
        items=items_text,
        followup_question=question,
    )

    client = _get_client(api_key)
    estimated_tokens = estimate_prompt_tokens(SYSTEM_PROMPT) + estimate_prompt_tokens(prompt)

    last_exc = None
    for attempt in range(1, 3):
        try:
            await _sonnet_itpm_bucket.acquire(estimated_tokens)
            await _sonnet_rpm_bucket.acquire(1)
            response = await client.messages.create(
                model=MODEL_SYNTHESIS,
                max_tokens=_SYNTHESIS_MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text
            parsed = extract_json(raw_text)
            synthesis = {
                "total_relevant": len(results),
                "consensus": parsed.get("consensus", ""),
                "direction_summary": str(parsed.get("direction_summary", "") or ""),
                "summary": str(parsed.get("summary", "") or ""),
                "clusters": parsed.get("clusters") or [],
                "relevant_case_ids": parsed.get("relevant_case_ids") or [],
                "_quick": True,
            }
            await db.set_analysis_synthesis(analysis_id, synthesis)
            logger.info("quick_followup %s 完成，relevant=%d", analysis_id, len(synthesis["relevant_case_ids"]))
            return synthesis
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                delay = 5
                is_429 = _is_rate_limit_error(exc)
                if is_429:
                    delay = _extract_retry_after(exc) or 30
                logger.warning("quick_followup attempt %d 失敗：%s，retry in %.0fs", attempt, exc, delay)
                await asyncio.sleep(delay)

    logger.error("quick_followup %s 全部失敗：%s", analysis_id, last_exc)
    synthesis = {
        "total_relevant": len(results),
        "summary": f"快速分析失敗（{type(last_exc).__name__}），請改用完整精讀。",
        "clusters": [],
        "_quick": True,
    }
    await db.set_analysis_synthesis(analysis_id, synthesis)
    return synthesis


_SYNTHESIS_MAX_RETRIES = 3
_SYNTHESIS_MAX_ITEMS = 40       # prompt 中最多帶幾筆（top score）；86 筆全塞會截斷回應
_SYNTHESIS_POSITION_LEN = 60    # 每筆 position 截取長度（更短 = 給 Claude 更多回應空間）
_SYNTHESIS_MAX_TOKENS = 4096    # 回應空間（summary 300字 + clusters JSON 需要足夠空間）


def _validate_synthesis(data: object) -> dict:
    """驗證 + 正規化 Claude 回傳的 synthesis JSON。缺 key 補預設值。

    相容新舊格式：新版用 answer/direction_counts，舊版用 summary/consensus/direction_summary。
    前端統一讀 summary（由本函式從 answer 或 summary 取）。
    """
    if not isinstance(data, dict):
        raise ValueError(f"synthesis 回傳非 dict: {type(data)}")

    # answer（新）→ summary（前端統一讀 summary）
    summary = str(data.get("answer", "") or data.get("summary", "") or "")

    # direction_counts（新）→ direction_summary（前端統一讀 direction_summary）
    dc = data.get("direction_counts")
    if isinstance(dc, dict):
        dir_summary = f"支持 {dc.get('支持', 0)} 筆、反對 {dc.get('反對', 0)} 筆、中性 {dc.get('中性', 0)} 筆"
        support = dc.get("支持", 0)
        oppose = dc.get("反對", 0)
    else:
        dir_summary = str(data.get("direction_summary", "") or "")
        support = oppose = 0

    # consensus 自動推導（新 prompt 不再要求 Claude 填 consensus）
    # 類別：一致 / 多數 / 分歧 / 彙整 / 不足
    #   - 一致 / 多數 / 分歧：判決有立場（支持 or 反對）
    #   - 彙整：探索型問題（律師問「法院如何判斷 X」），全部 neutral，不該歸為「不足」
    #   - 不足：真的相關筆數過少
    consensus = str(data.get("consensus", "") or "")
    total = support + oppose + (int(data.get("total_relevant", 0)) - support - oppose)
    if not consensus or consensus not in ("一致", "多數", "分歧", "彙整", "不足"):
        if support > 0 and oppose == 0:
            consensus = "一致"
        elif oppose > 0 and support == 0:
            consensus = "一致"
        elif support > 0 and oppose > 0:
            ratio = support / (support + oppose)
            consensus = "多數" if ratio > 0.7 or ratio < 0.3 else "分歧"
        elif total >= 5:
            # 全 neutral（探索型問題）+ 至少 5 筆 → 法院認定彙整，不算「不足」
            consensus = "彙整"
        else:
            consensus = "不足"

    clusters = data.get("clusters") or []
    if not isinstance(clusters, list):
        clusters = []

    return {
        "total_relevant": int(data.get("total_relevant", 0)),
        "consensus": consensus,
        "direction_summary": dir_summary,
        "summary": summary,
        "clusters": clusters,
    }


def _build_stats_synthesis(results: list[dict]) -> dict:
    """從 DB 精讀結果直接產出統計摘要 + 方向分類 — 零 Claude call，永遠不會失敗。

    即使 Claude synthesis 全部重試失敗，律師仍能看到：
    - 統計數字（筆數、score 分布）
    - 支持/反對自動分類（從每筆的 [支持]/[反對] direction 標籤算出）
    - 方向摘要
    精讀結果一筆都不會浪費。
    """
    scores = [r.get("score", 0) for r in results if r.get("score")]
    total = len(results)
    avg = sum(scores) / len(scores) if scores else 0
    high = sum(1 for s in scores if s >= 7)
    mid = sum(1 for s in scores if 4 <= s < 7)
    low = sum(1 for s in scores if 1 <= s < 4)

    # 從 reason 的 [支持]/[反對] prefix 解析 direction → 自動分類
    support_ids: list[str] = []
    oppose_ids: list[str] = []
    neutral_ids: list[str] = []
    for r in results:
        reason = (r.get("reason") or "").strip()
        case_id = r.get("case_id", "")
        if reason.startswith("[支持]"):
            support_ids.append(case_id)
        elif reason.startswith("[反對]"):
            oppose_ids.append(case_id)
        else:
            neutral_ids.append(case_id)

    # 自動判斷 consensus（同 _validate_synthesis 的分類）
    if support_ids and not oppose_ids:
        consensus = "一致"
    elif oppose_ids and not support_ids:
        consensus = "一致"
    elif support_ids and oppose_ids:
        ratio = len(support_ids) / (len(support_ids) + len(oppose_ids))
        consensus = "多數" if ratio > 0.7 or ratio < 0.3 else "分歧"
    elif total >= 5:
        consensus = "彙整"   # 全 neutral 但筆數足，是探索型問題的合理結果
    else:
        consensus = "不足"

    # direction_summary 仍計算並存進 synthesis JSON（給可能想看細節的下游用），
    # 但不在 user-facing summary 文字中顯示 — 律師看到「支持 0 筆、反對 0 筆、
    # 中性 109 筆」這種計數對其判斷無價值（探索型問題下尤其誤導）。
    dir_summary = f"支持 {len(support_ids)} 筆、反對 {len(oppose_ids)} 筆、中性 {len(neutral_ids)} 筆"
    summary = f"共 {total} 筆相關判決。"
    # 取 score 最高的前 3 筆 position 給律師當骨架 — 不論支持 / 反對 / 中性
    sorted_results = sorted(results, key=lambda r: r.get("score", 0), reverse=True)
    top_positions = []
    for r in sorted_results[:3]:
        reason = (r.get("reason") or "").strip()
        # 剝掉 [支持] / [反對] / [中性] prefix，只保留法院認定摘要本身
        clean = re.sub(r"^\[(支持|反對|中性)\]\s*", "", reason)[:60]
        if clean:
            top_positions.append(clean)
    if top_positions:
        summary += "高分判決主要認定：" + "；".join(top_positions) + "。"
    # 不附「失敗」訊息 — _fallback: true 會讓前端顯示「重新生成摘要」按鈕

    # 自動 clusters — 只放「符合方向」的群組
    # 反對判決不獨立成 tab（律師問「找出 X」時，反對 X 的是雜訊不是對照組）
    # 反對判決仍在「全部」tab 看得到（有方向 badge）
    clusters = []
    if support_ids:
        clusters.append({"label": "支持", "case_ids": support_ids})
    # 反對判決只在沒有支持判決時才獨立顯示（= 全部是反對，律師需要知道）
    if oppose_ids and not support_ids:
        clusters.append({"label": "反對", "case_ids": oppose_ids})

    return {
        "total_relevant": total,
        "consensus": consensus,
        "direction_summary": dir_summary,
        "summary": summary,
        "clusters": clusters,
        "_fallback": True,
    }


async def run_synthesis(
    analysis_id: str,
    question: str,
    api_key: str | None = None,
) -> dict:
    """對 analysis_results 中 score > 0 的結果做 Claude synthesis call。

    保障機制（synthesis 永遠不會讓精讀結果白費）：
      Level 1: 正常 Claude call（max_items=40, max_tokens=4096）
      Level 2: 截斷偵測 → 自動縮減 items 重試
      Level 3: JSON parse 失敗 → 縮減 items + 簡化 prompt 重試
      Level 4: API 429 → 退避重試
      Level 5: 全部 Claude 失敗 → 純統計 fallback（零 Claude call，從 DB 數字算）
    每一層都會保存結果到 DB，律師隨時能看到精讀結果。
    """
    results = await db.get_analysis_results_scored(analysis_id)
    if not results:
        synthesis = {
            "total_relevant": 0,
            "consensus": "不足",
            "summary": "精讀後沒有判決有論述此問題，建議調整關鍵字或問題方向。",
            "clusters": [],
        }
        await db.set_analysis_synthesis(analysis_id, synthesis)
        return synthesis

    client = _get_client(api_key)
    max_items = _SYNTHESIS_MAX_ITEMS
    last_exc: Exception | None = None

    for attempt in range(1, _SYNTHESIS_MAX_RETRIES + 1):
        items_lines = []
        for idx, r in enumerate(results[:max_items], 1):
            court = r.get("court", "")
            case_id = r.get("case_id", "")
            score = r.get("score", 0)
            reason = (r.get("reason") or "").strip()
            # reason 格式：[支持] 法院認定… 或 [反對] 法院認定… 或純文字
            direction = "中性"
            position = reason[:_SYNTHESIS_POSITION_LEN]
            if reason.startswith("[支持]"):
                direction = "支持"; position = reason[4:].strip()[:_SYNTHESIS_POSITION_LEN]
            elif reason.startswith("[反對]"):
                direction = "反對"; position = reason[4:].strip()[:_SYNTHESIS_POSITION_LEN]
            elif reason.startswith("[中性]"):
                direction = "中性"; position = reason[4:].strip()[:_SYNTHESIS_POSITION_LEN]
            items_lines.append(f'{idx}. [{direction}] {court} {case_id} (score={score}): {position}')
        items_text = "\n".join(items_lines)

        listed_count = min(len(results), max_items)
        prompt = SYNTHESIS_PROMPT.format(
            question=question, n=len(results), listed=listed_count, items=items_text,
        )
        estimated_tokens = estimate_prompt_tokens(SYSTEM_PROMPT) + estimate_prompt_tokens(prompt)

        try:
            await _sonnet_itpm_bucket.acquire(estimated_tokens)
            await _sonnet_rpm_bucket.acquire(1)

            response = await client.messages.create(
                model=MODEL_SYNTHESIS,
                max_tokens=_SYNTHESIS_MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            )

            raw_text = response.content[0].text
            stop_reason = getattr(response, "stop_reason", None)
            logger.info(
                "synthesis attempt %d/%d: %d chars, stop=%s, text[:100]=%s",
                attempt, _SYNTHESIS_MAX_RETRIES, len(raw_text), stop_reason, raw_text[:100],
            )

            # 真正的截斷 = max_tokens（達 token 上限，回應不完整）
            # Anthropic API 實際 stop_reason 值：end_turn / max_tokens / stop_sequence / tool_use
            # 之前誤寫 != "end_of_turn"（多了 _of_）→ 任何 end_turn 都被當截斷 → Claude
            # 完整生成的 answer 全部被 discard 三次 → fallback。Bug 修補：明確檢查 max_tokens。
            if stop_reason == "max_tokens":
                logger.warning("synthesis 截斷（stop=%s），items %d→%d",
                               stop_reason, max_items, max(10, max_items // 2))
                max_items = max(10, max_items // 2)
                continue

            parsed = extract_json(raw_text)
            synthesis = _validate_synthesis(parsed)
            synthesis["total_relevant"] = len(results)

            # 記錄 synthesis 的 token usage
            usage = getattr(response, "usage", None)
            if usage:
                synthesis["_synth_input"] = getattr(usage, "input_tokens", 0)
                synthesis["_synth_output"] = getattr(usage, "output_tokens", 0)

            await db.set_analysis_synthesis(analysis_id, synthesis)
            logger.info("synthesis %s 完成，consensus=%s, total=%d",
                        analysis_id, synthesis.get("consensus"), len(results))
            return synthesis

        except Exception as exc:
            last_exc = exc
            is_429 = _is_rate_limit_error(exc)
            retry_after = _extract_retry_after(exc) if is_429 else None

            # JSON parse 失敗 → 同時縮減 items（下輪 prompt 更短 → 回應更精簡）
            if isinstance(exc, json.JSONDecodeError):
                # DEBUG: dump Claude 實際回的內容到 log，方便診斷 JSON parse 失敗
                try:
                    logger.error(
                        "synthesis %s attempt %d JSON parse FAIL at pos %d: %s\n"
                        "  raw_text 全文 (%d chars):\n%s\n"
                        "  raw_text end:\n%s",
                        analysis_id, attempt, exc.pos, exc.msg,
                        len(raw_text), raw_text,
                        raw_text[-200:] if len(raw_text) > 200 else raw_text,
                    )
                except Exception:
                    pass
                max_items = max(10, max_items // 2)

            if attempt < _SYNTHESIS_MAX_RETRIES:
                delay = (retry_after if (is_429 and retry_after) else
                         30 * attempt if is_429 else 5 * attempt)
                logger.warning(
                    "synthesis %d/%d 失敗（%s），items→%d，sleep %.0fs",
                    attempt, _SYNTHESIS_MAX_RETRIES,
                    type(exc).__name__, max_items, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("synthesis %s 全 %d 次失敗：%s",
                             analysis_id, _SYNTHESIS_MAX_RETRIES, exc)

    # Level 5: 全部 Claude 失敗 → 純統計 fallback（從 DB 已有的 score 直接算）
    logger.info("synthesis %s 降級為統計 fallback（%d 筆結果）", analysis_id, len(results))
    synthesis = _build_stats_synthesis(results)
    await db.set_analysis_synthesis(analysis_id, synthesis)
    return synthesis


def _citation_precision_passes(
    search_keywords: list[str] | None,
    extracted_citations_raw: str | list | None,
) -> tuple[bool, str | None]:
    """Stage 3 精準過濾：律師的法條 keyword 是否在判決的 extracted_citations 中 tuple-match 命中。

    Stage 1 走 MCP 全文字串比對，會把「本件與 X 條無關」這種否定句也撈進候選池。
    到 Stage 3 時 task_judgments.extracted_citations 已有結構化 tuple 清單，用
    Citation.covers() 做精準比對可以剔除假陽性，省 Claude tokens。

    規則：
      - 非法條 keyword（parse_keyword 失敗）→ 不參與此 filter，由 Claude 語意判斷
      - 法條 keyword → 至少要有一筆 extracted_citation 被 covers()，否則 reject
      - extracted_citations 空 / malformed → 放行（citation_extractor 可能漏抓，
        寧可噪音也不錯殺真陽性）

    回傳 (passes, reason)：
      passes = True  → 通過 filter（或無 citation kw / 無法驗證）
      passes = False → 所有 citation kw 都沒命中，reason 描述哪個 kw 沒命中
    """
    from src.pipeline.citation_normalizer import parse_keyword, Citation

    if not search_keywords:
        return True, None

    # 任何非法條 keyword 都讓 Claude 自行評斷（跳過 filter）
    citation_queries: list[tuple[str, Citation]] = []
    for kw in search_keywords:
        q = parse_keyword(kw)
        if q is not None:
            citation_queries.append((kw, q))
    if not citation_queries:
        return True, None

    # 解析 extracted_citations
    if isinstance(extracted_citations_raw, str):
        try:
            extracted = json.loads(extracted_citations_raw)
        except (json.JSONDecodeError, TypeError):
            return True, None
    else:
        extracted = extracted_citations_raw or []

    if not isinstance(extracted, list) or not extracted:
        # 結構化 citation 空 → 可能 citation_extractor 漏抓、可能判決真的沒引該條
        # 保守放行交給 Claude 判斷
        return True, None

    # 反序列化成 Citation 物件（tuples / list 都接）
    extracted_cites: list[Citation] = []
    for raw in extracted:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            extracted_cites.append(Citation(
                law=raw[0] if raw[0] else None,
                article=int(raw[1]) if raw[1] is not None else None,
                sub=raw[2] if len(raw) > 2 and raw[2] is not None else None,
                paragraph=raw[3] if len(raw) > 3 and raw[3] is not None else None,
                item=raw[4] if len(raw) > 4 and raw[4] is not None else None,
                subitem=raw[5] if len(raw) > 5 and raw[5] is not None else None,
            ))
        except (ValueError, TypeError):
            continue

    if not extracted_cites:
        return True, None  # 有 raw 資料但全部解析失敗 → 放行

    # 每個 citation kw 都必須至少在 extracted 中有一筆被 covers()
    for kw, q in citation_queries:
        if not any(q.covers(c) for c in extracted_cites):
            return False, kw
    return True, None


async def run_analysis_v2(
    analysis_id: str,
    task_id: str,
    question: str,
    read_facts: bool = False,
    on_batch_done: Callable[[list[dict]], Coroutine[Any, Any, None]] | None = None,
    api_key: str | None = None,
    discovery_keyword: str | None = None,
    case_id_filter: list[str] | None = None,
    search_keywords: list[str] | None = None,
    judgment_queue: asyncio.Queue | None = None,
    expected_total: int | None = None,
    search_domain: str = "judgment",
) -> dict:
    """Stage 3 v2：每筆判決跑 ANALYSIS_PROMPT_V2，score 定義為論述詳細度。

    - 讀 reasoning + main_text + cited_statutes；read_facts=True 時加讀 facts
    - 輸出 DB schema 相容：score + match ('yes' if score>0 else 'no') + reason (= position) + excerpt
    - 不跑 synthesis；呼叫方自己在全部完成後呼叫 run_synthesis()

    search_keywords：原始搜尋關鍵字（用於 reasoning 超長時智慧截取相關段落）。

    兩種執行模式：
      - DB-read 模式（judgment_queue=None）：一次讀取 task_judgments，全部丟 gather。
        用於 recovery / 單獨追問 / legacy call site。
      - Streaming 模式（judgment_queue 提供）：CONCURRENCY 個 consumer worker 從 queue 取
        判決逐筆處理，caller 用 None sentinel 通知結束。用於 producer-consumer 交錯模式
        （fetch 邊抓邊餵 Claude，消除兩輪制的空窗期）。
        caller 必須自行過濾 already_done，並於 queue 結束時 put N 個 None。
        expected_total = queue 將送出的判決總數（用於 total 與 use_two_pass 計算）。
    """
    # 讀取欄位：結構化段落全部讀取（facts 預設 always-on，預算由 _SECONDARY_BUDGET 控制）
    ai_read_fields = ["reasoning", "main_text", "facts", "cited_statutes"]

    streaming_mode = judgment_queue is not None
    judgments: list[dict] = []
    already_done_count = 0

    if not streaming_mode:
        judgments = await db.get_task_judgments(task_id)
        if case_id_filter:
            # 雙 key 匹配：case_id_filter 可能含 JID 或格式化名稱，
            # task_judgments.case_id 為格式化名稱，source_url 含 JID。
            from src.worker.runner import _build_judgment_map
            jmap = _build_judgment_map(judgments)
            matched_case_ids = set()
            for fid in case_id_filter:
                j = jmap.get(fid)
                if j:
                    matched_case_ids.add(j["case_id"])
            judgments = [j for j in judgments if j["case_id"] in matched_case_ids]
            if not judgments and case_id_filter:
                logger.warning(
                    "case_id_filter 有 %d 筆但全部未匹配 task_judgments（可能格式不一致）",
                    len(case_id_filter),
                )

        # Recovery 重跑時：跳過已有 analysis_results 的判決，避免重複
        existing_results = await db.get_analysis_results(analysis_id)
        already_done_ids = {r["case_id"] for r in existing_results}
        if already_done_ids:
            judgments = [j for j in judgments if j["case_id"] not in already_done_ids]
            logger.info("analysis %s recovery：跳過 %d 筆已完成，剩餘 %d 筆",
                         analysis_id, len(already_done_ids), len(judgments))
        already_done_count = len(already_done_ids)
        total = len(judgments) + already_done_count
        await db.update_analysis(
            analysis_id, total=total,
            completed=already_done_count, status="running",
        )
    else:
        # Streaming 模式：caller 已過濾 already_done、expected_total 只含「剩餘要分析的」
        # 但 UI 進度條 total 需反映「整批判決總數」（含 resume 情境的 already_done）、
        # 否則 completed 跑起來會 >= total（資料庫累積 counter）、UI banner 顯示「剩餘 0 筆」
        existing_for_total = await db.get_analysis_results(analysis_id)
        already_done_count = len({r["case_id"] for r in existing_for_total})
        total = int(expected_total or 0) + already_done_count
        # total 會在下面 use_two_pass 決定後再更新
        await db.update_analysis(analysis_id, status="running")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    batch_results: list[dict] = []
    # Token usage 累積器
    _total_input_tokens = 0
    _total_output_tokens = 0

    # 多 keyword discovery：過濾掉法條 keyword（走規則展開不需 discovery），
    # 剩下的 round-robin 分配給每筆判決。每筆 Claude call 仍只問一個 keyword 的變體。
    from src.pipeline.citation_normalizer import parse_keyword as _parse_kw
    discoverable_kws = [
        kw for kw in (search_keywords or [])
        if _parse_kw(kw) is None and len(kw) > 1
    ]
    # Fallback：如果全是法條，用 discovery_keyword（可能也是法條但至少保持舊行為）
    if not discoverable_kws and discovery_keyword and _parse_kw(discovery_keyword) is None:
        discoverable_kws = [discovery_keyword]

    # per-keyword discovered variants：{canonical_kw: {variant: count}}
    discovered_per_kw: dict[str, dict[str, int]] = {kw: {} for kw in discoverable_kws}

    # Streaming 模式下 total 來自 caller（= expected_total，已扣除 already_done）
    #   → 用它決定 use_two_pass，一致看待「本輪實際要處理」的筆數
    # DB-read 模式下 len(judgments) 就是本輪要處理的（已扣除 already_done）
    workload_count = total if streaming_mode else len(judgments)
    use_two_pass = workload_count >= TWO_PASS_THRESHOLD
    _report_lock = asyncio.Lock()

    async def _analyze_auto(judgment: dict, idx: int) -> dict:
        """每筆自動流轉：screening → score>0 就立刻精讀 → 寫 DB → 推 SSE。

        不分兩個 gather。每筆獨立完成全流程，8 並行 slot 自然分配。
        """
        case_id = judgment["case_id"]

        # 資料完整性檢查：核心欄位全空 = MCP 解析失敗，不浪費 Claude call
        has_content = bool(
            (judgment.get("reasoning") or "").strip()
            or (judgment.get("main_text") or "").strip()
            or (judgment.get("facts") or "").strip()
            or (judgment.get("full_text") or "").strip()
        )
        if not has_content:
            result = {"case_id": case_id, "match": "data_error", "score": None,
                      "excerpt": "", "reason": "資料不完整：MCP 解析失敗，無可讀內容"}
            try:
                await db.create_analysis_result(
                    analysis_id=analysis_id, case_id=case_id,
                    match="data_error", score=None, excerpt="", reason=result["reason"],
                )
                # two_pass 模式每筆 2 步；data_error 跳過兩輪，一次加 2
                delta = 2 if use_two_pass else 1
                await db.increment_analysis_progress(analysis_id, completed_delta=delta)
            except Exception:
                pass
            return result

        # 法條精準 filter：律師的 citation keyword 必須在 extracted_citations 中 tuple-covered
        # — 剔除「本件與 X 條無關」這類 Stage 1 字串誤抓、免跑 Claude 省 token
        cit_pass, cit_miss_kw = _citation_precision_passes(
            search_keywords, judgment.get("extracted_citations")
        )
        if not cit_pass:
            result = {
                "case_id": case_id, "match": "no", "score": 0, "excerpt": "",
                "reason": f"未引用「{cit_miss_kw}」（Stage 1 字串比對誤抓）",
            }
            try:
                await db.create_analysis_result(
                    analysis_id=analysis_id, case_id=case_id,
                    match="no", score=0, excerpt="", reason=result["reason"],
                )
                delta = 2 if use_two_pass else 1
                await db.increment_analysis_progress(analysis_id, completed_delta=delta)
            except Exception:
                pass
            return result

        kw_for_discovery = (
            discoverable_kws[idx % len(discoverable_kws)] if discoverable_kws
            else (discovery_keyword or question)
        )

        async def _call_and_parse(budget: int | None, do_discovery: bool) -> dict:
            field_text, field_label = _get_field_text(
                judgment, ai_read_fields, search_keywords, question,
                budget_override=budget,
            )
            is_screening = budget is not None and budget < FIELD_BUDGET_TOTAL
            async with semaphore:
                parsed = await _call_claude_v2(
                    case_id, field_text, field_label, question,
                    keyword=kw_for_discovery if not is_screening else question,
                    api_key=api_key,
                    search_domain=search_domain,
                )
            nonlocal _total_input_tokens, _total_output_tokens
            _total_input_tokens += parsed.pop("_usage_input", 0)
            _total_output_tokens += parsed.pop("_usage_output", 0)

            score = int(parsed.get("score", 0))
            # interpretation mode：無 direction，固定 "中性" 以利下游 schema 相容（但前端不顯示）
            if search_domain == "interpretation":
                direction = "中性"
            else:
                direction = str(parsed.get("direction", "中性") or "中性").strip()
            if direction not in ("支持", "反對", "中性"):
                direction = "中性"
            raw_position = str(parsed.get("position", "") or "")[:200]
            position = f"[{direction}] {raw_position}" if raw_position else ""
            raw_excerpt = str(parsed.get("excerpt", "") or "")[:500]
            found_in = str(parsed.get("found_in", "") or "").strip()
            found_label = FIELD_LABELS.get(found_in, "")
            excerpt = f"[{found_label}] {raw_excerpt}" if found_label and raw_excerpt else raw_excerpt
            match = "yes" if score > 0 else "no"

            if do_discovery and not is_screening:
                candidates = parsed.get("variant_candidates") or []
                if isinstance(candidates, list) and kw_for_discovery in discovered_per_kw:
                    bucket = discovered_per_kw[kw_for_discovery]
                    for c in candidates:
                        cs = str(c).strip()
                        if cs and cs != kw_for_discovery:
                            bucket[cs] = bucket.get(cs, 0) + 1

            # score=0 且 excerpt 為空：用搜尋關鍵字從全文截取上下文（零 LLM cost）
            # P2-4 加 section filter：若 keyword 出現位置前 500 字內有「主張/抗辯/答辯 section
            # marker」且之後沒有「本院…」marker、代表該位置仍在當事人立場段內、放棄這個 window
            # （跟 Claude 路徑一致：excerpt 不該從當事人主張段挑）
            if score == 0 and not excerpt and search_keywords:
                text = (judgment.get("reasoning") or judgment.get("full_text") or "")
                for kw in search_keywords:
                    pos = text.find(kw)
                    if pos < 0:
                        continue
                    # 往前看 500 字、check 是否在當事人主張 section 內
                    lookback_start = max(0, pos - 500)
                    lookback = text[lookback_start:pos]
                    # 覆蓋所有程序當事人變體：原告/被告（第一審）、上訴人/被上訴人（上訴）、
                    # 抗告人（抗告）、聲請人/相對人（聲請）、參加人、再審、自訴/告訴人（刑事）。
                    # 每個身分可接：主張/起訴/略以/抗辯/答辯/辯稱/陳述/聲明/訴稱/聲請/求為
                    party_markers = [
                        # 第一審當事人
                        '原告主張', '原告起訴', '原告略以', '原告抗辯', '原告陳述',
                        '被告答辯', '被告抗辯', '被告略以', '被告辯稱', '被告陳述',
                        # 上訴 / 抗告
                        '上訴人主張', '上訴人抗辯', '上訴人略以', '上訴意旨',
                        '被上訴人答辯', '被上訴人抗辯', '被上訴人略以',
                        '抗告人主張', '抗告人略以', '抗告意旨',
                        # 聲請 / 相對人
                        '聲請人主張', '聲請人略以', '聲請意旨',
                        '相對人答辯', '相對人主張', '相對人略以',
                        # 參加人
                        '參加人主張', '參加人略以',
                        # 再審
                        '再審原告主張', '再審原告略以', '再審被告答辯',
                        # 刑事
                        '自訴人主張', '告訴人',
                    ]
                    court_markers = ['本院認為', '本院查', '本院見解', '本院認定', '本院判斷']
                    last_party  = max((lookback.rfind(m) for m in party_markers), default=-1)
                    last_court  = max((lookback.rfind(m) for m in court_markers), default=-1)
                    if last_party > last_court:
                        continue  # 這個 kw 落在當事人 section、換下個 kw
                    start = max(0, pos - 30)
                    end = min(len(text), pos + len(kw) + 50)
                    snippet = text[start:end].replace('\n', ' ').strip()
                    if start > 0: snippet = '…' + snippet
                    if end < len(text): snippet = snippet + '…'
                    excerpt = snippet[:120]
                    break

            # Log excerpt quality anomalies（非阻塞、不 re-pick、一週後 review 決定要不要加後處理）
            try:
                from src.utils.excerpt_anomaly_log import log_excerpt_anomaly
                await log_excerpt_anomaly(
                    case_id=case_id, analysis_id=analysis_id, score=score,
                    excerpt=raw_excerpt, main_text=judgment.get("main_text"),
                )
            except Exception:
                pass
            return {"case_id": case_id, "match": match, "score": score,
                    "excerpt": excerpt, "reason": position}

        try:
            if use_two_pass:
                # Round 1: screening（3K budget）— step 1 of 2
                r1 = await _call_and_parse(SCREENING_BUDGET, do_discovery=False)
                await db.create_analysis_result(
                    analysis_id=analysis_id, case_id=case_id,
                    match=r1["match"], score=r1["score"],
                    excerpt=r1["excerpt"], reason=r1["reason"],
                )
                m_delta = 1 if r1["score"] and r1["score"] > 0 else 0
                await db.increment_analysis_progress(
                    analysis_id, completed_delta=1, match_delta=m_delta,
                )

                # score > 0 → 立刻接 Round 2（12K budget）— step 2 of 2
                if r1["score"] and r1["score"] > 0:
                    r2 = await _call_and_parse(None, do_discovery=True)
                    await db.update_analysis_result(
                        analysis_id=analysis_id, case_id=case_id,
                        match=r2["match"], score=r2["score"],
                        excerpt=r2["excerpt"], reason=r2["reason"],
                    )
                    # R1 已加 match=1。若 R2 精讀後 score 退回 0，補一筆 -1 退回
                    # match_count；否則 0 不動。這樣 match_count 準確反映 R2 最終分數，
                    # 律師看「相關 N 筆」不會虛報。
                    r2_score = r2["score"] or 0
                    match_compensation = 0 if r2_score > 0 else -1
                    await db.increment_analysis_progress(
                        analysis_id, completed_delta=1, match_delta=match_compensation,
                    )
                    return r2
                # score=0 → skip Round 2，補 step 2 的 completed（進度條準確到 100%）
                await db.increment_analysis_progress(
                    analysis_id, completed_delta=1, match_delta=0,
                )
                return r1
            else:
                # 少量判決：直接完整 budget
                result = await _call_and_parse(None, do_discovery=True)
                await db.create_analysis_result(
                    analysis_id=analysis_id, case_id=case_id,
                    match=result["match"], score=result["score"],
                    excerpt=result["excerpt"], reason=result["reason"],
                )
                m_delta = 1 if result["score"] and result["score"] > 0 else 0
                await db.increment_analysis_progress(
                    analysis_id, completed_delta=1, match_delta=m_delta,
                )
                return result

        except Exception as exc:
            logger.warning("stage3_v2 case %s 失敗：%s", case_id, exc)
            # 把 exception summary 帶到 reason 欄 + SSE 讓 FE 能偵測 API 問題
            # （credit 不足 / API key 錯 / rate limit）並顯示友善 banner
            err_msg = str(exc)[:300]
            result = {"case_id": case_id, "match": "error", "score": None,
                      "excerpt": "", "reason": err_msg}
            try:
                await db.create_analysis_result(
                    analysis_id=analysis_id, case_id=case_id,
                    match="error", score=None, excerpt="", reason=err_msg,
                )
                # error 跳過兩輪，two_pass 模式加 2
                delta = 2 if use_two_pass else 1
                await db.increment_analysis_progress(analysis_id, completed_delta=delta)
            except Exception:
                pass
            return result

    async def _run_and_report(j: dict, idx: int) -> dict:
        # Graceful abort：律師按「中止並查看目前結果」→ 這筆跳過（不打 Claude、不寫 DB、
        # 不推 SSE），保持為 missing、resume 時由 already_done_ids 補抓。
        # 已進入 _analyze_auto 的 CONCURRENCY（8）筆讓它跑完，但新進來的都 short-circuit。
        from src.worker.runner import _is_graceful_abort
        if _is_graceful_abort(analysis_id):
            return {"match": "aborted", "score": None, "_aborted": True}
        result = await _analyze_auto(j, idx)
        async with _report_lock:
            batch_results.append(result)
            try:
                if on_batch_done:
                    # 第二參數帶當下累積的 scoring token 用量，給 runner 推 SSE 給律師看 live ticker
                    usage_so_far = {
                        "scoring_input": _total_input_tokens,
                        "scoring_output": _total_output_tokens,
                    }
                    await on_batch_done([result], usage_so_far)
            except Exception as sse_exc:
                logger.warning("on_batch_done SSE 推送失敗：%s", sse_exc)
        return result

    # 兩階段：每筆判決固定 2 步（screening + fullread 或 skip），total = 2*N
    # score=0 不進 Round 2，但立刻補 +1 completed（代表 skip），確保進度條準確到 100%
    # Streaming 模式下 completed 初值需對齊 two_pass（already_done × 2）
    if use_two_pass:
        new_total = total * 2
        if streaming_mode:
            # 修正 already_done 的 completed offset（caller 一開始可能設為 already_done_count，
            # 但 two_pass 每筆 = 2 step，需 × 2 才能與 workers 的 increment 對齊）
            await db.update_analysis(analysis_id, total=new_total)
        else:
            # DB-read 模式 caller 已把 completed 設為 already_done_count，需 × 2 對齊
            await db.update_analysis(
                analysis_id, total=new_total,
                completed=already_done_count * 2,
            )
        logger.info(
            "analysis_v2 %s 自動流轉模式 %d 筆（streaming=%s, screening %dK + auto-promote, total=%d steps）",
            analysis_id, workload_count, streaming_mode, SCREENING_BUDGET // 1000, new_total,
        )
    else:
        logger.info("analysis_v2 %s 單輪模式 %d 筆（streaming=%s）",
                    analysis_id, workload_count, streaming_mode)

    if streaming_mode:
        # Producer-consumer：CONCURRENCY 個 worker 持續從 queue 取判決，遇 None 結束。
        # idx 用全域 counter（給 keyword round-robin 用）。
        _idx_counter = 0
        _idx_lock = asyncio.Lock()

        async def _consumer_worker():
            nonlocal _idx_counter
            while True:
                item = await judgment_queue.get()
                try:
                    if item is None:
                        return
                    async with _idx_lock:
                        idx = _idx_counter
                        _idx_counter += 1
                    try:
                        await _run_and_report(item, idx)
                    except Exception as worker_exc:
                        logger.warning("consumer worker 處理 %s 異常：%s",
                                       item.get("case_id", "?"), worker_exc)
                finally:
                    judgment_queue.task_done()

        workers = [asyncio.create_task(_consumer_worker()) for _ in range(CONCURRENCY)]
        await asyncio.gather(*workers, return_exceptions=True)
    else:
        all_coros = [_run_and_report(j, i) for i, j in enumerate(judgments)]
        await asyncio.gather(*all_coros, return_exceptions=True)

    # match_count 從 DB 讀取（最準確，不依賴 in-memory batch_results）
    final_analysis = await db.get_analysis(analysis_id)
    match_count = (final_analysis or {}).get("match_count", 0)
    finished_at = datetime.now(timezone.utc).isoformat()
    await db.update_analysis(
        analysis_id, status="running",   # 仍在跑 synthesis，不標 done
        match_count=match_count, finished_at=finished_at,
    )
    logger.info("analysis_v2 %s 精讀完成，相關 %d/%d 筆，tokens in=%d out=%d",
                analysis_id, match_count, total, _total_input_tokens, _total_output_tokens)

    # 多 keyword variant discovery
    for kw, candidates in discovered_per_kw.items():
        if not candidates:
            continue
        try:
            await _persist_discovered_variants(kw, candidates)
        except Exception as exc:
            logger.warning("variant discovery %r 階段錯誤：%s", kw, exc)

    # 回傳 scoring usage 供 runner 合併到 synthesis
    return {
        "scoring_input": _total_input_tokens,
        "scoring_output": _total_output_tokens,
    }


async def run_analysis(
    analysis_id: str,
    task_id: str,
    question: str,
    ai_read_fields: list[str],
    on_batch_done: Callable[[list[dict]], Coroutine[Any, Any, None]] | None = None,
    api_key: str | None = None,
    discovery_keyword: str | None = None,
) -> None:
    """
    對 task_id 的所有 task_judgments 執行 Claude 精讀，結果寫入 analysis_results。

    on_batch_done: async callable(batch_results)，每批完成後呼叫，供 runner 推 SSE。
    discovery_keyword: 若提供且非條號引用，啟用 variant candidate 發現 — 聚合 Claude
        在每篇判決回傳的 variant_candidates，任務結束後用 MCP 做 corpus 驗證寫入
        synonym_dictionary（tier 由 corpus_hits 決定）。None 或條號 keyword 不啟用。
    """
    judgments = await db.get_task_judgments(task_id)
    total = len(judgments)

    await db.update_analysis(analysis_id, total=total, status="running")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    batch_results: list[dict] = []
    REPORT_EVERY = 15

    # 收集每篇判決回傳的 variant_candidates，key=variant string, value=出現次數
    discovered: dict[str, int] = {}

    async def analyze_one(judgment: dict) -> dict:
        case_id = judgment["case_id"]
        field_text, field_label = _get_field_text(judgment, ai_read_fields)

        # Semaphore 只保護 Claude API 呼叫，不涵蓋 DB 寫入
        async with semaphore:
            try:
                parsed = await _call_claude(
                    case_id, field_text, field_label, question,
                    keyword=discovery_keyword or question,
                    api_key=api_key,
                )
                match = parsed.get("match", "no")
                score = int(parsed.get("score", 0))
                excerpt = parsed.get("excerpt", "")
                reason = parsed.get("reason", "")
                # 抽取 variant_candidates（若 Claude 有回）
                candidates = parsed.get("variant_candidates") or []
                if isinstance(candidates, list):
                    for c in candidates:
                        cs = str(c).strip()
                        if cs and cs != (discovery_keyword or ""):
                            discovered[cs] = discovered.get(cs, 0) + 1
            except Exception:
                match, score, excerpt, reason = "error", None, "", ""

        # Semaphore 釋放後再寫 DB，不佔用 Claude 並行名額
        await db.create_analysis_result(
            analysis_id=analysis_id,
            case_id=case_id,
            match=match,
            score=score,
            excerpt=excerpt,
            reason=reason,
        )
        match_delta = 1 if match in ("yes", "partial") else 0
        await db.increment_analysis_progress(
            analysis_id, completed_delta=1, match_delta=match_delta
        )
        return {
            "case_id": case_id,
            "match": match,
            "score": score,
            "excerpt": excerpt,
            "reason": reason,
        }

    tasks_coros = [analyze_one(j) for j in judgments]
    completed = 0

    # 分批送出並回報進度
    for i in range(0, total, REPORT_EVERY):
        batch_coros = tasks_coros[i: i + REPORT_EVERY]
        results = await asyncio.gather(*batch_coros, return_exceptions=False)
        completed += len(results)
        batch_results.extend(results)

        if on_batch_done:
            await on_batch_done(list(results))

        logger.debug("analysis %s：%d/%d 完成", analysis_id, completed, total)

    match_count = sum(1 for r in batch_results if r["match"] in ("yes", "partial"))
    finished_at = datetime.now(timezone.utc).isoformat()
    await db.update_analysis(
        analysis_id,
        status="done",
        match_count=match_count,
        finished_at=finished_at,
    )
    logger.info("analysis %s 完成，共 %d 筆命中", analysis_id, match_count)

    # 精讀發現：任務結束後把 Claude 找到的 variant_candidates 寫入字典
    # 只有 discovery_keyword 不為空且為非條號時才啟用（條號走規則展開，不需 discovery）
    if discovery_keyword and discovered:
        try:
            from src.pipeline.citation_normalizer import parse_keyword
            if parse_keyword(discovery_keyword) is None:
                # 非條號 → 啟用 synonym discovery
                await _persist_discovered_variants(discovery_keyword, discovered)
        except Exception as exc:
            logger.warning("variant discovery 階段錯誤：%s", exc)


async def _persist_discovered_variants(
    canonical: str,
    candidates_count: dict[str, int],
) -> None:
    """把精讀發現的變體候選寫入 synonym_dictionary，**強制設為 candidate tier**。

    設計決策（重要）：
      即使 Claude 指出某詞是 variant 且 corpus 有大量命中（≥50），我們仍不自動升
      confirmed。因為實測顯示 Claude 會把「雇主」「勞僱契約」這種**不同法律概念**
      誤判為「僱傭」的 variant，這些詞在判決書中當然高頻，但拿它去自動展開搜尋是錯的。
      所以 discovery 路徑只產生 `candidate`，由律師在 UI preview 時 ✓ 確認過
      達到 _AUTO_PROMOTE_ACCEPTS 次數才會升級 confirmed 並影響搜尋展開。
      律師如果看到 `雇主` 這種 candidate，直接按 × 就會進入 rejected，不再出現。

    corpus_hits 仍會 verify + 記錄，但只作為 UI 顯示的參考（讓律師看到「這詞在判決書中有多普遍」），
    不用它決定 tier。
    """
    from src import mcp_client
    logger.info(
        "精讀發現 %d 個 variant candidates for %r，開始 corpus 驗證",
        len(candidates_count), canonical,
    )

    # 過濾：排除之前被律師拒絕過的 variant（不再推薦）
    filtered_candidates = {}
    for variant, count in candidates_count.items():
        # 不推薦單字 variant（搜尋不會只搜一個字）
        if len(variant) <= 1:
            continue
        # 查 DB 是否曾被拒絕
        existing = await db.get_synonyms(canonical)
        rejected = any(r["variant"] == variant and r["tier"] == "rejected" for r in existing)
        if rejected:
            continue
        filtered_candidates[variant] = count
    candidates_count = filtered_candidates
    if not candidates_count:
        logger.info("所有 discovered variants 已被過濾（拒絕 / 單字法規），跳過")
        return

    corpus_hits_map: dict[str, int] = {}
    for variant, _in_task_count in candidates_count.items():
        try:
            hits = await mcp_client.count_judgments(variant)
            if hits is not None:
                corpus_hits_map[variant] = hits
        except Exception as exc:
            logger.warning("corpus verify %r 失敗：%s", variant, exc)

    # 直接寫入，tier 由 upsert_synonyms 依 corpus_hits 判定 — 但我們馬上覆蓋為 candidate
    await db.upsert_synonyms(
        canonical=canonical,
        variants=list(candidates_count.keys()),
        source="discovered",
        corpus_hits_map=corpus_hits_map,
        discovery_count_delta=candidates_count,
    )
    # 強制 discovered 來源的都設為 candidate（除非之前已被律師 accept 成 confirmed）
    from src.db.database import _conn, _now
    async with _conn() as conn:
        for variant in candidates_count:
            await conn.execute(
                """
                UPDATE synonym_dictionary
                   SET tier = 'candidate'
                 WHERE canonical = ? AND variant = ?
                   AND source = 'discovered'
                   AND accept_count = 0
                   AND reject_count < 2
                """,
                (canonical, variant),
            )
        await conn.commit()

    logger.info(
        "discovered variants 寫入完成（全部 tier=candidate 等律師確認）：%s",
        {v: {"hits": corpus_hits_map.get(v), "in_task": c}
         for v, c in candidates_count.items()},
    )
