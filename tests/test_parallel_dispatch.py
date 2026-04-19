"""並行 dispatch_work 的回歸測試。

驗證的核心不是「會不會跑更快」（那是整合測試範疇）而是：
1. Semaphore 上限正確限流（模擬 10 個 work、只有 5 個同時跑）
2. 同時觸發多個 work 不會造成 DB 異常（UNIQUE 約束擋住 race）
3. stage25_inflight 的 record / clear / list 基本正確
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

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


# ─── stage25_inflight CRUD ─────────────────────────────────────────────

def test_record_and_list_stage25_inflight(tmp_db):
    async def run():
        await db.record_stage25_inflight("taskA", ["case1", "case2", "case3"])
        await db.record_stage25_inflight("taskB", ["caseX"])
        rows = await db.list_stage25_inflight()
        assert len(rows) == 2
        by_id = {r["task_id"]: r for r in rows}
        assert by_id["taskA"]["case_ids"] == ["case1", "case2", "case3"]
        assert by_id["taskB"]["case_ids"] == ["caseX"]

    asyncio.run(run())


def test_clear_stage25_inflight(tmp_db):
    async def run():
        await db.record_stage25_inflight("taskA", ["case1"])
        await db.clear_stage25_inflight("taskA")
        rows = await db.list_stage25_inflight()
        assert rows == []
        # 清不存在的也 safe（no-op）
        await db.clear_stage25_inflight("nonexistent")

    asyncio.run(run())


def test_record_replace_same_task(tmp_db):
    """同 task 重複呼叫 record → 取最新 case_ids（律師 cancel 後重起另一批）。"""
    async def run():
        await db.record_stage25_inflight("taskA", ["old1", "old2"])
        await db.record_stage25_inflight("taskA", ["new1"])
        rows = await db.list_stage25_inflight()
        assert len(rows) == 1
        assert rows[0]["case_ids"] == ["new1"]

    asyncio.run(run())


def test_delete_task_cascades_stage25_inflight(tmp_db):
    """刪除 task 時順便清 stage25_inflight（避免重啟時 recovery 殘留死 task）。"""
    async def run():
        async with db._conn() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, mode, keyword, filter_fields, status, created_at) "
                "VALUES ('taskA', 'keyword', '', '', 'done', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.commit()
        await db.record_stage25_inflight("taskA", ["c1"])
        await db.delete_task("taskA")
        rows = await db.list_stage25_inflight()
        assert rows == []

    asyncio.run(run())


# ─── dispatch_work semaphore 限流 ──────────────────────────────────────

def test_dispatch_work_semaphore_bounds_concurrency(monkeypatch):
    """派 10 個 work、_STAGE_CONCURRENCY=5 → 同時跑的 coroutines 不超過 5。"""
    runner._stage_sem = None  # reset so first call rebuilds with current setting

    peak_in_flight = 0
    in_flight = 0
    lock = asyncio.Lock()

    class FakeWork:
        def __init__(self, i):
            self.type = "fake"
            self.task_id = f"t{i}"

    async def fake_run(work):
        nonlocal in_flight, peak_in_flight
        async with lock:
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.05)  # 佔槽位夠久，讓併發能重疊
        async with lock:
            in_flight -= 1

    # patch _execute_work to count concurrency
    monkeypatch.setattr(runner, "_execute_work", fake_run)

    async def run():
        # 強制新建 semaphore 綁定當前 loop
        runner._stage_sem = asyncio.Semaphore(runner._STAGE_CONCURRENCY)
        tasks = [asyncio.create_task(runner.dispatch_work(FakeWork(i))) for i in range(10)]
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert peak_in_flight <= runner._STAGE_CONCURRENCY, \
        f"peak {peak_in_flight} > limit {runner._STAGE_CONCURRENCY}"
    assert peak_in_flight >= 2, \
        f"peak {peak_in_flight} too low — 沒真的並行（可能 sem 或 asyncio.gather 有問題）"


# ─── Stage 3 recovery env var fallback ────────────────────────────────

def test_recover_new_task_uses_env_api_key(tmp_db, monkeypatch):
    """Env var ANTHROPIC_API_KEY 存在時，pending analysis 應該被 re-dispatch（而非 mark failed）。"""
    async def run():
        # Setup：建一個 task + pending analysis + 假的 task_search_hits（讓 recovery 走 stage3 path）
        async with db._conn() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, mode, keyword, filter_fields, status, "
                "search_params, created_at) VALUES "
                "('taskR', 'keyword', 'kw', '', 'done', '{}', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.execute(
                "INSERT INTO task_search_hits (task_id, case_id, court, date, "
                "source_url, fetched_at) VALUES ('taskR', 'c1', 'X', '113-01-01', '', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.execute(
                "INSERT INTO analyses (id, task_id, question, ai_read_field, "
                "narrow_state, status, created_at) VALUES "
                "('anaR', 'taskR', 'q?', 'reasoning,main_text,facts,cited_statutes', "
                "'{}', 'pending', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.commit()

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        # 攔截 dispatch_work（現在 recovery 走 create_task 非同步派出）
        captured = []
        async def fake_dispatch(work):
            captured.append(work)
        monkeypatch.setattr(runner, "dispatch_work", fake_dispatch)

        await runner._recover_new_task({"id": "taskR", "keyword": "kw"}, {})
        # create_task 要 yield 給 event loop 跑到 fake_dispatch
        await asyncio.sleep(0)

        assert len(captured) == 1
        work = captured[0]
        assert work.type == "stage3_analyze"
        assert work.task_id == "taskR"
        assert work.analysis_id == "anaR"
        assert work.api_key == "sk-test-fake"

        ana = await db.get_analysis("anaR")
        assert ana["status"] == "pending", \
            f"env var 在，不應 mark failed：實際 {ana['status']}"

    asyncio.run(run())


def test_recover_new_task_without_env_marks_failed(tmp_db, monkeypatch):
    """Env var 不存在時維持舊行為：mark failed、不 dispatch。"""
    async def run():
        async with db._conn() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, mode, keyword, filter_fields, status, "
                "search_params, created_at) VALUES "
                "('taskR2', 'keyword', 'kw', '', 'done', '{}', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.execute(
                "INSERT INTO task_search_hits (task_id, case_id, court, date, "
                "source_url, fetched_at) VALUES ('taskR2', 'c1', 'X', '113-01-01', '', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.execute(
                "INSERT INTO analyses (id, task_id, question, ai_read_field, "
                "narrow_state, status, created_at) VALUES "
                "('anaR2', 'taskR2', 'q?', 'reasoning', '{}', 'running', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.commit()

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        captured = []
        async def fake_dispatch(work):
            captured.append(work)
        monkeypatch.setattr(runner, "dispatch_work", fake_dispatch)

        await runner._recover_new_task({"id": "taskR2", "keyword": "kw"}, {})
        await asyncio.sleep(0)

        assert captured == [], "無 env var 時不該 dispatch"
        ana = await db.get_analysis("anaR2")
        assert ana["status"] == "failed"

    asyncio.run(run())


def test_dispatch_work_allows_full_concurrency_up_to_limit(monkeypatch):
    """10 個 work 應該能並行到 5（上限）— 確認 peak 確實達到 sem 值。"""
    runner._stage_sem = None

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    class FakeWork:
        def __init__(self, i):
            self.type = "fake"
            self.task_id = f"t{i}"

    async def fake_run(work):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.1)
        async with lock:
            in_flight -= 1

    monkeypatch.setattr(runner, "_execute_work", fake_run)

    async def run():
        runner._stage_sem = asyncio.Semaphore(runner._STAGE_CONCURRENCY)
        await asyncio.gather(*[runner.dispatch_work(FakeWork(i)) for i in range(10)])

    asyncio.run(run())
    assert peak == runner._STAGE_CONCURRENCY, \
        f"expected peak=={runner._STAGE_CONCURRENCY}, got {peak}"
