"""Phase 2 新 endpoint 單元測試：/abort、/resume、/finalize（擴充後）。

測試覆蓋：
- 狀態 guard（pending/running 才能 abort；partial 才能 resume；running 或 is_preliminary=1 才能 finalize）
- DB 副作用（finalize 即時升格路徑直接改 status + is_preliminary）
- 旗標設置（abort / running-finalize 設 in-memory flag）
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.api import analyses as analyses_api
from src.db import database as db
from src.worker import runner


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    schema_path = Path(__file__).resolve().parents[1] / "schema.sql"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "SCHEMA_PATH", str(schema_path))
    asyncio.run(db.init_db())
    return db_path


async def _mk_task_and_analysis(
    *, status: str, is_preliminary: int = 0, has_synthesis: bool = False,
    match_count: int = 0, rows: int = 0,
) -> tuple[str, str]:
    """塞一個測試用 task + analysis。回傳 (task_id, analysis_id)。"""
    task_id = f"task-{status}-{is_preliminary}"
    analysis_id = f"ana-{status}-{is_preliminary}"
    await db.create_task(
        task_id=task_id, mode="keyword", keyword="測試",
        filter_fields="reasoning", status="done",
    )
    await db.create_analysis(
        analysis_id=analysis_id, task_id=task_id,
        question="測試問題", ai_read_field="reasoning", status=status,
    )
    updates = {"match_count": match_count}
    if has_synthesis:
        # set_analysis_synthesis 會同時寫 is_preliminary；用它避免直接 update 漏欄
        await db.set_analysis_synthesis(
            analysis_id,
            {"summary": "partial summary", "relevant_case_ids": ["c1"]},
            is_preliminary=bool(is_preliminary),
        )
    elif is_preliminary:
        updates["synthesis_is_preliminary"] = 1
    if updates:
        await db.update_analysis(analysis_id, **updates)
    # 塞 rows 筆 analysis_results 讓 count_analysis_results 有值
    for i in range(rows):
        await db.create_analysis_result(
            analysis_id=analysis_id, case_id=f"case-{i}",
            match="yes", score=7, excerpt="", reason="",
        )
    return task_id, analysis_id


# ─── /abort ─────────────────────────────────────────────────────────────

def test_abort_on_running_sets_flag(tmp_db):
    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(status="running")
        resp = await analyses_api.abort_analysis(task_id, analysis_id)
        assert resp["abort_requested"] is True
        assert resp["current_status"] == "running"
        assert runner._is_graceful_abort(analysis_id)
        runner._clear_graceful_abort(analysis_id)  # 清理
    asyncio.run(run())


def test_abort_on_pending_allowed(tmp_db):
    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(status="pending")
        resp = await analyses_api.abort_analysis(task_id, analysis_id)
        assert resp["abort_requested"] is True
        runner._clear_graceful_abort(analysis_id)
    asyncio.run(run())


def test_abort_on_done_rejects(tmp_db):
    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(status="done")
        with pytest.raises(HTTPException) as exc:
            await analyses_api.abort_analysis(task_id, analysis_id)
        assert exc.value.status_code == 409
        assert not runner._is_graceful_abort(analysis_id)
    asyncio.run(run())


def test_abort_bogus_task_404(tmp_db):
    async def run():
        with pytest.raises(HTTPException) as exc:
            await analyses_api.abort_analysis("bogus", "bogus")
        assert exc.value.status_code == 404
    asyncio.run(run())


# ─── /resume ────────────────────────────────────────────────────────────

def test_resume_on_partial_dispatches(tmp_db, monkeypatch):
    """status=partial → status=running、dispatch_work 被呼叫 1 次、不 reset match_count。"""
    dispatched: list = []
    async def fake_dispatch(work):
        dispatched.append(work)
    # 攔截 create_task + dispatch_work 呼叫（避免真的跑背景）
    monkeypatch.setattr(analyses_api, "dispatch_work", fake_dispatch)
    monkeypatch.setattr(
        analyses_api.asyncio, "create_task",
        lambda coro: asyncio.ensure_future(coro),
    )

    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(
            status="partial", is_preliminary=1, has_synthesis=True,
            match_count=5, rows=3,
        )
        resp = await analyses_api.resume_analysis(task_id, analysis_id, x_api_key=None)
        assert resp["status"] == "running"
        # 等 pending create_task 完成
        await asyncio.sleep(0)
        assert len(dispatched) == 1
        assert dispatched[0].analysis_id == analysis_id
        # match_count 保留、不 reset
        a = await db.get_analysis(analysis_id)
        assert a["status"] == "running"
        assert a["match_count"] == 5
        # synthesis_is_preliminary 保留（續跑中再中止時判斷 was_resumed 用）
        assert a["synthesis_is_preliminary"] == 1
    asyncio.run(run())


def test_resume_double_click_second_rejected(tmp_db, monkeypatch):
    """P0-3: double-click /resume → 第一個成功 status→running、第二個看到 status=running → 409
    防止兩個 worker 同時跑同 analysis 造成 DB race / 結果重寫。
    """
    dispatched: list = []
    async def fake_dispatch(work):
        dispatched.append(work)
    monkeypatch.setattr(analyses_api, "dispatch_work", fake_dispatch)
    monkeypatch.setattr(
        analyses_api.asyncio, "create_task",
        lambda coro: asyncio.ensure_future(coro),
    )

    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(
            status="partial", is_preliminary=1, has_synthesis=True,
            match_count=5, rows=3,
        )
        # 第一個 resume 成功
        resp1 = await analyses_api.resume_analysis(task_id, analysis_id, x_api_key=None)
        assert resp1["status"] == "running"
        # 第二個立刻跟進（模擬 double-click）→ status 已是 running、不是 partial → 409
        with pytest.raises(HTTPException) as exc:
            await analyses_api.resume_analysis(task_id, analysis_id, x_api_key=None)
        assert exc.value.status_code == 409
        # dispatch_work 只被呼叫 1 次（不是 2 次）
        await asyncio.sleep(0)
        assert len(dispatched) == 1
    asyncio.run(run())


def test_update_analysis_if_status_atomic(tmp_db):
    """DB helper：只在 expected_status 符合時 update。"""
    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(status="partial")
        # expected=partial、符合 → update 成功
        ok = await db.update_analysis_if_status(
            analysis_id, expected_status="partial", status="running",
        )
        assert ok is True
        a = await db.get_analysis(analysis_id)
        assert a["status"] == "running"
        # 再來一次 expected=partial、現在是 running → update 失敗
        ok2 = await db.update_analysis_if_status(
            analysis_id, expected_status="partial", status="done",
        )
        assert ok2 is False
        a2 = await db.get_analysis(analysis_id)
        assert a2["status"] == "running"  # 沒被覆寫
        # 不存在的 analysis_id → False
        ok3 = await db.update_analysis_if_status(
            "bogus", expected_status="partial", status="done",
        )
        assert ok3 is False
    asyncio.run(run())


def test_resume_on_done_rejects(tmp_db):
    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(status="done")
        with pytest.raises(HTTPException) as exc:
            await analyses_api.resume_analysis(task_id, analysis_id, x_api_key=None)
        assert exc.value.status_code == 409
    asyncio.run(run())


# ─── /finalize ──────────────────────────────────────────────────────────

def test_finalize_running_with_preliminary_instant_upgrade(tmp_db, monkeypatch):
    """路徑 1（running + 有 preliminary）→ 立即 DB 升格、is_final=True、
    同時 set graceful_abort + finalize 旗讓 scoring 停下。"""
    sse_calls: list = []

    class FakeSseBus:
        @staticmethod
        async def publish(task_id, event, data):
            sse_calls.append((task_id, event, data))
    import src
    monkeypatch.setattr(src, "sse_bus", FakeSseBus)

    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(
            status="running", is_preliminary=1, has_synthesis=True,
            match_count=10, rows=8,
        )
        resp = await analyses_api.finalize_preliminary(task_id, analysis_id)
        assert resp["is_final"] is True
        assert resp["current_status"] == "done"
        # DB 立即升格
        a = await db.get_analysis(analysis_id)
        assert a["status"] == "done"
        assert a["synthesis_is_preliminary"] == 0
        # 兩個旗標都設了、讓 scoring cooperative 停下
        assert runner._is_graceful_abort(analysis_id)
        assert runner._is_finalize_requested(analysis_id)
        runner._clear_graceful_abort(analysis_id)
        runner._clear_finalize_requested(analysis_id)
    asyncio.run(run())


def test_finalize_running_without_preliminary_sets_flag(tmp_db):
    """路徑 2（running + 無 preliminary、罕見情境）→ 設 flag、is_final=False（等 retry loop）。"""
    async def run():
        # 沒 has_synthesis=True → is_preliminary=0 + synthesis=NULL
        task_id, analysis_id = await _mk_task_and_analysis(status="running")
        resp = await analyses_api.finalize_preliminary(task_id, analysis_id)
        assert resp["is_final"] is False
        assert resp["current_status"] == "running"
        assert resp["has_preliminary"] is False
        assert runner._is_finalize_requested(analysis_id)
        runner._clear_finalize_requested(analysis_id)
        # DB 狀態不變（由 runner 後續升格、或 retry loop miss 後走 final synthesis）
        a = await db.get_analysis(analysis_id)
        assert a["status"] == "running"
    asyncio.run(run())


def test_finalize_partial_instant_upgrade(tmp_db, monkeypatch):
    """路徑 2：status=partial + is_preliminary=1 → 即時 DB 升格、is_final=True、推 SSE。"""
    sse_calls: list = []

    class FakeSseBus:
        @staticmethod
        async def publish(task_id, event, data):
            sse_calls.append((task_id, event, data))

    # 攔截 sse_bus（/finalize 內是 lazy import）
    import src
    monkeypatch.setattr(src, "sse_bus", FakeSseBus)

    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(
            status="partial", is_preliminary=1, has_synthesis=True,
            match_count=8, rows=5,
        )
        resp = await analyses_api.finalize_preliminary(task_id, analysis_id)
        assert resp["is_final"] is True
        assert resp["current_status"] == "done"
        # DB 立即升格
        a = await db.get_analysis(analysis_id)
        assert a["status"] == "done"
        assert a["synthesis_is_preliminary"] == 0
        # SSE 推了 stage3_synthesis_done + analysis_done
        events = [e for _, e, _ in sse_calls]
        assert "stage3_synthesis_done" in events
        assert "analysis_done" in events
        # synthesis_done payload 含 is_final=True
        syn_payload = next(d for _, e, d in sse_calls if e == "stage3_synthesis_done")
        assert syn_payload["is_final"] is True
    asyncio.run(run())


def test_finalize_done_without_preliminary_409(tmp_db):
    """沒 preliminary 可升格 → 409。"""
    async def run():
        task_id, analysis_id = await _mk_task_and_analysis(status="done")
        with pytest.raises(HTTPException) as exc:
            await analyses_api.finalize_preliminary(task_id, analysis_id)
        assert exc.value.status_code == 409
    asyncio.run(run())


# ─── runner graceful_abort helpers ───────────────────────────────────────

def test_graceful_abort_flag_lifecycle():
    aid = "test-ana-123"
    assert not runner._is_graceful_abort(aid)
    runner.request_graceful_abort(aid)
    assert runner._is_graceful_abort(aid)
    runner._clear_graceful_abort(aid)
    assert not runner._is_graceful_abort(aid)


# ─── db count_analysis_results ──────────────────────────────────────────

def test_count_analysis_results(tmp_db):
    async def run():
        _, analysis_id = await _mk_task_and_analysis(status="running", rows=7)
        count = await db.count_analysis_results(analysis_id)
        assert count == 7
        count0 = await db.count_analysis_results("nonexistent")
        assert count0 == 0
    asyncio.run(run())
