"""Excerpt 品質異常自動記錄到 JSONL 檔（data/excerpt_anomalies.jsonl）。

V2 prompt 要求 Claude 從「法院判斷段落」挑 excerpt、不要挑主文或當事人主張。
本 log 監測 Claude 是否聽話、不自動 re-pick（對 excerpt 的影響次級、錢成本優先）。

累積一週後 jq 統計：
    jq -r '.kind' data/excerpt_anomalies.jsonl | sort | uniq -c | sort -rn
    若 party_claim_prefix / main_text_leak 比例 > 10% → 考慮加後處理 re-pick
    若 empty_but_scored 比例高 → prompt 太嚴、考慮放寬

偵測類型：
    party_claim_prefix     — excerpt 開頭是「原告主張/被告抗辯/...」等當事人用語
    main_text_leak         — excerpt 是 main_text 的完整子字串（主文洩漏）
    empty_but_scored       — excerpt 為空但 score > 0（可能 prompt 過嚴、over-correct）
    charge_imposition_leak — excerpt 含刑事判決「論罪/論罪科刑/罪數/量刑」典型語句
                             （「核被告X所為，均係犯刑法第Y條」「應依...論處」等）
                             這是構成要件成立後的 downstream 決定、不是構成要件認定本身
    excerpt_not_in_source  — LLM excerpt 在快取判決原文中定位不到（捏造 / 抓錯文件 /
                             快取殘缺）。A1 診斷副產品：寬鬆比對（NFKC + 去標點 + 省略號
                             分段 + 連續片段），coverage < 0.4 才報，誤報極低（1924 筆實測
                             僅 1 筆 = 110訴更一23，case_id 是判決但快取到 112 年裁定）。

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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# judgment-search/src/utils/excerpt_anomaly_log.py → judgment-search/data/
_LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "excerpt_anomalies.jsonl"

_write_lock = asyncio.Lock()

# 當事人主張開頭 pattern（含 [理由] 等 label prefix 跳過）
# 覆蓋所有程序當事人變體：
#   民事 / 行政第一審：原告、被告、參加人
#   上訴 / 抗告：上訴人、被上訴人、抗告人
#   聲請：聲請人、相對人
#   再審：再審原告、再審被告
#   刑事：自訴人、告訴人、反訴原告、反訴被告
#   其他：選定當事人
_PARTY_CLAIM_PREFIX_RE = re.compile(
    r'^\s*(?:\[[^\]]+\]\s*)?'
    r'(?:原告|被告|上訴人|被上訴人|抗告人|聲請人|相對人|參加人'
    r'|再審原告|再審被告|自訴人|告訴人|反訴原告|反訴被告|選定當事人)'
    r'(?:[^，。；\n]{0,10})?'
    r'(?:主張|起訴|略以|抗辯|答辯|辯稱|陳述|陳稱|聲明|指稱|質疑|爭執|訴稱|聲請|求為)'
)

# 刑事判決「論罪 / 論罪科刑 / 罪數 / 量刑」典型語句
# 這些是構成要件成立後的 downstream 決定、不該作為「法院如何認定構成要件」的 excerpt
# 規則：excerpt 內任何位置出現任一 pattern 都算命中（不限開頭）
# Pattern 設計以「關鍵 token 組合」為主、允許中間插入任意字（被告名單等）
_CHARGE_IMPOSITION_PATTERNS = [
    # 論罪：X 所為，（均）係犯（刑法/本條例）/ 應依X條論處 / 論以X罪
    # 「所為、均係犯」「所為、係犯」「係犯刑法第X條」都是論罪定番
    re.compile(r'所為[^\n]{0,5}?(?:均)?(?:係|為)犯'),
    re.compile(r'均(?:係|為)犯(?:刑法|本條例|[^\n。]{2,8}?(?:法|條例))'),
    re.compile(r'應依[^\n。]{0,30}?(?:刑法|本條例)第[\u4e00-\u9fff\d]{1,8}條[^\n。]{0,30}?論處'),
    re.compile(r'論以[^\n。]{0,20}?(?:一|二|三|X|\d)?罪'),
    # 罪數：應以一罪論 / 數罪併罰 / 想像競合 / 從一重論處 / 接續犯
    re.compile(r'應以[^\n]{0,5}?一罪論'),
    re.compile(r'數罪併罰'),  # 極特定刑法術語、單獨出現即屬論罪段
    re.compile(r'想像競合犯'),
    re.compile(r'從一重(?:罪)?(?:論處|處斷)'),
    # 量刑：爰以行為人之責任 / 爰審酌被告 / 爰依刑法第57條 / 量處X刑
    re.compile(r'爰(?:以行為人之責任|審酌|依)'),
    re.compile(r'量處.{0,10}?(?:有期徒刑|拘役|罰金)'),
]

# ── excerpt_not_in_source（A1 canary）─────────────────────────────────────
# 寬鬆比對，刻意偏向「定位得到」以壓低誤報：normalize（NFKC + 台→臺 + 去空白/標點）後，
# 把 excerpt 依省略號分段，逐段檢查是否為快取原文子字串；coverage < 0.4 才報。
_ZW_CHARS = "​‌‍﻿⁠"
_ELLIPSIS_RE = re.compile(r'(?:\.{2,}|。{2,}|[…⋯]+)')
_LABEL_PREFIX_RE = re.compile(r'^\s*\[[^\]]+\]\s*')
_MATCH_STRIP_RE = re.compile(
    r'[\s　，。：；！？、（）()「」『』〔〕\[\]【】,.:;!?~～\-—…⋯"\'`*_／/]')
_FRAG_COVERAGE_THRESHOLD = 0.4   # < 0.4 視為定位不到（A1 實測：真案例 coverage=0.0）
_FRAG_MIN_LEN = 6                 # 太短的片段不足以當證據，跳過


def _norm_for_match(s: str) -> str:
    if not s:
        return ''
    s = unicodedata.normalize('NFKC', s)
    s = ''.join(ch for ch in s if ch not in _ZW_CHARS)
    s = s.replace('　', '').replace('台', '臺')
    return _MATCH_STRIP_RE.sub('', s)


def _excerpt_locates(excerpt: str, source_text: str) -> tuple[bool, float]:
    """excerpt 是否定位得到於 source_text。回 (located, coverage)。

    保守設計：source 為空、或 excerpt 無實質片段時回 (True, 1.0)（不報異常）。
    """
    src = _norm_for_match(source_text)
    if not src:
        return (True, 1.0)
    body = _LABEL_PREFIX_RE.sub('', excerpt or '').strip()
    total = matched = 0.0
    for frag in _ELLIPSIS_RE.split(body):
        nf = _norm_for_match(frag)
        if len(nf) < _FRAG_MIN_LEN:
            continue
        total += len(nf)
        if nf in src:
            matched += len(nf)
        elif len(nf) >= 8 and any(nf[i:i + 8] in src for i in range(len(nf) - 7)):
            matched += len(nf) * 0.5   # 真片段嵌進自己措辭 → 半分，不算純捏造
    if total == 0:
        return (True, 1.0)
    cov = matched / total
    return (cov >= _FRAG_COVERAGE_THRESHOLD, cov)


def detect_anomaly_kinds(
    *, excerpt: str, score: int | None, main_text: str | None,
    source_text: str | None = None,
) -> list[str]:
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

    # charge_imposition_leak: excerpt 含刑事論罪 / 罪數 / 量刑典型語句
    if excerpt_clean and any(p.search(excerpt_clean) for p in _CHARGE_IMPOSITION_PATTERNS):
        kinds.append('charge_imposition_leak')

    # excerpt_not_in_source: LLM excerpt 在快取判決原文中定位不到（A1 canary）
    # 只在有提供 source_text + excerpt 非空時檢查；保守（coverage < 0.4 才報）
    if excerpt_clean and source_text:
        located, _cov = _excerpt_locates(excerpt_clean, source_text)
        if not located:
            kinds.append('excerpt_not_in_source')

    return kinds


async def log_excerpt_anomaly(
    *,
    case_id: str,
    analysis_id: str,
    score: int | None,
    excerpt: str,
    main_text: str | None = None,
    source_text: str | None = None,
) -> None:
    """偵測 + 寫 JSONL。失敗吞掉（不影響 scoring pipeline）。

    source_text：快取的判決原文（reasoning / full_text 等），供 excerpt_not_in_source
    定位檢查用；不傳則跳過該項檢查（向後相容）。
    """
    try:
        kinds = detect_anomaly_kinds(
            excerpt=excerpt, score=score, main_text=main_text, source_text=source_text,
        )
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
