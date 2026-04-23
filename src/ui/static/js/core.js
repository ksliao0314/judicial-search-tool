'use strict';

// ─── Global Error Guard ──────────────────────────
// 捕捉未處理的 runtime error，避免單一錯誤炸掉整頁互動
window.addEventListener('error', e => {
  console.error('[Global error]', e.message, e.filename, e.lineno);
});
window.addEventListener('unhandledrejection', e => {
  console.error('[Unhandled rejection]', e.reason);
});

// ─── Safe DOM helper ─────────────────────────────
// getElementById 的 null-safe 版本：找不到元素時 log warning 但不 crash
function $el(id) {
  const el = document.getElementById(id);
  if (!el) console.warn(`[DOM] Element #${id} not found`);
  return el;
}

// ─── Constants ───────────────────────────────────
var KEY_STORAGE = 'anthropic_api_key';
var API = {
  tasks:           '/api/tasks',
  strategy:        '/api/strategy',
  expandPreview:   '/api/expand-preview',
  shouldPreview:   '/api/should-preview',
  synonymFeedback: '/api/synonym-feedback',
  task:            (id)     => `/api/tasks/${id}`,
  analyses:        (tid)    => `/api/tasks/${tid}/analyses`,
  stream:          (tid)    => `/api/tasks/${tid}/stream`,
  judgments:       (tid)    => `/api/tasks/${tid}/judgments`,
  starredCases:    '/api/cases/starred',
  caseStar:        (cid)    => `/api/cases/${encodeURIComponent(cid)}/star`,
  caseAnalyses:    (cid)    => `/api/cases/${encodeURIComponent(cid)}/analyses`,
  prefilterResult: (tid)    => `/api/tasks/${tid}/prefilter-result`,
  prefilterStart:  (tid)    => `/api/tasks/${tid}/reasoning-prefilter`,
  workersDebug:    '/api/debug/workers',
  killWorker:      (tid)    => `/api/tasks/${tid}/kill-worker`,
};

// ─── SSE safe handler wrapper ─────────────────────
// 所有 SSE event listener 都應用此 wrapper，防止 JSON.parse 失敗或任何異常中斷整條 listener chain
// 卡片開啟時鎖住頁面捲動，關閉時恢復
let _savedScrollTop = 0;
function lockBodyScroll() {
  const home = document.getElementById('view-home');
  if (home && !home.dataset.locked) {
    _savedScrollTop = home.scrollTop;
    home.dataset.locked = '1';
    home.style.position = 'fixed';
    home.style.inset = '0';
    home.style.overflow = 'hidden';
    // 鎖定 html + body 防止 iOS Safari 穿透捲動
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    document.body.style.overscrollBehavior = 'none';
    document.body.style.touchAction = 'none';
  }
}
function unlockBodyScroll() {
  const anyOpen = !document.getElementById('search-card').classList.contains('hidden')
    || !document.getElementById('reader-card').classList.contains('hidden')
    || !document.getElementById('tasklist-card').classList.contains('hidden')
    || !document.getElementById('search-help-card').classList.contains('hidden');
  if (!anyOpen) {
    const home = document.getElementById('view-home');
    if (home && home.dataset.locked) {
      delete home.dataset.locked;
      home.style.position = '';
      home.style.inset = '';
      home.style.overflow = '';
      home.scrollTop = _savedScrollTop;
      // 解鎖 html + body
      document.documentElement.style.overflow = '';
      document.body.style.overflow = '';
      document.body.style.overscrollBehavior = '';
      document.body.style.touchAction = '';
    }
  }
}

function sseHandler(fn) {
  return async (e) => {
    try {
      await fn(e);
    } catch (err) {
      console.error('[SSE handler error]', err);
    }
  };
}

// ─── App State ────────────────────────────────────
var state = {
  mode: 'keyword',           // 'keyword' | 'semantic'
  view: 'home',              // 'home' | 'strategy' | 'results' | 'history'

  // 使用者星標的 case_ids（跨 task 全域、DB 持久化）。
  // 啟動時由 initStarredCases() 從 /api/cases/starred 載入；toggle 時 sync 到 API。
  // 位置在 state 頂層而非 state.card 下 — 語意從「本 task 內標記」升級到「律師資產」。
  starred: new Set(),

  tasks: [],
  currentTaskId: null,
  analyses: [],
  primaryAnalysisId: null,
  secondaryAnalysisId: null,
  judgments: [],
  strategies: [],            // for semantic mode
  selectedStrategyIdx: 0,
  filters: { minScore: 6, matchType: '' },
  tasksPanelOpen: true,
  sse: null,                 // EventSource

  // 此 task 的 facts 欄位覆蓋率（民事/行政「事實及理由」常合併歸 reasoning，
  // facts 多空 → 「同時分析事實」勾選等同無效，UI 用此資訊 disable checkbox）
  // 由 GET /api/tasks/{id} 載入；total === 0 表示尚未抓全文，不下結論
  factsCoverage: { total: 0, with_facts: 0 },

  // 兩階段流程（stage 2 view）
  hits: [],                  // task_search_hits 全部清單（stage 1 結果）
  stage2: {
    selectedTiers:  new Set(),   // 法院層級 multi-select
    yearFrom: null,              // 拉桿值（民國年）；null = 不限
    yearTo:   null,
    yearMin:  null,              // 從 hits 推出的可選範圍（slider min/max）
    yearMax:  null,
  },

  // 浮動搜尋卡片
  card: {
    open: false,
    taskId: null,
    analysisId: null,
    state: null,          // 'a' | 'b' | 'c'
    searching: false,     // Stage 1 搜尋進行中
    comboProgress: null,  // {idx, total} — stage 1 keyword 變體進度（8 個變體時顯示「變體 3/8」）
    progress: 0,          // 0-100
    progressPhase: '',    // 'fetch' | 'read' | 'synth'
    fetchTotal: 0,        // 全文抓取總數（進度條算 %）
    analyzeTotal: 0,      // Claude 分析總數
    clusters: [],         // from synthesis
    activeCluster: null,  // cluster index or null
    allResults: [],       // State C 的全部結果（用於 cluster filter + pagination）
    resultsOffset: 0,     // pagination
    sortBy: 'score',      // 'score' | 'date'
    readCaseIds: new Set(),        // 已讀的 case_ids（per session，點過 reader 就加入）
    searchWarnings: [],           // Stage 1 截斷警告
    skippedCaseIds: [],           // stage2.5 fetch 失敗、可手動重試的 case_id JID 列表
    reasoningFilter: false,       // 理由預篩 toggle
    prefilterCaseIds: null,       // 預篩命中的 case_ids
    prefilterRunning: false,      // 預篩進行中
    prefilterTotal: 0,
    prefilterFetched: 0,
    prefilterMatched: 0,
  },

  // 通知鈴鐺
  bell: {
    tasks: new Map(),           // taskId -> {status, progress, keyword, analysisId, unread}
    sseConnections: new Map(),  // taskId -> EventSource
    unreadCount: 0,
  },
};

// 法院層級對照（與後端 search.py 的 COURT_TIERS 順序一致：specific 在前避免 substring 衝突）
// 114 年行政訴訟改制後「高等行政法院地方庭」為獨立 tier（審初審行政案件，獨立運作）；
// 法律上歸屬高等行政法院但實務上律師會把它跟本院分開篩選，故單獨一個選項。
var STAGE2_COURT_TIERS = ['憲法法庭', '最高行政法院', '最高法院', '高等行政法院', '高等行政法院地方庭', '高等法院', '智慧財產及商業法院', '地方法院', '其他'];
var TIER_DISPLAY_NAME = {
  '智慧財產及商業法院': '智財法院',
  '高等行政法院地方庭': '行政地方庭',  // 節省 checkbox 寬度
};

function inferTier(court) {
  if (!court) return '其他';
  if (court.includes('憲法法庭'))     return '憲法法庭';
  if (court.includes('最高行政法院')) return '最高行政法院';
  if (court.includes('最高法院'))     return '最高法院';
  if (court.includes('智慧財產') || court.includes('商業法院')) return '智慧財產及商業法院';
  // 必須在 '高等行政法院' 之前檢查 — 否則 "臺北高等行政法院 地方庭" 會先被歸為高等行政法院
  if (court.includes('高等行政法院') && court.includes('地方庭')) return '高等行政法院地方庭';
  if (court.includes('高等行政法院')) return '高等行政法院';
  if (court.includes('高等法院'))     return '高等法院';
  if (court.includes('地方法院') || court.includes('少年及家事')) return '地方法院';
  return '其他';
}

// ─── API fetch helper ─────────────────────────────
function apiFetch(url, opts = {}) {
  const key = localStorage.getItem(KEY_STORAGE);
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (key) headers['X-Api-Key'] = key;
  return fetch(url, { ...opts, headers });
}

