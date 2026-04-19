"""同義詞展開：LLM call + cache + corpus verification。

用途：律師輸入非法條式 keyword（如「僱傭」「優先承買權」「信賴保護」）時，
展開為異體字 / 簡寫 / 錯字變形等變體，增加搜尋覆蓋率。

L1 事務所資產策略：每次展開結果都寫進 `synonym_dictionary` 表，並用 MCP
對每個 variant 做一次 search 統計 `corpus_hits`。長期累積出高品質詞表。

避免對條文 keyword 使用這個模組 —— 那些走 CitationNormalizer 的規則展開。
由 pipeline 層判斷：若 CitationNormalizer.parse_keyword 成功則走 citation 路徑；
否則走 synonym 路徑。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from src import mcp_client
from src.db import database as db
from src.utils.json_parse import extract_json

logger = logging.getLogger(__name__)

_default_client: anthropic.AsyncAnthropic | None = None


def _get_client(api_key: str | None = None) -> anthropic.AsyncAnthropic:
    if api_key:
        return anthropic.AsyncAnthropic(api_key=api_key)
    global _default_client
    if _default_client is None:
        _default_client = anthropic.AsyncAnthropic()
    return _default_client


SYSTEM_PROMPT = """\
你是台灣法律領域的術語同義字助理。律師給你一個法律或實務用語 keyword，
列出它在判決書中可能的所有同義寫法。只輸出 JSON array 字串，原 keyword 必在內。

**可以**展開的類別：
1. 異體字 / 繁簡變體（僱/雇、傭/佣/庸、蹟/績、複/覆）
2. 法律用語的不同寫法（合約/契約、優先承買權/優先承購權）
3. 法條名稱的正式全名 ↔ 常用簡稱（勞動基準法/勞基法）
4. 判決書常見錯字變形（古蹟/古績）
5. 習慣性縮寫（財務報表/財報、建築物/建物）

**絕對不要**展開的類別：
- 法律意義不同的概念（撤銷 ≠ 廢止、不予處罰 ≠ 免罰、授益處分 ≠ 負擔處分、
  僱傭 ≠ 僱用 ≠ 勞務契約，這些是不同法律概念或不同層級）
- 英文、中英混搭、注音、任何非繁體中文寫法
- 你不 100% 確定真的出現在台灣判決書中的詞
- 近義但意義有細微差別的詞（語感近 ≠ 同義）
- 上位或下位概念（契約 ≠ 買賣契約）

寧可少一個不展開，不要多加不該有的。最多 8 個。

範例：
輸入「合約」→ ["合約", "契約"]
輸入「優先承買權」→ ["優先承買權", "優先承購權", "優先購買權"]
輸入「勞基法」→ ["勞動基準法", "勞基法"]
輸入「僱傭」→ ["僱傭", "雇傭", "僱庸", "雇佣"]
輸入「古蹟」→ ["古蹟", "古績", "古跡"]
"""


async def _call_claude_expand(keyword: str, api_key: str | None = None) -> list[str]:
    """呼叫 Claude 展開，回傳 variant list。"""
    client = _get_client(api_key)
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": f"輸入「{keyword}」"}],
    )
    raw = response.content[0].text.strip()
    try:
        data = extract_json(raw)
        if isinstance(data, list):
            variants = [str(v).strip() for v in data if str(v).strip()]
            # 確保 keyword 本身在內
            if keyword not in variants:
                variants = [keyword] + variants
            return variants
    except Exception as exc:
        logger.warning("Claude 展開 %s 解析失敗：%s raw=%r", keyword, exc, raw[:200])
    return [keyword]  # fallback：只回原 keyword


async def _verify_with_corpus(variant: str) -> int:
    """用 MCP search 對 variant 做 count 驗證。回傳命中判決數（可能 0 = 幻覺）。

    失敗時回 -1（區分「未驗證」vs「驗證為 0」）。
    """
    try:
        hits = await mcp_client.search_judgments(keyword=variant, max_results=1)
        # MCP 不回 total_count 就看 list 是否非空；空 = 0；有至少一個 = ≥1
        # 實際 MCP 回傳 response.total_count，我們這裡 hits 已是 list
        return 1 if hits else 0
    except Exception as exc:
        logger.warning("corpus 驗證 %r 失敗：%s", variant, exc)
        return -1


# ---------------------------------------------------------------------------
# 主要 API
# ---------------------------------------------------------------------------

async def expand(
    keyword: str,
    api_key: str | None = None,
    use_cache: bool = True,
    verify_corpus: bool = True,
    only_confirmed: bool = False,
) -> dict:
    """展開 keyword 為同義詞 variant list，並寫入字典累積。

    only_confirmed:
        True — 只回傳 tier='confirmed' 的 variants（給搜尋 pipeline 自動展開用）。
               若字典裡沒任何 confirmed，會 fallback 呼叫 Claude 展開（首次搜尋該 keyword）。
        False — 回傳所有 tier（給 UI preview，讓律師看到 candidates / likely_typo 做決定）。

    回傳 {
        "canonical": str,
        "variants": [...],
        "from_cache": bool,
    }
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"canonical": "", "variants": [], "from_cache": False}

    # 優先讀字典 cache
    if use_cache:
        min_tier = "confirmed" if only_confirmed else None
        existing = await db.get_synonyms(keyword, min_tier=min_tier)
        if existing:
            # 字典有資料 → 僅遞增 usage_count，不重展開
            variant_names = [r["variant"] for r in existing]
            # Ensure keyword 本身在列表最前（早期 cache 可能漏）
            if keyword not in variant_names:
                existing = [{"variant": keyword, "corpus_hits": None, "accept_count": 0,
                             "reject_count": 0, "source": "cache_autoadd", "tier": "confirmed",
                             "discovery_count": 0, "usage_count": 0,
                             "first_seen_at": "", "last_used_at": ""}] + existing
                variant_names = [keyword] + variant_names
            await db.upsert_synonyms(
                canonical=keyword,
                variants=variant_names,
                source="cache",
            )
            return {
                "canonical": keyword,
                "variants": existing,
                "from_cache": True,
            }

    # only_confirmed 模式下字典沒有 confirmed 記錄 → 不 fallback LLM，直接用原字
    if only_confirmed:
        return {
            "canonical": keyword,
            "variants": [{"variant": keyword, "corpus_hits": None, "accept_count": 0,
                          "reject_count": 0, "source": "original", "tier": "confirmed",
                          "discovery_count": 0, "usage_count": 0,
                          "first_seen_at": "", "last_used_at": ""}],
            "from_cache": False,
        }

    # Cache miss → Claude 展開（僅在 only_confirmed=False 時，即 UI preview 模式）
    logger.info("展開 keyword=%r (Claude)", keyword)
    variants = await _call_claude_expand(keyword, api_key=api_key)

    # Corpus verification（對每個 variant 做小規模 MCP search）
    corpus_hits_map: dict[str, int] = {}
    if verify_corpus:
        for v in variants:
            count = await _verify_with_corpus(v)
            if count >= 0:
                corpus_hits_map[v] = count

    # 寫入字典
    await db.upsert_synonyms(
        canonical=keyword,
        variants=variants,
        source="claude",
        corpus_hits_map=corpus_hits_map,
    )

    # 組回應
    result_variants = []
    for v in variants:
        result_variants.append({
            "variant": v,
            "corpus_hits": corpus_hits_map.get(v),
            "accept_count": 0,
            "reject_count": 0,
            "source": "claude",
        })

    return {
        "canonical": keyword,
        "variants": result_variants,
        "from_cache": False,
    }


async def record_feedback(canonical: str, variant: str, accepted: bool) -> None:
    """律師在 UI 點擊「✓」或「×」時呼叫。"""
    await db.record_synonym_feedback(canonical, variant, accepted)
