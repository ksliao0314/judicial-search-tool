"""自然語言描述 → 搜尋策略（LLM 拆解，~1 秒同步回傳 2-3 個策略）。"""
import logging

import anthropic

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
你是一位台灣法律資料庫搜尋專家，協助律師將自然語言查詢轉換為結構化搜尋策略。
回答時只輸出 JSON，不加任何其他文字。
"""

STRATEGY_PROMPT = """\
律師想找的判決情境如下：

{query}

請設計 2 到 3 個不同的搜尋策略，涵蓋不同的關鍵字選擇與欄位組合。
策略之間應有差異（精準 / 擴大 / 含背景脈絡），互補而不重複。

可用的 filter_field（字串過濾欄位）：
- reasoning（理由）
- main_text（主文）
- facts（事實）
- cited_statutes（引用法條，列表精確比對，適合法條號碼）
- full_text（完整判決原文；當 MCP 對簡易判決的分段不穩時，用它當穩健備援以避免漏案）

可用的 ai_read_field（送給 AI 精讀的欄位，可與 filter_field 不同）：
- reasoning
- main_text
- facts
- cited_statutes
- full_text（精讀成本較高，只在確實需要跨段對照整份判決時才選）

#### 台灣判決結構知識（決定欄位時請參考）

1. **民事/行政「事實及理由」合併段**：絕大多數民事判決與行政判決把事實與理由合併為一段（標題即「事實及理由」），MCP 將此整段歸入 reasoning，**facts 欄位通常為空**。律師若想找「被告辯稱 / 原告主張 / 案件背景」這類事實情境，在民事/行政案上**應改勾 reasoning 而非 facts**。
2. **刑事判決**：facts 欄位較常有內容（犯罪事實段獨立），此時 facts 過濾才有意義。但要注意簡易判決（交通裁決、易科罰金等）也常合併段，facts 仍可能空。
3. **cited_statutes 是列表精確比對**：找特定法條號碼（如「民法第184條」「行政罰法第7條」）時優先用 cited_statutes，比 reasoning 字串比對更精準（不會被「本件與第7條無關」這種否定句誤判）。
4. **最高法院 / 最高行政法院**：判決段落較長（單一論點段可能 1000+ 字），AI 精讀單次 input tokens 較高；若主要打這層級的案，cost 估算要拉高。
5. **main_text 適合判決結果**：找「撤銷原處分」「免罰」「駁回」「給付 N 元」等結果論時，main_text 命中率比 reasoning 高且精準。

只回傳以下 JSON 格式，不加任何說明：
{{
  "strategies": [
    {{
      "name": "策略名稱（5字以內）",
      "keywords": ["關鍵字1", "關鍵字2"],
      "filter_fields": ["reasoning"],
      "ai_read_fields": ["reasoning"],
      "description": "此策略的搜尋邏輯說明（30字以內）",
      "recall_estimate": "召回預估（較少相關度高 / 較多涵蓋廣 / 最廣含背景）"
    }}
  ]
}}
"""


async def decompose_query(query: str, api_key: str | None = None) -> list[dict]:
    """
    呼叫 Claude 將自然語言拆解為 2-3 個搜尋策略。
    回傳 list of strategy dict。
    """
    client = _get_client(api_key)
    prompt = STRATEGY_PROMPT.format(query=query)

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )

    data = extract_json(response.content[0].text)
    strategies = data.get("strategies", [])
    logger.info("策略拆解完成，共 %d 個策略", len(strategies))
    return strategies
