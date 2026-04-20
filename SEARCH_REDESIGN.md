# 兩階段搜尋設計（Search Redesign）

> Status: **Spec — 待實作**
> Author note: 取代 CLAUDE.md 中的 single-shot pipeline。實作完成後 CLAUDE.md 會 reroute 到本文。

## 動機

現行流程：律師按下搜尋 → 後端一氣呵成做完 search → 抓全文 → 字串過濾 → Claude 精讀。

問題：
- 進階條件（法院層級、年度、案件類型）綁死在搜尋按鈕，律師看不到搜尋總量無法決定要不要收斂
- 精讀成本高：每筆判決一次 `get_judgment` + 一次 Claude call。律師根本不會看的判決也照吃成本
- 判決總量是結果出來才知道，但這個數字應該影響「要不要收斂條件」這個前置決策

新流程依律師實際工作習慣拆三段：**廣搜 → 收斂 → 精讀**。

---

## 三階段流程

### Stage 1: 廣搜（list only）

律師輸入：
- 關鍵字（空格分隔多個）
- 智慧展開 toggle（保留現有 citation/synonym 展開行為）

後端動作：
- MCP `search_judgments` **預設窮盡**（hard cap 5000，超過顯示「5000+ 已截斷，請加條件再搜」）
- **不傳** court / case_type / year_from / year_to 給 MCP（這些篩選改在 stage 2 做）
- 結果寫入新表 `task_search_hits`（只存 metadata，無全文）

UI：
- 完成後顯示「找到 N 筆判決」
- 法院層級 / 年度 / 案件類型分布視覺化（counts）
- 列表（virtual scroll）：court + case_id + date
- 點任一筆 → 直接開閱讀器，reader 用 MCP `get_judgment` 即時抓全文（MCP 自帶 30 天 cache，不寫 `task_judgments`）

### Stage 2: Narrowing（純前端互動）

律師在 stage 1 結果上加篩選：
- 法院層級 multi-select（顯示每層級 count）
- 年度範圍
- 案件類型 multi-select（民事 / 刑事 / 行政）
- 即時更新「目前選取 M 筆」

純 client-side filter，不重打 MCP（資料 stage 1 都拿到了）。可以反覆調整無 cost。

### Stage 3: 精讀

> ⚠ **v1 Superseded** — 本節描述 v1 設計。v2 redesign 見文末「Stage 3 v2 redesign」，
> 取消獨立「深度篩選」步驟、拿掉 filter_fields/ai_read_fields picker、加 synthesis 總結。（NL 指令）

律師按「對選取的 M 筆做 AI 精讀」→ 開 modal：
- NL 指令文字框（例：「找出法院有使用行政罰法第 7 條撤銷原處分的判決」）
- 選 `filter_fields`（字串過濾欄位：理由/主文/事實/全文）
- 選 `ai_read_fields`（Claude 讀哪些欄位，預設同 filter_fields）
- 送出

後端動作（背景 worker）：
1. 對 narrow 後的 M 筆，逐筆 `get_judgment` 抓全文，寫入 `task_judgments`（同一 task 重複 case_id 用 UNIQUE key 去重，已抓的不重抓）
2. **字串過濾**：用原始 keyword + 展開的 variants，對 `filter_fields` 欄位比對。命中的留下 → 給 Claude
3. **Claude 精讀**：以 NL 指令為 question，讀 `ai_read_fields`，逐批寫 `analysis_results`
4. 整路推 SSE 進度

---

## Schema 變更

### 新增表 `task_search_hits`

```sql
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
```

> 實作備註：實際打 MCP 後發現 `case_type` 欄位 MCP 回空字串。改存 `cause`（案由）+ `summary`（搜尋摘要）。
> 民事/刑事/行政三大類由 `court` 名稱推導（行政法院 → 行政；其餘看分庭/案號），不存欄位。

### `tasks` 表調整

- **移除** `filter_fields`（移到 `analyses`）
- **保留** `keyword`、`status`、時間欄位
- `mode` 暫不刪（先 default `'keyword'`），semantic 模式稍後再說
- `search_params` JSON 簡化：只剩 `expand_keywords`、`exhaustive`（court / year / case_type 不再走 MCP）

### `analyses` 表調整

新增兩個欄位：
- `filter_fields TEXT`（CSV，stage 3 字串過濾欄位）
- `narrow_state TEXT`（JSON，紀錄這次精讀的 narrow 條件 — court_tiers / year_from / year_to / case_types）

`ai_read_field` 既有，保留。

### `task_judgments` 表

語意改變：現在代表「為 stage 3 精讀而抓全文的判決」。schema 不動。

填充時機：stage 3 啟動時逐筆抓。同一 task 內：
- 若 case_id 已存在於 `task_judgments`（前次精讀已抓），跳過抓全文，重用既有資料
- 否則 `get_judgment` → INSERT

---

## API 變更

### POST `/api/tasks` — 只觸發 Stage 1

```json
// Request
{
  "keyword": "行政罰法第7條",
  "expand_keywords": true,
  "exhaustive": true
}

// Response (sync — 等 stage 1 結束)
{
  "task_id": "...",
  "hits_total": 247,
  "stage": "stage1_done"
}
```

備案：非同步（立刻回 task_id + SSE）。Stage 1 窮盡可能 30 秒以上，建議走非同步。

### GET `/api/tasks/{id}/hits` — Stage 1 清單

```json
[
  { "case_id": "TPAA,113,訴,1234,...", "court": "臺北高等行政法院", "date": "113-08-15", "source_url": "...", "case_type": "行政" },
  ...
]
```

前端拿到後做 stage 2 互動篩選。

### GET `/api/tasks/{id}/hits/{case_id}` — 閱讀器即時抓單筆

直接代理 MCP `get_judgment`，不寫 `task_judgments`（純讀取，MCP cache 自然吃到）。

### POST `/api/tasks/{id}/analyses` — Stage 3 精讀

```json
// Request
{
  "question": "找出法院有使用行政罰法第7條撤銷原處分的判決",
  "ai_read_fields": ["reasoning"],
  "filter_fields": ["reasoning", "cited_statutes"],
  "narrow": {
    "court_tiers": ["最高行政法院", "高等行政法院"],
    "year_from": 110,
    "year_to": 114,
    "case_types": ["行政"]
  }
}

// Response
{ "analysis_id": "..." }
```

後端從 `task_search_hits` 拿 narrow 後的子集 → 推進 worker queue。

### 不變：

- GET `/api/tasks` — 任務清單
- GET `/api/tasks/{id}` — 單一任務 + analyses
- GET `/api/tasks/{id}/judgments` — analyses 結果 join task_judgments（精讀完成後的 results）
- GET `/api/tasks/{id}/stream` — SSE
- DELETE `/api/tasks/{id}` — 刪除（含 cancel）

---

## SSE 事件

| 事件 | 何時 |
|------|------|
| `stage1_progress` (新) | MCP 窮盡多輪過程，每輪推一次 cumulative count |
| `stage1_done` (新) | Stage 1 完成，hits_total |
| `fetch_progress` (沿用) | Stage 3 抓全文進度 |
| `judgments_ready` (沿用) | Stage 3 字串過濾完成，after_filter |
| `batch_done` (沿用) | Stage 3 Claude 每批完成 |
| `analysis_done` (沿用) | Stage 3 全部完成 |
| `task_done` (沿用) | 整體任務完成 |

---

## Worker 變更

### 新 work item: `Stage1SearchWork`

```python
@dataclass
class Stage1SearchWork:
    type: str = field(default="stage1_search", init=False)
    task_id: str = ""
    keyword: str = ""
    expand_keywords: bool = True
    exhaustive: bool = True
    api_key: str | None = None
```

執行：keyword 展開（同現行邏輯）→ MCP 窮盡 search → 寫 `task_search_hits` → SSE `stage1_done`。**不抓全文，不精讀。**

### 改名 `NewAnalysisWork` → `Stage3AnalyzeWork`

```python
@dataclass
class Stage3AnalyzeWork:
    type: str = field(default="stage3_analyze", init=False)
    task_id: str = ""
    analysis_id: str = ""
    question: str = ""
    ai_read_fields: list[str] = field(default_factory=list)
    filter_fields: list[str] = field(default_factory=list)
    narrow: dict = field(default_factory=dict)
    api_key: str | None = None
```

執行：
1. 從 `task_search_hits` 依 `narrow` 篩出子集
2. 對每筆 case_id，若 `task_judgments` 沒有則 `get_judgment` + INSERT
3. 字串過濾（filter.py 邏輯沿用）
4. Claude 精讀（analyze.py 邏輯沿用）

### 拆掉 `FullTaskWork`

新流程下不存在「一氣呵成」的 work。

### `_check_task_alive` 沿用

兩個 work type 入口都仍呼叫，保留刪除取消功能。

---

## UI 變更

### 首頁（home view）

**移除：**
- 「關鍵字」「語意」mode toggle
- 進階條件 panel（年度、法院層級、主文包含）

**保留：**
- 大標題「判決智能檢索」
- 關鍵字輸入框
- 智慧展開 toggle
- 搜尋按鈕
- 執行中任務卡片 + 歷史搜尋入口

> Semantic 模式 code 暫不刪，只是入口從 UI 拿掉。等 keyword 流程穩了再回來想 semantic 怎麼接。

### Stage 1 完成後的 view（新）

替代現在的 results view 上半段。任務頂 bar 不變（返回 / 任務切換 / 新增搜尋）。

```
┌────────────────────────────────────────────────────────┐
│ 找到 1,247 筆判決                              [AI精讀] │
├────────────────────────────────────────────────────────┤
│ 法院層級       年度        案件類型                      │
│ ☐ 憲法 (0)    ▮▮▮▮▮▮      ☐ 民事 (203)                 │
│ ☐ 最高 (12)   110─114      ☐ 刑事 (89)                  │
│ ☑ 最高行 (45) (slider)     ☑ 行政 (955)                 │
│ ☑ 高行 (211)               目前選取 1,166 筆            │
│ ☐ 高等 (89)                                             │
│ ☐ 地方 (890)                                            │
├────────────────────────────────────────────────────────┤
│ ▌ 最高行政法院  113訴1234  113-08-15      點此閱讀 →    │
│ ▌ 臺北高行     113訴1567  113-07-22                    │
│ ▌ ...                                                  │
└────────────────────────────────────────────────────────┘
```

互動：
- 篩選器即時更新 list + count
- 點任一筆 → 開閱讀器（reader 抓 get_judgment 即時呈現）
- 點「AI 精讀」→ 開 stage 3 modal

### Stage 3 modal

```
┌──── AI 精讀（對選取的 1,166 筆）──────────────────┐
│                                                     │
│  指令                                               │
│  ┌─────────────────────────────────────────────┐   │
│  │ 找出法院有使用行政罰法第7條撤銷原處分的判決    │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
│  字串過濾欄位（先用關鍵字過濾，再交給 AI）          │
│  ☑ 理由  ☑ 引用法條  ☐ 主文  ☐ 事實  ☐ 全文        │
│                                                     │
│  AI 精讀欄位（Claude 實際讀的內容）                 │
│  ☑ 理由  ☐ 主文  ☐ 事實  ☐ 全文                    │
│                                                     │
│                          [取消]  [開始精讀]         │
└─────────────────────────────────────────────────────┘
```

送出後：
- 跳回 stage 2 view（保留 narrow 狀態）
- 加一個 progress strip：「精讀中 47/1166」
- 列表加 score 欄（精讀完成的判決即時填入）

### Stage 3 完成後

跟現在 results view 類似：分析 tab、篩選 by score / match type、reader 含 AI 摘要卡。

可以對同一個 narrowed 子集做多次「新追問」（reuse `task_judgments` 已抓的全文，只跑 Claude 精讀）。

---

## 既有資料相容

- 既有 `tasks` + `task_judgments` 不動
- 既有任務 `task_search_hits` 為空 → UI 偵測「legacy 任務」直接走舊路徑（顯示 task_judgments + analyses 結果，不顯示 stage 1/2 介面）
- 不做資料遷移（不回填 task_search_hits）

---

## 暫不做（後續）

1. **Semantic 模式重構**：等 keyword 流程穩定後，semantic 模式的角色重新定位為「keyword 提案器」。Code 暫留，UI 隱藏入口。
2. **Stage 2 篩選狀態持久化**：目前純前端 state，重整頁面消失。律師很想要再加。
3. **Stage 1 cache**：同 keyword 短時間內重搜要不要 cache hits？暫無，依 MCP 自身 cache（24 小時）。
4. **Stage 3 narrow 比對**：先做完整重跑，不檢查「上次精讀的 narrow + 這次的差集」優化。
5. **MCP cap 超過時的 UX**：5000+ 截斷的明確訊息與「我接受截斷」按鈕，第一版用 alert 即可。

---

## 實作順序建議

1. **DB layer**：新增 `task_search_hits` 表 + db helpers
2. **Worker**：新增 `Stage1SearchWork`，拆 `FullTaskWork` → `Stage3AnalyzeWork`
3. **API**：`POST /tasks` 改只觸發 stage 1；新增 `GET /tasks/{id}/hits`；改 `POST /tasks/{id}/analyses` 收 narrow + filter_fields
4. **前端首頁**：移除 mode toggle / 進階條件 / filter_fields，留純 keyword 輸入
5. **前端 stage 2 view**：分布視覺化 + 互動篩選器 + list + 「AI 精讀」按鈕
6. **前端 stage 3 modal**：NL 指令 + filter_fields + ai_read_fields
7. **Reader**：支援 `GET /tasks/{id}/hits/{case_id}`（stage 1 list 點開即讀的路徑）
8. **Legacy 相容**：openTask 偵測 `task_search_hits` 為空 → 走舊 UI
