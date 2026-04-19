"""Parser 異常自動記錄到 JSONL 檔（data/parser_anomalies.jsonl）。

每次 stage 2.5 fetch 完一筆判決後檢查結構訊號；若偵測到 anomaly，append 一行
JSON 到記錄檔，供日後分析（哪種法院 / 年份 / 案由最常出問題）。

非阻塞、容忍寫入失敗（log warning 但不拋例外，不影響 fetch pipeline）。
寫入用 asyncio.Lock 序列化，避免 8 並行 fetch 時穿插寫入造成壞行。

JSONL 格式：每行一個 dict
    {
      "ts": "2026-04-17T10:30:00+00:00",
      "case_id": "...",
      "court": "最高行政法院",
      "jid": "TPAA,110,上,74,20220413,1",
      "task_id": "...",
      "anomaly_types": ["empty_main_text", "long_line_no_marker"],
      "metrics": {"reasoning_len": 23612, "max_line_chars": 2029, ...}
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 寫入到 judgment-search/data/parser_anomalies.jsonl
# (judgment-search/src/utils/anomaly_log.py → judgment-search/data/)
_LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "parser_anomalies.jsonl"

_write_lock = asyncio.Lock()

# 偵測閾值
_EMPTY_REASONING_THRESHOLD = 100   # reasoning 去空白後 < 100 字 → 嚴重
_EMPTY_MAIN_TEXT_THRESHOLD = 10
_ALL_FIELDS_EMPTY_THRESHOLD = 50   # 三個都 < 50 字 → parse 完全失敗
_LONG_LINE_THRESHOLD = 500         # 單一行 > 500 字（最高行 .htmlcontent 類問題）
_QUOTE_IMBALANCE_THRESHOLD = 5     # |open - close| > 5 → OCR / parse 異常

# 行首 outline marker 偵測（與 parser 規則同步）
# 數字類：Python 3 re 的 \d 預設含 Unicode digit（含全形 １-９），這邊不用特別處理
# 數字類；但分隔符必須涵蓋 . 、 ． 三種 — 判決書常用 `１、` 或 `1.`（臺北高行 107
# 全字 69 號即用 ５、全形 ＋ 、），之前只擋 `\.` 漏抓 CJK 逗號 `、` 的 case。
_LINE_LEAD = [
    re.compile(r"^[壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸][、，]"),  # L0
    re.compile(r"^[一二三四五六七八九十]{1,4}、"),
    re.compile(r"^[\u3220-\u3229]"),
    re.compile(r"^[（(][一二三四五六七八九十]{1,3}[）)]"),
    re.compile(r"^[\u2488-\u249B]"),
    re.compile(r"^\d{1,2}[.、．](?!\d)"),
    re.compile(r"^[\u2474-\u2487]"),
    re.compile(r"^[（(]\d{1,2}[）)]"),
    re.compile(r"^[\u2460-\u2473]"),
]

_CITED_REF_IN_TEXT = re.compile(r"第\d{1,4}條")


def _has_outline_marker(line: str) -> bool:
    s = line.lstrip()
    return any(p.match(s) for p in _LINE_LEAD)


# ── Outline number-gap detector ─────────────────────────────────────────
# 判決正式的 outline 一定從 1 開始連續。若 parser 把非 outline（例如引用條號、
# 法規說明）誤當成 marker，多半會出現「從非 1 起」或「跳號」的序列；反之亦然，
# 真 outline 起點被 parser 漏掉（可能因為落在 quote 裡、boundary check 拒絕等）
# 也會呈現「從中段起」。兩者都是結構解析錯誤的強訊號，適合送進 anomaly log。
#
# 刻意只掃 L1 / L2 / L3（最常出現結構意義的三層）；L0 在判決中很少、L4/L5 過短
# 雜訊太多。序列 < 2 個 marker 不判斷（無法偵測 gap）。
_L1_MARKER_HEAD = re.compile(r"^([一二三四五六七八九十百零〇]{1,4})[、，.．]")
_L2_ENCLOSED_HEAD = re.compile(r"^([\u3220-\u3229])")
_L2_PAREN_HEAD = re.compile(r"^[（(]([一二三四五六七八九十百零〇]{1,3})[）)]")
_L3_ENCLOSED_HEAD = re.compile(r"^([\u2488-\u249B])")
_L3_ARABIC_HEAD = re.compile(r"^([\d\uFF10-\uFF19]{1,2})[.．](?![\d\uFF10-\uFF19])")

_CJK_NUM_BASIC = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_cjk_num(s: str) -> int | None:
    """CJK 數字（1-99）→ int。超出範圍或不認識的字 return None。"""
    if not s:
        return None
    if s in _CJK_NUM_BASIC:
        return _CJK_NUM_BASIC[s]
    # 十一 ~ 十九
    if len(s) == 2 and s[0] == "十" and s[1] in _CJK_NUM_BASIC:
        return 10 + _CJK_NUM_BASIC[s[1]]
    # 二十 ~ 九十
    if len(s) == 2 and s[1] == "十" and s[0] in _CJK_NUM_BASIC and s[0] != "十":
        return _CJK_NUM_BASIC[s[0]] * 10
    # 二十一 ~ 九十九
    if len(s) == 3 and s[1] == "十" and s[0] in _CJK_NUM_BASIC and s[2] in _CJK_NUM_BASIC:
        return _CJK_NUM_BASIC[s[0]] * 10 + _CJK_NUM_BASIC[s[2]]
    return None


def _parse_fullwidth_arabic(s: str) -> int | None:
    """含全形數字 `０１２…９` 的字串 → int；失敗 return None。"""
    try:
        return int(s.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    except (ValueError, TypeError):
        return None


def _parse_line_marker(line: str) -> tuple[str, int] | None:
    """Line 行首是否為 outline marker？返回 (layer, num) 或 None。"""
    s = line.lstrip()
    if not s:
        return None
    if m := _L1_MARKER_HEAD.match(s):
        n = _parse_cjk_num(m.group(1))
        if n is not None:
            return ("L1", n)
    if m := _L2_ENCLOSED_HEAD.match(s):
        return ("L2", ord(m.group(1)) - 0x3220 + 1)
    if m := _L2_PAREN_HEAD.match(s):
        n = _parse_cjk_num(m.group(1))
        if n is not None:
            return ("L2", n)
    if m := _L3_ENCLOSED_HEAD.match(s):
        return ("L3", ord(m.group(1)) - 0x2488 + 1)
    if m := _L3_ARABIC_HEAD.match(s):
        n = _parse_fullwidth_arabic(m.group(1))
        if n is not None:
            return ("L3", n)
    return None


def _find_outline_number_gaps(reasoning: str) -> dict[str, dict]:
    """偵測 outline marker 序列的「parser 跑偏」強信號。

    ## 背景

    真實判決的 L1/L2/L3 序列本來就不會從頭到尾連續 1-N —— 判決常分多個 section
    （事實、理由、本院認定），每個 section 各自從 `一、二、三、` 起編，產出像
    `[1,2,3,4, 1,1,2, 1,2,3,4,5]` 的多區段序列。若把全文當單一序列偵測 gap，
    會對正常判決誤報（實測 5 件 fixture 4 件會被誤打）。

    ## 當前偵測規則（保守）

    只 flag **最強信號**：某層的整個文件序列**完全沒出現過 `1`**。

    這表示 parser 在該層有偵測到 markers，但沒有任何一個是「第 1 項」——
    強烈暗示該層的真正起點（`一、` / `㈠` / `⒈` / `1.`）被 parser 漏掉
    （可能因為落在 quote 裡、被 boundary check 誤拒、或前面段太長被截斷）。

    ## 已知不抓

    - 使用者 ⒎⒏⒐ 邊界案（`[1, 7, 8, 9]`）— 因為有 `1` 存在，不 flag
      這類由 Pass 2.5 heuristic 負責，anomaly log 不重複報

    ## 輸出

    回傳 {layer: {"sequence": [...], "issue": "no_one", "total": N}}
    """
    if not reasoning:
        return {}
    layers: dict[str, list[int]] = {}
    for ln in reasoning.split("\n"):
        parsed = _parse_line_marker(ln)
        if parsed:
            layer, num = parsed
            layers.setdefault(layer, []).append(num)

    found: dict[str, dict] = {}
    for layer, nums in layers.items():
        if len(nums) < 2:
            continue
        # 最強信號：整層序列完全沒出現 `1`
        # → parser 有偵測到 markers 但沒找到真正的起點
        if 1 not in nums:
            found[layer] = {
                "issue": "no_one",
                "first": nums[0],
                "sequence": nums[:10],
                "total": len(nums),
            }
    return found


def check_anomalies(judgment: dict) -> tuple[list[str], dict[str, Any]]:
    """檢查單筆判決的 parse 結構訊號，回傳 (anomaly_types, metrics)。

    anomaly_types 為空 list 表示無異常。metrics 永遠回傳，便於日後 query 統計。
    """
    reasoning = (judgment.get("reasoning") or "").strip()
    main_text = (judgment.get("main_text") or "").strip()
    facts = (judgment.get("facts") or "").strip()
    full_text = (judgment.get("full_text") or "").strip()
    cited_statutes = judgment.get("cited_statutes") or []
    if isinstance(cited_statutes, str):
        try:
            cited_statutes = json.loads(cited_statutes)
        except (json.JSONDecodeError, TypeError):
            cited_statutes = []

    anomalies: list[str] = []

    # ─ 欄位空缺 ──────────────────────────────────────
    # all-empty 是 parse 完全失敗（最嚴重）— 個別 empty 就不再加，避免雜訊
    if (len(reasoning) < _ALL_FIELDS_EMPTY_THRESHOLD
            and len(facts) < _ALL_FIELDS_EMPTY_THRESHOLD
            and len(main_text) < _ALL_FIELDS_EMPTY_THRESHOLD):
        anomalies.append("all_fields_empty")
    else:
        if len(reasoning) < _EMPTY_REASONING_THRESHOLD:
            anomalies.append("empty_reasoning")
        if len(main_text) < _EMPTY_MAIN_TEXT_THRESHOLD:
            anomalies.append("empty_main_text")

    # ─ 段落結構問題：超長行且無 outline marker ────────
    # 觸發示例：最高行 .htmlcontent 段落沒切；雖修補後改善但個案仍可能出現
    max_line_chars = 0
    long_lines_no_marker = 0
    if reasoning:
        for ln in reasoning.split("\n"):
            ll = len(ln)
            if ll > max_line_chars:
                max_line_chars = ll
            if ll > _LONG_LINE_THRESHOLD and not _has_outline_marker(ln):
                long_lines_no_marker += 1
    if long_lines_no_marker > 0:
        anomalies.append("long_line_no_marker")

    # ─ 引號不對稱嚴重（OCR / parse 異常）──────────────
    open_q = reasoning.count("「") if reasoning else 0
    close_q = reasoning.count("」") if reasoning else 0
    if abs(open_q - close_q) > _QUOTE_IMBALANCE_THRESHOLD:
        anomalies.append("quote_unbalanced")

    # ─ cited_statutes 列表空，但 reasoning 有條號 ─────
    if not cited_statutes and reasoning and _CITED_REF_IN_TEXT.search(reasoning):
        anomalies.append("cited_statutes_extractor_miss")

    # ─ Outline marker 號碼跳號 / 起點非 1 ───────────────
    # 真 outline 一定從 1 開始連續；出現 `一, 二, 九` 或 `七, 八, 九` 強烈暗示
    # parser 誤判或漏判。引文跳號的合法 case（`⒈..⒍`）也會被flag，剛好就是
    # Pass 2.5 heuristic 漏網的邊界、值得進 log 供後續分析。
    outline_gaps = _find_outline_number_gaps(reasoning)
    if outline_gaps:
        anomalies.append("outline_number_gap")

    metrics = {
        "reasoning_len": len(reasoning),
        "main_text_len": len(main_text),
        "facts_len": len(facts),
        "full_text_len": len(full_text),
        "cited_statutes_count": len(cited_statutes) if isinstance(cited_statutes, list) else 0,
        "max_line_chars": max_line_chars,
        "long_lines_no_marker": long_lines_no_marker,
        "open_quotes": open_q,
        "close_quotes": close_q,
    }
    if outline_gaps:
        metrics["outline_gaps"] = outline_gaps

    return anomalies, metrics


async def log_judgment(
    judgment: dict,
    task_id: str | None = None,
    jid: str | None = None,
) -> bool:
    """檢查 + 寫入；若無 anomaly 不寫，回傳是否有寫入。

    fetch pipeline 在 db.create_task_judgment 後呼叫。失敗只 log warning，
    不拋例外（不影響搜尋流程）。
    """
    try:
        anomalies, metrics = check_anomalies(judgment)
    except Exception as exc:
        logger.warning("anomaly check 失敗 (case_id=%s): %s", judgment.get("case_id"), exc)
        return False

    if not anomalies:
        return False

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "case_id": judgment.get("case_id", ""),
        "court": judgment.get("court", ""),
        "jid": jid or "",
        "task_id": task_id or "",
        "anomaly_types": anomalies,
        "metrics": metrics,
    }

    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        async with _write_lock:
            # 同步 file I/O 丟給 default executor，避免 block event loop
            # （8 並行 fetch 在磁碟壓力下原本會集體卡 event loop）
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _append_line, line)
        return True
    except OSError as exc:
        logger.warning("無法寫入 anomaly log %s: %s", _LOG_PATH, exc)
        return False


def _append_line(line: str) -> None:
    """在 executor thread 執行的同步檔案寫入。"""
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)
