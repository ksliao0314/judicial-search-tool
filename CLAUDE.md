# 判決檢索工具 — Claude Code 專案說明

## 專案概述

給律師用的司法院判決 AI 篩選工具。整合 `mcp-taiwan-legal-db` 與 Claude API，
讓律師用關鍵字找到符合特定法律論點的判決。

核心設計原則：
- 任務是持續存活的研究工作空間，不是一次性請求
- 同一批判決可以反覆用不同問題分析，結果並列對比
- 分析在背景執行，律師不需要等待

> **權威使用者文件**：[`judgment-search/README.md`](judgment-search/README.md) — 本檔是原始 spec、README 是當下實作的真相。Spec 與 README 衝突時以 README 為準。

---

## 近期實作補充（2026-04-18 為止，spec 之外的功能）

以下是 spec 沒寫但已實作的功能，按議題分類：

### 搜尋
- **OR 語法**：keyword 欄支援 `A | B`、`A｜B`（全形）、`A OR B`（不分大小寫）三種寫法。前端 / runner 在送 MCP 前 split 成獨立查詢、結果 union；OR/`|` 絕不會送進 MCP request。實作 `runner.py` 的 `_parse_or_groups` / `_flatten_keywords`
- **0-hit task auto-delete**：搜尋 0 筆的 task 在 user 關閉 card 時自動 DELETE，避免任務清單殘留空條目
- **expanded_variants 持久化**：stage 1 展開後的同義 / 法條變體扁平存進 `task.search_params.expanded_variants`，供 reader 高亮用
- **窮盡搜尋 month/day pagination**：超過 500 筆硬上限時切窗口

### 結構解析
- **Pass 2.5 — 法條款號引文撤銷**：偵測「下列情形之一：三、... 六、... 分別定有明文」的 pattern，把被誤切成 L1 的款號 merge 回前段。Refined heuristics：跳號 OR（從非 1 起 + 有 closure 關鍵字）才 revert；連續從 1 起、≥5 連續項、含巢狀 L2/L3 都保留為真 outline。實作 `app.js` 的 `_unsplitCitedArticleClauses`
- **Citation prefix 加「按」「認定」**：`按「...」` 是法律文書引法條起手式（pattern：按 + 必須緊接「）；「認定：」是仲裁判斷 / 本院認定的 closure。兩者都納入 `_CITATION_PREFIX_PATTERNS`
- **MCP `_insert_outline_breaks`**：最高行政法院 `.htmlcontent` Word-paste 格式段內無換行，在「句末標點 + outline marker」之間強制插 `\n` 復原段落結構（fork 修補）
- **Anomaly log（parser 持續優化的核心反饋）**：`data/parser_anomalies.jsonl` 自動記錄 parse 異常 — `all_fields_empty` / `empty_reasoning` / `long_line_no_marker` / `quote_unbalanced` / `cited_statutes_extractor_miss` / `outline_number_gap`（L1/L2/L3 某層沒 `1` 表示 parser 漏起點）等；每次 fetch 完一筆判決非同步 append。**改 parser heuristic 前先 `jq` 查類型分布 + 法院分布，改完用 5 件 fixture regression 守門**。實作 `src/utils/anomaly_log.py`，查詢配方見 README「開發者注意事項」段

### LLM
- **AI 精讀 V2 prompt**：判決以【理由】【主文】【事實】【引用法條】section headers 餵給 model；輸出加 `direction`（支持/反對/中性）+ `position`（60 字法院立場）+ `found_in`（excerpt 來自哪段）
- **兩階段評分**：第一輪 3K char screening、score>0 才跑第二輪 12K full-pass
- **Smart truncate**：reasoning 超 budget 時用 keyword + question 拆詞當定位，每 hit 前後 2K 窗口、頭尾保留 300 字 context
- **探索型 vs 立場型 prompt 分流**：問題如「法院如何判斷 X」→ 全中性 direction 是正常，prompt 教 model 聚焦「要件 / 標準」；不寫「對立見解」
- **Synthesis 加「彙整」consensus**：探索型問題 + 全中性 + 筆數 ≥ 5 → 推導為「彙整」（label：「法院認定彙整」），原本被誤判為「不足」
- **Followup question-relevance 重排**：`run_quick_followup` 用追問問題的 keyword 命中數當主排序、score 為 tiebreaker，prompt 中前 80 筆優先選相關度高
- **Token live ticker**：`batch_done` SSE 帶累積 `usage`，UI 即時顯示「已用 X.XK tokens · 約 US$X.XX」

### Reader UI
- **AI 評價區塊**：reader 頂部固定區塊，顯示 score 大字（≥7 紅 / 4-6 黑 / <4 灰）+ direction badge + 法院立場摘要 + 可點擊原文摘錄框（seal 暖銅 `#6D5A41` 配色，跟列表 `.hit-mark` 一致；2026-04-19 前為 cyan `#0891B2`）。點摘錄框 smooth scroll 到判決中對應段 + 暖銅 flash 1.5 秒。實作 `_buildAiEvalBlock`
- **案由 pill**：header 中欄垂直置中 chip（`bg-seal/10` 淡紅膠囊），用 CSS grid `1fr auto 1fr` 固定位置；narrow (<1024px) 隱藏、案由回 meta row
- **鍵盤導覽**：`A`/`K` 上一則、`D`/`J` 下一則（A/D 是 WASD 直覺、J/K 是 vim）、`ESC` 關閉。到頭 / 尾自動 toast「已是第一/最後一則」1.5 秒淡出
- **Reader nav 用完整池**：A/D/J/K 走 `allResults + irrelevantResults + dataErrorResults` 全部判決，不受 cluster tab 過濾。Case ID 比對先 normalize 掉所有空白（半 / 全形 / NBSP），避開 FastAPI path decoding 帶來的隱形差異
- **OR/| token 不入 highlight**：reader 高亮 regex 跳過 separator
- **`overflow-anchor: none`**：rc-text/rc-scroll 關掉 Chrome scroll anchoring，避免 AI 評價區塊高度變動觸發 auto scroll-correction 造成跳動感
- **Outline sidebar 用 `scrollTop` 不用 `scrollIntoView`**：避免 scrollIntoView 連動 ancestor scroll 造成主區跳動
- **Cluster 「使用者標記」tab**：tab active 時內嵌下載 PDF icon（不再彈出獨立 tab 影響 cluster 列穩定）

### Stage 1 流程細節
- Stage 1 v2 用 cartesian product 構造 AND-of-OR 查詢清單。Variants 數動態：1 keyword → 8 / 2 → 4 / 3 → 3 / 4+ → 2 each。MAX_AND_QUERIES=20 做截斷防爆炸
- 每 OR group 獨立做 cartesian product，combos 全合併執行
- `task_search_hits` 即時寫入 + SSE 推送，律師看 stage 2 列表筆數秒秒往上跳

---

## MCP 工具

本專案使用 `mcp-taiwan-legal-db`（`.mcp.json` 已設定，Claude Code 啟動時自動載入）。

### `search_judgments` — 搜尋判決清單

```python
search_judgments(
    keyword="行政罰法第7條",  # 全文關鍵字，非案號
    court="最高行政法院",      # 可選
    case_type="行政",         # 可選：民事 / 刑事 / 行政 / 懲戒
    year_from=110,            # 可選：民國年
    year_to=114,              # 可選：民國年
)
# 注意：全文比對，理由 / 主文 / 事實都算命中
# 回傳：字號、法院、日期的清單
```

### `get_judgment` — 取得單筆判決（已結構化）

```python
get_judgment(jid="TPAA,110,訴,123,20211015,1")
# 回傳：
# {
#   case_id, court, date,
#   main_text,      # 主文
#   facts,          # 事實
#   reasoning,      # 理由
#   cited_statutes, # 引用法條列表 ← 比 reasoning 全文搜尋更精確
#   source_url
# }
# MCP 內建快取：全文 30 天 / 搜尋結果 24 小時
```

**`reasoning` 欄位由 MCP 切好，不需要自己 parse HTML。**
**不需要使用 content.js 的邏輯。**

### 五個欄位的用途

| 欄位 | 適合的查詢類型 |
|------|--------------|
| `reasoning` | 法律論點、法條引用、裁量判斷、法院見解 |
| `main_text` | 判決結果：刑期長短、撤銷、免罰、駁回 |
| `facts` | 案件情境：罪名、被告行為、案發背景 |
| `cited_statutes` | 特定法條是否被引用（比 reasoning 字串比對更精確，不會被否定句誤判）|
| `full_text` | 完整判決原文備援：MCP parser 對簡易判決（交通裁決等）常切不出 `reasoning`，整段文字跑進 `facts`。勾此欄位可避免假陰性；精讀成本較高，只在需要跨段對照時使用 |

`filter_field`（字串過濾用哪個欄位）與 `ai_read_field`（送給 Claude 精讀的欄位）
可以不同。例如：在 `main_text` 過濾出「判 2 年以上」的竊盜案，
但送給 Claude 精讀的是 `reasoning`，讓 AI 分析量刑理由。

---

## 輸入流程

律師只輸入關鍵字（含 OR `A | B` 語法），搜尋域用頂部切換器選 **法院判決** 或 **憲法解釋**。

```
律師輸入關鍵字（可多個；支援 OR: A | B）
＋ 選擇過濾欄位：[理由] [主文] [事實] [引用法條] [全文]（可多選）
＋ 選擇 AI 精讀欄位（預設同過濾欄位，可個別指定）
  ↓
Stage 1  search_judgments 廣搜 → 寫 task_search_hits（字號清單）
  ↓
Stage 2  律師前端篩選（法院層級 / 年度 / 案由）— 純 client、零 API
  ↓
Stage 2.5  選定子集 → 背景逐筆 get_judgment（全域 token bucket 60 req/min / burst 30）
             全欄位存 task_judgments（reasoning / main_text / facts / cited_statutes / full_text）
  ↓
Stage 3  律師輸入精讀問題 → Claude 批次精讀（per-model token bucket）
  ↓
結果存 analysis_results；追問可新增 analysis 層、不重抓 MCP
```

> **歷史註記**：原 spec 設計過「自然語言模式 + LLM 策略拆解（`strategy.py` + 策略選擇器 UI）」，2026-04 決定不保留作為主流程，入口從 UI 移除、code 暫留成 dormant module（見 [`SEARCH_REDESIGN.md`](judgment-search/SEARCH_REDESIGN.md)）。

---

## 任務架構：研究工作空間

任務不是一次性請求，是持續存活的研究工作空間。

### 三層結構

```
任務（Task）
├── 基礎資料集：task_judgments
│   這批判決的 reasoning / main_text / facts 全部快取在這裡
│   一旦建立就固定，追問不會重跑這一步
│
├── 分析層（Analysis）可以有多個，每次追問新增一層
│   ├── 分析 #1「免罰撤銷原處分」→ 312 筆，跑完
│   ├── 分析 #2「過失比例認定」  →  87 筆，跑完
│   └── 分析 #3「裁量萎縮至零」  →  進行中...
│
└── 篩選層（Filter）純前端狀態，不存資料庫
    疊加在任何分析層上，即時過濾
    條件：法院層級 / 年度 / score 門檻 / match 類型
```

### 清單顯示：主分析層 + 副標記

律師選擇一個分析層作為主排序依據，其他分析層的結果以小標籤附在旁邊：

```
                      主：分析#1免罰    分析#2過失   分析#3裁量
臺北高行 112訴1234      9.2 ████        6.1 ██        —
最高行政 111上1567      7.8 ███         8.4 ████      9.0 █████
臺中高行 113訴890       3.1 █           —             7.2 ███
```

主分析層決定清單排序。副標籤讓律師一眼看出同一筆判決在不同問題下的表現。
`—` 表示該筆判決在那個分析層中未命中（字串過濾排除或 AI 評為 no）。

### 追問的兩個層次

**批次追問**（對整個任務的基礎資料集）：
新輸入問題 → 新增分析層 → 背景跑 AI → 結果加到清單副標籤

**單筆追問**（對單一判決）：
在閱讀器中針對這份判決即時問答 → Claude 只讀這份的指定欄位 →
即時回答，不建立新分析層，不影響清單排序

---

## 背景任務佇列

### 任務生命週期

```
律師送搜尋（同步）
  → 建立 task 記錄（status: pending）
  → 建立第一個 analysis 記錄（status: pending）
  → 立刻回應 task_id，前端訂閱 SSE

背景 worker
  → 更新 task status: running
  → 第一步：search_judgments，建立 task_judgments 骨架
  → 第二步：get_judgment 逐筆，字串過濾，填滿 task_judgments
             ※ 把 reasoning / main_text / facts 全部存下來
               不管律師這次選哪個欄位，都存，供日後追問用
  → 第三步：Claude 批次精讀，寫入 analysis_results
  → 每批完成推 SSE 事件
  → 全部完成更新 status: done
```

### Worker 設計

- 單機：`asyncio.Queue`，FastAPI lifespan 啟動背景 worker
- 一次跑一個任務（避免對司法院和 Claude API 造成壓力）
- 同一任務內的新分析層（追問）插隊到當前任務的尾端，不等其他任務
- 任務狀態持久化，server 重啟後 `pending` / `running` 任務自動恢復
- get_judgment 失敗：指數退避（5s → 15s → 45s），3 次後記錄跳過
- Claude API 失敗：同一筆最多重試 2 次，仍失敗記 `error`，不阻塞任務

### SSE 事件格式

```
event: judgments_ready
data: {"task_id": "...", "total_search": 1204, "after_filter": 247}

event: batch_done
data: {"task_id": "...", "analysis_id": "...",
       "completed": 30, "total": 247, "results": [...]}

event: analysis_done
data: {"task_id": "...", "analysis_id": "...", "match_count": 89}

event: task_done
data: {"task_id": "...", "elapsed_sec": 1240}
```

---

## UI 設計規格

### 設計方向：司法文件室

使用情境是律師長時間、高專注度的研究工作，不是快速查詢。
設計語言參考高端律師事務所研究室——沉穩、高資訊密度、有空間感。

- 主題：淺色（米紙底 `parchment #F7F7F5`，墨黑文字 `ink #111110`）
  長時間閱讀大量中文，淺色比深色主題眼睛負擔小
- 互動靜止色（tab active / form focus / 導覽 hover / link）：墨黑 `ink #111110`
  — 黑字壓米紙的「排印感」最符合法律文件氣質
- 強調色（唯一 accent · 表 live / selected / clickable）：暖銅 `seal #6D5A41`（`dim #4E3F2B` / `ghost rgba(109,90,65,0.08)`）
  — 進度條 / 執行中 pill / 關鍵字 chip / hit mark 點跳轉框 / 勾選 checkbox 都走這色
  — Tailwind token 名稱保留 `seal`（法律印章隱喻），但實體 hex 已從 2026-04 前的青 `#0891B2` 改為暖銅，與米紙同家族
- 字型：`Noto Serif TC`（內文）＋ `JetBrains Mono`（字號 / 數字 / 代號）
- 命中高亮：暖銅底色 `rgba(109,90,65,.08)` ＋ 細邊框，不用螢光黃 / 不用青
- 狀態色（非主色、語意分流）：pending = `amber`、success = `emerald`、error = `red`、警告 = `amber` dim
  — 這些用 Tailwind 原生 palette，只做語意區隔，不用來當品牌色

### 版面：兩段式

```
┌──────────────────────────────────────────────────┐
│  頂部：搜尋指揮台（固定高度，永遠可見）               │
├──────────────────────────────────────────────────┤
│  工作區（全寬，依狀態切換）                          │
│                                                   │
│  狀態 A：任務面板 ＋ 判決清單                        │
│  狀態 B：判決閱讀器（清單縮為左側薄欄 260px）         │
└──────────────────────────────────────────────────┘
```

### 頂部：搜尋指揮台

```
[關鍵字 ▾]  ___________________________________________  [搜尋]

過濾欄位：[✓ 理由]  [ 主文]  [ 事實]  [ 引用法條]  [ 全文]
AI精讀：  [✓ 同過濾欄位]  或個別指定（展開後可各自設定）
法院：[全部 ▾]   年度：[110]──[114]   類型：[✓ 判決][ 裁定]
```

### 狀態 A：任務面板 ＋ 判決清單

**任務面板**（頂部橫帶，可折疊）：

```
任務 #3  行政罰法第7條  執行中  ████████░░  247/612  剩餘 8 分鐘   [暫停]
任務 #2  信賴保護原則   完成    分析 3 層   最新：裁量萎縮 23 筆   [展開]
任務 #1  比例原則       完成    分析 1 層   免罰撤銷 89 筆         [展開]
```

點擊任務列 → 下方清單切換到該任務的結果。

**分析層切換器**（清單頂部 tab）：

```
[分析#1 免罰撤銷 312筆]  [分析#2 過失認定 87筆]  [分析#3 裁量 23筆 ●進行中]
                    ＋ [新追問]
```

選中的 tab 是主排序依據，其他 tab 的結果顯示為副標籤。

**篩選列**（分析層切換器下方）：

```
法院：[全部 ▾]   年度：[全部 ▾]   Score：[≥ 6 ▾]   命中類型：[全部 ▾]
```

純前端即時過濾，不呼叫 API。

**判決清單**（全寬，每筆 64px）：

```
│▌ 臺北高等行政法院   112訴1234   2023-11-15   [理由]
│  主：9.2  ██████████  │  #2過失 6.1  │  #3裁量 —
```

左側暖銅細線（`▌`）標記 match: yes 的判決。
主 score 用大字（`JetBrains Mono`），副標籤用小字。
`—` 表示未命中。

### 狀態 B：判決閱讀器

```
┌──────────────────────────────────────────────────────────────┐
│ 清單薄欄 260px      │  閱讀器主區                             │
│                     │                                         │
│ ▌ 112訴1234  9.2   │  ┌─ AI 摘要卡（可收起）──────────────┐  │
│   111上1567  7.8   │  │  分析#1「免罰撤銷原處分」           │  │
│   113訴890   3.1   │  │  score 9.2  命中欄位：理由          │  │
│   ...              │  │  「法院明確指出行政罰法第7條...」    │  │
│                    │  │  判斷：完全符合，法院援引該條文      │  │
│                    │  │  認定無故意過失，撤銷原處分          │  │
│                    │  └────────────────────────────────────┘  │
│                    │                                         │
│                    │  理由全文                               │
│                    │  （命中段落暖銅底色 + 細框標記）         │
│                    │                                         │
│                    │  ──────────────────────────────────── │
│                    │  [← 上一筆]  [→ 下一筆]                │
│                    │  [複製引用格式]  [開啟司法院原文]        │
│                    │  [追問這份判決]                         │
└────────────────────┴─────────────────────────────────────────┘
```

AI 摘要卡放在全文**上方**，律師先看摘要再決定要不要讀全文。

「複製引用格式」輸出：
`臺北高等行政法院112年度訴字第1234號判決意旨參照`

「追問這份判決」展開即時問答列（不建立新分析層）：
```
> 這份判決中法院對「故意」的認定標準是什麼？  [送出]
```

---

## 資料庫 Schema

```sql
-- 任務本體
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,   -- UUID
    mode          TEXT NOT NULL,      -- 'keyword' | 'semantic'
    keyword       TEXT NOT NULL,      -- 搜尋關鍵字（多個空格分隔）
    filter_fields TEXT NOT NULL,      -- 過濾欄位（逗號分隔）
    status        TEXT NOT NULL,      -- pending | running | done | failed
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

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
    fetched_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tj_task ON task_judgments(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tj_case ON task_judgments(task_id, case_id);

-- 每次追問（分析層）
CREATE TABLE IF NOT EXISTS analyses (
    id            TEXT PRIMARY KEY,   -- UUID
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    question      TEXT NOT NULL,      -- 律師輸入的精讀問題（NL）
    ai_read_field TEXT NOT NULL,      -- 送給 Claude 的欄位（逗號分隔）
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
```

**重要**：`task_judgments` 永遠存 reasoning / main_text / facts 三個欄位，
不管律師第一次選哪個欄位過濾。這樣後續追問可以跨欄位，不需重新呼叫 MCP。

**清單主 / 副分析 JOIN 查詢**：
```sql
SELECT
    tj.case_id, tj.court, tj.date, tj.source_url,
    p.score   AS primary_score,
    p.match   AS primary_match,
    p.excerpt AS primary_excerpt,
    s.score   AS secondary_score,
    s.match   AS secondary_match
FROM task_judgments tj
LEFT JOIN analysis_results p
    ON p.case_id = tj.case_id AND p.analysis_id = :primary_analysis_id
LEFT JOIN analysis_results s
    ON s.case_id = tj.case_id AND s.analysis_id = :secondary_analysis_id
WHERE tj.task_id = :task_id
ORDER BY p.score DESC NULLS LAST;
```

---

## 目錄結構

```
judgment-search/
├── CLAUDE.md
├── README.md
├── .mcp.json                   ← MCP server 設定
├── pyproject.toml
├── schema.sql
├── src/
│   ├── main.py                 ← FastAPI 進入點，啟動 server + worker
│   ├── pipeline/
│   │   ├── search.py           ← search_judgments 封裝
│   │   ├── filter.py           ← get_judgment + 字串過濾
│   │   ├── analyze.py          ← Claude API 批次精讀（被 worker 呼叫）
│   │   └── strategy.py         ← [dormant] NL 查詢策略拆解；UI 入口已移除，code 暫留
│   ├── worker/
│   │   ├── queue.py            ← asyncio.Queue 任務佇列
│   │   └── runner.py           ← 背景 worker：取任務 → 執行 → 推 SSE
│   ├── db/
│   │   └── database.py         ← SQLite 存取層
│   ├── api/
│   │   ├── tasks.py            ← POST /tasks, GET /tasks/{id}
│   │   ├── analyses.py         ← POST /tasks/{id}/analyses（追問）
│   │   ├── stream.py           ← GET /tasks/{id}/stream（SSE）
│   │   └── judgments.py        ← GET /tasks/{id}/judgments（清單查詢）
│   ├── ui/
│   │   └── static/             ← 前端（HTML / CSS / JS）
│   └── utils/
│       └── retry.py            ← 指數退避重試
└── tests/
    ├── test_filter.py
    ├── test_analyze.py
    ├── test_strategy.py
    └── test_worker.py
```

---

## Claude API Prompt

```python
ANALYSIS_PROMPT = """
你是一位台灣法律研究助理。以下是一份法院判決的{field_label}段落。
請判斷這份判決的{field_label}，是否符合以下條件：

{question}

請只回傳 JSON，不要任何其他文字：
{{
  "match": "yes" | "no" | "partial",
  "score": 1到10的整數,
  "excerpt": "命中的關鍵段落，限150字以內，若不符合則留空字串",
  "reason": "你的判斷理由，限80字以內"
}}

{field_label}內容如下：
{field_text}
"""

# field_label 對應表：
# reasoning      → 「理由」
# main_text      → 「主文」
# facts          → 「事實」
# cited_statutes → 「引用法條」
# full_text      → 「全文」
```

---

## 常見問題

**Q：為什麼不在 search_judgments 時就限定欄位搜尋？**
A：`mcp-taiwan-legal-db` 的 `search_judgments` 做全文比對，沒有欄位限定參數。
欄位過濾只能在拿到 `get_judgment` 的結構化回傳後，用字串比對做。

**Q：content.js 在這個專案裡用嗎？**
A：不用。content.js 是瀏覽器 content script，供司法院網頁側欄導覽用。
本專案判決解析由 MCP `get_judgment` 負責，欄位已切好。

**Q：task_judgments 為什麼要存全部欄位，不管律師選哪個？**
A：讓追問可以跨欄位。律師第一次選「理由」，追問時可能想問「主文有沒有撤銷」。
如果只存第一次選的欄位，追問就要重新打 MCP，浪費時間且有限速風險。

**Q：cited_statutes 怎麼用於字串過濾？**
A：`cited_statutes` 是列表，存為 JSON 字串。過濾時：
```python
import json
statutes = json.loads(judgment["cited_statutes"] or "[]")
if keyword in statutes:  # 列表成員比對，不是字串 contains
    ...
# 這樣「本件與行政罰法第7條無關」就不會誤判為命中
```

**Q：律師同時開多個任務，worker 怎麼處理？**
A：任務依序進佇列，worker 一次跑一個任務。
同一任務內的新分析層（追問）插隊到當前任務的尾端，優先於佇列中其他任務。
任務面板上各任務狀態透過 SSE 即時更新。

**Q：追問是否需要重新呼叫 MCP？**
A：不需要。追問對 `task_judgments` 中已快取的欄位文字重新跑 Claude 分析，
完全不打 MCP 或司法院，速度快且無限速問題。
