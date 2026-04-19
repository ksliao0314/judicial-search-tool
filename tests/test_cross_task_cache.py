"""跨 task judgment cache 測試（find_cached_judgment + parser_version 失效邏輯）。

Phase 1 的核心風險在 invalidation：
- parser_version 不符 → 必須 miss（否則律師看到帶舊 parser bug 的資料）
- fetched_at 超過 max_age_days → 必須 miss
- backfill 的 'v0' row 不能被當前 PARSER_VERSION 的新 task 命中（保護既存資料漸進汰換）

_run_stage25_fetch 整合測試需要 mock MCP / SSE / 整個 task lifecycle，成本高；
unit 覆蓋 invalidation 邏輯就已經擋住 80% 的風險面。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.db import database as db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """每個測試用獨立的 SQLite 檔 + 跑完 schema + migrations。"""
    db_path = tmp_path / "test.db"
    schema_path = Path(__file__).resolve().parents[1] / "schema.sql"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "SCHEMA_PATH", str(schema_path))
    asyncio.run(db.init_db())
    return db_path


async def _create_task(task_id: str, mode: str = "keyword") -> None:
    """create_task 可能有額外欄位要求；這裡直連 SQLite 走最小 insert。"""
    async with db._conn() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, mode, keyword, filter_fields, status, created_at) "
            "VALUES (?, ?, '', '', 'done', ?)",
            (task_id, mode, datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()


async def _seed_judgment(
    task_id: str,
    case_id: str,
    parser_version: str,
    fetched_at: str | None = None,
    reasoning: str = "本院認為...",
) -> None:
    """寫一筆 task_judgments，可選擇覆寫 fetched_at 以模擬老資料。"""
    await db.create_task_judgment(
        task_id=task_id,
        case_id=case_id,
        court="最高行政法院",
        date="2024-01-01",
        source_url="https://example.com",
        reasoning=reasoning,
        main_text="駁回上訴。",
        facts=None,
        cited_statutes=["民法第184條"],
        parser_version=parser_version,
        full_text="本件判決全文...",
        extracted_citations=[["民法", 184, None, None, None, None]],
        judges=["法官甲"],
        parties={"原告": ["甲"], "被告": ["乙"]},
        cause="損害賠償",
    )
    # 若要模擬老資料，直接 UPDATE fetched_at
    if fetched_at is not None:
        async with db._conn() as conn:
            await conn.execute(
                "UPDATE task_judgments SET fetched_at = ? "
                "WHERE task_id = ? AND case_id = ?",
                (fetched_at, task_id, case_id),
            )
            await conn.commit()


# ─── find_cached_judgment 邏輯 ────────────────────────────────────────


def test_cache_hit_same_version(tmp_db):
    """task A 存了 v1 判決 → find_cached_judgment v1 命中。"""
    async def run():
        await _create_task("taskA")
        await _seed_judgment("taskA", "TPAA,113,判,1,20240101,1", "v1")
        cached = await db.find_cached_judgment(
            "TPAA,113,判,1,20240101,1", parser_version="v1"
        )
        assert cached is not None
        assert cached["case_id"] == "TPAA,113,判,1,20240101,1"
        assert cached["parser_version"] == "v1"
        assert cached["reasoning"] == "本院認為..."
        # 確認 JSON 欄位仍是字串（呼叫端自行 loads）
        assert json.loads(cached["cited_statutes"]) == ["民法第184條"]

    asyncio.run(run())


def test_cache_miss_version_mismatch(tmp_db):
    """task A 存了 v1，但查 v2 → 必須 miss（保護 parser 升級時不用 stale cache）。"""
    async def run():
        await _create_task("taskA")
        await _seed_judgment("taskA", "case-x", "v1")
        cached = await db.find_cached_judgment("case-x", parser_version="v2")
        assert cached is None

    asyncio.run(run())


def test_cache_miss_v0_backfill_not_reused(tmp_db):
    """migration backfill 的 'v0' row 不能被 'v1' 新 task 命中（漸進汰換保證）。"""
    async def run():
        await _create_task("taskA")
        await _seed_judgment("taskA", "case-legacy", "v0")
        cached = await db.find_cached_judgment("case-legacy", parser_version="v1")
        assert cached is None

    asyncio.run(run())


def test_cache_miss_stale_beyond_ttl(tmp_db):
    """fetched_at 超過 max_age_days → miss。"""
    async def run():
        await _create_task("taskA")
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        await _seed_judgment("taskA", "case-old", "v1", fetched_at=old)
        # 30 天門檻 → 45 天前的資料必須 miss
        cached = await db.find_cached_judgment(
            "case-old", parser_version="v1", max_age_days=30
        )
        assert cached is None

    asyncio.run(run())


def test_cache_hit_within_ttl(tmp_db):
    """fetched_at 在 max_age_days 內 → hit。"""
    async def run():
        await _create_task("taskA")
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        await _seed_judgment("taskA", "case-recent", "v1", fetched_at=recent)
        cached = await db.find_cached_judgment(
            "case-recent", parser_version="v1", max_age_days=30
        )
        assert cached is not None
        assert cached["case_id"] == "case-recent"

    asyncio.run(run())


def test_cache_returns_newest_when_multiple_tasks(tmp_db):
    """多個 task 都存過同 case_id → 回傳最新 fetched_at 的那筆。"""
    async def run():
        await _create_task("taskA")
        await _create_task("taskB")
        older = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        await _seed_judgment("taskA", "case-shared", "v1",
                             fetched_at=older, reasoning="舊內容")
        # taskB 寫入時自動用當下時間戳，會比 taskA 新
        await _seed_judgment("taskB", "case-shared", "v1",
                             reasoning="新內容")
        cached = await db.find_cached_judgment("case-shared", parser_version="v1")
        assert cached is not None
        # ORDER BY fetched_at DESC LIMIT 1 → taskB 的較新那筆
        assert cached["reasoning"] == "新內容"

    asyncio.run(run())


# ─── migration backfill 驗證 ──────────────────────────────────────────


def test_migration_backfills_v0_on_legacy_rows(tmp_path, monkeypatch):
    """真正模擬 schema 升級：先手動建一個「舊版」task_judgments（無 parser_version 欄位），
    插資料後跑 init_db → 遷移 7 應該 ALTER 加欄位 + backfill 為 'v0'。"""
    import aiosqlite
    db_path = tmp_path / "migrate.db"
    schema_path = Path(__file__).resolve().parents[1] / "schema.sql"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "SCHEMA_PATH", str(schema_path))

    async def run():
        # Step 1: 建一個沒有 parser_version 欄位的 task_judgments（模擬遷移前狀態）
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, mode TEXT, keyword TEXT, "
                "filter_fields TEXT, status TEXT, created_at TEXT)"
            )
            await conn.execute(
                "CREATE TABLE task_judgments ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  task_id TEXT, case_id TEXT, court TEXT, date TEXT, source_url TEXT,"
                "  reasoning TEXT, main_text TEXT, facts TEXT,"
                "  cited_statutes TEXT, fetched_at TEXT"
                ")"
            )
            await conn.execute(
                "INSERT INTO tasks VALUES ('taskLegacy', 'keyword', '', '', 'done', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.execute(
                "INSERT INTO task_judgments "
                "(task_id, case_id, court, date, source_url, reasoning, main_text, "
                " facts, cited_statutes, fetched_at) "
                "VALUES ('taskLegacy', 'case-old', '院', '', '', '', '', '', '[]', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await conn.commit()

        # Step 2: 跑 init_db → 遷移 7 應觸發 ALTER + UPDATE backfill
        await db.init_db()

        # Step 3: 驗證 legacy row 的 parser_version 已 backfill 為 'v0'
        async with db._conn() as conn:
            cur = await conn.execute(
                "SELECT parser_version FROM task_judgments WHERE case_id = 'case-old'"
            )
            row = await cur.fetchone()
            assert row["parser_version"] == "v0"

        # Step 4: v0 legacy row 確實不會被 'v1' 查詢命中（漸進汰換）
        cached = await db.find_cached_judgment("case-old", parser_version="v1")
        assert cached is None

    asyncio.run(run())
