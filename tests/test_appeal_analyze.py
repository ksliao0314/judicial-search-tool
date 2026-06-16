"""勞動部訴願 mode 的 analyze.py 回歸測試（network-free、mock Claude client）。

test_appeal_source.py 只蓋 scraper（appeal_source.py）；本檔補上 analyze.py 的訴願分支：
  1. ANALYSIS_PROMPT_APPEAL + _V2_SUFFIX_APPEAL 用 _call_claude_v2 的 kwargs .format 不 KeyError
  2. _split_scoring_prompt(appeal_prompt) 走 fallback（訴願 marker ≠ 判決 marker）
  3. _call_claude_v2 selector：appeal / interpretation / default 各選對 template
  4. run_synthesis appeal 分支：前置 _SYNTHESIS_APPEAL_NOTE、JSON 範例存活、重試不重複前置
  5. appeal JSON 輸出 key 與 run_analysis_v2 parser 的 parsed.get(...) 契約一致

不打網路、不打 Claude：client 與 token bucket 全 mock。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.db import database as db
from src.pipeline import analyze


# ─── Claude client / response 假件 ────────────────────────────────────────

class _FakeUsage:
    input_tokens = 120
    output_tokens = 60
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.content = [_FakeContent(text)]
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _FakeMessages:
    """records 收每次 create 的 kwargs；responses 可為單一 response 或 list（逐次 pop）。"""

    def __init__(self, records: list, responses):
        self._records = records
        self._responses = responses

    async def create(self, **kwargs):
        self._records.append(kwargs)
        resp = self._responses.pop(0) if isinstance(self._responses, list) else self._responses
        if isinstance(resp, Exception):
            raise resp
        return resp


class _FakeClient:
    def __init__(self, records: list, responses):
        self.messages = _FakeMessages(records, responses)


def _full_prompt_text(create_kwargs: dict) -> str:
    """把一次 create 的 system blocks + user messages 文字接起來（供 marker 比對）。"""
    parts: list[str] = []
    for blk in create_kwargs.get("system") or []:
        parts.append(blk.get("text", ""))
    for msg in create_kwargs.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


# 一份 appeal 形狀的合法回應（key 對齊 ANALYSIS_PROMPT_APPEAL 的 JSON 範例）
_APPEAL_PAYLOAD = json.dumps(
    {
        "score": 7,
        "direction": "支持",
        "position": "訴願會認定原處分違法，撤銷原處分",
        "excerpt": "本部審酌訴願人主張，認原處分認事用法有違誤，原處分撤銷",
        "found_in": "reasoning",
        "variant_candidates": [],
    },
    ensure_ascii=False,
)


@pytest.fixture
def no_rate_limit(monkeypatch):
    """把四個 module-level token bucket 的 acquire no-op 掉。

    這些是 unit test、不測限流；no-op 後既快又避免 module-level bucket 的 asyncio.Lock
    在多次 asyncio.run() 新 loop 間出現跨 loop 綁定問題。"""
    async def _noop(*_a, **_k):
        return None

    for name in (
        "_haiku_itpm_bucket", "_haiku_rpm_bucket",
        "_sonnet_itpm_bucket", "_sonnet_rpm_bucket",
    ):
        monkeypatch.setattr(getattr(analyze, name), "acquire", _noop)


# ─── 1. prompt format 不 KeyError + JSON 範例花括號存活 ────────────────────

def test_appeal_prompt_formats_without_keyerror():
    # 與 _call_claude_v2 完全相同的 kwargs
    rendered = analyze.ANALYSIS_PROMPT_APPEAL.format(
        field_label="理由",
        question="在何種情形下訴願會撤銷原處分？",
        keyword="原處分撤銷",
        field_text="【理由】訴願審議委員會認為原處分有違法之處。",
    )
    # JSON 範例的 {{ }} 正確 escape 成單花括號、score 行存活
    assert '"score": 0到10的整數' in rendered
    assert '"direction": "支持" | "反對" | "中性"' in rendered
    assert '"variant_candidates": []' in rendered
    # _V2_SUFFIX_APPEAL 的訴願 marker（非判決 marker）
    assert "訴願決定書內容如下（各段以【】標示段落類型）：" in rendered
    # field_text / question 確實 render 進去
    assert "【理由】訴願審議委員會認為原處分有違法之處。" in rendered
    assert "在何種情形下訴願會撤銷原處分？" in rendered
    # _VARIANT_RULES 的 {keyword} 被替換（沒有殘留 placeholder）
    assert "「原處分撤銷」" in rendered
    assert "{keyword}" not in rendered
    assert "{field_text}" not in rendered


def test_appeal_suffix_marker_differs_from_v2():
    # _V2_SUFFIX_APPEAL 用訴願 marker，與 scoring 切分用的 _SCORING_FIELD_MARKER 不同
    appeal_marker = "訴願決定書內容如下（各段以【】標示段落類型）："
    assert appeal_marker in analyze._V2_SUFFIX_APPEAL
    assert appeal_marker not in analyze._SCORING_FIELD_MARKER
    assert "判決內容如下" in analyze._SCORING_FIELD_MARKER


# ─── 2. _split_scoring_prompt(appeal) 走 fallback ─────────────────────────

def test_split_scoring_prompt_appeal_fallback():
    appeal_prompt = analyze.ANALYSIS_PROMPT_APPEAL.format(
        field_label="理由", question="q?", keyword="kw",
        field_text="【理由】訴願決定理由內容",
    )
    # 前提：appeal prompt 不含 scoring 用的判決 marker → rfind miss → fallback 分支
    assert analyze._SCORING_FIELD_MARKER not in appeal_prompt

    system_blocks, user_content = analyze._split_scoring_prompt(appeal_prompt)

    # fallback：單一 system block（SYSTEM_PROMPT + cache_control），user_content == 完整 prompt
    assert len(system_blocks) == 1
    assert system_blocks[0]["text"] == analyze.SYSTEM_PROMPT
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert user_content == appeal_prompt


def test_split_scoring_prompt_v2_splits_for_contrast():
    # 對照組：標準 V2 prompt 含判決 marker → 切成 2 個 system block（含 cached instructions）
    v2_prompt = analyze.ANALYSIS_PROMPT_V2.format(
        field_label="理由", question="q?", keyword="kw", field_text="【理由】判決理由",
    )
    assert analyze._SCORING_FIELD_MARKER in v2_prompt
    system_blocks, user_content = analyze._split_scoring_prompt(v2_prompt)
    assert len(system_blocks) == 2
    assert system_blocks[1]["cache_control"] == {"type": "ephemeral"}
    # user_content 只剩內文段（從 marker 起）、不含完整指示
    assert user_content.startswith("判決內容如下")


# ─── 3. _call_claude_v2 template selector ─────────────────────────────────

def test_call_claude_v2_template_selector(monkeypatch, no_rate_limit):
    # (search_domain, 必須出現的獨有 marker, 不該出現的他模 marker)
    cases = [
        ("appeal", "勞動部訴願決定書", ["大法官解釋", "固非無見"]),
        ("interpretation", "大法官解釋", ["勞動部訴願決定書", "固非無見"]),
        ("judgment", "固非無見", ["勞動部訴願決定書", "大法官解釋"]),
        # 未知 / 預設 domain → 落到 ANALYSIS_PROMPT_V2 分支
        ("something-else", "固非無見", ["勞動部訴願決定書", "大法官解釋"]),
    ]
    for domain, must_have, must_not in cases:
        records: list = []
        fake = _FakeClient(records, _FakeResponse(_APPEAL_PAYLOAD))
        monkeypatch.setattr(analyze, "_get_client", lambda api_key=None, _f=fake: _f)

        asyncio.run(analyze._call_claude_v2(
            "c1", "【理由】內文", "理由", "問題?", "關鍵字", search_domain=domain,
        ))

        assert len(records) == 1
        full = _full_prompt_text(records[0])
        assert must_have in full, f"{domain}: 應使用對應 template，缺 marker {must_have!r}"
        for m in must_not:
            assert m not in full, f"{domain}: 不該混入他 template marker {m!r}"


def test_call_claude_v2_returns_parsed_json(monkeypatch, no_rate_limit):
    records: list = []
    fake = _FakeClient(records, _FakeResponse(_APPEAL_PAYLOAD))
    monkeypatch.setattr(analyze, "_get_client", lambda api_key=None: fake)
    parsed = asyncio.run(analyze._call_claude_v2(
        "c1", "【理由】內文", "理由", "問題?", "關鍵字", search_domain="appeal",
    ))
    assert parsed["score"] == 7
    assert parsed["direction"] == "支持"
    # usage 有附帶（供呼叫端累積 token）
    assert parsed["_usage_input"] == 120
    assert parsed["_usage_output"] == 60


# ─── 4. run_synthesis appeal 分支 ─────────────────────────────────────────

_SYNTH_RESULTS = [
    {"court": "勞動部", "case_id": "勞動法訴字第1號", "score": 8, "reason": "[支持] 撤銷原處分"},
    {"court": "勞動部", "case_id": "勞動法訴字第2號", "score": 5, "reason": "[反對] 維持原處分"},
    {"court": "勞動部", "case_id": "勞動法訴字第3號", "score": 3, "reason": "[中性] 審查標準說明"},
]

_SYNTH_PAYLOAD = json.dumps(
    {
        "total_relevant": 3,
        "answer": "訴願審議委員會於本批決定中對原處分之合法性審查……",
        "direction_counts": {"支持": 1, "反對": 1, "中性": 1},
        "clusters": [],
    },
    ensure_ascii=False,
)


def _patch_synthesis_db(monkeypatch, domain: str):
    async def _scored(_aid):
        return list(_SYNTH_RESULTS)

    async def _get_analysis(_aid):
        return {"id": _aid, "task_id": "task1"}

    async def _get_task(_tid):
        return {"id": _tid, "search_domain": domain}

    saved: dict = {}

    async def _set_synth(aid, synthesis):
        saved["aid"] = aid
        saved["synthesis"] = synthesis

    monkeypatch.setattr(db, "get_analysis_results_scored", _scored)
    monkeypatch.setattr(db, "get_analysis", _get_analysis)
    monkeypatch.setattr(db, "get_task", _get_task)
    monkeypatch.setattr(db, "set_analysis_synthesis", _set_synth)
    return saved


def test_run_synthesis_appeal_prepends_note(monkeypatch, no_rate_limit):
    _patch_synthesis_db(monkeypatch, domain="appeal")
    records: list = []
    fake = _FakeClient(records, _FakeResponse(_SYNTH_PAYLOAD))
    monkeypatch.setattr(analyze, "_get_client", lambda api_key=None: fake)

    result = asyncio.run(analyze.run_synthesis("ana1", "訴願會在何種情形撤銷原處分？"))

    assert len(records) == 1
    prompt = records[0]["messages"][0]["content"]
    # 訴願 note 前置（且只一次）
    assert prompt.startswith(analyze._SYNTHESIS_APPEAL_NOTE)
    note_marker = "【重要・本批為勞動部訴願決定書，非法院判決】"
    assert prompt.count(note_marker) == 1
    # SYNTHESIS_PROMPT 的 JSON 範例 {{ }} 存活（n 已填入 3）
    assert '"total_relevant": 3' in prompt
    assert '"direction_counts"' in prompt
    assert result["total_relevant"] == 3


def test_run_synthesis_appeal_note_not_doubled_on_retry(monkeypatch, no_rate_limit):
    """第一輪 max_tokens 截斷 → continue 重建 prompt；note 每輪重建後仍只前置一次。"""
    _patch_synthesis_db(monkeypatch, domain="appeal")
    records: list = []
    responses = [
        _FakeResponse(_SYNTH_PAYLOAD, stop_reason="max_tokens"),  # 觸發 continue（無 sleep）
        _FakeResponse(_SYNTH_PAYLOAD, stop_reason="end_turn"),    # 第二輪成功
    ]
    fake = _FakeClient(records, responses)
    monkeypatch.setattr(analyze, "_get_client", lambda api_key=None: fake)

    result = asyncio.run(analyze.run_synthesis("ana1", "q?"))

    assert len(records) == 2
    note_marker = "【重要・本批為勞動部訴願決定書，非法院判決】"
    for rec in records:
        prompt = rec["messages"][0]["content"]
        assert prompt.count(note_marker) == 1, "note 不應跨重試累積前置"
        assert prompt.startswith(analyze._SYNTHESIS_APPEAL_NOTE)
    assert result["total_relevant"] == 3


def test_run_synthesis_judgment_has_no_appeal_note(monkeypatch, no_rate_limit):
    """對照組：judgment domain 不前置訴願 note。"""
    _patch_synthesis_db(monkeypatch, domain="judgment")
    records: list = []
    fake = _FakeClient(records, _FakeResponse(_SYNTH_PAYLOAD))
    monkeypatch.setattr(analyze, "_get_client", lambda api_key=None: fake)

    asyncio.run(analyze.run_synthesis("ana1", "q?"))

    prompt = records[0]["messages"][0]["content"]
    assert analyze._SYNTHESIS_APPEAL_NOTE not in prompt
    assert prompt.startswith("你是律師的法律研究助理")


# ─── 5. appeal JSON 輸出 key 與 run_analysis_v2 parser 契約一致 ────────────

def test_appeal_output_keys_consumed_by_parser(monkeypatch, no_rate_limit):
    records: list = []
    fake = _FakeClient(records, _FakeResponse(_APPEAL_PAYLOAD))
    monkeypatch.setattr(analyze, "_get_client", lambda api_key=None: fake)
    parsed = asyncio.run(analyze._call_claude_v2(
        "c1", "【理由】內文", "理由", "撤銷原處分的情形", "原處分", search_domain="appeal",
    ))

    # 重現 run_analysis_v2 的 parsed.get(...) 萃取邏輯 — 每個 key 都讀得到、零 KeyError
    score = int(parsed.get("score", 0))
    direction = str(parsed.get("direction", "中性") or "中性").strip()
    assert direction in ("支持", "反對", "中性")
    raw_position = str(parsed.get("position", "") or "")[:200]
    raw_excerpt = str(parsed.get("excerpt", "") or "")[:500]
    found_in = str(parsed.get("found_in", "") or "").strip()
    found_label = analyze.FIELD_LABELS.get(found_in, "")
    candidates = parsed.get("variant_candidates") or []

    assert score == 7
    assert direction == "支持"
    assert raw_position.startswith("訴願會認定")
    assert raw_excerpt.startswith("本部審酌")
    assert found_in == "reasoning"
    assert found_label == "理由"          # found_in 能 map 回 FIELD_LABELS
    assert candidates == []

    # 而且 appeal prompt 的 JSON 範例確實宣告 parser 會讀的每個 key
    rendered = analyze.ANALYSIS_PROMPT_APPEAL.format(
        field_label="理由", question="q", keyword="kw", field_text="t",
    )
    for key in ("score", "direction", "position", "excerpt", "found_in", "variant_candidates"):
        assert f'"{key}"' in rendered, f"appeal prompt JSON 範例缺 key: {key}"
