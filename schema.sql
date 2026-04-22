-- 任務本體
--
-- `mode` 欄位歷史：原本是 input mode（'keyword' | 'semantic'，來自 v0.1 spec），
-- 現在保留但語意不同 — **這個欄位專指搜尋 pipeline mode**：
--   'keyword'（legacy）  — 原舊語意，任務都是這個值；2026-04 前的所有資料
--   'semantic'（legacy）  — 從未真正使用過，但 schema 保留
-- 新欄位 `search_domain` 才是我們的主角（'judgment' | 'interpretation'）。
-- 原本想重用 mode 但怕 legacy 資料帶 'keyword' 字串被解讀為新語意，故另起欄位。
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,   -- UUID
    mode          TEXT NOT NULL,      -- 'keyword' | 'semantic'（legacy，全是 keyword）
    search_domain TEXT NOT NULL DEFAULT 'judgment',  -- 'judgment'（法院判決）| 'interpretation'（憲法解釋）
    keyword       TEXT NOT NULL,      -- 搜尋關鍵字（多個空格分隔）
    filter_fields TEXT NOT NULL,      -- 過濾欄位（逗號分隔）
    status        TEXT NOT NULL,      -- pending | running | done | failed
    search_params TEXT,               -- JSON：court / case_type / year_from_to / max_results / exhaustive（供 server 重啟 recovery）
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

-- Stage 1 廣搜結果（list only，無全文）
-- 律師按搜尋後 MCP search_judgments 的命中清單存於此。
-- 後續 stage 2 互動篩選（court / year / cause）純前端 client filter；
-- stage 3 律師下 NL 指令時才會對 narrow 後子集做 get_judgment + 寫 task_judgments。
CREATE TABLE IF NOT EXISTS task_search_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    case_id     TEXT NOT NULL,           -- = MCP 回傳的 jid（結構化）
    court       TEXT NOT NULL,           -- 含分庭，e.g. '臺中高等行政法院 地方庭'
    date        TEXT NOT NULL,           -- 民國日期字串，e.g. '115-04-10'
    source_url  TEXT NOT NULL,
    cause       TEXT,                    -- 案由（MCP 回傳，e.g. '就業服務法' / '交通裁決'）
    summary     TEXT,                    -- MCP 搜尋結果摘要，給 stage 2 略讀預覽用
    fetched_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tsh_task ON task_search_hits(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tsh_case ON task_search_hits(task_id, case_id);

-- 基礎資料集：通過字串過濾的判決，欄位全部存下來
CREATE TABLE IF NOT EXISTS task_judgments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    case_id     TEXT NOT NULL,
    court       TEXT NOT NULL,
    date        TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    reasoning   TEXT,                 -- 永遠存，供日後追問
    main_text   TEXT,                 -- 永遠存，供日後追問
    facts       TEXT,                 -- 永遠存，供日後追問
    cited_statutes TEXT,              -- JSON array 字串
    full_text   TEXT,                 -- 完整判決原文（MCP parser 分段不準時的穩健備援）
    extracted_citations TEXT,         -- JSON array of [law, article, sub, paragraph, item, subitem]；供條號 keyword 做 tuple match（不靠字串）
    judges      TEXT,                 -- JSON array of 法官姓名
    parties     TEXT,                 -- JSON object {原告: [...], 被告: [...], 訴訟代理人: [...], 法定代理人: [...]}
    cause       TEXT,                 -- 案由（如「土壤及地下水污染整治法」）
    parser_version TEXT,              -- cache invalidation key（見 mcp_client.PARSER_VERSION）；NULL = 舊 row，不列入跨 task cache 複用
    fetched_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tj_task ON task_judgments(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tj_case ON task_judgments(task_id, case_id);
-- idx_tj_case_lookup 在 init_db 遷移 7 後建立（依賴 parser_version 欄位），
-- 升級 pre-v7 DB 時該欄位尚未 ALTER，不能放這裡。

-- 每次追問（分析層）
CREATE TABLE IF NOT EXISTS analyses (
    id            TEXT PRIMARY KEY,   -- UUID
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    question      TEXT NOT NULL,      -- 自然語言問題
    ai_read_field TEXT NOT NULL,      -- 送給 Claude 的欄位（逗號分隔）
    filter_fields TEXT,               -- Stage 3 v1 用，v2 redesign 後不再使用（保留欄位給 legacy 任務）
    narrow_state  TEXT,               -- Stage 3 narrow 條件（JSON：court_tiers / year_from / year_to / case_types）
    synthesis     TEXT,               -- Stage 3 v2：全部精讀完的總結（JSON: {total_relevant, consensus, summary}）
    synthesis_is_preliminary INTEGER DEFAULT 0,  -- 1 = 初步 synthesis（還在跑 missing judgments 的 retry），0 = 最終版
    status        TEXT NOT NULL,      -- pending | running | done | failed
    total         INTEGER,
    completed     INTEGER DEFAULT 0,
    match_count   INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_analyses_task ON analyses(task_id);

-- 每個分析層 × 每筆判決的結果
CREATE TABLE IF NOT EXISTS analysis_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id TEXT NOT NULL REFERENCES analyses(id),
    case_id     TEXT NOT NULL,
    match       TEXT NOT NULL,        -- yes | no | partial | error
    score       INTEGER,              -- null 表示 error
    excerpt     TEXT,
    reason      TEXT,
    analyzed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ar_analysis ON analysis_results(analysis_id);
CREATE INDEX IF NOT EXISTS idx_ar_score    ON analysis_results(analysis_id, score DESC);
-- UNIQUE(analysis_id, case_id) 在 init_db 遷移 14 後建立、原因：
-- pre-v1.0.5 DB 可能含雙胞胎 row、要先清 dupe 才能加 UNIQUE、所以不能放這裡（fresh install 也不會不一致）

-- 事務所同義詞字典（L1 資產）：每次使用者 keyword 展開的結果都累積進來，
-- 經 corpus verification + 律師 accept/reject 後變成高信度條目。
-- Schema 為未來商業化（L3 dataset license / L4 SaaS）預留。
--
-- tier 定義：
--   'confirmed'    — 高頻真同義詞（corpus_hits>=50），搜尋 pipeline 自動展開
--   'candidate'    — 邊界案例（6-49 hits），預覽顯示供律師 review
--   'likely_typo'  — 低頻可能錯字（1-5 hits），不進搜尋，僅存案供統計
--   'rejected'     — 幻覺或律師多次拒絕，完全忽略
CREATE TABLE IF NOT EXISTS synonym_dictionary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical       TEXT NOT NULL,     -- 使用者輸入的原始 keyword
    variant         TEXT NOT NULL,     -- 展開出的同義變體
    source          TEXT NOT NULL,     -- 'claude' | 'user_added' | 'discovered' (精讀發現) | 'corpus_verified'
    tier            TEXT DEFAULT 'candidate',  -- 'confirmed' | 'candidate' | 'likely_typo' | 'rejected'
    corpus_hits     INTEGER,           -- MCP search 該 variant 得到的判決數（NULL=尚未驗證，0=幻覺）
    usage_count     INTEGER DEFAULT 0, -- 該 canonical 被律師查詢的次數
    accept_count    INTEGER DEFAULT 0, -- 律師按「✓」的次數（累計到 threshold 會自動升級 tier）
    reject_count    INTEGER DEFAULT 0, -- 律師按「×」的次數（累計到 threshold 會自動降級 tier）
    discovery_count INTEGER DEFAULT 0, -- 被精讀 discover 的累計次數（多次被多篇判決提到 = 強信號）
    first_seen_at   TEXT NOT NULL,
    last_used_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_syn_pair ON synonym_dictionary(canonical, variant);
CREATE INDEX IF NOT EXISTS idx_syn_canonical ON synonym_dictionary(canonical);

-- 理由預篩結果持久化（單一 task 最多一個；新結果覆蓋舊結果）。
-- 生命週期：
--   律師按 toggle → INSERT OR REPLACE 整筆，status='running'、recovery_attempts=0
--   跑中進度推進 → UPDATE matched_case_ids / matched（不動 attempts / narrow / total）
--   完成 → UPDATE status='done', finished_at
--   律師改 filter（narrow 變更）→ 新 work 覆蓋整筆；舊 work 偵測 narrow 不同自己 abort
--   server 重啟 recovery → attempts++，達 MAX 就 mark 'cancelled' 讓律師介入
CREATE TABLE IF NOT EXISTS task_prefilter_results (
    task_id           TEXT PRIMARY KEY REFERENCES tasks(id),
    narrow            TEXT NOT NULL,    -- JSON snapshot（court_tiers / year_from / year_to）
    matched_case_ids  TEXT NOT NULL,    -- JSON array
    total             INTEGER NOT NULL, -- narrow 後應篩的總筆數
    matched           INTEGER NOT NULL,
    status            TEXT NOT NULL,    -- 'running' | 'done' | 'cancelled'
    recovery_attempts INTEGER DEFAULT 0,
    started_at        TEXT NOT NULL,
    finished_at       TEXT              -- null 表仍在跑（包含 recovery pending）
);

-- 使用者星標的判決（跨 task 共用）。
-- 單機單一律師場景：case_id 當 PK，不加 user_id。
-- 刪除 task 不連動刪 star — 星標是律師的持久資產，跟 task 生命週期解耦。
CREATE TABLE IF NOT EXISTS case_stars (
    case_id    TEXT PRIMARY KEY,
    starred_at TEXT NOT NULL
);

-- 律師在 reader 中對判決文字做的黃底劃記（cross-device 同步用、脫離 localStorage）。
-- before_ctx / after_ctx = 選取範圍前後 20 字、reopen reader 時用上下文比對重新定位
--   （比單靠 text 精確：處理重複文字命中）
-- 刪除 task 不連動刪 highlight — 跟星標同設計，跨 task 永久資產。
CREATE TABLE IF NOT EXISTS case_highlights (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    before_ctx TEXT,
    after_ctx  TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ch_case ON case_highlights(case_id);

-- Stage 2.5 in-flight tracker：給 server 重啟 recovery 用。
-- POST /tasks/{id}/fetch-judgments 時 INSERT、_run_stage25_fetch 結束時 DELETE。
-- Server 重啟時 main.py lifespan 掃這張表 → 重新 dispatch 每筆。
-- 列在此的 task_id 可能已抓一半（task_judgments 部分填滿）；
-- _run_stage25_fetch 內部的 INSERT OR IGNORE 讓重抓變 idempotent（只是多跑 MCP）。
CREATE TABLE IF NOT EXISTS stage25_inflight (
    task_id     TEXT PRIMARY KEY,
    case_ids    TEXT NOT NULL,  -- JSON array
    started_at  TEXT NOT NULL
);
