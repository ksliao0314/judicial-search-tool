"""SQLite 存取層（aiosqlite）：init_db + 四張資料表的 CRUD helpers。"""
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = "judgment_search.db"
SCHEMA_PATH = "schema.sql"


# ---------------------------------------------------------------------------
# 連線管理
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _conn() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        # 並發寫入（如 Stage 2.5 並行 fetch 的 create_task_judgment）取鎖時最多等 5s，
        # 而非立刻拋 SQLITE_BUSY。WAL 下單一 writer，這給瞬間排隊空間。
        await db.execute("PRAGMA busy_timeout=5000")
        yield db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """建立資料庫並套用 schema.sql（idempotent）。

    輕量遷移：
      - v0.1 → v0.2: task_judgments.full_text
      - v0.2 → v0.3: tasks.search_params（JSON，存 court/case_type/year/max_results/exhaustive 供 recovery 還原）
      - v0.3 → v0.4: task_judgments.extracted_citations（JSON array of citation tuples）
                     新增 synonym_dictionary 表（由 CREATE IF NOT EXISTS 自動建立）
      - v0.4 → v0.5: 新增 task_search_hits 表（兩階段搜尋的 stage 1 結果，由 CREATE IF NOT EXISTS 自動建立）
      - v0.5 → v0.6: analyses 新增 filter_fields / narrow_state（stage 3 用，legacy analyses 為 NULL）
    """
    async with _conn() as db:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            await db.executescript(f.read())

        # 遷移 1：task_judgments.full_text
        cursor = await db.execute("PRAGMA table_info(task_judgments)")
        tj_cols = {row["name"] for row in await cursor.fetchall()}
        if "full_text" not in tj_cols:
            await db.execute("ALTER TABLE task_judgments ADD COLUMN full_text TEXT")
            logger.info("遷移：task_judgments 新增 full_text 欄位")

        # 遷移 2：tasks.search_params
        cursor = await db.execute("PRAGMA table_info(tasks)")
        t_cols = {row["name"] for row in await cursor.fetchall()}
        if "search_params" not in t_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN search_params TEXT")
            logger.info("遷移：tasks 新增 search_params 欄位")

        # 遷移 3：task_judgments.extracted_citations
        if "extracted_citations" not in tj_cols:
            await db.execute("ALTER TABLE task_judgments ADD COLUMN extracted_citations TEXT")
            logger.info("遷移：task_judgments 新增 extracted_citations 欄位")

        # 遷移 4：synonym_dictionary.tier / discovery_count（配合精讀發現 + 自動分級）
        cursor = await db.execute("PRAGMA table_info(synonym_dictionary)")
        syn_cols = {row["name"] for row in await cursor.fetchall()}
        if "tier" not in syn_cols:
            await db.execute("ALTER TABLE synonym_dictionary ADD COLUMN tier TEXT DEFAULT 'candidate'")
            # Backfill: 用既有 corpus_hits 決定 tier
            await db.execute("""
                UPDATE synonym_dictionary
                   SET tier = CASE
                       WHEN corpus_hits IS NULL OR corpus_hits = 0 THEN 'rejected'
                       WHEN corpus_hits BETWEEN 1 AND 5            THEN 'likely_typo'
                       WHEN corpus_hits BETWEEN 6 AND 49           THEN 'candidate'
                       ELSE 'confirmed'
                   END
            """)
            logger.info("遷移：synonym_dictionary 新增 tier 欄位並 backfill")
        if "discovery_count" not in syn_cols:
            await db.execute("ALTER TABLE synonym_dictionary ADD COLUMN discovery_count INTEGER DEFAULT 0")
            logger.info("遷移：synonym_dictionary 新增 discovery_count 欄位")

        # 遷移 5：analyses.filter_fields / analyses.narrow_state（兩階段搜尋 stage 3 用）
        cursor = await db.execute("PRAGMA table_info(analyses)")
        a_cols = {row["name"] for row in await cursor.fetchall()}
        if "filter_fields" not in a_cols:
            await db.execute("ALTER TABLE analyses ADD COLUMN filter_fields TEXT")
            logger.info("遷移：analyses 新增 filter_fields 欄位")
        if "narrow_state" not in a_cols:
            await db.execute("ALTER TABLE analyses ADD COLUMN narrow_state TEXT")
            logger.info("遷移：analyses 新增 narrow_state 欄位")

        # 遷移 6：analyses.synthesis（stage 3 v2 redesign — 全部精讀完的綜合摘要 JSON）
        if "synthesis" not in a_cols:
            await db.execute("ALTER TABLE analyses ADD COLUMN synthesis TEXT")
            logger.info("遷移：analyses 新增 synthesis 欄位")

        # 遷移 11：analyses.synthesis_is_preliminary — 「初步 synthesis + 背景補齊 + 最終覆蓋」機制
        #   達門檻時（剩 ≤ 10/20/30 筆）先跑一次 synthesis，flag=1；律師看到 banner「初步結果」
        #   所有 missing judgment retry 完 → 重跑 synthesis 覆蓋、flag=0 → final 結果
        #   Recovery 時若已有部分結果，也走同路徑給律師救回來
        if "synthesis_is_preliminary" not in a_cols:
            await db.execute(
                "ALTER TABLE analyses ADD COLUMN synthesis_is_preliminary INTEGER DEFAULT 0"
            )
            logger.info("遷移：analyses 新增 synthesis_is_preliminary 欄位")

        # 遷移 13：analyses.scoring_input_tokens / scoring_output_tokens
        #   scoring 階段累積的 Haiku token 用量，隨 on_batch_done 增量寫入。
        #   目的：讓 preliminary synthesis / final synthesis / 手動升格 done 都能
        #   讀到完整的「scoring+synthesis 總成本」呈現給律師。原本 scoring tokens
        #   只活在 run_analysis_v2 的 in-memory 變數、preliminary 時無法取得，
        #   導致律師看到的 _usage 只有 synthesis 那段。
        if "scoring_input_tokens" not in a_cols:
            await db.execute(
                "ALTER TABLE analyses ADD COLUMN scoring_input_tokens INTEGER DEFAULT 0"
            )
            logger.info("遷移：analyses 新增 scoring_input_tokens 欄位")
        if "scoring_output_tokens" not in a_cols:
            await db.execute(
                "ALTER TABLE analyses ADD COLUMN scoring_output_tokens INTEGER DEFAULT 0"
            )
            logger.info("遷移：analyses 新增 scoring_output_tokens 欄位")

        # 遷移 10：tasks.search_domain（區分「法院判決」vs「憲法解釋」兩種搜尋模式）
        # 'judgment' = 走 FJUD search_judgments（原本所有任務）
        # 'interpretation' = 走 cons search_interpretations（釋字 + 新制憲判字）
        # 舊 row backfill 'judgment'（100% 正確，既有都是 FJUD 判決搜尋）
        if "search_domain" not in t_cols:
            await db.execute(
                "ALTER TABLE tasks ADD COLUMN search_domain TEXT NOT NULL DEFAULT 'judgment'"
            )
            logger.info("遷移：tasks 新增 search_domain 欄位（backfill 'judgment'）")

        # 遷移 8：stage25_inflight 表（schema.sql 的 CREATE IF NOT EXISTS 已建，
        # 這裡 no-op；列在這裡只是讓遷移紀錄完整，將來若要改欄位有地方加邏輯）

        # 遷移 7：task_judgments.parser_version（跨 task cache invalidation key）
        # 既存 row backfill 為 'v0'，不列入 find_cached_judgment 複用（要求 parser_version 相符才會命中）。
        # 新 fetch 會寫入 mcp_client.PARSER_VERSION，律師開新 task 時漸進累積可複用資料。
        if "parser_version" not in tj_cols:
            await db.execute("ALTER TABLE task_judgments ADD COLUMN parser_version TEXT")
            await db.execute(
                "UPDATE task_judgments SET parser_version = 'v0' WHERE parser_version IS NULL"
            )
            logger.info("遷移：task_judgments 新增 parser_version 欄位（backfill 'v0'）")
        # Cache lookup index 一律確保存在（fresh install + upgrade 兩條路徑都要）。
        # schema.sql 不建此 index 是因為 pre-v7 DB 欄位尚未 ALTER 進去。
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tj_case_lookup "
            "ON task_judgments(case_id, parser_version, fetched_at)"
        )

        # 遷移 14（v1.0.5）：analysis_results (analysis_id, case_id) 去重 + UNIQUE 約束
        # Bug：v1.0.5 前兩階段評分 Round 2 失敗時、backend 會另寫一筆 match='error' row、
        #      同 case 變成兩筆 row、completed 被多算、display「N/M」會超過 total。
        # Fix：code 層已改成 R2 失敗時 UPDATE 既有 row（保留 R1 結果 + 加失敗註記）；
        #      這裡的 migration 一次性清舊 dupe + 加 UNIQUE index 防未來 regression。
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_ar_analysis_case_unique'"
        )
        if not await cursor.fetchone():
            # 清 dupe step 1：同 (analysis_id, case_id) 有 error + 非 error 時、刪 error 那筆
            await db.execute("""
                DELETE FROM analysis_results
                WHERE id IN (
                    SELECT ar1.id FROM analysis_results ar1
                    WHERE ar1.match = 'error'
                    AND EXISTS (
                        SELECT 1 FROM analysis_results ar2
                        WHERE ar2.analysis_id = ar1.analysis_id
                        AND ar2.case_id = ar1.case_id
                        AND ar2.match != 'error'
                    )
                )
            """)
            # 清 dupe step 2：極罕見的多筆非 error 情況（手動資料汙染等）、只保留 id 最大的
            await db.execute("""
                DELETE FROM analysis_results
                WHERE id NOT IN (
                    SELECT MAX(id) FROM analysis_results
                    GROUP BY analysis_id, case_id
                )
            """)
            # 加 UNIQUE index（belt+suspenders：code fix + schema guard 雙保險）
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ar_analysis_case_unique "
                "ON analysis_results(analysis_id, case_id)"
            )
            logger.info(
                "遷移：analysis_results 清 dupe + 加 UNIQUE(analysis_id, case_id)"
            )

        # 遷移 15（v1.0.5.x）：analyses.skipped_case_ids — 記錄 stage2.5 fetch 失敗的 case_id
        # 背景：stage3 fetch 時、MCP 若回空（司法院 WAF throttle / 暫時性錯誤）、3-retry 都
        #   失敗 → skip 跑完後 warning 只有總筆數、律師無法得知是哪幾筆、也無法重試。
        # Schema：analyses.skipped_case_ids TEXT（JSON list of case_id 字串）、NULL = 無 skip。
        # 舊 row backfill 為 NULL（pre-v1.0.5.x 沒記錄這資訊、無從補）。
        if "skipped_case_ids" not in a_cols:
            await db.execute(
                "ALTER TABLE analyses ADD COLUMN skipped_case_ids TEXT"
            )
            logger.info("遷移：analyses 新增 skipped_case_ids 欄位")

        # 遷移 16：case_stars 從全域 (case_id PK) 改為 per-analysis (analysis_id, case_id PK)。
        # 每個分析的法律爭點不同、律師標記理由也不同 → 星標綁 analysis_id。
        # 使用者選「清空重來」：偵測到舊 schema（無 analysis_id 欄）→ DROP + 重建（不保留舊全域星標）。
        cursor = await db.execute("PRAGMA table_info(case_stars)")
        cs_cols = {row["name"] for row in await cursor.fetchall()}
        if cs_cols and "analysis_id" not in cs_cols:
            await db.execute("DROP TABLE case_stars")
            await db.execute(
                "CREATE TABLE case_stars ("
                " analysis_id TEXT NOT NULL,"
                " case_id     TEXT NOT NULL,"
                " starred_at  TEXT NOT NULL,"
                " PRIMARY KEY (analysis_id, case_id))"
            )
            logger.info("遷移：case_stars 改為 per-analysis（analysis_id, case_id）；清空舊全域星標")

        await db.commit()
    logger.info("資料庫初始化完成：%s", DB_PATH)


# ---------------------------------------------------------------------------
# Tier 分級邏輯（共用於新增 / 升降級）
# ---------------------------------------------------------------------------

def tier_from_corpus_hits(hits: int | None) -> str:
    """corpus_hits → tier。共用邏輯讓 insert 時與 backfill 時結論一致。"""
    if hits is None or hits == 0:
        return "rejected"
    if hits <= 5:
        return "likely_typo"
    if hits <= 49:
        return "candidate"
    return "confirmed"


# 自動升降級門檻（search 只用 confirmed）
_AUTO_PROMOTE_ACCEPTS = 3    # >= 3 次 ✓ 的 candidate / likely_typo 升為 confirmed
_AUTO_DEMOTE_REJECTS = 2     # >= 2 次 × 的 confirmed 降為 candidate


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

async def create_task(
    task_id: str,
    mode: str,
    keyword: str,
    filter_fields: str,
    status: str = "pending",
    search_params: dict | None = None,
    search_domain: str = "judgment",
) -> None:
    """建立 tasks row。search_params 以 JSON 字串存，供 server 重啟時 recovery 還原。
    search_domain: 'judgment'（法院判決、走 FJUD）| 'interpretation'（憲法解釋、走 cons）
    """
    sp_json = json.dumps(search_params or {}, ensure_ascii=False)
    async with _conn() as db:
        await db.execute(
            """
            INSERT INTO tasks (id, mode, keyword, filter_fields, status, search_params, search_domain, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, mode, keyword, filter_fields, status, sp_json, search_domain, _now()),
        )
        await db.commit()


async def get_task(task_id: str) -> dict | None:
    async with _conn() as db:
        cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return _row_to_dict(await cursor.fetchone())


async def list_tasks() -> list[dict]:
    async with _conn() as db:
        cursor = await db.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_task(task_id: str, **fields: object) -> None:
    """動態更新 tasks 資料表欄位。"""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [task_id]
    async with _conn() as db:
        await db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", vals)
        await db.commit()


async def get_pending_tasks() -> list[dict]:
    """回傳所有需要恢復的任務，供 server 重啟後恢復用。
    包含：task 本身 pending/running，或 task done 但有 analysis pending/running。
    """
    async with _conn() as db:
        cursor = await db.execute(
            """SELECT DISTINCT t.* FROM tasks t
               LEFT JOIN analyses a ON a.task_id = t.id
               WHERE t.status IN ('pending', 'running')
                  OR a.status IN ('pending', 'running')
               ORDER BY t.created_at"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# stage25_inflight：Stage 2.5 fetch 進行中的紀錄，給 server 重啟 recovery 用
# ---------------------------------------------------------------------------

async def record_stage25_inflight(task_id: str, case_ids: list[str]) -> None:
    """POST /fetch-judgments 呼叫時紀錄。已存在則覆寫（律師可能重開相同 task
    的新 fetch，取最新為準）。"""
    async with _conn() as db:
        await db.execute(
            """INSERT OR REPLACE INTO stage25_inflight
               (task_id, case_ids, started_at) VALUES (?, ?, ?)""",
            (task_id, json.dumps(case_ids, ensure_ascii=False), _now()),
        )
        await db.commit()


async def clear_stage25_inflight(task_id: str) -> None:
    """_run_stage25_fetch 正常結束 / cancel 時清除。不存在也 no-op。"""
    async with _conn() as db:
        await db.execute("DELETE FROM stage25_inflight WHERE task_id = ?", (task_id,))
        await db.commit()


async def list_stage25_inflight() -> list[dict]:
    """Server 啟動時掃、回傳需恢復的 fetch 工作。回傳 [{task_id, case_ids: list, started_at}]。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT task_id, case_ids, started_at FROM stage25_inflight "
            "ORDER BY started_at"
        )
        rows = await cursor.fetchall()
        out = []
        for r in rows:
            try:
                case_ids = json.loads(r["case_ids"])
            except (json.JSONDecodeError, TypeError):
                continue   # 壞資料跳過
            out.append({
                "task_id": r["task_id"],
                "case_ids": case_ids,
                "started_at": r["started_at"],
            })
        return out


# ---------------------------------------------------------------------------
# synonym_dictionary（L1 事務所資產：累積的同義詞字典）
# ---------------------------------------------------------------------------

async def get_all_synonyms() -> list[dict]:
    """回傳 synonym_dictionary 條目，供設定頁管理用。

    過濾：
      - 排除 rejected 和 likely_typo（已拒絕的不再推薦）
      - 排除 variant 只有 1 個字且 canonical 是法規名（法規縮寫不會只有一個字）

    回傳格式：[{canonical, variant, tier, corpus_hits, accept_count, reject_count, ...}, ...]
    前端按 canonical 分組顯示。
    """
    _LAW_SUFFIXES = ('法', '規則', '條例', '辦法')
    async with _conn() as db:
        cursor = await db.execute(
            """
            SELECT canonical, variant, source, tier, corpus_hits,
                   accept_count, reject_count, discovery_count, first_seen_at
              FROM synonym_dictionary
             WHERE tier IN ('confirmed', 'candidate')
             ORDER BY canonical,
               CASE tier WHEN 'confirmed' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
               COALESCE(corpus_hits, 0) DESC
            """
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            row = dict(r)
            # 單字 variant 不推薦（搜尋不會只搜一個字）
            if len(row["variant"]) <= 1:
                continue
            results.append(row)
        return results


async def get_confirmed_synonym_groups() -> dict[str, list[str]]:
    """回傳所有 confirmed 同義詞群組。

    回傳 {canonical: [variant1, variant2, ...]}，每組包含 canonical 本身。
    用於組合詞子字串替換展開。
    """
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT canonical, variant FROM synonym_dictionary WHERE tier = 'confirmed'"
        )
        rows = await cursor.fetchall()
    groups: dict[str, list[str]] = {}
    for r in rows:
        canon = r["canonical"]
        if canon not in groups:
            groups[canon] = [canon]  # canonical 本身也是一個變體
        v = r["variant"]
        if v not in groups[canon]:
            groups[canon].append(v)
    return groups


async def delete_synonym_variant(canonical: str, variant: str) -> bool:
    """硬刪除字典中特定 canonical/variant 組合。

    回傳 True 表示確實刪除了一列；False 表示找不到該條目。
    """
    async with _conn() as db:
        cur = await db.execute(
            "DELETE FROM synonym_dictionary WHERE canonical = ? AND variant = ?",
            (canonical, variant),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_synonyms(canonical: str, min_tier: str | None = None) -> list[dict]:
    """查字典中某 canonical 已累積的 variants。

    min_tier:
      None = 回傳所有 tier（給 UI preview 看）
      'confirmed' = 只回傳 confirmed（給 search pipeline 自動展開用）
    """
    tier_filter = ""
    params = [canonical]
    if min_tier == "confirmed":
        tier_filter = "AND tier = 'confirmed'"

    async with _conn() as db:
        cursor = await db.execute(
            f"""
            SELECT variant, source, tier, corpus_hits, accept_count, reject_count,
                   discovery_count, usage_count, first_seen_at, last_used_at
              FROM synonym_dictionary
             WHERE canonical = ? {tier_filter}
             ORDER BY
               CASE tier WHEN 'confirmed' THEN 0 WHEN 'candidate' THEN 1
                         WHEN 'likely_typo' THEN 2 ELSE 3 END,
               (accept_count - reject_count) DESC,
               COALESCE(corpus_hits, 0) DESC
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def upsert_synonyms(
    canonical: str,
    variants: list[str],
    source: str = "claude",
    corpus_hits_map: dict[str, int] | None = None,
    discovery_count_delta: dict[str, int] | None = None,
) -> None:
    """寫入 / 更新一批同義詞條目。

    新增時：用 corpus_hits 決定 tier；若 corpus_hits 未知（None）→ tier='candidate'
    既存時：更新 last_used_at；若新 corpus_hits 已知，重算 tier（但不覆蓋律師手動升降結果，
            靠 reject_count/accept_count 的紀錄讓下次 feedback 還能調整）
    discovery_count_delta: 每個 variant 本次被精讀發現幾次（+= 到 discovery_count）
    """
    corpus_hits_map = corpus_hits_map or {}
    discovery_count_delta = discovery_count_delta or {}
    async with _conn() as db:
        for v in variants:
            hits = corpus_hits_map.get(v)
            dc_delta = discovery_count_delta.get(v, 0)
            tier = tier_from_corpus_hits(hits) if hits is not None else "candidate"
            await db.execute(
                """
                INSERT INTO synonym_dictionary
                  (canonical, variant, source, tier, corpus_hits,
                   usage_count, discovery_count, first_seen_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(canonical, variant) DO UPDATE SET
                  last_used_at    = excluded.last_used_at,
                  corpus_hits     = COALESCE(excluded.corpus_hits, synonym_dictionary.corpus_hits),
                  discovery_count = synonym_dictionary.discovery_count + ?,
                  tier = CASE
                    -- 已有 rejected / confirmed 且有律師回饋紀錄 → 保留不動
                    WHEN synonym_dictionary.tier = 'rejected' AND synonym_dictionary.reject_count > 0 THEN synonym_dictionary.tier
                    WHEN synonym_dictionary.tier = 'confirmed' AND synonym_dictionary.accept_count > 0 THEN synonym_dictionary.tier
                    -- 否則依新 corpus_hits 重算（若 excluded.corpus_hits 為 NULL 保留舊值）
                    ELSE CASE
                      WHEN COALESCE(excluded.corpus_hits, synonym_dictionary.corpus_hits) IS NULL
                           OR COALESCE(excluded.corpus_hits, synonym_dictionary.corpus_hits) = 0 THEN 'rejected'
                      WHEN COALESCE(excluded.corpus_hits, synonym_dictionary.corpus_hits) <= 5  THEN 'likely_typo'
                      WHEN COALESCE(excluded.corpus_hits, synonym_dictionary.corpus_hits) <= 49 THEN 'candidate'
                      ELSE 'confirmed'
                    END
                  END
                """,
                (canonical, v, source, tier, hits, dc_delta, _now(), _now(), dc_delta),
            )
        # 記一次 usage_count（canonical 級別：所有該 canonical 的 rows +1）
        await db.execute(
            "UPDATE synonym_dictionary SET usage_count = usage_count + 1 WHERE canonical = ?",
            (canonical,),
        )
        await db.commit()


async def record_synonym_feedback(canonical: str, variant: str, accepted: bool) -> None:
    """律師在 UI 點擊「✓ 確認用」或「× 拒絕」時回報。
    達 threshold 自動升 / 降級 tier。
    """
    col = "accept_count" if accepted else "reject_count"
    async with _conn() as db:
        await db.execute(
            f"UPDATE synonym_dictionary SET {col} = {col} + 1 WHERE canonical = ? AND variant = ?",
            (canonical, variant),
        )
        # 自動升降級
        if accepted:
            await db.execute(
                """
                UPDATE synonym_dictionary
                   SET tier = 'confirmed'
                 WHERE canonical = ? AND variant = ?
                   AND accept_count >= ?
                   AND tier != 'confirmed'
                """,
                (canonical, variant, _AUTO_PROMOTE_ACCEPTS),
            )
        else:
            await db.execute(
                """
                UPDATE synonym_dictionary
                   SET tier = 'rejected'
                 WHERE canonical = ? AND variant = ?
                   AND reject_count >= ?
                """,
                (canonical, variant, _AUTO_DEMOTE_REJECTS),
            )
        await db.commit()


# ---------------------------------------------------------------------------
# task_prefilter_results（理由預篩持久化 + recovery）
# ---------------------------------------------------------------------------

MAX_PREFILTER_RECOVERY_ATTEMPTS = 3


async def init_prefilter_result(
    task_id: str, narrow_json: str, total: int,
) -> None:
    """律師手動啟動預篩時呼叫。INSERT OR REPLACE 整筆、recovery_attempts 重置為 0。

    narrow_json 必須是正規化後的 JSON 字串（sort_keys=True）— 這是後續 ownership
    check 的 key：work 記住自己的 narrow_json，for-loop 內比對 DB 若不同就 abort。
    """
    async with _conn() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO task_prefilter_results
              (task_id, narrow, matched_case_ids, total, matched,
               status, recovery_attempts, started_at, finished_at)
            VALUES (?, ?, '[]', ?, 0, 'running', 0, ?, NULL)
            """,
            (task_id, narrow_json, total, _now()),
        )
        await db.commit()


async def update_prefilter_progress(
    task_id: str, matched_case_ids_json: str, matched: int,
) -> None:
    """跑中更新進度 — 只動 matched_case_ids / matched，不碰 narrow / attempts / status。"""
    async with _conn() as db:
        await db.execute(
            """
            UPDATE task_prefilter_results
               SET matched_case_ids = ?, matched = ?
             WHERE task_id = ?
            """,
            (matched_case_ids_json, matched, task_id),
        )
        await db.commit()


async def mark_prefilter_done(
    task_id: str, matched_case_ids_json: str, matched: int,
) -> None:
    """正常完成 — status='done' + finished_at。"""
    async with _conn() as db:
        await db.execute(
            """
            UPDATE task_prefilter_results
               SET matched_case_ids = ?, matched = ?,
                   status = 'done', finished_at = ?
             WHERE task_id = ?
            """,
            (matched_case_ids_json, matched, _now(), task_id),
        )
        await db.commit()


async def mark_prefilter_cancelled(task_id: str) -> None:
    """中斷 — status='cancelled' + finished_at。用於：
    - 非 ownership-lost 的 exception 路徑
    - recovery 達 MAX_ATTEMPTS 放棄時
    """
    async with _conn() as db:
        await db.execute(
            """
            UPDATE task_prefilter_results
               SET status = 'cancelled', finished_at = ?
             WHERE task_id = ?
            """,
            (_now(), task_id),
        )
        await db.commit()


async def increment_prefilter_attempts(task_id: str) -> int:
    """recovery 觸發時 +1，回傳 new attempts 值。"""
    async with _conn() as db:
        await db.execute(
            """
            UPDATE task_prefilter_results
               SET recovery_attempts = recovery_attempts + 1
             WHERE task_id = ?
            """,
            (task_id,),
        )
        cursor = await db.execute(
            "SELECT recovery_attempts FROM task_prefilter_results WHERE task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row["recovery_attempts"] if row else 0


async def get_prefilter_result(task_id: str) -> dict | None:
    """供前端 openTask / ownership check 讀取。回傳原始 row（JSON 欄位仍為字串）。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT * FROM task_prefilter_results WHERE task_id = ?",
            (task_id,),
        )
        return _row_to_dict(await cursor.fetchone())


async def list_running_prefilters() -> list[dict]:
    """recovery 掃用：所有 status='running' 的 row。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT * FROM task_prefilter_results WHERE status = 'running'"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def delete_prefilter_result(task_id: str) -> None:
    """律師按「清除」或 task 被刪時 cascade。"""
    async with _conn() as db:
        await db.execute(
            "DELETE FROM task_prefilter_results WHERE task_id = ?", (task_id,),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# case_stars
# ---------------------------------------------------------------------------

async def star_case(analysis_id: str, case_id: str) -> None:
    """在某分析層加星標。INSERT OR REPLACE — 重複 star 只刷 starred_at。"""
    async with _conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO case_stars (analysis_id, case_id, starred_at) "
            "VALUES (?, ?, ?)",
            (analysis_id, case_id, _now()),
        )
        await db.commit()


async def unstar_case(analysis_id: str, case_id: str) -> None:
    """取消某分析層的星標。不存在時 DELETE 為 no-op。"""
    async with _conn() as db:
        await db.execute(
            "DELETE FROM case_stars WHERE analysis_id = ? AND case_id = ?",
            (analysis_id, case_id),
        )
        await db.commit()


async def list_starred_cases(analysis_id: str) -> list[str]:
    """回傳某分析層已星標 case_id（newest first）。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT case_id FROM case_stars WHERE analysis_id = ? ORDER BY starred_at DESC",
            (analysis_id,),
        )
        rows = await cursor.fetchall()
        return [r["case_id"] for r in rows]


# ---------------------------------------------------------------------------
# case_highlights（律師 reader 黃底劃記、跨裝置同步）
# ---------------------------------------------------------------------------

async def list_case_highlights(case_id: str) -> list[dict]:
    """回傳某 case_id 的所有黃底劃記（created 升冪、讓律師先劃的先顯示）。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT id, text, before_ctx, after_ctx, created_at "
            "FROM case_highlights WHERE case_id = ? ORDER BY created_at ASC",
            (case_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def add_case_highlight(
    case_id: str, text: str, before_ctx: str, after_ctx: str
) -> int:
    """新增一筆劃記。return auto-increment id。dedup 在 route 層做、DB 不強制 unique
    （允許律師對同一段文字重複標記的 edge case、不致 constraint error）。
    """
    async with _conn() as db:
        cursor = await db.execute(
            "INSERT INTO case_highlights (case_id, text, before_ctx, after_ctx, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (case_id, text, before_ctx, after_ctx, _now()),
        )
        await db.commit()
        return cursor.lastrowid


async def remove_case_highlight(highlight_id: int) -> bool:
    """刪除指定 id 的劃記。return True 若真的刪到 row。"""
    async with _conn() as db:
        cursor = await db.execute(
            "DELETE FROM case_highlights WHERE id = ?", (highlight_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_case_analyses(case_id: str) -> list[dict]:
    """回傳某 case_id 歷來在所有 task / analysis 中的精讀結果（case-level 聚合視圖）。

    跨 task 反查：analysis_results → analyses → tasks。
    排除 match='error' 與 score IS NULL（未完成 / 失敗的分析對律師無參考價值）。
    最新分析先出（analyzed_at DESC）。
    """
    async with _conn() as db:
        cursor = await db.execute(
            """
            SELECT
                ar.case_id,
                ar.match,
                ar.score,
                ar.excerpt,
                ar.reason,
                ar.analyzed_at,
                a.id         AS analysis_id,
                a.question,
                a.synthesis,
                a.created_at AS analysis_created_at,
                t.id         AS task_id,
                t.keyword    AS task_keyword,
                t.created_at AS task_created_at,
                t.search_domain
              FROM analysis_results ar
              JOIN analyses a ON ar.analysis_id = a.id
              JOIN tasks     t ON a.task_id = t.id
             WHERE ar.case_id = ?
               AND ar.match != 'error'
               AND ar.score IS NOT NULL
             ORDER BY ar.analyzed_at DESC
            """,
            (case_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def delete_task(task_id: str) -> dict:
    """刪除任務及所有附屬資料（case_stars → analysis_results → analyses → task_judgments → task_search_hits → tasks）。

    回傳刪除統計，供 API 層回報。schema 沒有 CASCADE 宣告，手動依序刪。
    case_stars 自 migration 16 起為 per-analysis（綁 analysis_id），隨任務底下的 analyses
    一併清除，否則會留下 analysis_id 已不存在的孤兒星標 row（永遠 join 不到、無 UI 可清）。
    """
    async with _conn() as db:
        # 先刪 analysis_results（透過任務底下的 analyses 反查）
        cur = await db.execute(
            """
            DELETE FROM analysis_results
             WHERE analysis_id IN (SELECT id FROM analyses WHERE task_id = ?)
            """,
            (task_id,),
        )
        deleted_results = cur.rowcount

        # case_stars 自 migration 16 改為 per-analysis；刪 task 連帶刪其 analyses，故星標
        # 也要清（必須在 DELETE analyses 之前，subquery 要 analyses 還在）。
        await db.execute(
            """
            DELETE FROM case_stars
             WHERE analysis_id IN (SELECT id FROM analyses WHERE task_id = ?)
            """,
            (task_id,),
        )

        cur = await db.execute("DELETE FROM analyses WHERE task_id = ?", (task_id,))
        deleted_analyses = cur.rowcount

        cur = await db.execute("DELETE FROM task_judgments WHERE task_id = ?", (task_id,))
        deleted_judgments = cur.rowcount

        cur = await db.execute("DELETE FROM task_search_hits WHERE task_id = ?", (task_id,))
        deleted_hits = cur.rowcount

        # 清 stage25_inflight（若任務刪除時 Stage 2.5 還在進行）— 下次啟動不會殘留
        await db.execute("DELETE FROM stage25_inflight WHERE task_id = ?", (task_id,))

        # 清 task_prefilter_results — prefilter row 綁在 task，task 消失 row 也消失
        await db.execute("DELETE FROM task_prefilter_results WHERE task_id = ?", (task_id,))

        cur = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        deleted_tasks = cur.rowcount

        await db.commit()

    return {
        "tasks": deleted_tasks,
        "analyses": deleted_analyses,
        "task_judgments": deleted_judgments,
        "task_search_hits": deleted_hits,
        "analysis_results": deleted_results,
    }


# ---------------------------------------------------------------------------
# task_search_hits — Stage 1 廣搜結果（無全文）
# ---------------------------------------------------------------------------

async def bulk_insert_task_search_hits(task_id: str, hits: list[dict]) -> int:
    """批次寫入 stage 1 結果。

    `hits` 來自 MCP search_judgments，每筆預期含：
      jid (= case_id) / court / date / url / cause / summary
    使用 INSERT OR IGNORE — 同 task 內 case_id UNIQUE，重複呼叫安全。
    回傳實際新增筆數。
    """
    if not hits:
        return 0
    now = _now()
    rows = []
    for h in hits:
        case_id = h.get("jid") or h.get("case_id") or ""
        if not case_id:
            continue
        rows.append((
            task_id,
            case_id,
            h.get("court", ""),
            h.get("date", ""),
            h.get("url") or h.get("source_url", ""),
            h.get("cause"),
            h.get("summary"),
            now,
        ))
    if not rows:
        return 0
    async with _conn() as db:
        await db.executemany(
            """
            INSERT OR IGNORE INTO task_search_hits
              (task_id, case_id, court, date, source_url, cause, summary, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        # SQLite 不能直接拿 executemany 的總 rowcount；改 query 一次驗總數
        cur = await db.execute(
            "SELECT COUNT(*) FROM task_search_hits WHERE task_id = ?", (task_id,)
        )
        total = (await cur.fetchone())[0]
        await db.commit()
    return total


async def get_task_search_hits(task_id: str) -> list[dict]:
    """回傳整份 stage 1 結果（依 date 降序）。前端拿到後做 stage 2 client filter。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT * FROM task_search_hits WHERE task_id = ? ORDER BY date DESC, id",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def count_task_search_hits(task_id: str) -> int:
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM task_search_hits WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_task_search_hit(task_id: str, case_id: str) -> dict | None:
    """單筆查詢，給 reader 即時讀取單筆判決時用（決定要不要進一步打 MCP get_judgment）。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT * FROM task_search_hits WHERE task_id = ? AND case_id = ?",
            (task_id, case_id),
        )
        return _row_to_dict(await cursor.fetchone())


# ---------------------------------------------------------------------------
# task_judgments
# ---------------------------------------------------------------------------

async def create_task_judgment(
    task_id: str,
    case_id: str,
    court: str,
    date: str,
    source_url: str,
    reasoning: str | None,
    main_text: str | None,
    facts: str | None,
    cited_statutes: list[str] | None,
    parser_version: str,
    full_text: str | None = None,
    extracted_citations: list[list] | None = None,
    judges: list[str] | None = None,
    parties: dict | None = None,
    cause: str | None = None,
) -> None:
    """extracted_citations：list of [law, article, sub, paragraph, item, subitem] tuple-like lists。
    由 CitationExtractor 對 full_text 解析得到，供 filter-time 做 tuple match。

    judges：法官姓名 list。parties：{原告/被告/訴訟代理人/...}。cause：案由字串。
    三者由 MCP parser 解析，用於前端閱讀器 header 顯示。

    parser_version：必填。一律傳 mcp_client.PARSER_VERSION，除非是跨 task cache 複用
    既存 row（此時傳來源 row 的 parser_version，代表「來自同一版 parser 的副本」）。
    """
    cited_json = json.dumps(cited_statutes or [], ensure_ascii=False)
    citations_json = json.dumps(extracted_citations or [], ensure_ascii=False)
    judges_json = json.dumps(judges or [], ensure_ascii=False) if judges else None
    parties_json = json.dumps(parties or {}, ensure_ascii=False) if parties else None
    async with _conn() as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO task_judgments
              (task_id, case_id, court, date, source_url,
               reasoning, main_text, facts, cited_statutes, full_text,
               extracted_citations, judges, parties, cause,
               parser_version, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, case_id, court, date, source_url,
             reasoning, main_text, facts, cited_json, full_text,
             citations_json, judges_json, parties_json, cause,
             parser_version, _now()),
        )
        await db.commit()


async def find_cached_judgment(
    case_id: str,
    parser_version: str,
    max_age_days: int = 30,
) -> dict | None:
    """跨 task cache lookup：任何既存 task_judgments row 只要 case_id +
    parser_version 相符且 fetched_at 在 max_age_days 內，就可以被新 task 直接複用。

    回傳完整 row dict（含 JSON 欄位仍為字串，呼叫端自行 json.loads），或 None。

    max_age_days 預設 30，對齊 MCP fork 的 file cache TTL。判決書發布後基本不會改動，
    30 天是保守上限（實務上可以更久，但配合 MCP 既有 TTL 讓兩層一致便於推理）。
    """
    from datetime import timedelta
    threshold = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    async with _conn() as db:
        cursor = await db.execute(
            """
            SELECT * FROM task_judgments
             WHERE case_id = ?
               AND parser_version = ?
               AND fetched_at >= ?
             ORDER BY fetched_at DESC
             LIMIT 1
            """,
            (case_id, parser_version, threshold),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_task_judgments(task_id: str) -> list[dict]:
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT * FROM task_judgments WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def count_task_judgments(task_id: str) -> int:
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM task_judgments WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_facts_coverage(task_id: str) -> dict:
    """回傳此 task 的 facts 欄位覆蓋率 {total, with_facts}。

    用於 UI 提示：當 with_facts/total < 0.2，「同時精讀事實」勾選等同無效（民事/
    行政「事實及理由」合併段時 facts 為空，是 MCP parser 既存設計）。
    `total = 0` 表示尚未抓全文，呼叫端應略過提示。
    """
    async with _conn() as db:
        cursor = await db.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN facts IS NOT NULL AND TRIM(facts) <> '' THEN 1 ELSE 0 END) AS with_facts
            FROM task_judgments
            WHERE task_id = ?
            """,
            (task_id,),
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {"total": 0, "with_facts": 0}
        return {"total": row[0], "with_facts": row[1] or 0}


# ---------------------------------------------------------------------------
# analyses
# ---------------------------------------------------------------------------

async def create_analysis(
    analysis_id: str,
    task_id: str,
    question: str,
    ai_read_field: str,
    total: int | None = None,
    status: str = "pending",
    filter_fields: str | None = None,
    narrow_state: dict | None = None,
) -> None:
    """filter_fields / narrow_state 為 stage 3 兩階段流程用；legacy 路徑可省略（NULL）。"""
    narrow_json = json.dumps(narrow_state, ensure_ascii=False) if narrow_state else None
    async with _conn() as db:
        await db.execute(
            """
            INSERT INTO analyses
              (id, task_id, question, ai_read_field, filter_fields, narrow_state,
               status, total, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (analysis_id, task_id, question, ai_read_field,
             filter_fields, narrow_json,
             status, total, _now()),
        )
        await db.commit()


async def get_analysis(analysis_id: str) -> dict | None:
    async with _conn() as db:
        cursor = await db.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,))
        return _row_to_dict(await cursor.fetchone())


async def list_analyses(task_id: str) -> list[dict]:
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT * FROM analyses WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_analysis_if_status(
    analysis_id: str, expected_status: str, **fields: object
) -> bool:
    """Atomic check-and-set：只在 analysis.status 等於 expected_status 時 update。

    用於防止 double-click race（例如 /resume endpoint 被 double-click 時、保證只有一次
    UPDATE 成功、另一次 rowcount=0 → 回 409）。
    回傳 True = update 成功、False = status 不符（或 analysis 不存在）。
    """
    if not fields:
        return False
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [analysis_id, expected_status]
    async with _conn() as db:
        cursor = await db.execute(
            f"UPDATE analyses SET {cols} WHERE id = ? AND status = ?",
            vals,
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_analysis(analysis_id: str, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [analysis_id]
    async with _conn() as db:
        await db.execute(f"UPDATE analyses SET {cols} WHERE id = ?", vals)
        await db.commit()


async def increment_analysis_progress(
    analysis_id: str, completed_delta: int = 1, match_delta: int = 0
) -> None:
    """原子性遞增 completed 與 match_count。"""
    async with _conn() as db:
        await db.execute(
            """
            UPDATE analyses
               SET completed   = completed   + ?,
                   match_count = match_count + ?
             WHERE id = ?
            """,
            (completed_delta, match_delta, analysis_id),
        )
        await db.commit()


async def increment_scoring_tokens(
    analysis_id: str, input_delta: int, output_delta: int,
) -> None:
    """原子性遞增 scoring 階段的累積 token 用量。

    從 runner.on_batch_done 用「本次 batch 新增量」呼叫；retry iteration 另起一個
    run_analysis_v2 時也可以用同樣路徑持續累積（run 內部 local counter 重置、但 DB
    值用增量寫入、不會丟失之前 run 的累積）。

    Preliminary synthesis / final synthesis / 手動升格 都從 DB 讀這 2 欄合進 _usage，
    律師看到的 tokens/成本顯示才完整。
    """
    if input_delta <= 0 and output_delta <= 0:
        return
    async with _conn() as db:
        await db.execute(
            """
            UPDATE analyses
               SET scoring_input_tokens  = scoring_input_tokens  + ?,
                   scoring_output_tokens = scoring_output_tokens + ?
             WHERE id = ?
            """,
            (max(0, input_delta), max(0, output_delta), analysis_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# analysis_results
# ---------------------------------------------------------------------------

async def create_analysis_result(
    analysis_id: str,
    case_id: str,
    match: str,
    score: int | None,
    excerpt: str | None,
    reason: str | None,
) -> None:
    async with _conn() as db:
        await db.execute(
            """
            INSERT INTO analysis_results
              (analysis_id, case_id, match, score, excerpt, reason, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (analysis_id, case_id, match, score, excerpt, reason, _now()),
        )
        await db.commit()


async def delete_analysis_results(analysis_id: str) -> int:
    """刪除某 analysis 的全部 analysis_results，回傳刪除筆數。"""
    async with _conn() as db:
        cursor = await db.execute(
            "DELETE FROM analysis_results WHERE analysis_id = ?",
            (analysis_id,),
        )
        await db.commit()
        return cursor.rowcount


async def update_analysis_result(
    analysis_id: str,
    case_id: str,
    match: str,
    score: int | None,
    excerpt: str | None,
    reason: str | None,
) -> None:
    """覆蓋某 analysis 的單筆 result（兩階段評分 Round 2 用）。"""
    async with _conn() as db:
        await db.execute(
            """
            UPDATE analysis_results
               SET match = ?, score = ?, excerpt = ?, reason = ?, analyzed_at = ?
             WHERE analysis_id = ? AND case_id = ?
            """,
            (match, score, excerpt, reason, _now(), analysis_id, case_id),
        )
        await db.commit()


async def get_analysis_results(analysis_id: str) -> list[dict]:
    """回傳某 analysis 的全部 analysis_results（不分 score）。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT case_id, score, match FROM analysis_results WHERE analysis_id = ?",
            (analysis_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def count_analysis_results(analysis_id: str) -> int:
    """回傳某 analysis 已寫入的 result 筆數（graceful abort 決定要跑 partial synthesis 還是 cancelled 用）。"""
    async with _conn() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM analysis_results WHERE analysis_id = ?",
            (analysis_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def list_analysis_results_feed(analysis_id: str, limit: int = 60) -> dict:
    """給 card live feed 用：回最新 N 筆顯示列 + 真實總筆數。

    升序是為了 feed 插入後 scroll-to-bottom 時最新在底下。limit 只限「顯示列數」（避免
    SSE 中途重連時一次 populate 上千筆 DOM）；total 回真實總筆數 —— 否則重開卡片時
    backfill 會把 count 設成 limit、讓 >60 筆的分析「即時回傳筆數」從實際值倒退成 60。
    """
    async with _conn() as db:
        cursor = await db.execute(
            """
            SELECT case_id, score, match
              FROM analysis_results
             WHERE analysis_id = ?
             ORDER BY analyzed_at DESC
             LIMIT ?
            """,
            (analysis_id, limit),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        cur2 = await db.execute(
            "SELECT COUNT(*) FROM analysis_results WHERE analysis_id = ?",
            (analysis_id,),
        )
        total = (await cur2.fetchone())[0]
        return {"items": list(reversed(rows)), "total": total}


async def get_analysis_results_scored(analysis_id: str) -> list[dict]:
    """回傳 score > 0 的 analysis_results，帶 task_judgments 的 court 資訊。

    給 stage 3 v2 synthesis 用 — 只需要相關的判決（無關的 score=0 不送給 synthesis prompt）。
    依 score 降序 + case_id 排序（score tie-break 穩定）。
    """
    async with _conn() as db:
        cursor = await db.execute(
            """
            SELECT ar.case_id, ar.score, ar.reason, ar.excerpt,
                   tj.court, tj.date
              FROM analysis_results ar
              LEFT JOIN task_judgments tj
                ON tj.case_id = ar.case_id
                AND tj.task_id = (SELECT task_id FROM analyses WHERE id = ?)
             WHERE ar.analysis_id = ?
               AND ar.score IS NOT NULL
               AND ar.score > 0
             ORDER BY ar.score DESC, ar.case_id
            """,
            (analysis_id, analysis_id),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def set_analysis_synthesis(analysis_id: str, synthesis: dict, is_preliminary: bool = False) -> None:
    """寫入 analyses.synthesis（JSON）+ is_preliminary flag。

    is_preliminary=True：還在跑 missing judgments retry，律師看到的是「初步結果」版
    is_preliminary=False：final synthesis，覆蓋先前 preliminary 並把 flag 清 0
    """
    async with _conn() as db:
        await db.execute(
            "UPDATE analyses SET synthesis = ?, synthesis_is_preliminary = ? WHERE id = ?",
            (json.dumps(synthesis, ensure_ascii=False), 1 if is_preliminary else 0, analysis_id),
        )
        await db.commit()


async def list_missing_judgments(task_id: str, analysis_id: str) -> list[str]:
    """回傳 task_judgments 中「尚未有 analysis_results row」的 case_ids。

    「missing」定義：task_judgments 存在但 analysis_results 裡完全沒出現過的 case_id —
    代表 scoring loop 還沒跑到或被中斷。match=error 的 row 不算 missing（已經重試 3 次耗盡）。
    """
    async with _conn() as db:
        cursor = await db.execute(
            """
            SELECT tj.case_id
              FROM task_judgments tj
             WHERE tj.task_id = ?
               AND NOT EXISTS (
                   SELECT 1 FROM analysis_results ar
                    WHERE ar.analysis_id = ? AND ar.case_id = tj.case_id
               )
            """,
            (task_id, analysis_id),
        )
        return [row["case_id"] for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# 多分析層 JOIN 查詢（清單頁主查詢）
# ---------------------------------------------------------------------------

async def get_judgments_with_analyses(
    task_id: str,
    primary_analysis_id: str,
    secondary_analysis_id: str | None = None,
    min_score: int | None = None,
    court_filter: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict] | dict:
    """
    回傳 task_judgments，左 JOIN 主分析層與（可選）副分析層結果。
    主分析層 score DESC 排序，NULL 排最後。

    若提供 limit/offset 則回傳 {items, total, limit, offset}；
    否則回傳 list[dict]（向後相容）。
    """
    filters = ["tj.task_id = :task_id"]
    params: dict = {"task_id": task_id, "primary_id": primary_analysis_id}

    if secondary_analysis_id:
        params["secondary_id"] = secondary_analysis_id

    if min_score is not None:
        # 設 min_score 表示律師要看「已分析且達門檻」的判決。
        # 排除 match='error'（score=NULL）避免分析失敗的案件混進清單誤導。
        # 未設 min_score 時保留 NULL rows（代表「尚未跑完 / error」，UI 仍可區分顯示）。
        filters.append("p.score IS NOT NULL AND p.score >= :min_score AND p.match != 'error'")
        params["min_score"] = min_score

    if court_filter:
        filters.append("tj.court LIKE :court_filter")
        params["court_filter"] = f"%{court_filter}%"

    if year_from is not None:
        filters.append("CAST(substr(tj.date, 1, 4) AS INTEGER) >= :year_from")
        params["year_from"] = year_from

    if year_to is not None:
        filters.append("CAST(substr(tj.date, 1, 4) AS INTEGER) <= :year_to")
        params["year_to"] = year_to

    where = " AND ".join(filters)

    # 只有提供 secondary_analysis_id 時才加 s.* 到 SELECT + JOIN
    # 否則舊版 SELECT 永遠引用 s.score 會觸發 "no such column: s.score"
    if secondary_analysis_id:
        secondary_join = (
            "LEFT JOIN analysis_results s "
            "ON s.case_id = tj.case_id AND s.analysis_id = :secondary_id"
        )
        secondary_cols = "s.score AS secondary_score, s.match AS secondary_match,"
    else:
        secondary_join = ""
        secondary_cols = "NULL AS secondary_score, NULL AS secondary_match,"

    base_from = f"""
        FROM task_judgments tj
        LEFT JOIN analysis_results p
            ON p.case_id = tj.case_id AND p.analysis_id = :primary_id
        {secondary_join}
        WHERE {where}
    """

    order_by = "ORDER BY CASE WHEN p.score IS NULL THEN 1 ELSE 0 END, p.score DESC"

    select_cols = f"""
            tj.case_id, tj.court, tj.date, tj.source_url,
            p.score     AS primary_score,
            p.match     AS primary_match,
            p.excerpt   AS primary_excerpt,
            p.reason    AS primary_reason,
            {secondary_cols}
            tj.fetched_at
    """

    sql = f"SELECT {select_cols} {base_from} {order_by}"

    paginated = limit is not None and offset is not None
    if paginated:
        sql += " LIMIT :_limit OFFSET :_offset"
        params["_limit"] = limit
        params["_offset"] = offset

    async with _conn() as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        items = [dict(r) for r in rows]

        if not paginated:
            return items

        count_sql = f"SELECT COUNT(*) {base_from}"
        cursor2 = await db.execute(count_sql, params)
        total = (await cursor2.fetchone())[0]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
