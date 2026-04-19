# 判決檢索工具

> 給律師用的台灣司法院判決 / 憲法解釋 AI 篩選工具。  
> 律師輸入關鍵字，系統去司法院抓判決或大法官解釋、AI 逐筆讀完、給每筆 0-10 分與立場標註，律師專注在「法院判斷」上。

兩個搜尋域切換：
- **法院判決（FJUD）**— 對 `data.aspx` 全文搜尋，涵蓋民事 / 刑事 / 行政 / 懲戒、全法院層級
- **憲法解釋**— 對 `cons.judicial.gov.tw` 搜尋憲法法庭判決 + 司法院大法官解釋，由 [`cons_normalizer.py`](src/pipeline/cons_normalizer.py) normalize 成 FJUD-shape，reader / analyze pipeline 共用同一套資料結構

本文件給接手 / 貢獻此專案的工程師。產品域知識在 [CLAUDE.md](CLAUDE.md)、搜尋 pipeline 完整規格在 [SEARCH_REDESIGN.md](SEARCH_REDESIGN.md)。

---

## 它在解決什麼問題

司法院判決系統有 1000 萬+ 筆裁判書，搜尋介面只支援關鍵字＋全文比對。律師找特定法律論點時，**關鍵字搜出 500 筆 → 律師逐一點開判決原文 → 90% 不相關**，每天耗在篩選上的時間遠多於閱讀。

本工具：
- 把「搜出多少筆」與「實際相關」分開：先用司法院關鍵字搜出候選池（最多 500 筆），再用 Claude 對每筆判決精讀打分。
- 律師看清單時看到的是 AI 已標好「9 分 [支持] 法院認定 X 成立、附 150 字原文摘錄」，省掉「點開→看一眼→關掉」的循環。
- 同一批判決可以反覆追問（不再花 token 重抓 MCP）。

---

## 三大核心議題

整個專案圍繞三組技術議題展開。下面每一項都點出要解決的痛點、對策、與對應原始碼位置；實作細節與「為什麼這樣設計」另見 [重要技術決策](#重要技術決策)。

### 議題一：搜索架構

**痛點**：司法院搜尋只支援「全文比對」、單次顯示上限 500 筆。律師從廣泛關鍵字出發要收斂到相關判決，單靠司法院介面完全不夠。

| 面向 | 做法 | 位置 |
|---|---|---|
| **召回率**：避免漏案 | OR 語法（`A \| B` / 全形 `｜` / `A OR B`）：同 group 各 variant 平行打 MCP → 結果 union | [src/worker/runner.py](src/worker/runner.py) `_parse_or_groups` |
| | 超過 500 筆硬上限 → month/day 範圍 keyset pagination 切窗口、合併去重 | [src/pipeline/search.py](src/pipeline/search.py) |
| | 同義詞展開（confirmed tier）：「僱傭 / 雇用 / 雇傭」自動互相轉換 | [src/pipeline/synonym_expander.py](src/pipeline/synonym_expander.py) |
| **精準度**：避免誤判 | 法條比對走 `cited_statutes` 列表 **tuple match**（非字串 contains），避免「本件與第 7 條無關」這種否定句被字串比對誤判 | [src/pipeline/citation_normalizer.py](src/pipeline/citation_normalizer.py) |
| | 法律簡稱 72 組（「勞基法 ↔ 勞動基準法」）+ 事務所確認同義組 3 組（「假處分 ↔ 暫時處分」等）內建同義字典、跨 task 共用 | [law_abbreviations.json](src/data/law_abbreviations.json) + [synonym_seed.json](src/data/synonym_seed.json) |
| **成本控制** | Stage 2 client-side 篩選：法院層級 / 年度 / 案由關鍵字，純前端、零 API call | [src/ui/static/js/app.js](src/ui/static/js/app.js) |
| | Stage 2.5 深度抓取：全域 token bucket 60 req/min / burst 30（`filter.py _mcp_fetch_bucket`），跨 task 共用、保護司法院 | [src/worker/runner.py](src/worker/runner.py) `_run_stage25_fetch` |

#### 法條 tuple 比對（Citation normalization）

律師在查詢「民法第 184 條」時，**不該被判決內文的「本件與民法第 184 條無關」這種否定句誤判為命中**。字串比對做不到，tuple match 才能。

每個引用法條正規化為 6 欄結構：

```python
Citation(law, article, sub, paragraph, item, subitem)
# 例：「民法第 184 條第 1 項第 2 款」→ Citation("民法", 184, None, 1, 2, None)
#     「民法第 179 條之 1」 → Citation("民法", 179, 1, None, None, None)
```

`covers()` 邏輯（律師問題 vs 判決實際引用）：

| 律師查詢 | 判決書寫 | 命中？ | 理由 |
|---|---|---|---|
| 民法第 184 條 | 民法第 184 條第 1 項第 2 款 | ✓ | 律師查得粗、判決寫得細 → 命中 |
| 民法第 184 條第 1 項 | 民法第 184 條第 2 項 | ✗ | 同條但不同項 → 不命中 |
| 第 7 條（無法名）| 行政罰法第 7 條 | ✓ | 律師未寫法名 → law=None 放寬 |
| 民法第 184 條 | 刑法第 184 條 | ✗ | 法名不同 → 不命中 |

**複合引用**也正確處理：
- `民法第 184 條、第 188 條` → 兩個 Citation tuples
- `民法第 184 條第 1 項第 1、3、5 款` → 三個 tuples 共用 (184, 1)
- `同法第 179 條` → back-reference 前文的 law name

實作（兩階段接力，各負一層）：
- **Stage 1（廣搜）**：MCP `search_judgments` 是司法院原生全文字串比對，故 [`citation_normalizer.top_search_variants`](src/pipeline/citation_normalizer.py) 對每個法條 keyword 挑 top 5 格式變體（Arabic/Chinese 數字、`之`/`-` suffix、全名/簡稱）分別發查詢 → union 去重，召回覆蓋各種判決書寫法
- **Stage 2.5（fetch）**：[`citation_extractor.py`](src/pipeline/citation_extractor.py) 掃判決文字產出結構化 `list[Citation]`，存進 `task_judgments.extracted_citations`
- **Stage 3（精讀前 filter）**：[`analyze._citation_precision_passes`](src/pipeline/analyze.py) 把律師的 citation keyword 用 `parse_keyword` 轉成 `Citation(law, art, sub, p, i, si)`、跟 extracted tuples 走 `Citation.covers()` 比對；**不命中的判決不跑 Claude、直接寫 `match='no'` 並標註 reason「未引用 X 條（Stage 1 字串比對誤抓）」**，省 token 又讓律師清楚看到哪些是假陽性
- 非法條 keyword（如「比例原則」）不走此 filter，完全由 Claude 語意判斷

#### 同義詞 tier 展開邏輯

法律用語多種寫法（「僱傭 / 雇用 / 雇傭」/「古蹟 / 古績」/ 簡稱 / 異體字），搜尋時要能自動展開、但不能把「不等價」的詞誤當同義（會把搜尋結果打爛）。用 4 tier 分離、**discovery 來的一律 candidate、必須律師手動確認才能升 confirmed 生效於搜尋**：

| Tier | 搜尋時行為 | 來源 |
|---|---|---|
| **`confirmed`** | 自動展開、進搜尋變體池 | 律師 ✓ 累積 ≥3 次的 candidate、或啟動時 seed 檔進來（`law_abbreviations.json` 72 組法律簡稱、`synonym_seed.json` 事務所 seed） |
| **`candidate`** | **不自動展開**、在設定面板「待確認」section 顯示給律師審核 | **Stage 3 精讀時 Claude 順便 discover 的變體**（主要來源） |
| **`likely_typo`** | 不展開 | 初次 upsert 時 corpus_hits 1–5 分進這檔（corpus 幾乎查不到、高機率是 LLM 幻覺） |
| **`rejected`** | 永不展開、不再推薦 | 律師 ✕ 累積 ≥2 次、或 corpus_hits=0 |

**主流程（discovery-driven、律師手動確認）**：

```
Stage 3 精讀每筆判決時、Claude prompt 帶 do_discovery=True
  ↓ 順便列出判決裡看到的 variant candidates（如「僱傭」→「雇傭」「僱庸」）
  ↓
_persist_discovered_variants 寫 synonym_dictionary
  ↓ 強制 candidate tier（即使 corpus_hits ≥ 50 也不自動升 confirmed）
  ↓ 為什麼：Claude 會把「雇主」「勞僱契約」這種**不同法律概念**誤判為同義
  ↓        corpus 命中再高也不能 auto 生效、必須律師把關
  ↓
律師打開「設定 → 同義詞庫 → 待確認」section
  ↓ ✓ accept：accept_count++，累計 ≥3 次 → auto 升 confirmed
  ↓ ✕ reject：reject_count++、累計 ≥2 次 → 降 rejected、永不再推薦
  ↓
下次搜尋 → 展開 pipeline 只取 confirmed tier、candidate 不進搜尋變體
```

**Thresholds**（`src/db/database.py`）：
```python
_AUTO_PROMOTE_ACCEPTS = 3    # candidate/likely_typo ≥3 次 ✓ → confirmed
_AUTO_DEMOTE_REJECTS  = 2    # confirmed ≥2 次 ✕ → candidate
```

**搜尋展開實際路徑（`synonym_expander.py`）**：

```
律師輸入「僱傭」
  ↓
查 synonym_dictionary（only_confirmed=True）
  → 取 tier='confirmed' 的 variants：{「僱傭」, 「雇傭」, 「僱庸」}（律師確認過的）
  ↓
展開後 keyword list 丟 stage 1 窮盡搜尋（cartesian product + union）
```

**Seed 檔**：`law_abbreviations.json`（72 組法律簡稱、如「勞基法 ↔ 勞動基準法」）+ `synonym_seed.json`（事務所用語）啟動時同步進字典、直接進 confirmed tier、不用律師逐一確認。見 `runner.py` 的 `sync_law_abbreviations_to_synonyms` / `sync_synonym_seed_to_dict`。

**`synonym_dictionary` 表跨 task 共用**、律師 feedback 終身累積、律所多人共用時效益最大。律師在事務所持續用、candidate 清單會自然收斂成事務所特有的用語庫。

流程全圖見下方「[Stage 流程](#stage-流程4-階段律師主動推進)」段。

### 議題二：判決結構判讀優化

**痛點**：律師讀 40K+ 字的判決要能快速跳章節、對照法條、不被散落的引文干擾。司法院 HTML 與 MCP 輸出各種邊界情境會讓素樸的字串切分錯亂。這是整個專案處理最多細節的一塊。

**MCP parser 層（Python）**

| 問題 | 對策 | 位置 |
|---|---|---|
| 最高行政法院用 `.htmlcontent` Word-paste 格式、段落**完全沒換行符**（整段 2000 字一行） | `_insert_outline_breaks()` 在句末標點 + outline marker 之間強制插 `\n` | [mcp-taiwan-legal-db/.../judicial_parser.py](mcp-taiwan-legal-db/mcp_server/parsers/judicial_parser.py) |
| 民事/行政「事實及理由」合併段、facts 欄位多為空 | MCP 統一歸入 reasoning；前端偵測 `facts_coverage < 20%` 時自動 disable「同時精讀事實」勾選框 | [src/api/tasks.py](src/api/tasks.py) `get_facts_coverage` |
| Parse 結構異常（空欄位、單行過長、引號不平衡、cited_statutes 漏抓）| 自動偵測 → `data/parser_anomalies.jsonl`，累積供日後 query 修 parser 邊界 | [src/utils/anomaly_log.py](src/utils/anomaly_log.py) |

**Frontend parser 層（JavaScript）**

| 問題 | 對策 |
|---|---|
| **6 層階層 marker** 體系（L0 壹貳 / L1 一二 / L2 ㈠(一) / L3 ⒈1. / L4 ⑴(1) / L5 ①） | 每層獨立偵測 + 連續性 ≥ 2 才視為有效列舉；司法院用 PUA 字元表 `㈩` 之後的 `⑪⑫⑬` 做 context-aware 處理 |
| 「台財融㈥字第XXX號」「之㈡」誤判為 outline marker | **Boundary check**：marker 前一字必須是句末標點 / 空白 / 閉引號括弧之一 |
| 缺漏 `」` 導致 depth 卡住、後半段被誤判為引號內 | **Context-aware 雙閾值**：偵測到 `判決：/規定：/按` 等 citation prefix 用寬鬆閾值（30000 字），否則保守（500 字）force-close |
| 引條文款號「下列情形之一：三、... 六、... 分別定有明文」被誤當本案 outline | **Pass 2.5** 撤銷：數字不連續 OR（從非 1 起 + 有 closure 關鍵字）→ merge 回前段、不顯示於 outline |
| 舊判決無正式 `一、二、三、` marker | **Informal L1 promotion**：用「上訴人主張」「本院認為」等短語偵測、promote 為 outline |
| 刑事判決 ASCII box-drawing 表格被當一般段落 | 偵測 `┌└├─` 等字元、整段標 `level: 'table'`，後續 pass 全跳過 |

**閱讀器 UX 層**

- Hanging indent 用單一 `<p>` + `text-indent` / `padding-left`（避免 `<mark>` 高亮跨 span 斷）
- 各種 marker 寬度自動 em 計算對齊（CJK 1em / ASCII 0.5em）
- **AI 評價區塊** 放 reader 頂部：score 大字 + direction badge + 法院立場摘要 + 可點擊的原文摘錄（跳到判決中對應段並 seal 暖銅 flash）
- IntersectionObserver 當前章節 highlight + sticky header + 進度條
- `A / K` = 上一則、`D / J` = 下一則、`ESC` = 關閉；到頭/尾自動 toast「已是第一/最後一則」

### 議題三：LLM 介入的地方

Claude 在 pipeline 兩處介入，每處都有成本控制與品質設計：

#### (A) AI 精讀 — 成本最大、最吃設計

把每筆判決交給 Claude 逐筆讀，輸出結構化評分。痛點是長判決的 token 成本與「lost in the middle」注意力流失。

| 面向 | 做法 |
|---|---|
| **Per-judgment 評分** | Claude Haiku 4.5（便宜、快），輸出 `score (0-10)` + `direction (支持/反對/中性)` + `position (60 字法院立場)` + `excerpt (150 字原文摘錄)` + `found_in (來自哪段)` |
| **兩階段評分** | 第一輪 3K char budget 粗篩，`score > 0` 的再跑 12K budget 完整評分（對 200 筆判決省 60% tokens） |
| **Smart truncate** | reasoning 超 budget 時用「搜尋關鍵字 + NL 問題拆詞」當定位依據、每個 hit 前後 2K 窗口、頭尾保留 300 字 context |
| **V2 結構化 prompt** | 判決以【理由】【主文】【事實】【引用法條】section headers 餵給 model；prompt 內寫「根據問題類型判斷哪些段落重要」 |
| **Token bucket 限流** | Haiku Tier 1 上限 50K ITPM / 50 RPM，code 內設 40K / 40（20% safety margin） |
| **Synthesis 綜合摘要** | Claude Sonnet 4.6（品質高、只跑 1 次）— 讀每筆的 direction + position 做歸納，不重讀全文 |
| **探索型 vs 立場型分流** | 問題如「法院如何判斷 X」→ 全中性 direction 是正常，prompt 教 model 聚焦「要件 / 標準」；問題有立場分布 → 才寫「對立見解」 |
| **追問零成本** | 對同一批 task_judgments 跑新問題，讀 DB cached 欄位，不重抓 MCP、不重跑 per-judgment 評分（除非律師明確 re-run） |

實作全在 [src/pipeline/analyze.py](src/pipeline/analyze.py)。

##### 評分結構 + 推薦排序

AI 精讀的每筆輸出存在 `analysis_results` 表，律師看到的清單就是這些評分的呈現：

```
analysis_results
├ case_id            判決字號
├ score      0-10    AI 對此筆「論述詳細度」的評分
├ match      yes/no/partial
├ reason     "[支持|反對|中性] 法院立場摘要（60 字內）"
└ excerpt    "[理由|主文|事實|引用法條] 判決原文摘錄（150 字內）"
```

**預設排序：score 高→低**。律師想找某法律論點時，最有實質論述的判決自然排最上面。

清單上同時顯示：
- Score 大字（≥7 seal 暖銅 / 4-6 warm-500 / <4 warm-400）
- Direction badge（支持 emerald / 反對 red / 中性 warm）
- Position（法院立場 60 字）
- Excerpt（原文摘錄 150 字，用 seal 暖銅 `.hit-mark` 底色）

**排序替代：切到日期排序**（header 上方有切換按鈕 `分數 ↔ 日期`），走 `date DESC`（新到舊）。

**Cluster 分 tab**（讓律師用不同視角看同一批結果）：
- **全部**（score > 0）— 預設 tab
- **使用者標記** — 律師手動 star 的（Per-session 狀態，存 `state.card.starred` set）。Tab active 時右側浮出「下載 PDF」icon 打包 ZIP
- **無關**（score = 0，match ≠ data_error）— 灰色 dashed 樣式、避開但不隱藏，讓律師可以驗證 AI 沒誤剔
- **AI 自動分群**（Synthesis 產出的 clusters）— LLM 針對探索型問題會按「判斷要件」分群、立場型問題按「理由類型」分群。每 cluster 一個 tab，顯示該群代表性 case_ids

Tab 切換純 client-side 過濾（零 API call）。閱讀器的 A/D/J/K 上下篇導覽走**完整池**（所有 123 筆相關+無關+資料異常），不受 tab 綁定。

**AI 評價區塊**（閱讀器頂部）把這份結構的 score + direction + position + excerpt 整合呈現：點「原文摘錄」框 → smooth scroll 跳到判決中對應段 + seal 暖銅 flash 高亮 1.5 秒。實作在 [`src/ui/static/js/app.js`](src/ui/static/js/app.js) 的 `_buildAiEvalBlock`。

#### (B) 同義關鍵字候選名單

法律用語多種寫法（「僱傭 / 雇用 / 雇傭」/「古蹟 / 古績」），搜尋時需要自動展開同義組。

Tier 設計（**discovery 一律 candidate、律師手動確認才升 confirmed**）：

| Tier | 搜尋時行為 | 來源 |
|---|---|---|
| `confirmed` | 自動展開、進搜尋變體 | 律師 ✓ 累積 ≥3 次、或 seed 檔進來（`law_abbreviations.json` 72 組 / `synonym_seed.json`）|
| `candidate` | 不自動展開、UI 待確認清單 | Stage 3 精讀時 Claude 順便 discover |
| `likely_typo` | 不展開 | 初次 upsert 時 corpus_hits 1–5 |
| `rejected` | 不展開 | 律師 ✕ 累積 ≥2 次、或 corpus_hits=0 |

Confirmed tier 永久存 `synonym_dictionary` 表、跨 task 共用。搜尋 pipeline 用 `only_confirmed=True` 模式只取 confirmed。實作在 [src/pipeline/synonym_expander.py](src/pipeline/synonym_expander.py) + [src/api/expansion.py](src/api/expansion.py)；精讀 discovery 在 [src/pipeline/analyze.py `_persist_discovered_variants`](src/pipeline/analyze.py)；thresholds `_AUTO_PROMOTE_ACCEPTS=3` / `_AUTO_DEMOTE_REJECTS=2` 見 `src/db/database.py`。律師在「設定 → 同義詞庫 → 待確認」逐一 ✓ ✕ 建自己事務所的用語庫。

---

## 系統架構

```
┌──────────────────────────────────────────────────────────────┐
│  Browser (vanilla JS + Tailwind CDN)                         │
└────────────────────────┬─────────────────────────────────────┘
                         │ HTTP + SSE
┌────────────────────────▼─────────────────────────────────────┐
│  FastAPI (src/main.py)                                       │
│  ├─ src/api/      tasks / analyses / stream / judgments      │
│  │                cases / expansion / workers                │
│  ├─ src/worker/   dispatch_work + asyncio.Semaphore(3)       │
│  │                （fire-and-forget task，非 queue）          │
│  └─ src/pipeline/ search / filter / analyze                  │
│                   citation_* / synonym_expander / cons_*     │
└──────────┬───────────────────────────┬───────────────────────┘
           │                           │
           ▼                           ▼
┌──────────────────┐    ┌──────────────────────────────────┐
│  SQLite (9 表)   │    │  外部服務                         │
│  judgment_       │    │  ├─ mcp-taiwan-legal-db (fork)   │
│  search.db       │    │  │  ├ 司法院 data.aspx (FJUD)    │
│                  │    │  │  └ cons.judicial.gov.tw      │
│                  │    │  │    (憲法法庭 / 大法官解釋)     │
│                  │    │  │    (httpx + Playwright fb)    │
└──────────────────┘    │  └─ Anthropic API                │
                        │     └ Claude Haiku/Sonnet 4.x    │
                        └──────────────────────────────────┘
```

### 各層職責

| 層 | 主要檔案 | 做什麼 |
|---|---|---|
| **API** | `src/api/*.py` | RESTful + SSE，純薄層 — 接 request → `asyncio.create_task(dispatch_work(…))` → 回 task_id。含 `cases`（跨 task 星標 / 歷史分析）、`expansion`（同義詞 preview / CRUD）、`workers`（診斷 + kill）|
| **Worker** | `src/worker/runner.py` | `dispatch_work` 接四種 WorkItem（stage1_search / stage25_fetch / reasoning_prefilter / stage3_analyze）；`_stage_sem = Semaphore(3)` 全域併發上限，MCP 端本來就 serialize、sem 只防暴衝 |
| **Pipeline** | `src/pipeline/*.py` | search → filter → analyze、同義詞展開、法條 tuple 比對、憲法解釋 normalize。每階段獨立可測 |
| **DB** | `src/db/database.py` | aiosqlite，9 張表：`tasks` / `task_search_hits`（stage 1 廣搜清單） / `task_judgments`（全文快取） / `task_prefilter_results`（理由預篩） / `analyses` / `analysis_results` / `synonym_dictionary`（跨 task） / `case_stars`（跨 task 星標） / `stage25_inflight`（fetch 重啟恢復）|
| **MCP Client** | `src/mcp_client.py` | 持久化 stdio MCP session，連 mcp-taiwan-legal-db subprocess |
| **Frontend** | `src/ui/static/` | 純 HTML/JS/CSS，無 build step，dev/prod 同一份 |

### Stage 流程（4 階段，律師主動推進）

```
Stage 1   律師輸入關鍵字（含 OR 語法）→ 同義詞展開 → search_judgments → 候選清單
          結果存 task_search_hits（只有字號 + 法院 + 日期 + 案由）

Stage 2   律師在前端對候選清單做 client-side 篩選（法院層級 / 年度 / 案由關鍵字）
          純前端、零 API call

Stage 2.5 律師選定子集 → POST /fetch-judgments → worker 逐筆 get_judgment
          （全域 token bucket 60 req/min / burst 30，跨 task 共用）
          結果存 task_judgments（reasoning / main_text / facts / cited_statutes 全部存）
          可選「內文細篩」做字串過濾

Stage 3   律師輸入精讀問題 → POST /analyses → worker 對每筆跑 Claude
          結果存 analysis_results（score / direction / position / excerpt / found_in）
          每批完成推 SSE，前端漸進渲染
          完成後可追問 → 新增 analysis 層、不重抓 MCP
```

---

## Stage 3 進階控制：中止 / 續跑 / 定稿（2026-04-19 新設計）

律師長時間任務中最常見的需求：**跑到一半看出苗頭了、想提早停下看初步結果**。
這節描述三組對 scoring phase 的控制動作、何時觸發哪條路徑。

### 三個 endpoint

| Endpoint | 用途 | 適用狀態 |
|---|---|---|
| `POST /api/tasks/{tid}/analyses/{aid}/abort` | 律師按「中止」| `running` or `pending`（命中 ≥ 3 才走 graceful，否則前端自動改走 kill-worker） |
| `POST /api/tasks/{tid}/analyses/{aid}/resume` | 律師按「繼續未完成的分析」| `partial`（atomic check-and-set、防 double-click race） |
| `POST /api/tasks/{tid}/analyses/{aid}/finalize` | 律師按「就用現在結果定稿」| 有 `synthesis_is_preliminary=1` 即可；running 情境也走即時升格 |

### 中止（Abort）— 三條路徑依當下狀態分流

```
                        │
       按「中止分析」    │ 按「中止並查看目前結果」
                        │
           ▼                                ▼
  ┌─ fetch 階段 ─────┐    ┌─ scoring ≥ 3 命中 ─────────────┐
  │ DELETE /fetch-   │    │ POST /abort                    │
  │  judgments       │    │ → set graceful_abort flag      │
  │ + kill-worker    │    │ → fire-and-forget background   │
  │ → State A        │    │   _fire_abort_partial_synthesis│
  └──────────────────┘    │ → run_synthesis（5-15 秒）     │
                          │ → 寫 status='partial'          │
  ┌─ scoring < 3 命中 ┐   │ → publish stage3_partial_done  │
  │ DELETE /fetch-    │   │   is_final=False               │
  │  judgments        │   │ → FE 切 State C 看 partial    │
  │ + kill-worker     │   └────────────────────────────────┘
  │ → State A         │
  └───────────────────┘
```

- **判準用 `match_count`（命中數）而非 `rows_done`（已分析數）**：沒命中 synthesis 不出東西
- Fast path 跑 synthesis 前會 double-check `_is_graceful_abort` 旗是否還在（防 `/resume` race）
- Synthesis call 失敗時寫 fallback synthesis（`_fallback=True`）+ SSE、避免 FE 永卡「AI 綜合分析中…」

### 續跑（Resume）

```
POST /resume
  → atomic UPDATE WHERE status='partial' SET status='running'  (防 double-click)
  → _clear_graceful_abort（清掉先前 /abort 留下的旗）
  → 新 Stage3AnalyzeWork dispatch
  → run_analysis_v2 streaming 模式、already_done_ids 跳過已分析筆數
  → total = expected_total（剩餘）+ already_done_count（UI 進度正確反映「全量」）
```

續跑不 reset `completed` / `match_count` / `analysis_results`、只追補剩下的。

### 定稿（Finalize）— 兩條路徑

```
POST /finalize
  │
  ├─ 有 preliminary synthesis（含 running 情境）─────────┐
  │   status == 'running' ⇒ 同時設 graceful_abort + finalize 旗
  │                       讓背景 scoring workers cooperative 停下
  │   DB 立即升格：status='done'、is_preliminary=0
  │   Publish stage3_synthesis_done with is_final=True
  │   回 response body `is_final=True`
  │   → FE 可直接 re-render 不等 SSE（0 延遲）
  │
  └─ 無 preliminary、scoring 剛開始（罕見）────────────┐
      只設 finalize 旗、回 `is_final=False`
      Retry loop 邊界 check 後升格
      → FE 等 SSE + 5 秒 fallback check 保險
```

### SSE 事件（新增）

- `stage3_partial_done`：abort graceful path 完成、partial synthesis 可看。payload 含 `done`, `total`, `match_count`, `is_final`, `synthesis`。`is_final=False` = 律師還能 /resume 或 /finalize；`is_final=True` = 已升格 done
- `stage3_cancelled`：命中 <3 的情境、status→cancelled、不跑 synthesis

### UI 狀態機

```
State A  任務面板 / 判決清單
State B  scoring 進度畫面（live feed + ticker）
State C  分析結果畫面（synthesis + 判決列表）

           Stage 3 start
               ↓
           State B ──── 中止 / scoring<3 命中 ───→ State A（kill-worker）
               │
               │ 中止 / scoring≥3 命中 → partial synthesis
               ↓
           State C ─── 繼續未完成的分析 ──→ State B（續跑）
               │       (狀態：partial→running、banner「重開中」→「初步結果」)
               │
               └─ 就用現在結果定稿 → State C 最終版（banner 消失）
```

**Banner 文案切換邏輯**（`renderCardSynthesis` 依 `analysis.status` + `is_preliminary`）：
- `running + is_preliminary=1 + total<=completed` → 「重開中 · 任務重開中…」（transient、等 backend 更新 total）
- `running + is_preliminary=1 + total>completed` → 「初步結果 · 仍在分析剩餘 N 筆」+ [查看進度][定稿]
- `partial + is_preliminary=1` → 「已中止 · 停於 X/Y（命中 K）」+ [繼續未完成的分析][定稿]
- `status ∈ {failed, cancelled, done}` → 一律不顯示 banner

### 相關 in-memory flags（uvicorn lifecycle 內）

- `_graceful_abort_requested: set[str]` — /abort 設、/resume 清、scoring-end branch 清
- `_finalize_requested: set[str]` — /finalize running 路徑設、retry loop 讀到 break

---

## 快速開始

### 系統需求
- macOS / Linux（Windows 未測）
- Python 3.11+
- Node.js（只跑 JS syntax check 用，非必須）
- 約 3-5 GB 磁碟（Playwright Chromium + venv）
- Anthropic API Key（Stage 3 精讀必需）

### 安裝

```bash
cd judicial-search-tool

# Python venv（用 uv 也行）
python3.12 -m venv .venv
source .venv/bin/activate

# 主專案 + MCP fork（兩個都要 editable install — 見「重要技術決策」）
pip install -e .
pip install -e ./mcp-taiwan-legal-db

# Playwright Chromium（MCP fallback 用，HTTP fetch 失敗時才啟動）
.venv/bin/python -m playwright install chromium

# DB schema 初始化（首次 / schema 改動時）
sqlite3 judgment_search.db < schema.sql
```

### 啟動

```bash
# 開發模式（hot reload）
.venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8765 --reload

# 開瀏覽器
open http://127.0.0.1:8765/
```

API key 在 UI 設定面板輸入（會存 localStorage、走 subprocess env 傳給 backend，不寫 disk）。

### 跑測試

```bash
.venv/bin/python -m pytest tests/
```

目前 198 個測試（citation extractor、filter、worker、anomaly_log、5 件真實判決 fixture regression、abort/resume/finalize 語意）。`tests/inspect_*.py` / `validate_*.py` 是 ad-hoc 工具不歸 pytest。

---

## 專案結構

```
judgment-search/
├── README.md              ← 你看的這份
├── CLAUDE.md              ← 給 Claude Code 的產品 spec（domain 知識 + UI 規格）
├── SEARCH_REDESIGN.md     ← 兩階段搜尋 + Stage 3 v2 完整規格（權威來源）
├── pyproject.toml         ← 注意：mcp-taiwan-legal-db 故意不在 dependencies
├── schema.sql             ← SQLite schema
├── judgment_search.db     ← runtime 產生，已 gitignore
├── data/                  ← runtime 資料
│   └── parser_anomalies.jsonl  ← stage 2.5 fetch 偵測到的 parse 異常
├── mcp-taiwan-legal-db/   ← MCP fork（editable install），改了 max_results / month-day / cache key / parser
├── src/
│   ├── main.py            ← FastAPI app + lifespan + worker 啟動
│   ├── mcp_client.py      ← 持久化 MCP stdio session
│   ├── sse_bus.py         ← per-task SSE pub/sub
│   ├── api/
│   │   ├── tasks.py       ← POST /tasks（stage 1 廣搜）
│   │   ├── judgments.py   ← GET /tasks/{id}/judgments（多分析層 JOIN 清單）
│   │   ├── analyses.py    ← POST /tasks/{id}/analyses（stage 3 精讀）
│   │   ├── stream.py      ← GET /tasks/{id}/stream（SSE）
│   │   ├── cases.py       ← 跨 task case-level：星標 / 該案過往分析歷史
│   │   ├── expansion.py   ← 同義詞 preview / 字典 CRUD
│   │   └── workers.py     ← 執行中 work 列表 / kill（律師卡住時用）
│   ├── pipeline/
│   │   ├── search.py      ← search_judgments wrapper + 同義詞展開
│   │   ├── filter.py      ← get_judgment + 字串欄位過濾（+ 全域 MCP 限流 bucket）
│   │   ├── analyze.py     ← Claude 精讀 + smart truncate + 兩階段評分 + synthesis
│   │   ├── strategy.py    ← [dormant] NL 查詢策略拆解；UI 入口已移除，code 暫留（POST /api/strategy 仍回應）
│   │   ├── citation_extractor.py    ← 從判決文字抽出 (法名, 條, 項, 款) tuple
│   │   ├── citation_normalizer.py   ← Citation tuple match
│   │   ├── synonym_expander.py      ← 同義詞展開（confirmed/candidate tiers）
│   │   ├── cons_normalizer.py       ← 憲法解釋（cons.judicial）回傳 normalize 成 FJUD-shape
│   │   └── pdf_generator.py
│   ├── worker/
│   │   └── runner.py      ← WorkItem 類型 + dispatch_work + sem(3) + recovery
│   │                        （舊版 `queue.py` 已移除，改為 fire-and-forget asyncio task）
│   ├── db/database.py     ← aiosqlite layer，所有 SQL 都在這
│   ├── data/              ← static data
│   │   ├── law_abbreviations.json   ← 法律全名 ↔ 簡稱（72 組；啟動時同步進 synonym_dictionary）
│   │   └── synonym_seed.json        ← 事務所確認一般法律用語同義組（假處分 / 僱傭 / 相對優勢地位 等）
│   ├── ui/static/
│   │   ├── index.html     ← single-page，Tailwind 設定：parchment + ink + seal 暖銅
│   │   ├── styles.css     ← 補 Tailwind 沒做到的（reader UI、AI 評價框、tier）
│   │   └── js/
│   │       ├── core.js    ← state、API helpers、reusable utils
│   │       ├── app.js     ← 主 app 邏輯（reader、search、analyze 流程）
│   │       └── init.js    ← 啟動 hook
│   └── utils/
│       ├── retry.py       ← 指數退避
│       ├── rate_limiter.py ← Token bucket（Claude API ITPM/RPM）
│       ├── json_parse.py  ← Robust JSON extract from LLM output
│       └── anomaly_log.py ← Parser 異常 → JSONL（見「故障排除」）
└── tests/
    ├── fixtures/          ← 5 件真實判決 parsed JSON snapshot
    ├── test_judgment_structure_regression.py  ← 跟 fixtures 對照
    ├── test_anomaly_log.py
    ├── test_citation.py / test_filter.py / test_analyze.py / test_strategy.py / test_worker.py
    ├── regenerate_fixtures.py  ← 重建 fixture（繞過 MCP cache 直接打 parser）
    └── validate_*.py / inspect_*.py  ← ad-hoc 偵錯工具
```

---

## 重要技術決策（為什麼這樣設計）

### 1. MCP 用 fork 而非 upstream
**Why**：upstream `mcp-taiwan-legal-db` 預設 `max_results=20`、不支援 month/day filter、cache key 沒涵蓋 case_type、最高行 `.htmlcontent` parse 段落擠成一團（一行 2000 字）。我們改了：
- `max_results` clamp 到 500（對應司法院網站單次顯示上限）
- 加 `month_from/day_from/month_to/day_to` 做窮盡搜尋的 keyset pagination
- search cache key 涵蓋 case_type / court / month / day
- court fallback：搜尋無結果時自動拿掉 court filter 重試
- `_insert_outline_breaks()` pass：在 `[。：；]` + outline marker 之間強制 `\n`，修最高行 .htmlcontent 段落沒硬斷行的 bug

**怎麼維持同步**：fork 已 detach，upstream 改動需手動 merge。`pyproject.toml` 的 `dependencies` 故意不列 mcp-taiwan-legal-db，避免 pip 自動拉 upstream 蓋掉 fork。

### 2. facts 欄位民事/行政常為空
**Why**：絕大多數民事與行政判決把「事實」與「理由」合併為一個段落（標題即「事實及理由」）。MCP parser 將整段歸入 `reasoning`，**facts 為空是正確設計**，不是 bug。

**對使用者影響**：律師若想找「被告辯稱 / 案件背景」這類事實情境，**在民事/行政案上應用 reasoning 而非 facts**。前端 `applyFactsCoverageHint()` 偵測此情境，自動 disable「同時精讀事實」勾選框。

### 3. task_judgments 永遠存所有欄位
**Why**：律師第一次選 `filter_field=理由`，追問時可能想「主文有沒有撤銷」。如果只存第一次選的欄位，追問就要重新打 MCP — 浪費時間且有限速風險。

**怎麼做**：worker 一律寫 `reasoning + main_text + facts + cited_statutes + full_text` 到 task_judgments，後續追問跨欄位都從 DB 取。

### 4. 兩階段評分（screening + full-pass）
**Why**：Claude API token 成本。對 200 筆判決全部跑 12K char budget 太貴。

**怎麼做**：第一輪用 3K char budget 粗篩（per item ~$0.001），對 score>0 的（通常 30% 以下）才跑 12K budget 完整評分。閾值在 `analyze.py` 的 `TWO_PASS_THRESHOLD = 20`。

### 5. Smart truncate + keyword window
**Why**：reasoning 可能 46K 字，超出 token 預算。但「lost in the middle」問題會讓 Claude 對中段內容失焦。

**怎麼做**：把搜尋關鍵字 + NL 問題拆詞當定位依據，每個 hit 取前後 2K 窗口，合併重疊。頭尾各保留 300 字 context（法院判決常在尾部下結論）。`_smart_truncate()` 與 `_extract_question_terms()` 在 `analyze.py`。

### 6. Citation prefix 雙閾值（normal vs citation mode）
**Why**：判決原文偶有缺漏 `」`（OCR / 資料錯誤），孤立 `「` 會讓深度卡住、後半段被誤判為引號內 → 內部 outline marker 全被吞掉。但合法長引用（大法官解釋、整條法條原文）可能極長。

**怎麼做**：偵測 `「` 前是否有 citation prefix（`判決：/解釋：/條規定：/認定：/按` 等），有 → 信任長引用（30000 字、10 markers）；無 → 保守 force-close（500 字、3 markers）。Patterns 在 `app.js` 的 `_CITATION_PREFIX_PATTERNS`。

### 7. Haiku 評分 + Sonnet 總結
**Why**：per-judgment 評分要跑 100-300 次，用 Sonnet 太貴；synthesis 只跑 1 次但需要綜合判斷力。

**設定**：`MODEL_SCORING = "claude-haiku-4-5-20251001"`、`MODEL_SYNTHESIS = "claude-sonnet-4-6"`。Token bucket 限流（`ITPM_LIMIT = 40_000` Haiku Tier 1 -20% safety margin）。

### 8. 司法院搜尋 500 筆硬上限 → keyset pagination
**Why**：司法院 web 介面單次最多顯示 500 筆。超過要切換條件。

**怎麼做**：當搜尋估計超過 500 筆，用 month/day 範圍切窗口（如 `year=110, month=1-3` → `4-6` → ...），結果合併去重。實作在 `pipeline/search.py` 的窮盡搜尋分支。

### 9. iCloud UF_HIDDEN gotcha → 手動設 PYTHONPATH
**Why**：macOS 把 site-packages 下的 `.pth` 檔標 `UF_HIDDEN` flag（iCloud / Time Machine 同步副作用），Python 3.12+ 的 site.py 跳過 hidden `.pth` → editable install 失效 → MCP subprocess import 不到 `mcp_server`。

**怎麼做**：`mcp_client.py` 的 `init_mcp()` 明確把 fork 根目錄加進 `PYTHONPATH` env var，繞過 `.pth` 機制。

### 10. Vanilla JS 沒框架
**Why**：律師工具的 UI 複雜度沒到需要 React/Vue。框架引進的 build step + bundle size + state 同步成本 > 收益。Tailwind CDN 直接用、無 PostCSS pipeline。

**Trade-off**：6000+ 行 app.js 維護壓力較大。`core.js` 抽 state 與 API wrapper、`app.js` 放主邏輯、`init.js` 放啟動 hook。dev mode hot reload 靠 uvicorn `--reload`（Python 改才 reload，JS/CSS 改 browser refresh 即可）。

### 11. 全程 SSE 而非 WebSocket

SSE 單向（server→client）就夠用、省掉 WebSocket 的雙向 framing + close handshake 複雜度、而且跟 FastAPI 的 async generator 原生合拍。前端用標準 `EventSource`、不用額外 lib、斷線 auto-reconnect 免處理。

### 12. 窮盡搜尋的 MCP failure retry（2026-04-19）

`run_search_exhaustive` 會用 date cursor 反覆對 MCP 下探、直到某輪拿到 <500 筆視為「抓到底」。**但 MCP 一次性呼叫失敗（network ReadError / 司法院 WAF 拒連）也會讓 hits 回傳 0 筆**、被誤判成抓到底提早結束（原本沒這段時、「公法上不當得利」這類大量 keyword 卡在 1000 筆）。

修法：
- `mcp_client.search_judgments` 偵測 `{"success": False}` → raise `MCPSearchError`（原本吞成空 list）
- `_exhaustive_single_keyword` 加 3 次 retry with exponential backoff（1s→2s→4s）
- 3 次都失敗才 break、log warning 告知累積筆數（律師能看）

### 13. F5 WAF cookies 的雙層自動 refresh

司法院網站 F5 WAF cookies（`mcp-taiwan-legal-db/data/.judicial_cookies.json`）過期後（~24 小時），**TCP 層 reset connection**、httpx 丟 `ReadError` / `ConnectError`。原本 `get_with_waf_retry` 只在 `is_blocked(r.text)` 為 True 時 refresh、但 network-level 錯誤根本沒 response body、refresh 不會觸發 → 永遠失敗。

修法（見 `mcp-taiwan-legal-db/mcp_server/tools/waf_bypass.py`）：
```python
try:
    r = await func(url, **kwargs)
except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError):
    # network-level → refresh cookies + retry
    await waf.refresh()
    ...
if waf.is_blocked(r.text):
    # body-level block → refresh + retry
    ...
```

### 14. 中止的三段式設計：disruptive vs graceful

律師按「中止分析」的實際行為依當下狀態自動選路徑（不給律師選、邏輯自動判）：

| 階段 | 路徑 | 按鈕文案 |
|---|---|---|
| Fetch 階段（progressPhase='fetch'）| Disruptive：`DELETE /fetch-judgments` + `kill-worker` → State A | 「中止分析」|
| Scoring + match_count < 3 | Disruptive（沒結果可保留）→ State A | 「中止分析」|
| Scoring + match_count ≥ 3 | Graceful：`POST /abort` + 背景跑 partial synthesis → State C | 「中止並查看目前結果」|

判準用 `match_count`（命中數）而非 `rows_done`（已分析數）：沒命中的判決再多、synthesis 也只會輸出「精讀後沒有判決有論述此問題」、partial 無意義。

**Graceful path 的 in-flight edge**：已進入 `_run_and_report` 的判決（最多 CONCURRENCY=8 筆）會跑完才釋放 worker；若 Claude 卡住、律師按中止後 15 秒自動出現「強制結束」按鈕（走 kill-worker、丟 8 筆 in-flight 資料）。

### 15. Finalize 即時升格（不等 retry loop）

Preliminary synthesis（partial / preliminary watcher 寫）升格成 final 的路徑：

- 原設計（2026-04-19 前）：`/finalize` 設 flag、等 retry loop 下一輪邊界 check 才升格 → 若 scoring 第一輪還在跑、律師可能乾等 1 分鐘
- 新設計：`/finalize` 有 `synthesis_is_preliminary=1` 就**立即 DB 升格** `status=done`、`is_preliminary=0`、同時設 `graceful_abort` 旗讓 scoring cooperative 停下；回 response body `is_final=True`、FE 立即 re-render（不等 SSE）
- 兩旗並存時（abort + finalize）**abort 不覆寫已升格的 done**（scoring-end branch re-read analysis、status=done+synthesis → skip）

### 16. 「任何中止都是 partial」（2026-04-19 決策）

原設計中「續跑中再中止 → 升格 done（was_resumed→done）」實測不好用：律師以為只是想提早看結果、沒想到被鎖進 done。改成：

- **任何中止都寫 partial**、保留 `is_preliminary=1`、可重複續跑
- **升格 done 只有兩條路**：scoring 自然跑完、或律師主動按「就用現在結果定稿」

### 17. `task_judgments` 的 case_id 格式陷阱

MCP `get_judgment` 正常回傳 `case_id` 為人類案號（「108年度訴字第1145號」）、`source_url` 含 JID（`TPBA,108,訴,1145,20220414,3`）。**但歷史上某些 parser 失敗情境、MCP 回空 judgment dict with `case_id = JID`（fallback 到外層 jid 變數）、然後被 `create_task_judgment` 存入 DB**。結果：task_judgments 某些 row 的 `case_id=JID` + 內容全空。

律師看起來就是「N 筆 data_error」。修法：
- 短期：SQL 刪這些壞 rows、下次 resume 會重抓（MCP 現在 parser 正常會有內容）
- 長期 TODO：`create_task_judgment` 加 guard — 若內容全空拒寫 + 記 anomaly log

### 18. MCP search cache key 不含 max_results（latent bug）

MCP server 的 `search_cache` query_params 只記錄 keyword + 日期範圍等、**不含 `max_results`**。若某次測試用 `max_results=10` 查詢、MCP cache 把「這個 query 的結果是 10 筆」固化、之後 `max_results=500` 的查詢也回那 10 筆。

現況：實務上窮盡搜尋永遠用 `max_results=500`、synonym_expander 用 `max_results=1`、兩者互不觸及相同 cache key（因為 synonym 用不同 keyword 變體）。Latent bug、不急修。若跑 ad-hoc debug 測試時一定用 `max_results=500` 或用不同 keyword、免污染 cache。

SSE 事件類型清單（供 FE 訂閱參考）：`stage25_progress / judgments_ready / batch_done / stage3_synthesis_start / stage3_synthesis_done / stage3_partial_done / stage3_cancelled / preliminary_synthesis_done / analysis_done / analysis_failed / task_done`。前端用 `EventSource` 訂閱 `/api/tasks/{id}/stream`。

---

## 開發者注意事項

### 改 MCP parser
`mcp-taiwan-legal-db/mcp_server/parsers/judicial_parser.py` 是 fork 的核心。改動後跑：
```bash
.venv/bin/python tests/regenerate_fixtures.py    # 重建 5 件真實判決 fixture
.venv/bin/python -m pytest tests/test_judgment_structure_regression.py
```
fixture regenerate 會繞過 MCP cache 直接打 parser，所以一定反映當下 parser 行為。MCP 端有自己的 30 天 sqlite cache，舊資料要等 TTL 或手動 invalidate。

### 改 frontend parser（解析判決文字結構）
`src/ui/static/js/app.js` 的 `parseJudgmentParagraphs` 系列。6 層階層 marker 體系（L0 壹貳 / L1 一二 / L2 ㈠ / L3 ⒈ / L4 ⑴ / L5 ①）+ PUA 延伸 + boundary check + 引號保護。改之前讀 `~/.claude/projects/.../memory/judgment_structure_knowledge.md`（如果該 memory 不存在，看 [SEARCH_REDESIGN.md](SEARCH_REDESIGN.md) 與本檔「重要技術決策」#6）。

### 用 `data/parser_anomalies.jsonl` 做 parser 迭代資料來源

**這是本專案持續優化結構解析與斷行邏輯的核心反饋機制** — 每次 stage 2.5 fetch 完一筆判決，[`anomaly_log.log_judgment`](src/utils/anomaly_log.py) 會自動檢查 5+ 種結構訊號（空欄位、超長行無 marker、引號嚴重不對稱、cited_statutes 漏抓、outline marker 號碼跳號等），有異常就 append 一行 JSON 到 `data/parser_anomalies.jsonl`；非同步、非阻塞、失敗僅 log 不影響 pipeline。

**律師每抓一批判決、這個檔就累積一批真實世界的 parser 失敗樣本**，是調整 heuristic（Pass 2.5 撤銷條件、boundary check 容忍度、citation prefix 清單等）的第一手資料。下次要改 parser 前，**務必先看累積分布**：

```bash
# 哪種 anomaly 最常見 → 優先修的 parser bug
jq -r '.anomaly_types[]' data/parser_anomalies.jsonl | sort | uniq -c | sort -rn

# 哪個法院最常出問題 → 優先處理的 court-specific HTML
jq -r '.court' data/parser_anomalies.jsonl | sort | uniq -c | sort -rn

# outline_number_gap 的 case 看 first marker 分布
jq -r 'select(.anomaly_types[] == "outline_number_gap") | .metrics.outline_gaps' \
   data/parser_anomalies.jsonl | head -20
```

偵測規則、閾值調整、完整 jq recipe 見 [`src/utils/anomaly_log.py`](src/utils/anomaly_log.py) 模組 docstring 與開發者 memory `~/.claude/projects/.../memory/parser_anomaly_log.md`。

**文化約定**：新增或調整 parser heuristic 前，先跑上述 jq 查詢確認當前痛點；改完後搭配 `tests/test_judgment_structure_regression.py` 的 5 件 fixture 守門避免退步。長期看 anomaly log 類型分布的趨勢（某種 anomaly 類型從月均 50 降到 5，即為 heuristic 改動成功的量化證據）。

### 加新分析欄位（如 court_tier, citation_density）
1. `schema.sql` 加欄位
2. `db/database.py` 的 `get_task_judgments` SELECT 含新欄位
3. `worker/runner.py` 的 `_run_stage25_fetch` 算出值並寫入
4. 前端 `core.js` state 加對應欄位、`app.js` UI 顯示

### 加新 Claude prompt
主要 prompt 在 `src/pipeline/analyze.py`（per-judgment 評分 + synthesis）。改完跑：
```bash
.venv/bin/python -m pytest tests/test_analyze.py
```
（`strategy.py` 為 dormant 模組，若要改仍可跑 `tests/test_strategy.py`。）

### 注意事項：commit / 環境

- **不 commit**：`judgment_search.db*`、`data/parser_anomalies.jsonl`、`.venv/`、`__pycache__/`、`tests/__pycache__/`
- **API key**：永遠不寫 disk、不 echo 到 log。`localStorage` 在 frontend 存、subprocess env 傳給 backend
- **prompt 改動**：對應的 test fixture（`test_*.py` 內的 `expected_*`）一定要更新

### Server 重啟時的 task recovery 行為

Server 重啟（`--reload` 檔案變動、systemd restart、Docker 重啟等）會導致 worker 記憶體裡的 per-request API key 消失。Stage 3 的 LLM 呼叫需要 key，所以 recovery 策略分兩層：

| 情境 | 行為 | 說明 |
|---|---|---|
| **env var `ANTHROPIC_API_KEY` 已設** | 自動 re-queue pending/running analysis，用 env key | 生產部署（systemd / Docker）的標準路徑 — 重啟完全無感 |
| **env var 沒設，但瀏覽器在線** | mark failed → 前端 `loadHomeTasks` 偵測到 10 分鐘內失敗 + localStorage 有 key → 靜默自動 retry + toast | 開發環境常見，律師不用做事 |
| **env var 沒設 + 瀏覽器沒開** | mark failed → 律師下次打開時，前端同上自動 retry | 仍然 zero-click |
| **env var 沒設 + localStorage 沒 key** | mark failed → 前端顯示 banner 「N 個任務中斷 — 全部重試」 | 最差 fallback；使用者先設 key 再手動重試 |

實作：[`src/worker/runner.py`](src/worker/runner.py) `_recover_new_task` (後端)、[`src/ui/static/js/app.js`](src/ui/static/js/app.js) `handleRestartAutoRetry` (前端)。

**Stage 2.5（cache fetch）** 走另一套 recovery — `stage25_inflight` 表紀錄進行中的 fetch，server 重啟時掃這張表自動重跑（fetch 無 LLM 成本、不依賴 key）。實作 `src/worker/runner.py` 的 `_recover_stage25_inflight`。

**要注意**：env var 跟 per-request header 的 key 可能不同（律師 UI 輸入一把、env 另一把）。程式優先使用 per-request header，僅在 recovery 時 fallback 到 env。若兩把 key 對應不同 Anthropic 帳戶，成本會跑到 env 那個帳戶上。

---

## 故障排除

### MCP 連不上 / Stage 1 搜尋一直 spinner
1. 看 server log 有沒有 `MCP 初始化失敗`
2. 確認 `pip install -e ./mcp-taiwan-legal-db` 跑過
3. 若 macOS 上 iCloud sync 過此目錄，可能是 UF_HIDDEN — 手動執行：
   ```bash
   chflags nohidden .venv/lib/python*/site-packages/*.pth
   ```
4. Playwright Chromium 沒裝：`.venv/bin/python -m playwright install chromium`

### Stage 3 跑到一半 RateLimit
- 看 `analyze.py` 的 `ITPM_LIMIT / RPM_LIMIT`，預設 Haiku Tier 1 (40K ITPM / 40 RPM with -20% safety)
- Anthropic 升 tier 後改這兩個常數

### UI 顯示「API 較慢, 自動重試中」／「N 秒無進度」
**這是慢、不是錯**。發生條件與處理：

- **兩個以上的 analysis 同時跑**時特別明顯。`_STAGE_CONCURRENCY = 3`（[src/worker/runner.py](src/worker/runner.py)）允許最多 3 個 analysis 並行，每個內部 `CONCURRENCY = 8`，最壞情況 24 條 Haiku 請求搶同一個 40K ITPM / 40 RPM bucket
- **長判決**（反托拉斯、稅務等理由動輒 50K+ 字）單筆 estimated tokens 高，bucket 更快飽和
- **觸發 429 的 backoff 是 30s / 60s / 90s**（[src/pipeline/analyze.py:486](src/pipeline/analyze.py)），多條並行 retry 時 stall 90–150s 是正常的
- **驗證方法**：查 `analysis_results.analyzed_at` 序列，只要時間戳還在推進（即使間隔長到 2 分鐘），就是在工作、不是當機
  ```bash
  sqlite3 judgment_search.db "SELECT analyzed_at FROM analysis_results WHERE analysis_id='<id>' ORDER BY analyzed_at DESC LIMIT 10"
  ```
- **想避免**：一次只送一個 analysis，或把 `_STAGE_CONCURRENCY` 調到 1（犧牲多工換穩定進度感）

### Reader UI 看到怪段落 / outline 異常
- 看 `data/parser_anomalies.jsonl` 有沒有該案的紀錄：
  ```bash
  jq -c "select(.case_id | contains(\"104年度建上字第98號\"))" data/parser_anomalies.jsonl
  ```
- 沒紀錄表示是 frontend `parseJudgmentParagraphs` 的判定問題；有紀錄表示 MCP parser 端
- 找出該案的 jid，跑 `tests/inspect_anomalies.py` 看細節

### Server `--reload` 沒生效
uvicorn `--reload` 只 watch Python file。JS/CSS 改瀏覽器 hard refresh（`Cmd+Shift+R`）即可，不需要 server 重啟。

---

## 部署

本工具設計為 single-machine、single-worker 架構（後文「核心限制」會解釋為何不能多 worker）。最常見部署形態有三種，視使用人數與環境選擇。

### 核心限制（部署前必讀）

| 限制 | 嚴重度 | 說明 |
|---|---|---|
| **`--workers 1` 強制** | 🔴 致命 | worker 用 in-process state（`_stage_sem`、SSE bus、stage25_inflight set、worker registry），多 process uvicorn 各自獨立這堆 state → 同一 task 的進度訊號會回錯 process、任務分散執行；加上 SQLite 同時 writer 會 `database is locked`。**永遠用 `--workers 1`** |
| **MCP fork 不能走 PyPI** | 🔴 致命 | `mcp-taiwan-legal-db` 是 fork（改了 max_results / month-day / parser），不該 publish PyPI。任何部署環境都要把 fork source 一起帶過去手動 `pip install -e ./mcp-taiwan-legal-db` |
| **nginx 必須關 SSE buffering** | 🔴 致命 | 不關律師看不到任何進度、會以為 server 掛了 |
| **Playwright Chromium 系統 deps** | 🔴 致命 | Linux 缺 `libnss3 / libgbm1 / libxshmfence1` 等 lib，Chromium 啟不起來。用 `playwright install --with-deps chromium`（要 root）或 playwright 官方 base image |

### 形態 A：macOS 個人永久跑（最簡單）

適合：只給自己用、機器整天開。**現在的 dev 環境本身就接近這個**，只多兩件事：
1. 拿掉 `--reload` 旗標
2. launchd plist 開機自啟

範例 `~/Library/LaunchAgents/com.lawyer.judgment-search.plist`：
```xml
<key>ProgramArguments</key>
<array>
  <string>/Users/<you>/judgment-search/.venv/bin/python</string>
  <string>-m</string><string>uvicorn</string>
  <string>src.main:app</string>
  <string>--host</string><string>127.0.0.1</string>
  <string>--port</string><string>8765</string>
</array>
<key>WorkingDirectory</key>
<string>/Users/<you>/judgment-search</string>
<key>EnvironmentVariables</key>
<dict>
  <key>ANTHROPIC_API_KEY</key><string>sk-...</string>
</dict>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
```

`launchctl load` 一次後，機器重開就會自動跑。缺點：機器睡眠就斷。

### 形態 B：Linux server（事務所內網 / VPS）

適合：律師同事 2-5 人共用。最常見的 production 形態。

**systemd unit**（`/etc/systemd/system/judgment-search.service`）：
```ini
[Unit]
Description=Judgment Search Tool
After=network.target

[Service]
Type=simple
User=judgment
WorkingDirectory=/opt/judgment-search
Environment="ANTHROPIC_API_KEY=sk-..."
ExecStart=/opt/judgment-search/.venv/bin/python -m uvicorn src.main:app \
  --host 127.0.0.1 --port 8765 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**nginx reverse proxy**（含 SSE 必要設定）：
```nginx
server {
    listen 443 ssl http2;
    server_name judgments.firm.local;
    ssl_certificate     /etc/letsencrypt/live/.../fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_buffering off;            # ★ SSE 必須，沒關律師看不到進度
        proxy_read_timeout 3600s;       # ★ Stage 3 可能跑數十分鐘
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Host $host;
    }
}
```

### 形態 C：Docker container

適合：想隔離環境、跨機器部署。

最大的雷：**base image 必須包 Chromium 系統 libs**。建議用 `mcr.microsoft.com/playwright/python:v1.40.0-jammy`（已內建 Chromium 與 deps），自己裝太雜。

骨架 `Dockerfile`：
```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy
WORKDIR /app
COPY pyproject.toml schema.sql ./
COPY src/ ./src/
COPY mcp-taiwan-legal-db/ ./mcp-taiwan-legal-db/   # ★ fork source 必須 COPY 進來
RUN pip install -e . -e ./mcp-taiwan-legal-db
RUN python -m playwright install chromium

VOLUME ["/app/data", "/app/judgment_search.db"]    # ★ 持久化
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "src.main:app", \
     "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
```

`docker run` 範例：
```bash
docker run -d \
  -p 8765:8765 \
  -e ANTHROPIC_API_KEY=sk-... \
  -v /srv/judgment-search/data:/app/data \
  -v /srv/judgment-search/judgment_search.db:/app/judgment_search.db \
  -v /srv/judgment-search/mcp-cache:/root/.cache/mcp-taiwan-legal-db \
  --restart unless-stopped \
  judgment-search:latest
```

### MCP fork 打包方式（三選一）

不要在 production 直接 `git clone + pip install -e .`，三個原因：
1. iCloud / Dropbox / NFS / btrfs snapshot 等同步機制會把 `.pth` 檔標 hidden flag → editable install 失效
2. editable install 把 source path 寫進 `.pth`，部署目錄改位置就壞
3. fork commit 會 silent drift，這次 deploy 跟下次行為可能不同

**Option 1 — vendored source + editable install**（最簡單，形態 A/B 推薦）
```bash
# Build 機：tar 整包（含 mcp-taiwan-legal-db/）
tar czf deploy-$(git rev-parse --short HEAD).tar.gz judgment-search/
# Production：解開 + 建 venv + editable install
tar xzf deploy-*.tar.gz && cd judgment-search
python3.12 -m venv .venv
.venv/bin/pip install -e . -e ./mcp-taiwan-legal-db
.venv/bin/python -m playwright install --with-deps chromium
```

**Option 2 — wheel 打包**（半工程化，若懼 editable install）
```bash
# Build 機：fork 打 wheel
cd mcp-taiwan-legal-db && pip wheel . -w ../wheels/
# Production：直接 install wheel，免 fork source 進部署目錄
.venv/bin/pip install ../wheels/mcp_taiwan_legal_db-*.whl
```
好處：部署目錄怎麼搬都不影響 import。  
缺點：fork 改 code 要重 build wheel + 重 deploy。

**Option 3 — Docker COPY**（形態 C 用）  
見上面 Dockerfile，`COPY mcp-taiwan-legal-db /app/mcp-taiwan-legal-db` + `RUN pip install -e .`，layer cache 友善。

### Production 設定差異

| 項目 | Dev | Production |
|---|---|---|
| `--reload` | ✅ | ❌（穩定性 + 啟動速度）|
| `--workers` | 不指定（預設 1） | 顯式 `--workers 1` |
| API Key 來源 | UI localStorage | env var `ANTHROPIC_API_KEY`，backend 從 env 讀 |
| Host bind | `127.0.0.1` | `127.0.0.1`（讓 nginx proxy）或 `0.0.0.0`（直曝外網，須有 firewall）|
| Log | stdout（uvicorn 預設）| systemd journal / Docker log driver / logrotate file |
| `data/parser_anomalies.jsonl` | 累積即可 | logrotate 週切 + archive |
| MCP cache 位置 | `~/.cache/mcp-taiwan-legal-db` | 同 — 但確認在 persistent volume / 不是 tmpfs |

### Fork 維護與 commit pin

**本 repo 的 MCP fork 已 flatten 進 monorepo**（`mcp-taiwan-legal-db/` 沒 `.git`）—
便於同事單次 `git clone` 就能拿到完整可跑的版本。對應 upstream fork 的快照：

- **Upstream**：https://github.com/lawchat-oss/mcp-taiwan-legal-db
- **當前 snapshot commit**：`e946840`（fork: port month/day params 到 upstream 新版 JudicialSearchClient）
- Fork 自己的 3 個 commit（領先 upstream）：`e946840` / `fdebd22` / `059d658`

維護 workflow（要 sync upstream 時）：
1. 去原始有 `.git` 的 MCP fork 工作目錄（個人本機 `judgment-search/mcp-taiwan-legal-db/`）做 `git pull` + 合併
2. 跑 `tests/regenerate_fixtures.py` + `pytest` 確認沒 regression
3. 記下新的 commit hash
4. `rsync -a --exclude='.git' --exclude='data/cache/' .../mcp-taiwan-legal-db/ ./mcp-taiwan-legal-db/` 覆蓋
5. 更新本段記錄的 snapshot commit hash + commit

upstream 改動時主要合併衝突點：`judicial_parser.py`（我們加的 `_insert_outline_breaks`）、`judicial_search.py`（max_results clamp、month/day params）、`waf_bypass.py`（network-level error auto-refresh、2026-04-19 加的）。

### 容易忘的維運

| 任務 | 頻率 | 怎麼做 |
|---|---|---|
| 看 anomaly log | 每月 | `jq -r '.anomaly_types[]' data/parser_anomalies.jsonl \| sort \| uniq -c \| sort -rn` 看哪種 parser bug 最常出現 |
| MCP cache 體積 | 每季 | `du -sh ~/.cache/mcp-taiwan-legal-db/`；超過 5 GB 建議手動清舊資料（30 天 TTL 應該會自清） |
| API Key rotation | Anthropic 政策 | env var 換掉 + systemd reload，無需 code 改動 |
| Fork upstream 升級 | 看 upstream 活躍度 | 拉 upstream → merge fork → 跑 `tests/test_judgment_structure_regression.py` 確認沒退化 |

---

## 相關文件

| 文件 | 用途 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | 給 Claude Code 的 spec：產品域知識、UI 規格、資料模型 |
| [SEARCH_REDESIGN.md](SEARCH_REDESIGN.md) | 兩階段搜尋 + Stage 3 v2 完整規格（權威來源） |
| `~/.claude/projects/.../memory/` | 開發者個人 memory：解析器知識庫、reader UI 決策、技術踩坑、parser anomaly log 用法 |
| `tests/regenerate_fixtures.py` 註解 | fixture 維護流程 |
| `src/utils/anomaly_log.py` 模組 docstring | parser_anomalies.jsonl 格式與偵測規則 |
