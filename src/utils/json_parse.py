"""Claude 回應的 JSON 解析工具。

Claude 偶爾會在 JSON 外包一層 markdown code block（```json ... ```）。
extract_json 統一處理這種情況，使用正則比字串 split 更不容易被邊界案例誤解析。
"""
import json
import re


_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```", re.IGNORECASE)


def extract_json(text: str) -> dict | list:
    """
    從 Claude 回應文字中解析出 JSON。

    處理情形（按嘗試順序）：
    1. 純 JSON 文字（最常見）
    2. markdown code block 包住的 JSON（```json ... ``` 或 ``` ... ```）
    3. JSON 前後有說明文字 — 找第一個 { 或 [ 到最後一個 } 或 ]

    若解析失敗則拋出 json.JSONDecodeError。
    """
    stripped = text.strip()

    # 先嘗試直接解析（最快速路徑）
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 嘗試從 code block 提取
    match = _CODE_BLOCK_RE.search(stripped)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 嘗試找 JSON 的首尾花括號 / 方括號
    for open_ch, close_ch in [('{', '}'), ('[', ']')]:
        first = stripped.find(open_ch)
        last = stripped.rfind(close_ch)
        if first != -1 and last > first:
            try:
                return json.loads(stripped[first:last + 1])
            except json.JSONDecodeError:
                pass

    # 無法解析，重新拋出原始錯誤
    return json.loads(stripped)  # 這次一定會拋出 JSONDecodeError
