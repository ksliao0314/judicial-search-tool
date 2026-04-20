"""Excerpt 品質異常自動記錄到 JSONL 檔（data/excerpt_anomalies.jsonl）。

V2 prompt 要求 Claude 從「法院判斷段落」挑 excerpt、不要挑主文或當事人主張。
本 log 監測 Claude 是否聽話、不自動 re-pick（對 excerpt 的影響次級、錢成本優先）。

累積一週後 jq 統計：
    jq -r '.kind' data/excerpt_anomalies.jsonl | sort | uniq -c | sort -rn
    若 party_claim_prefix / main_text_leak 比例 > 10% → 考慮加後處理 re-pick
    若 empty_but_scored 比例高 → prompt 太嚴、考慮放寬

偵測類型：
    party_claim_prefix — excerpt 開頭是「原告主張/被告抗辯/...」等當事人用語
    main_text_leak    — excerpt 是 main_text 的完整子字串（主文洩漏）
    empty_but_scored  — excerpt 為空但 score > 0（可能 prompt 過嚴、over-correct）

JSONL 格式：每行一個 dict
    {
      "ts": "2026-04-20T...",
      "kind": "party_claim_prefix",
      "case_id": "105年度訴字第1766號",
      "analysis_id": "...",
      "score": 9,
      "excerpt_preview": "原告主張略以：(一)按文資法第12條..."
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# judgment-search/src/utils/excerpt_anomaly_log.py → judgment-search/data/
_LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "excerpt_anomalies.jsonl"

_write_lock = asyncio.Lock()

# 當事人主張開頭 pattern（含 [理由] 等 label prefix 跳過）
_PARTY_CLAIM_PREFIX_RE = re.compile(
    r'^\s*(?:\[[^\]]+\]\s*)?'
    r'(?:原告|被告|上訴人|被上訴人|抗告人|聲請人|再審原告|再審被告)'
    r'(?:[^，。；]{0,10})?'
    r'(?:主張|起訴|略以|抗辯|答辯|辯稱|陳述|聲明|指稱|質疑|爭執)'
)


def detect_anomaly_kinds(*, excerpt: str, score: int | None, main_text: str | None) -> list[str]:
    """回傳此 excerpt 觸發的 anomaly kind list（空 list = 無異常）。同步 function、純 string check。"""
    kinds: list[str] = []
    excerpt_clean = (excerpt or '').strip()

    # party_claim_prefix: 開頭匹配當事人主張
    if excerpt_clean and _PARTY_CLAIM_PREFIX_RE.match(excerpt_clean):
        kinds.append('party_claim_prefix')

    # main_text_leak: excerpt（剝 label prefix 後）是 main_text 的子字串
    if excerpt_clean and main_text:
        ex_no_label = re.sub(r'^\[[^\]]+\]\s*', '', excerpt_clean).strip()
        mt_clean = main_text.strip()
        # 只有 excerpt 明確長度 > 10 + 整段落在 main_text 內才算（避免極短詞誤判）
        if len(ex_no_label) > 10 and ex_no_label in mt_clean:
            kinds.append('main_text_leak')

    # empty_but_scored: score > 0 但 excerpt 空（可能 prompt 太嚴）
    if score is not None and score > 0 and not excerpt_clean:
        kinds.append('empty_but_scored')

    return kinds


async def log_excerpt_anomaly(
    *,
    case_id: str,
    analysis_id: str,
    score: int | None,
    excerpt: str,
    main_text: str | None = None,
) -> None:
    """偵測 + 寫 JSONL。失敗吞掉（不影響 scoring pipeline）。"""
    try:
        kinds = detect_anomaly_kinds(excerpt=excerpt, score=score, main_text=main_text)
        if not kinds:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kinds[0] if len(kinds) == 1 else ','.join(kinds),
            "kinds": kinds,
            "case_id": case_id,
            "analysis_id": analysis_id,
            "score": score,
            "excerpt_preview": (excerpt or '')[:120].replace('\n', ' '),
        }
        async with _write_lock:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as exc:
        logger.warning("excerpt anomaly log 寫入失敗: %s", exc)
