'use strict';

// ─── Worker status polling（讓律師看到排隊中的任務） ─────────────
// 每 3 秒 poll /api/debug/workers，把結果渲染到 bell 旁的「排隊 N」indicator
// 及卡片 State B 的「age」診斷資訊（給律師判斷是否該按強制中止）
let _workersPollTimer = null;
let _workersSnapshot = { sem_capacity: 3, sem_available: 3, waiters: 0, work_timeout_sec: 3600, active: [] };

async function pollWorkersOnce() {
  try {
    const res = await apiFetch(API.workersDebug);
    if (!res.ok) return;
    _workersSnapshot = await res.json();
    renderWorkersIndicator();
    // 卡片打開 + 在 State B → 更新「工作 N 秒無進度」警告
    if (state.card.open && state.card.state === 'b' && state.card.taskId) {
      updateCardStuckWarning(state.card.taskId);
    }
  } catch (err) {
    // 靜默 — polling 失敗不該影響主流程
  }
}

// State B 的卡住警告：距離上次 SSE 進度事件（batch_done）超過 STALL_THRESHOLD 秒
// → 顯示紅字警告。只看「單次 gap」、不累加；task 重啟 / batch 完成都會把 gap 歸零
const _STALL_THRESHOLD = 60;  // 秒
let _lastProgressAt = 0;

function markProgressReceived() {
  _lastProgressAt = Date.now();
}

function updateCardStuckWarning(taskId) {
  const age = getTaskWorkerAge(taskId);
  if (age === null) return;  // 沒有 active worker → 不顯示警告
  const etaEl = document.getElementById('card-progress-eta');
  if (!etaEl) return;

  // 未收到過 SSE → 用 worker age 當 gap；收到過 → 用距上次進度時間
  const secSinceProgress = _lastProgressAt ? Math.floor((Date.now() - _lastProgressAt) / 1000) : age;

  let warnEl = document.getElementById('card-stuck-warn');
  if (secSinceProgress >= _STALL_THRESHOLD) {
    if (!warnEl) {
      warnEl = document.createElement('div');
      warnEl.id = 'card-stuck-warn';
      warnEl.className = 'mt-3 text-[11px] font-mono text-red-500';
      etaEl.parentElement?.appendChild(warnEl);
    }
    warnEl.textContent = `⚠ LLM 已 ${secSinceProgress} 秒未回應，若持續無反應請按「中止分析」`;
  } else {
    warnEl?.remove();
  }
}

function startWorkersPolling() {
  if (_workersPollTimer) return;
  pollWorkersOnce();
  _workersPollTimer = setInterval(pollWorkersOnce, 3000);
}

function renderWorkersIndicator() {
  const el = document.getElementById('workers-indicator');
  if (!el) return;
  const s = _workersSnapshot;
  const busy = s.active.length;
  const queued = s.waiters;
  if (busy === 0 && queued === 0) {
    el.classList.add('hidden');
    return;
  }
  el.classList.remove('hidden');
  const queueNote = queued > 0 ? `，排隊 ${queued}` : '';
  el.textContent = `執行中 ${busy}${queueNote}`;
  el.title = s.active.map(w =>
    `${w.type} · ${w.task_id.slice(0,8)} · ${w.age_sec}s`
  ).join('\n');
}

// 取得特定 task 的 active work age（卡片 State B 判斷是否該 enable「強制中止」）
function getTaskWorkerAge(taskId) {
  const w = _workersSnapshot.active.find(a => a.task_id === taskId);
  return w ? w.age_sec : null;
}

// ─── Stage 2 filter 持久化（localStorage per task） ─────────────
// 律師跳出卡片再回來不會丟失選好的 tier / year 範圍（原本每次 openTask 都 reset）
const _STAGE2_KEY = (tid) => `stage2_filter_${tid}`;

function loadStage2FilterFromStorage(taskId) {
  const empty = {
    selectedTiers: new Set(),
    yearFrom: null, yearTo: null, yearMin: null, yearMax: null,
  };
  try {
    const raw = localStorage.getItem(_STAGE2_KEY(taskId));
    if (!raw) return empty;
    const saved = JSON.parse(raw);
    return {
      selectedTiers: new Set(Array.isArray(saved.selectedTiers) ? saved.selectedTiers : []),
      yearFrom: saved.yearFrom ?? null,
      yearTo:   saved.yearTo   ?? null,
      yearMin:  null,  // 由 setupCardYearSlider 從 hits 重算，不從 storage 讀
      yearMax:  null,
    };
  } catch {
    return empty;
  }
}

function saveStage2FilterToStorage(taskId) {
  if (!taskId) return;
  try {
    localStorage.setItem(_STAGE2_KEY(taskId), JSON.stringify({
      selectedTiers: [...state.stage2.selectedTiers],
      yearFrom: state.stage2.yearFrom,
      yearTo:   state.stage2.yearTo,
    }));
  } catch {}
}

// ─── Starred cases bootstrap ──────────────────────
// App 啟動時從 DB 載入律師過往星標。失敗不阻塞其他功能（降級為空 set）。
async function initStarredCases() {
  try {
    const res = await apiFetch(API.starredCases);
    if (!res.ok) throw new Error(`starredCases ${res.status}`);
    const ids = await res.json();
    state.starred = new Set(Array.isArray(ids) ? ids : []);
  } catch (err) {
    console.warn('[star] 載入星標失敗，維持空 set:', err);
  }
}

// ─── View transitions ─────────────────────────────
function showView(name) {
  ['home', 'strategy', 'results', 'history'].forEach(v => {
    const el = document.getElementById(`view-${v}`);
    if (el) el.classList.toggle('hidden', v !== name);
  });
  state.view = name;
  // 同步 header nav tab active state
  if (typeof setActiveNavTab === 'function') {
    setActiveNavTab(name === 'history' ? 'history' : 'home');
  }
  // 切到 history 時 render
  if (name === 'history') renderHistory();
}

// ─── Browser history / routing ────────────────────
// 每次 user 主動切換 view 都呼叫 navTo() 推一筆 history entry，
// 讓瀏覽器上一頁/下一頁能在 home / results / reader 之間穿梭。
// 內部 (openTask / openReader 被 popstate 反向呼叫) 不應再 navTo，避免無限循環。
function navTo(s, replace = false) {
  const target = s || { view: 'home' };
  if (replace) {
    history.replaceState(target, '', location.pathname);
  } else {
    history.pushState(target, '', location.pathname);
  }
  applyRouterState(target);
}

function applyRouterState(s) {
  const target = s || { view: 'home' };
  if (target.view === 'home') {
    closeReaderPanel();
    closeReaderCard(true);
    closeSearchCard();
    showView('home');
    return;
  }
  if (target.view === 'card') {
    showView('home');
    closeReaderCard(true);
    // Re-show card without pushing another history entry
    state.card.open = true;
    state.card.taskId = target.taskId;
    state.currentTaskId = target.taskId;
    document.getElementById('search-card-backdrop').classList.remove('hidden');
    document.getElementById('search-card').classList.remove('hidden');
    lockBodyScroll();
    const cs = target.cardState || 'c';
    // State C 需要確保結果資料已載入（從 reader card 返回時資料可能仍在 state 中）
    if (cs === 'c' && state.card.allResults.length > 0) {
      // 資料仍在 → 直接切到 State C，用 replaceState 避免再推一筆 history
      state.card.state = cs;
      ['a', 'b', 'c'].forEach(x => {
        document.getElementById(`card-state-${x}`).classList.toggle('hidden', x !== cs);
      });
      const inner = document.getElementById('search-card-inner');
      inner.style.width = '90vw'; inner.style.maxWidth = '1400px';
      inner.style.height = '90vh'; inner.style.maxHeight = '90vh';
      document.getElementById('card-header-text').textContent = '分析結果';
      document.getElementById('card-search-bar').classList.add('hidden');
    } else {
      setCardState(cs);
    }
    return;
  }
  if (target.view === 'reader-card') {
    openReaderCard(target.taskId, target.caseId);
    return;
  }
  if (target.view === 'strategy') {
    closeReaderPanel();
    showView('strategy');
    return;
  }
  if (target.view === 'history') {
    closeReaderPanel();
    closeReaderCard(true);
    closeSearchCard();
    document.getElementById('tasklist-card')?.classList.add('hidden');
    showView('history');
    return;
  }
  if (target.view === 'results') {
    closeReaderPanel();
    // Always go through openTask — it decides card vs legacy based on hasHits
    if (target.taskId) {
      openTask(target.taskId);
    } else {
      showView('results');
    }
    return;
  }
  if (target.view === 'reader') {
    if (target.taskId && target.taskId !== state.currentTaskId) {
      // 切到正確的任務後才能 openReader（reader 依賴 currentTaskId 抓判決）
      openTask(target.taskId).then(() => openReader(target.caseId));
    } else {
      openReader(target.caseId);
    }
    return;
  }
}

window.addEventListener('popstate', e => applyRouterState(e.state));

// ─── Homepage ─────────────────────────────────────
async function loadHomeTasks() {
  try {
    const res = await apiFetch(API.tasks);
    if (!res.ok) throw new Error(res.status);
    state.tasks = await res.json();
    renderHomeTasks();
    // 偵測最近失敗 + 有 localStorage key → 背景自動 retry（Option C）。
    // 一次 page load 只跑一次，不會跟手動操作衝突。
    handleRestartAutoRetry();
  } catch {
    document.getElementById('home-tasks-loading').textContent = '無法連線到後端 API';
  }
}

// 任務卡片 — 給 home 執行中 + history 列表共用。
// ── Task status helpers ──────────────────────────────
function getTaskPhase(t) {
  // 回傳任務目前的階段
  // 進行中：'searching' | 'analyzing'
  // 待處理：'ready'
  // 已完成：'done'
  // 失敗：  'failed'

  // Bell 即時狀態覆寫（比 API 快照更即時）
  const bellInfo = state.bell.tasks.get(t.id);
  if (bellInfo) {
    if (bellInfo.status === 'running') return 'analyzing';
    if (bellInfo.status === 'done') return 'done';
  }

  const hitsTotal = t.hits_total || 0;
  const analyses = t.analyses || [];
  const hasHits = hitsTotal > 0;
  const v2Analysis = analyses.find(a => a.synthesis);
  // partial = 律師按中止後產的 partial synthesis、還沒定稿也還有未分析的判決、
  // 應視為「進行中」而非已完成（task list 不應顯示綠色完成狀態）
  const runningAnalysis = analyses.find(
    a => a.status === 'running' || a.status === 'pending' || a.status === 'partial'
  );

  if (t.status === 'failed') return 'failed';
  // runningAnalysis 要在 v2Analysis 之前 check — 「重新分析」情境下 task 已有
  // 舊的 synthesis（v2Analysis truthy）同時又有新 analysis 跑中；律師要看到它在
  // 進行中、而非被誤分類成 done。partial 也走這條。
  if (runningAnalysis) return 'analyzing';
  if (v2Analysis) return 'done';
  if (hasHits && (t.status === 'done')) return 'ready';
  if (t.status === 'running' || t.status === 'pending') return 'searching';
  return t.status === 'done' ? 'ready' : 'failed';
}

// isTaskQueued 已移除（2026-04）：v2 架構 5-way 並行後，「progress 為 0 而他人
// progress 已增加」不一定代表在排隊，只是這個 task 比較慢碰到第一次 progress
// event。保留會造成假陽性「排隊中」標籤。真正的 queue wait 罕見（≥6 個 task 同
// 時跑才會發生），統一用「準備中...」已足夠。

function getTaskCategory(t) {
  const phase = getTaskPhase(t);
  // 2026-04-19：pending（等待設定分析範圍）與 active（搜尋/分析中）合併為 active，
  // 對律師來說都是「還沒完成」的工作，沒有分開的必要
  if (phase === 'searching' || phase === 'analyzing' || phase === 'ready') return 'active';
  if (phase === 'done') return 'completed';
  return 'completed'; // failed 歸已完成
}

function getTaskOrigKw(t) {
  try { const sp = JSON.parse(t.search_params || '{}'); if (sp.original_keyword) return sp.original_keyword; } catch {}
  return t.keyword || '';
}

// ─── 右側欄進行中任務 item（設計版 warm neutral card + 黑 CTA）─────
function renderHomeActiveItem(t) {
  const phase = getTaskPhase(t);
  const kw = escHtml(getTaskOrigKw(t));
  const hitsTotal = t.hits_total || 0;
  const bellInfo = state.bell.tasks.get(t.id);
  const progress = bellInfo?.progress || 0;
  const domain = t.search_domain || 'judgment';
  const tag = domain === 'interpretation' ? '釋' : '判';

  let phaseText = '準備中…';
  const hasPartial = (t.analyses || []).some(a => a.status === 'partial');
  const resumedRunning = (t.analyses || []).some(a => a.status === 'running' && a.synthesis);
  if (phase === 'searching') {
    const liveHits = (t.id === state.currentTaskId) ? Math.max(state.hits.length, hitsTotal) : hitsTotal;
    phaseText = `搜尋中 · 已找到 ${liveHits.toLocaleString()} 筆`;
  } else if (phase === 'analyzing') {
    phaseText = hasPartial ? '已中止 · 待繼續或定稿'
      : bellInfo?.progressPhase === 'fetch' ? '全文快取中'
      : bellInfo?.progressPhase === 'synth' ? '產出摘要中'
      : bellInfo?.progressPhase === 'read' || bellInfo?.progressPhase === 'screen' ? 'AI 分析中'
      : resumedRunning ? '繼續分析中…'
      : '準備中…';
  } else if (phase === 'ready') {
    phaseText = `${hitsTotal.toLocaleString()} 筆 · 待設定分析範圍`;
  }

  const progressBar = phase === 'analyzing' && progress > 0
    ? `<div class="active-card-progress"><div class="active-card-progress-fill" style="width:${progress}%"></div></div>`
    : '';

  return `
    <div class="active-card" onclick="openTask('${t.id}')">
      <div class="active-card-row">
        <span class="active-card-tag">${tag}</span>
        <div style="flex:1;min-width:0">
          <div class="active-card-title">${kw}</div>
          <div class="active-card-meta">${escHtml(phaseText)}</div>
        </div>
      </div>
      ${progressBar}
      <div class="active-card-cta">查看 →</div>
    </div>`;
}

// ─── 搜尋下方「最近完成」item ───
// 2026-04-19：版型跟歷史搜尋卡片對齊（共用 .history-card class stack），
// 以律師視角：兩個地方看到的卡片長一樣，認知負擔最小。
function renderHomeRecentItem(t) {
  const kw = escHtml(getTaskOrigKw(t));
  const domain = t.search_domain || 'judgment';
  const isInterp = domain === 'interpretation';
  const domainLabel = isInterp ? '釋' : '判';

  const analyses = (t.analyses || []).filter(a => a.status === 'done' && a.match_count != null);
  const primary = analyses.sort((a, b) => (b.match_count || 0) - (a.match_count || 0))[0];
  const match = primary?.match_count || 0;
  const question = primary?.question ? escHtml(primary.question.slice(0, 30)) : '';
  const date = (t.created_at || '').slice(5, 10);

  const cardCls = [
    'history-card',
    'is-completed',   // 最近「完成」區塊都是完成態
    isInterp ? 'is-interpretation' : 'is-judgment',
  ].join(' ');

  return `
    <div class="${cardCls}" onclick="openTask('${t.id}')">
      <div class="history-card-badge-row">
        <span class="history-card-domain-pill">${domainLabel}</span>
        <span class="history-card-date">${date}</span>
      </div>
      <div class="history-card-title">${kw}</div>
      <div class="history-card-subtitle">${question || '&nbsp;'}</div>
      <div class="history-card-stats-footer">
        <span class="history-card-stats-count">${match}</span>
        <span class="history-card-stats-unit">筆</span>
      </div>
    </div>`;
}

// ─── History view render ─────────────────────────────
const _historyState = {
  filter: 'all',      // all | active | completed
  type: 'all',        // all | judgment | interpretation
  visibleCount: 30,   // 無限 scroll 初始窗
};
const _HISTORY_PAGE_SIZE = 30;

function _historyFilterTasks(tasks) {
  const byFilter = tasks.filter(t => {
    if (_historyState.filter === 'all') return true;
    return getTaskCategory(t) === _historyState.filter;
  });
  const byType = byFilter.filter(t => {
    if (_historyState.type === 'all') return true;
    return (t.search_domain || 'judgment') === _historyState.type;
  });
  return [...byType].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
}

function renderHistory() {
  const tasks = state.tasks || [];

  // 更新 page header 「共 N 筆」
  const pageCountEl = document.getElementById('history-page-count');
  if (pageCountEl) pageCountEl.textContent = `共 ${tasks.length} 筆`;

  // 更新 tab count
  const counts = { all: tasks.length };
  for (const t of tasks) {
    const cat = getTaskCategory(t);
    counts[cat] = (counts[cat] || 0) + 1;
  }
  document.querySelectorAll('.history-tab-count').forEach(el => {
    el.textContent = counts[el.dataset.count] || 0;
  });

  const filtered = _historyFilterTasks(tasks);
  const visible = filtered.slice(0, _historyState.visibleCount);

  const gridEl = document.getElementById('history-grid');
  const emptyEl = document.getElementById('history-empty');

  if (visible.length === 0) {
    gridEl.innerHTML = '';
    emptyEl.classList.remove('hidden');
  } else {
    emptyEl.classList.add('hidden');
    gridEl.innerHTML = visible.map(renderHistoryCard).join('');
  }
}

function renderHistoryCard(t) {
  const phase = getTaskPhase(t);
  const category = getTaskCategory(t);
  const kw = escHtml(getTaskOrigKw(t));
  const isActive = category === 'active';
  // 仍區分 searching/pending 的 UI 變體（待處理要顯示「繼續」按鈕），但統一歸類在 active
  const isPending = phase === 'ready';
  const isRunning = isActive && !isPending;
  const domain = t.search_domain || 'judgment';
  const isInterp = domain === 'interpretation';
  const cardCls = ['history-card',
                   isActive ? 'is-active' : 'is-completed',
                   isInterp ? 'is-interpretation' : 'is-judgment'].filter(Boolean).join(' ');

  // 分類 pill：只顯示「判」或「釋」酒紅字，拿掉「· 法院判決」/「· 憲法解釋」後綴
  const domainLabel = isInterp ? '釋' : '判';

  const date = (t.created_at || '').slice(5, 10);

  // 副標
  const analyses = t.analyses || [];
  const doneAnalyses = analyses.filter(a => a.status === 'done' && a.match_count != null);
  const primary = doneAnalyses.length
    ? [...doneAnalyses].sort((a, b) => (b.match_count || 0) - (a.match_count || 0))[0]
    : null;
  // preliminary synthesis 已出、但 retry loop 尚未跑完 → analysis.status 仍是 'running'
  // 這種狀態加個灰色「初步」chip 提示律師「可以先看結果，但仍在補齊」
  const hasPreliminary = analyses.some(a => a.synthesis_is_preliminary && a.status !== 'done');
  let subtitle = '';
  if (primary?.question) subtitle = primary.question.slice(0, 30);
  else if (isActive) {
    // 進行中：嘗試用 search_params 的 main_text 當副標
    try {
      const sp = JSON.parse(t.search_params || '{}');
      if (sp.main_text) subtitle = `主文含：${sp.main_text}`;
    } catch {}
  }

  // Footer
  let footer;
  if (isActive) {
    const bellInfo = state.bell.tasks.get(t.id);
    const progress = bellInfo?.progress || 0;
    // partial 優先於 bellInfo（partial 時 bellInfo.progressPhase 通常已清）
    const hasPartial = analyses.some(a => a.status === 'partial');
    // Resume transient：status=running + 有 synthesis + bellInfo 尚未被 batch_done 刷新
    // → 顯示「繼續分析中…」避免 fallback「準備中…」誤導（其實 worker 已在跑）
    const resumedRunning = analyses.some(a => a.status === 'running' && a.synthesis);
    const phaseText = hasPartial ? '已中止 · 待繼續或定稿'
      : isPending ? '待設定分析範圍'
      : phase === 'searching' ? `搜尋中 · ${(t.hits_total || 0).toLocaleString()} 筆`
      : bellInfo?.progressPhase === 'fetch' ? '全文快取中'
      : bellInfo?.progressPhase === 'synth' ? '產出摘要中'
      : bellInfo?.progressPhase === 'read' || bellInfo?.progressPhase === 'screen' ? 'AI 分析中'
      : resumedRunning ? '繼續分析中…'
      : '準備中…';
    const countLabel = t.hits_total ? `${t.hits_total.toLocaleString()} 筆` : '';
    // 2026-04-19：拿掉醒目黑色 CTA 按鈕 — 進行中卡片與已完成卡片高度要一致（律師
    // 反饋：第一列被拉高看起來不齊）。改用跟完成卡片相同的 footer layout：左側狀態
    // 文字大字、右側小字 meta、下方細進度條（若正在跑）。視覺輕盈但仍傳達「進行中」。
    const progressBar = isRunning && progress > 0
      ? `<div class="active-card-progress mt-2"><div class="active-card-progress-fill" style="width:${progress}%"></div></div>`
      : '';
    footer = `
      <div class="history-card-stats-footer history-card-active-footer">
        <span class="history-card-active-phase">${escHtml(phaseText)}</span>
        ${countLabel ? `<span class="history-card-active-count">${countLabel}</span>` : ''}
      </div>
      ${progressBar}`;
  } else if (primary) {
    // 已完成：只顯示命中筆數大字，不再顯示 / total · pct% / 進度條 / 開啟 →
    footer = `
      <div class="history-card-stats-footer">
        <span class="history-card-stats-count">${primary.match_count}</span>
        <span class="history-card-stats-unit">筆</span>
      </div>`;
  } else {
    footer = `
      <div class="history-card-stats-footer">
        <span class="history-card-stats-meta">—</span>
      </div>`;
  }

  const prelimChip = hasPreliminary
    ? '<span class="history-card-prelim-chip">初步</span>'
    : '';
  return `
    <div class="${cardCls}" onclick="openTask('${t.id}')">
      <div class="history-card-badge-row">
        <span class="history-card-domain-pill">${domainLabel}</span>
        ${prelimChip}
        <span class="history-card-date">${date}</span>
      </div>
      <div class="history-card-title">${kw}</div>
      <div class="history-card-subtitle">${subtitle ? escHtml(subtitle) : '&nbsp;'}</div>
      ${footer}
    </div>`;
}

// History toolbar 事件
(function setupHistoryToolbar() {
  document.addEventListener('click', (e) => {
    const hfilter = e.target.closest('[data-hfilter]');
    const htype = e.target.closest('[data-htype]');
    if (hfilter) {
      _historyState.filter = hfilter.dataset.hfilter;
      document.querySelectorAll('[data-hfilter]').forEach(b =>
        b.classList.toggle('history-tab-active', b.dataset.hfilter === _historyState.filter));
      _historyState.visibleCount = _HISTORY_PAGE_SIZE;
      renderHistory();
    }
    if (htype) {
      _historyState.type = htype.dataset.htype;
      document.querySelectorAll('[data-htype]').forEach(b =>
        b.classList.toggle('history-type-active', b.dataset.htype === _historyState.type));
      _historyState.visibleCount = _HISTORY_PAGE_SIZE;
      renderHistory();
    }
  });

  // 無限 scroll：section 的 overflow-auto 捲到 sentinel 附近就 load 更多
  const sentinel = document.getElementById('history-scroll-sentinel');
  if (sentinel) {
    new IntersectionObserver((entries) => {
      if (!entries[0].isIntersecting) return;
      if (state.view !== 'history') return;
      const filtered = _historyFilterTasks(state.tasks || []);
      if (_historyState.visibleCount >= filtered.length) return;
      _historyState.visibleCount += _HISTORY_PAGE_SIZE;
      renderHistory();
    }, { root: document.getElementById('view-history'), rootMargin: '200px' }).observe(sentinel);
  }
})();

function renderHomeTaskItem(t) {
  const phase = getTaskPhase(t);
  const kw = escHtml(getTaskOrigKw(t));
  const hitsTotal = t.hits_total || 0;
  const bellInfo = state.bell.tasks.get(t.id);
  const progress = bellInfo?.progress || 0;

  let statusHtml = '';
  let progressBar = '';

  if (phase === 'searching') {
    // 搜尋中用 state.hits.length（即時）或 t.hits_total（API 快照），取較大值
    const liveHits = (t.id === state.currentTaskId) ? Math.max(state.hits.length, hitsTotal) : hitsTotal;
    statusHtml = `<span class="flex items-center gap-1.5 text-xs font-mono text-amber-600">
      <span class="pulse-dot w-1.5 h-1.5 rounded-full bg-amber-500 inline-block"></span>
      搜尋中，已找到 ${liveHits.toLocaleString()} 則判決
    </span>`;
    progressBar = `<div class="mt-2 h-1 bg-warm-200 rounded-full overflow-hidden">
      <div class="h-full bg-amber-400 progress-running rounded-full" style="width:100%"></div>
    </div>`;
  } else if (phase === 'analyzing') {
    // progressPhase 由 SSE 事件填；第一次事件還沒進來時 fallback 「準備中...」
    const phaseTxt = bellInfo?.progressPhase === 'fetch' ? '全文快取中'
      : bellInfo?.progressPhase === 'synth' ? '正在產出結果'
      : bellInfo?.progressPhase === 'read' || bellInfo?.progressPhase === 'screen' ? 'AI 分析中'
      : '準備中...';
    statusHtml = `<span class="flex items-center gap-1.5 text-xs font-mono text-seal">
      <span class="pulse-dot w-1.5 h-1.5 rounded-full bg-seal inline-block"></span>
      ${phaseTxt}
    </span>`;
    progressBar = `<div class="mt-2 h-1 bg-warm-200 rounded-full overflow-hidden">
      <div class="h-full bg-seal rounded-full transition-[width] duration-500" style="width:${progress}%"></div>
    </div>`;
  } else if (phase === 'ready') {
    statusHtml = `<span class="text-xs font-mono text-seal">搜尋完成 ${hitsTotal.toLocaleString()} 筆，請設定分析範圍</span>`;
  } else if (phase === 'done') {
    const analyses = t.analyses || [];
    const matchCount = analyses.reduce((s, a) => s + (a.match_count || 0), 0);
    statusHtml = `<span class="text-xs font-mono text-emerald-600">分析完成 · ${matchCount} 筆相關</span>`;
  }

  // 所有狀態都可點擊進入卡片
  const isActive = phase === 'searching' || phase === 'analyzing';
  const stopBtn = isActive
    ? `<button onclick="stopHomeTask('${t.id}', '${phase}', event)" aria-label="停止任務"
              class="opacity-0 group-hover:opacity-100 transition-opacity text-warm-400 hover:text-red-500 shrink-0 p-1"
              title="${phase === 'searching' ? '停止搜尋（將刪除任務）' : '停止分析（任務轉為待處理）'}">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="6" y="6" width="12" height="12" rx="1"/>
        </svg>
      </button>` : '';
  const deleteBtn = `<button onclick="deleteTask('${t.id}', event)" aria-label="刪除任務"
            class="opacity-0 group-hover:opacity-100 transition-opacity text-warm-200 hover:text-red-400 shrink-0 p-1"
            title="刪除任務">
      <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>
        <path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/>
      </svg>
    </button>`;

  // search_domain badge：判 (法院判決、seal 紅) / 釋 (憲法解釋、深藍)
  const domain = t.search_domain || 'judgment';
  const domainBadge = domain === 'interpretation'
    ? `<span class="shrink-0 inline-flex items-center justify-center w-5 h-5 rounded-sm text-[10px] font-serif bg-indigo-100 text-indigo-700" title="憲法解釋">釋</span>`
    : `<span class="shrink-0 inline-flex items-center justify-center w-5 h-5 rounded-sm text-[10px] font-serif bg-seal/10 text-seal" title="法院判決">判</span>`;

  return `
    <div class="group flex items-center gap-3 px-4 py-3 border border-warm-200
                bg-white/60 hover:bg-warm-100 cursor-pointer transition-colors rounded-sm"
         onclick="navTo({view:'results', taskId:'${t.id}'})">
      ${domainBadge}
      <div class="flex-1 min-w-0">
        <div class="font-serif text-sm text-ink truncate mb-1">${kw}</div>
        ${statusHtml}
        ${progressBar}
      </div>
      <div class="flex items-center gap-0.5">
        ${stopBtn}
        ${deleteBtn}
      </div>
    </div>`;
}

// ── Task list card (grid) item ────────────────────────
const TASK_CARD_STYLES = {
  active:    { border: 'border-amber-200', bg: 'bg-amber-50/40', label: '進行中', labelCls: 'text-amber-600 bg-amber-100' },
  pending:   { border: 'border-seal/30',   bg: 'bg-seal/5',      label: '待處理', labelCls: 'text-seal bg-seal/10' },
  completed: { border: 'border-emerald-200', bg: 'bg-emerald-50/30', label: '已完成', labelCls: 'text-emerald-700 bg-emerald-100' },
};

function renderTaskGridCard(t) {
  const phase = getTaskPhase(t);
  const cat = getTaskCategory(t);
  const style = TASK_CARD_STYLES[cat] || TASK_CARD_STYLES.completed;
  const kw = escHtml(getTaskOrigKw(t));
  const hitsTotal = t.hits_total || 0;
  const date = new Date(t.created_at).toLocaleDateString('zh-TW');

  const analyses = t.analyses || [];
  const matchCount = analyses.reduce((s, a) => s + (a.match_count || 0), 0);
  const metaLine = hitsTotal > 0
    ? `${hitsTotal} 筆判決${matchCount > 0 ? ` · ${matchCount} 相關` : ''}`
    : date;

  return `
    <div class="group relative border ${style.border} ${style.bg} hover:shadow-md
                cursor-pointer transition-all rounded-sm px-4 py-3"
         onclick="closeTaskListCard(); navTo({view:'results', taskId:'${t.id}'})">
      <button onclick="deleteTaskFromList('${t.id}', event)" aria-label="刪除"
              class="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity
                     text-warm-400 hover:text-red-500 p-0.5">
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
      <div class="flex items-start gap-2 mb-2 pr-5">
        ${(t.search_domain || 'judgment') === 'interpretation'
          ? `<span class="shrink-0 inline-flex items-center justify-center w-5 h-5 rounded-sm text-[10px] font-serif bg-indigo-100 text-indigo-700" title="憲法解釋">釋</span>`
          : `<span class="shrink-0 inline-flex items-center justify-center w-5 h-5 rounded-sm text-[10px] font-serif bg-seal/10 text-seal" title="法院判決">判</span>`}
        <div class="font-serif text-sm text-ink truncate flex-1">${kw}</div>
      </div>
      <div class="flex items-center justify-between">
        <span class="text-[10px] font-mono px-1.5 py-0.5 rounded-sm ${style.labelCls}">${style.label}</span>
        <span class="text-[10px] font-mono text-warm-400">${metaLine}</span>
      </div>
    </div>`;
}

// ── Render home task queue ─────────────────────────────
function renderTaskLists() {
  document.getElementById('home-tasks-loading').classList.add('hidden');

  const allSorted = [...state.tasks].sort((a, b) => b.created_at.localeCompare(a.created_at));

  // 進行中（searching / analyzing / ready 待設定分析範圍 — 2026-04-19 把 ready 併入
  // 進行中：對使用者來說「已搜尋完但還沒設定分析」也是未完成的工作，跟歷史頁分類
  // 邏輯一致）
  const active = allSorted.filter(t => {
    const p = getTaskPhase(t);
    return p === 'searching' || p === 'analyzing' || p === 'ready';
  });

  // 最近完成（已分析完成的）取 2 筆
  const recentCompleted = allSorted.filter(t => getTaskPhase(t) === 'done').slice(0, 2);

  // ── 右側欄：進行中任務 ──
  // 有任務：列出實卡 + 補滿到 3 格（虛線空位填餘數）
  // 完全沒任務：只顯示一個「無進行中任務」佔位，不鋪滿 3 格虛空
  const aside = document.getElementById('home-aside');
  const activeList = document.getElementById('home-active-list');
  const MAX_ACTIVE_SLOTS = 3;
  const displayActive = active.slice(0, MAX_ACTIVE_SLOTS);
  let activeHtml;
  if (displayActive.length === 0) {
    activeHtml = '<div class="active-empty active-empty-idle">無進行中任務</div>';
  } else {
    const emptySlots = MAX_ACTIVE_SLOTS - displayActive.length;
    activeHtml = displayActive.map(renderHomeActiveItem).join('')
      + Array(emptySlots).fill('<div class="active-empty">空位 · 最多 3 筆</div>').join('');
  }
  activeList.innerHTML = activeHtml;
  const headerLabel = document.getElementById('home-active-header-label');
  // 不顯示計數（0/3 或 N/3 都省略）— 卡片視覺自帶進度，標籤保留單純區塊標題
  if (headerLabel) headerLabel.textContent = '進行中';
  aside.classList.remove('hidden');

  // ── 搜尋下方：最近完成 2 筆 ──
  const recentSection = document.getElementById('home-recent-section');
  const recentList = document.getElementById('home-recent-list');
  const recentLabel = document.getElementById('home-recent-header-label');
  if (recentCompleted.length > 0) {
    recentList.innerHTML = recentCompleted.map(renderHomeRecentItem).join('');
    if (recentLabel) recentLabel.textContent = '最近完成';
    recentSection.classList.remove('hidden');
  } else {
    recentList.innerHTML = '';
    recentSection.classList.add('hidden');
  }

  // ── Update history view if open ──
  if (state.view === 'history') renderHistory();

  // ── Update task list card if open (legacy) ──
  const tlc = document.getElementById('tasklist-card');
  if (tlc && !tlc.classList.contains('hidden')) renderTaskListGrid();

  // Restart recovery banner 不在這裡 render — 由 handleRestartAutoRetry 決定：
  // 有 localStorage key → 背景自動 retry + toast；無 key / retry 失敗 → 才顯示 banner。
  // 這樣使用者 page load 時不會先看到 banner 再閃掉（「已自動重試」的路徑更順）。
}

// 抽出來給 handleRestartAutoRetry 和 renderRestartRecoveryBanner 共用
function collectRecentFailedAnalyses() {
  const tenMinAgo = Date.now() - 10 * 60 * 1000;
  const out = [];
  for (const t of state.tasks) {
    const analyses = t.analyses || [];
    // 每 task 只挑最近一個 failed 代表（避免同 task 多 analysis 重複算）
    const recentFailed = analyses
      .filter(a => a.status === 'failed' && !a.synthesis &&
                   a.created_at && new Date(a.created_at).getTime() > tenMinAgo)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    if (recentFailed) out.push({ task: t, analysis: recentFailed });
  }
  return out;
}

// Option C：loadHomeTasks 完時自動偵測中斷任務 + 自動重試（若有 localStorage key）。
// Flow：有 key → 背景靜默 retry + toast；無 key → fallback 到 banner 讓使用者手動。
// 同 analysis 只 auto-retry 一次：每個 analysis.id 記在 localStorage，避免「錯 key →
// retry fail → 律師刷新 → auto-retry fail → ...」的無限重試迴圈。
let _restartAutoRetryAttempted = false;
const AUTO_RETRY_STORAGE = 'autoRetriedAnalysisIds';

function _loadAutoRetriedIds() {
  try {
    const raw = localStorage.getItem(AUTO_RETRY_STORAGE);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch { return new Set(); }
}
function _markAutoRetried(analysisIds) {
  try {
    const s = _loadAutoRetriedIds();
    for (const id of analysisIds) s.add(id);
    // 上限 500 筆，超過就截尾（避免 localStorage 無限成長；老 id 早就過 10 分鐘窗口了）
    const arr = [...s].slice(-500);
    localStorage.setItem(AUTO_RETRY_STORAGE, JSON.stringify(arr));
  } catch {}
}

async function handleRestartAutoRetry() {
  if (_restartAutoRetryAttempted) return;
  const allFailed = collectRecentFailedAnalyses();
  if (allFailed.length === 0) return;
  _restartAutoRetryAttempted = true;

  // 排除已 auto-retry 過的（無論成敗）— 不要對同一個 analysis 重複 retry
  const retriedIds = _loadAutoRetriedIds();
  const failed = allFailed.filter(({ analysis }) => !retriedIds.has(analysis.id));
  if (failed.length === 0) {
    // 全部都已 auto-retry 過還是 failed → 走 banner 讓使用者自己判斷要不要再試
    renderRestartRecoveryBanner();
    return;
  }

  const hasKey = !!localStorage.getItem(KEY_STORAGE);
  if (!hasKey) {
    // 沒 key → fallback 到舊 banner，讓使用者自己點（可能先去設 key 再 retry）
    renderRestartRecoveryBanner();
    return;
  }

  // 有 key → 靜默批量 retry；無論成敗都記一次（下次刷新不會再 auto-retry 這批）
  _markAutoRetried(failed.map(f => f.analysis.id));
  const results = await Promise.allSettled(
    failed.map(async ({ task, analysis }) => {
      const res = await apiFetch(
        `/api/tasks/${task.id}/analyses/${analysis.id}/retry`, { method: 'POST' });
      if (!res.ok) throw new Error(await res.text().catch(() => res.statusText));
      // Sync bell 讓 home list 立刻變「進行中/排隊中」
      state.bell.tasks.set(task.id, {
        status: 'running', progress: 0, keyword: task.keyword || '',
        analysisId: analysis.id, unread: false, question: analysis.question || '',
      });
      subscribeBellTask(task.id);
      return { task, analysis };
    })
  );
  const ok = results.filter(r => r.status === 'fulfilled').length;
  const errCount = results.length - ok;
  // 重新拉 tasks（backend 已改 status），讓 UI 顯示最新狀態
  try {
    const res = await apiFetch(API.tasks);
    if (res.ok) state.tasks = await res.json();
  } catch {}
  renderNotificationBell();
  renderTaskLists();
  if (ok > 0) showHomeToast(`已自動重試 ${ok} 個中斷的任務${errCount ? `，${errCount} 個失敗` : ''}`);
  if (errCount > 0 && ok === 0) {
    // 全部失敗 → 落回 banner 讓使用者檢查
    renderRestartRecoveryBanner();
  }
}

// 首頁頂部淡出 toast（有別於 reader card 內的 _showReaderToast）
function showHomeToast(msg) {
  const existing = document.getElementById('home-toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.id = 'home-toast';
  toast.className = 'fixed top-4 left-1/2 -translate-x-1/2 z-50 px-4 py-2 ' +
    'bg-ink/90 text-warm-50 text-xs font-mono rounded-sm shadow-lg ' +
    'transition-opacity duration-300';
  toast.style.opacity = '0';
  toast.textContent = msg;
  document.body.appendChild(toast);
  requestAnimationFrame(() => { toast.style.opacity = '1'; });
  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

function renderRestartRecoveryBanner() {
  const banner = document.getElementById('home-restart-banner');
  if (!banner) return;
  const failedTasks = collectRecentFailedAnalyses();
  if (failedTasks.length === 0) {
    banner.classList.add('hidden');
    banner.innerHTML = '';
    return;
  }
  const n = failedTasks.length;
  banner.classList.remove('hidden');
  banner.innerHTML = `
    <div class="flex items-center gap-3 px-4 py-2.5 border border-amber-300 bg-amber-50/70 rounded-sm">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
           class="text-amber-600 shrink-0" aria-hidden="true">
        <path d="M21 12a9 9 0 11-3.75-7.3"/><polyline points="21 3 21 9 15 9"/>
      </svg>
      <div class="flex-1 text-xs font-serif text-amber-900 leading-relaxed">
        <b>${n} 個分析任務中斷</b>（可能是伺服器重啟）— 一鍵重試
      </div>
      <button onclick="batchRetryFailedAnalyses()" id="btn-batch-retry"
              class="px-3 py-1 text-xs font-mono border border-amber-500 text-amber-700
                     hover:bg-amber-100 transition-colors rounded-sm shrink-0">
        全部重試
      </button>
      <button onclick="dismissRestartBanner()" aria-label="關閉"
              class="text-amber-500 hover:text-amber-700 shrink-0 p-1" title="暫時隱藏">
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>`;
  banner._failedTasks = failedTasks;  // 傳給 handler，避免 re-query
}

function dismissRestartBanner() {
  const banner = document.getElementById('home-restart-banner');
  if (banner) { banner.classList.add('hidden'); banner.innerHTML = ''; banner._failedTasks = null; }
}

async function batchRetryFailedAnalyses() {
  const banner = document.getElementById('home-restart-banner');
  const failedTasks = banner?._failedTasks || [];
  if (failedTasks.length === 0) return;
  const btn = document.getElementById('btn-batch-retry');
  if (btn) { btn.disabled = true; btn.textContent = '重試中...'; }
  let ok = 0, failed = 0;
  for (const { task, analysis } of failedTasks) {
    try {
      const res = await apiFetch(`/api/tasks/${task.id}/analyses/${analysis.id}/retry`, { method: 'POST' });
      if (!res.ok) throw new Error(await res.text());
      // 同步 bell 狀態讓 home 立刻顯示「排隊中/進行中」
      state.bell.tasks.set(task.id, {
        status: 'running', progress: 0, keyword: task.keyword || '',
        analysisId: analysis.id, unread: false, question: analysis.question || '',
      });
      subscribeBellTask(task.id);
      ok += 1;
    } catch (err) {
      console.warn('Retry failed', task.id, err);
      failed += 1;
    }
  }
  // 重新載入 task list（DB status 已更新）
  try {
    const res = await apiFetch(API.tasks);
    if (res.ok) state.tasks = await res.json();
  } catch {}
  dismissRestartBanner();
  renderNotificationBell();
  renderTaskLists();
  if (failed > 0) {
    alert(`重試完成：成功 ${ok} 筆、失敗 ${failed} 筆`);
  }
}

const renderHomeTasks = renderTaskLists;

// ── Stop task from home queue ─────────────────────────
async function stopHomeTask(taskId, phase, event) {
  event.stopPropagation();
  if (phase === 'searching') {
    // Stage 1 搜尋中 → 刪除整個 task
    if (!confirm('停止搜尋將刪除此任務，確定？')) return;
    await deleteTask(taskId, event);
  } else {
    // Stage 3 分析中 → 中止分析，任務轉為待處理
    try {
      await apiFetch(`/api/tasks/${taskId}/fetch-judgments`, { method: 'DELETE' });
    } catch {}
    // 清除 bell tracking
    state.bell.tasks.delete(taskId);
    const bellSrc = state.bell.sseConnections.get(taskId);
    if (bellSrc) { bellSrc.close(); state.bell.sseConnections.delete(taskId); }
    renderNotificationBell();
    // Refresh tasks
    const res = await apiFetch(API.tasks);
    if (res.ok) state.tasks = await res.json();
    renderTaskLists();
  }
}

// ── Delete task from task list card ───────────────────
async function deleteTaskFromList(taskId, event) {
  event.stopPropagation();
  if (!confirm('確定刪除此任務？')) return;
  await deleteTask(taskId, event);
}

// ── Task list card ─────────────────────────────────
function openTaskListCard() {
  // Auto-select: 進行中 > 待處理 > 已完成
  const counts = { active: 0, pending: 0, completed: 0 };
  state.tasks.forEach(t => counts[getTaskCategory(t)]++);
  if (counts.active > 0) _tasklistFilter = 'active';
  else if (counts.pending > 0) _tasklistFilter = 'pending';
  else _tasklistFilter = 'completed';

  renderTaskListGrid();
  document.getElementById('tasklist-backdrop').classList.remove('hidden');
  document.getElementById('tasklist-card').classList.remove('hidden');
  lockBodyScroll();
}

function closeTaskListCard() {
  document.getElementById('tasklist-backdrop').classList.add('hidden');
  document.getElementById('tasklist-card').classList.add('hidden');
  unlockBodyScroll();
}

let _tasklistFilter = 'active';

function renderTaskListGrid() {
  const allSorted = [...state.tasks].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const filtered = allSorted.filter(t => getTaskCategory(t) === _tasklistFilter);

  const gridEl  = document.getElementById('tasklist-grid');
  const emptyEl = document.getElementById('tasklist-empty');
  const countEl = document.getElementById('tasklist-count');
  countEl.textContent = `${allSorted.length} 筆`;

  // Update filter button counts & active state
  const counts = { active: 0, pending: 0, completed: 0 };
  allSorted.forEach(t => counts[getTaskCategory(t)]++);
  document.querySelectorAll('.tasklist-filter').forEach(btn => {
    const f = btn.dataset.filter;
    btn.classList.toggle('active', f === _tasklistFilter);
    const labels = { active: '進行中', pending: '待處理', completed: '已完成' };
    btn.textContent = `${labels[f]} ${counts[f]}`;
  });

  if (filtered.length === 0) {
    gridEl.innerHTML = '';
    emptyEl.classList.remove('hidden');
  } else {
    gridEl.innerHTML = filtered.map(renderTaskGridCard).join('');
    emptyEl.classList.add('hidden');
  }
}

document.getElementById('tasklist-close').addEventListener('click', closeTaskListCard);
document.getElementById('tasklist-backdrop').addEventListener('click', closeTaskListCard);

// Filter buttons
document.querySelectorAll('.tasklist-filter').forEach(btn => {
  btn.addEventListener('click', () => {
    _tasklistFilter = btn.dataset.filter;
    renderTaskListGrid();
  });
});

function statusLabel(s) {
  return { pending:'等待中', running:'執行中', done:'完成', failed:'失敗' }[s] || s;
}
function statusColor(s) {
  return { pending:'text-warm-400', running:'text-amber-600', done:'text-emerald-600', failed:'text-red-600' }[s] || 'text-warm-400';
}

// ─── Search domain toggle (法院判決 ↔ 憲法解釋) ──────────────────────
// 預設 'judgment'；不記 localStorage、每次新頁面重置到預設
function applySearchDomain(domain) {
  document.querySelectorAll('.search-domain-btn').forEach(btn => {
    const active = btn.dataset.domain === domain;
    btn.setAttribute('aria-checked', active ? 'true' : 'false');
    // 設計版 pill：active 加 .on（CSS 控色），inactive 全由 .home-seg-btn 基礎樣式
    btn.classList.toggle('on', active);
  });
  const input = document.getElementById('main-search');
  if (input) input.placeholder = '輸入關鍵字';
  // 主文含（jud_jmain）只對 judgment 有意義；interpretation 模式遮起來但保留空間，
  // 讓右側「搜尋說明」按鈕位置不會隨切換跳動（visibility 保留佈局、display:none 會吃掉）
  const mainTextRow = document.getElementById('home-filter-row');
  if (mainTextRow) {
    if (domain === 'interpretation') {
      mainTextRow.style.visibility = 'hidden';
      mainTextRow.setAttribute('aria-hidden', 'true');
    } else {
      mainTextRow.style.visibility = '';
      mainTextRow.removeAttribute('aria-hidden');
    }
  }
}
document.querySelectorAll('.search-domain-btn').forEach(btn => {
  btn.addEventListener('click', () => applySearchDomain(btn.dataset.domain));
});

// ─── Search ───────────────────────────────────────
document.getElementById('btn-search').addEventListener('click', handleSearch);

// Search help card
document.getElementById('btn-search-help').addEventListener('click', () => {
  document.getElementById('search-help-backdrop').classList.remove('hidden');
  document.getElementById('search-help-card').classList.remove('hidden');
  lockBodyScroll();
});
document.getElementById('search-help-close').addEventListener('click', () => {
  document.getElementById('search-help-backdrop').classList.add('hidden');
  document.getElementById('search-help-card').classList.add('hidden');
  unlockBodyScroll();
});
document.getElementById('search-help-backdrop').addEventListener('click', () => {
  document.getElementById('search-help-backdrop').classList.add('hidden');
  document.getElementById('search-help-card').classList.add('hidden');
  unlockBodyScroll();
});
document.getElementById('main-search').addEventListener('keydown', e => {
  // 注音/中文輸入法選字也會觸發 Enter — 用 isComposing 或 keyCode 229 擋掉
  if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) handleSearch();
});
document.getElementById('main-text-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) handleSearch();
});

async function handleSearch() {
  const kw = document.getElementById('main-search').value.trim();
  if (!kw) { document.getElementById('main-search').focus(); return; }
  // 永遠展開（法條變體 + 已批准同義詞），不彈預覽 modal
  return runKeywordSearch(kw);
}

// 讀首頁 toggle 當前選擇的 search domain；預設 'judgment'
function getCurrentSearchDomain() {
  const active = document.querySelector('.search-domain-btn[aria-checked="true"]');
  return active?.dataset.domain || 'judgment';
}

// 讀「目前開啟中的 task」的 search_domain（reader / results 渲染時用）
function _task_search_domain() {
  const tid = state.card.taskId || state.currentTaskId;
  if (!tid) return 'judgment';
  const task = state.tasks.find(t => t.id === tid);
  return task?.search_domain || 'judgment';
}

function runKeywordSearch(keyword, opts = {}) {
  const domain = getCurrentSearchDomain();
  // 憲法解釋模式：沒 main_text / exhaustive / expand_keywords 語意
  const mainText = domain === 'interpretation'
    ? null
    : (document.getElementById('main-text-input')?.value.trim() || null);
  return createAndRunTask({
    keyword,
    search_domain: domain,
    expand_keywords: domain === 'judgment',
    exhaustive: domain === 'judgment',
    main_text: mainText,
    original_keyword: opts.originalKeyword || keyword,
  });
}

// ─── Search Plan Preview ──────────────────────────

// 暫存展開結果：律師確認後用於真正搜尋
// executor: (finalKeyword: string) => Promise — 由呼叫方提供，決定展開結果要如何拿去建立任務
//   keyword 模式傳 runKeywordSearch；semantic 模式傳 runSemanticTaskWithKeyword
// options:
//   semanticStrategy: 語意模式的策略物件（用於顯示 filter/ai_read fields 與隱藏「跳過展開」按鈕）
//   semanticQuery:    語意模式的律師原始自然語言輸入
let _expansionState = null;

async function showExpansionPreview(keyword, executor, options = {}) {
  const overlay = document.getElementById('expand-overlay');
  const content = document.getElementById('expand-content');
  content.innerHTML = '<div class="text-sm font-serif text-warm-400">準備搜尋計畫中…</div>';
  overlay.classList.remove('hidden');
  overlay.classList.add('flex');

  try {
    const res = await apiFetch(API.expandPreview, {
      method: 'POST',
      body: JSON.stringify({ keyword }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    _expansionState = {
      originalKeyword: keyword,
      keywords: data.keywords,
      executor,                                     // 確認/跳過時呼叫
      semanticStrategy: options.semanticStrategy || null,
      semanticQuery:    options.semanticQuery || null,
      // 預設 enabled：citation 全開；synonym 看 tier — confirmed 開、其他關
      enabled: data.keywords.map(k => {
        const metaArr = k.variant_metadata || [];
        return k.search_variants.map(v => {
          if (k.type !== 'synonym') return true;
          const meta = metaArr.find(m => m.variant === v);
          const tier = meta?.tier || 'confirmed';
          return tier === 'confirmed';
        });
      }),
    };
    // 語意模式隱藏「跳過展開」按鈕（語意模式本來就是律師需要 Claude 代為決策，不該跳過）
    document.getElementById('expand-skip').style.display =
      options.semanticStrategy ? 'none' : '';
    renderExpansionPreview();
  } catch (err) {
    content.innerHTML = `<div class="text-sm font-serif text-red-600">搜尋計畫準備失敗：${escHtml(err.message || err)}</div>`;
  }
}

function _closeExpandModal() {
  document.getElementById('expand-overlay').classList.add('hidden');
  document.getElementById('expand-overlay').classList.remove('flex');
}

// Tier → 顯示樣式 / 中文標籤
const TIER_META = {
  confirmed:   { label: '高信度',   chipCls: 'bg-seal/5 border-seal/30 text-ink',                    defaultOn: true  },
  candidate:   { label: '候選',     chipCls: 'bg-amber-50 border-amber-200 text-amber-700',            defaultOn: false },
  likely_typo: { label: '疑似錯字', chipCls: 'bg-warm-100 border-warm-200 text-warm-400',              defaultOn: false },
  rejected:    { label: '已拒絕',   chipCls: 'bg-red-50 border-red-200 text-red-600 line-through',     defaultOn: false },
};

function renderExpansionPreview() {
  const content = document.getElementById('expand-content');
  if (!_expansionState) return;

  const s = _expansionState;
  const isSemantic = !!s.semanticStrategy;

  // —— 計算指標：送 MCP 次數、filter 變體數 ——
  let totalSearchVariants = 0;   // 送 MCP 次數（每個 keyword 的 top-5 加總）
  let totalFilterVariants = 0;   // filter 用的變體總數
  s.keywords.forEach((k, kIdx) => {
    s.enabled[kIdx].forEach((on, vIdx) => { if (on) totalSearchVariants++; });
    totalFilterVariants += (k.filter_variants || k.search_variants).length;
  });

  // —— Section 1：律師意圖（兩階段流程下，stage 1 只決定關鍵字；
  //   filter_fields / ai_read_fields 移到 stage 3 modal 律師再選）——
  const question = isSemantic ? s.semanticQuery : s.originalKeyword;

  const intentHtml = `
    <div class="border border-warm-200 bg-warm-50/60 px-3 py-2.5 text-xs leading-relaxed">
      <div class="flex gap-2 mb-1"><span class="font-mono text-warm-400 w-16 shrink-0">關鍵字</span><span class="font-serif text-ink">${escHtml(question)}</span></div>
      <div class="text-warm-400 font-serif italic mt-1.5 leading-snug" style="font-size:11px">
        過濾欄位與 AI 分析欄位將在搜尋結果出來後、下指令分析時再選擇。
      </div>
    </div>
  `;

  // —— Section 2：第一輪 — MCP 搜尋（關鍵字展開 chips） ——
  const keywordBlocks = s.keywords.map((k, kIdx) => {
    const typeLabel = k.type === 'citation' ? '法條規則展開' : (k.from_cache ? 'AI 字典 cache' : 'AI 展開');
    const metaArr = k.variant_metadata || [];
    const chips = k.search_variants.map((v, vIdx) => {
      const meta = metaArr.find(m => m.variant === v);
      const tier = meta?.tier || 'confirmed';
      const tmeta = TIER_META[tier] || TIER_META.confirmed;
      const hits = meta?.corpus_hits;
      const enabled = s.enabled[kIdx][vIdx];
      const hitsBadge = (hits !== undefined && hits !== null)
        ? `<span class="ml-1 text-[10px] text-warm-400">(${hits === 0 ? '⚠ 0' : hits + (hits >= 50 ? '+' : '')})</span>`
        : '';
      const tierBadge = (k.type === 'synonym' && tier !== 'confirmed')
        ? `<span class="ml-1 text-[9px] font-mono px-1 rounded-sm bg-warm-200/60 border border-current">${tmeta.label}</span>`
        : '';
      const baseChipCls = enabled ? tmeta.chipCls : 'bg-warm-100 border-warm-200 text-warm-400 line-through';
      return `
        <span class="inline-flex items-center gap-1 px-2 py-0.5 mr-1 mb-1 border rounded-sm text-xs font-mono ${baseChipCls}"
              data-kidx="${kIdx}" data-vidx="${vIdx}">
          ${escHtml(v)}${hitsBadge}${tierBadge}
          <button class="ml-1 text-warm-400 hover:text-red-600 expand-chip-toggle" data-kidx="${kIdx}" data-vidx="${vIdx}" title="切換啟用/停用">${enabled ? '×' : '+'}</button>
        </span>`;
    }).join('');
    return `
      <div class="pl-3 border-l-2 border-seal/30 mb-2">
        <div class="flex items-baseline gap-2 mb-1">
          <span class="font-serif text-sm font-semibold text-ink">${escHtml(k.original)}</span>
          <span class="text-[10px] font-mono text-warm-400 uppercase tracking-wider">${typeLabel}</span>
        </div>
        <div class="flex flex-wrap">${chips || '<span class="text-xs text-warm-400">（此關鍵字無展開結果）</span>'}</div>
      </div>`;
  }).join('');

  const stage1Html = `
    <div>
      <div class="flex items-baseline gap-2 mb-2">
        <span class="font-mono text-[10px] text-seal bg-seal/10 px-1.5 py-0.5">第一輪</span>
        <span class="font-serif text-sm font-semibold">司法院全文檢索</span>
        <span class="text-xs text-warm-500 ml-auto">將送 <b class="text-ink">${totalSearchVariants}</b> 次 MCP 搜尋、合併去重</span>
      </div>
      ${keywordBlocks}
    </div>
  `;

  // —— Section 3：第二輪 — 過濾 ——
  const stage2Html = `
    <div class="border-t border-warm-200 pt-3">
      <div class="flex items-baseline gap-2 mb-1">
        <span class="font-mono text-[10px] text-seal bg-seal/10 px-1.5 py-0.5">第二輪</span>
        <span class="font-serif text-sm font-semibold">逐筆抓全文 + 字串過濾</span>
        <span class="text-xs text-warm-500 ml-auto">用 <b class="text-ink">${totalFilterVariants}</b> 種變體做字串比對</span>
      </div>
      <div class="text-xs text-warm-500 leading-relaxed pl-3 border-l-2 border-warm-200">
        在判決全文中比對上述任一變體字面，或做法條 tuple match（條號自動）
      </div>
    </div>
  `;

  // —— Section 4：第三輪 — Claude 分析 ——
  const stage3Html = `
    <div class="border-t border-warm-200 pt-3">
      <div class="flex items-baseline gap-2 mb-1">
        <span class="font-mono text-[10px] text-seal bg-seal/10 px-1.5 py-0.5">第三輪</span>
        <span class="font-serif text-sm font-semibold">Claude 分析</span>
        <span class="text-xs text-warm-500 ml-auto">依問題判斷每篇判決相關度 (1-10 分)</span>
      </div>
      <div class="text-xs text-warm-500 leading-relaxed pl-3 border-l-2 border-warm-200">
        讀理由、主文等欄位，回傳 match / score / excerpt / reason
      </div>
    </div>
  `;

  content.innerHTML = intentHtml + stage1Html + stage2Html + stage3Html;

  // 綁 chip toggle
  content.querySelectorAll('.expand-chip-toggle').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const k = parseInt(btn.dataset.kidx, 10);
      const v = parseInt(btn.dataset.vidx, 10);
      const wasEnabled = _expansionState.enabled[k][v];
      _expansionState.enabled[k][v] = !wasEnabled;
      // 回報 accept/reject 到字典（只對同義詞類型；citation 類型跳過）
      const kw = _expansionState.keywords[k];
      if (kw.type === 'synonym') {
        const variant = kw.search_variants[v];
        fetch(API.synonymFeedback, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            canonical: kw.original,
            variant,
            accepted: !wasEnabled,  // 切換後的新狀態
          }),
        }).catch(() => {/* silent */});
      }
      renderExpansionPreview();
    });
  });
}

document.getElementById('expand-close').addEventListener('click', _closeExpandModal);
document.getElementById('expand-cancel').addEventListener('click', _closeExpandModal);

// 「跳過展開，搜原字」：不用任何變體，直接用律師原始輸入送出
document.getElementById('expand-skip').addEventListener('click', () => {
  if (!_expansionState) return;
  const executor = _expansionState.executor;
  const original = _expansionState.originalKeyword;
  _closeExpandModal();
  // 暫關 hdr-expand 避免後端又跑一次展開（律師已明確拒絕展開）
  const prev = document.getElementById('hdr-expand').checked;
  document.getElementById('hdr-expand').checked = false;
  executor(original).finally(() => {
    document.getElementById('hdr-expand').checked = prev;
  });
});

// 確認：用勾選的 variants 組 keyword（空格連接）
document.getElementById('expand-confirm').addEventListener('click', () => {
  if (!_expansionState) return;
  const enabledVariants = [];
  _expansionState.keywords.forEach((k, kIdx) => {
    k.search_variants.forEach((v, vIdx) => {
      if (_expansionState.enabled[kIdx][vIdx]) enabledVariants.push(v);
    });
  });
  const finalKeyword = enabledVariants.join(' ') || _expansionState.originalKeyword;
  const originalKeyword = _expansionState.originalKeyword;
  const executor = _expansionState.executor;
  _closeExpandModal();
  // 已展開並挑選完，關閉 hdr-expand 避免後端重跑（送的是 variants 本身，不是原 keyword）
  const prev = document.getElementById('hdr-expand').checked;
  document.getElementById('hdr-expand').checked = false;
  executor(finalKeyword, { originalKeyword }).finally(() => {
    document.getElementById('hdr-expand').checked = prev;
  });
});

async function handleSemanticAnalyze() {
  const q = document.getElementById('semantic-search').value.trim();
  if (!q) { document.getElementById('semantic-search').focus(); return; }

  document.getElementById('btn-analyze-semantic').textContent = '分析中…';
  document.getElementById('btn-analyze-semantic').disabled = true;

  try {
    const res = await apiFetch(API.strategy, { method: 'POST', body: JSON.stringify({ query: q }) });
    if (!res.ok) throw new Error(await res.text());
    const { strategies } = await res.json();
    state.strategies = strategies;
    state.selectedStrategyIdx = 0;
    renderStrategyView(q, strategies);
    navTo({ view: 'strategy' });
  } catch (err) {
    alert(`策略分析失敗：${err.message}`);
  } finally {
    document.getElementById('btn-analyze-semantic').textContent = '分析語意';
    document.getElementById('btn-analyze-semantic').disabled = false;
  }
}

// ─── Strategy Selector ────────────────────────────
function renderStrategyView(query, strategies) {
  document.getElementById('strategy-query-display').textContent = query;
  const container = document.getElementById('strategy-cards');
  container.innerHTML = strategies.map((s, i) => `
    <div class="strategy-card border border-warm-200 hover:border-warm-400 cursor-pointer transition-colors
                ${i === 0 ? 'border-seal bg-seal/5' : 'bg-warm-50'} p-5"
         data-idx="${i}" onclick="selectStrategy(${i})">
      <div class="flex items-start gap-3 mb-3">
        <div class="w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0 mt-0.5
                    ${i === 0 ? 'border-seal' : 'border-warm-300'}">
          ${i === 0 ? '<div class="w-2 h-2 rounded-full bg-seal"></div>' : ''}
        </div>
        <div class="flex-1">
          <span class="font-serif text-sm font-semibold text-ink">${escHtml(s.name || `策略 ${i+1}`)}</span>
          <span class="ml-2 text-xs text-warm-400 font-serif">${escHtml(s.description || '')}</span>
        </div>
      </div>
      <div class="pl-7 flex flex-wrap gap-1.5 mb-2">
        ${(s.keywords || []).map(k => `
          <span class="font-mono text-xs bg-parchment border border-warm-200 px-2 py-0.5">${escHtml(k)}</span>
        `).join('')}
      </div>
      <div class="pl-7 flex items-center gap-4 text-xs text-warm-400 font-mono">
        <span>過濾：${(s.filter_fields||[]).join('、')}</span>
        <span>分析：${(s.ai_read_fields||[]).join('、')}</span>
        <span class="ml-auto text-warm-400">${escHtml(s.recall_estimate || '')}</span>
      </div>
    </div>
  `).join('');
}

function selectStrategy(idx) {
  state.selectedStrategyIdx = idx;
  document.querySelectorAll('.strategy-card').forEach((el, i) => {
    const active = i === idx;
    el.className = el.className.replace(/border-seal bg-seal\/5|border-warm-200/, active ? 'border-seal bg-seal/5' : 'border-warm-200');
    const dot = el.querySelector('.rounded-full.border-2');
    if (dot) {
      dot.className = dot.className.replace(/border-seal|border-warm-300/, active ? 'border-seal' : 'border-warm-300');
      dot.innerHTML = active ? '<div class="w-2 h-2 rounded-full bg-seal"></div>' : '';
    }
  });
}

document.getElementById('btn-strategy-back').addEventListener('click', () => navTo({view:'home'}));
document.getElementById('btn-strategy-exec').addEventListener('click', async () => {
  const s = state.strategies[state.selectedStrategyIdx];
  if (!s) return;
  const q = document.getElementById('strategy-query-display').textContent;
  const combinedKeyword = (s.keywords || []).join(' ');
  if (!combinedKeyword) return;

  // 兩階段流程下，semantic 策略只負責 NL → keyword 提案；
  // filter_fields / question 的決策延後到 stage 3 (analyses 端點)。
  // 暫時把策略選的 keywords 丟進新 stage 1 流程；之後 step 7+ 再給 semantic 專屬 UI。
  const runSemantic = (finalKeyword) => createAndRunTask({
    keyword: finalKeyword,
    expand_keywords: false,   // 律師已透過 expansion modal 確認 variants
    exhaustive: true,
  });

  // 語意模式**一定要**顯示搜尋計畫 — Claude 替律師做了多個決策
  // （關鍵字選擇、過濾欄位、AI分析欄位），律師要透明看過才送出。
  // 忽略 hdr-expand 狀態；本路徑無「跳過展開」捷徑。
  await showExpansionPreview(combinedKeyword, runSemantic, {
    semanticStrategy: s,
    semanticQuery: q,
  });
});

// ─── Create Task (兩階段) ─────────────────────────
// 新 POST /tasks 只觸發 stage 1（廣搜）：立刻回 task_id，前端訂閱 SSE 等 stage1_done。
// Stage 3 分析 (analyses 端點) 由 stage 2 view 的「AI 分析」按鈕啟動，不再首頁建立。
async function createAndRunTask(params) {
  try {
    const res = await apiFetch(API.tasks, { method: 'POST', body: JSON.stringify(params) });
    if (!res.ok) throw new Error(await res.text());
    const { task_id } = await res.json();

    state.currentTaskId     = task_id;
    state.primaryAnalysisId = null;
    state.analyses          = [];
    state.judgments         = [];
    state.hits              = [];
    state.stage2 = {
      selectedTiers: new Set(),
      yearFrom: null, yearTo: null, yearMin: null, yearMax: null,
    };

    // 同步 task 清單
    const tasksRes = await apiFetch(API.tasks);
    state.tasks = tasksRes.ok ? await tasksRes.json() : state.tasks;
    renderTaskLists();

    // 先訂閱 SSE（避免 worker 在 POST 回應後、訂閱前搶先 publish 導致漏事件）
    state.card.taskId = task_id;
    state.card.searching = true;
    subscribeTaskForCard(task_id);

    // 再開卡片，顯示搜尋中狀態
    openSearchCard(task_id, 'a');
  } catch (err) {
    alert(`搜尋失敗：${err.message}`);
  }
}

// Stage 1 專用 SSE：只追蹤 stage1_progress + stage1_done，完成後開卡片
function subscribeTaskForCard(taskId) {
  if (state.sse) state.sse.close();
  const src = new EventSource(API.stream(taskId));
  state.sse = src;

  // SSE 連線建立後：立刻抓一次目前的 hits，補上連線前可能漏掉的事件
  src.addEventListener('open', async () => {
    try {
      const res = await apiFetch(API.task(taskId));
      if (!res.ok) return;
      const task = await res.json();

      // 刷新首頁任務清單（不管 task 狀態，確保首頁顯示最新）
      const tasksRes = await apiFetch(API.tasks);
      if (tasksRes.ok) { state.tasks = await tasksRes.json(); renderTaskLists(); }

      // task 已完成（SSE 訂閱前搜尋就跑完了）→ 直接載入結果，關閉 SSE
      if (task.status === 'done') {
        await loadStage2Hits(false);
        state.card.searching = false;
        if (state.card.open && state.card.taskId === taskId) setCardState('a');
        src.close(); state.sse = null;
        return;
      }

      // 搜尋中：同步目前已有的 hits 到 header + 列表
      if ((task.hits_total || 0) > state.hits.length) {
        const hitsRes = await apiFetch(`${API.task(taskId)}/hits`);
        if (hitsRes.ok) state.hits = await hitsRes.json();
        const countEl = document.getElementById('card-search-count');
        if (countEl) countEl.textContent = state.hits.length.toLocaleString();
        renderTaskLists();
      }
    } catch {}
  });

  src.addEventListener('stage1_progress', sseHandler(e => {
    const d = JSON.parse(e.data);
    // Append new hits
    const existing = new Set(state.hits.map(h => h.case_id));
    for (const h of (d.new_hits || [])) {
      if (!existing.has(h.case_id)) state.hits.push(h);
    }
    // 即時更新卡片（如果已開啟）
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'a') {
      // 更新 header 搜尋筆數
      const countEl = document.getElementById('card-search-count');
      if (countEl) countEl.textContent = state.hits.length.toLocaleString();
      // 即時重繪 chips / slider / preview
      renderCardTierChips();
      setupCardYearSlider();
      updateCardFilteredCount();
      renderCardHitPreview();
      updateCardCostHint();
    }
    // 即時更新首頁任務列（卡片關閉時也能看到進度）
    renderTaskLists();
  }));

  src.addEventListener('stage1_done', sseHandler(async e => {
    const d = JSON.parse(e.data);
    src.close();
    state.sse = null;

    // Sync hits if needed
    if (state.hits.length !== d.hits_total) {
      await loadStage2Hits(false);
    }

    // Refresh tasks
    const tasksRes = await apiFetch(API.tasks);
    if (tasksRes.ok) state.tasks = await tasksRes.json();

    // 鈴鐺通知：搜尋完成，請設定分析範圍
    const task = state.tasks.find(t => t.id === taskId);
    state.bell.tasks.set(taskId, {
      status: 'ready',
      progress: 100,
      keyword: task ? getTaskOrigKw(task) : '',
      unread: true,
      question: '',
    });
    renderNotificationBell();
    renderTaskLists();

    // 結束搜尋狀態 → 重繪卡片 header（去掉 pulse dot）
    state.card.searching = false;
    state.card.searchWarnings = d.warnings || [];
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'a') {
      state.stage2 = {
        selectedTiers: new Set(),
        yearFrom: null, yearTo: null, yearMin: null, yearMax: null,
      };
      setCardState('a');  // re-render with final state
    }
  }));

  // Stage 1 失敗 — 搜尋過程發生錯誤
  src.addEventListener('stage1_failed', sseHandler(e => {
    const d = JSON.parse(e.data);
    state.card.searching = false;
    if (state.card.open && state.card.taskId === d.task_id) {
      const hdr = document.getElementById('card-header-text');
      hdr.innerHTML = `<span class="text-red-600">搜尋失敗</span>`;
      document.getElementById('card-search-bar').classList.add('hidden');
      setCardState('a');
    }
    // 更新首頁任務狀態
    const t = state.tasks.find(x => x.id === d.task_id);
    if (t) t.status = 'failed';
    renderTaskLists();
  }));

  src.onerror = () => {
    src.close();
    state.sse = null;
    // 復原 searching flag，避免 AI 分析按鈕永遠 disabled
    if (state.card.searching) {
      state.card.searching = false;
      if (state.card.open && state.card.state === 'a') setCardState('a');
      renderTaskLists();
    }
  };
}

// ─── SSE ─────────────────────────────────────────
// analysisId 為 null 代表訂閱 stage 1（沒有特定 analysis 要過濾 batch_done）；
// 有值代表訂閱 stage 3 的某個 analysis（會用來 filter batch_done 事件）。
// 確保 SSE 已連線且 readyState=OPEN。POST 前呼叫，避免 worker 比 SSE 訂閱早 publish 導致事件丟失。
async function ensureSseSubscribed(taskId) {
  if (state.sse && state.sse.readyState === EventSource.OPEN) return;
  if (state.sse) { try { state.sse.close(); } catch {} state.sse = null; }
  subscribeTask(taskId);
  await new Promise(resolve => {
    if (!state.sse) return resolve();
    if (state.sse.readyState === EventSource.OPEN) return resolve();
    const onOpen = () => { state.sse?.removeEventListener('open', onOpen); resolve(); };
    state.sse.addEventListener('open', onOpen);
    setTimeout(resolve, 3000);   // 安全 timeout
  });
}

function subscribeTask(taskId, analysisId = null) {
  if (state.sse) state.sse.close();

  const src = new EventSource(API.stream(taskId));
  state.sse = src;




  // Stage 1 進度：每 round 完成就 append 新 hits，列表筆數即時往上跳
  src.addEventListener('stage1_progress', sseHandler(async e => {
    const d = JSON.parse(e.data);
    setResultsLoading(null);
    // 第一次收到 progress：可能還在 loading 模式 → 切到 stage 2 view
    // 也順便初始化 state.stage2 篩選條件（loadStage2Hits 會做，這裡精簡：直接設 state）
    if (state.view === 'results' && state.hits.length === 0) {
      state.stage2 = {
        selectedTiers:  new Set(),
        yearFrom: null, yearTo: null, yearMin: null, yearMax: null,
      };
      setStageMode('stage2');
    }
    // Append 新 hits（dedupe 防呆，雖然後端已 INSERT OR IGNORE）
    const existing = new Set(state.hits.map(h => h.case_id));
    for (const h of (d.new_hits || [])) {
      if (!existing.has(h.case_id)) state.hits.push(h);
    }
    renderStage2();   // 重算 filter chip counts、年度拉桿範圍、列表
  }));

  // Stage 1 完成：truncated 提示 + 更新 task 清單 hits_total + 收掉「搜尋中…」indicator
  src.addEventListener('stage1_done', sseHandler(async e => {
    const d = JSON.parse(e.data);
    setResultsLoading(null);
    if (d.truncated) {
      console.warn(`Stage 1 結果超過 ${d.hits_total} 筆已截斷`);
    }
    const tasksRes = await apiFetch(API.tasks);
    if (tasksRes.ok) state.tasks = await tasksRes.json();
    updateTaskDropdownLabel();
    renderTaskDropdown();
    renderTaskLists();
    // 保險：若中途有 stage1_progress event 漏收，收尾時補 sync 整份 hits
    if (state.hits.length !== d.hits_total) {
      await loadStage2Hits();
    } else {
      // hits 已 sync，但 task.status 從 running 翻 done → 重渲染收掉「搜尋中…」indicator
      renderStage2();
    }
  }));

  src.addEventListener('filter_progress', sseHandler(e => {
    const d = JSON.parse(e.data);
    const pct = d.total > 0 ? Math.round((d.fetched / d.total) * 100) : 0;
    setResultsLoading(`Stage 3 抓取判決全文 ${d.fetched} / ${d.total}（${pct}%），已通過過濾 ${d.passed_so_far} 筆…`);
  }));

  src.addEventListener('judgments_ready', sseHandler(e => {
    const d = JSON.parse(e.data);
    setResultsLoading(`過濾完成：${d.after_filter} 筆通過，開始 AI 分析…`);
  }));

  src.addEventListener('batch_done', sseHandler(e => {
    const d = JSON.parse(e.data);
    // 沒有指定 analysisId 就接受所有 batch_done（stage 3 由 analyses 端點啟動的會自動帶 id）
    if ((!analysisId || d.analysis_id === analysisId) && Array.isArray(d.results)) {
      state.judgments.push(...d.results.map(r => ({ ...r, _batch: true })));
      renderJudgmentList(filteredJudgments());
    }
    updateAnalysisTabProgress(d.analysis_id, d.completed, d.total);
    const twoPass = d.total >= 40;
    const doneJudg = twoPass ? Math.floor(d.completed / 2) : d.completed;
    const totalJudg = twoPass ? Math.floor(d.total / 2) : d.total;
    const matchPart = (typeof d.match_count === 'number') ? `（命中 ${d.match_count}）` : '';
    setResultsLoading(`AI 分析中 ${doneJudg} / ${totalJudg} 筆${matchPart}…`);
  }));

  src.addEventListener('analysis_done', sseHandler(e => {
    const d = JSON.parse(e.data);
    updateAnalysisTabProgress(d.analysis_id, null, null, d.match_count, 'done');
  }));

  // Analysis failed — 顯示錯誤狀態 + retry 按鈕
  src.addEventListener('analysis_failed', sseHandler(async e => {
    const d = JSON.parse(e.data);
    const a = state.analyses.find(x => x.id === d.analysis_id);
    if (a) a.status = 'failed';
    // Card State B 進度畫面 → 切到錯誤狀態
    if (state.card.open && state.card.analysisId === d.analysis_id && state.card.state === 'b') {
      renderCardError(d.error || '分析過程中發生錯誤');
    }
    // Bell 更新
    const bellInfo = state.bell.tasks.get(d.task_id);
    if (bellInfo && bellInfo.analysisId === d.analysis_id) {
      bellInfo.status = 'failed';
      renderNotificationBell();
    }
    // State C 的 analysis history tabs 也要刷新
    if (state.card.open && state.card.state === 'c') {
      renderAnalysisHistoryTabs();
    }
    await reloadAnalyses();
  }));

  // Stage 3 v2 synthesis 開始 — UI 顯示「摘要生成中」
  src.addEventListener('stage3_synthesis_start', sseHandler(e => {
    const d = JSON.parse(e.data);
    if (state.primaryAnalysisId === d.analysis_id) {
      // 若已在 stage 3 view，重繪一次把 header 改「分析中」狀態
      if (state.view === 'results') loadStage3View(d.analysis_id);
    }
  }));

  // Stage 3 v2 synthesis 完成 — 自動切到 stage 3 results view
  src.addEventListener('stage3_synthesis_done', sseHandler(async e => {
    const d = JSON.parse(e.data);
    setResultsLoading(null);
    // 確保 state.analyses 帶有最新的 synthesis 欄位
    await reloadAnalyses();
    await loadStage3View(d.analysis_id);
  }));

  // 理由預篩 SSE — 不再用 state.card.reasoningFilter 當 guard
  // （async openTask 期間 reset 把 flag 清為 false、restorePrefilterFromDb 再設回
  // true 之間有 race 視窗，舊設計下這期間的 SSE 會被擋掉）。現在後端只在 ownership
  // match 時才推 event，信任事件本身 → 只驗 taskId 一致就處理。
  src.addEventListener('reasoning_prefilter_progress', sseHandler(e => {
    const d = JSON.parse(e.data);
    if (state.card.open && state.card.taskId === d.task_id) {
      state.card.prefilterFetched = d.fetched;
      state.card.prefilterMatched = d.matched;
      state.card.prefilterCaseIds = d.matched_case_ids;
      state.card.prefilterRunning = true;
      state.card.reasoningFilter = true;  // 事件到達 = 有在跑 = 按鈕該是 active
      _syncReasoningToggleButtonUI(true);
      const statusEl = document.getElementById('card-reasoning-status');
      statusEl.classList.remove('hidden');
      const reusedNote = d.reused ? ` · 其中 ${d.reused} 筆快取複用` : '';
      statusEl.innerHTML = `<span class="pulse-dot w-1 h-1 rounded-full bg-seal inline-block"></span> 下載全文中 ${d.fetched}/${d.total} · 目前命中 ${d.matched} 筆${reusedNote}`;
      document.getElementById('card-filtered-count').textContent = d.matched.toLocaleString();
      updateCardCostHint();
    }
  }));

  src.addEventListener('reasoning_prefilter_done', sseHandler(e => {
    const d = JSON.parse(e.data);
    if (state.card.open && state.card.taskId === d.task_id) {
      state.card.prefilterRunning = false;
      state.card.prefilterCaseIds = d.matched_case_ids;
      state.card.prefilterMatched = d.matched;
      const statusEl = document.getElementById('card-reasoning-status');
      statusEl.classList.remove('hidden');
      const reusedNote = d.reused ? `，快取複用 ${d.reused} 筆` : '';
      statusEl.textContent = `完成 · ${d.matched} 筆命中（共下載 ${d.total} 筆全文${reusedNote}）`;
      document.getElementById('card-filtered-count').textContent = d.matched.toLocaleString();
      updateCardCostHint();
    }
  }));

  src.addEventListener('reasoning_prefilter_cancelled', sseHandler(e => {
    const d = JSON.parse(e.data);
    if (state.card.open && state.card.taskId === d.task_id) {
      state.card.prefilterRunning = false;
      document.getElementById('card-reasoning-status').classList.add('hidden');
      state.card.reasoningFilter = false;
      _syncReasoningToggleButtonUI(false);
      updateCardFilteredCount();
      updateCardCostHint();
    }
  }));

  src.addEventListener('task_done', sseHandler(async () => {
    setResultsLoading(null);
    src.close();
    // Reload full judgment list from API（only if we have a primary analysis）
    if (state.primaryAnalysisId) await reloadJudgments();
    await reloadAnalyses();
    // Refresh task list in both views
    const tasksRes = await apiFetch(API.tasks);
    if (tasksRes.ok) state.tasks = await tasksRes.json();
    updateTaskDropdownLabel();
    renderTaskDropdown();
    renderTaskLists();
  }));

  src.onerror = () => {
    setResultsLoading(null);
    src.close();
  };
}

async function reloadJudgments() {
  if (!state.currentTaskId || !state.primaryAnalysisId) return;
  try {
    const url = API.judgments(state.currentTaskId)
      + `?primary_analysis_id=${state.primaryAnalysisId}`
      + (state.secondaryAnalysisId ? `&secondary_analysis_id=${state.secondaryAnalysisId}` : '')
      + (state.filters.minScore ? `&min_score=${state.filters.minScore}` : '')
      + (state.filters.matchType ? `&match=${state.filters.matchType}` : '');
    const res = await apiFetch(url);
    if (!res.ok) { console.warn('reloadJudgments failed:', res.status); return; }
    state.judgments = await res.json();
    renderJudgmentList(filteredJudgments());
  } catch (err) {
    console.error('reloadJudgments error:', err);
  }
}

async function reloadAnalyses() {
  if (!state.currentTaskId) return;
  try {
    const res = await apiFetch(API.analyses(state.currentTaskId));
    if (!res.ok) return;
    state.analyses = await res.json();
    renderAnalysisTabs(state.analyses);
  } catch {}
}

// ─── Open existing task ───────────────────────────
async function openTask(taskId) {
  state.currentTaskId = taskId;
  state.hits = [];
  state.judgments = [];

  try {
    const taskRes = await apiFetch(API.task(taskId));
    if (!taskRes.ok) throw new Error();
    const task = await taskRes.json();
    state.analyses = task.analyses || [];
    state.primaryAnalysisId = state.analyses.find(a => a.status === 'done')?.id
      || state.analyses[0]?.id || null;
    state.factsCoverage = task.facts_coverage || { total: 0, with_facts: 0 };
    applyFactsCoverageHint();

    const hasHits = (task.hits_total || 0) > 0;
    const hasAnalyses = state.analyses.length > 0;
    // 取最新的已完成 v2 analysis（最後建立的）
    const v2Analysis = [...state.analyses].reverse().find(a => a.synthesis);
    const runningAnalysis = state.analyses.find(a => a.status === 'running' || a.status === 'pending');
    // 最近一次失敗的 analysis（用在「bell 說 running 但 DB 已經 failed」的 case）。
    // 之前這個 case 會掉到 State A 的篩選頁，有「開始分析」按鈕會引誘使用者再 fire
    // 一次；現在導向 State B + renderCardError，讓使用者看到「上次失敗，可重試」。
    const failedAnalysis = [...state.analyses].reverse().find(a => a.status === 'failed');

    // 清 stale bell：bell 說 running 但 DB 沒有 running/pending 的 analysis → bell
    // 是從前一次 session 殘留（task 已經 failed / done 但 SSE close event 沒收到）。
    // 不清掉 getTaskPhase 會永遠回 'analyzing'、home 永遠顯示「進行中」。
    const _bellInfo = state.bell.tasks.get(taskId);
    if (_bellInfo?.status === 'running' && !runningAnalysis) {
      state.bell.tasks.delete(taskId);
      const _src = state.bell.sseConnections.get(taskId);
      if (_src) { _src.close(); state.bell.sseConnections.delete(taskId); }
      renderNotificationBell();
    }

    // ── 搜尋仍在進行中（no hits yet）→ 開卡片 + 訂閱 SSE ──
    const taskActive = (task.status === 'running' || task.status === 'pending');
    if (!hasHits && taskActive) {
      state.card.searching = true;
      openSearchCard(taskId, 'a');
      subscribeTaskForCard(taskId);
      return;
    }

    // ── New-flow tasks (with hits) → use card ──
    if (hasHits) {
      // 已有 hits = Stage 1 至少已完成；清除可能殘留的搜尋狀態
      if (!taskActive) state.card.searching = false;
      await loadStage2Hits(false);  // data only, don't render old stage2 view
      // 恢復上次的 stage2 filter（從 localStorage per task）— 律師跳出卡片再回來
      // 不會丟失勾選的 tier / 拉過的 year 範圍
      state.stage2 = loadStage2FilterFromStorage(taskId);

      if (runningAnalysis && v2Analysis) {
        // 有正在跑的分析 + 有前次完成的結果 → State C + loading tab
        subscribeBellTask(taskId);
        const existingBell = state.bell.tasks.get(taskId);
        if (!existingBell) {
          // 從 DB 的 completed/total 推算進度（server 重啟後 bell 清空時用）
          const completed = runningAnalysis.completed || 0;
          const total = runningAnalysis.total || 0;
          const estProgress = total > 0 ? Math.round(33 + (completed / total) * 57) : 0;
          const estPhase = completed > 0 ? 'read' : (total > 0 ? 'fetch' : '');
          state.bell.tasks.set(taskId, {
            status: 'running', progress: estProgress, progressPhase: estPhase,
            keyword: task.keyword || '',
            analysisId: runningAnalysis.id, unread: false, question: runningAnalysis.question || '',
          });
        }
        renderNotificationBell();
        state.card.analysisId = v2Analysis.id;
        openSearchCard(taskId, 'c');
        await renderCardResults(v2Analysis.id);
        renderAnalysisHistoryTabs();
      } else if (runningAnalysis) {
        // 只有正在跑的分析（無前次結果）→ State B
        // 先初始化 bell（從 DB completed/total 推算），再 render progress
        const existingBell = state.bell.tasks.get(taskId);
        if (!existingBell) {
          const completed = runningAnalysis.completed || 0;
          const total = runningAnalysis.total || 0;
          const estProgress = total > 0 ? Math.round(33 + (completed / total) * 57) : 0;
          const estPhase = completed > 0 ? 'read' : (total > 0 ? 'fetch' : '');
          state.bell.tasks.set(taskId, {
            status: 'running', progress: estProgress, progressPhase: estPhase,
            keyword: task.keyword || '',
            analysisId: runningAnalysis.id, unread: false, question: runningAnalysis.question || '',
          });
        }
        subscribeBellTask(taskId);
        state.card.analysisId = runningAnalysis.id;
        openSearchCard(taskId, 'b');
        renderCardProgress(runningAnalysis.question || '');
        renderNotificationBell();
      } else if (v2Analysis) {
        // 有完成的結果（無正在跑的）→ State C
        state.card.analysisId = v2Analysis.id;
        openSearchCard(taskId, 'c');
        await renderCardResults(v2Analysis.id);
      } else if (failedAnalysis) {
        // 有失敗的 analysis 但沒有跑完或在跑的 → 導 State B + error 訊息 + 重試按鈕。
        // 比 State A 安全：State A 的「開始分析」按鈕長得就像「開新的」，使用者會意外
        // 又 fire 一次，造成 API key 重新消耗 + worker queue 塞車。
        state.card.analysisId = failedAnalysis.id;
        openSearchCard(taskId, 'b');
        renderCardProgress(failedAnalysis.question || '');
        renderCardError('上次分析未完成（可能是伺服器重啟或 Claude API 失敗）');
      } else {
        // Stage 1 done, 完全無任何 analysis（含失敗的）→ State A（全新任務的起點）
        openSearchCard(taskId, 'a');
      }

      // Subscribe SSE if task/analysis still active
      if (taskActive) {
        subscribeTaskForCard(taskId);
      }
      return;
    }

    // ── Legacy tasks (no hits) → old results view ──
    showView('results');
    setResultsLoading('載入任務…');
    setStageMode('loading');
    updateTaskDropdownLabel();
    renderTaskDropdown();
    renderAnalysisTabs(state.analyses);

    if (hasAnalyses) {
      setStageMode('legacy');
      if (state.primaryAnalysisId) await reloadJudgments();
      setResultsLoading(null);
    } else {
      if (task.status === 'running' || task.status === 'pending') {
        setStageMode('loading');
        setResultsLoading('Stage 1 廣搜進行中…');
      } else {
        setStageMode('legacy');
        setResultsLoading(null);
      }
    }

    if (taskActive || runningAnalysis) {
      subscribeTask(taskId, runningAnalysis ? runningAnalysis.id : null);
    }
  } catch (err) {
    console.error('openTask 失敗', err);
    // 最常見原因：uvicorn 掛了（TypeError: Failed to fetch）或後端 5xx。
    // 沒 toast 的話 onclick 靜默無反應，使用者會以為前端壞了。
    const isNetwork = err instanceof TypeError;
    showHomeToast(isNetwork
      ? '連不上後端伺服器，請重啟伺服器或聯絡系統管理員'
      : '載入任務失敗，請重啟伺服器或聯絡系統管理員');
  }
}

// ─── Task Dropdown (results view) ────────────────
function renderTaskDropdown() {
  const menu = document.getElementById('task-dd-menu');
  if (!menu) return;
  menu.innerHTML = state.tasks.length
    ? state.tasks.map((t, i) => {
        const isActive  = t.id === state.currentTaskId;
        const isRunning = t.status === 'running' || t.status === 'pending';
        const dot = isRunning
          ? `<span class="pulse-dot w-1.5 h-1.5 rounded-full bg-amber-500 inline-block shrink-0"></span>`
          : t.status === 'done'
          ? `<span class="w-1.5 h-1.5 rounded-full bg-emerald-500 inline-block shrink-0"></span>`
          : `<span class="w-1.5 h-1.5 rounded-full bg-red-400 inline-block shrink-0"></span>`;
        return `
          <div class="flex items-center gap-2.5 px-3 py-2.5 cursor-pointer hover:bg-warm-100 transition-colors
                      ${isActive ? 'bg-warm-50' : ''}"
               onclick="navTo({view:'results', taskId:'${t.id}'}); closeAllDropdowns()">
            ${dot}
            <div class="flex-1 min-w-0">
              <div class="font-serif text-sm text-ink truncate">${escHtml(t.keyword)}</div>
              <div class="font-mono text-[10px] text-warm-400">#${state.tasks.length - i} · ${new Date(t.created_at).toLocaleDateString('zh-TW')}</div>
            </div>
            ${isActive ? '<span class="text-seal text-xs shrink-0">✓</span>' : ''}
          </div>`;
      }).join('')
    : '<div class="px-3 py-3 text-xs font-mono text-warm-400">尚無任務</div>';
}

function updateTaskDropdownLabel() {
  const t = state.tasks.find(t => t.id === state.currentTaskId);
  const labelEl  = document.getElementById('task-dd-label');
  const statusEl = document.getElementById('task-dd-status');
  if (!labelEl || !statusEl) return;
  if (!t) { labelEl.textContent = '—'; statusEl.innerHTML = ''; return; }
  const kw = t.keyword.length > 40 ? t.keyword.slice(0, 40) + '…' : t.keyword;
  labelEl.textContent = kw;
  const isRunning = t.status === 'running' || t.status === 'pending';
  statusEl.innerHTML = isRunning
    ? `<span class="pulse-dot w-1.5 h-1.5 rounded-full bg-amber-500 inline-block ml-1"></span>`
    : '';
}

// Results 「← 返回」 button — 走 history.back() 讓使用者回到先前的 view（home 或 history）
document.getElementById('results-back-btn').addEventListener('click', () => {
  // 若沒有 history entry（例如直接深連結進到 results），就主動 nav 回首頁
  if (history.length > 1) {
    history.back();
  } else {
    navTo({ view: 'home' });
  }
});

// Task dropdown toggle
document.getElementById('task-dd-btn').addEventListener('click', e => {
  e.stopPropagation();
  const menu = document.getElementById('task-dd-menu');
  const open = !menu.classList.contains('hidden');
  closeAllDropdowns();
  if (!open) {
    renderTaskDropdown();
    menu.classList.remove('hidden');
  }
});

// ─── Analysis Tabs ────────────────────────────────
function renderAnalysisTabs(analyses) {
  state.analyses = analyses;
  const container = document.getElementById('analysis-tabs');
  container.innerHTML = analyses.map((a, i) => {
    const isPrimary = a.id === state.primaryAnalysisId;
    const badge = a.status === 'running'
      ? `<span class="ml-1.5 font-mono text-xs bg-amber-50 text-amber-700 px-1.5 py-0.5 inline-flex items-center gap-1">
           <span class="pulse-dot w-1 h-1 rounded-full bg-amber-500 inline-block"></span>
           ${a.completed ?? 0}筆
         </span>`
      : `<span class="ml-1.5 font-mono text-xs ${isPrimary ? 'bg-seal/20 text-seal' : 'bg-warm-200 text-warm-400'} px-1.5 py-0.5">
           ${a.match_count ?? 0} 筆
         </span>`;
    const tipText = a.question ? escAttr(a.question) : '';
    return `
      <button class="analysis-tab ${isPrimary ? 'active' : 'text-warm-400'} text-sm font-serif pb-2.5 pt-3"
              data-id="${a.id}" onclick="switchPrimaryAnalysis('${a.id}')"
              title="${tipText}">
        分析&nbsp;<span class="font-mono">#${i+1}</span>${badge}
      </button>
    `;
  }).join('');
}

function updateAnalysisTabProgress(analysisId, completed, total, matchCount, status) {
  const a = state.analyses.find(x => x.id === analysisId);
  if (!a) return;
  if (completed !== null && completed !== undefined) a.completed = completed;
  if (total !== null && total !== undefined) a.total = total;
  if (matchCount !== undefined) a.match_count = matchCount;
  if (status) a.status = status;
  renderAnalysisTabs(state.analyses);
}

async function switchPrimaryAnalysis(id) {
  state.primaryAnalysisId = id;
  renderAnalysisTabs(state.analyses);
  await reloadJudgments();
}

// ─── Stage 2 (廣搜瀏覽) ───────────────────────────
// 兩階段流程的中介 view：列出 task_search_hits + 互動篩選器，
// 律師 narrow 後可開單筆 reader 即時讀，或按「AI 分析」開 stage 3 modal（step 6 實作）。

function setStageMode(mode) {
  // mode: 'stage2' | 'stage3' | 'legacy' | 'loading'
  const stage2El = document.getElementById('view-stage2');
  const stage3El = document.getElementById('view-stage3');
  const legacyEl = document.getElementById('legacy-results-body');

  const hideAll = () => {
    stage2El.classList.add('hidden');  stage2El.classList.remove('flex');
    stage3El.classList.add('hidden');  stage3El.classList.remove('flex');
    legacyEl.classList.add('hidden');
  };
  hideAll();

  if (mode === 'stage2') {
    stage2El.classList.remove('hidden');
    stage2El.classList.add('flex');
  } else if (mode === 'stage3') {
    stage3El.classList.remove('hidden');
    stage3El.classList.add('flex');
  } else {
    // 'legacy' 或 'loading' 顯示 legacy 區塊（含 results-loading）
    legacyEl.classList.remove('hidden');
  }
}

async function loadStage2Hits(renderView = true) {
  if (!state.currentTaskId) return;
  try {
    const res = await apiFetch(`/api/tasks/${state.currentTaskId}/hits`);
    if (!res.ok) throw new Error(res.status);
    state.hits = await res.json();
    // 重置 stage 2 篩選 state（換 task 時不要繼承上一個 task 的篩選）
    state.stage2 = {
      selectedTiers:  new Set(),
      yearFrom: null, yearTo: null, yearMin: null, yearMax: null,
    };
    if (renderView) {
      renderStage2();
      setStageMode('stage2');
    }
  } catch (err) {
    console.error('Stage 2 hits 載入失敗', err);
  }
}


function renderStage2() {
  const task = state.tasks.find(t => t.id === state.currentTaskId);
  document.getElementById('stage2-total').textContent = state.hits.length.toLocaleString();

  // task keyword 顯示，順帶顯示 main_text 篩選（若有）
  let kwLabel = task ? `· ${task.keyword}` : '';
  if (task) {
    let sp = {};
    try { sp = JSON.parse(task.search_params || '{}'); } catch {}
    if (sp.main_text) kwLabel += `　主文含「${sp.main_text}」`;
  }
  document.getElementById('stage2-task-keyword').textContent = kwLabel;

  // Stage 1 進行中：顯示「搜尋中…」indicator，律師看到數字仍在累積
  const stillRunning = task && (task.status === 'running' || task.status === 'pending');
  document.getElementById('stage2-running-indicator').classList.toggle('hidden', !stillRunning);

  setupStage2YearSlider();
  renderStage2Filters();
  renderStage2List();
}

// ── 年度範圍拉桿 ──
// 從 state.hits 推年份範圍 → 設定 slider min/max + 初始位置 + 上下限標籤
function setupStage2YearSlider() {
  const years = state.hits
    .map(h => parseInt((h.date || '').split('-')[0], 10))
    .filter(Number.isFinite);
  const fromInp = document.getElementById('stage2-year-from');
  const toInp   = document.getElementById('stage2-year-to');
  const wrap    = document.getElementById('stage2-year-slider-wrap');
  const minLbl  = document.getElementById('stage2-year-min');
  const maxLbl  = document.getElementById('stage2-year-max');
  const valLbl  = document.getElementById('stage2-year-label');

  if (years.length === 0) {
    wrap.style.opacity = '0.4';
    valLbl.textContent = '無資料';
    minLbl.textContent = maxLbl.textContent = '—';
    return;
  }
  wrap.style.opacity = '1';

  const minY = Math.min(...years);
  const maxY = Math.max(...years);
  state.stage2.yearMin = minY;
  state.stage2.yearMax = maxY;

  // 單一年度時不顯示 slider，只顯示固定值
  if (minY === maxY) {
    wrap.style.opacity = '0.4';
    valLbl.textContent = `僅 ${minY} 年`;
    minLbl.textContent = maxLbl.textContent = String(minY);
    state.stage2.yearFrom = state.stage2.yearTo = null; // 不需 filter
    return;
  }

  // Stage 1 漸進載入：新 hits 抵達時範圍可能擴大（更舊或更新的判決）。
  // 若律師還沒動拉桿（兩端都在前一次的極限），就跟著新範圍擴大；
  // 若律師已 narrow，保留他選的值，只 clamp 到新範圍內。
  const prevMin = state.stage2.yearMin;
  const prevMax = state.stage2.yearMax;
  const wasFullRange = state.stage2.yearFrom == null
                    || (state.stage2.yearFrom === prevMin && state.stage2.yearTo === prevMax);

  fromInp.min = toInp.min = String(minY);
  fromInp.max = toInp.max = String(maxY);
  if (wasFullRange) {
    state.stage2.yearFrom = minY;
    state.stage2.yearTo   = maxY;
  } else {
    state.stage2.yearFrom = Math.max(minY, Math.min(state.stage2.yearFrom, maxY));
    state.stage2.yearTo   = Math.max(minY, Math.min(state.stage2.yearTo,   maxY));
  }
  fromInp.value = String(state.stage2.yearFrom);
  toInp.value   = String(state.stage2.yearTo);
  minLbl.textContent = String(minY);
  maxLbl.textContent = String(maxY);
  updateStage2YearLabels();
}

// 拖拉時：constrain（from 不能 > to），更新 fill 寬度 + label，re-filter
function onYearSliderInput(which) {
  const fromInp = document.getElementById('stage2-year-from');
  const toInp   = document.getElementById('stage2-year-to');
  let from = parseInt(fromInp.value, 10);
  let to   = parseInt(toInp.value, 10);
  if (which === 'from' && from > to) { from = to; fromInp.value = String(from); }
  if (which === 'to'   && to < from) { to = from; toInp.value = String(to); }
  state.stage2.yearFrom = from;
  state.stage2.yearTo   = to;
  updateStage2YearLabels();
  renderStage2List();
}

function updateStage2YearLabels() {
  const { yearFrom, yearTo, yearMin, yearMax } = state.stage2;
  const valLbl = document.getElementById('stage2-year-label');
  const fill   = document.getElementById('stage2-year-fill');
  if (yearMin == null || yearMax == null || yearMin === yearMax) {
    fill.style.left = '0%';
    fill.style.right = '0%';
    return;
  }
  const span = yearMax - yearMin;
  const leftPct  = ((yearFrom - yearMin) / span) * 100;
  const rightPct = ((yearMax - yearTo)   / span) * 100;
  fill.style.left  = leftPct  + '%';
  fill.style.right = rightPct + '%';
  // 標籤文字：僅顯示 narrow 後實際範圍；若沒 narrow 就標「全部」
  if (yearFrom === yearMin && yearTo === yearMax) {
    valLbl.textContent = `全部 ${yearMin}-${yearMax}`;
  } else {
    valLbl.textContent = `${yearFrom} — ${yearTo}`;
  }
}

function renderStage2Filters() {
  // ── 法院層級 counts ──
  const tierCounts = {};
  for (const t of STAGE2_COURT_TIERS) tierCounts[t] = 0;
  for (const h of state.hits) tierCounts[inferTier(h.court)]++;

  const tierEl = document.getElementById('stage2-tier-filters');
  tierEl.innerHTML = STAGE2_COURT_TIERS.filter(t => tierCounts[t] > 0).map(tier => {
    const checked = state.stage2.selectedTiers.has(tier);
    return `
      <button type="button" data-tier="${escAttr(tier)}"
        class="stage2-chip flex items-center gap-1.5 px-2 py-0.5 rounded-sm border transition-colors
               ${checked ? 'bg-seal/10 border-seal text-seal' : 'border-warm-200 text-warm-500 hover:border-warm-400 hover:text-ink'}">
        <span class="font-serif">${TIER_DISPLAY_NAME[tier] || tier}</span>
        <span class="font-mono text-[10px] opacity-70">${tierCounts[tier]}</span>
      </button>`;
  }).join('');
  tierEl.querySelectorAll('button[data-tier]').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.tier;
      if (state.stage2.selectedTiers.has(t)) state.stage2.selectedTiers.delete(t);
      else state.stage2.selectedTiers.add(t);
      renderStage2Filters();
      renderStage2List();
    });
  });

}

function applyStage2Filters() {
  const f = state.stage2;
  // 拉桿在「全範圍」時不算 narrow（避免無謂的 yr parse）
  const yearActive = f.yearMin != null && f.yearMax != null
                  && f.yearFrom != null && f.yearTo != null
                  && (f.yearFrom > f.yearMin || f.yearTo < f.yearMax);
  return state.hits.filter(h => {
    if (f.selectedTiers.size > 0 && !f.selectedTiers.has(inferTier(h.court))) return false;
    if (yearActive) {
      const yr = parseInt((h.date || '').split('-')[0], 10);
      if (Number.isFinite(yr)) {
        if (yr < f.yearFrom || yr > f.yearTo) return false;
      }
      // 解析失敗時保留（不錯殺）
    }
    return true;
  });
}




function renderStage2List() {
  const filtered = applyStage2Filters();
  const total = state.hits.length;

  const sel = document.getElementById('stage2-selected');
  sel.textContent = filtered.length === total
    ? `全部 ${total.toLocaleString()} 筆`
    : `目前選取 ${filtered.length.toLocaleString()} / ${total.toLocaleString()} 筆`;

  // 「AI 分析」按鈕：選取數 > 0 才能按
  document.getElementById('btn-stage3-modal').disabled = filtered.length === 0;

  const listEl = document.getElementById('stage2-hits-list');
  const emptyEl = document.getElementById('stage2-empty');
  if (filtered.length === 0) {
    listEl.innerHTML = '';
    emptyEl.classList.remove('hidden');
    return;
  }
  emptyEl.classList.add('hidden');

  listEl.innerHTML = filtered.map(h => {
    const safeId = escAttr(h.case_id);
    const court = escHtml(h.court || '');
    const causeBadge = h.cause
      ? `<span class="font-mono text-[10px] bg-warm-100 text-warm-500 px-1.5 py-0.5 shrink-0">${escHtml(h.cause)}</span>`
      : '';
    const cachedBadge = '';   // stage 2.5 deep fetch UI 已移除，不再顯示 ●全文 徽章
    const summaryText = (h.summary || '').replace(/^\s*\.\.\.|\.\.\.\s*$/g, '').trim();
    const summaryPreview = summaryText
      ? `<p class="text-xs text-warm-500 font-serif mt-1 leading-relaxed line-clamp-2">${escHtml(summaryText.slice(0, 200))}</p>`
      : '';
    return `
      <div class="px-6 py-3 border-b border-warm-100 hover:bg-warm-50/50 cursor-pointer transition-colors fade-in"
           onclick="navTo({view:'reader', taskId:state.currentTaskId, caseId:'${safeId}'})">
        <div class="flex items-baseline gap-3">
          <span class="font-serif text-sm font-semibold text-ink">${court}</span>
          <span class="font-mono text-xs text-seal truncate">${escHtml(h.case_id)}</span>
          <span class="font-mono text-xs text-warm-400 ml-auto shrink-0">${escHtml(h.date)}</span>
        </div>
        <div class="flex items-center gap-2 mt-1">${causeBadge}${cachedBadge}</div>
        ${summaryPreview}
      </div>`;
  }).join('');
}

// 年度拉桿：drag 中即時 filter（input 事件比 change 更靈敏）
document.getElementById('stage2-year-from').addEventListener('input', () => onYearSliderInput('from'));
document.getElementById('stage2-year-to').addEventListener('input',   () => onYearSliderInput('to'));

// ─── Stage 3 v2: AI 分析 modal ─────────────────────
// 律師按「AI 分析」→ 填 NL 問題 + 可選讀事實 → POST /analyses → worker 開始 stage 3
//   (fetch → per-judgment Claude 評分 → synthesis 總結)

// 粗估 Stage 3 tokens / 時間（中文字 ≈ 1 token）
// 實際流程（見 analyze.py TWO_PASS_THRESHOLD=20、CONCURRENCY=8）：
//   n ≥ 20：two-pass：每筆先 screening（3K budget）、score>0 再跑 full-pass（12K budget）
//           歷史命中率約 30-45%、取 0.4 做加權
//   n < 20：直接 full-pass（12K budget）
// 每 Claude call 另加 prompt overhead（~400 input / ~300 output）
// 事實段：facts 欄位納入時、input tokens 每筆約 +1500（不影響 output）
// Synthesis：~100 tokens/筆 input + ~2000 tokens output（fixed）
const STAGE3_TOKENS_SCREENING_IN    = 3500;   // screening budget 的中文字
const STAGE3_TOKENS_FULLPASS_IN     = 12000;  // full-pass budget 的中文字
const STAGE3_TOKENS_PROMPT_OVERHEAD = 400;    // 每次 call 的 prompt 模板
const STAGE3_TOKENS_OUTPUT_PER      = 400;    // 每次 call 的 output (JSON)
const STAGE3_TOKENS_FACTS_BUMP      = 1500;   // 事實段加料
const STAGE3_FULLPASS_RATE          = 0.4;    // screening 後進 full-pass 的比例
const STAGE3_TOKENS_SYNTH_PER       = 100;
const STAGE3_TOKENS_SYNTH_OUTPUT    = 2000;

const STAGE3_TWO_PASS_THRESHOLD = 20;         // 跟 backend analyze.py 一致

// 時間估算：用「wall-clock 每筆」而非理論 concurrency 計算。
// 真正瓶頸是 Anthropic 的 token-per-minute rate limit、不是 CONCURRENCY（8）。
// 從過去任務的 DB 資料校準（finished_at - created_at / 實際 judg 數）：
//   兩階段 (n≥20) 中位數 ~22 秒/筆（範圍 6-73、最常落在 15-40）
//   單階段 (n<20) 中位數 ~10 秒/筆（full-pass 單 call、rate-limit 壓力低）
//   取中位數偏保守值做預估，避免誤導律師「只要 3 分鐘」
const STAGE3_SEC_PER_J_TWO_PASS = 25;
const STAGE3_SEC_PER_J_ONE_PASS = 10;
const STAGE3_SEC_SYNTHESIS      = 20;
const STAGE3_FACTS_TIME_PENALTY = 1.2;        // 讀事實加 +20% tokens → +20% 時間

function formatTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(0) + 'K';
  return String(n);
}

function formatDuration(sec) {
  if (sec < 60) return `~${sec} 秒`;
  if (sec < 3600) return `~${Math.ceil(sec / 60)} 分鐘`;
  return `~${(sec / 3600).toFixed(1)} 小時`;
}

// 核心估算器：輸入「要分析的判決數 n」+「是否讀事實」→ 回 { tokens, seconds }
function _estimateStage3(n, readFacts) {
  if (!n) return { tokens: 0, seconds: 0 };
  const twoPass = n >= STAGE3_TWO_PASS_THRESHOLD;
  const factsBump = readFacts ? STAGE3_TOKENS_FACTS_BUMP : 0;

  // ── Tokens ──
  let tokensIn, tokensOut, callsPerJudgment;
  if (twoPass) {
    // screening: 每筆都跑
    const screenIn  = STAGE3_TOKENS_SCREENING_IN + STAGE3_TOKENS_PROMPT_OVERHEAD + factsBump;
    const screenOut = STAGE3_TOKENS_OUTPUT_PER;
    // full-pass: STAGE3_FULLPASS_RATE 比例跑
    const fullIn    = STAGE3_FULLPASS_RATE * (STAGE3_TOKENS_FULLPASS_IN + STAGE3_TOKENS_PROMPT_OVERHEAD + factsBump);
    const fullOut   = STAGE3_FULLPASS_RATE * STAGE3_TOKENS_OUTPUT_PER;
    tokensIn  = n * (screenIn + fullIn);
    tokensOut = n * (screenOut + fullOut);
    callsPerJudgment = 1 + STAGE3_FULLPASS_RATE;
  } else {
    // one-pass：每筆一次 full-pass call
    tokensIn  = n * (STAGE3_TOKENS_FULLPASS_IN + STAGE3_TOKENS_PROMPT_OVERHEAD + factsBump);
    tokensOut = n * STAGE3_TOKENS_OUTPUT_PER;
    callsPerJudgment = 1;
  }
  // Synthesis
  const synthIn  = n * STAGE3_TOKENS_SYNTH_PER + 600;
  const synthOut = STAGE3_TOKENS_SYNTH_OUTPUT;
  const tokens = Math.round(tokensIn + tokensOut + synthIn + synthOut);

  // ── 時間（從實際任務經驗校準、不用理論 concurrency 算）──
  // 瓶頸是 Anthropic TPM rate limit、不是 code 的 concurrency
  const perJ = twoPass ? STAGE3_SEC_PER_J_TWO_PASS : STAGE3_SEC_PER_J_ONE_PASS;
  const factsPenalty = readFacts ? STAGE3_FACTS_TIME_PENALTY : 1.0;
  const scoringSec = Math.ceil(n * perJ * factsPenalty);
  const seconds = scoringSec + STAGE3_SEC_SYNTHESIS;

  return { tokens, seconds };
}

function computeStage3Cost() {
  const n = applyStage2Filters().length;
  const readFacts = document.getElementById('stage3-read-facts').checked;
  const { tokens } = _estimateStage3(n, readFacts);
  return { n, totalTokens: tokens };
}

// 民事/行政「事實及理由」常合併歸入理由段，facts 欄位多為空 — 勾「同時分析事實」
// 等同無效卻仍按 facts tokens 計費。當此 task 的 facts 覆蓋率 < 20% 時 disable
// 兩個 read-facts checkbox（card UI + modal UI），label 標示實際覆蓋。
const _FACTS_COVERAGE_THRESHOLD = 0.20;
const _FACTS_COVERAGE_MIN_SAMPLE = 5;
const _READ_FACTS_CHECKBOX_IDS = ['card-read-facts', 'stage3-read-facts'];

function applyFactsCoverageHint() {
  const { total, with_facts } = state.factsCoverage || { total: 0, with_facts: 0 };
  // 樣本不足或尚未抓全文 — 不下結論，恢復 checkbox 為可用
  const insufficient = total < _FACTS_COVERAGE_MIN_SAMPLE;
  const mostlyEmpty = !insufficient && (with_facts / total) < _FACTS_COVERAGE_THRESHOLD;

  for (const id of _READ_FACTS_CHECKBOX_IDS) {
    const cb = document.getElementById(id);
    if (!cb) continue;
    const wrap = cb.closest('label') || cb.parentElement;
    if (!wrap) continue;
    const labelSpan = wrap.querySelector('span');

    if (mostlyEmpty) {
      cb.checked = false;
      cb.disabled = true;
      wrap.classList.add('opacity-50', 'cursor-not-allowed');
      wrap.title = `此批 ${total} 件中僅 ${with_facts} 件有「事實」欄位（民事/行政「事實及理由」常合併歸入理由段）— 勾選等同無效`;
      if (labelSpan && !labelSpan.dataset.factsHinted) {
        labelSpan.dataset.factsHinted = 'true';
        labelSpan.dataset.originalText = labelSpan.textContent;
        labelSpan.textContent = `${labelSpan.dataset.originalText}（${with_facts}/${total} 件有）`;
      }
    } else {
      cb.disabled = false;
      wrap.classList.remove('opacity-50', 'cursor-not-allowed');
      wrap.title = '';
      if (labelSpan && labelSpan.dataset.factsHinted) {
        labelSpan.textContent = labelSpan.dataset.originalText;
        delete labelSpan.dataset.factsHinted;
        delete labelSpan.dataset.originalText;
      }
    }
  }
}

function updateStage3CostHint() {
  const readFacts = document.getElementById('stage3-read-facts').checked;
  const n = applyStage2Filters().length;
  const { tokens, seconds } = _estimateStage3(n, readFacts);
  document.getElementById('stage3-cost-count').textContent = n.toLocaleString();
  document.getElementById('stage3-cost-tokens').textContent = formatTokens(tokens);
  document.getElementById('stage3-cost-time').textContent = formatDuration(seconds);
}

function openStage3Modal() {
  const filtered = applyStage2Filters();
  if (filtered.length === 0) return;
  document.getElementById('stage3-overlay').classList.remove('hidden');
  // 重置欄位
  document.getElementById('stage3-question').value = '';
  document.getElementById('stage3-read-facts').checked = false;
  document.getElementById('stage3-submit').disabled = false;
  document.getElementById('stage3-submit').textContent = '開始分析';
  applyFactsCoverageHint();
  updateStage3CostHint();
  setTimeout(() => document.getElementById('stage3-question').focus(), 50);
}
function closeStage3Modal() {
  document.getElementById('stage3-overlay').classList.add('hidden');
}

document.getElementById('btn-stage3-modal').addEventListener('click', openStage3Modal);
document.getElementById('stage3-close').addEventListener('click', closeStage3Modal);
document.getElementById('stage3-cancel').addEventListener('click', closeStage3Modal);
document.getElementById('stage3-read-facts').addEventListener('change', updateStage3CostHint);

async function submitStage3Analyze() {
  const q = document.getElementById('stage3-question').value.trim();
  if (!q) {
    document.getElementById('stage3-question').focus();
    return;
  }
  const readFacts = document.getElementById('stage3-read-facts').checked;

  // 把 stage 2 現有 narrow 條件送到後端
  const f = state.stage2;
  const narrow = {};
  if (f.selectedTiers.size > 0) narrow.court_tiers = [...f.selectedTiers];
  const yearActive = f.yearMin != null && f.yearMax != null
                  && f.yearFrom != null && f.yearTo != null
                  && (f.yearFrom > f.yearMin || f.yearTo < f.yearMax);
  if (yearActive) {
    narrow.year_from = f.yearFrom;
    narrow.year_to = f.yearTo;
  }

  const submitBtn = document.getElementById('stage3-submit');
  submitBtn.disabled = true;
  submitBtn.textContent = '送出中…';

  try {
    // 確保 SSE 已訂上（task 可能是 done 狀態，openTask 時沒訂）
    await ensureSseSubscribed(state.currentTaskId);
    const res = await apiFetch(`/api/tasks/${state.currentTaskId}/analyses`, {
      method: 'POST',
      body: JSON.stringify({
        question: q,
        read_facts: readFacts,
        narrow,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { analysis_id, flow } = await res.json();
    state.primaryAnalysisId = analysis_id;
    // 更新 analyses 清單（stage 3 進行中會由 SSE 驅動 UI；Phase C 會接 synthesis 卡）
    await reloadAnalyses();
    closeStage3Modal();
    console.info(`Stage 3 analysis ${flow} 啟動：${analysis_id}`);
  } catch (err) {
    alert(`送出失敗：${err.message}`);
    submitBtn.disabled = false;
    submitBtn.textContent = '開始分析';
  }
}

document.getElementById('stage3-submit').addEventListener('click', submitStage3Analyze);
// Esc 關閉 stage3 modal
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('stage3-overlay').classList.contains('hidden')) {
    closeStage3Modal();
  }
});


// ─── Stage 3 v2: 結果 view ─────────────────────────

const CONSENSUS_LABEL = {
  '一致': { text: '法院見解：高度一致', cls: 'text-emerald-700 bg-emerald-50' },
  '多數': { text: '法院見解：多數見解（有例外）', cls: 'text-amber-700 bg-amber-50' },
  '分歧': { text: '法院見解：分歧', cls: 'text-red-600 bg-red-50' },
  '彙整': { text: '法院認定彙整', cls: 'text-seal bg-seal/10' },
  '不足': { text: '資料不足，難抽共識', cls: 'text-warm-500 bg-warm-100' },
};

async function loadStage3View(analysisId) {
  if (!state.currentTaskId || !analysisId) return;
  state.primaryAnalysisId = analysisId;

  try {
    // 1. 取 analysis（含 synthesis JSON）
    const analyses = state.analyses.length
      ? state.analyses
      : (await apiFetch(API.analyses(state.currentTaskId)).then(r => r.ok ? r.json() : []));
    state.analyses = analyses;
    const a = analyses.find(x => x.id === analysisId);
    if (!a) { console.warn('analysis not found', analysisId); return; }

    // 2. 取 judgments + scores（JOIN analysis_results）
    const url = `${API.judgments(state.currentTaskId)}?primary_analysis_id=${analysisId}`;
    const res = await apiFetch(url);
    const allResults = res.ok ? await res.json() : [];
    // 分桶：score>0 相關；score==0 或 null 無關
    const relevant = allResults.filter(r => (r.primary_score ?? 0) > 0)
                               .sort((a, b) => (b.primary_score ?? 0) - (a.primary_score ?? 0));
    const irrelevant = allResults.filter(r => !((r.primary_score ?? 0) > 0));

    renderStage3View(a, relevant, irrelevant);
    setStageMode('stage3');
  } catch (err) {
    console.error('loadStage3View 失敗', err);
  }
}

function renderStage3View(analysis, relevant, irrelevant) {
  const task = state.tasks.find(t => t.id === state.currentTaskId);
  document.getElementById('stage3-keyword-label').textContent = task ? `· ${task.keyword}` : '';
  const q = analysis.question || '';
  document.getElementById('stage3-question-label').textContent = `「${q}」`;

  // 執行中指示
  const running = analysis.status === 'running' || analysis.status === 'pending';
  document.getElementById('stage3-running-indicator').classList.toggle('hidden', !running);

  // synthesis card
  let synth = null;
  try { synth = analysis.synthesis ? JSON.parse(analysis.synthesis) : null; } catch {}
  const synthConsensus = document.getElementById('stage3-synth-consensus');
  const synthCount = document.getElementById('stage3-synth-count');
  const synthSummary = document.getElementById('stage3-synth-summary');
  if (synth) {
    const meta = CONSENSUS_LABEL[synth.consensus] || CONSENSUS_LABEL['不足'];
    synthConsensus.className = `font-mono text-xs px-1.5 py-0.5 rounded-sm ${meta.cls}`;
    synthConsensus.textContent = meta.text;
    synthCount.textContent = `${synth.total_relevant || 0} 筆相關`;
    synthSummary.innerHTML = _renderInlineMarkdown(synth.summary || '');
  } else if (running) {
    synthConsensus.className = 'font-mono text-xs text-warm-400';
    synthConsensus.textContent = '分析進行中…';
    synthCount.textContent = '';
    synthSummary.textContent = '摘要將於全部分析完成後生成。律師可先看下方已完成的判決。';
  } else {
    synthConsensus.className = 'font-mono text-xs text-warm-400';
    synthConsensus.textContent = '—';
    synthCount.textContent = '';
    synthSummary.textContent = '（無綜合摘要）';
  }

  // 相關清單
  document.getElementById('stage3-relevant-count').textContent = `${relevant.length} 筆`;
  const relEl = document.getElementById('stage3-relevant-list');
  if (relevant.length === 0) {
    relEl.innerHTML = `<div class="text-center py-10 text-warm-400 font-serif text-sm italic">
      ${running ? '分析進行中，結果會陸續出現…' : '分析後沒有判決有論述此問題，建議調整關鍵字或問題。'}
    </div>`;
  } else {
    relEl.innerHTML = relevant.map(r => {
      const score = r.primary_score ?? 0;
      const position = r.primary_reason ?? r.reason ?? '';
      const excerpt = r.primary_excerpt ?? r.excerpt ?? '';
      const safeId = escAttr(r.case_id);
      const scoreColor = score >= 7 ? 'text-seal' : score >= 4 ? 'text-warm-500' : 'text-warm-400';
      return `
        <div class="py-3 border-b border-warm-100 hover:bg-warm-50/50 cursor-pointer transition-colors fade-in"
             onclick="navTo({view:'reader', taskId:state.currentTaskId, caseId:'${safeId}'})">
          <div class="flex items-baseline gap-3 mb-1">
            <span class="font-mono text-xl font-medium ${scoreColor} leading-none w-8 shrink-0">${score}</span>
            <div class="flex-1 min-w-0">
              <div class="flex items-baseline gap-2">
                <span class="font-serif text-sm font-semibold text-ink">${escHtml(r.court || '')}</span>
                <span class="font-mono text-xs text-seal truncate">${escHtml(r.case_id)}</span>
                <span class="font-mono text-xs text-warm-400 ml-auto shrink-0">${escHtml(r.date || '')}</span>
              </div>
              ${position ? `<p class="text-xs text-warm-600 font-serif italic mt-1 leading-relaxed">${escHtml(position)}</p>` : ''}
              ${excerpt ? `<p class="text-xs font-serif mt-1 leading-relaxed result-excerpt-box inline-block">${escHtml(excerpt.slice(0, 180))}</p>` : ''}
            </div>
          </div>
        </div>`;
    }).join('');
  }

  // 無關 toggle + 列表
  document.getElementById('stage3-irrelevant-count').textContent = irrelevant.length;
  const irrEl = document.getElementById('stage3-irrelevant-list');
  irrEl.innerHTML = irrelevant.map(r => {
    const safeId = escAttr(r.case_id);
    return `
      <div class="flex items-baseline gap-3 px-2 py-1.5 text-xs text-warm-400 hover:text-warm-600 cursor-pointer transition-colors"
           onclick="navTo({view:'reader', taskId:state.currentTaskId, caseId:'${safeId}'})">
        <span class="font-mono w-5 shrink-0">—</span>
        <span class="font-serif truncate">${escHtml(r.court || '')}</span>
        <span class="font-mono text-[11px] truncate">${escHtml(r.case_id)}</span>
        <span class="font-mono text-[11px] ml-auto shrink-0">${escHtml(r.date || '')}</span>
      </div>`;
  }).join('');
}

// 無關 toggle 展開收起
document.getElementById('stage3-irrelevant-toggle').addEventListener('click', () => {
  const list = document.getElementById('stage3-irrelevant-list');
  const chev = document.getElementById('stage3-irrelevant-chev');
  const open = !list.classList.contains('hidden');
  list.classList.toggle('hidden', open);
  chev.style.transform = open ? '' : 'rotate(180deg)';
});

// 「← 回篩選」回 stage 2 view（律師調 narrow 後可再跑一次分析）
document.getElementById('btn-stage3-back-to-stage2').addEventListener('click', () => {
  setStageMode('stage2');
  renderStage2();
});


// ═══════════════════════════════════════════════════
//  SEARCH CARD — 浮動卡片 State A / B / C
// ═══════════════════════════════════════════════════

// ─── Card lifecycle ──────────────────────────────────

function openSearchCard(taskId, cardState = 'a') {
  state.card.open = true;
  state.card.taskId = taskId;
  state.currentTaskId = taskId;
  document.getElementById('search-card-backdrop').classList.remove('hidden');
  document.getElementById('search-card').classList.remove('hidden');
  lockBodyScroll();
  // Push history so browser back closes the card
  history.pushState({ view: 'card', taskId, cardState }, '', location.pathname);
  setCardState(cardState);
}

function closeSearchCard() {
  // 空搜尋結果的 task 自動刪除 — 避免任務清單殘留「0 筆 / 已完成」的無用 row，
  // 且點那 row 會進一個空白篩選畫面，體驗糟
  const closingTaskId = state.card.taskId;
  const closingTask = state.tasks.find(t => t.id === closingTaskId);
  const isEmptyCompleted =
    closingTaskId && closingTask &&
    closingTask.status === 'done' &&
    (closingTask.hits_total || 0) === 0 &&
    !state.card.searching;
  state.card.open = false;
  // 清掉 pending final refresh：下次律師重開卡片會 reloadAnalyses fetch 最新 synthesis
  state.card._pendingFinalRefresh = null;
  // 中止中 UI（timer + 強制結束按鈕）也清、避免 DOM 殘留影響下次開卡
  _clearAbortUI();
  // API 錯誤 banner 也清（下次開新 task 重新偵測）
  state.card._apiBannerShown = false;
  const apiBanner = document.getElementById('card-api-error-banner');
  if (apiBanner) apiBanner.remove();
  document.getElementById('search-card-backdrop').classList.add('hidden');
  document.getElementById('search-card').classList.add('hidden');
  unlockBodyScroll();
  if (isEmptyCompleted) {
    // 樂觀移除前端 state；後端刪除失敗也不 rollback（UI 上已沒這筆）
    state.tasks = state.tasks.filter(t => t.id !== closingTaskId);
    apiFetch(`/api/tasks/${closingTaskId}`, { method: 'DELETE' }).catch(() => {});
  }
  renderTaskLists();
}

function setCardState(s) {
  state.card.state = s;
  // 離開 State B 時清理 timer（防止堆疊）
  if (s !== 'b' && state.card._agoTimer) {
    clearInterval(state.card._agoTimer);
    state.card._agoTimer = null;
  }
  // 更新 history entry 讓瀏覽器上一頁回到正確的 card state
  history.replaceState(
    { view: 'card', taskId: state.card.taskId, cardState: s },
    '', location.pathname,
  );
  ['a', 'b', 'c'].forEach(x => {
    document.getElementById(`card-state-${x}`).classList.toggle('hidden', x !== s);
  });
  // 空搜尋結果（0 hits）→ 隱藏 state A 內容（沒東西可篩選、分析按鈕按了也沒用）
  // 只留 header message；關閉按鈕走原本的 close 流程、closeSearchCard 會偵測並刪除 task
  const stateABody = document.getElementById('card-state-a');
  if (stateABody) {
    const isEmptyResult = s === 'a' && !state.card.searching && state.hits.length === 0;
    stateABody.style.visibility = isEmptyResult ? 'hidden' : '';
  }
  // Size: A/B = 680px narrow, C = 85% screen two-column
  const inner = document.getElementById('search-card-inner');
  if (s === 'c') {
    inner.style.width = '90vw';
    inner.style.maxWidth = '1400px';
    inner.style.height = '90vh';
    inner.style.maxHeight = '90vh';
  } else {
    inner.style.width = '680px';
    inner.style.maxWidth = '';
    inner.style.height = '';
    inner.style.maxHeight = '85vh';
  }
  // Header text + search progress bar
  const hdr = document.getElementById('card-header-text');
  const searchBar = document.getElementById('card-search-bar');
  const task = state.tasks.find(t => t.id === state.card.taskId);
  const kw = getTaskOriginalKeyword(task);
  if (s === 'a') {
    if (state.card.searching) {
      hdr.innerHTML = `<span class="flex items-center gap-2">
        <span class="pulse-dot w-1.5 h-1.5 rounded-full bg-seal inline-block"></span>
        搜尋中 · 已找到 <span id="card-search-count">${state.hits.length.toLocaleString()}</span> 筆
      </span>`;
      searchBar.classList.remove('hidden');
    } else if (state.hits.length === 0) {
      // 0 筆命中：告訴使用者沒結果、隱藏分析面板（card-state-a 內容）、
      // 留一個顯眼的「關閉」CTA。關閉時會自動刪除這筆空任務（見 closeSearchCard）。
      hdr.innerHTML = `<span class="block">搜尋完成・沒有判決符合這組關鍵字</span>
        <span class="block text-xs text-warm-400 font-normal mt-1">關閉此任務後會自動從任務清單移除；請調整關鍵字後重新搜尋</span>`;
      searchBar.classList.add('hidden');
    } else {
      const warns = state.card.searchWarnings || [];
      if (warns.length > 0) {
        hdr.innerHTML = `已完成，共找到 ${state.hits.length.toLocaleString()} 筆，請設定 AI 分析範圍
          <span class="block text-xs text-amber-600 font-normal mt-1">${warns.map(w => escHtml(w)).join('；')}</span>`;
      } else {
        hdr.textContent = `已完成，共找到 ${state.hits.length.toLocaleString()} 筆，請設定 AI 分析範圍`;
      }
      searchBar.classList.add('hidden');
    }
  } else if (s === 'b') {
    // 設計版：前面加藍色 pulse dot
    hdr.innerHTML = `<span class="inline-flex items-center gap-2">
      <span class="pulse-dot w-1.5 h-1.5 rounded-full inline-block"
            style="background:var(--d-accent)"></span>
      AI 分析中 · ${escHtml(kw)}
    </span>`;
    searchBar.classList.add('hidden');
  } else if (s === 'c') {
    hdr.textContent = '分析結果';
    searchBar.classList.add('hidden');
  }
  // header keyword chip 只在 State C 顯示 — 其他 state 藏起來
  const hdrKwChip = document.getElementById('card-header-keyword-chip');
  if (hdrKwChip) hdrKwChip.classList.toggle('hidden', s !== 'c');
  updateCardHeaderStats(s);
  // Render the appropriate state
  if (s === 'a') renderCardNarrow();
  // b and c are set by their dedicated render functions
}

// Close button
document.getElementById('card-close').addEventListener('click', closeSearchCard);
document.getElementById('card-delete-task').addEventListener('click', async () => {
  const taskId = state.card.taskId;
  if (!taskId) return;
  if (!confirm('確定要刪除此搜尋任務？此操作不可復原。')) return;
  try {
    if (state.sse) { try { state.sse.close(); } catch {} state.sse = null; }
    await apiFetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
    state.tasks = state.tasks.filter(t => t.id !== taskId);
    state.bell.tasks.delete(taskId);
    const bellSrc = state.bell.sseConnections.get(taskId);
    if (bellSrc) { bellSrc.close(); state.bell.sseConnections.delete(taskId); }
    closeSearchCard();
    renderNotificationBell();
    renderTaskLists();
  } catch (err) {
    alert(`刪除失敗：${err.message}`);
  }
});
document.getElementById('search-card-backdrop').addEventListener('click', closeSearchCard);
// Esc closes card
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && state.card.open) closeSearchCard();
});

// ─── State A: Narrow + AI 分析指令 ──────────────────

function renderCardNarrow() {
  renderCardKeywordChip();
  renderCardTierChips();
  setupCardYearSlider();
  updateCardFilteredCount();
  renderCardHitPreview();
  updateCardCostHint();

  // 主文篩選：顯示當前 task 的 main_text 值
  const task = state.tasks.find(t => t.id === state.card.taskId);
  let currentMainText = '';
  try { currentMainText = JSON.parse(task?.search_params || '{}').main_text || ''; } catch {}
  const mtInput = document.getElementById('card-main-text');
  const mtCurrent = document.getElementById('card-main-text-current');
  if (mtInput) mtInput.value = currentMainText;
  if (mtCurrent) {
    if (currentMainText) {
      mtCurrent.classList.remove('hidden');
      mtCurrent.textContent = currentMainText;
    } else {
      mtCurrent.classList.add('hidden');
    }
  }

  // Reset question, facts toggle, and reasoning filter（reasoning filter 下方會
  // fetch prefilter-result 恢復跑中/完成狀態 — reset 只是給無 prefilter 的 task 一個 baseline）
  document.getElementById('card-question').value = '';
  document.getElementById('card-read-facts').checked = false;
  const reasonBtn = document.getElementById('card-reasoning-toggle');
  reasonBtn.dataset.active = 'false';
  reasonBtn.className = reasonBtn.className.replace('bg-seal/10 border-seal text-seal', 'border-warm-200 text-warm-500');
  document.getElementById('card-reasoning-status').classList.add('hidden');
  state.card.reasoningFilter = false;
  state.card.prefilterCaseIds = null;
  state.card.prefilterRunning = false;
  state.card.prefilterNarrowJson = null;

  // 從 DB 恢復 prefilter 狀態（若有）：running / done / cancelled 各走不同 UI 路徑
  restorePrefilterFromDb(state.card.taskId).catch(err => console.warn('[prefilter] restore 失敗:', err));

  // 搜尋中：disable 分析按鈕 + 加提示
  const submitBtn = document.getElementById('card-submit-analyze');
  if (state.card.searching) {
    submitBtn.disabled = true;
    submitBtn.textContent = '搜尋完成後可分析';
    submitBtn.classList.add('opacity-50');
  } else {
    submitBtn.disabled = false;
    submitBtn.textContent = 'AI　分　析';
    submitBtn.classList.remove('opacity-50');
  }
}

function getTaskOriginalKeyword(task) {
  if (!task) return '';
  // 優先用 search_params.original_keyword（使用者原始輸入）
  try {
    const sp = JSON.parse(task.search_params || '{}');
    if (sp.original_keyword) return sp.original_keyword;
  } catch {}
  return task.keyword || '';
}

function renderCardKeywordChip() {
  const task = state.tasks.find(t => t.id === state.card.taskId);
  const kw = getTaskOriginalKeyword(task);
  const el = document.getElementById('card-keyword-chip');
  const keywords = kw.split(/\s+/).filter(Boolean);
  // Keyword chips + add button (only in State A, not searching)
  const addBtn = (!state.card.searching && state.card.state === 'a')
    ? `<button onclick="startAddKeyword(this)" aria-label="增加關鍵字"
              class="w-5 h-5 flex items-center justify-center border border-dashed border-warm-300
                     text-warm-400 hover:border-seal hover:text-seal rounded-sm transition-colors text-sm leading-none">+</button>`
    : '';
  el.innerHTML = keywords.map(k => `
    <span class="flex items-center gap-1.5 px-2 py-0.5 rounded-sm border
                 border-warm-300 bg-warm-100 text-warm-600 text-xs cursor-default">
      <span class="font-serif">${escHtml(k)}</span>
    </span>
  `).join('') + addBtn;
}

function startAddKeyword(btnEl) {
  // Already has input? Don't duplicate
  if (btnEl.parentElement.querySelector('.kw-inline-input')) return;
  const wrapper = document.createElement('span');
  wrapper.className = 'kw-inline-input flex items-center gap-1';
  wrapper.innerHTML = `
    <input type="text" placeholder="新關鍵字" autofocus
      class="w-24 border-b border-seal bg-transparent text-xs font-mono text-ink
             placeholder-warm-300 outline-none py-0.5" />
    <button class="text-[10px] font-mono text-seal hover:underline">加</button>
    <button class="text-[10px] font-mono text-warm-400 hover:text-ink">取消</button>`;
  btnEl.parentElement.insertBefore(wrapper, btnEl);
  btnEl.style.display = 'none';
  const input = wrapper.querySelector('input');
  const addBtn = wrapper.querySelectorAll('button')[0];
  const cancelBtn = wrapper.querySelectorAll('button')[1];
  input.focus();

  const doAdd = async () => {
    const newKw = input.value.trim();
    if (!newKw) { wrapper.remove(); btnEl.style.display = ''; return; }
    await addKeywordAndResearch(newKw);
  };
  addBtn.addEventListener('click', doAdd);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) doAdd();
    if (e.key === 'Escape') { wrapper.remove(); btnEl.style.display = ''; }
  });
  cancelBtn.addEventListener('click', () => { wrapper.remove(); btnEl.style.display = ''; });
}

async function addKeywordAndResearch(newKeyword) {
  const oldTaskId = state.card.taskId;
  const oldTask = state.tasks.find(t => t.id === oldTaskId);
  if (!oldTask) return;

  const oldOrigKw = getTaskOriginalKeyword(oldTask);
  const combinedKw = oldOrigKw + ' ' + newKeyword;

  // 靜默刪除舊 task（不跳 confirm、不導航）
  try {
    if (state.sse) { try { state.sse.close(); } catch {} state.sse = null; }
    await apiFetch(`/api/tasks/${oldTaskId}`, { method: 'DELETE' });
    state.tasks = state.tasks.filter(t => t.id !== oldTaskId);
  } catch {}

  // 取得舊 task 的 main_text 設定
  let mainText = null;
  try {
    const sp = JSON.parse(oldTask.search_params || '{}');
    mainText = sp.main_text || null;
  } catch {}

  // 建新 task，維持卡片開著
  state.hits = [];
  state.stage2 = { selectedTiers: new Set(), yearFrom: null, yearTo: null, yearMin: null, yearMax: null };

  try {
    const res = await apiFetch(API.tasks, {
      method: 'POST',
      body: JSON.stringify({
        keyword: combinedKw,
        expand_keywords: true,
        exhaustive: true,
        main_text: mainText,
        original_keyword: combinedKw,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { task_id } = await res.json();

    state.currentTaskId = task_id;
    state.card.taskId = task_id;
    state.card.searching = true;
    state.primaryAnalysisId = null;
    state.analyses = [];

    // 更新 task list
    const tasksRes = await apiFetch(API.tasks);
    if (tasksRes.ok) state.tasks = await tasksRes.json();
    renderTaskLists();

    // 訂閱 SSE
    subscribeTaskForCard(task_id);

    // 重繪卡片（搜尋中狀態）
    setCardState('a');
  } catch (err) {
    alert(`搜尋失敗：${err.message}`);
    closeSearchCard();
  }
}

function renderCardTierChips() {
  const el = document.getElementById('card-tier-filters');
  const task = state.tasks.find(t => t.id === state.card.taskId);
  const isInterp = task?.search_domain === 'interpretation';

  // 憲法解釋模式：tier 沒區分意義，顯示 disabled 標籤保留視覺一致性
  if (isInterp) {
    el.innerHTML = `
      <span class="flex items-center gap-1.5 px-2 py-0.5 rounded-sm border border-warm-200
                   text-warm-400 text-xs opacity-70 cursor-not-allowed">
        <span class="font-serif">憲法法庭及大法官解釋</span>
        <span class="font-mono text-[10px]">${state.hits.length}</span>
      </span>`;
    return;
  }

  const tierCounts = {};
  for (const t of STAGE2_COURT_TIERS) tierCounts[t] = 0;
  for (const h of state.hits) tierCounts[inferTier(h.court)]++;

  el.innerHTML = STAGE2_COURT_TIERS.filter(t => tierCounts[t] > 0).map(tier => {
    const checked = state.stage2.selectedTiers.has(tier);
    return `
      <button type="button" data-tier="${escAttr(tier)}"
        class="flex items-center gap-1.5 px-2 py-0.5 rounded-sm border transition-colors text-xs
               ${checked ? 'bg-seal/10 border-seal text-seal' : 'border-warm-200 text-warm-500 hover:border-warm-400 hover:text-ink'}">
        <span class="font-serif">${TIER_DISPLAY_NAME[tier] || tier}</span>
        <span class="font-mono text-[10px] opacity-70">${tierCounts[tier]}</span>
      </button>`;
  }).join('');
  el.querySelectorAll('button[data-tier]').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.tier;
      if (state.stage2.selectedTiers.has(t)) state.stage2.selectedTiers.delete(t);
      else state.stage2.selectedTiers.add(t);
      saveStage2FilterToStorage(state.card.taskId);
      renderCardTierChips();
      updateCardFilteredCount();
      renderCardHitPreview();
      updateCardCostHint();
      maybeRestartPrefilterOnNarrowChange();  // B 方案：narrow 變 → 自動重啟 prefilter
    });
  });
}

function setupCardYearSlider() {
  const yearColumn = document.getElementById('card-year-column');

  // Interpretation mode：Stage 1 hit 沒帶 date（search_interpretations 不回 date，要
  // 到 Stage 2.5 fetch 才拿得到），無法在此階段做年度 filter。整塊隱藏、避免「無資料」誤會。
  const isInterp = _task_search_domain() === 'interpretation';
  if (isInterp) {
    if (yearColumn) yearColumn.style.display = 'none';
    state.stage2.yearFrom = state.stage2.yearTo = null;
    state.stage2.yearMin = state.stage2.yearMax = null;
    return;
  }
  if (yearColumn) yearColumn.style.display = '';

  const years = state.hits
    .map(h => parseInt((h.date || '').split('-')[0], 10))
    .filter(Number.isFinite);
  const fromInp = document.getElementById('card-year-from');
  const toInp   = document.getElementById('card-year-to');
  const valLbl  = document.getElementById('card-year-label');
  const minLbl  = document.getElementById('card-year-min');
  const maxLbl  = document.getElementById('card-year-max');
  const track   = document.getElementById('card-year-track');

  if (years.length === 0) {
    valLbl.textContent = '無資料';
    minLbl.textContent = maxLbl.textContent = '—';
    return;
  }
  const minY = Math.min(...years);
  const maxY = Math.max(...years);
  state.stage2.yearMin = minY;
  state.stage2.yearMax = maxY;

  const sliderWrap = document.getElementById('card-year-slider-wrap');
  if (minY === maxY) {
    valLbl.textContent = `僅 ${minY} 年`;
    minLbl.textContent = maxLbl.textContent = '';
    state.stage2.yearFrom = state.stage2.yearTo = null;
    // 單一年度：隱藏拉桿
    if (sliderWrap) sliderWrap.style.display = 'none';
    return;
  }
  if (sliderWrap) sliderWrap.style.display = '';

  const wasFullRange = state.stage2.yearFrom == null
    || (state.stage2.yearFrom <= minY && state.stage2.yearTo >= maxY);
  fromInp.min = toInp.min = String(minY);
  fromInp.max = toInp.max = String(maxY);
  if (wasFullRange) {
    state.stage2.yearFrom = minY;
    state.stage2.yearTo   = maxY;
  } else {
    state.stage2.yearFrom = Math.max(minY, Math.min(state.stage2.yearFrom, maxY));
    state.stage2.yearTo   = Math.max(minY, Math.min(state.stage2.yearTo,   maxY));
  }
  fromInp.value = String(state.stage2.yearFrom);
  toInp.value   = String(state.stage2.yearTo);
  minLbl.textContent = String(minY);
  maxLbl.textContent = String(maxY);
  updateCardYearLabels();
}

function updateCardYearLabels() {
  const { yearFrom, yearTo, yearMin, yearMax } = state.stage2;
  const valLbl = document.getElementById('card-year-label');
  const track  = document.getElementById('card-year-track');
  if (yearMin == null || yearMax == null || yearMin === yearMax) return;
  const span = yearMax - yearMin;
  const leftPct  = ((yearFrom - yearMin) / span) * 100;
  const rightPct = ((yearMax - yearTo)   / span) * 100;
  track.style.left  = leftPct  + '%';
  track.style.right = rightPct + '%';
  valLbl.textContent = (yearFrom === yearMin && yearTo === yearMax)
    ? `全部 ${yearMin}-${yearMax}` : `${yearFrom} — ${yearTo}`;
}

document.getElementById('card-year-from').addEventListener('input', () => onCardYearInput('from'));
document.getElementById('card-year-to').addEventListener('input',   () => onCardYearInput('to'));

// ─── 主文篩選：Enter 觸發重新搜尋（同 addKeywordAndResearch 模式）──
{
  const mtInp = document.getElementById('card-main-text');
  if (mtInp) mtInp.addEventListener('keydown', async e => {
    if (e.key !== 'Enter' || e.isComposing) return;
    const newMainText = mtInp.value.trim() || null;
    const oldTask = state.tasks.find(t => t.id === state.card.taskId);
    if (!oldTask) return;

    // 取得舊 main_text，沒變就不重搜
    let oldMainText = null;
    try { oldMainText = JSON.parse(oldTask.search_params || '{}').main_text || null; } catch {}
    if (newMainText === oldMainText) return;

    const kw = getTaskOriginalKeyword(oldTask);
    const oldTaskId = state.card.taskId;

    // 靜默刪除舊 task
    try {
      if (state.sse) { try { state.sse.close(); } catch {} state.sse = null; }
      await apiFetch(`/api/tasks/${oldTaskId}`, { method: 'DELETE' });
      state.tasks = state.tasks.filter(t => t.id !== oldTaskId);
    } catch {}

    // 建新 task（同關鍵字 + 新 main_text）
    state.hits = [];
    state.stage2 = { selectedTiers: new Set(), yearFrom: null, yearTo: null, yearMin: null, yearMax: null };
    try {
      const res = await apiFetch(API.tasks, {
        method: 'POST',
        body: JSON.stringify({
          keyword: kw, expand_keywords: true, exhaustive: true,
          main_text: newMainText, original_keyword: kw,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const { task_id } = await res.json();
      state.currentTaskId = task_id;
      state.card.taskId = task_id;
      state.card.searching = true;
      state.primaryAnalysisId = null;
      state.analyses = [];
      const tasksRes = await apiFetch(API.tasks);
      if (tasksRes.ok) state.tasks = await tasksRes.json();
      renderTaskLists();
      subscribeTaskForCard(task_id);
      setCardState('a');
    } catch (err) {
      alert(`搜尋失敗：${err.message}`);
    }
  });
}
function onCardYearInput(which) {
  const fromInp = document.getElementById('card-year-from');
  const toInp   = document.getElementById('card-year-to');
  let from = parseInt(fromInp.value, 10);
  let to   = parseInt(toInp.value, 10);
  // 至少間隔 1 年
  if (which === 'from' && from >= to) { from = to - 1; fromInp.value = String(from); }
  if (which === 'to'   && to <= from) { to = from + 1; toInp.value = String(to); }
  state.stage2.yearFrom = from;
  state.stage2.yearTo   = to;
  saveStage2FilterToStorage(state.card.taskId);
  updateCardYearLabels();
  updateCardFilteredCount();
  renderCardHitPreview();
  updateCardCostHint();
  // B 方案：narrow 變 → 自動重啟 prefilter（只在律師已啟動 prefilter 時才作用）
  maybeRestartPrefilterOnNarrowChange();
}

function getCardFilteredHits() {
  return applyStage2Filters();
}

function updateCardFilteredCount() {
  const filtered = getCardFilteredHits();
  document.getElementById('card-filtered-count').textContent = filtered.length.toLocaleString();
}

function renderCardHitPreview() {
  const filtered = getCardFilteredHits();
  const previewEl = document.getElementById('card-hit-preview');
  if (filtered.length === 0) {
    previewEl.innerHTML = `<p class="text-xs text-warm-400 font-mono py-3">無符合條件的判決</p>`;
    return;
  }
  // Show max 2 items
  previewEl.innerHTML = filtered.slice(0, 2).map(h => `
    <div class="py-2 border-b border-warm-100">
      <div class="flex items-baseline gap-2">
        <span class="font-serif text-sm text-ink">${escHtml(h.court || '')}</span>
        <span class="font-mono text-xs text-seal truncate">${escHtml(h.case_id)}</span>
        <span class="font-mono text-xs text-warm-400 ml-auto shrink-0">${escHtml(h.date)}</span>
      </div>
    </div>
  `).join('');
}

function updateCardCostHint() {
  const filtered = getCardFilteredHits();
  // 理由預篩有結果時用 prefilter count，否則用篩選後 count
  const n = (state.card.reasoningFilter && state.card.prefilterCaseIds)
    ? state.card.prefilterCaseIds.length
    : filtered.length;
  const readFacts = document.getElementById('card-read-facts').checked;
  const { tokens, seconds } = _estimateStage3(n, readFacts);
  document.getElementById('card-cost-tokens').textContent = formatTokens(tokens);
  document.getElementById('card-cost-time').textContent = formatDuration(seconds);
  document.getElementById('card-cost-mini').textContent =
    n > 0 ? `估 ${formatTokens(tokens)} tokens · ${formatDuration(seconds)}` : '';
}

document.getElementById('card-read-facts').addEventListener('change', updateCardCostHint);

// ─── Reasoning pre-filter toggle ────────────────────

// toggle 按鈕 UI 狀態（active / inactive）統一 helper — 被 restore / SSE 事件共用
function _syncReasoningToggleButtonUI(active) {
  const btn = document.getElementById('card-reasoning-toggle');
  if (!btn) return;
  btn.dataset.active = active ? 'true' : 'false';
  if (active) {
    btn.className = btn.className.replace('border-warm-200 text-warm-500', 'bg-seal/10 border-seal text-seal');
  } else {
    btn.className = btn.className.replace('bg-seal/10 border-seal text-seal', 'border-warm-200 text-warm-500');
  }
}

// ─── Prefilter 持久化恢復 + narrow 變更自動重啟（B 方案） ─────────
async function restorePrefilterFromDb(taskId) {
  const res = await apiFetch(API.prefilterResult(taskId));
  if (!res.ok) return;
  const r = await res.json();
  if (!r) return;  // 沒有 prefilter row — 保持 reset 狀態

  const statusEl = document.getElementById('card-reasoning-status');
  const btn = document.getElementById('card-reasoning-toggle');

  if (r.status === 'cancelled') {
    // 顯示中斷 banner（律師可以按 toggle 重啟、或 × 清除）
    showPrefilterCancelledBanner(taskId, r);
    return;
  }

  // running 或 done：恢復 active UI
  state.card.reasoningFilter = true;
  state.card.prefilterCaseIds = r.matched_case_ids || null;
  state.card.prefilterRunning = (r.status === 'running');
  state.card.prefilterNarrowJson = JSON.stringify(r.narrow || {}, Object.keys(r.narrow || {}).sort());
  state.card.prefilterTotal = r.total;
  state.card.prefilterMatched = r.matched;

  _syncReasoningToggleButtonUI(true);

  statusEl.classList.remove('hidden');
  if (r.status === 'running') {
    statusEl.innerHTML = `<span class="pulse-dot w-1 h-1 rounded-full bg-seal inline-block"></span> 比對中 ${r.matched}/${r.total}...`;
  } else {
    statusEl.innerHTML = `<span class="text-seal">✓</span> 理由篩選：${r.matched} 筆命中 / ${r.total} 筆`;
  }
  updateCardFilteredCount();
  updateCardCostHint();
}

function showPrefilterCancelledBanner(taskId, result) {
  const statusEl = document.getElementById('card-reasoning-status');
  statusEl.classList.remove('hidden');
  statusEl.innerHTML = `
    <span class="text-warn">⚠</span> 預篩中斷（recovery 已放棄，${result.recovery_attempts} 次嘗試失敗）。
    <button id="card-reasoning-retry" class="ml-2 text-seal hover:underline">重新執行</button>
    <button id="card-reasoning-clear" class="ml-2 text-warm-400 hover:text-ink hover:underline">清除</button>
  `;
  document.getElementById('card-reasoning-retry')?.addEventListener('click', async () => {
    // 按 toggle 模擬重試（INSERT OR REPLACE 會把 status 覆蓋回 'running'、attempts=0）
    document.getElementById('card-reasoning-toggle').click();
  });
  document.getElementById('card-reasoning-clear')?.addEventListener('click', async () => {
    await apiFetch(API.prefilterResult(taskId), { method: 'DELETE' });
    statusEl.classList.add('hidden');
  });
}

// 當 stage2 filter 變動、且 prefilter 正在跑 → 自動 re-submit（B 方案）
// narrow 覆蓋後後端舊 work 會 ownership-lost 自己 abort
async function maybeRestartPrefilterOnNarrowChange() {
  if (!state.card.reasoningFilter) return;
  const newNarrow = buildCurrentNarrow();
  const newNarrowJson = JSON.stringify(newNarrow, Object.keys(newNarrow).sort());
  if (newNarrowJson === state.card.prefilterNarrowJson) return;

  // 重啟（靜默 — 進度條會自動從 0 開始，toast 提示律師）
  state.card.prefilterNarrowJson = newNarrowJson;
  state.card.prefilterRunning = true;
  state.card.prefilterCaseIds = null;
  state.card.prefilterMatched = 0;
  state.card.prefilterFetched = 0;

  const statusEl = document.getElementById('card-reasoning-status');
  statusEl.classList.remove('hidden');
  statusEl.innerHTML = '<span class="pulse-dot w-1 h-1 rounded-full bg-seal inline-block"></span> 依新篩選重新比對...';

  await ensureSseSubscribed(state.card.taskId);
  try {
    await apiFetch(API.prefilterStart(state.card.taskId), {
      method: 'POST',
      body: JSON.stringify({ narrow: newNarrow }),
    });
  } catch (err) {
    statusEl.textContent = '重啟失敗：' + err.message;
    state.card.prefilterRunning = false;
  }
}

function buildCurrentNarrow() {
  const f = state.stage2;
  const narrow = {};
  if (f.selectedTiers.size > 0) narrow.court_tiers = [...f.selectedTiers];
  const yearActive = f.yearMin != null && f.yearMax != null
    && (f.yearFrom > f.yearMin || f.yearTo < f.yearMax);
  if (yearActive) { narrow.year_from = f.yearFrom; narrow.year_to = f.yearTo; }
  return narrow;
}

document.getElementById('card-reasoning-toggle').addEventListener('click', async () => {
  const btn = document.getElementById('card-reasoning-toggle');
  const wasActive = btn.dataset.active === 'true';
  const checked = !wasActive;
  btn.dataset.active = String(checked);
  // Toggle button style
  if (checked) {
    btn.className = btn.className.replace('border-warm-200 text-warm-500', 'bg-seal/10 border-seal text-seal');
  } else {
    btn.className = btn.className.replace('bg-seal/10 border-seal text-seal', 'border-warm-200 text-warm-500');
  }
  state.card.reasoningFilter = checked;

  if (checked) {
    // 啟動預篩
    state.card.prefilterRunning = true;
    state.card.prefilterCaseIds = null;
    state.card.prefilterFetched = 0;
    state.card.prefilterMatched = 0;

    const statusEl = document.getElementById('card-reasoning-status');
    statusEl.classList.remove('hidden');
    statusEl.innerHTML = '<span class="pulse-dot w-1 h-1 rounded-full bg-seal inline-block"></span> 準備下載全文...';

    // 計算 narrow（共用 helper）
    const narrow = buildCurrentNarrow();
    state.card.prefilterNarrowJson = JSON.stringify(narrow, Object.keys(narrow).sort());

    const filtered = getCardFilteredHits();
    state.card.prefilterTotal = filtered.length;

    // 確保 SSE 已訂閱（複用 bell SSE 或 state.sse）
    await ensureSseSubscribed(state.card.taskId);

    // POST 啟動預篩（後端會 init_prefilter_result UPSERT，attempts 自動歸 0）
    try {
      await apiFetch(API.prefilterStart(state.card.taskId), {
        method: 'POST',
        body: JSON.stringify({ narrow }),
      });
    } catch (err) {
      statusEl.textContent = '啟動失敗：' + err.message;
      state.card.prefilterRunning = false;
    }
  } else {
    // 取消勾選 — 律師明確放棄預篩結果：DELETE DB row（backend 舊 work 下次 ownership
    // check 會 miss 自己 abort）。已寫好的 task_judgments 保留不動（AI 分析仍可用）。
    state.card.reasoningFilter = false;
    state.card.prefilterCaseIds = null;
    state.card.prefilterNarrowJson = null;
    state.card.prefilterRunning = false;
    document.getElementById('card-reasoning-status').classList.add('hidden');
    if (state.card.taskId) {
      apiFetch(API.prefilterResult(state.card.taskId), { method: 'DELETE' })
        .catch(err => console.warn('[prefilter] DELETE 失敗:', err));
    }
    // 恢復數字為原始篩選數
    updateCardFilteredCount();
    updateCardCostHint();
  }
});

// ─── Card: Submit AI 分析 ────────────────────────────

document.getElementById('card-submit-analyze').addEventListener('click', async () => {
  const btn = document.getElementById('card-submit-analyze');
  if (btn.disabled || state.card.searching) return;
  btn.disabled = true; btn.classList.add('opacity-50'); // 立即 disable 防雙擊
  // 檢查 API key
  if (!localStorage.getItem(KEY_STORAGE)) {
    if (confirm('尚未設定 Anthropic API Key，AI 分析需要 API Key 才能運作。\n\n是否前往設定？')) {
      openSettings('api-key');
    }
    btn.disabled = false; btn.classList.remove('opacity-50');
    return;
  }
  const q = document.getElementById('card-question').value.trim();
  if (!q) { document.getElementById('card-question').focus(); btn.disabled = false; btn.classList.remove('opacity-50'); return; }

  const readFacts = document.getElementById('card-read-facts').checked;
  const f = state.stage2;
  const narrow = {};
  if (f.selectedTiers.size > 0) narrow.court_tiers = [...f.selectedTiers];
  const yearActive = f.yearMin != null && f.yearMax != null
    && (f.yearFrom > f.yearMin || f.yearTo < f.yearMax);
  if (yearActive) { narrow.year_from = f.yearFrom; narrow.year_to = f.yearTo; }

  btn.textContent = '送出中...';

  try {
    // 先訂閱 SSE（在 POST 之前，避免 worker 搶先 publish 事件漏接）
    // 關閉舊的 bell SSE（如果有），強制建新連線
    const oldBellSrc = state.bell.sseConnections.get(state.card.taskId);
    if (oldBellSrc) { oldBellSrc.close(); state.bell.sseConnections.delete(state.card.taskId); }
    subscribeBellTask(state.card.taskId);

    const res = await apiFetch(`/api/tasks/${state.card.taskId}/analyses`, {
      method: 'POST',
      body: JSON.stringify({
        question: q,
        read_facts: readFacts,
        narrow,
        reasoning_filter: state.card.reasoningFilter,
        // 預篩已有結果時帶 case_ids（讓 worker 跳過已知未命中的）
        prefilter_case_ids: (state.card.reasoningFilter && state.card.prefilterCaseIds?.length)
          ? state.card.prefilterCaseIds : null,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { analysis_id } = await res.json();
    state.card.analysisId = analysis_id;
    state.primaryAnalysisId = analysis_id;
    state.card.progress = 0;
    state.card.progressPhase = 'fetch';
    state.card.fetchTotal = 0;
    state.card.analyzeTotal = 0;
    state.card._doneJudg = 0;  // 新分析開始 → 中止按鈕 label 回到「中止分析」
    state.card._matchCount = 0;
    state.card._feedItems = [];  // 新分析開始 → 清空即時 feed 歷史
    state.card._feedItemCaseIds = new Set();
    state.card._lastFeedTime = null;  // 新分析開始 → 時鐘 reset（renderCardProgress 會處理）
    state.card._origTotal = null;  // 重置 Round 判斷基準
    state.card._round2Count = null;

    // Register in bell
    const task = state.tasks.find(t => t.id === state.card.taskId);
    state.bell.tasks.set(state.card.taskId, {
      status: 'running', progress: 0, keyword: task?.keyword || '',
      analysisId: analysis_id, unread: false, question: q,
    });
    renderNotificationBell();
    renderTaskLists();  // 首頁任務列即時顯示「AI 分析中」

    // 已有前次分析結果 → 回 State C，新提問顯示為 loading tab
    // 首次分析 → State B 進度畫面
    await reloadAnalyses();
    const hasPreviousResults = state.analyses.some(a => a.synthesis && a.id !== analysis_id);
    if (hasPreviousResults) {
      // 回到 State C，顯示前次結果 + 新提問 loading tab
      const prevDone = [...state.analyses].reverse().find(a => a.synthesis);
      if (prevDone) {
        await renderCardResults(prevDone.id);
      }
      setCardState('c');
      renderAnalysisHistoryTabs();
    } else {
      renderCardProgress(q);
      setCardState('b');
    }
  } catch (err) {
    alert(`送出失敗：${err.message}`);
    btn.disabled = false;
    btn.textContent = 'AI　分　析';
  }
});

// ─── State B: Progress ──────────────────────────────

function renderCardProgress(question) {
  // 注意：不要在 JS 包 `「...」` ──.progress-question-b 有 CSS ::before/::after
  // 自動加引號，JS 再包一層會變 `「「...」」`。
  // 同時剝掉 user 可能自己打的頭尾引號，才不會跟 CSS 的加進來疊層。
  const stripped = (question || '').replace(/^[「『"]+|[」』"]+$/g, '').trim();
  document.getElementById('card-progress-question').textContent = stripped;
  // 恢復 State B 的正常外觀：進度條顏色 + 中止按鈕 可能被前一次 renderCardError 改過
  const fill = document.getElementById('card-progress-fill');
  if (fill) { fill.classList.remove('bg-red-400'); fill.classList.add('bg-seal'); }
  const abortBtn = document.getElementById('card-abort-analyze');
  if (abortBtn) {
    abortBtn.classList.remove('hidden', 'opacity-60');
    abortBtn.disabled = false;  // 前一次可能被 disruptive / graceful flow disable
  }
  // 從 state.analyses 撈當下 analysis 的 completed/total/match_count（不依賴 bellInfo、
  // 因為 bellInfo 在「State C → 查看進度 → 回 State B」場景下是舊值）
  const curAnalysis = (state.analyses || []).find(a => a.id === state.card.analysisId) || {};
  const aCompleted = curAnalysis.completed || 0;
  const aTotal = curAnalysis.total || 0;
  const aMatch = curAnalysis.match_count || 0;
  const twoPassHere = aTotal >= 40;
  const doneJudgInit = twoPassHere ? Math.floor(aCompleted / 2) : aCompleted;
  const totalJudgInit = twoPassHere ? Math.floor(aTotal / 2) : aTotal;
  state.card._doneJudg = doneJudgInit;
  state.card._matchCount = aMatch;
  _updateAbortButtonLabel();

  const bellInfo = state.bell.tasks.get(state.card.taskId);
  const bellProgress = bellInfo?.progress || 0;
  const phase = bellInfo?.progressPhase || '';
  // 進度：有 analysis.total 就用實際 DB 值算、沒有才 fallback bellInfo
  const progressFromDb = (aTotal > 0) ? Math.round(33 + (aCompleted / aTotal) * 57) : null;
  const progress = progressFromDb !== null ? progressFromDb : bellProgress;
  let label = '準備中...';
  if (phase === 'fetch') label = '全文快取中';
  else if (phase === 'screen' || phase === 'read') label = 'AI 分析中';
  else if (phase === 'synth') label = '正在產出結果';
  else if (aTotal > 0 && aCompleted > 0) {
    // 從 State C 切回 State B、bellInfo 尚未更新、用 DB 真值算 label
    const matchPart = `（命中 ${aMatch}）`;
    label = `AI 分析中 ${doneJudgInit} / ${totalJudgInit}${matchPart}`;
  } else if (progress > 33) label = 'AI 分析中';
  else if (progress > 0) label = '全文快取中';
  document.getElementById('card-progress-fill').style.width = Math.max(2, progress) + '%';
  document.getElementById('card-progress-label').textContent = label;
  document.getElementById('card-progress-eta').textContent = '';

  // Reset ticker 基準邏輯：
  //   - _lastFeedTime == null（首次進 State B 或新 analysis）→ 有結果就 set 現在、否則等首筆
  //   - _lastFeedTime 已有值（律師切 State C 又切回 State B）→ 保留、不要掩蓋真正卡住的 LLM
  // 這樣反覆切換不會隱藏 API stuck、只有「從沒進過 State B」才強制 reset 時鐘
  if (state.card._lastFeedTime == null) {
    state.card._lastFeedTime = aCompleted > 0 ? Date.now() : null;
  }
  // 即時 feed：從 state 重建（跳出卡片再回來不會丟失）
  const feed = document.getElementById('card-live-feed');
  const timeEl = document.getElementById('card-live-last-time');
  const countEl = document.getElementById('card-live-count');
  const agoEl = document.getElementById('card-live-ago');

  if (!state.card._feedItems) state.card._feedItems = [];

  // 先用既有 state 做「瞬間顯示」避免閃空 — 這些可能是上次 card open 時累積的、
  // 但關卡期間的 SSE 事件因 state.card.open=false 被 gated out（見 appendLiveFeed
  // 在 batch_done handler 處的 guard），所以 state 幾乎一定落後於 DB 實際筆數
  if (state.card._feedItems.length > 0 && feed) {
    feed.innerHTML = '';
    for (const html of state.card._feedItems) {
      const row = document.createElement('div');
      row.className = 'flex items-center gap-2 text-[11px] font-mono';
      row.innerHTML = html;
      feed.appendChild(row);
    }
    feed.scrollTop = feed.scrollHeight;
    if (countEl) countEl.textContent = state.card._feedItemCount || 0;
  } else {
    if (feed) feed.innerHTML = '<p class="text-[11px] font-mono text-warm-400 italic">等待 Claude 回傳...</p>';
    if (timeEl) timeEl.textContent = '';
    if (countEl) countEl.textContent = '0';
    if (agoEl) agoEl.textContent = '';
    state.card._lastFeedTime = null;
    state.card._feedEventCount = 0;
    state.card._feedItemCount = 0;
  }

  // 永遠從 DB 補抓一次 — 覆蓋關卡期間漏接的 batch_done 事件。
  // backfillLiveFeed 會做 merge-by-case_id、不會因瞬間 SSE 事件丟資料
  if (state.card.analysisId) backfillLiveFeed(state.card.taskId, state.card.analysisId);

  // ─ 共用 1s ticker：timeEl 顯示「已執行 MM:SS」、agoEl 顯示燈號 + API 回應狀態
  if (state.card._agoTimer) clearInterval(state.card._agoTimer);
  const _curAnalysis = (state.analyses || []).find(x => x.id === state.card.analysisId);
  state.card._analysisStartMs = _curAnalysis?.created_at
    ? new Date(_curAnalysis.created_at).getTime()
    : Date.now();
  const _fmtElapsed = (ms) => {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(ss)}` : `${pad(m)}:${pad(ss)}`;
  };
  const _tick = () => {
    if (state.card.state !== 'b') { clearInterval(state.card._agoTimer); return; }
    const now = Date.now();
    const timeEl2 = document.getElementById('card-live-last-time');
    const agoEl2 = document.getElementById('card-live-ago');
    if (timeEl2) {
      const start = state.card._analysisStartMs || now;
      timeEl2.textContent = `已執行 ${_fmtElapsed(now - start)}`;
    }
    if (agoEl2) {
      if (!state.card._lastFeedTime) {
        agoEl2.innerHTML = '<span class="text-warm-400">● 等待首筆回傳</span>';
        agoEl2.className = 'text-[10px] font-mono';
      } else {
        const sec = Math.round((now - state.card._lastFeedTime) / 1000);
        if (sec < 15) {
          agoEl2.innerHTML = '<span class="text-emerald-500">●</span> <span class="text-warm-500">正常回應中</span>';
        } else if (sec < 45) {
          agoEl2.innerHTML = `<span class="text-amber-500">●</span> <span class="text-amber-600">回應較慢（${sec}s）</span>`;
        } else {
          agoEl2.innerHTML = `<span class="text-red-500">●</span> <span class="text-red-600">API 無回應，系統自動重試（${sec}s）</span>`;
        }
        agoEl2.className = 'text-[10px] font-mono';
      }
    }
  };
  _tick();  // 立即跑一次、避免 1s 空白
  state.card._agoTimer = setInterval(_tick, 1000);
}

// 把單筆 analysis_result 轉成 live feed 的 row HTML（append / backfill 共用）。
function _liveFeedRowHtml(r, elapsedTag = '') {
  const score = r.score ?? '—';
  const scoreColor = score >= 7 ? 'text-seal' : score >= 1 ? 'text-warm-500' : 'text-warm-400';
  const caseShort = (r.case_id || '').replace(/.*?(\d+年度\S+字第\d+號).*/, '$1') || r.case_id || '';
  const matchIcon = r.match === 'error' ? '✕' : score > 0 ? '●' : '○';
  const matchColor = r.match === 'error' ? 'text-red-400' : score > 0 ? 'text-seal' : 'text-warm-400';
  return `
    <span class="${matchColor}">${matchIcon}</span>
    <span class="${scoreColor} w-5 text-right shrink-0">${score}</span>
    <span class="text-ink truncate">${escHtml(caseShort)}</span>
    ${elapsedTag}`;
}

// 從 DB 撈分析結果補 feed：unconditional 呼叫，讓「即時回傳 N 筆」永遠等於 DB 真相。
//
// 背景：batch_done SSE 事件 handler 有 guard `state.card.open && state.card.state==='b'`，
// 卡片關閉期間事件被 gated out、_feedItems 追不上 DB → 使用者重開卡片會看到
// 落後的數字（如「分析 27/60 但即時回傳只有 5 筆」）。
//
// 解法：renderCardProgress 呼叫 backfill 時永遠重建，用 case_id 為 key 把 DB 結果
// 作為 truth，保留 state._feedItemCaseIds 供重啟時 resync。
async function backfillLiveFeed(taskId, analysisId) {
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/analyses/${analysisId}/results-feed`);
    if (!res.ok) return;
    const items = await res.json();
    // 期間狀態可能已飄走（使用者關卡片 / 切到別 analysis）→ 不動 DOM
    if (!state.card.open || state.card.analysisId !== analysisId || state.card.state !== 'b') return;

    const feed = document.getElementById('card-live-feed');
    if (feed) feed.innerHTML = '';
    state.card._feedItems = [];
    state.card._feedItemCaseIds = new Set();

    for (const r of items) {
      const html = _liveFeedRowHtml(r);
      state.card._feedItems.push(html);
      state.card._feedItemCaseIds.add(r.case_id);
      if (feed) {
        const row = document.createElement('div');
        row.className = 'flex items-center gap-2 text-[11px] font-mono';
        row.innerHTML = html;
        feed.appendChild(row);
      }
    }
    state.card._feedItemCount = items.length;
    const countEl = document.getElementById('card-live-count');
    if (countEl) countEl.textContent = state.card._feedItemCount;
    if (feed && items.length > 0) feed.scrollTop = feed.scrollHeight;
    else if (feed && items.length === 0) {
      feed.innerHTML = '<p class="text-[11px] font-mono text-warm-400 italic">等待 Claude 回傳...</p>';
    }
  } catch {}
}

// 偵測 Anthropic API error 的種類，回 kind 字串或 null
// reason 字串由 backend 從 Exception repr 截 300 字傳過來
function _detectApiErrorKind(reason) {
  if (!reason) return null;
  const r = reason.toLowerCase();
  if (r.includes('credit balance') || r.includes('plans & billing') || r.includes('billing')) return 'billing';
  if (r.includes('authentication') || r.includes('invalid x-api-key') || r.includes('401')) return 'auth';
  if (r.includes('rate_limit') || r.includes('rate limit') || r.includes('429')) return 'rate';
  if (r.includes('overloaded') || r.includes('529')) return 'overloaded';
  return null;
}

// 顯示 API 錯誤 banner（一張 card 只顯示一次；律師按 ✕ 關閉後不再彈）
function _showApiErrorBanner(kind, detail) {
  if (state.card._apiBannerShown) return;
  state.card._apiBannerShown = true;
  // Banner 插在 card header 下、所有 state 都看得到
  const header = document.getElementById('card-header-text')?.closest('div.shrink-0');
  if (!header) return;
  let banner = document.getElementById('card-api-error-banner');
  if (banner) banner.remove();
  banner = document.createElement('div');
  banner.id = 'card-api-error-banner';
  banner.className = 'flex items-start gap-3 px-5 py-3 border-b border-red-200 bg-red-50 text-[12px]';
  const messages = {
    billing: {
      title: 'Anthropic API 餘額不足',
      body: '你的 Anthropic 帳戶 credit 已用完，所有 Claude 呼叫都會失敗。請至 Console 加值。',
      link: 'https://console.anthropic.com/settings/billing',
      linkLabel: '前往加值',
    },
    auth: {
      title: 'Anthropic API key 認證失敗',
      body: 'API key 無效或權限不足。請確認 localStorage 存的 key 是否正確、未過期。',
      link: 'https://console.anthropic.com/settings/keys',
      linkLabel: '管理 API Key',
    },
    rate: {
      title: 'Anthropic API 被 rate limit',
      body: '短時間內送出太多請求、被限流。系統會自動退避重試、但速度會變慢。',
      link: 'https://console.anthropic.com/settings/limits',
      linkLabel: '查看限額',
    },
    overloaded: {
      title: 'Anthropic API 伺服器繁忙',
      body: 'Claude 伺服器暫時過載（529）。稍後會自動重試。',
      link: 'https://status.anthropic.com/',
      linkLabel: 'Anthropic 狀態頁',
    },
  };
  const m = messages[kind] || messages.billing;
  banner.innerHTML = `
    <span class="text-red-500 shrink-0 text-base leading-none">⚠</span>
    <div class="flex-1 min-w-0">
      <div class="font-serif text-red-700 font-medium">${m.title}</div>
      <div class="text-warm-600 mt-0.5">${m.body}</div>
      <div class="mt-1 flex items-center gap-3">
        <a href="${m.link}" target="_blank" rel="noopener"
           class="font-mono text-[11px] text-seal hover:text-ink underline underline-offset-2">
          ${m.linkLabel} →
        </a>
        <span class="font-mono text-[10px] text-warm-400 truncate max-w-md" title="${escHtml(detail || '')}">${escHtml((detail || '').slice(0, 80))}${detail && detail.length > 80 ? '…' : ''}</span>
      </div>
    </div>
    <button id="card-api-error-banner-close" aria-label="關閉"
      class="text-warm-400 hover:text-ink shrink-0 text-base leading-none">✕</button>`;
  header.parentElement.insertBefore(banner, header.nextSibling);
  const closeBtn = document.getElementById('card-api-error-banner-close');
  if (closeBtn) closeBtn.addEventListener('click', () => { banner.remove(); });
}

// 即時活動 feed：每筆 Claude 回傳顯示案號 + score + 距上次回傳的秒數
function appendLiveFeed(results) {
  const feed = document.getElementById('card-live-feed');
  if (!feed) return;
  // 即使 results 為空也更新計數器（證明 SSE 有到）
  state.card._feedEventCount = (state.card._feedEventCount || 0) + 1;
  state.card._feedItemCount = (state.card._feedItemCount || 0) + (results?.length || 0);
  const countEl = document.getElementById('card-live-count');
  if (countEl) countEl.textContent = state.card._feedItemCount;
  // API 錯誤偵測：掃 error results 的 reason、判斷是否為 billing/key/rate 問題
  for (const r of (results || [])) {
    if (r.match === 'error' && r.reason) {
      const kind = _detectApiErrorKind(r.reason);
      if (kind) { _showApiErrorBanner(kind, r.reason); break; }
    }
  }
  if (!results?.length) {
    console.warn('[liveFeed] batch_done event #' + state.card._feedEventCount + ' has empty results');
    return;
  }

  // 移除「等待中」提示
  const placeholder = feed.querySelector('p.italic');
  if (placeholder) placeholder.remove();

  const now = Date.now();
  const elapsed = state.card._lastFeedTime
    ? Math.round((now - state.card._lastFeedTime) / 1000)
    : null;
  state.card._lastFeedTime = now;

  // 更新「上次回傳」時間
  const timeEl = document.getElementById('card-live-last-time');
  if (timeEl) {
    const t = new Date();
    timeEl.textContent = `${t.getHours().toString().padStart(2,'0')}:${t.getMinutes().toString().padStart(2,'0')}:${t.getSeconds().toString().padStart(2,'0')}`;
  }

  if (!state.card._feedItems) state.card._feedItems = [];
  if (!state.card._feedItemCaseIds) state.card._feedItemCaseIds = new Set();

  let appendedThisBatch = 0;
  for (const r of results) {
    // dedup by case_id — 避免 backfill 與 SSE 事件 race 造成同一筆判決重複顯示
    if (r.case_id && state.card._feedItemCaseIds.has(r.case_id)) continue;
    if (r.case_id) state.card._feedItemCaseIds.add(r.case_id);

    const score = r.score ?? '—';
    const scoreColor = score >= 7 ? 'text-seal' : score >= 1 ? 'text-warm-500' : 'text-warm-400';
    const caseShort = (r.case_id || '').replace(/.*?(\d+年度\S+字第\d+號).*/, '$1') || r.case_id || '';
    const matchIcon = r.match === 'error' ? '✕' : score > 0 ? '●' : '○';
    const matchColor = r.match === 'error' ? 'text-red-400' : score > 0 ? 'text-seal' : 'text-warm-400';
    const elapsedTag = elapsed !== null ? `<span class="text-warm-400 ml-auto shrink-0">${elapsed}s</span>` : '';

    const rowHtml = `
      <span class="${matchColor}">${matchIcon}</span>
      <span class="${scoreColor} w-5 text-right shrink-0">${score}</span>
      <span class="text-ink truncate">${escHtml(caseShort)}</span>
      ${elapsedTag}`;
    state.card._feedItems.push(rowHtml);
    appendedThisBatch++;

    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 text-[11px] font-mono fade-in';
    row.innerHTML = rowHtml;
    feed.appendChild(row);
  }
  // 校正 count：只算實際新加的（避免 dedup 後計數膨脹）
  if (appendedThisBatch !== (results?.length || 0)) {
    state.card._feedItemCount = state.card._feedItemCount - (results.length - appendedThisBatch);
    const countEl = document.getElementById('card-live-count');
    if (countEl) countEl.textContent = state.card._feedItemCount;
  }

  // 自動捲到底部
  feed.scrollTop = feed.scrollHeight;
}

function updateCardProgress(pct, label, eta) {
  state.card.progress = pct;
  markProgressReceived();  // 告知 stuck 偵測：剛有進度更新
  if (!state.card.open || state.card.state !== 'b') return;
  document.getElementById('card-progress-fill').style.width = Math.min(100, pct) + '%';
  if (label) document.getElementById('card-progress-label').textContent = label;
  if (eta)   document.getElementById('card-progress-eta').textContent = eta;
  // 進度更新就清掉 stuck 警告（下次 poll 會依 age 重新判斷）
  document.getElementById('card-stuck-warn')?.remove();
}

// 分析過程即時 token / 成本 ticker — 律師看當下累計、避免「跑完才知花多少」
// Haiku pricing 跟 runner.py 同步：$0.80/MTok input、$4.00/MTok output
function updateCardTokenTicker(usage) {
  if (!usage || !state.card.open || state.card.state !== 'b') return;
  const sIn  = usage.scoring_input  || 0;
  const sOut = usage.scoring_output || 0;
  const total = sIn + sOut;
  if (total === 0) return;
  const usd = (sIn * 0.80 + sOut * 4.00) / 1_000_000;
  const ntd = (usd * 32).toFixed(2);
  let el = document.getElementById('card-token-ticker');
  if (!el) {
    const labelEl = document.getElementById('card-progress-label');
    const parent = labelEl?.parentElement;
    if (!parent) return;
    el = document.createElement('div');
    el.id = 'card-token-ticker';
    el.className = 'mt-2 text-[11px] font-mono text-warm-400 tabular-nums';
    parent.appendChild(el);
  }
  el.innerHTML = `已用 <span class="text-ink">${(total / 1000).toFixed(1)}K</span> tokens · ` +
                 `約 <span class="text-ink">US$${usd.toFixed(3)}</span>（NT$${ntd}）`;
}

// Abort button — 依當下 phase 分流：
//   fetch 階段          → disruptive kill-worker、回 State A（沒結果可保留）
//   scoring / < 3 筆    → disruptive kill-worker、回 State A（保留 < 3 筆無 synthesis 價值）
//   scoring / ≥ 3 筆    → graceful /abort、等 SSE stage3_partial_done → State C 看 partial
//   synth 階段          → 不該能中止（進度條已 90%+、幾秒內就結束）、但保險還是 disruptive
document.getElementById('card-abort-analyze').addEventListener('click', async () => {
  const taskId = state.card.taskId;
  const analysisId = state.card.analysisId;
  if (!taskId || !analysisId) return;
  if (_canGracefulAbort()) {
    await _startGracefulAbort(taskId, analysisId);
  } else {
    await _startDisruptiveAbort(taskId);
  }
});

// 判定：現在這階段按中止是否會產生可查看的 partial synthesis
// scoring 階段 + 實際「命中」≥ 3 筆才走 graceful 路徑；命中 < 3 跑 synthesis 也
// 產不出有用結果（會得到「精讀後沒有判決有論述此問題」的空 partial），走 disruptive。
function _canGracefulAbort() {
  const info = state.bell.tasks.get(state.card.taskId);
  const phase = info?.progressPhase;
  const matchCount = state.card._matchCount || 0;
  return (phase === 'read' || phase === 'screen') && matchCount >= 3;
}

// Graceful abort — 保留已分析的 ≥3 筆命中、立即進 partial synthesis、切 State C
// Backend /abort 收到後 fire-and-forget 背景跑 synthesis（fast path）。律師體感：
//   按下中止 → 按鈕立刻變「AI 綜合分析中…」→ 5-15 秒後切 State C 看結果。
// Scoring 那邊 in-flight Claude call 繼續跑但對律師透明（scoring-end 會 detect
// synthesis 已寫 → skip 重複）。API key 透過 x-api-key header 傳給 synthesis call。
async function _startGracefulAbort(taskId, analysisId) {
  const btn = document.getElementById('card-abort-analyze');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'AI 綜合分析中…';
    btn.classList.add('opacity-60');
  }
  try {
    const r = await apiFetch(`/api/tasks/${taskId}/analyses/${analysisId}/abort`, { method: 'POST' });
    if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
    // Backend 回 { fast_synthesis: true } → 已在背景跑 synthesis、等 stage3_partial_done SSE
  } catch (err) {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('opacity-60');
      _updateAbortButtonLabel();
    }
    alert(`中止失敗：${err.message}`);
  }
}

// Disruptive abort — DELETE /fetch-judgments（fetch phase cooperative cancel）+
// POST /kill-worker（asyncio.cancel worker task）、立即回 State A
// fetch 階段用：cancel flag 優雅處理 in-flight fetch；scoring < 3 筆用：沒結果可保留
async function _startDisruptiveAbort(taskId) {
  const btn = document.getElementById('card-abort-analyze');
  if (btn) { btn.disabled = true; btn.textContent = '中止中…'; }
  try {
    await apiFetch(`/api/tasks/${taskId}/fetch-judgments`, { method: 'DELETE' });
  } catch {}
  try {
    await apiFetch(API.killWorker(taskId), { method: 'POST' });
  } catch (err) {
    console.warn('[kill-worker] 失敗:', err);
  }
  _clearAbortUI();
  state.bell.tasks.delete(taskId);
  renderNotificationBell();
  setCardState('a');
  const submitBtn = document.getElementById('card-submit-analyze');
  if (submitBtn) {
    submitBtn.disabled = false;
    submitBtn.textContent = 'AI　分　析';
    submitBtn.classList.remove('opacity-50');
  }
}

// 清理中止中的 UI（timer + 強制結束按鈕）
function _clearAbortUI() {
  if (state.card._abortTickTimer) {
    clearInterval(state.card._abortTickTimer);
    state.card._abortTickTimer = null;
  }
  const forceBtn = document.getElementById('card-force-abort');
  if (forceBtn) forceBtn.remove();
}

// 中止按鈕動態文案：
//   scoring + doneJudg ≥ 3 → 「中止並查看目前結果」（graceful、會切 State C）
//   其他（fetch / scoring < 3 筆 / synth）→ 「中止分析」（disruptive、直接回 State A）
function _updateAbortButtonLabel() {
  const btn = document.getElementById('card-abort-analyze');
  if (!btn || btn.classList.contains('hidden')) return;
  if (btn.disabled) return;  // 中止進行中時不覆寫 timer label
  btn.textContent = _canGracefulAbort() ? '中止並查看目前結果' : '中止分析';
}

// ─── State B: Error (analysis failed) ─────────────────

function renderCardError(errorMsg) {
  // 將 State B 的進度條替換為錯誤訊息 + 重試按鈕
  document.getElementById('card-progress-fill').style.width = '100%';
  document.getElementById('card-progress-fill').classList.remove('bg-seal');
  document.getElementById('card-progress-fill').classList.add('bg-red-400');
  document.getElementById('card-progress-label').textContent = '分析失敗';
  document.getElementById('card-progress-eta').innerHTML = `
    <span class="text-red-600 text-xs">${escHtml(errorMsg)}</span>
    <button onclick="retryCurrentAnalysis()" class="mt-3 px-4 py-1.5 text-xs font-mono border border-seal text-seal
           hover:bg-seal/10 transition-colors rounded-sm">
      重新分析
    </button>
  `;
  // 隱藏「中止分析」按鈕：已經 failed 沒東西可中止，且按了會 setCardState('a') 把
  // 使用者丟回篩選頁，那裡的「開始分析」按鈕又會引誘他們誤觸發。
  const abortBtn = document.getElementById('card-abort-analyze');
  if (abortBtn) abortBtn.classList.add('hidden');
}

async function retryCurrentAnalysis() {
  const taskId = state.card.taskId;
  const analysisId = state.card.analysisId;
  if (!taskId || !analysisId) return;
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/analyses/${analysisId}/retry`, { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    // 重置 UI
    const a = state.analyses.find(x => x.id === analysisId);
    if (a) { a.status = 'pending'; a.completed = 0; a.match_count = 0; }
    // 恢復進度條顏色 + 把「中止分析」按鈕再顯示回來（renderCardError 時被藏起來）
    document.getElementById('card-progress-fill').classList.remove('bg-red-400');
    document.getElementById('card-progress-fill').classList.add('bg-seal');
    const abortBtn = document.getElementById('card-abort-analyze');
    if (abortBtn) abortBtn.classList.remove('hidden');
    renderCardProgress(a?.question || '');
    // 重新訂閱 SSE
    subscribeBellTask(taskId);
    const _retryTask = state.tasks.find(x => x.id === taskId);
    state.bell.tasks.set(taskId, {
      status: 'running', progress: 0, keyword: _retryTask ? getTaskOrigKw(_retryTask) : '',
      analysisId, unread: false, question: a?.question || '',
    });
    renderNotificationBell();
  } catch (err) {
    alert(`重試失敗：${err.message}`);
  }
}

async function retryAnalysis(taskId, analysisId) {
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/analyses/${analysisId}/retry`, { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const a = state.analyses.find(x => x.id === analysisId);
    if (a) { a.status = 'pending'; a.completed = 0; a.match_count = 0; }
    subscribeBellTask(taskId);
    const _retryTask = state.tasks.find(x => x.id === taskId);
    state.bell.tasks.set(taskId, {
      status: 'running', progress: 0, keyword: _retryTask ? getTaskOrigKw(_retryTask) : '',
      analysisId, unread: false, question: a?.question || '',
    });
    renderNotificationBell();
    renderAnalysisHistoryTabs();
  } catch (err) {
    alert(`重試失敗：${err.message}`);
  }
}

// ─── State C: Results ───────────────────────────────

const CARD_PAGE_SIZE = 30;

async function renderCardResults(analysisId) {
  state.card.analysisId = analysisId;
  state.card.resultsOffset = 0;
  state.card.activeCluster = null;
  state.card.sortBy = 'score';

  // Reload analyses to get latest synthesis
  await reloadAnalyses();
  const analysis = state.analyses.find(a => a.id === analysisId);
  if (!analysis) return;

  // Fetch all results (no pagination for initial load — we page client-side)
  const url = `${API.judgments(state.card.taskId)}?primary_analysis_id=${analysisId}`;
  const res = await apiFetch(url);
  const allResults = res.ok ? await res.json() : [];

  // 分流：score>0 相關 / score=0+match≠data_error 無關 / data_error 資料異常
  const relevant = allResults.filter(r => (r.primary_score ?? 0) > 0)
    .sort((a, b) => (b.primary_score ?? 0) - (a.primary_score ?? 0));
  const irrelevant = allResults.filter(r => (r.primary_score ?? 0) === 0 && r.primary_match !== 'data_error');
  const dataErrors = allResults.filter(r => r.primary_match === 'data_error');
  state.card.allResults = relevant;
  state.card.irrelevantResults = irrelevant;
  state.card.dataErrorResults = dataErrors;

  // Synthesis card
  let synth = null;
  try { synth = analysis.synthesis ? JSON.parse(analysis.synthesis) : null; } catch {}
  // 非終版 banner 判斷（修 2026-04-19 bug：failed/cancelled/done 不該顯示「仍在分析」）：
  // is_preliminary=1 + status ∈ {running, partial} 才是真正「非終版」
  const bannerInfo = (
    analysis.synthesis_is_preliminary &&
    (analysis.status === 'running' || analysis.status === 'partial')
  ) ? {
    status: analysis.status,
    completed: analysis.completed || 0,
    total: analysis.total || 0,
    match_count: analysis.match_count || 0,
  } : null;
  renderCardSynthesis(synth, bannerInfo);

  // Clusters
  state.card.clusters = synth?.clusters || [];
  renderCardClusterTabs();

  // Keyword chips（State C：放 header，節省左欄空間）
  const task = state.tasks.find(t => t.id === state.card.taskId);
  const origKw = getTaskOriginalKeyword(task);
  const kwChipEl = document.getElementById('card-header-keyword-chip');
  if (kwChipEl) {
    kwChipEl.innerHTML = origKw.split(/\s+/).filter(Boolean).map(k => `
      <span class="flex items-center gap-1.5 px-2 py-0.5 rounded-sm border
                   border-warm-300 bg-warm-100 text-warm-600 text-xs cursor-default">
        <span class="font-serif">${escHtml(k)}</span>
      </span>
    `).join('');
    kwChipEl.classList.remove('hidden');
  }

  // User question
  document.getElementById('card-results-question').textContent = analysis.question || '';
  // 分析範圍 meta（N 筆判決 · 欄位）
  const scopeEl = document.getElementById('card-results-scope');
  if (scopeEl) {
    const total = (analysis.total != null ? analysis.total : state.card.allResults.length)
      + (state.card.irrelevantResults?.length || 0);
    const fields = (analysis.ai_read_field || '').split(',').filter(Boolean);
    const fieldLabel = { reasoning: '理由', main_text: '主文', facts: '事實',
                         cited_statutes: '引用法條', full_text: '全文' };
    const fieldTxt = fields.length ? fields.map(f => fieldLabel[f] || f).join('、') : '理由';
    scopeEl.textContent = `分析範圍：${total} 筆判決 · 僅${fieldTxt}`;
  }

  // Analysis history tabs
  renderAnalysisHistoryTabs();

  // Results list (first page)
  renderCardResultsPage(true);

  // Header stats 要在 allResults 填好後重算（setCardState 可能早於此）
  updateCardHeaderStats('c');

  // Update header
  const skipped = state.card.fetchSkipped || 0;
  const skippedSuffix = skipped ? `　⚠ ${skipped} 筆下載失敗未分析` : '';
  document.getElementById('card-header-text').innerHTML =
    '分析結果'
    + (skippedSuffix ? `<span class="text-amber-600 text-xs font-normal ml-2">${skippedSuffix}</span>` : '');

  // Empty state
  document.getElementById('card-results-empty').classList.toggle('hidden', relevant.length > 0);
}

// LLM 摘要常含 markdown bold 標記（`**xxx**`），輕量 inline 渲染為 <strong>。
// 流程：先 escHtml 防 XSS（synthesis 內容來自 Claude 可能含 < > & 特殊字元），
// 再對轉義後文本做 ** 配對替換。`*` 字元不是 HTML 特殊字元、escape 後仍存在
function _renderInlineMarkdown(text) {
  if (!text) return '';
  return escHtml(text).replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>');
}

function renderCardSynthesis(synth, bannerInfo = null) {
  const consensusEl = document.getElementById('card-synth-consensus');
  const summaryEl   = document.getElementById('card-synth-summary');
  // 非終版 banner：bannerInfo 非 null 表示當下 synthesis 是 preliminary 或 partial
  // 兩變形依 status 分流：
  //   running → 「初步結果、仍在分析剩餘 N 筆」+ [就用現在結果定稿]
  //   partial → 「已中止於 X/Y（命中 K）」+ [繼續未完成的分析] + [就用現在結果定稿]
  // status ∈ {failed, cancelled, done} 一律不顯示（caller 在 bannerInfo 判斷時已剔除）
  const synthContainer = document.getElementById('card-synthesis');
  let banner = document.getElementById('card-synth-preliminary-banner');
  if (bannerInfo) {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'card-synth-preliminary-banner';
      banner.className = 'mb-3 flex items-center gap-3 px-3 py-2 rounded-sm border border-amber-300 bg-amber-50 text-[12px]';
      synthContainer.insertBefore(banner, synthContainer.firstChild);
    }
    const doneJudg = bannerInfo.completed || 0;
    const totalJudg = bannerInfo.total || 0;
    const twoPass = totalJudg >= 40;
    const doneJ = twoPass ? Math.floor(doneJudg / 2) : doneJudg;
    const totalJ = twoPass ? Math.floor(totalJudg / 2) : totalJudg;
    const remaining = Math.max(0, totalJ - doneJ);
    const matchC = bannerInfo.match_count || 0;

    if (bannerInfo.status === 'partial') {
      banner.innerHTML = `
        <span class="font-mono text-[10px] uppercase tracking-widest text-amber-700 shrink-0">已中止</span>
        <span class="text-warm-600 flex-1">停於 <span class="font-mono text-ink">${doneJ}</span> / <span class="font-mono text-ink">${totalJ}</span>（命中 <span class="font-mono text-ink">${matchC}</span>）· 尚有 <span class="font-mono text-ink">${remaining}</span> 筆未分析</span>
        <button id="card-synth-resume-btn"
          class="font-mono text-[11px] text-seal hover:text-ink border border-seal hover:border-ink px-2 py-1 rounded-sm transition-colors shrink-0">
          繼續未完成的分析 →
        </button>
        <button id="card-synth-finalize-btn"
          class="font-mono text-[11px] text-warm-600 hover:text-ink border border-warm-400 hover:border-ink px-2 py-1 rounded-sm transition-colors shrink-0">
          就用現在結果定稿 ✓
        </button>`;
      const resumeBtn = document.getElementById('card-synth-resume-btn');
      if (resumeBtn) resumeBtn.addEventListener('click', handleResumeAnalysis);
    } else {
      // status === 'running'
      // Transient 情境：/resume 剛設 status=running、但 worker 還沒跑到 run_analysis_v2
      //   → total 是舊值、remaining <= 0。顯示「任務重開中…」避免「仍在分析剩餘 0 筆」誤導
      const initializing = (doneJ >= totalJ);
      const bodyText = initializing
        ? '任務重開中，正在準備分析剩餘判決…'
        : `仍在分析剩餘 <span class="font-mono text-ink">${remaining}</span> 筆，完成後自動更新為最終版`;
      banner.innerHTML = `
        <span class="font-mono text-[10px] uppercase tracking-widest text-amber-700 shrink-0">${initializing ? '重開中' : '初步結果'}</span>
        <span class="text-warm-600 flex-1">${bodyText}</span>
        <button id="card-synth-view-progress-btn"
          class="font-mono text-[11px] text-warm-600 hover:text-ink border border-warm-400 hover:border-ink px-2 py-1 rounded-sm transition-colors shrink-0">
          查看進度 →
        </button>
        <button id="card-synth-finalize-btn"
          class="font-mono text-[11px] text-seal hover:text-ink border border-seal hover:border-ink px-2 py-1 rounded-sm transition-colors shrink-0">
          就用現在結果定稿 ✓
        </button>`;
      const progressBtn = document.getElementById('card-synth-view-progress-btn');
      if (progressBtn) progressBtn.addEventListener('click', () => {
        // 從 state.analyses 找當下 analysis、重新初始化 State B 畫面
        // 否則 bellInfo.progressPhase / _lastFeedTime / _feedItemCount 會是進 State C 前的舊值、
        // 顯示成「準備中…」+「LLM 已 XXX 秒未回應」+「即時回傳 0 筆」
        const a = (state.analyses || []).find(x => x.id === state.card.analysisId);
        renderCardProgress(a?.question || '');
        setCardState('b');
      });
    }
    const finalizeBtn = document.getElementById('card-synth-finalize-btn');
    if (finalizeBtn) finalizeBtn.addEventListener('click', handleFinalizePreliminary);
  } else if (banner) {
    banner.remove();
  }

  if (!synth) {
    consensusEl.textContent = '—';
    consensusEl.className = 'font-mono text-xs text-warm-400';
    summaryEl.innerHTML = '';
    return;
  }
  const meta = CONSENSUS_LABEL[synth.consensus] || CONSENSUS_LABEL['不足'];
  consensusEl.className = `font-mono text-xs px-1.5 py-0.5 rounded-sm ${meta.cls}`;
  consensusEl.textContent = meta.text;

  // synthesis 失敗降級：只看 _fallback 標記（不看 summary 內容，避免摘要中含「失敗」字樣誤觸）
  if (synth._fallback) {
    summaryEl.innerHTML = `
      <span class="block">${_renderInlineMarkdown(synth.summary || '')}</span>
      <button onclick="reSynthesis()" class="mt-2 px-3 py-1 text-xs font-mono border border-seal text-seal
             hover:bg-seal/10 transition-colors rounded-sm">
        重新生成摘要（不重跑分析）
      </button>`;
  } else {
    // LLM answer 常含 **bold** markdown，render 為 <strong>
    summaryEl.innerHTML = _renderInlineMarkdown(synth.summary || '');
  }

  // Token 用量與成本顯示
  const usage = synth._usage;
  if (usage && usage.total_cost_usd > 0) {
    const totalTokens = (usage.scoring_input || 0) + (usage.scoring_output || 0)
                      + (usage.synthesis_input || 0) + (usage.synthesis_output || 0);
    const costNTD = (usage.total_cost_usd * 32).toFixed(1);
    // 用固定 ID 避免重複 appendChild（re-render 時覆蓋而非累積）
    const parent = summaryEl.parentElement;
    let costTag = parent.querySelector('#card-usage-tag');
    if (!costTag) {
      costTag = document.createElement('div');
      costTag.id = 'card-usage-tag';
      parent.appendChild(costTag);
    }
    costTag.className = 'mt-2 text-[10px] font-mono text-warm-400';
    costTag.textContent = `本次消耗 ${(totalTokens/1000).toFixed(1)}K tokens · 約 US$${usage.total_cost_usd.toFixed(3)}（NT$${costNTD}）`;
  }
}

async function handleFinalizePreliminary() {
  const taskId = state.card.taskId;
  const analysisId = state.card.analysisId;
  if (!taskId || !analysisId) return;
  const btn = document.getElementById('card-synth-finalize-btn');
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '定稿中…'; }
  try {
    const r = await apiFetch(`/api/tasks/${taskId}/analyses/${analysisId}/finalize`, { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    // 依 is_final 分流（雙保險設計）：
    //   is_final=true  → backend 已 DB 升格（partial / failed / stuck 路徑）→ 立刻 reload + re-render
    //   is_final=false → backend 只 set flag（running 路徑）→ 等 SSE、5 秒後 fallback check
    if (data.is_final) {
      await reloadAnalyses();
      await renderCardResults(analysisId);
      renderAnalysisHistoryTabs();
    } else {
      // 5 秒 fallback：若 SSE 還沒到但 DB 已升格，主動 re-render；否則繼續等 SSE（不 hard fail）
      setTimeout(async () => {
        try {
          await reloadAnalyses();
          const a = state.analyses.find(x => x.id === analysisId);
          if (a && a.status === 'done' && !a.synthesis_is_preliminary) {
            await renderCardResults(analysisId);
            renderAnalysisHistoryTabs();
          }
        } catch {}
      }, 5000);
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = origText || '就用現在結果定稿 →'; }
    alert('定稿失敗：' + e.message);
  }
}

async function handleResumeAnalysis() {
  const taskId = state.card.taskId;
  const analysisId = state.card.analysisId;
  if (!taskId || !analysisId) return;
  const btn = document.getElementById('card-synth-resume-btn');
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '續跑中…'; }
  try {
    const r = await apiFetch(`/api/tasks/${taskId}/analyses/${analysisId}/resume`, { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
    // Resume 成功 → status=running、worker 已 dispatch、SSE 會在 batch_done 陸續到達
    // 確保 bell SSE 訂閱（可能在 partial 階段被關閉）
    subscribeBellTask(taskId);
    // 重新 render synthesis 區（fetch 最新 analysis，此時 status=running + synthesis 存在
    // → banner 會切成 running-prelim 變形）
    await reloadAnalyses();
    // state.tasks 也刷新 — 任務卡片上的 phaseText 依 state.tasks[].analyses[].status 判斷、
    // 若不同步刷會停留在「已中止 · 待繼續或定稿」即使已 resume
    try {
      const tasksRes = await apiFetch(API.tasks);
      if (tasksRes.ok) { state.tasks = await tasksRes.json(); renderTaskLists(); }
    } catch {}
    const a = state.analyses.find(x => x.id === analysisId);
    if (a?.synthesis) {
      try {
        const synth = JSON.parse(a.synthesis);
        const bannerInfo = (a.synthesis_is_preliminary && a.status === 'running')
          ? {
              status: 'running',
              completed: a.completed || 0,
              total: a.total || 0,
              match_count: a.match_count || 0,
            }
          : null;
        renderCardSynthesis(synth, bannerInfo);
      } catch {}
    }
    renderAnalysisHistoryTabs();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = origText || '繼續未完成的分析 →'; }
    alert('續跑失敗：' + e.message);
  }
}

async function reSynthesis() {
  const taskId = state.card.taskId;
  const analysisId = state.card.analysisId;
  if (!taskId || !analysisId) return;

  // 把整個 synthesis 卡片換成 loading 狀態
  const summaryEl = document.getElementById('card-synth-summary');
  const consensusEl = document.getElementById('card-synth-consensus');
  const prevSummary = summaryEl?.innerHTML;
  const prevConsensus = consensusEl?.textContent;
  if (consensusEl) { consensusEl.textContent = '生成中'; consensusEl.className = 'font-mono text-xs text-warm-400'; }
  if (summaryEl) summaryEl.innerHTML = `
    <div class="flex items-center gap-2 py-2">
      <span class="pulse-dot w-1.5 h-1.5 rounded-full bg-seal inline-block"></span>
      <span class="text-sm font-serif text-warm-500">正在重新生成摘要與智慧分類…</span>
    </div>`;

  try {
    const res = await apiFetch(`/api/tasks/${taskId}/analyses/${analysisId}/re-synthesis`, { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    await reloadAnalyses();
    const analysis = state.analyses.find(a => a.id === analysisId);
    if (analysis?.synthesis) {
      try {
        const synth = JSON.parse(analysis.synthesis);
        renderCardSynthesis(synth);
        state.card.clusters = synth.clusters || [];
        renderCardClusterTabs();
      } catch {}
    }
  } catch (err) {
    alert(`重新生成失敗：${err.message}`);
    // 恢復原內容
    if (summaryEl) summaryEl.innerHTML = prevSummary || '';
    if (consensusEl) consensusEl.textContent = prevConsensus || '';
  }
}

function countClusterMatches(cluster) {
  // 用跟 getCardVisibleResults 相同的模糊匹配邏輯計算實際命中數
  if (!cluster?.case_ids || cluster.case_ids.length === 0) return 0;
  return state.card.allResults.filter(r => {
    const rid = r.case_id || '';
    return cluster.case_ids.some(cid =>
      rid === cid || rid.includes(cid) || cid.includes(rid)
    );
  }).length;
}

function renderCardClusterTabs() {
  const tabsEl = document.getElementById('card-cluster-tabs');
  const sectionEl = document.getElementById('card-cluster-section');
  const clusters = state.card.clusters || [];
  const totalRelevant = state.card.allResults.length;
  const activeIdx = state.card.activeCluster;
  const starredCount = state.card.allResults.filter(r => state.starred.has(r.case_id)).length;

  // 過濾掉實際匹配為 0 的 cluster
  const validClusters = clusters.map((c, i) => ({ ...c, _idx: i, _count: countClusterMatches(c) }))
    .filter(c => c._count > 0);

  // 至少要有 clusters 或有 starred 才顯示
  if (validClusters.length === 0 && starredCount === 0) {
    if (sectionEl) sectionEl.style.display = 'none';
    return;
  }
  if (sectionEl) sectionEl.style.display = '';

  let html = `<button class="cluster-tab ${activeIdx == null ? 'active' : ''}"
                data-cluster-idx="all">全部 ${totalRelevant}</button>`;
  // 使用者標記 tab — 純切換 cluster，不挾帶下載按鈕（下載按鈕另以
  // data-action="download-starred" 的 icon 按鈕內嵌在 tab 右側角落，
  // 跟 cluster 視覺分離、不會「彈出」破壞 cluster 列穩定性）
  if (starredCount > 0) {
    const isStarredActive = activeIdx === 'starred';
    html += `<button class="cluster-tab ${isStarredActive ? 'active' : ''}"
               data-cluster-idx="starred">
      <span class="inline-flex items-center gap-1">
        <svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24"
             fill="currentColor" stroke="currentColor" stroke-width="2">
          <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
        </svg>
        使用者標記 ${starredCount}
      </span>${isStarredActive ? `
        <a data-action="download-starred" title="下載全部標記判決 PDF"
           class="ml-2 inline-flex items-center justify-center w-5 h-5 rounded-sm hover:bg-seal/20 text-seal transition-colors">
          <svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
        </a>` : ''}
    </button>`;
  }
  validClusters.forEach(c => {
    html += `<button class="cluster-tab ${activeIdx === c._idx ? 'active' : ''}"
               data-cluster-idx="${c._idx}">${escHtml(c.label)} ${c._count}</button>`;
  });
  // 無關判決 + 資料異常 tab（不顯眼，放最後）
  const irrelevantCount = (state.card.irrelevantResults || []).length;
  const dataErrorCount = (state.card.dataErrorResults || []).length;
  if (irrelevantCount > 0) {
    html += `<button class="cluster-tab ${activeIdx === 'irrelevant' ? 'active' : ''}"
               data-cluster-idx="irrelevant"
               style="opacity:0.5;border-style:dashed">無關 ${irrelevantCount}</button>`;
  }
  // 資料異常不顯示 tab（隱藏）
  tabsEl.innerHTML = html;

  tabsEl.querySelectorAll('.cluster-tab').forEach(btn => {
    btn.addEventListener('click', (e) => {
      // 內嵌的「下載 PDF」icon — 跳過 cluster 切換、執行下載
      if (e.target.closest('[data-action="download-starred"]')) {
        e.preventDefault();
        e.stopPropagation();
        downloadStarredPdfs();
        return;
      }
      const idx = btn.dataset.clusterIdx;
      state.card.activeCluster = idx === 'all' ? null : (['starred','irrelevant','data_error'].includes(idx) ? idx : parseInt(idx, 10));
      state.card.resultsOffset = 0;
      // 律師切回「全部」→ 若有 pending final refresh 就在這裡套用
      if (state.card.activeCluster === null && state.card._pendingFinalRefresh) {
        _applyPendingFinalRefresh();
        return;
      }
      renderCardClusterTabs();
      renderCardResultsPage(true);
    });
  });
}

function getCardVisibleResults() {
  if (state.card.activeCluster === 'irrelevant') {
    return [...(state.card.irrelevantResults || [])];
  }
  if (state.card.activeCluster === 'data_error') {
    return [...(state.card.dataErrorResults || [])];
  }
  let results = [...state.card.allResults];
  // Filter by cluster or starred
  if (state.card.activeCluster === 'starred') {
    results = results.filter(r => state.starred.has(r.case_id));
  } else if (state.card.activeCluster != null) {
    const cluster = state.card.clusters[state.card.activeCluster];
    if (cluster?.case_ids && cluster.case_ids.length > 0) {
      results = results.filter(r => {
        const rid = r.case_id || '';
        return cluster.case_ids.some(cid =>
          rid === cid || rid.includes(cid) || cid.includes(rid)
        );
      });
    }
  }
  // Sort
  if (state.card.sortBy === 'date') {
    results.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
  } else {
    results.sort((a, b) => (b.primary_score ?? 0) - (a.primary_score ?? 0));
  }
  return results;
}

// Header 右上「已分析 N 筆 · N 命中」 — setCardState 與 renderCardResults 都會呼叫
function updateCardHeaderStats(s) {
  s = s || state.card.state;
  const statsEl = document.getElementById('card-header-stats');
  if (!statsEl) return;
  if (s !== 'c') { statsEl.classList.add('hidden'); return; }
  const relevant = (state.card.allResults || []).length;
  const irrelevant = (state.card.irrelevantResults || []).length;
  const analyzed = relevant + irrelevant;
  statsEl.textContent = `已分析 ${analyzed} 筆 · ${relevant} 命中`;
  statsEl.classList.remove('hidden');
}

function updateCardSortButtons() {
  const scoreBtn = document.getElementById('card-sort-score');
  const dateBtn  = document.getElementById('card-sort-date');
  if (!scoreBtn || !dateBtn) return;
  const activeScore = state.card.sortBy === 'score';
  scoreBtn.classList.toggle('result-sort-active', activeScore);
  dateBtn.classList.toggle('result-sort-active', !activeScore);
}

// 從 case_id 解析出可讀的字號顯示
// 支援格式化名稱（「臺灣高等法院民事裁定　96年度抗字第1054號」）和 JID（「TPBA,112,全,57,20231116,1」）
function parseCaseDisplay(rawCaseId) {
  const s = (rawCaseId || '').replace(/[\u3000\s]+/g, ' ').trim();

  // 格式 1：格式化名稱 → 抓「N年度X字第M號」
  const numMatch = s.match(/(\d+年度\S+字第\d+號)/);
  if (numMatch) {
    const caseNum = numMatch[1];
    const typeMatch = s.match(/(民事|刑事|行政|懲戒)(判決|裁定)/);
    const caseType = typeMatch ? typeMatch[0] : '';
    return { display: caseNum + caseType, caseNum, caseType };
  }

  // 格式 2：JID（TPBA,112,全,57,20231116,1）→ 轉成「112年度全字第57號」
  const jidMatch = s.match(/^[A-Z]+,(\d+),([^,]+),(\d+),/);
  if (jidMatch) {
    const display = `${jidMatch[1]}年度${jidMatch[2]}字第${jidMatch[3]}號`;
    return { display, caseNum: display, caseType: '' };
  }

  return { display: s, caseNum: '', caseType: '' };
}

function renderCardResultsPage(reset = false) {
  const results = getCardVisibleResults();
  const listEl  = document.getElementById('card-results-list');
  const countEl = document.getElementById('card-results-count');
  const moreEl  = document.getElementById('card-load-more-wrap');
  const emptyEl = document.getElementById('card-results-empty');

  if (reset) {
    listEl.innerHTML = '';
    state.card.resultsOffset = 0;
  }

  countEl.textContent = `${results.length} 筆相關判決`;
  emptyEl.classList.toggle('hidden', results.length > 0);
  updateCardSortButtons();

  const start = state.card.resultsOffset;
  const page = results.slice(start, start + CARD_PAGE_SIZE);
  state.card.resultsOffset = start + page.length;

  // Store results in a lookup for click handler (avoids encoding case_id in HTML attributes)
  if (reset) state.card._resultLookup = {};
  const lookup = state.card._resultLookup || {};

  const html = page.map((r, i) => {
    const idx = start + i;
    lookup[idx] = r;
    const excerpt = r.primary_excerpt ?? r.excerpt ?? '';
    const excerptDisplay = excerpt ? excerpt.slice(0, 150) : '';
    const parsed = parseCaseDisplay(r.case_id);
    const isRead = state.card.readCaseIds.has(r.case_id);
    const isStarred = state.starred.has(r.case_id);
    const readCls = isRead ? 'opacity-60' : '';
    const starHtml = isStarred ? '<span class="text-amber-500 text-xs ml-1" title="已標記">★</span>' : '';
    // direction badge：從 reason 的 [支持]/[反對] prefix 解析
    // interpretation mode 固定無 direction、不顯示 badge
    const reason = r.primary_reason ?? r.reason ?? '';
    const dirMatch = reason.match(/^\[(支持|反對|中性)\]/);
    const _taskDomain = _task_search_domain();
    const dir = _taskDomain === 'interpretation' ? '' : (dirMatch ? dirMatch[1] : '');
    const dirBadge = dir === '支持' ? '<span class="text-[10px] font-mono px-1 py-0.5 rounded-sm bg-emerald-50 text-emerald-700 border border-emerald-200">支持</span>'
      : dir === '反對' ? '<span class="text-[10px] font-mono px-1 py-0.5 rounded-sm bg-red-50 text-red-600 border border-red-200">反對</span>'
      : '';
    // interpretation mode 顯示規則：
    //   court label = 「司法院」（而非 task_judgments.court 的「憲法法庭」）
    //   link text = 去「司法院」前綴的「釋字第N號」/「XXX年憲判字第N號」
    // 其他（普通判決）：照原樣
    const isInterpRow = /^司法院釋字/.test(r.case_id) || /^釋字/.test(r.case_id);
    const courtDisplay = isInterpRow ? '司法院' : (r.court || '');
    const linkDisplay = isInterpRow
      ? parsed.display.replace(/^司法院/, '')
      : parsed.display;
    const dateDisplay = r.date || '';
    const brokenData = !courtDisplay && !dateDisplay;
    const brokenBadge = brokenData ? '<span class="text-[10px] font-mono px-1 py-0.5 text-warm-400 border border-warm-200 rounded-sm">資料不完整</span>' : '';
    return `
      <div class="card-result-row py-3 border-b border-warm-100 ${readCls}"
           data-result-idx="${idx}" data-case-id="${escAttr(r.case_id)}">
        <div class="flex items-baseline gap-2 mb-0.5">
          ${dirBadge}${brokenBadge}
          <span class="font-serif text-sm font-semibold text-ink">${escHtml(courtDisplay)}</span>
          <span class="font-serif text-sm text-seal hover:underline cursor-pointer">${escHtml(linkDisplay)}</span>
          <span class="font-mono text-xs text-warm-400 ml-auto shrink-0">${escHtml(dateDisplay)}${starHtml}</span>
        </div>
        ${excerptDisplay ? `
          <div class="mt-1">
            <p class="text-xs font-serif leading-relaxed line-clamp-3 result-excerpt-box">${escHtml(excerptDisplay)}</p>
          </div>` : (() => {
            // 無 excerpt：顯示 reason（去掉 direction prefix）或 score 資訊
            const cleanReason = reason.replace(/^\[(支持|反對|中性)\]\s*/, '').trim();
            if (cleanReason) return `<div class="mt-1"><p class="text-xs text-warm-500 font-serif leading-relaxed">${escHtml(cleanReason.slice(0, 100))}</p></div>`;
            return '';
          })()}
      </div>`;
  }).join('');

  state.card._resultLookup = lookup;

  if (reset) listEl.innerHTML = html;
  else listEl.insertAdjacentHTML('beforeend', html);

  moreEl.classList.toggle('hidden', state.card.resultsOffset >= results.length);
}

// Load more button (manual click)
document.getElementById('card-load-more').addEventListener('click', () => renderCardResultsPage(false));

// Infinite scroll: 自動載入下一頁（IntersectionObserver 偵測 load-more 按鈕出現在視口內）
try {
  const _moreWrap = document.getElementById('card-load-more-wrap');
  if (_moreWrap) {
    let _loadingMore = false;
    const _scrollRoot = _moreWrap.closest('.overflow-y-auto') || null;
    const _obs = new IntersectionObserver(entries => {
      if (entries[0]?.isIntersecting && !_moreWrap.classList.contains('hidden') && !_loadingMore) {
        _loadingMore = true;
        renderCardResultsPage(false);
        requestAnimationFrame(() => { _loadingMore = false; });
      }
    }, { root: _scrollRoot, threshold: 0.1 });
    _obs.observe(_moreWrap);
  }
} catch (e) { console.warn('IntersectionObserver setup failed:', e); }

// Click delegation for result rows (avoids encoding case_id in onclick attributes)
document.getElementById('card-results-list').addEventListener('click', e => {
  const row = e.target.closest('[data-result-idx]');
  if (!row) return;
  const idx = parseInt(row.dataset.resultIdx, 10);
  const r = state.card._resultLookup?.[idx];
  if (!r) return;
  // 一律從判決頂部開啟（AI 評價區塊在最上方，律師看完評價後可點摘錄框跳對應段）
  openReaderCard(state.card.taskId, r.case_id);
});

// AI 評價區塊裡的「原文摘錄」框 → 點擊跳到判決中對應段落 + flash highlight
// Listener 掛在 #rc-text 而非 document，只在 reader 內容範圍內觸發（P1-3）
// 排除選字情境：若 click 完成時 selection 還有字，視為使用者在拖選 — 不跳
// 跳轉失敗（fuzzyFind 在判決文本中找不到對應段）→ 把 "↘ 跳到原文"
// 換成 "找不到對應段落" 紅色 2 秒，給 user 回饋（P1-2）
document.getElementById('rc-text').addEventListener('click', (e) => {
  const box = e.target.closest('.ai-eval-excerpt');
  if (!box) return;
  const sel = window.getSelection && window.getSelection();
  if (sel && sel.toString().length > 0) return;
  if (!_readerJudgment) return;
  const r = _findAiEvalForCase(_readerJudgment.case_id);
  if (!r) {
    _showExcerptClickFeedback(box, 'AI 評價資料遺失');
    return;
  }
  // 用 raw excerpt（含 [理由] prefix）會讓 fuzzy find 開頭對不上 → 先剝掉
  const raw = r.primary_excerpt ?? r.excerpt ?? '';
  const text = raw.replace(/^\[(理由|主文|事實|引用法條|全文)\]\s*/, '');
  if (!text) {
    _showExcerptClickFeedback(box, '無原文摘錄可跳轉');
    return;
  }
  const ok = scrollToExcerptInReader(text);
  if (!ok) _showExcerptClickFeedback(box, '找不到對應段落');
});

function _showExcerptClickFeedback(box, msg) {
  const hint = box.querySelector('[data-excerpt-hint]');
  if (!hint) return;
  // 用 innerHTML 而非 textContent — 因為 hint 內含 SVG icon，textContent swap 會 strip 掉
  const orig = hint.dataset.origHtml || hint.innerHTML;
  hint.dataset.origHtml = orig;
  // msg 是純文字（程式內 hardcoded，無 user input），直接塞入安全
  hint.textContent = msg;
  hint.classList.add('text-red-600');
  hint.classList.remove('text-warm-400', 'group-hover:text-ink');
  clearTimeout(hint._revertTimer);
  hint._revertTimer = setTimeout(() => {
    hint.innerHTML = orig;
    hint.classList.remove('text-red-600');
    hint.classList.add('text-warm-400', 'group-hover:text-ink');
  }, 2000);
}

// Sort toggle
document.getElementById('card-sort-score').addEventListener('click', () => {
  if (state.card.sortBy === 'score') return;
  state.card.sortBy = 'score';
  renderCardResultsPage(true);
});
document.getElementById('card-sort-date').addEventListener('click', () => {
  if (state.card.sortBy === 'date') return;
  state.card.sortBy = 'date';
  renderCardResultsPage(true);
});

// ─── 重設提問（已整合進追問輸入框，完整模式自動走 State A）──────────
// card-new-question 按鈕已移除，保留 null-safe stub
const _oldNewQ = document.getElementById('card-new-question');
if (_oldNewQ) _oldNewQ.addEventListener('click', () => {
  state.card.searching = false;
  setCardState('a');
  setTimeout(() => document.getElementById('card-question').focus(), 100);
});

// ─── 快速追問：基於既有摘要回答（1 次 Claude call）──────────
{
  const fInput = document.getElementById('card-followup-input');
  const fBtn   = document.getElementById('card-followup-submit');
  const fMode  = document.getElementById('card-followup-mode');
  let _fTimer = null;
  let _fQuickMode = true;

  // 判斷模式（關鍵字比對既有摘要）
  function detectFollowupMode(q) {
    if (!q.trim()) {
      fBtn.disabled = true; fMode.classList.add('hidden'); return;
    }
    fBtn.disabled = false;

    // 從追問提取詞組，比對既有 results 的 reason+excerpt
    const terms = q.replace(/[，。？！、；：「」（）\s]+/g, ' ').trim().split(/\s+/).filter(t => t.length >= 2);
    if (!terms.length) { _fQuickMode = true; updateModeDisplay(); return; }

    const corpus = (state.card.allResults || []).map(r =>
      (r.primary_reason || r.reason || '') + ' ' + (r.primary_excerpt || r.excerpt || '')
    ).join(' ');

    const hits = terms.filter(t => corpus.includes(t)).length;
    const ratio = hits / terms.length;
    _fQuickMode = ratio >= 0.3;
    updateModeDisplay();
  }

  function updateModeDisplay() {
    fMode.classList.remove('hidden');
    const n = (state.card.allResults || []).length;
    if (_fQuickMode) {
      fMode.innerHTML = `<span class="text-seal">⚡ 快速分析</span>（基於 ${n} 筆既有摘要）預計 5 秒
        <span class="ml-2 text-warm-400 hover:text-seal cursor-pointer" onclick="toggleFollowupMode()">改用 → 完整分析</span>`;
      fBtn.textContent = '快速分析';
    } else {
      fMode.innerHTML = `<span class="text-amber-600">📖 完整分析</span>（重讀全文）預計較久
        <span class="ml-2 text-warm-400 hover:text-seal cursor-pointer" onclick="toggleFollowupMode()">改用 → 快速分析</span>`;
      fBtn.textContent = '開始分析';
    }
  }

  window.toggleFollowupMode = function() {
    _fQuickMode = !_fQuickMode;
    updateModeDisplay();
  };

  // 輸入 debounce
  if (fInput) fInput.addEventListener('input', () => {
    clearTimeout(_fTimer);
    _fTimer = setTimeout(() => detectFollowupMode(fInput.value), 300);
  });

  // Enter 送出
  if (fInput) fInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing && !fBtn.disabled) fBtn.click();
  });

  // 送出
  if (fBtn) fBtn.addEventListener('click', async () => {
    const q = fInput.value.trim();
    if (!q) return;
    fBtn.disabled = true;
    fBtn.textContent = '分析中...';

    if (_fQuickMode) {
      // 快速模式：直接 call API
      try {
        const sourceId = state.card.analysisId;
        const res = await apiFetch(`/api/tasks/${state.card.taskId}/quick-followup`, {
          method: 'POST',
          body: JSON.stringify({ source_analysis_id: sourceId, question: q }),
        });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();

        // 顯示結果：更新 synthesis 卡片
        await reloadAnalyses();
        const analysis = state.analyses.find(a => a.id === data.analysis_id);
        if (analysis?.synthesis) {
          try {
            const synth = JSON.parse(analysis.synthesis);
            synth._quick = true;
            renderCardSynthesis(synth);
            state.card.clusters = synth.clusters || [];
            renderCardClusterTabs();
          } catch {}
        }
        // 更新問題顯示
        document.getElementById('card-results-question').textContent = q;
        // 更新 header
        document.getElementById('card-header-text').textContent =
          `快速分析 · 基於 ${(state.card.allResults || []).length} 筆摘要`;
        // 清空輸入
        fInput.value = '';
        fMode.classList.add('hidden');
        // 切到新 analysis tab
        state.card.analysisId = data.analysis_id;
        renderAnalysisHistoryTabs();
      } catch (err) {
        alert(`快速分析失敗：${err.message}`);
      }
    } else {
      // 完整模式：直接用原有 narrow 條件 + 新問題送 Stage 3，不跳回 State A
      try {
        // 複用上一次 analysis 的 narrow 條件
        const prevAnalysis = state.analyses.find(a => a.id === state.card.analysisId);
        let narrow = {};
        try { narrow = JSON.parse(prevAnalysis?.narrow_state || '{}'); } catch {}

        // 關閉舊 bell SSE，建新的
        const oldBellSrc = state.bell.sseConnections.get(state.card.taskId);
        if (oldBellSrc) { oldBellSrc.close(); state.bell.sseConnections.delete(state.card.taskId); }
        subscribeBellTask(state.card.taskId);

        const res = await apiFetch(`/api/tasks/${state.card.taskId}/analyses`, {
          method: 'POST',
          body: JSON.stringify({ question: q, read_facts: false, narrow }),
        });
        if (!res.ok) throw new Error(await res.text());
        const { analysis_id } = await res.json();

        // 更新 state
        state.card.analysisId = analysis_id;
        state.primaryAnalysisId = analysis_id;
        state.card.progress = 0;
        state.card._feedItems = [];  // 新分析 → 清空即時 feed
        state.card._feedItemCaseIds = new Set();
        const task = state.tasks.find(t => t.id === state.card.taskId);
        state.bell.tasks.set(state.card.taskId, {
          status: 'running', progress: 0, keyword: task?.keyword || '',
          analysisId: analysis_id, unread: false, question: q,
        });
        renderNotificationBell();
        renderTaskLists();

        // 切到 State B 顯示進度
        await reloadAnalyses();
        renderCardProgress(q);
        setCardState('b');
        renderAnalysisHistoryTabs();

        fInput.value = '';
        fMode.classList.add('hidden');
      } catch (err) {
        alert(`分析失敗：${err.message}`);
      }
    }

    fBtn.disabled = false;
    fBtn.textContent = _fQuickMode ? '快速分析' : '開始分析';
  });
}

// ─── Analysis history tabs ──────────────────────────
function renderAnalysisHistoryTabs() {
  const tabsEl = document.getElementById('card-analysis-tabs');
  // 顯示所有 analyses：已完成、進行中、失敗的都顯示
  const allAnalyses = state.analyses.filter(a =>
    a.synthesis || a.status === 'running' || a.status === 'pending' || a.status === 'failed'
  );
  if (allAnalyses.length <= 1 && !allAnalyses.some(a =>
    a.status === 'running' || a.status === 'pending' || a.status === 'failed'
  )) {
    tabsEl.classList.add('hidden');
    return;
  }
  tabsEl.classList.remove('hidden');
  const activeId = state.card.analysisId;
  tabsEl.innerHTML = allAnalyses.map(a => {
    const isActive = a.id === activeId;
    const isRunning = a.status === 'running' || a.status === 'pending';
    const isFailed = a.status === 'failed';
    const q = (a.question || '').slice(0, 20) + ((a.question || '').length > 20 ? '…' : '');
    const spinner = isRunning
      ? `<span class="pulse-dot w-1.5 h-1.5 rounded-full bg-seal inline-block shrink-0"></span>`
      : '';
    const failIcon = isFailed
      ? `<span class="w-1.5 h-1.5 rounded-full bg-red-500 inline-block shrink-0"></span>`
      : '';
    const cls = isFailed
      ? 'border-red-300 text-red-600 bg-red-50 cursor-pointer'
      : isRunning
        ? 'border-seal/40 text-seal bg-seal/5 cursor-pointer'
        : isActive
          ? 'bg-seal/10 border-seal text-seal'
          : 'border-warm-200 text-warm-500 hover:border-warm-400 hover:text-ink cursor-pointer';
    const suffix = isFailed
      ? `<span class="text-[10px] text-red-500 ml-0.5">失敗</span>`
      : '';
    return `
      <button class="px-2.5 py-1 text-xs font-mono rounded-sm border transition-colors ${cls}"
              data-analysis-id="${escAttr(a.id)}"
              data-analysis-running="${isRunning ? '1' : ''}"
              data-analysis-failed="${isFailed ? '1' : ''}"
              ${isActive && !isRunning && !isFailed ? 'disabled' : ''}>
        <span class="inline-flex items-center gap-1.5">${spinner}${failIcon}${escHtml(q)}${suffix}</span>
      </button>`;
  }).join('');

  tabsEl.querySelectorAll('button[data-analysis-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const aid = btn.dataset.analysisId;
      if (aid === state.card.analysisId && !btn.dataset.analysisRunning && !btn.dataset.analysisFailed) return;

      if (btn.dataset.analysisFailed) {
        // 點 failed tab → 切到 State B 顯示錯誤 + 重試按鈕
        const analysis = state.analyses.find(a => a.id === aid);
        state.card.analysisId = aid;
        renderCardProgress(analysis?.question || '');
        setCardState('b');
        renderCardError('分析過程中發生錯誤，請點擊重新分析');
      } else if (btn.dataset.analysisRunning) {
        // 點 running tab → 切到 State B 顯示進度
        const analysis = state.analyses.find(a => a.id === aid);
        state.card.analysisId = aid;
        renderCardProgress(analysis?.question || '');
        setCardState('b');
        // 恢復進度
        const bellInfo = state.bell.tasks.get(state.card.taskId);
        if (bellInfo && bellInfo.progress > 0) {
          const phaseTxt = bellInfo.progressPhase === 'fetch' ? '全文快取中'
            : (bellInfo.progressPhase === 'screen' || bellInfo.progressPhase === 'read') ? 'AI 分析中'
            : bellInfo.progressPhase === 'synth' ? '正在產出結果'
            : 'AI 分析中';
          updateCardProgress(bellInfo.progress, phaseTxt);
        }
      } else {
        // 點 done tab → 顯示結果
        await renderCardResults(aid);
        renderAnalysisHistoryTabs();
      }
    });
  });
}


// ═══════════════════════════════════════════════════
//  JUDGMENT READER CARD
// ═══════════════════════════════════════════════════

let _readerJudgment = null;  // cached judgment data for current reader
let _readerSection = 'reasoning';

// ─── Outline parsing (adapted from judicial-outline-extension content.js) ──
const _CJK_UPPER = /^[壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸]+\s*[、.,．]/;
const _CJK_NUM   = /^[一二三四五六七八九十百零〇]+\s*[、.,．]/;
const _PAREN_NUM = /^[（(]\s*[一二三四五六七八九十百零〇]+\s*[）)]/;
const _ENCLOSED  = /^[\u3220-\u3229]/;

// 子項標記分層：依判決書常見用法排序，縮排逐層加深
// level 3  ⒈⒉⒊ (U+2488-249B)  或  1. 2. 3. 阿拉伯加點
// level 4  ⑴⑵⑶ (U+2474-2487)  或  (1)(2)(3) 括弧阿拉伯
// level 5  ①②③ (U+2460-2473)  圈數字
const _SUB_DOT_U       = /^[\u2488-\u249B]/;                           // ⒈
const _SUB_PAREN_U     = /^[\u2474-\u2487]/;                           // ⑴
const _SUB_CIRCLED_U   = /^[\u2460-\u2473]/;                           // ①
// 阿拉伯加點：1. 9. 10. ... 含全形 １. ９. １０.；但不得匹配到 1.5 / 103. / 10.0（period 後非數字）
const _SUB_DOT_ARABIC  = /^[1-9\uFF11-\uFF19][0-9\uFF10-\uFF19]?[.、．][\s\u3000]*(?=[^\d\uFF10-\uFF19.、．\s])/;
// 括弧阿拉伯：(1) (10) 或 （１）等全形變體
const _SUB_PAREN_ARABIC = /^[(（]\s*[1-9\uFF11-\uFF19][0-9\uFF10-\uFF19]?\s*[)）]/;

function _detectOutlineLevel(text) {
  const t = (text || '').replace(/^\s+/, '');
  if (!t) return null;
  if (_CJK_UPPER.test(t)) return 0;
  if (_CJK_NUM.test(t))   return 1;
  if (_ENCLOSED.test(t) || _PAREN_NUM.test(t)) return 2;
  if (_SUB_DOT_U.test(t) || _SUB_DOT_ARABIC.test(t)) return 3;
  if (_SUB_PAREN_U.test(t) || _SUB_PAREN_ARABIC.test(t)) return 4;
  if (_SUB_CIRCLED_U.test(t)) return 5;
  return null;
}

// ── 引號深度與 context-aware force-close ──
// 判決原文偶有缺漏 」（OCR / 資料錯誤），孤立 「 會讓深度卡住，後半段被誤判為引號內。
// 但合法長引用（大法官解釋、前案判決、整條法條）也可能很長。兩者取捨：
//   - 偵測到 citation prefix（「判決：」「解釋：」「條規定：」等）→ 信任模式：寬鬆閾值
//   - 無 citation prefix → 保守模式：短字數 + 少 marker 即強制關閉
// 核心原則：寧可讓 orphan quote 多吞幾個 outline（type B 錯誤），也不要讓合法引用內容
// 的結構污染本案 outline（type A 錯誤）。
const _QUOTE_SPAN_NORMAL = 500;
const _QUOTE_SPAN_CITATION = 30000;
const _QUOTE_MARKERS_NORMAL = 3;
const _QUOTE_MARKERS_CITATION = 10;

// Citation prefix 偵測：掃 `「` 前 ~30 字是否有引用脈絡關鍵字
// (1) 判決/裁定/解釋/函釋 等引述其他見解的詞
// (2) 條/項/規定/明定 等法條引述
// (3) 案號格式（XX年度XX字第XX號）
// (4) 「按」直接接 「 — 法律文書引法條/前案的標準起手式（按「所謂…」此有最高
//     法院X年X字第N號判決可資參照）。要求「按」貼著「，且前面是句首/空白/標點，
//     避免 `按民法…，「相對人」乃指…` 等定義性引號被誤判
const _CITATION_PREFIX_PATTERNS = [
  /(?:判決(?:意旨)?|裁定(?:意旨)?|大法官解釋|憲法解釋|司法院解釋|解釋(?:意旨|文)?|函釋|函示|函文|要旨|意旨|略以|略謂|明定|明文規定|揭示|認為|認定|指出|參照|規定|條)$/,
  /\d+年度?\S{1,20}字第?\d+號(?:判決|裁定)?$/,
  /(?:^|[\s\u3000、，,。；;])按$/,
];
function _hasCitationContext(tail) {
  // 去除尾端標點/空白，讓 keyword 剛好在結尾
  const cleaned = (tail || '').replace(/[：:)）、，,。；;\s\u3000]+$/, '');
  for (const re of _CITATION_PREFIX_PATTERNS) {
    if (re.test(cleaned)) return true;
  }
  return false;
}

function _buildQuoteMask(text) {
  const mask = new Array(text.length).fill(false);
  let depth = 0;
  let openAt = -1;
  let maxSpan = _QUOTE_SPAN_NORMAL;
  for (let i = 0; i < text.length; i++) {
    // 檢查是否孤立開頭太久 → 強制重置
    if (depth > 0 && openAt >= 0 && (i - openAt) > maxSpan) {
      depth = 0;
      openAt = -1;
    }
    const c = text[i];
    if (c === '「' || c === '『') {
      if (depth === 0) {
        openAt = i;
        // 視 citation 脈絡決定此引號用寬鬆還是保守閾值
        const tail = text.slice(Math.max(0, i - 30), i);
        maxSpan = _hasCitationContext(tail) ? _QUOTE_SPAN_CITATION : _QUOTE_SPAN_NORMAL;
      }
      depth++;
    }
    mask[i] = depth > 0;
    if (c === '」' || c === '』') {
      depth = Math.max(0, depth - 1);
      if (depth === 0) openAt = -1;
    }
  }
  return mask;
}

// 中文數字字串 → 整數。支援「一」到「九十九」；無法解析回 null。
// 用於判斷連續 L1 block 的數字是否連續（跳號 = 疑似引法條款號）。
const _CJK_NUM_MAP = { 一:1, 二:2, 三:3, 四:4, 五:5, 六:6, 七:7, 八:8, 九:9 };
function _parseCjkNumeral(s) {
  if (!s) return null;
  if (s.length === 1) {
    if (s === '十') return 10;
    return _CJK_NUM_MAP[s] ?? null;
  }
  // 十X（11-19）
  if (s.startsWith('十')) {
    const rest = s.slice(1);
    const u = _CJK_NUM_MAP[rest];
    return u != null ? 10 + u : null;
  }
  // X十 / X十Y
  const idx = s.indexOf('十');
  if (idx > 0) {
    const tens = _CJK_NUM_MAP[s.slice(0, idx)];
    if (tens == null) return null;
    const unitsStr = s.slice(idx + 1);
    const units = unitsStr ? _CJK_NUM_MAP[unitsStr] : 0;
    if (units == null) return null;
    return tens * 10 + units;
  }
  return null;
}

// 從 L1 段落開頭抽出中文數字、回傳整數。格式：`一、` / `三、` / `十二、` 等
function _l1NumeralOf(text) {
  const m = (text || '').match(/^([一二三四五六七八九十]+)[、，]/);
  return m ? _parseCjkNumeral(m[1]) : null;
}

// Pass 2.5：撤銷法條款號引文被誤當 L1 的 split（見 parseJudgmentParagraphs 註解）
// 範例輸入被誤切成：
//   [{level:1, text:"三、對於..."}, {level:1, text:"六、如不許..."}, {level:null, text:"民事訴訟法第447條第1項第3、6款分別定有明文"}]
// 輸出：[{level:null or 原前段 level, text:"... 三、對於... 六、如不許..."}, {level:null, text:"民事訴訟法..."}]
const _CITATION_CLOSURE_RE = /定有明文|規定如下|所明定|亦有明文|明定之/;

// prev 段尾「引介清單」pattern：下列X：/ 包括：/ 情形之一：/ 規定如下：
// 真 outline 的 section header 用「部分：/ 主張：/ 答辯：」等、不會命中此 regex
const _LIST_INTRO_RE = /(?:下列[\u4e00-\u9fff]{0,8}|包括[\u4e00-\u9fff]{0,4}|情形之一|規定如下)[：。]?$/;

// 將一串連續 L1 依「非遞增轉折」切成嚴格遞增的 sub-block，每個獨立判斷
// 例：[3, 1, 2, 3, 4, 4]（三、按原告之訴 ＋ 黨規 一二三四 ＋ 四、綜上所述）
//     → [[3], [1,2,3,4], [4]]，其中 [1,2,3,4] 判斷為被引清單、獨立 revert
// 不能合整塊判斷 — 合起來 isVeryLong=true 直接 skip、被引清單就抓不到
function _splitL1BlockByResets(block) {
  const subBlocks = [];
  let current = [block[0]];
  for (let k = 1; k < block.length; k++) {
    const prevNum = _l1NumeralOf(block[k - 1].text);
    const curNum = _l1NumeralOf(block[k].text);
    if (prevNum != null && curNum != null && curNum <= prevNum) {
      subBlocks.push(current);
      current = [block[k]];
    } else {
      current.push(block[k]);
    }
  }
  subBlocks.push(current);
  return subBlocks;
}

// 評估單一 block（sub-block）是否該 revert、執行對應動作
// tailParagraphs：block 後續段落（用來偵測 closure 有沒有在後面 200 字內）
function _evaluateBlockAndPush(block, result, tailParagraphs) {
  // 單 L1 不判定（誤殺風險高）
  if (block.length < 2) { result.push(...block); return; }

  const nums = block.map(b => _l1NumeralOf(b.text));
  const allValid = nums.every(n => n != null);
  let isDiscontinuous = false;
  if (allValid) {
    for (let k = 1; k < nums.length; k++) {
      if (nums[k] !== nums[k - 1] + 1) { isDiscontinuous = true; break; }
    }
  }

  // 引文收口：block 尾部 或 後續 200 字內有「定有明文」類詞
  let hasCitationClosure = _CITATION_CLOSURE_RE.test(block[block.length - 1].text);
  if (!hasCitationClosure) {
    const tail = tailParagraphs.slice(0, 3).map(x => x.text).join('').slice(0, 200);
    if (_CITATION_CLOSURE_RE.test(tail)) hasCitationClosure = true;
  }

  // Guard：block 內含 L2+ 巢狀標記 → 幾乎必為真 outline、不撤銷
  let hasNested = false;
  for (const b of block) {
    const qm = _buildQuoteMask(b.text);
    const inner = _findSubItemMarkers(b.text, qm);
    if (inner.some(m => m.level > 1)) { hasNested = true; break; }
  }

  const startsFromOne = allValid && nums[0] === 1;
  const isVeryLong = block.length >= 5;

  // Guard：block 內部連續 + 首項 num 接續 result 最近 L1 num → 真 outline 延續、不 revert
  // 例：一、原告主張（L2/L3 子項把 L1 block 打斷）→ 二、被告則以→ 三、法院見解 …
  //     block 是 [二,三]、最近 L1 是 一、、2=1+1 → 延續
  let isContinuation = false;
  if (allValid && !isDiscontinuous) {
    const wantPrevNum = nums[0] - 1;
    for (let k = result.length - 1; k >= 0; k--) {
      const r = result[k];
      if (r.level !== 1) continue;
      isContinuation = _l1NumeralOf(r.text) === wantPrevNum;
      break;
    }
  }

  // Trigger：prev 段尾為「引介清單」pattern → 即使 startsFromOne 也 revert
  // 例（黨規）：「...分別裁決下列處分：\n一、勸告。...四、除名；...定有明文」
  //     sub-block [1,2,3,4] 從 1 起、有 closure、prev 是引介清單 → revert
  let prevHasListIntro = false;
  const _prev = result[result.length - 1];
  if (_prev && _prev.text) {
    prevHasListIntro = _LIST_INTRO_RE.test(_prev.text.slice(-20));
  }

  const shouldRevert = !hasNested && !isVeryLong && !isContinuation && (
    isDiscontinuous ||                              // 跳號是強訊號、任何長度都 revert
    (hasCitationClosure && !startsFromOne) ||      // closure 弱訊號、只在「非從 1 起」才算
    (hasCitationClosure && prevHasListIntro)       // prev 引介清單 + closure、壓過 startsFromOne guard
  );
  if (shouldRevert) {
    const prev = result[result.length - 1];
    const mergedText = block.map(b => b.text).join('');
    if (prev && prev.level !== -1 && prev.level !== 0 && prev.level !== 'table') {
      prev.text = prev.text + mergedText;
    } else {
      result.push({ level: null, text: mergedText, offset: block[0].offset });
    }
    return;
  }
  result.push(...block);
}

function _unsplitCitedArticleClauses(paragraphs) {
  const result = [];
  let i = 0;
  while (i < paragraphs.length) {
    const p = paragraphs[i];
    if (p.level !== 1) { result.push(p); i++; continue; }

    // 收集連續 L1，再按「非遞增轉折」切成 sub-blocks
    let j = i;
    const rawBlock = [];
    while (j < paragraphs.length && paragraphs[j].level === 1) {
      rawBlock.push(paragraphs[j]);
      j++;
    }
    const subBlocks = _splitL1BlockByResets(rawBlock);
    for (let s = 0; s < subBlocks.length; s++) {
      // tail 用來偵測 closure 在後續段落 — 對最後一個 sub-block 用 paragraphs[j..]、
      // 其他 sub-block 用下個 sub-block 開頭（下個 sub-block 本身 就是 block 後的內容）
      const tail = s === subBlocks.length - 1
        ? paragraphs.slice(j)
        : subBlocks[s + 1];
      _evaluateBlockAndPush(subBlocks[s], result, tail);
    }
    i = j;
  }
  return result;
}

// 段落內掃描所有子項標記位置（含阿拉伯變體），需要脈絡檢查避免誤判
function _findSubItemMarkers(text, quoteMask) {
  // 掃描段落內所有階層標記（L0-L5），回傳 { index, level }，依 index 排序
  // 呼叫端依 level 做 connectivity 分組：同 level ≥ 2 次才視為有效列舉
  const items = [];
  const add = (idx, level) => {
    if (!quoteMask[idx]) items.push({ index: idx, level });
  };
  // 邊界：字串開頭、或前一字元為句末/分隔/閉引號，才算列舉起點
  // 包含：句號/分號/冒號/問號/驚嘆號（全半形）、閉引號「」『』、換行/空白
  // 閉引號之所以列入：判決書常見「引完條文」。」（X）...」模式，MCP 硬斷會把
  //   （X）和 」 黏在一起成 `」（二）`，若 」 不是 boundary，（二）會被漏判
  const isBoundary = (i) => {
    if (i === 0) return true;
    return /[。；：？！?!.\n\s\u3000」』）)]/.test(text[i - 1]);
  };

  // L0 壹、貳、
  for (const m of text.matchAll(/[壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸]+\s*[、．.]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 0);
  }
  // L1 一、二、（邊界排除「第一、」「第二項」等）
  for (const m of text.matchAll(/[一二三四五六七八九十百零〇]+\s*[、．.]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 1);
  }
  // L2 ㈠-㈩ Unicode（函號常見「台財融㈥字第XXX號」，需 boundary check）
  for (const m of text.matchAll(/[\u3220-\u3229]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 2);
  }
  // L2 （一）(一) 全/半形括弧中文數字
  for (const m of text.matchAll(/[（(]\s*[一二三四五六七八九十百零〇]+\s*[）)]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 2);
  }
  // L3 ⒈ Unicode（需 boundary check 避免在複合編號內誤拆，如「事由㈡之⒈」）
  for (const m of text.matchAll(/[\u2488-\u249B]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 3);
  }
  // L3 1. 阿拉伯加點 — 半形 1-9 與全形 １-９（U+FF11-FF19）都接。
  // 後面必須非數字（半/全形都擋），避免誤配 1.5 / １．５ 或 inline 列表 "第１、２項"。
  // Judgment 實例：臺北高行 107 年度全字第 69 號 用 "１、２、３、" 做要件分層。
  for (const m of text.matchAll(/([1-9\uFF11-\uFF19][0-9\uFF10-\uFF19]?)[.、．](?=[^\d\uFF10-\uFF19.、．])/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 3);
  }

  // L4 ⑴ Unicode（需 boundary check 避免誤拆函號或複合編號）
  for (const m of text.matchAll(/[\u2474-\u2487]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 4);
  }
  // L4 (1) 括弧阿拉伯 — 半形與全形數字都接
  for (const m of text.matchAll(/[(（]\s*([1-9\uFF11-\uFF19][0-9\uFF10-\uFF19]?)\s*[)）]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 4);
  }
  // L5 ① Unicode（需 boundary check 避免誤拆函號或複合編號）
  for (const m of text.matchAll(/[\u2460-\u2473]/g)) {
    if (!isBoundary(m.index)) continue;
    add(m.index, 5);
  }

  return items.sort((a, b) => a.index - b.index);
}

// 長段軟斷：句號後以判決常用轉折詞起頭，且前段已累積 ≥ 300 字
const _TRANSITION_RE = /^(?:惟查|惟按|惟|又按|又|另按|另|再查|查|按|至|本院|本件|是以|準此|從而|故)/;
const _SOFT_WRAP_MIN_LEN = 500;        // 短於此不拆
const _SOFT_WRAP_MIN_SEG = 300;        // 拆點前的段至少要有這麼長

// ASCII 表格偵測：刑事判決常以 box-drawing chars 畫表格（證據清單、量刑考量等）
// 行首以 ┌├└│─ 任一字元開始的行視為表格內容，與前後行合併為 level='table' 段落，
// 渲染時以 <pre font-mono whitespace-pre> 保留原始對齊
const _TABLE_LINE_START_RE = /^\s*[┌┐└┘├┤┬┴┼│─]/;

// 非正式章節標記：老判決或結構差的判決常用這些短語當「無編號的章節起始」
// 例：最高法院 106 台上 1144 整篇理由沒有 一、二、三、，靠這些短語分節
// 用途 A：soft-wrap 時加入切分觸發，避免「...合先敘明。上訴人主張：...」被併成同段
// 用途 B：若整篇完全無正式 L0/L1 marker，這些段落提升為 L1 進 outline 側欄
const _INFORMAL_L1_RE = new RegExp(
  '^(?:' +
  '上訴人(?:起訴|聲請)?(?:主張|陳稱|辯稱|則以|則稱|略謂|略以)' +
  '|原告(?:起訴)?(?:主張|陳稱|聲明|略稱|略謂)' +
  '|被上訴人(?:則以|答辯|辯稱|則稱|抗辯|略以|略謂)' +
  '|被告(?:則以|答辯|辯稱|抗辯|略以)' +
  '|原審(?:法院)?(?:認|以為|認定|略以|略謂)' +
  '|原判決(?:認|以|略|認定|論述)' +
  '|本院(?:按|認為|之判斷|查|審酌|以為)' +
  ')'
);

// 裸標記：文字 trim 後只剩標記本體（無後接內文）。這種段落若有深層子項緊接，
// 要把自己併進子項避免孤零零佔一行（例：㈡\n⑴按…\n⑵… 應變成 ㈡⑴按…）。
const _BARE_MARKER_RE = new RegExp(
  '^(?:' +
  '[\\u3220-\\u3229][、．.]?' +
  '|[（(]\\s*[一二三四五六七八九十百零〇]+\\s*[）)][、．.]?' +
  '|[壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸]+\\s*[、．.]?' +
  '|[一二三四五六七八九十百零〇]+\\s*[、．.]?' +
  '|[\\u2488-\\u249B][、．.]?' +
  '|[\\u2474-\\u2487][、．.]?' +
  '|[\\u2460-\\u2473][、．.]?' +
  // 半/全形阿拉伯數字（同 _SUB_DOT_ARABIC / _SUB_PAREN_ARABIC 一致）
  '|[1-9\\uFF11-\\uFF19][0-9\\uFF10-\\uFF19]?[.、．]' +
  '|[(（]\\s*[1-9\\uFF11-\\uFF19][0-9\\uFF10-\\uFF19]?\\s*[)）][、．.]?' +
  ')$'
);
function _isBareMarker(text) {
  return _BARE_MARKER_RE.test((text || '').trim());
}

function buildOutline(text) {
  // 從 parseJudgmentParagraphs 產生的段落陣列萃取 outline
  // 關鍵優勢：
  //   1. 引號感知 — 引用條文內的 一、二、 不會被誤當 outline
  //   2. mid-line marker — 「五、本院查：（一）…（二）…」的（一）（二）被正確抓到
  //   3. offset 與主閱讀區 data-offset anchor 一致 — click 能正確捲動
  // 只取 level 0-2 + section header（-1）呈現於側欄，避免 3-5 子項造成側欄過於密集
  const paragraphs = parseJudgmentParagraphs(text);
  const items = [];
  for (const p of paragraphs) {
    if (p.level === null) continue;
    if (p.level === 'table') continue;  // 表格不進 outline
    // section header 對應到舊 API 的 level=0（側欄用 rc-outline-level-0 樣式）
    const outlineLevel = p.level === -1 ? 0 : p.level;
    if (outlineLevel > 2) continue;
    const rawLabel = p.text.replace(/\s+/g, '');
    // 外框 CSS line-clamp 限 2 行，JS 先切到 ~44 字給 CSS 當緩衝 — 避免一條標籤吃掉整片側欄高度
    // 判決慣例「標題：正文」— 側欄只需標題。若 label 含「：」則截到首個「：」止、
    // 不秀後面正文；沒有「：」才退回 44 字硬切
    const colonIdx = rawLabel.indexOf('：');
    let label = colonIdx >= 0
      ? rawLabel.slice(0, colonIdx + 1)
      : rawLabel.slice(0, 44) + (rawLabel.length > 44 ? '…' : '');
    // 再剝掉常見引介語尾「略以：」「則以：」— 這些 phrase 在 outline 裡沒資訊價值
    // （e.g. 「三、抗告意旨略以：」→「三、抗告意旨」；「二、原法院則以：」→「二、原法院」）
    label = label.replace(/(?:略|則)以：$/, '');
    items.push({ level: outlineLevel, text: label, offset: p.offset });
  }
  return items;
}

function renderRcOutline(text) {
  // 舊釋字：不跑完整 outline 偵測（cons 資料無層級、「一、二、三」多為引用條文）
  // 側欄結構 = 「解釋文」/「解釋理由書」兩大段 + 理由書內段落起首的 一、二、三 outline items（若 ≥2 個）
  if (_isOldInterpretationReader()) {
    const listEl = document.getElementById('rc-outline-list');
    const items = [];
    const mtIdx = text.indexOf('解釋文');
    const rIdx = text.indexOf('解釋理由書');
    if (mtIdx >= 0) items.push({ level: 0, text: '解釋文', offset: mtIdx });
    if (rIdx >= 0) items.push({ level: 0, text: '解釋理由書', offset: rIdx });

    // 掃理由書內的 L1 marker：段落起首（前面是 \n\n 或 body 起始）的 一、/二、...
    // 段內表列（如 445「禁制區...為：一、總統府」）前面是「：」，不命中 \n\n，避免誤判。
    // 最少要 2 個才加進側欄。
    //
    // 實作要點：先跳過「解釋理由書\n」header + 後續空白，從 body 第一字開始掃，
    // `^` 就能命中 body 起首的「一、」（如釋字 613 第一段直接以「一、」開頭）
    if (rIdx >= 0) {
      const headerLen = '解釋理由書'.length;
      let bodyStart = rIdx + headerLen;
      while (bodyStart < text.length && /[\s\n]/.test(text[bodyStart])) bodyStart++;
      const body = text.slice(bodyStart);
      const L1_RE = /(?:^|\n\s*\n)\s*([一二三四五六七八九十]{1,3}、\s*[\u4e00-\u9fff][^\n]{0,60})/g;
      const subItems = [];
      let m;
      while ((m = L1_RE.exec(body)) !== null) {
        const markerStart = m.index + m[0].indexOf(m[1]);
        const offset = bodyStart + markerStart;
        let title = m[1].trim().replace(/[。，：；]$/, '');
        if (title.length > 44) title = title.slice(0, 44) + '…';
        subItems.push({ level: 1, text: title, offset });
      }
      if (subItems.length >= 2) items.push(...subItems);
    }

    if (items.length === 0) {
      listEl.innerHTML = '<p class="text-[11px] text-warm-400 font-serif italic">（此釋字無結構標記）</p>';
      return;
    }
    listEl.innerHTML = items.map((item, idx) => `
      <button class="rc-outline-item rc-outline-level-${item.level}"
              data-outline-idx="${idx}" data-offset="${item.offset}"
              title="${escAttr(item.text)}">${escHtml(item.text)}</button>
    `).join('');
    const textEl = document.getElementById('rc-text');
    listEl.querySelectorAll('.rc-outline-item').forEach(btn => {
      btn.addEventListener('click', () => {
        const targetIdx = parseInt(btn.dataset.outlineIdx, 10);
        const target = items[targetIdx];
        if (!target) return;
        _scrollToOutlineAnchor(target.offset);
        listEl.querySelectorAll('.rc-outline-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });
    return;
  }

  const items = buildOutline(text);
  const listEl = document.getElementById('rc-outline-list');

  if (items.length === 0) {
    listEl.innerHTML = '<p class="text-[11px] text-warm-400 font-serif italic">未偵測到層級標記</p>';
    return;
  }

  listEl.innerHTML = items.map((item, idx) => `
    <button class="rc-outline-item rc-outline-level-${item.level}"
            data-outline-idx="${idx}" data-offset="${item.offset}"
            title="${escAttr(item.text)}">${escHtml(item.text)}</button>
  `).join('');

  // Click to scroll
  listEl.querySelectorAll('.rc-outline-item').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.outlineIdx, 10);
      const target = items[idx];
      if (!target) return;
      _scrollToOutlineAnchor(target.offset);
      listEl.querySelectorAll('.rc-outline-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });
}

// 2026-04-19：outline 點擊跳段不再用 anchor.scrollIntoView() —
//   (1) anchor 是 0×0 absolute-positioned span，Chrome 對這類元素的 scrollIntoView
//       行為不穩（本份 105年度停字第125號 實測點擊完全不動）
//   (2) scrollEl.scrollTo({ behavior: 'smooth' }) 在此容器也不動（可能 overflow-anchor
//       + 巢狀 flex 引發 Chrome smooth 實作 bug）
// 用 getBoundingClientRect 計算絕對位置，再用 setInterval 跑緩動動畫（避免 rAF
// 在背景分頁暫停造成律師開多個分頁時卡住）。
let _outlineScrollAnim = null;
function _scrollToOutlineAnchor(offset) {
  const scrollEl = document.getElementById('rc-scroll');
  const textEl = document.getElementById('rc-text');
  if (!scrollEl || !textEl) return;
  const anchor = textEl.querySelector(`[data-offset="${offset}"]`);
  if (!anchor) return;
  const ar = anchor.getBoundingClientRect();
  const sr = scrollEl.getBoundingClientRect();
  const target = Math.max(0, Math.min(
    scrollEl.scrollHeight - scrollEl.clientHeight,
    scrollEl.scrollTop + (ar.top - sr.top) - 24,
  ));
  const from = scrollEl.scrollTop;
  const distance = target - from;
  if (Math.abs(distance) < 2) { scrollEl.scrollTop = target; return; }
  if (_outlineScrollAnim) clearInterval(_outlineScrollAnim);
  const duration = Math.min(500, Math.max(180, Math.abs(distance) * 0.4));
  const start = Date.now();
  const ease = (t) => t < 0.5 ? 2*t*t : 1 - Math.pow(-2*t+2, 2)/2;
  _outlineScrollAnim = setInterval(() => {
    const t = Math.min(1, (Date.now() - start) / duration);
    scrollEl.scrollTop = from + distance * ease(t);
    if (t >= 1) { clearInterval(_outlineScrollAnim); _outlineScrollAnim = null; }
  }, 16);
}

// ── 段落合併 + 結構化排版 ──
// MCP 回來的文字有硬性斷行（每 ~35 字斷一次）。合併同段落的行，
// 但在層級標記（壹、一、(一)、㈠…）和 section header（主文/事實/理由）出現處保留段落分隔。

const _SECTION_HEADER_RE = /^[主事理][\s\u3000]*[文實由][\s\u3000]*$/;

function _isOutlineStart(trimmedLine) {
  if (!trimmedLine) return false;
  if (_SECTION_HEADER_RE.test(trimmedLine)) return true;
  return _detectOutlineLevel(trimmedLine) !== null;
}

function _getOutlineLevel(trimmedLine) {
  if (_SECTION_HEADER_RE.test(trimmedLine)) return -1; // section header
  return _detectOutlineLevel(trimmedLine);
}

// 名字被 MCP 硬斷 guard：
// 司法院 .text-pre 26 字硬斷 + 中文姓名含 CJK 數字結尾（e.g. 馬英九）→ 「馬英\n九、…」
// 會被 _isOutlineStart 誤判為 L1 outline start。
// 啟發式：前段尾 2-3 字皆 CJK 無標點 + 本行以單個 CJK 數字 [一-九] + 、起頭 → 續行，不當 outline。
// 範圍刻意窄：只攔 [一-九]（最常見姓名字，如 王一 / 李二 / 馬英九），不攔 十、十一、壹、（一）等
//   — 真實 outline 的 L1 轉折前 99% 以句末標點結尾，一般不會命中此 guard。
function _looksLikeMCPHardBreakedName(paraText, newLine) {
  if (!paraText) return false;
  if (!/^[一二三四五六七八九]\s*[、．.]/.test(newLine)) return false;
  const tail3 = paraText.slice(-3);
  if (tail3.length < 2) return false;
  // 尾 2-3 字須全為常見漢字（CJK Unified Ideographs）— 擋掉標點 / 英數 / 全半形空白
  if (!/^[\u4e00-\u9fff]+$/.test(tail3)) return false;
  return true;
}

// 從段落 text 抽出 marker 前綴與 body，用於渲染時做 hanging indent（flex 兩欄）
// 回傳 [marker, body]；若無 marker 返回 ['', text]
function _extractMarker(text, level) {
  if (level === null || level === -1) return ['', text];
  // 依 level 對應 marker pattern（需與 _detectOutlineLevel 同步）
  const patterns = {
    0: /^[壹貳參肆伍陸柒捌玖拾甲乙丙丁戊己庚辛壬癸]+\s*[、．.]/,
    1: /^[一二三四五六七八九十百零〇]+\s*[、．.]/,
    // L2 含 PUA（U+E000-F8FF）：判決書有 ㈩ 之後用 PUA 字元存 ⑪⑫⑬ 等延伸列表
    2: /^(?:[\u3220-\u3229\uE000-\uF8FF][、．.]?|[（(]\s*[一二三四五六七八九十百零〇]+\s*[）)][、．.]?)/,
    3: /^(?:[\u2488-\u249B][、．.]?|[1-9\uFF11-\uFF19][0-9\uFF10-\uFF19]?[.、．])/,
    4: /^(?:[\u2474-\u2487][、．.]?|[(（]\s*[1-9\uFF11-\uFF19][0-9\uFF10-\uFF19]?\s*[)）][、．.]?)/,
    5: /^[\u2460-\u2473][、．.]?/,
  };
  const re = patterns[level];
  if (!re) return ['', text];
  const m = text.match(re);
  if (!m) return ['', text];
  return [m[0], text.slice(m[0].length)];
}

// 把原始文字解析為結構化段落陣列：[{ level: -1|0|1|2|3|4|5|null, text, offset }]
// 經過四遍：行合併（引號感知）→ 子項拆分（L0-L5）→ 軟斷行 → 裸標記合併
// formatJudgmentText、buildOutline 皆使用此 helper，確保主區段落與 outline 標記一致
function parseJudgmentParagraphs(text) {
  if (!text) return [];
  const lines = text.split('\n');
  const paragraphs = []; // { level: -1|0|1|2|null, text: string, offset: number }
  let currentPara = null;
  let charOffset = 0;
  // 跨行追蹤 quote 深度：「...」內即使遇到 一、 標記也不斷段，避免拆散引用條文
  // Force-close 保險（兩條規則 OR 觸發）：
  //   (1) 累積字數超過 qMaxSpan 仍未閉合 → 視為孤立 「
  //   (2) quote 區間內遇到 ≥ qMaxMarkers 個行首 outline marker → 視為孤立 「
  // Context-aware：開引號時 snapshot「citation prefix 是否存在」決定用寬鬆或保守閾值：
  //   - 有 prefix（判決：/ 規定：/ 條 / 解釋：等）→ 信任長引用（30000 字、10 markers）
  //   - 無 prefix → 保守（500 字、3 markers），快速救 orphan quote
  let quoteDepth = 0;
  let quoteOpenOffset = -1;
  let quoteInnerMarkers = 0;
  let qMaxSpan = _QUOTE_SPAN_NORMAL;
  let qMaxMarkers = _QUOTE_MARKERS_NORMAL;
  // Context-aware PUA 偵測：司法院系統用 U+E000-F8FF 表示超過 ㈩ 的 ⑪⑫⑬... 列表字元
  // 只有在已經進入 L2 區間（㈠㈡...㈩）的脈絡下，行首 PUA 才視為 L2 延伸
  // 遇 L0/L1 重置（新 section 開始，上一批 L2 列表不算數）
  let sawL2InSection = false;

  // s: current line text; prefixContext: paragraph text + any accumulation BEFORE this line
  const updateQuote = (s, prefixContext) => {
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === '「' || c === '『') {
        if (quoteDepth === 0) {
          // 視 citation 脈絡決定本引號用哪組閾值
          const combinedTail = (prefixContext + s.slice(0, i)).slice(-30);
          if (_hasCitationContext(combinedTail)) {
            qMaxSpan = _QUOTE_SPAN_CITATION;
            qMaxMarkers = _QUOTE_MARKERS_CITATION;
          } else {
            qMaxSpan = _QUOTE_SPAN_NORMAL;
            qMaxMarkers = _QUOTE_MARKERS_NORMAL;
          }
          quoteOpenOffset = charOffset;
          quoteInnerMarkers = 0;
        }
        quoteDepth++;
      } else if (c === '」' || c === '』') {
        quoteDepth = Math.max(0, quoteDepth - 1);
        if (quoteDepth === 0) { quoteOpenOffset = -1; quoteInnerMarkers = 0; }
      }
    }
  };

  for (const rawLine of lines) {
    // 每行開頭檢查 quote force-close 條件
    if (quoteDepth > 0 && quoteOpenOffset >= 0) {
      const spanExceeded = (charOffset - quoteOpenOffset) > qMaxSpan;
      const markersExceeded = quoteInnerMarkers >= qMaxMarkers;
      if (spanExceeded || markersExceeded) {
        quoteDepth = 0;
        quoteOpenOffset = -1;
        quoteInnerMarkers = 0;
      }
    }
    const trimmed = rawLine.replace(/^\s+/, '').replace(/\s+$/, '');

    if (!trimmed) {
      // 空行 = 段落結束（除非仍在 quote 內 → 保持合併）
      if (quoteDepth === 0 && currentPara) {
        paragraphs.push(currentPara); currentPara = null;
      }
      charOffset += rawLine.length + 1;
      continue;
    }

    // ASCII 表格偵測：行首為 box-drawing char 視為表格內容
    // 保留原始 rawLine（含前後全形空白），以 \n 串接多行成整塊 table
    // 不經 outline / quote / sub-item 等後續邏輯，完整當作獨立段落
    if (_TABLE_LINE_START_RE.test(rawLine)) {
      const rawLineKept = rawLine.replace(/\s+$/, '');  // 只去尾端空白
      if (currentPara && currentPara.level === 'table') {
        currentPara.text += '\n' + rawLineKept;
      } else {
        if (currentPara) paragraphs.push(currentPara);
        currentPara = { level: 'table', text: rawLineKept, offset: charOffset };
      }
      charOffset += rawLine.length + 1;
      continue;
    }

    // Snapshot paragraph text BEFORE any merge，供 updateQuote 做 citation prefix lookback
    const paraContextBefore = currentPara ? currentPara.text : '';

    // Context-aware PUA：若已進入 L2 區間（㈠～㈩），行首 PUA 視為 L2 延伸（⑪⑫⑬...）
    const isPuaContinuation =
      quoteDepth === 0 && sawL2InSection && /^[\uE000-\uF8FF]/.test(trimmed);

    // 名字硬斷 guard：前段尾被 MCP 26 字硬斷切進中文姓名時（如 馬英\n九、國民黨…），
    // 下行的 `九、` 不是 outline marker，純粹是姓名 + 頓號分隔的一部分 → 走續行合併路徑
    //
    // 但不對 L0 / section header 套用：26 字硬斷只發生在 prose body；
    // L0 短標題（如 `壹、程序部分`）不帶標點就收尾時、若後接 L1 `一、...`，
    // 尾 3 字全 CJK（序部分）+ 新行 `一、` 也會誤觸發 guard，把 L1 子項併入 L0
    // 標題顯示成 `壹、程序部分一、按「...」`。因 L0/section 永遠是短標題、絕不
    // 含 prose body 跨行的硬斷場景，這裡排除掉即可。L1+ 保留，因長 body 可能
    // 真的有人名跨 26 字邊界。
    const _maybeHardBreakedName =
      quoteDepth === 0 && currentPara &&
      currentPara.level !== 'table' &&
      currentPara.level !== 0 && currentPara.level !== -1 &&
      _looksLikeMCPHardBreakedName(currentPara.text, trimmed);

    if (quoteDepth === 0 && (_isOutlineStart(trimmed) || isPuaContinuation) && !_maybeHardBreakedName) {
      // 遇到層級標記 = 新段落（僅在 quote 外判斷）
      if (currentPara) paragraphs.push(currentPara);
      const level = isPuaContinuation ? 2 : _getOutlineLevel(trimmed);
      // 更新 sawL2InSection：L0/L1 重置；L2 設為 true；L3+ 不影響
      if (level === 0 || level === 1) sawL2InSection = false;
      else if (level === 2) sawL2InSection = true;
      if (level === -1) {
        // Section header（主文/理由/事實）獨立成段，不合併後續行
        paragraphs.push({ level: -1, text: trimmed.replace(/[\s\u3000]/g, ''), offset: charOffset });
        currentPara = null;
      } else {
        currentPara = { level, text: trimmed, offset: charOffset };
      }
      // 新段落的 quote prefix context 只有這行本身
      updateQuote(trimmed, '');
    } else if (currentPara && currentPara.level !== 'table') {
      // 同段落續行 → 合併（去掉硬斷行）
      // 關鍵：table 段落不可被非 table 文字合併進去，否則 table 內嵌亂碼
      const lastChar = currentPara.text.slice(-1);
      const firstChar = trimmed.charAt(0);
      const needSpace = /[A-Za-z0-9]/.test(lastChar) && /[A-Za-z0-9]/.test(firstChar);
      currentPara.text += (needSpace ? ' ' : '') + trimmed;
      if (quoteDepth > 0 && _isOutlineStart(trimmed)) {
        quoteInnerMarkers++;
      }
      // lookback 得用合併前的段落文字 + 本行串接前綴
      updateQuote(trimmed, paraContextBefore);
    } else {
      // 三種可能進入此分支：
      //   (a) currentPara === null 且非 outline → 新建普通段
      //   (b) currentPara 是 table，此行非 table → 結束 table，新建普通段
      //   (c) quoteDepth > 0 且 currentPara 是 table → 也結束 table
      if (currentPara) paragraphs.push(currentPara);
      currentPara = { level: null, text: trimmed, offset: charOffset };
      updateQuote(trimmed, '');
    }
    charOffset += rawLine.length + 1;
  }
  if (currentPara) paragraphs.push(currentPara);

  // 第二遍：段落內嵌子項標記拆分
  // 規則：
  //   a) Section header (-1) 跳過；其餘一律掃描內嵌階層標記
  //   b) 掃出引號外的所有 marker（L0-L5 都掃），依 level 分組
  //   c) 同 level ≥ 2 次才視為有效列舉（避免單一出現的引用編號誤判）
  //   d) 引號內的 marker 全部忽略（可能是合約/法條原文）
  //   e) 首段（前面的文字）保留原 level；拆出的段依 marker level 指派新層級
  //   f) 深層段落（3/4/5）也繼續掃，因為可能巢狀更深列舉
  const afterSubSplit = [];
  for (const p of paragraphs) {
    if (p.level === -1 || p.level === 'table') { afterSubSplit.push(p); continue; }

    const quoteMask = _buildQuoteMask(p.text);
    const allMarkers = _findSubItemMarkers(p.text, quoteMask);
    if (allMarkers.length === 0) { afterSubSplit.push(p); continue; }

    // 依 level 分群，只保留 ≥ 2 次的 level
    const grouped = {};
    for (const mk of allMarkers) {
      (grouped[mk.level] = grouped[mk.level] || []).push(mk);
    }
    const validMarkers = [];
    for (const lvl of Object.keys(grouped)) {
      if (grouped[lvl].length >= 2) validMarkers.push(...grouped[lvl]);
    }
    if (validMarkers.length === 0) { afterSubSplit.push(p); continue; }

    validMarkers.sort((a, b) => a.index - b.index);

    // 首段（第一個 marker 前的文字）保留原 level
    const firstIdx = validMarkers[0].index;
    if (firstIdx > 0) {
      const head = p.text.slice(0, firstIdx).trim();
      if (head) afterSubSplit.push({ level: p.level, text: head, offset: p.offset });
    }
    // 每個 marker 到下一個 marker 的範圍 → 獨立成段，採 marker 自身的 level
    for (let i = 0; i < validMarkers.length; i++) {
      const start = validMarkers[i].index;
      const end = (i + 1 < validMarkers.length) ? validMarkers[i + 1].index : p.text.length;
      const seg = p.text.slice(start, end).trim();
      if (seg) afterSubSplit.push({
        level: validMarkers[i].level,
        text: seg,
        offset: p.offset + start,
      });
    }
  }

  // Pass 2.5：撤銷法條款號引文被誤當 L1 的 split
  // 偵測判決中「依下列情形之一：三、... 六、... 定有明文」這種 pattern —
  // 三跟六其實是引條文的款號、不是本案 outline。
  // 觸發條件（OR）：
  //   (a) 連續 L1 block 數字不連續（如 3 → 6 跳號，不應為本案 outline）
  //   (b) block 後 200 字 / block 末尾含「定有明文 / 規定如下 / 所明定」等引文收口
  // Guard：
  //   - block 只有 1 個 L1：不判定（單項無法看連續性、誤殺風險高）
  //   - block 內含 L2/L3/L4/L5 巢狀標記：保留（引文罕見有巢狀結構、幾乎必為真 outline）
  const afterCitationUnsplit = _unsplitCitedArticleClauses(afterSubSplit);

  // 第三遍：軟斷行 — 無階層標記的長段，於句號 + 判決常用轉折詞處斷開
  // 只處理 level=null 的普通段落；有階層（0-5）、section header 不動
  const splitParagraphs = [];
  for (const p of afterCitationUnsplit) {
    if (p.level !== null || p.text.length < _SOFT_WRAP_MIN_LEN) {
      splitParagraphs.push(p); continue;
    }
    const quoteMask = _buildQuoteMask(p.text);
    const splits = [];  // 拆點 index（拆點之後為新段起始）
    let lastSplit = 0;
    const re = /。[\s\u3000]*/g;
    let m;
    while ((m = re.exec(p.text)) !== null) {
      const nextIdx = m.index + m[0].length;
      if (nextIdx >= p.text.length) continue;
      if (quoteMask[nextIdx]) continue;                // 引號內不斷
      if ((nextIdx - lastSplit) < _SOFT_WRAP_MIN_SEG) continue;  // 太短不斷
      // 非正式章節標記（如「上訴人主張」）需要較長 lookahead 才能匹配
      const lookahead = p.text.slice(nextIdx, nextIdx + 4);
      const lookaheadLong = p.text.slice(nextIdx, nextIdx + 12);
      if (!_TRANSITION_RE.test(lookahead) && !_INFORMAL_L1_RE.test(lookaheadLong)) continue;
      splits.push(nextIdx);
      lastSplit = nextIdx;
    }
    if (splits.length === 0) { splitParagraphs.push(p); continue; }

    let prev = 0;
    for (const cut of splits) {
      const seg = p.text.slice(prev, cut).trim();
      if (seg) splitParagraphs.push({ level: null, text: seg, offset: p.offset + prev });
      prev = cut;
    }
    const tail = p.text.slice(prev).trim();
    if (tail) splitParagraphs.push({ level: null, text: tail, offset: p.offset + prev });
  }

  // 第四遍：裸標記合併
  // 若段落 trim 後只剩標記本體（㈡ / 1. / (一) 等）且下一段是更深層級，
  // 把標記併入子段開頭，避免「㈡」孤零零佔一行。
  // 保留原段的 offset（= outline 錨點位置），level 改用子段的。
  const mergedParagraphs = [];
  for (let i = 0; i < splitParagraphs.length; i++) {
    const cur = splitParagraphs[i];
    const nxt = splitParagraphs[i + 1];
    const canMerge =
      cur.level !== null && cur.level !== -1 &&
      _isBareMarker(cur.text) &&
      nxt && nxt.level !== null && nxt.level !== -1 &&
      nxt.level > cur.level;
    if (canMerge) {
      mergedParagraphs.push({
        level: nxt.level,
        text: cur.text.trim() + nxt.text,
        offset: cur.offset,
      });
      i++;  // 跳過已合併的 nxt
    } else {
      mergedParagraphs.push(cur);
    }
  }

  // 第五遍（fallback）：informal L1 promotion
  // 僅當整份判決完全沒有正式 L0/L1 marker 時啟動（這是結構極差的老判決）
  // 把符合「上訴人主張/被上訴人則以/本院按」等短語起頭的 null 段提升為 L1，
  // 讓側欄 outline 能顯示基本章節分界。
  // 有任何正式 L0/L1 的判決 → 完全不進此分支 → 零 regression。
  const hasFormalTop = mergedParagraphs.some(p => p.level === 0 || p.level === 1);
  if (!hasFormalTop) {
    for (let i = 0; i < mergedParagraphs.length; i++) {
      const p = mergedParagraphs[i];
      if (p.level !== null) continue;
      const head = p.text.replace(/^\s+/, '');
      if (_INFORMAL_L1_RE.test(head)) {
        mergedParagraphs[i] = { ...p, level: 1 };
      }
    }
  }

  return mergedParagraphs;
}

// 把 parseJudgmentParagraphs 輸出的段落陣列渲染成結構化 HTML
function formatJudgmentText(text) {
  const mergedParagraphs = parseJudgmentParagraphs(text);
  if (!mergedParagraphs.length) return '';

  let html = '';
  for (const p of mergedParagraphs) {
    const escapedText = escHtml(p.text);
    const anchor = `<span data-offset="${p.offset}" style="position:absolute;margin-top:-8px"></span>`;

    // 內文 <p> 統一排版：判決書實際印刷風格
    //   - text-align: justify（兩端對齊）
    //   - leading-[1.7]（緊湊行距）
    //   - Hanging indent 改用 CSS text-indent + padding-left（不再用 flex）：
    //     marker 與 body 同在 <p> 裡，搜尋關鍵字可跨越 marker+body 高亮不被切斷
    //     每段依實際 marker 寬度（計算 em）動態設定 indent，各種 marker 都精確對齊
    // baseP 不設 text-size，由 #rc-text 的 CSS 控制（支援字型大小切換）
    const baseP = 'font-serif text-ink leading-[1.7] text-justify';

    // 計算 marker 寬度（em）：CJK/全形 = 1em，ASCII/半形 = 0.5em
    // 例：(一) = 0.5 + 1 + 0.5 = 2em，㈠ = 1em，壹、 = 2em，1. = 1em
    const markerEm = (s) => {
      let w = 0;
      for (const c of s) {
        w += c.charCodeAt(0) < 0x0080 ? 0.5 : 1;
      }
      return w;
    };

    // helper：產出 hanging indent 結構（單一 <p>，marker+body 在同一 text node）
    const renderHanging = (marker, body, containerCls) => {
      if (!marker) {
        return `<div class="${containerCls}">${anchor}
          <p class="${baseP}">${escHtml(body)}</p>
        </div>`;
      }
      const em = markerEm(marker);
      const hangStyle = `padding-left: ${em}em; text-indent: -${em}em`;
      return `<div class="${containerCls}">${anchor}
        <p class="${baseP}" style="${hangStyle}">${escHtml(marker)}${escHtml(body)}</p>
      </div>`;
    };

    if (p.level === 'table') {
      // ASCII 表格區塊 — 用等寬字型保留原始對齊
      // text-[13px] 較小避免過寬；overflow-x-auto 讓超寬表格水平捲動
      // leading 1.5 接近原文緊湊感；whitespace-pre 保留空白與換行
      html += `<div class="relative my-3">${anchor}
        <pre class="font-mono text-[13px] leading-[1.5] text-ink overflow-x-auto whitespace-pre border border-warm-200 bg-parchment/50 p-3 rounded-sm">${escHtml(p.text)}</pre>
      </div>`;
    } else if (p.level === -1) {
      // Section header（主文/理由/事實）— 保留原設計
      html += `<div class="relative mt-8 mb-4 first:mt-0">
        ${anchor}
        <div class="border-b border-warm-300 pb-2">
          <h3 class="font-serif text-base font-semibold text-ink tracking-wide">${escapedText}</h3>
        </div>
      </div>`;
    } else if (p.level === 0) {
      // L0: 壹、貳、…（章節標題）— 整行 semibold（含 marker + body）
      const [m, b] = _extractMarker(p.text, 0);
      html += renderHanging(m, b, 'relative mt-3 mb-1 first:mt-2 font-semibold tracking-wide');
    } else if (p.level === 1) {
      // L1: 一、二、…
      const [m, b] = _extractMarker(p.text, 1);
      html += renderHanging(m, b, 'relative mt-2 mb-0');
    } else if (p.level === 2) {
      // L2: ㈠、（一）
      const [m, b] = _extractMarker(p.text, 2);
      html += renderHanging(m, b, 'relative mt-1 mb-0 pl-1');
    } else if (p.level === 3 || p.level === 4 || p.level === 5) {
      // L3-L5: ⒈ / ⑴ / ①…
      const [m, b] = _extractMarker(p.text, p.level);
      html += renderHanging(m, b, 'relative mt-1 mb-0 pl-4');
    } else {
      // 普通段落（無階層標記）
      html += `<div class="relative mt-1 mb-0">${anchor}
        <p class="${baseP}">${escapedText}</p>
      </div>`;
    }
  }
  return html;
}

function renderRcText(text) {
  const textEl = document.getElementById('rc-text');
  if (!text) { textEl.innerHTML = '<p class="text-warm-400 font-serif italic py-8 text-center">（無內容）</p>'; return; }
  textEl.innerHTML = formatJudgmentText(text);
}

// 主文預處理：尊重 MCP 斷行為段落分隔，只合併被硬斷的跨行句
//
// 設計原則：MCP 已依判決書原文斷行，行末是 。 就是獨立段落，行末不是 。 就是 hard-wrap 續行。
// 不可自行以 。 拆句 — 因為「其餘聲請駁回。聲請訴訟費用由X負擔。」這類「多句同段」是常見格式，
// 由 MCP 的斷行決定是否拆，不是句數決定。
//
// 唯一例外：若某行結尾非句末標點（。！？）代表 MCP ~35 字硬 wrap 切在句中，
// 需併入下一行避免視覺斷成兩段（如「...判決聲\n請暫時處分...不受理。」→ 合併）。
function _splitMainTextSentences(mainText) {
  if (!mainText) return '';
  const rawLines = mainText.split(/[\r\n]+/).map(s => s.trim()).filter(s => s);
  const merged = [];
  let buffer = '';
  // Terminator：句末標點（。！？）可接任意閉引號（」』）
  // 例：「本件聲請駁回。」/「本件裁定…」都算句末
  const TERMINATOR_RE = /[。！？!?][」』]*$/;
  for (const line of rawLines) {
    buffer += line;
    if (TERMINATOR_RE.test(line)) {
      merged.push(buffer);
      buffer = '';
    }
  }
  if (buffer) merged.push(buffer);
  return merged.join('\n\n');
}

// ── AI 評價區塊（reader 最頂部、當事人之上）──
// 從 state.card.allResults / state.judgments 找此 case 在主分析下的 result
// 找不到（無分析、score=null）→ 不顯示區塊
// 找到 score=0 → 顯示簡化版（無原文摘錄框）
// 找到 score>0 → 完整版：score 大字 + direction badge + position + 可點原文摘錄框
function _findAiEvalForCase(caseId) {
  const lookups = [
    state.card?.allResults,
    state.judgments,
  ];
  for (const arr of lookups) {
    if (!Array.isArray(arr)) continue;
    const r = arr.find(x => x.case_id === caseId);
    if (r && (r.primary_score != null || r.score != null)) return r;
  }
  return null;
}

function _buildAiEvalBlock(caseId) {
  const r = _findAiEvalForCase(caseId);
  if (!r) return '';

  const score = r.primary_score ?? r.score ?? 0;
  const rawReason = r.primary_reason ?? r.reason ?? '';
  const rawExcerpt = r.primary_excerpt ?? r.excerpt ?? '';

  // reason 形如 "[支持] 法院認定..." → 拆出 direction + position
  // interpretation mode 不評 direction，backend 固定寫 "中性"，前端直接 suppress badge
  const task = state.tasks.find(tt => tt.id === state.card.taskId || tt.id === state.currentTaskId);
  const isInterp = task?.search_domain === 'interpretation';
  const dirMatch = rawReason.match(/^\[(支持|反對|中性)\]\s*(.*)$/s);
  const direction = isInterp ? '' : (dirMatch ? dirMatch[1] : '');
  const position = dirMatch ? dirMatch[2] : rawReason;

  // excerpt 形如 "[理由] 是此，異議人..." → 拆出 found_in label + 內文
  const exMatch = rawExcerpt.match(/^\[(理由|主文|事實|引用法條|全文)\]\s*(.*)$/s);
  const foundInLabel = exMatch ? exMatch[1] : '';
  const excerptText = exMatch ? exMatch[2] : rawExcerpt;

  // 設計版 direction pill：支持綠 / 反對紅 / 中性 ai-tint 棕
  const dirCls = direction === '支持' ? 'ai-eval-direction-support'
               : direction === '反對' ? 'ai-eval-direction-oppose'
               : 'ai-eval-direction-neutral';
  const dirBadgeHtml = direction
    ? `<span class="ai-eval-direction ${dirCls}">${direction}</span>`
    : '';

  // Header：kicker「AI」+「評價與摘要」+ 大 score + /10（左）+ direction pill（右）
  const headerHtml = `
    <div class="ai-eval-header-row">
      <div class="ai-eval-title">
        <span class="ai-eval-kicker">AI</span>
        <span class="ai-eval-label">評價與摘要</span>
        <span class="ai-eval-score">${score}</span>
        <span class="ai-eval-score-max">/10</span>
      </div>
      ${dirBadgeHtml}
    </div>`;

  // score = 0 → 簡化版
  if (score === 0) {
    return `
      <section class="mb-6 pb-4 border-b border-warm-200">
        ${headerHtml}
        <p class="text-sm font-serif text-warm-500 italic">
          AI 評：此判決對研究問題未有實質論述（score 0）。
        </p>
      </section>`;
  }

  // 完整版 — position 直接顯示（direction 已移到 header 右側）
  const positionHtml = position
    ? `<p class="ai-eval-position">${escHtml(position)}</p>`
    : '';

  const excerptBlockHtml = excerptText
    ? `
      <div class="ai-eval-excerpt mt-3 px-3 py-2 min-h-[44px] group" data-ai-eval-excerpt="1">
        <div class="flex items-baseline gap-2 mb-1">
          <span class="text-[10px] font-mono text-warm-500 tracking-wide">
            原文摘錄${foundInLabel ? ` · ${foundInLabel}` : ''}
          </span>
          <span data-excerpt-hint class="ml-auto inline-flex items-center gap-1 text-[11px] font-mono text-warm-400 group-hover:text-ink transition-colors" title="點擊跳到判決中對應段落">
            <svg class="w-3 h-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
              <path d="M3 3 L9 9 M9 9 L9 5 M9 9 L5 9" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            跳到原文
          </span>
        </div>
        <p class="ai-eval-excerpt-text text-[13px] font-serif text-ink leading-relaxed">${escHtml(excerptText)}</p>
      </div>`
    : `<p class="mt-3 text-xs font-serif text-warm-400 italic">（無原文摘錄）</p>`;

  return `
    <section class="mb-6 pb-4 border-b border-warm-200" data-ai-eval-block="1">
      ${headerHtml}
      ${positionHtml}
      ${excerptBlockHtml}
    </section>`;
}

// 辨認當前 reader 的判決是否為舊制釋字
// 目的：跳過針對一般判決設計的 parseJudgmentParagraphs / outline 偵測
//   舊釋字只有「解釋文 + 解釋理由書」兩大塊、無層級結構
//   cons 原始資料無硬斷行、reasoning 直接是完整段落（\n\n 分段）
// 前端用 case_id 前綴「司法院釋字」辨認；新制憲判字不套此簡化
function _isOldInterpretationReader() {
  const cid = _readerJudgment?.case_id || '';
  return /^司法院釋字/.test(cid) || /^釋字/.test(cid);
}

function renderRcCombinedText() {
  // 全文模式：AI 評價 + 當事人 + 主文 + 理由（+ 事實 if exists）依序排列
  // 依司法院判決書格式：AI 評價 → 當事人 → 主文 → 事實/理由 → 法官署名
  const textEl = document.getElementById('rc-text');
  if (!_readerJudgment) { textEl.innerHTML = ''; return; }

  const isOldInterp = _isOldInterpretationReader();
  const mainText = _readerJudgment.main_text || '';
  const reasoning = _readerJudgment.reasoning || '';
  const facts = _readerJudgment.facts || '';

  let combined = '';
  if (isOldInterp) {
    // 舊釋字：解釋文 + 解釋理由書 兩大塊，標題用本身語境
    // 爭點（cons 的 issues）存在 facts 欄位、但已在 header 的案由 pill 顯示、這裡不重複
    if (mainText) combined += '解釋文\n' + mainText + '\n\n';
    if (reasoning) combined += '解釋理由書\n' + reasoning;
  } else {
    if (mainText) combined += '主　文\n' + _splitMainTextSentences(mainText) + '\n\n';
    if (facts) combined += '事　實\n' + facts + '\n\n';
    if (reasoning) combined += '理　由\n' + reasoning;
  }

  // Fallback to full_text if none of the structured fields have content
  if (!combined.trim()) combined = _readerJudgment.full_text || '';

  // ── 開頭：當事人 block（預設收合，位於主文之上）──
  let partiesHtml = '';
  let parties = {};
  try { parties = JSON.parse(_readerJudgment.parties || '{}'); } catch {}
  const partyKeys = Object.keys(parties || {});
  if (partyKeys.length) {
    const partyRows = partyKeys.map(role => {
      const members = (parties[role] || []).map(m => escHtml(m)).join('<br>');
      return `<div class="flex items-start gap-4 py-1">
        <span class="text-sm font-mono text-warm-500 shrink-0 w-[5.5rem]">${escHtml(role)}</span>
        <span class="text-[14px] font-serif text-ink leading-[1.7]">${members}</span>
      </div>`;
    }).join('');
    partiesHtml = `
      <details class="rc-parties group mb-6">
        <summary class="rc-parties-summary">
          <span class="rc-parties-label">當事人</span>
          <span class="rc-parties-toggle-text group-open:hidden">展開</span>
          <span class="rc-parties-toggle-text hidden group-open:inline">收合</span>
          <svg class="rc-parties-chevron" xmlns="http://www.w3.org/2000/svg"
               width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 12 15 18 9"/>
          </svg>
        </summary>
        <div class="rc-parties-body">
          ${partyRows}
        </div>
      </details>`;
  }

  // AI 評價區塊 — reader 最頂部，無 case 對應 result 時回傳空字串自動隱藏
  const aiEvalHtml = _buildAiEvalBlock(_readerJudgment.case_id);

  // 舊釋字：cons 資料無硬斷行、reasoning 是完整段落（\n\n 分段），不跑 parser/outline
  // 避免「一、總統府...」這類引用條文清單被誤判為 outline 層級
  const bodyHtml = isOldInterp
    ? _renderInterpretationPlain(combined)
    : formatJudgmentText(combined);
  let html = aiEvalHtml + partiesHtml + bodyHtml;

  // 末尾附上「法官」章節（視覺樣式與主文/理由 section header 一致，
  // 但直接 append HTML、不經 formatJudgmentText，因此不會出現在左欄 outline）
  // 法官名單單行呈現、以 2 個全形空白分隔（最多 5 位，一行容納得下）
  let judges = [];
  try { judges = JSON.parse(_readerJudgment.judges || '[]'); } catch {}
  if (judges && judges.length) {
    // Grid 排版：auto-fill 5 columns on 寬螢幕、自動 wrap
    // 每名在獨立 span 且 whitespace-nowrap，避免「城仲模」被中文 word-break 拆行
    // 舊釋字用「大法官」header；一般判決用「法官」
    const sectionTitle = isOldInterp ? '大法官' : '法官';
    const judgeCells = judges.map(j =>
      `<span class="whitespace-nowrap font-serif text-ink">${escHtml(j)}</span>`
    ).join('');
    html += `
      <div class="relative mt-8 mb-4">
        <div class="border-b border-warm-300 pb-2">
          <h3 class="font-serif text-base font-semibold text-ink tracking-wide">${sectionTitle}</h3>
        </div>
      </div>
      <div class="mt-3 grid gap-x-6 gap-y-2 leading-[1.9]"
           style="grid-template-columns: repeat(auto-fill, minmax(5.5rem, 1fr));">
        ${judgeCells}
      </div>`;
  }

  textEl.innerHTML = html;
}

// 舊釋字極簡 render：跳過 parseJudgmentParagraphs、outline 偵測、段落合併等
// 邏輯，直接把 combined text 依段落分隔符切成 <p>。處理三種特殊行：
//   1. section header（「解釋文」「解釋理由書」）→ 大字 + 橫線
//   2. 一般段落 → 縮排首行（中文排版慣例）的 <p>
//   3. 段落內若已含 \n（罕見）→ 當段落內軟換行
function _renderInterpretationPlain(text) {
  if (!text) return '';
  const SECTION_HEADERS = new Set(['解釋文', '解釋理由書']);
  // 先按 \n\n 切大段
  const blocks = text.split(/\n\s*\n+/).map(b => b.trim()).filter(Boolean);
  const parts = [];
  for (const block of blocks) {
    // Block 第一行可能是 section header
    const nlIdx = block.indexOf('\n');
    const firstLine = (nlIdx === -1 ? block : block.slice(0, nlIdx)).trim();
    if (SECTION_HEADERS.has(firstLine)) {
      parts.push(`
        <div class="relative mt-6 mb-3" data-offset="${text.indexOf(block)}">
          <div class="border-b border-warm-300 pb-2">
            <h3 class="font-serif text-base font-semibold text-ink tracking-wide">${escHtml(firstLine)}</h3>
          </div>
        </div>`);
      const rest = nlIdx === -1 ? '' : block.slice(nlIdx + 1).trim();
      if (rest) {
        parts.push(_renderInterpretationParagraph(rest, text.indexOf(block) + nlIdx + 1));
      }
    } else {
      parts.push(_renderInterpretationParagraph(block, text.indexOf(block)));
    }
  }
  return parts.join('');
}

function _renderInterpretationParagraph(para, offset) {
  // 段內若含 \n（非段落分隔），當軟換行處理：保留換行但共用同一個 <p>
  // 大多數情況是單段、單行
  const escaped = escHtml(para).replace(/\n/g, '<br>');
  return `
    <div class="mb-3" data-offset="${offset}">
      <p class="font-serif text-ink leading-[1.95] text-[15px]"
         style="text-indent: 2em; text-align: justify;">${escaped}</p>
    </div>`;
}

// ─── Open / Close Reader ─────────────────────────────

async function openReaderCard(taskId, caseId, { replaceHistory = false } = {}) {
  // 標記已讀（列表會淡化已讀 row）
  state.card.readCaseIds.add(caseId);
  const readRow = document.querySelector(`[data-result-idx][data-case-id="${CSS.escape(caseId)}"]`);
  if (readRow) readRow.classList.add('opacity-60');
  // Fetch judgment data
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/judgments/${encodeURIComponent(caseId)}`);
    if (!res.ok) throw new Error(res.status);
    _readerJudgment = await res.json();
  } catch (err) {
    alert(`無法載入判決：${err.message}`);
    // 確保不殘留空白的 reader overlay
    document.getElementById('reader-card-backdrop').classList.add('hidden');
    document.getElementById('reader-card').classList.add('hidden');
    return;
  }

  // Header metadata block (裁判字號 / 裁判日期 / 案由)
  // 法官、當事人都不放 header，由 renderRcCombinedText 顯示在內文：
  //   - 當事人 → 在主文上方（依判決書格式）
  //   - 法官署名 → 整份判決末尾
  const parsed = parseCaseDisplay(_readerJudgment.case_id || caseId);
  const court = _readerJudgment.court || '';
  const metaEl = document.getElementById('rc-meta');
  const cause = _readerJudgment.cause || '';

  // 舊釋字 case_id 本身已是全稱「司法院釋字第N號」、不再拼 court prefix 造成重複
  const rawCid = _readerJudgment.case_id || caseId;
  const isOldInterp = /^司法院釋字/.test(rawCid) || /^釋字/.test(rawCid);
  const caseNumberDisplay = isOldInterp
    ? rawCid   // 律師慣用的全稱直接顯示
    : `${court}${parsed.display}`;

  // 設計版兩行：line1 案名 + mono 日期、line2「案由」kicker + dot + 案由內文
  const dateStr = _readerJudgment.date || '';
  metaEl.innerHTML = `
    <div class="rc-meta-title-row">
      <span class="rc-meta-title">${escHtml(caseNumberDisplay)}</span>
      ${dateStr ? `<span class="rc-meta-date">· ${escHtml(dateStr)}</span>` : ''}
    </div>
    ${cause ? `<div class="rc-meta-cause-row">
      <span class="rc-meta-kicker">案由</span>
      <span class="rc-meta-cause-dot"></span>
      <span class="rc-meta-cause">${escHtml(cause)}</span>
    </div>` : ''}
  `;

  // 案由 pill 已併入 rc-meta 內 inline（line 2「議題 · 案由」），中欄 pill 永遠隱藏
  const causePill = document.getElementById('rc-cause-pill');
  if (causePill) {
    causePill.classList.add('hidden');
    causePill.textContent = '';
    causePill.title = '';
  }

  // 設定星星狀態
  const starBtn = document.getElementById('rc-star');
  const isStarred = state.starred.has(_readerJudgment.case_id);
  starBtn.querySelector('svg').setAttribute('fill', isStarred ? 'currentColor' : 'none');
  starBtn.classList.toggle('text-amber-500', isStarred);
  starBtn.classList.toggle('text-warm-400', !isStarred);

  // Render combined view
  renderRcCombinedText();
  const combined = buildCombinedText();
  renderRcOutline(combined);

  // Keyword highlighting — 用 task 的所有關鍵字（含展開變體）標紅
  highlightKeywordsInReader(taskId);

  // Show card first (so scroll works on visible element)
  document.getElementById('reader-card-backdrop').classList.remove('hidden');
  document.getElementById('reader-card').classList.remove('hidden');
  lockBodyScroll();

  // 套用字型大小設定（從 localStorage 讀）
  _applyReaderFontSize();

  // 掛上閱讀進度條與 outline 當前章節 observer
  // 必須在 card 顯示後才能正確取 scrollHeight/getBoundingClientRect
  requestAnimationFrame(() => {
    _attachReaderScrollHandler();
    _attachOutlineObserver();
  });

  // 更新上/下篇按鈕 enabled 狀態
  _updateNavButtons();

  // 一律從頂部開啟（AI 評價區塊在頂部；若需跳到 excerpt 對應段，由 AI 評價
  // 區塊裡「原文摘錄」框的 click 事件觸發）
  const scrollEl = document.getElementById('rc-scroll');
  if (scrollEl) scrollEl.scrollTop = 0;

  // 每次打開 reader 都 reset 到「判決本文」tab（歷史分析 tab 要點才切）
  switchReaderTab('content');

  // 背景載入跨 task 歷史分析（不阻塞 reader 顯示）— 律師打開 reader 後
  // 「歷史分析 (N)」badge 會稍後填入筆數；點才切 tab
  loadReaderHistory(_readerJudgment.case_id);

  // 歷史：初次進 reader → push 一筆新 entry；A/D 導覽跳下一則 → replace 現有 entry
  // 避免堆疊累積（否則 ESC / history.back 只會退一次導覽、不會回到分析結果）
  const historyState = { view: 'reader-card', taskId, caseId };
  if (replaceHistory) history.replaceState(historyState, '', location.pathname);
  else                 history.pushState(historyState, '', location.pathname);
}

// ─── Reader tab: 判決本文 ↔ 歷史分析 ─────────────────
function switchReaderTab(tab) {
  const contentBtn = document.querySelector('.rc-tab-content');
  const historyBtn = document.querySelector('.rc-tab-history');
  const contentPanel = document.getElementById('rc-content-panel');
  const historyPanel = document.getElementById('rc-history-panel');
  if (!contentBtn || !historyBtn || !contentPanel || !historyPanel) return;

  const isHistory = tab === 'history';
  contentPanel.classList.toggle('hidden', isHistory);
  contentPanel.classList.toggle('flex', !isHistory);
  historyPanel.classList.toggle('hidden', !isHistory);

  const sidebarToggle = document.getElementById('rc-sidebar-toggle');
  if (sidebarToggle) sidebarToggle.classList.toggle('hidden', isHistory);

  contentBtn.classList.toggle('border-ink', !isHistory);
  contentBtn.classList.toggle('text-ink', !isHistory);
  contentBtn.classList.toggle('border-transparent', isHistory);
  contentBtn.classList.toggle('text-warm-400', isHistory);

  historyBtn.classList.toggle('border-ink', isHistory);
  historyBtn.classList.toggle('text-ink', isHistory);
  historyBtn.classList.toggle('border-transparent', !isHistory);
  historyBtn.classList.toggle('text-warm-400', !isHistory);
}

async function loadReaderHistory(caseId) {
  if (!caseId) return;
  const listEl = document.getElementById('rc-history-list');
  const countEl = document.getElementById('rc-history-count');
  if (!listEl || !countEl) return;

  // Loading state
  listEl.innerHTML = `<div class="text-sm text-warm-400 font-mono">載入中…</div>`;
  countEl.textContent = '(…)';

  try {
    const res = await apiFetch(API.caseAnalyses(caseId));
    if (!res.ok) throw new Error(`case analyses ${res.status}`);
    const items = await res.json();
    renderReaderHistory(items);
  } catch (err) {
    console.error('[history] 載入失敗:', err);
    listEl.innerHTML = `<div class="text-sm text-warm-400">無法載入歷史分析</div>`;
    countEl.textContent = '(—)';
  }
}

function renderReaderHistory(items) {
  const listEl = document.getElementById('rc-history-list');
  const countEl = document.getElementById('rc-history-count');
  if (!listEl || !countEl) return;

  countEl.textContent = `(${items.length})`;

  if (!items.length) {
    listEl.innerHTML = `
      <div class="rc-hist-empty">這份判決尚未在任何任務中被分析過</div>`;
    return;
  }

  // 2026-04-19 重寫：視覺跟判決本文 tab 的 AI評價區塊（.ai-eval-*）對齊 —
  //   - score 大字 + /10 + direction pill
  //   - 原本 `[中性]`「[理由]」方括號文字 → 改 pill + kicker
  //   - 任務名稱 → 可點 chip，click 跳回該任務
  //   - 加頂部 overview kicker 給語境
  const overview = `
    <div class="rc-hist-overview">
      這份判決在你 <strong>${items.length}</strong> 個任務中被分析過
    </div>`;

  const cardsHtml = items.map(r => {
    const score = r.score ?? 0;
    const rawReason = r.reason || '';
    const rawExcerpt = r.excerpt || '';
    const isInterp = r.search_domain === 'interpretation';

    // 跟 _buildAiEvalBlock 相同的 parse 邏輯
    const dirMatch = rawReason.match(/^\[(支持|反對|中性)\]\s*(.*)$/s);
    const direction = isInterp ? '' : (dirMatch ? dirMatch[1] : '');
    const position = dirMatch ? dirMatch[2] : rawReason;

    const exMatch = rawExcerpt.match(/^\[(理由|主文|事實|引用法條|全文)\]\s*(.*)$/s);
    const foundInLabel = exMatch ? exMatch[1] : '';
    const excerptText = exMatch ? exMatch[2] : rawExcerpt;

    const dirCls = direction === '支持' ? 'ai-eval-direction-support'
                 : direction === '反對' ? 'ai-eval-direction-oppose'
                 : 'ai-eval-direction-neutral';
    const dirBadge = direction
      ? `<span class="ai-eval-direction ${dirCls}">${direction}</span>`
      : '';

    const question = escHtml(r.question || '（無問題記錄）');
    const keyword = escHtml(r.task_keyword || '');
    const when = (r.analyzed_at || '').slice(0, 10);
    const taskId = r.task_id ? escAttr(r.task_id) : '';

    const taskChip = (keyword && taskId)
      ? `<button class="rc-hist-task-chip" onclick="openTask('${taskId}')" title="跳到此任務">
           <span class="rc-hist-task-chip-kicker">任務</span>
           <span class="rc-hist-task-chip-name">${keyword}</span>
         </button>`
      : (keyword ? `<span class="rc-hist-task-chip rc-hist-task-chip-static">${keyword}</span>` : '');

    const excerptBlock = excerptText ? `
      <div class="rc-hist-excerpt-wrap">
        ${foundInLabel ? `<div class="rc-hist-excerpt-kicker">原文摘錄 · ${escHtml(foundInLabel)}</div>` : ''}
        <div class="rc-hist-excerpt-text">${escHtml(excerptText)}</div>
      </div>` : '';

    return `
      <article class="rc-hist-card">
        <header class="rc-hist-header">
          <div class="rc-hist-title-wrap">
            <span class="ai-eval-kicker">分析</span>
            <h3 class="rc-hist-question">${question}</h3>
          </div>
          <div class="rc-hist-score-wrap">
            <span class="ai-eval-score">${score}</span>
            <span class="ai-eval-score-max">/10</span>
            ${dirBadge}
          </div>
        </header>
        <div class="rc-hist-meta">
          ${taskChip}
          ${when ? `<span class="rc-hist-date">${when}</span>` : ''}
        </div>
        ${position ? `<p class="rc-hist-position">${escHtml(position)}</p>` : ''}
        ${excerptBlock}
      </article>`;
  }).join('');

  listEl.innerHTML = overview + cardsHtml;
}

// ─── Keyword highlighting ─────────────────────────────
// 快取 keyword regex（同一 task 開多筆判決不需重建）
let _kwRegexCache = { taskId: null, re: null };

function highlightKeywordsInReader(taskId) {
  const task = state.tasks.find(t => t.id === taskId);
  if (!task) return;

  let re;
  if (_kwRegexCache.taskId === taskId && _kwRegexCache.re) {
    re = _kwRegexCache.re;
  } else {
    // OR 語法（「A|B」「A｜B」「A OR B」）是前端指令，剝掉 separator 取出實際 keyword
    // 中文 IME 打出的常是全形豎線 ｜(U+FF5C)，要接；律師也常打 `地位or相對` 不加空格，
    // 所以 CJK 字夾住的 `or/OR` 也視為分隔（與 runner._OR_SEP_RE 同步）
    const raw = task.keyword || '';
    const userKws = raw
      .split(/\s*[|｜]\s*|\s+(?:[Oo][Rr])\s+|(?<=[\u4e00-\u9fff])(?:[Oo][Rr])(?=[\u4e00-\u9fff])/)
      .flatMap(g => g.split(/\s+/))           // 每 group 內部空格 AND
      .map(s => s.trim())
      .filter(Boolean);
    // 加上 backend 展開的同義 / 法條變體（從 search_params.expanded_variants 讀）—
    // 這樣律師搜「僱傭」時 reader 也會標紅「雇用 / 雇傭」等實際進過 search 的變體
    let expanded = [];
    try {
      const sp = task.search_params ? JSON.parse(task.search_params) : null;
      if (sp && Array.isArray(sp.expanded_variants)) expanded = sp.expanded_variants;
    } catch {}
    const allKws = [...new Set([...userKws, ...expanded])].filter(Boolean);
    if (allKws.length === 0) return;
    const sorted = [...allKws].sort((a, b) => b.length - a.length);
    const escaped = sorted.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    re = new RegExp(`(${escaped.join('|')})`, 'g');
    _kwRegexCache = { taskId, re };
  }

  // 遍歷 rc-text 裡的 text nodes，包裝匹配部分
  const textEl = document.getElementById('rc-text');
  const walker = document.createTreeWalker(textEl, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);

  for (const node of textNodes) {
    const val = node.nodeValue;
    if (!re.test(val)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let lastIdx = 0;
    let match;
    while ((match = re.exec(val)) !== null) {
      if (match.index > lastIdx) {
        frag.appendChild(document.createTextNode(val.slice(lastIdx, match.index)));
      }
      const mark = document.createElement('span');
      mark.className = 'rc-kw-hit';
      mark.textContent = match[0];
      frag.appendChild(mark);
      lastIdx = re.lastIndex;
    }
    if (lastIdx < val.length) {
      frag.appendChild(document.createTextNode(val.slice(lastIdx)));
    }
    node.parentNode.replaceChild(frag, node);
  }
}

// ─── Scroll to excerpt position ───────────────────────

// 滑動視窗比對：在 haystack 中找與 needle 最高匹配率的位置
function _fuzzyFind(haystack, needle, threshold = 0.8) {
  if (!haystack || !needle) return -1;
  const nLen = needle.length;
  if (nLen === 0 || haystack.length < nLen) return -1;

  // 精確匹配優先
  const exact = haystack.indexOf(needle);
  if (exact !== -1) return exact;

  // 滑動視窗：逐位置計算字元匹配數
  let bestPos = -1, bestRatio = 0;
  const limit = haystack.length - Math.floor(nLen * threshold); // 不用滑到底
  for (let i = 0; i < limit; i++) {
    let matches = 0;
    for (let j = 0; j < nLen && i + j < haystack.length; j++) {
      if (haystack[i + j] === needle[j]) matches++;
    }
    const ratio = matches / nLen;
    if (ratio > bestRatio) {
      bestRatio = ratio;
      bestPos = i;
    }
    if (ratio >= 0.95) break; // 夠好了，提早結束
  }
  return bestRatio >= threshold ? bestPos : -1;
}

// 回 true 表示找到目標段並捲動；false 表示 fuzzyFind miss（給呼叫端顯示失敗回饋用）
function scrollToExcerptInReader(excerpt) {
  if (!excerpt) return false;
  const textEl = document.getElementById('rc-text');
  // 取 excerpt 前 30 字去空白當搜尋錨點
  const anchor = excerpt.replace(/\s+/g, '').slice(0, 30);
  if (anchor.length < 4) return false;

  // 收集全部 text nodes + 建立一條連續字串
  // 重要：跳過 AI 評價區塊內的 text nodes — 那裡也含 excerpt 文字，
  // 否則 fuzzyFind 第一個命中就在區塊本身、scroll 到頂部視覺上沒動
  const walker = document.createTreeWalker(textEl, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      if (node.parentElement && node.parentElement.closest('[data-ai-eval-block]')) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  let fullText = '';
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const clean = (node.nodeValue || '').replace(/\s+/g, '');
    nodes.push({ node, start: fullText.length, len: clean.length });
    fullText += clean;
  }

  // 模糊搜尋
  const pos = _fuzzyFind(fullText, anchor);
  if (pos === -1) return false;  // miss — 呼叫端顯示 feedback

  // 找到 pos 對應的 text node
  for (const n of nodes) {
    if (pos >= n.start && pos < n.start + n.len) {
      const parent = n.node.parentElement;
      if (!parent) return false;
      // 把目標段拉到「最近的可定位 block」
      // 段落 paragraph 通常是 <p> 或 wrapping <div>
      const block = parent.closest('p, div[class*="relative"]') || parent;
      block.scrollIntoView({ behavior: 'smooth', block: 'center' });
      // Flash highlight — cyan brand 藍 1.5s 漸淡到「持久淡標記」狀態保留（不消失）
      // 先清掉 reader 內任何先前的 flash target（每次只標一段），再套到當前 block
      // void offsetWidth 強制 reflow 讓 animation 即使重點同段也能重新觸發
      const textEl = document.getElementById('rc-text');
      textEl.querySelectorAll('.rc-flash-target').forEach(el => el.classList.remove('rc-flash-target'));
      void block.offsetWidth;
      block.classList.add('rc-flash-target');
      return true;
    }
  }
  return false;
}

function closeReaderCard(skipHistory = false) {
  const wasOpen = !document.getElementById('reader-card').classList.contains('hidden');
  document.getElementById('reader-card-backdrop').classList.add('hidden');
  document.getElementById('reader-card').classList.add('hidden');
  _readerJudgment = null;
  unlockBodyScroll();
  // 返回結果頁時重繪 cluster tabs（星星狀態可能改了）
  if (state.card.open && state.card.state === 'c') {
    renderCardClusterTabs();
    // 若 reader 開著時 final synthesis 完成、被延後 → 關 reader 後且律師在「全部」就套用
    // 在特定 cluster 則繼續延後（等律師切回全部或關卡片才刷新）
    if (state.card._pendingFinalRefresh && state.card.activeCluster === null) {
      _applyPendingFinalRefresh();
    }
  }
  if (wasOpen && !skipHistory) history.back();
}

// ─── Pending final synthesis refresh（Option D：延後 re-render 避免打斷律師瀏覽）
function _showPendingFinalBanner() {
  const synthContainer = document.getElementById('card-synthesis');
  if (!synthContainer) return;
  // 復用 preliminary banner slot（本來就是 synthesis 頂部的黃 bar）
  let banner = document.getElementById('card-synth-preliminary-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'card-synth-preliminary-banner';
    banner.className = 'mb-3 flex items-center gap-3 px-3 py-2 rounded-sm border border-amber-300 bg-amber-50 text-[12px]';
    synthContainer.insertBefore(banner, synthContainer.firstChild);
  }
  banner.innerHTML = `
    <span class="font-mono text-[10px] uppercase tracking-widest text-amber-700 shrink-0">已完成</span>
    <span class="text-warm-600 flex-1">全部分析完成、結果已更新（切回「全部」tab 或關閉閱讀器時自動套用）</span>
    <button id="card-synth-apply-final-btn"
      class="font-mono text-[11px] text-seal hover:text-ink border border-seal hover:border-ink px-2 py-1 rounded-sm transition-colors shrink-0">
      立即刷新 →
    </button>`;
  const btn = document.getElementById('card-synth-apply-final-btn');
  if (btn) btn.addEventListener('click', _applyPendingFinalRefresh);
}

async function _applyPendingFinalRefresh() {
  const analysisId = state.card._pendingFinalRefresh;
  if (!analysisId) return;
  state.card._pendingFinalRefresh = null;
  // 重新 render 後原本 activeCluster 會被 reset 到 null（全部）— 這是 option D 的協議
  await renderCardResults(analysisId);
  renderAnalysisHistoryTabs();
}

function switchReaderSection(section) {
  _readerSection = section;
  // Tab active state
  document.querySelectorAll('.rc-section-tab').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.section === section);
  });

  if (section === 'full_text') {
    // 全文 = 組合 主文 + 事實 + 理由，帶 section headers
    renderRcCombinedText();
    // Outline from combined text
    const combined = buildCombinedText();
    renderRcOutline(combined);
  } else {
    const text = _readerJudgment
      ? (_readerJudgment[section] || '')
      : '';
    renderRcText(text);
    renderRcOutline(text);
  }
  // Scroll to top
  document.getElementById('rc-text').scrollTop = 0;
}

function buildCombinedText() {
  // 必須跟 renderRcCombinedText 完全一致（主文要先 _splitMainTextSentences），
  // 否則 outline 的 offset 跟主區段落的 data-offset 對不上，點 outline 會跳錯位置。
  if (!_readerJudgment) return '';
  // 舊釋字走簡化格式：解釋文 + 解釋理由書（與 renderRcCombinedText 一致）
  if (_isOldInterpretationReader()) {
    let t = '';
    if (_readerJudgment.main_text) t += '解釋文\n' + _readerJudgment.main_text + '\n\n';
    if (_readerJudgment.reasoning) t += '解釋理由書\n' + _readerJudgment.reasoning;
    return t.trim() || (_readerJudgment.full_text || '');
  }
  let t = '';
  if (_readerJudgment.main_text) t += '主　文\n' + _splitMainTextSentences(_readerJudgment.main_text) + '\n\n';
  if (_readerJudgment.facts) t += '事　實\n' + _readerJudgment.facts + '\n\n';
  if (_readerJudgment.reasoning) t += '理　由\n' + _readerJudgment.reasoning;
  return t.trim() || (_readerJudgment.full_text || '');
}

// Event handlers
document.getElementById('rc-close').addEventListener('click', () => closeReaderCard());

// Download starred PDFs (bulk zip)
// 暫時停用 — PDF 下載功能尚未通過驗證、待重構（2026-04-19）
async function downloadStarredPdfs() {
  alert('功能開發中，尚不支援');
}

// Download PDF (single)
// 暫時停用 — PDF 下載功能尚未通過驗證、待重構（2026-04-19）
document.getElementById('rc-download-pdf').addEventListener('click', () => {
  alert('功能開發中，尚不支援');
});

// Star toggle — optimistic update + DB persist
document.getElementById('rc-star').addEventListener('click', async () => {
  if (!_readerJudgment) return;
  const caseId = _readerJudgment.case_id;
  const starBtn = document.getElementById('rc-star');
  const wasStarred = state.starred.has(caseId);
  // Optimistic UI
  if (wasStarred) {
    state.starred.delete(caseId);
    starBtn.querySelector('svg').setAttribute('fill', 'none');
    starBtn.classList.remove('text-amber-500');
    starBtn.classList.add('text-warm-400');
  } else {
    state.starred.add(caseId);
    starBtn.querySelector('svg').setAttribute('fill', 'currentColor');
    starBtn.classList.add('text-amber-500');
    starBtn.classList.remove('text-warm-400');
  }
  // Persist
  try {
    const res = await apiFetch(API.caseStar(caseId), {
      method: wasStarred ? 'DELETE' : 'POST',
    });
    if (!res.ok) throw new Error(`star API ${res.status}`);
  } catch (err) {
    console.error('[star] 持久化失敗，rollback:', err);
    // Rollback UI + state
    if (wasStarred) {
      state.starred.add(caseId);
      starBtn.querySelector('svg').setAttribute('fill', 'currentColor');
      starBtn.classList.add('text-amber-500');
      starBtn.classList.remove('text-warm-400');
    } else {
      state.starred.delete(caseId);
      starBtn.querySelector('svg').setAttribute('fill', 'none');
      starBtn.classList.remove('text-amber-500');
      starBtn.classList.add('text-warm-400');
    }
  }
});
document.getElementById('reader-card-backdrop').addEventListener('click', () => {
  // 點背景 = 全部關閉（閱讀器 + 分析結果卡片）→ 回首頁
  document.getElementById('reader-card-backdrop').classList.add('hidden');
  document.getElementById('reader-card').classList.add('hidden');
  _readerJudgment = null;
  state.card.open = false;
  document.getElementById('search-card-backdrop').classList.add('hidden');
  document.getElementById('search-card').classList.add('hidden');
  unlockBodyScroll();
  showView('home');
  history.replaceState({ view: 'home' }, '', location.pathname);
});

// Reader tab 切換：判決本文 ↔ 歷史分析
document.querySelectorAll('.rc-tab').forEach(btn => {
  btn.addEventListener('click', () => switchReaderTab(btn.dataset.rcTab));
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('reader-card').classList.contains('hidden')) {
    closeReaderCard();
    e.stopPropagation();  // don't also close search card
  }
}, true);  // capture phase — fires before search card's Esc handler

// ─── Reader 上/下篇切換 + 快捷鍵 ─────────────────────

// case_id 比對用：strip 掉所有類型空白（半形、tab、NBSP、全形 \u3000 等）
// 前後端 case_id 可能因 URL 編解碼、序列化路徑產生看不見的空白差異
function _normalizeCaseId(s) {
  return (s || '').replace(/[\s\u00A0\u3000\u2000-\u200F\uFEFF]/g, '');
}

// Reader 導覽清單 — 固定為完整判決池（相關 + 無關 + 資料異常），不套 cluster 過濾
// 原本想「當前在哪個 cluster 就在那 cluster 內 nav」但發現邊界 case（current 不在
// preferred cluster）會陷入 idx=-1 silent return；改為固定池穩定又可預期
// 用 Map 以 normalized case_id 為 key 去重 — 避免萬一同判決在多個 bucket 間的
// whitespace variant 造成「自身循環」開啟
function _readerNavList() {
  if (!state.card || !Array.isArray(state.card.allResults)) {
    return (typeof filteredJudgments === 'function') ? filteredJudgments() : [];
  }
  const seen = new Map();
  const pushUnique = r => {
    if (!r) return;
    const key = _normalizeCaseId(r.case_id);
    if (key && !seen.has(key)) seen.set(key, r);
  };
  (state.card.allResults || []).forEach(pushUnique);
  (state.card.irrelevantResults || []).forEach(pushUnique);
  (state.card.dataErrorResults || []).forEach(pushUnique);
  return Array.from(seen.values());
}

function _navigateJudgment(delta) {
  if (!_readerJudgment) return;
  const taskId = state.card.taskId || state.currentTaskId;
  if (!taskId) return;
  const list = _readerNavList();
  if (!list.length) return;
  const cur = _normalizeCaseId(_readerJudgment.case_id);
  const idx = list.findIndex(j => _normalizeCaseId(j.case_id) === cur);
  if (idx < 0) return;
  // Iterate 而非單純 idx+delta — 遇到同 normalized key 的重覆筆就跳過，
  // 避免開啟同一則判決造成視覺卡死 / 事件積壓（舊 bug：重覆開同 case 會塞爆 render queue）
  const step = delta > 0 ? 1 : -1;
  let target = null;
  for (let i = idx + step; i >= 0 && i < list.length; i += step) {
    if (_normalizeCaseId(list[i].case_id) !== cur) { target = list[i]; break; }
  }
  if (!target) { _showReaderToast(delta < 0 ? '已是第一則' : '已是最後一則'); return; }
  // A/D 導覽不新增歷史 entry，讓 ESC / X / 側欄返回可一次退回分析結果
  openReaderCard(taskId, target.case_id, { replaceHistory: true });
}

// 邊界提示用的迷你 toast：reader card 內絕對定位，1.5s 後自動淡出
// 不重複堆疊：新 toast 出現會替換舊的。純資訊性、不可點、無關閉按鈕
function _showReaderToast(msg) {
  const reader = document.getElementById('reader-card');
  if (!reader || reader.classList.contains('hidden')) return;
  const existing = reader.querySelector('.reader-toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'reader-toast';
  toast.textContent = msg;
  reader.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('reader-toast-fade');
    setTimeout(() => toast.remove(), 320);
  }, 1500);
}
function _updateNavButtons() {
  if (!_readerJudgment) return;
  const taskId = state.card.taskId || state.currentTaskId;
  if (!taskId) return;
  const list = _readerNavList();
  const cur = _normalizeCaseId(_readerJudgment.case_id);
  const idx = list.findIndex(j => _normalizeCaseId(j.case_id) === cur);
  const prevBtn = document.getElementById('rc-prev');
  const nextBtn = document.getElementById('rc-next');
  if (prevBtn) prevBtn.disabled = idx <= 0;
  if (nextBtn) nextBtn.disabled = idx < 0 || idx >= list.length - 1;
}
document.getElementById('rc-prev')?.addEventListener('click', () => _navigateJudgment(-1));
document.getElementById('rc-next')?.addEventListener('click', () => _navigateJudgment(1));
// 2026-04-19：原本是側欄收合 toggle，改成「返回分析列表」
// — 律師實際沒有收起 outline 的需求，這顆按鈕的位置（outline/content 交界、
// 左向 chevron）其實更像 back button。
document.getElementById('rc-sidebar-toggle')?.addEventListener('click', () => {
  closeReaderCard();
});

// Reader 開啟時鍵盤切換上下篇（避開 input 焦點）
//   A / K = 上一則    D / J = 下一則
document.addEventListener('keydown', e => {
  if (document.getElementById('reader-card').classList.contains('hidden')) return;
  if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === 'j' || k === 'd') { _navigateJudgment(1); e.preventDefault(); }
  else if (k === 'k' || k === 'a') { _navigateJudgment(-1); e.preventDefault(); }
});

// ─── Reader 閱讀進度條 ─────────────────────────────
let _readerScrollHandler = null;
function _attachReaderScrollHandler() {
  const scrollEl = document.getElementById('rc-scroll');
  const bar = document.getElementById('rc-progress');
  if (!scrollEl || !bar) return;
  // 移除舊 handler（重開判決時避免累加）
  if (_readerScrollHandler) scrollEl.removeEventListener('scroll', _readerScrollHandler);
  // 用 requestAnimationFrame 緩衝：scroll callback 同步 read scrollHeight/clientHeight
  // 又 write bar.style.width 是 layout thrashing 來源。改成 rAF 排到下一 frame 寫，
  // 一個 frame 內最多更新一次，避免 scroll 卡頓
  let rafPending = false;
  const update = () => {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      const max = scrollEl.scrollHeight - scrollEl.clientHeight;
      const pct = max > 0 ? Math.min(100, Math.max(0, (scrollEl.scrollTop / max) * 100)) : 0;
      bar.style.width = pct + '%';
    });
  };
  _readerScrollHandler = update;
  scrollEl.addEventListener('scroll', update, { passive: true });
  update();  // 初始
}

// ─── Outline 當前章節 highlight ─────
// 2026-04-19 重寫：原本用 IntersectionObserver 觀察 zero-size 的 <span data-offset>
// anchor，但 Chrome 對 0×0 absolute-positioned 元素不會回報 intersection（callback
// 永遠不 fire），外加「每次 IO 觸發都立刻 setActive」會讓捲動過程中底色連續切換、
// 字型 subpixel AA 跳動。
// 新做法：scroll 事件 + debounce 180ms，停下來才用 offsetTop 推算目前頂端的段落 →
// (1) 繞開 IO 對 0-size anchor 的限制（offsetTop 一律有效）
// (2) 捲動中完全不動 .active，停下才 paint 一次，沒有「底色隨捲動即時閃」的困擾
let _outlineScrollHandler = null;
let _outlineScrollEl = null;
function _attachOutlineObserver() {
  if (_outlineScrollHandler && _outlineScrollEl) {
    _outlineScrollEl.removeEventListener('scroll', _outlineScrollHandler);
    _outlineScrollHandler = null; _outlineScrollEl = null;
  }
  const scrollEl = document.getElementById('rc-scroll');
  const textEl = document.getElementById('rc-text');
  const outlineList = document.getElementById('rc-outline-list');
  if (!scrollEl || !textEl || !outlineList) return;

  const outlineItems = Array.from(outlineList.querySelectorAll('.rc-outline-item'));
  if (!outlineItems.length) return;
  const outlineOffsets = outlineItems.map(btn => {
    const o = parseInt(btn.dataset.offset || '-1', 10);
    return { btn, offset: isNaN(o) ? -1 : o };
  }).filter(x => x.offset >= 0).sort((a, b) => a.offset - b.offset);
  if (!outlineOffsets.length) return;

  // anchors 的 offsetTop 在 reader 剛開啟時不一定穩定（內容還在 reflow，特別是字型
  // lazy load、AI 評價區塊延後 inject），所以不在 attach 時快取 top，而是每次 tick
  // 重新讀一次 offsetTop。讀 offsetTop 很便宜（browser 內部已 cached layout），
  // 但會強制 browser 確保 layout clean — 對 scroll-driven tick 來說是可接受的成本。
  const anchorEls = Array.from(textEl.querySelectorAll('[data-offset]'))
    .map(el => ({ el, offset: parseInt(el.dataset.offset || '-1', 10) }))
    .filter(x => x.offset >= 0);
  if (!anchorEls.length) return;

  const setActive = (offset) => {
    let target = null;
    for (const { btn, offset: o } of outlineOffsets) {
      if (o <= offset) target = btn;
      else break;
    }
    outlineItems.forEach(b => b.classList.remove('active'));
    if (target) {
      target.classList.add('active');
      // 若 active item 不在 outline 視窗內就滾進視窗 — 只動 outlineBox.scrollTop，
      // 絕不用 scrollIntoView（會連動所有可捲動祖先、造成主區跳動）
      const outlineBox = document.getElementById('rc-outline');
      if (outlineBox) {
        const r = target.getBoundingClientRect();
        const br = outlineBox.getBoundingClientRect();
        const PAD = 8;
        if (r.top < br.top) {
          outlineBox.scrollTop -= (br.top - r.top + PAD);
        } else if (r.bottom > br.bottom) {
          outlineBox.scrollTop += (r.bottom - br.bottom + PAD);
        }
      }
    }
  };

  // 頂端判定偏移：把「viewport 頂端往下 TOP_MARGIN px 這條線」當 cursor，落在此
  // 線以上最靠近的 anchor 視為「目前這段」。TOP_MARGIN 提供 hysteresis，避免律師
  // 微調捲動時底色在邊界震盪。
  //
  // 注意：anchor 是 <span style="position:absolute;margin-top:-8px"> 嵌在段落
  // 內部，offsetParent 是段落本身不是 #rc-scroll，所以 offsetTop 永遠是 -8。
  // 改用 getBoundingClientRect 計算相對 scroll container 的視覺位置才準確。
  const TOP_MARGIN = 120;
  const pickTopOffset = () => {
    const scrollRectTop = scrollEl.getBoundingClientRect().top;
    // cursor 就是「viewport top + TOP_MARGIN」這條水平線的 y 座標
    const cursorY = scrollRectTop + TOP_MARGIN;
    let best = anchorEls[0];
    let bestY = -Infinity;
    for (const a of anchorEls) {
      const y = a.el.getBoundingClientRect().top;
      if (y <= cursorY && y >= bestY) { best = a; bestY = y; }
    }
    return best.offset;
  };

  // 2026-04-19 v3：時間戳 throttle（非 rAF）+ trailing tick
  //   - debounce 版（v1）要停下才標記、視覺斷裂；慣性捲動中觀感不連續
  //   - rAF 版（v2）在 preview/背景分頁時 rAF 會暫停，導致更新停滯
  //   - 此版 hybrid：每 40ms 最多 tick 一次（滑順），加 trailing timer 保證最後一次
  //     捲動事件一定會補跑 tick，避免停在邊界時狀態卡舊。
  //   - 往上捲也會觸發（scroll 事件兩方向都 fire），解決前版「往上捲不標記」。
  //   - hysteresis 由 TOP_MARGIN 提供（120px），段落還沒真的推上來時不會提前切，
  //     切完也不會馬上反向切回去。
  const THROTTLE_MS = 40;
  let lastTickAt = 0;
  let trailingTimer = null;
  let lastActiveOffset = -1;
  const tick = () => {
    lastTickAt = Date.now();
    const off = pickTopOffset();
    if (off !== lastActiveOffset) {
      lastActiveOffset = off;
      setActive(off);
    }
  };
  const onScroll = () => {
    const now = Date.now();
    if (now - lastTickAt >= THROTTLE_MS) {
      tick();
    }
    // trailing：即使在 throttle 窗內，也排一個 timer，保證停止捲動後那一刻
    // 的位置一定會被標記
    if (trailingTimer) clearTimeout(trailingTimer);
    trailingTimer = setTimeout(tick, THROTTLE_MS);
  };

  _outlineScrollHandler = onScroll;
  _outlineScrollEl = scrollEl;
  scrollEl.addEventListener('scroll', onScroll, { passive: true });
  // 初次開啟立刻標一次
  tick();
}

// ─── 字型大小切換 ─────────────────────
const _FONT_SIZE_KEY = 'reader-font-size';
function _applyReaderFontSize() {
  const textEl = document.getElementById('rc-text');
  if (!textEl) return;
  const sz = localStorage.getItem(_FONT_SIZE_KEY) || 'normal';
  textEl.classList.remove('rc-font-small', 'rc-font-large');
  if (sz === 'small') textEl.classList.add('rc-font-small');
  else if (sz === 'large') textEl.classList.add('rc-font-large');
}
function _changeReaderFontSize(delta) {
  const order = ['small', 'normal', 'large'];
  const cur = localStorage.getItem(_FONT_SIZE_KEY) || 'normal';
  const idx = order.indexOf(cur);
  const ni = Math.max(0, Math.min(order.length - 1, idx + delta));
  localStorage.setItem(_FONT_SIZE_KEY, order[ni]);
  _applyReaderFontSize();
}
document.getElementById('rc-font-smaller')?.addEventListener('click', () => _changeReaderFontSize(-1));
document.getElementById('rc-font-larger')?.addEventListener('click', () => _changeReaderFontSize(1));

// (Section tabs removed — always combined view)

// ─── Update openCardResult to use reader card ─────────
// (Replace the old implementation that closed the card and navigated to legacy reader)


// ═══════════════════════════════════════════════════
//  NOTIFICATION BELL
// ═══════════════════════════════════════════════════

function renderNotificationBell() {
  const listEl = document.getElementById('bell-list');
  const tasks = state.bell.tasks;

  if (tasks.size === 0) {
    listEl.innerHTML = `<div class="px-4 py-6 text-center text-xs font-mono text-warm-400">目前沒有進行中的任務</div>`;
    updateBellBadge();
    return;
  }

  let html = '';
  tasks.forEach((info, taskId) => {
    // 若 bell 當初沒塞 keyword（retry 路徑 bug / 舊資料），fallback 從 state.tasks 找
    let kwRaw = info.keyword || '';
    if (!kwRaw) {
      const _t = state.tasks.find(x => x.id === taskId);
      if (_t) kwRaw = getTaskOrigKw(_t);
    }
    const kwDisplay = escHtml(kwRaw).slice(0, 30) || '<span class="text-warm-400">（無關鍵字）</span>';
    const qDisplay = info.question ? escHtml(info.question).slice(0, 40) : '';
    const unreadDot = info.unread ? '<span class="w-1.5 h-1.5 rounded-full bg-red-500 inline-block shrink-0"></span>' : '';
    const safeId = escAttr(taskId);

    if (info.status === 'running') {
      // 分析進行中。progressPhase 由 SSE 事件填；首次事件前 fallback「準備中...」
      const phaseTxt = info.progressPhase === 'fetch' ? '全文快取中'
        : (info.progressPhase === 'screen' || info.progressPhase === 'read') ? 'AI 分析中'
        : info.progressPhase === 'synth' ? '正在產出結果'
        : '準備中...';
      html += `
        <div class="bell-item px-4 py-3 border-b border-warm-100"
             onclick="navTo({view:'results', taskId:'${safeId}'})">
          <div class="flex items-center gap-2 mb-1">
            <span class="pulse-dot w-1.5 h-1.5 rounded-full bg-seal inline-block shrink-0"></span>
            <span class="font-serif text-sm text-ink truncate">${kwDisplay}</span>
          </div>
          ${qDisplay ? `<div class="font-serif text-xs text-warm-500 truncate mb-1.5 pl-3.5">Q：${qDisplay}</div>` : ''}
          <div class="bell-item-progress mb-1">
            <div class="bell-item-progress-fill" style="width:${info.progress || 0}%"></div>
          </div>
          <span class="text-[10px] font-mono text-warm-400">${phaseTxt}</span>
        </div>`;
    } else if (info.status === 'ready') {
      // 搜尋完成，請設定分析範圍
      html += `
        <div class="bell-item px-4 py-3 border-b border-warm-100"
             onclick="state.bell.tasks.get('${safeId}').unread=false; updateBellBadge(); navTo({view:'results', taskId:'${safeId}'});">
          <div class="flex items-center gap-2">
            ${unreadDot}
            <span class="font-serif text-sm text-ink truncate">${kwDisplay}</span>
            <span class="font-mono text-[10px] text-seal ml-auto shrink-0">請設定分析範圍</span>
          </div>
        </div>`;
    } else {
      // 分析完成
      html += `
        <div class="bell-item px-4 py-3 border-b border-warm-100"
             onclick="state.bell.tasks.get('${safeId}').unread=false; updateBellBadge(); navTo({view:'results', taskId:'${safeId}'});">
          <div class="flex items-center gap-2">
            ${unreadDot}
            <span class="font-serif text-sm text-ink truncate">${kwDisplay}</span>
            <span class="font-mono text-[10px] text-emerald-600 ml-auto shrink-0">分析完成</span>
          </div>
          ${qDisplay ? `<div class="font-serif text-xs text-warm-500 truncate mt-1 pl-3.5">Q：${qDisplay}</div>` : ''}
        </div>`;
    }
  });
  listEl.innerHTML = html;
  updateBellBadge();
}

function updateBellBadge() {
  let count = 0;
  state.bell.tasks.forEach(info => { if (info.unread) count++; });
  state.bell.unreadCount = count;
  const badge = document.getElementById('bell-badge');
  if (count > 0) {
    badge.textContent = String(count);
    badge.classList.remove('hidden');
    badge.classList.add('flex');
  } else {
    badge.classList.add('hidden');
    badge.classList.remove('flex');
  }
}

// Bell dropdown toggle
document.getElementById('btn-bell').addEventListener('click', e => {
  e.stopPropagation();
  const dd = document.getElementById('bell-dropdown');
  dd.classList.toggle('hidden');
});
// Close bell dropdown on outside click
document.addEventListener('click', e => {
  const dd = document.getElementById('bell-dropdown');
  if (!dd.classList.contains('hidden') && !document.getElementById('bell-wrap').contains(e.target)) {
    dd.classList.add('hidden');
  }
});

// ─── Bell SSE subscription ──────────────────────────

function subscribeBellTask(taskId) {
  // Don't double-subscribe
  if (state.bell.sseConnections.has(taskId)) return;

  const src = new EventSource(API.stream(taskId));
  state.bell.sseConnections.set(taskId, src);

  // Fetch progress → card State B + bell
  src.addEventListener('stage25_progress', sseHandler(e => {
    const d = JSON.parse(e.data);
    // 如果 fetched === total（全部 cache hit，秒過），不更新 phase 為 fetch
    // 避免「全文快取中 53/53」閃現覆蓋後續的「AI 分析中」
    if (d.fetched >= d.total && d.total > 0) return;
    const info = state.bell.tasks.get(taskId);
    if (info) {
      const pct = d.total > 0 ? Math.round((d.fetched / d.total) * 33) : 0;
      info.progress = pct;
      info.progressPhase = 'fetch';
      renderNotificationBell();
      renderTaskLists();
    }
    _updateAbortButtonLabel();  // fetch 階段 → 「中止分析」
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'b') {
      state.card.fetchTotal = d.total;
      const pct = d.total > 0 ? Math.round((d.fetched / d.total) * 33) : 0;
      // reused 是跨 task 既有資料可複用的數量（見 runner.py _try_reuse_cached_judgment）
      const reusedNote = d.reused ? `（其中 ${d.reused} 筆由其他任務快取複用）` : '';
      updateCardProgress(pct, `全文快取中 ${d.fetched} / ${d.total}${reusedNote}`);
    }
  }));

  // judgments_ready = fetch 全部完成，即將開始 Claude 分析
  src.addEventListener('judgments_ready', sseHandler(e => {
    const d = JSON.parse(e.data);
    const info = state.bell.tasks.get(taskId);
    if (info) { info.progress = 33; info.progressPhase = 'screen'; renderNotificationBell(); renderTaskLists(); }
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'b') {
      const skippedNote = d.skipped ? `（${d.skipped} 筆下載失敗）` : '';
      updateCardProgress(33, `全文快取完成，開始 AI 分析 0 / ${d.after_filter || '?'}${skippedNote}`);
    }
    _updateAbortButtonLabel();  // screen 階段、doneJudg=0 → 「中止分析」
    // task_judgments 才剛 populated → 刷新 facts_coverage 給 UI
    if (taskId === state.currentTaskId) {
      apiFetch(API.task(taskId)).then(r => r.ok ? r.json() : null).then(t => {
        if (t && t.facts_coverage) {
          state.factsCoverage = t.facts_coverage;
          applyFactsCoverageHint();
        }
      }).catch(() => {});
    }
    // 記錄 skipped 數量，synthesis 完成後在 State C 顯示
    if (d.skipped) state.card.fetchSkipped = d.skipped;
  }));

  src.addEventListener('batch_done', sseHandler(e => {
    const d = JSON.parse(e.data);
    const info = state.bell.tasks.get(taskId);
    if (info) {
      const pct = d.total > 0 ? Math.round(33 + (d.completed / d.total) * 57) : 33;
      info.progress = pct;
      info.progressPhase = 'read';
      renderNotificationBell();
      renderTaskLists();
    }
    // 同步更新 state.analyses（本地快取、用於下次 renderCardProgress / banner 計算）
    const localA = (state.analyses || []).find(x => x.id === d.analysis_id);
    if (localA) {
      localA.completed = d.completed;
      localA.total = d.total;
      if (typeof d.match_count === 'number') localA.match_count = d.match_count;
    }
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'b') {
      state.card.analyzeTotal = d.total;
      const pct = d.total > 0 ? Math.round(33 + (d.completed / d.total) * 57) : 33;
      // 兩階段精讀（workload ≥ TWO_PASS_THRESHOLD=20）時 total = workload×2；
      // UI 給律師看「判決筆數」而非內部 step 數，除以 2 還原
      const twoPass = d.total >= 40;
      const doneJudg = twoPass ? Math.floor(d.completed / 2) : d.completed;
      const totalJudg = twoPass ? Math.floor(d.total / 2) : d.total;
      const matchPart = (typeof d.match_count === 'number') ? `（命中 ${d.match_count}）` : '';
      updateCardProgress(pct, `AI 分析中 ${doneJudg} / ${totalJudg}${matchPart}`);
      appendLiveFeed(d.results || []);
      if (d.usage) updateCardTokenTicker(d.usage);  // 即時 token / 成本 ticker
      // 中止按鈕判準用 match_count（命中 ≥ 3 才值得跑 partial synthesis）
      state.card._doneJudg = doneJudg;
      state.card._matchCount = d.match_count || 0;
      _updateAbortButtonLabel();
    }
    // State C live：更新 header stats + partial banner 的剩餘筆數（律師看 partial 結果
    // 時、backend 繼續 scoring、數字應該跟著漲）
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'c'
        && state.card.analysisId === d.analysis_id) {
      const twoPass2 = d.total >= 40;
      const doneJ = twoPass2 ? Math.floor(d.completed / 2) : d.completed;
      const totalJ = twoPass2 ? Math.floor(d.total / 2) : d.total;
      const matchCnt = typeof d.match_count === 'number' ? d.match_count : 0;
      const statsEl = document.getElementById('card-header-stats');
      if (statsEl) {
        statsEl.textContent = `已分析 ${doneJ} 筆 · ${matchCnt} 命中`;
        statsEl.classList.remove('hidden');
      }
      // 更新 partial banner：先 detect 是否需要 structural change（重開中 ↔ 初步結果）
      const banner = document.getElementById('card-synth-preliminary-banner');
      if (banner) {
        const remaining = Math.max(0, totalJ - doneJ);
        const isInitializing = (doneJ >= totalJ);
        const bannerSaysInitializing = banner.textContent.includes('重開中');
        if (isInitializing !== bannerSaysInitializing) {
          // 結構要變（e.g., total 被 backend 更新、remaining 從 0 變成 > 0）→ 完整 re-render
          const a = (state.analyses || []).find(x => x.id === d.analysis_id);
          const syn = a?.synthesis ? (() => { try { return JSON.parse(a.synthesis); } catch { return null; } })() : null;
          const bInfo = (a?.synthesis_is_preliminary && (a.status === 'running' || a.status === 'partial'))
            ? { status: a.status, completed: a.completed || 0, total: a.total || 0, match_count: a.match_count || 0 }
            : null;
          renderCardSynthesis(syn, bInfo);
        } else {
          // 只更新數字（.font-mono.text-ink 存在於「初步結果」variant 的 remaining 數字）
          const remainingEl = banner.querySelector('.font-mono.text-ink');
          if (remainingEl) remainingEl.textContent = String(remaining);
        }
      }
    }
  }));

  src.addEventListener('stage3_synthesis_start', sseHandler(e => {
    const info = state.bell.tasks.get(taskId);
    if (info) { info.progress = 90; info.progressPhase = 'synth'; renderNotificationBell(); renderTaskLists(); }
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'b') {
      updateCardProgress(90, '正在產出結果');
      // 若 graceful abort 正在 pending（abort timer 還在跑），代表現在已進入 partial synthesis
      // 階段：清 timer、移除強制結束按鈕、把中止按鈕換成「AI 綜合分析中」提示
      // （在這個階段按中止沒意義：synthesis 只需 1 次 Claude call、幾秒內結束）
      if (state.card._abortTickTimer) {
        _clearAbortUI();
        const btn = document.getElementById('card-abort-analyze');
        if (btn) {
          btn.disabled = true;
          btn.textContent = 'AI 綜合分析中…';
          btn.classList.add('opacity-60');
        }
      }
    }
  }));

  // Preliminary synthesis：scoring 期間剩餘 ≤ threshold 時提早出結果
  // 此時 card 自動切到 State C 顯示初步結果 + banner，retry / final 會繼續在背景跑
  src.addEventListener('preliminary_synthesis_done', sseHandler(async e => {
    const d = JSON.parse(e.data);
    if (state.card.open && state.card.taskId === taskId) {
      await reloadAnalyses();
      await renderCardResults(d.analysis_id);
      if (state.card.state !== 'c') setCardState('c');
    }
  }));

  src.addEventListener('stage3_synthesis_done', sseHandler(async e => {
    const d = JSON.parse(e.data);
    // 預設 true 以向後相容：舊 event 沒帶 is_final 視為 final（legacy 行為）
    const isFinal = d.is_final !== false;

    // 非 final（舊 path 不會命中，保留防禦）→ UI 只刷 synthesis，不關 SSE、不 mark done
    if (!isFinal) {
      if (state.card.open && state.card.taskId === taskId) {
        await reloadAnalyses();
        await renderCardResults(d.analysis_id);
      }
      return;
    }

    const info = state.bell.tasks.get(taskId);
    if (info) {
      info.status = 'done';
      info.progress = 100;
      info.unread = true;
      renderNotificationBell();
      renderTaskLists();
    }

    // Close bell SSE
    const bellSrc = state.bell.sseConnections.get(taskId);
    if (bellSrc) { bellSrc.close(); state.bell.sseConnections.delete(taskId); }

    // If card is open for this task
    if (state.card.open && state.card.taskId === taskId) {
      await reloadAnalyses();
      if (state.card.state === 'c') {
        // ─── 延後 re-render 判斷：律師在「特定 cluster / starred / 無關 / data_error」
        // 或 reader 已打開時，不要馬上 re-render（renderCardResults 會 reset activeCluster
        // 回「全部」tab、clusters 重抓後 label 可能變、律師失去瀏覽脈絡）。
        // 改為設 pending flag，等律師回到「全部」tab 或關閉 reader 時才套用。
        const readerOpen = !document.getElementById('reader-card').classList.contains('hidden');
        const inSpecificView = state.card.activeCluster != null;  // null = 全部
        if (readerOpen || inSpecificView) {
          state.card._pendingFinalRefresh = d.analysis_id;
          _showPendingFinalBanner();
          renderAnalysisHistoryTabs();
        } else {
          // 已在「全部」且 reader 關閉 → 立刻 re-render
          await renderCardResults(d.analysis_id);
          renderAnalysisHistoryTabs();
        }
      } else {
        // 在 State B → 切到 State C（第一次看結果、沒有瀏覽脈絡要保護）
        await renderCardResults(d.analysis_id);
        setCardState('c');
      }
    }

    // Also refresh task lists
    const tasksRes = await apiFetch(API.tasks);
    if (tasksRes.ok) state.tasks = await tasksRes.json();
    renderTaskLists();
  }));

  src.addEventListener('analysis_done', sseHandler(e => {
    // Handled by stage3_synthesis_done (synthesis always follows analysis_done)
  }));

  // 分析被取消（rows < 3，無 partial synthesis 價值 or task 刪除）— 回 State A
  src.addEventListener('stage3_cancelled', sseHandler(e => {
    const info = state.bell.tasks.get(taskId);
    if (info) { info.status = 'cancelled'; renderNotificationBell(); }
    const bellSrc = state.bell.sseConnections.get(taskId);
    if (bellSrc) { bellSrc.close(); state.bell.sseConnections.delete(taskId); }
    if (state.card.open && state.card.taskId === taskId) {
      _clearAbortUI();
      if (state.card.state === 'b') {
        setCardState('a');
        const btn = document.getElementById('card-submit-analyze');
        btn.disabled = false; btn.textContent = 'AI　分　析'; btn.classList.remove('opacity-50');
      }
    }
  }));

  // Graceful abort + rows ≥ 3 → partial synthesis done、切 State C 看結果
  // is_final=false（第一次中止）→ bell status='paused'、SSE 保持、可 /resume
  // is_final=true（續跑中又中止，視為接受定稿）→ 走 stage3_synthesis_done 類似邏輯、關 SSE
  src.addEventListener('stage3_partial_done', sseHandler(async e => {
    const d = JSON.parse(e.data);
    const info = state.bell.tasks.get(taskId);
    if (info) {
      info.status = d.is_final ? 'done' : 'paused';
      info.progress = d.is_final ? 100 : 90;
      info.unread = true;
      renderNotificationBell();
      renderTaskLists();
    }
    // is_final=true → 等同完成、關閉 SSE
    if (d.is_final) {
      const bellSrc = state.bell.sseConnections.get(taskId);
      if (bellSrc) { bellSrc.close(); state.bell.sseConnections.delete(taskId); }
    }
    // Card open 且同一 task → 清中止 UI、切 State C 看 partial 結果
    if (state.card.open && state.card.taskId === taskId) {
      _clearAbortUI();
      await reloadAnalyses();
      await renderCardResults(d.analysis_id);
      if (state.card.state !== 'c') setCardState('c');
      renderAnalysisHistoryTabs();
    }
  }));

  // 分析失敗
  src.addEventListener('analysis_failed', sseHandler(async e => {
    const d = JSON.parse(e.data);
    const info = state.bell.tasks.get(taskId);
    if (info) { info.status = 'failed'; renderNotificationBell(); renderTaskLists(); }
    const bellSrc = state.bell.sseConnections.get(taskId);
    if (bellSrc) { bellSrc.close(); state.bell.sseConnections.delete(taskId); }
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'b') {
      renderCardError(d.error || '分析過程中發生錯誤');
    }
    // State C tabs 也需要刷新
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'c') {
      await reloadAnalyses();
      renderAnalysisHistoryTabs();
    }
  }));

  src.onerror = () => {
    src.close();
    state.bell.sseConnections.delete(taskId);
    // 分析 SSE 斷線 → 恢復卡片到 State A
    if (state.card.open && state.card.taskId === taskId && state.card.state === 'b') {
      setCardState('a');
      document.getElementById('card-submit-analyze').disabled = false;
      document.getElementById('card-submit-analyze').textContent = 'AI　分　析';
    }
    renderTaskLists();
  };
}


// ─── Judgment List ────────────────────────────────
function filteredJudgments() {
  return state.judgments.filter(j => {
    const score = j.primary_score ?? j.score ?? 0;
    if (state.filters.minScore && score < state.filters.minScore) return false;
    const match = j.primary_match ?? j.match ?? '';
    if (state.filters.matchType && match !== state.filters.matchType) return false;
    return true;
  });
}

// 欄位代號 → 中文短標籤（對應 filter.py / analyze.py 的 FIELD_LABELS）
const FIELD_LABEL_MAP = {
  reasoning: '理由',
  main_text: '主文',
  facts: '事實',
  cited_statutes: '法條',
  full_text: '全文',
};

// 主分析層的 ai_read_field（逗號分隔字串）→ 清單顯示的短標籤。
// 單欄位：顯示該欄名；多欄位：顯示「多欄位」避免 badge 過長。
function primaryFieldLabel() {
  const primary = state.analyses.find(a => a.id === state.primaryAnalysisId);
  const raw = primary?.ai_read_field || '';
  const fields = raw.split(',').map(s => s.trim()).filter(Boolean);
  if (fields.length === 0) return '—';
  if (fields.length === 1) return FIELD_LABEL_MAP[fields[0]] || fields[0];
  return '多欄位';
}

function renderJudgmentList(items) {
  const list  = document.getElementById('judgment-list');
  const empty = document.getElementById('results-empty');
  const count = document.getElementById('results-count');

  count.textContent = `顯示 ${items.length} / ${state.judgments.length} 筆`;

  if (!items.length && state.judgments.length > 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  const fieldLabel = primaryFieldLabel();

  list.innerHTML = items.map(j => {
    const score  = j.primary_score ?? j.score ?? null;
    const match  = j.primary_match ?? j.match ?? '';
    const sec    = j.secondary_score;
    const excerpt = j.primary_excerpt ?? j.excerpt ?? '';
    const matchCls = match === 'yes' ? 'match-yes' : match === 'partial' ? 'match-partial' : '';
    const scoreColor = score >= 7 ? 'text-seal' : score >= 5 ? 'text-warm-500' : 'text-warm-400';
    const matchLabel = { yes:'完全命中', partial:'部分命中', no:'未命中', error:'錯誤' }[match] || '';
    return `
      <div class="judgment-row ${matchCls} flex items-center px-6 border-b border-warm-100 cursor-pointer min-h-[64px] group fade-in"
           onclick="selectRow(this, '${escAttr(j.case_id)}')">
        <div class="flex-1 min-w-0 py-3 pr-3">
          <div class="flex items-baseline gap-2.5">
            <span class="text-sm font-serif font-semibold text-ink leading-snug">${escHtml(j.court || '')}</span>
            <span class="font-mono text-xs text-seal font-medium">${escHtml(j.case_id || '')}</span>
          </div>
          <div class="flex items-center gap-2 mt-0.5">
            <span class="font-mono text-xs text-warm-400">${j.date || ''}</span>
            ${excerpt ? `<span class="text-warm-200">·</span><span class="text-xs text-warm-500 font-serif hit-mark truncate max-w-xs">${escHtml(excerpt)}</span>` : ''}
          </div>
        </div>
        <div class="w-20 shrink-0 flex justify-center">
          <span class="text-xs font-mono bg-seal/10 text-seal border border-seal/20 px-2 py-0.5 rounded-sm">${escHtml(fieldLabel)}</span>
        </div>
        <div class="w-44 shrink-0 px-3">
          ${score !== null ? `
            <div class="flex items-center gap-2">
              <span class="font-mono text-xl font-semibold ${scoreColor} leading-none w-10">${score}</span>
              <div class="flex-1 h-1.5 bg-warm-200 rounded-full overflow-hidden">
                <div class="score-bar-fill h-full bg-seal rounded-full" data-score="${Math.round(score*10)}"></div>
              </div>
            </div>
            <div class="mt-1 text-xs font-serif text-warm-400 pl-12">${matchLabel}</div>
          ` : `<span class="text-xs font-mono text-warm-400">—</span>`}
        </div>
        <div class="w-28 shrink-0 flex justify-center">
          ${sec !== null && sec !== undefined ? `
            <div class="text-center">
              <span class="font-mono text-sm font-medium text-warm-500">${sec}</span>
              <div class="w-16 h-1 bg-warm-200 rounded-full overflow-hidden mx-auto mt-1">
                <div class="score-bar-fill h-full bg-warm-400 rounded-full" data-score="${Math.round(sec*10)}"></div>
              </div>
            </div>
          ` : `<span class="font-mono text-xs text-warm-400">—</span>`}
        </div>
        <div class="w-5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity text-warm-400 text-xs">›</div>
      </div>
    `;
  }).join('');

  // Animate score bars
  requestAnimationFrame(() => {
    document.querySelectorAll('.score-bar-fill[data-score]').forEach(el => {
      setTimeout(() => { el.style.width = el.dataset.score + '%'; }, 80);
    });
  });
}

function selectRow(el, caseId) {
  document.querySelectorAll('.judgment-row').forEach(r => r.classList.remove('selected'));
  el.classList.add('selected');
  navTo({ view: 'reader', taskId: state.currentTaskId, caseId });
}

// ─── Loading state ────────────────────────────────
function setResultsLoading(msg) {
  const el = document.getElementById('results-loading');
  const msgEl = document.getElementById('results-loading-msg');
  if (msg) {
    el.classList.remove('hidden');
    el.classList.add('flex');
    msgEl.textContent = msg;
  } else {
    el.classList.add('hidden');
    el.classList.remove('flex');
  }
}

// ─── Filters ─────────────────────────────────────
function setupDropdown(btnId, menuId, onSelect) {
  const btn  = document.getElementById(btnId);
  const menu = document.getElementById(menuId);
  if (!btn || !menu) { console.warn(`[setupDropdown] missing #${btnId} or #${menuId}`); return; }
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const open = !menu.classList.contains('hidden');
    closeAllDropdowns();
    if (!open) menu.classList.remove('hidden');
  });
  menu.querySelectorAll('.dd-item').forEach(item => {
    item.addEventListener('click', () => {
      onSelect(item.dataset.val, item.textContent.trim());
      menu.classList.add('hidden');
    });
  });
}

function closeAllDropdowns() {
  document.querySelectorAll('.dd-menu').forEach(m => m.classList.add('hidden'));
}
document.addEventListener('click', closeAllDropdowns);

setupDropdown('score-dd-btn', 'score-dd-menu', (val) => {
  state.filters.minScore = val ? parseInt(val) : 0;
  document.getElementById('score-dd-label').textContent = val || '全部';
  renderJudgmentList(filteredJudgments());
});
setupDropdown('match-dd-btn', 'match-dd-menu', (val, label) => {
  state.filters.matchType = val;
  document.getElementById('match-dd-label').textContent = val ? ` ${label}` : '';
  renderJudgmentList(filteredJudgments());
});

// ─── Follow-up (新追問) ────────────────────────────
document.getElementById('btn-new-analysis').addEventListener('click', () => {
  document.getElementById('followup-overlay').classList.remove('hidden');
  document.getElementById('followup-question').focus();
});
document.getElementById('followup-close').addEventListener('click', () =>
  document.getElementById('followup-overlay').classList.add('hidden'));
document.getElementById('followup-cancel').addEventListener('click', () =>
  document.getElementById('followup-overlay').classList.add('hidden'));

document.getElementById('followup-submit').addEventListener('click', async () => {
  const q = document.getElementById('followup-question').value.trim();
  if (!q) { document.getElementById('followup-question').focus(); return; }
  const fields = [];
  if (document.getElementById('fu-reasoning').checked) fields.push('reasoning');
  if (document.getElementById('fu-main').checked)      fields.push('main_text');
  if (document.getElementById('fu-facts').checked)     fields.push('facts');
  if (document.getElementById('fu-fulltext').checked)  fields.push('full_text');
  if (!fields.length) fields.push('reasoning');

  document.getElementById('followup-overlay').classList.add('hidden');

  try {
    const res = await apiFetch(API.analyses(state.currentTaskId), {
      method: 'POST',
      body: JSON.stringify({ question: q, ai_read_fields: fields }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { analysis_id } = await res.json();
    state.analyses.push({ id: analysis_id, question: q, status: 'running', completed: 0, total: null, match_count: 0 });
    renderAnalysisTabs(state.analyses);
    subscribeTask(state.currentTaskId, analysis_id);
  } catch (err) {
    alert(`追問失敗：${err.message}`);
  }
});

// ─── API Key helpers ──────────────────────────────
function maskKey(k) { return k ? k.slice(0, 14) + '···' + k.slice(-4) : ''; }

function updateKeyStatus() {
  const stored = localStorage.getItem(KEY_STORAGE);
  // 首頁設定按鈕的小圓點
  document.getElementById('key-dot').className =
    `w-1.5 h-1.5 rounded-full inline-block ${stored ? 'bg-emerald-500' : 'bg-amber-400 dot-warn'}`;
  // 設定面板：綁定狀態卡片
  const cur = document.getElementById('key-current');
  const empty = document.getElementById('key-empty');
  if (cur) cur.classList.toggle('hidden', !stored);
  if (empty) empty.classList.toggle('hidden', !!stored);
  const masked = document.getElementById('key-masked');
  if (masked && stored) masked.textContent = maskKey(stored);
  // 輸入框 placeholder
  const inp = document.getElementById('key-input');
  if (inp) { inp.placeholder = stored ? '輸入新金鑰以覆蓋…' : 'sk-ant-...'; inp.value = ''; }
}

document.getElementById('key-save-btn').addEventListener('click', () => {
  const v = document.getElementById('key-input').value.trim();
  if (!v) { document.getElementById('key-input').focus(); return; }
  localStorage.setItem(KEY_STORAGE, v);
  updateKeyStatus();
});
document.getElementById('key-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
    document.getElementById('key-save-btn').click();
  }
});
document.getElementById('key-clear-btn').addEventListener('click', () => {
  localStorage.removeItem(KEY_STORAGE);
  updateKeyStatus();
});
let _keyVis = false;
document.getElementById('key-toggle-vis').addEventListener('click', () => {
  _keyVis = !_keyVis;
  document.getElementById('key-input').type = _keyVis ? 'text' : 'password';
});

// ─── Settings Drawer ──────────────────────────────
const API_SETTINGS = {
  synonyms:    '/api/settings/synonyms',
  lawAbbrev:   '/api/settings/law-abbreviations',
};

let _settingsOpen = false;
let _synData      = [];   // raw list from API
let _lawData      = {};   // raw dict from API
let _synFilter    = '';
let _lawFilter    = '';

function openSettings(tabId = 'api-key') {
  _settingsOpen = true;
  // 關閉可能開著的搜尋卡片 / 閱讀器（避免 z-index 遮擋）
  if (state.card.open) closeSearchCard();
  closeReaderCard(true);
  closeTaskListCard();
  updateKeyStatus();
  document.getElementById('settings-backdrop').classList.remove('hidden');
  document.getElementById('settings-drawer').style.transform = 'translateX(0)';
  switchSettingsTab(tabId);
  // Auto-load data when opening the relevant tabs
  if (tabId === 'synonyms')   loadSynonyms();
  if (tabId === 'law-abbrev') loadLawAbbrev();
}

function closeSettings() {
  _settingsOpen = false;
  document.getElementById('settings-drawer').style.transform = 'translateX(100%)';
  document.getElementById('settings-backdrop').classList.add('hidden');
}

function switchSettingsTab(tabId) {
  document.querySelectorAll('.settings-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  document.querySelectorAll('.settings-panel').forEach(panel => {
    panel.classList.toggle('hidden', panel.id !== `settings-tab-${tabId}`);
  });
  if (tabId === 'synonyms'   && !_synData.length)              loadSynonyms();
  if (tabId === 'law-abbrev' && !Object.keys(_lawData).length) loadLawAbbrev();
  if (tabId === 'cache')                                        loadCacheStats();
}

document.getElementById('btn-settings').addEventListener('click', () => openSettings('api-key'));
document.getElementById('settings-close').addEventListener('click', closeSettings);
document.getElementById('settings-backdrop').addEventListener('click', closeSettings);
document.querySelectorAll('.settings-tab').forEach(btn => {
  btn.addEventListener('click', () => switchSettingsTab(btn.dataset.tab));
});

// Escape closes drawer too
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _settingsOpen) closeSettings();
});

// ── Synonym management ────────────────────────────
async function loadSynonyms() {
  const el = document.getElementById('syn-confirmed-list');
  el.innerHTML = '<p class="py-6 text-center text-xs font-mono text-warm-400">載入中…</p>';
  try {
    const res = await apiFetch(API_SETTINGS.synonyms);
    if (!res.ok) throw new Error(res.status);
    _synData = await res.json();
    renderSynonyms();
  } catch (err) {
    el.innerHTML = `<p class="py-6 text-center text-xs font-mono text-red-500">載入失敗：${escHtml(String(err))}</p>`;
  }
}

document.getElementById('syn-reload').addEventListener('click', loadSynonyms);

// 手動新增同義詞
document.getElementById('syn-add-btn').addEventListener('click', async () => {
  const canonical = document.getElementById('syn-add-canonical').value.trim();
  const variant = document.getElementById('syn-add-variant').value.trim();
  if (!canonical || !variant) return;
  if (canonical === variant) return;
  const res = await apiFetch('/api/synonyms/add', {
    method: 'POST',
    body: JSON.stringify({ canonical, variant }),
  });
  if (res.ok) {
    document.getElementById('syn-add-canonical').value = '';
    document.getElementById('syn-add-variant').value = '';
    await loadSynonyms();
  }
});
// Enter key to add
document.getElementById('syn-add-variant').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.isComposing) document.getElementById('syn-add-btn').click();
});

// 匯出：把已核准的同義詞匯出為 JSON
document.getElementById('syn-export').addEventListener('click', () => {
  const confirmed = _synData.filter(r => r.tier === 'confirmed');
  const groups = {};
  confirmed.forEach(r => {
    if (!groups[r.canonical]) groups[r.canonical] = [];
    if (r.variant !== r.canonical) groups[r.canonical].push(r.variant);
  });
  const blob = new Blob([JSON.stringify(groups, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `synonyms_${new Date().toISOString().slice(0,10)}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

// 匯入：讀 JSON 檔案，每組寫入 confirmed
document.getElementById('syn-import').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const groups = JSON.parse(text);
    // 格式：{ "原詞": ["同義詞1", "同義詞2"], ... }
    let count = 0;
    for (const [canonical, variants] of Object.entries(groups)) {
      if (!Array.isArray(variants)) continue;
      for (const variant of variants) {
        if (!variant || variant.length <= 1 || variant === canonical) continue;
        await apiFetch('/api/synonyms/add', {
          method: 'POST',
          body: JSON.stringify({ canonical, variant }),
        }).catch(() => {});
        count++;
      }
    }
    await loadSynonyms();
    alert(`匯入完成：${count} 組同義詞`);
  } catch (err) {
    alert(`匯入失敗：${err.message}`);
  }
  e.target.value = '';  // reset file input
});
document.getElementById('syn-search').addEventListener('input', e => {
  _synFilter = e.target.value.trim().toLowerCase();
  renderSynonyms();
});

const _LAW_SUFFIXES = ['法', '規則', '條例', '辦法'];
function _isLawSynonym(canonical) {
  return _LAW_SUFFIXES.some(s => canonical.endsWith(s));
}

function renderSynonyms() {
  if (!_synData.length) {
    document.getElementById('syn-pending-section').classList.add('hidden');
    document.getElementById('syn-confirmed-list').innerHTML = '<p class="py-8 text-center text-xs font-mono text-warm-400">同義詞庫為空</p>';
    document.getElementById('syn-confirmed-count').textContent = '';
    return;
  }

  // Split by tier: confirmed vs pending (candidate). Ignore likely_typo and rejected.
  const confirmed = _synData.filter(r => r.tier === 'confirmed');
  const pending = _synData.filter(r => r.tier === 'candidate');

  // Group helper
  const groupByCanonical = (rows) => {
    const groups = {};
    rows.forEach(r => { if (!groups[r.canonical]) groups[r.canonical] = []; groups[r.canonical].push(r); });
    return Object.entries(groups).filter(([canon]) =>
      !_synFilter || canon.toLowerCase().includes(_synFilter) ||
      groups[canon].some(r => r.variant.toLowerCase().includes(_synFilter))
    );
  };

  // ── Pending section ──
  const pendingGroups = groupByCanonical(pending);
  const pendingSection = document.getElementById('syn-pending-section');
  const pendingList = document.getElementById('syn-pending-list');
  if (pendingGroups.length) {
    document.getElementById('syn-pending-count').textContent = `${pending.length} 組`;
    pendingList.innerHTML = pendingGroups.map(([canon, rows]) => `
      <div class="py-3">
        <div class="font-serif text-sm font-semibold text-ink mb-1.5">${escHtml(canon)}</div>
        <div class="space-y-1 pl-3">
          ${rows.map(r => `
            <div class="flex items-center gap-2 group">
              <span class="font-mono text-xs text-warm-600">${escHtml(r.variant)}</span>
              <span class="text-[10px] font-mono text-warm-400">${r.corpus_hits != null ? r.corpus_hits + '筆' : ''}</span>
              <div class="ml-auto flex gap-1">
                <button onclick="approveSynonym('${escAttr(canon)}','${escAttr(r.variant)}')"
                        aria-label="同意" title="加入詞庫"
                        class="text-[10px] font-mono px-1.5 py-0.5 border border-emerald-300 text-emerald-700 hover:bg-emerald-50 transition-colors rounded-sm">✓</button>
                <button onclick="rejectSynonym('${escAttr(canon)}','${escAttr(r.variant)}')"
                        aria-label="拒絕" title="拒絕並移除"
                        class="text-[10px] font-mono px-1.5 py-0.5 border border-red-200 text-red-500 hover:bg-red-50 transition-colors rounded-sm">✕</button>
              </div>
            </div>
          `).join('')}
        </div>
      </div>`).join('');
    pendingSection.classList.remove('hidden');
  } else {
    pendingSection.classList.add('hidden');
  }

  // ── Confirmed section: split by 法規 vs 詞彙 ──
  const confirmedGroups = groupByCanonical(confirmed);
  const lawGroups = confirmedGroups.filter(([canon]) => _isLawSynonym(canon));
  const wordGroups = confirmedGroups.filter(([canon]) => !_isLawSynonym(canon));
  const confirmedList = document.getElementById('syn-confirmed-list');
  document.getElementById('syn-confirmed-count').textContent = confirmedGroups.length ? `${confirmedGroups.length} 組` : '';

  const renderGroup = ([canon, rows]) => `
    <div class="py-3">
      <div class="font-serif text-sm font-semibold text-ink mb-1.5">${escHtml(canon)}</div>
      <div class="flex flex-wrap gap-1.5 pl-3 items-center">
        ${rows.map(r => `
          <span class="group relative font-mono text-xs border border-emerald-200 bg-emerald-50/40 text-emerald-800 rounded-sm px-2 py-0.5">
            ${escHtml(r.variant)}
            <button onclick="deleteSynVariant('${escAttr(canon)}','${escAttr(r.variant)}')"
                    class="absolute -top-1 -right-1 w-3.5 h-3.5 bg-red-500 text-white text-[8px] rounded-full
                           flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity
                           leading-none" aria-label="移除">×</button>
          </span>
        `).join('')}
        <button onclick="startInlineAdd('${escAttr(canon)}', this)" aria-label="新增變體"
                class="w-5 h-5 flex items-center justify-center border border-dashed border-warm-300
                       text-warm-400 hover:border-seal hover:text-seal rounded-sm transition-colors text-sm leading-none">+</button>
      </div>
    </div>`;

  let html = '';
  if (wordGroups.length) {
    html += `<div class="pt-2 pb-1"><span class="text-[10px] font-mono text-warm-400 uppercase tracking-widest">詞彙</span>
             <span class="text-[10px] font-mono text-warm-400 ml-1">${wordGroups.length} 組</span></div>`;
    html += wordGroups.map(renderGroup).join('');
  }
  if (lawGroups.length) {
    html += `<div class="pt-4 pb-1 ${wordGroups.length ? 'border-t border-warm-100 mt-2' : ''}">
             <span class="text-[10px] font-mono text-warm-400 uppercase tracking-widest">法規</span>
             <span class="text-[10px] font-mono text-warm-400 ml-1">${lawGroups.length} 組</span></div>`;
    html += lawGroups.map(renderGroup).join('');
  }
  if (!html) {
    html = '<p class="py-8 text-center text-xs font-mono text-warm-400">尚無已核准的同義詞</p>';
  }
  confirmedList.innerHTML = html;
}

// ✓ 同意 → 直接升 confirmed（3 次 accept 觸發，這裡直接打 3 次）
async function approveSynonym(canonical, variant) {
  for (let i = 0; i < 3; i++) {
    await apiFetch('/api/synonym-feedback', {
      method: 'POST',
      body: JSON.stringify({ canonical, variant, accepted: true }),
    }).catch(() => {});
  }
  await loadSynonyms();
}

// ✕ 拒絕 → 標記為 rejected（不再推薦），從 UI 消失
async function rejectSynonym(canonical, variant) {
  // 連續 reject 確保 tier 降為 rejected
  for (let i = 0; i < 3; i++) {
    await apiFetch('/api/synonym-feedback', {
      method: 'POST',
      body: JSON.stringify({ canonical, variant, accepted: false }),
    }).catch(() => {});
  }
  _synData = _synData.filter(r => !(r.canonical === canonical && r.variant === variant));
  renderSynonyms();
}

// 已核准詞彙旁的 + 按鈕 → inline 輸入框
function startInlineAdd(canonical, btnEl) {
  // 已經有輸入框就不重複建
  if (btnEl.parentElement.querySelector('.syn-inline-input')) return;
  const wrapper = document.createElement('span');
  wrapper.className = 'syn-inline-input flex items-center gap-1';
  wrapper.innerHTML = `
    <input type="text" placeholder="新變體" autofocus
      class="w-20 border-b border-seal bg-transparent text-xs font-mono text-ink
             placeholder-warm-300 outline-none py-0.5" />
    <button class="text-[10px] font-mono text-seal hover:underline">加</button>
    <button class="text-[10px] font-mono text-warm-400 hover:text-ink">取消</button>`;
  btnEl.parentElement.insertBefore(wrapper, btnEl);
  const input = wrapper.querySelector('input');
  const addBtn = wrapper.querySelectorAll('button')[0];
  const cancelBtn = wrapper.querySelectorAll('button')[1];
  input.focus();

  const doAdd = async () => {
    const v = input.value.trim();
    if (!v || v === canonical) { wrapper.remove(); return; }
    const res = await apiFetch('/api/synonyms/add', {
      method: 'POST',
      body: JSON.stringify({ canonical, variant: v }),
    });
    if (res.ok) await loadSynonyms();
    else wrapper.remove();
  };
  addBtn.addEventListener('click', doAdd);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) doAdd();
    if (e.key === 'Escape') wrapper.remove();
  });
  cancelBtn.addEventListener('click', () => wrapper.remove());
}

// 已核准詞庫的移除 → 直接刪除，不問
async function deleteSynVariant(canonical, variant) {
  await apiFetch(API_SETTINGS.synonyms, {
    method: 'DELETE',
    body: JSON.stringify({ canonical, variant }),
  }).catch(() => {});
  _synData = _synData.filter(r => !(r.canonical === canonical && r.variant === variant));
  renderSynonyms();
}

// ── Law abbreviation management ───────────────────
async function loadLawAbbrev() {
  const el = document.getElementById('law-list');
  el.innerHTML = '<p class="py-6 text-center text-xs font-mono text-warm-400">載入中…</p>';
  try {
    const res = await apiFetch(API_SETTINGS.lawAbbrev);
    if (!res.ok) throw new Error(res.status);
    _lawData = await res.json();
    renderLawAbbrev();
  } catch (err) {
    el.innerHTML = `<p class="py-6 text-center text-xs font-mono text-red-500">載入失敗：${escHtml(String(err))}</p>`;
  }
}

document.getElementById('law-reload').addEventListener('click', loadLawAbbrev);
document.getElementById('law-search').addEventListener('input', e => {
  _lawFilter = e.target.value.trim().toLowerCase();
  renderLawAbbrev();
});

function renderLawAbbrev() {
  const el = document.getElementById('law-list');
  const entries = Object.entries(_lawData).filter(([full, abbrevs]) =>
    !_lawFilter ||
    full.toLowerCase().includes(_lawFilter) ||
    abbrevs.some(a => a.toLowerCase().includes(_lawFilter))
  );

  if (!entries.length) {
    el.innerHTML = Object.keys(_lawData).length
      ? '<p class="py-8 text-center text-xs font-mono text-warm-400">無符合結果</p>'
      : '<p class="py-8 text-center text-xs font-mono text-warm-400">法條簡稱字典為空</p>';
    return;
  }

  el.innerHTML = entries.map(([full, abbrevs]) => `
    <div class="py-3 group">
      <div class="flex items-center gap-2 mb-1.5">
        <span class="font-serif text-sm text-ink font-semibold">${escHtml(full)}</span>
        <button onclick="deleteLawEntry('${escAttr(full)}', null)"
                aria-label="刪除整條法名" title="刪除整條（含所有簡稱）"
                class="opacity-0 group-hover:opacity-100 transition-opacity ml-auto text-[10px] font-mono
                       border border-red-200 text-red-500 hover:bg-red-50 px-1.5 py-0.5 transition-colors">
          刪除整條
        </button>
      </div>
      <div class="flex flex-wrap gap-1.5 pl-2">
        ${abbrevs.map(abbr => `
          <span class="inline-flex items-center gap-1 font-mono text-xs bg-warm-100 border border-warm-200 px-2 py-0.5">
            ${escHtml(abbr)}
            <button onclick="deleteLawEntry('${escAttr(full)}','${escAttr(abbr)}')"
                    aria-label="刪除簡稱 ${escAttr(abbr)}" title="刪除此簡稱"
                    class="text-warm-400 hover:text-red-500 transition-colors ml-0.5">×</button>
          </span>`).join('')}
      </div>
    </div>`).join('');
}

// ── Cache management ──────────────────────────────
async function loadCacheStats() {
  const el = document.getElementById('cache-stats');
  el.innerHTML = '<p class="text-xs font-mono text-warm-400">載入中…</p>';
  try {
    const res = await apiFetch('/api/settings/cache-stats');
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    const mb = (data.db_size_bytes / 1024 / 1024).toFixed(2);
    const tables = data.tables || {};
    const TABLE_LABELS = {
      judgment_cache:   '判決全文',
      search_cache:     '搜尋結果',
      regulation_cache: '法規條文',
    };
    el.innerHTML = `
      <div class="flex items-baseline gap-2 mb-2">
        <span class="font-mono text-lg font-semibold text-ink">${mb} MB</span>
        <span class="text-xs text-warm-400 font-serif">快取資料庫大小</span>
      </div>
      <div class="grid grid-cols-3 gap-2">
        ${Object.entries(tables).map(([t, v]) => `
          <div class="text-center">
            <div class="font-mono text-sm text-ink">${v.total}</div>
            <div class="text-[10px] text-warm-400 font-serif">${TABLE_LABELS[t] || t}</div>
            ${v.expired > 0
              ? `<div class="text-[10px] font-mono text-amber-400">${v.expired} 已過期</div>`
              : `<div class="text-[10px] font-mono text-warm-400">無過期</div>`}
          </div>`).join('')}
      </div>`;
  } catch (err) {
    el.innerHTML = `<p class="text-xs font-mono text-red-500">載入失敗：${escHtml(String(err))}</p>`;
  }
}

async function clearCache(expiredOnly) {
  const msgEl = document.getElementById('cache-action-msg');
  msgEl.classList.remove('hidden');
  msgEl.textContent = '清除中…';
  try {
    const res = await apiFetch(`/api/settings/cache?expired_only=${expiredOnly}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    const total = Object.values(data.deleted).reduce((a, b) => a + b, 0);
    const before = (data.db_size_before / 1024 / 1024).toFixed(2);
    const after  = (data.db_size_after  / 1024 / 1024).toFixed(2);
    msgEl.textContent = `已刪除 ${total} 筆，${before} MB → ${after} MB`;
    msgEl.className = 'text-xs font-mono text-emerald-700 mt-2';
    await loadCacheStats();
  } catch (err) {
    msgEl.textContent = `失敗：${err.message}`;
    msgEl.className = 'text-xs font-mono text-red-500 mt-2';
  }
}

document.getElementById('cache-refresh-stats').addEventListener('click', loadCacheStats);
document.getElementById('btn-clear-expired').addEventListener('click', () => clearCache(true));
document.getElementById('btn-clear-all-cache').addEventListener('click', () => {
  if (!confirm('確定清除全部快取？下次搜尋時所有判決全文將重新從司法院抓取。')) return;
  clearCache(false);
});

async function deleteLawEntry(fullName, abbreviation) {
  const msg = abbreviation
    ? `確定刪除「${fullName}」的簡稱「${abbreviation}」？`
    : `確定刪除「${fullName}」及其所有簡稱？`;
  if (!confirm(msg)) return;
  const res = await apiFetch(API_SETTINGS.lawAbbrev, {
    method: 'DELETE',
    body: JSON.stringify({ full_name: fullName, abbreviation: abbreviation }),
  });
  if (res.ok) {
    await loadLawAbbrev();
  } else {
    alert('刪除失敗');
  }
}

// ─── Utilities ───────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s) { return String(s).replace(/"/g,'&quot;'); }

// ─── Reader (State C) ─────────────────────────────
let _readerData      = null;
let _readerCaseId    = null;
let _readerFieldTab  = 'reasoning';
let _summaryOpen     = true;

function openReaderPanel() {
  document.getElementById('results-col-headers').classList.add('hidden');
  document.getElementById('judgment-list').classList.add('hidden');
  document.getElementById('results-empty').classList.add('hidden');
  const panel = document.getElementById('results-reader-panel');
  panel.classList.remove('hidden');
  panel.classList.add('flex');
}

function closeReaderPanel() {
  const panel = document.getElementById('results-reader-panel');
  panel.classList.add('hidden');
  panel.classList.remove('flex');
  document.getElementById('results-col-headers').classList.remove('hidden');
  document.getElementById('judgment-list').classList.remove('hidden');
  // Re-show empty if needed
  if (!filteredJudgments().length && state.judgments.length > 0) {
    document.getElementById('results-empty').classList.remove('hidden');
  }
}

async function openReader(caseId) {
  _readerCaseId = caseId;
  openReaderPanel();

  // 兩階段流程：reader 可能從 stage 2（hits 已抓但 task_judgments 還沒）開啟，
  // 也可能從 stage 3 / legacy 結果（task_judgments 已存在）開啟。
  // - stage 2 模式：narrow list 用 stage 2 filtered hits；data 走 /hits/{case_id}（MCP 即時）
  // - stage 3 / legacy：narrow list 用 filteredJudgments()；data 走 /judgments/{case_id}（DB cache）
  const isStage2Mode = state.hits.length > 0 && state.hits.some(h => h.case_id === caseId);

  // Render narrow list
  let items;
  if (isStage2Mode) {
    items = applyStage2Filters();
    // 安全網：若當前 caseId 不在 filtered 結果裡（律師更動 filter 後深連結），fallback 到完整 hits
    if (!items.some(h => h.case_id === caseId)) items = state.hits;
  } else {
    items = filteredJudgments();
  }

  document.getElementById('reader-list-count').textContent = `${items.length} 筆`;
  document.getElementById('reader-list-inner').innerHTML = items.map(j => {
    const score = j.primary_score ?? j.score ?? null;
    const match = j.primary_match ?? j.match ?? '';
    const borderCls = match === 'yes' ? 'border-l-seal' : match === 'partial' ? 'border-l-seal/40' : 'border-l-transparent';
    const scoreLine = score !== null
      ? `<div class="font-mono text-[10px] text-warm-400 mt-0.5">${score} 分</div>`
      : (j.cause ? `<div class="font-mono text-[10px] text-warm-400 mt-0.5">${escHtml(j.cause)}</div>` : '');
    return `
      <div class="border-l-4 ${borderCls} px-3 py-2.5 cursor-pointer transition-colors
                  ${j.case_id === caseId ? 'bg-seal/5' : 'hover:bg-warm-100'}"
           onclick="navTo({view:'reader', taskId:state.currentTaskId, caseId:'${escAttr(j.case_id)}'}, true)" role="option" aria-selected="${j.case_id === caseId}">
        <div class="font-serif text-xs text-ink font-semibold leading-snug truncate">${escHtml(j.court || '')}</div>
        <div class="font-mono text-[11px] text-seal truncate">${escHtml(j.case_id)}</div>
        ${scoreLine}
      </div>`;
  }).join('');

  // Scroll active item into view in narrow column
  requestAnimationFrame(() => {
    const active = document.querySelector('#reader-list-inner [aria-selected="true"]');
    if (active) active.scrollIntoView({ block: 'nearest' });
  });

  // Show loading
  document.getElementById('reader-loading').classList.remove('hidden');
  document.getElementById('reader-loading').classList.add('flex');
  document.getElementById('reader-content').classList.add('hidden');
  document.getElementById('reader-empty').classList.add('hidden');

  // 選擇 endpoint：stage 2 → /hits（MCP fresh，含 cause/summary）；否則走舊 /judgments（DB cache）
  // case_id 含逗號用 :path 後綴的 FastAPI 路由處理（/hits/{case_id:path}）
  const url = isStage2Mode
    ? `/api/tasks/${state.currentTaskId}/hits/${encodeURIComponent(caseId)}`
    : `/api/tasks/${state.currentTaskId}/judgments/${encodeURIComponent(caseId)}`;

  try {
    const res = await apiFetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _readerData = await res.json();
    renderReaderContent(_readerData, items.find(j => j.case_id === caseId));
  } catch (err) {
    document.getElementById('reader-loading').classList.add('hidden');
    document.getElementById('reader-loading').classList.remove('flex');
    document.getElementById('reader-empty').classList.remove('hidden');
    document.getElementById('reader-empty').textContent = `載入失敗：${err.message}`;
  }
}

function renderReaderContent(judgment, listItem) {
  document.getElementById('reader-loading').classList.add('hidden');
  document.getElementById('reader-loading').classList.remove('flex');

  // 兩階段流程：stage 2 模式 reader 沒有 AI 分析結果 → 整張 AI 摘要卡隱藏。
  const isStage2Mode = state.hits.length > 0 && state.hits.some(h => h.case_id === _readerCaseId);

  // ── AI summary card ──
  const score   = listItem?.primary_score ?? null;
  const match   = listItem?.primary_match ?? '';
  const excerpt = listItem?.primary_excerpt ?? '';
  const reason  = listItem?.primary_reason  ?? listItem?.reason ?? '';
  const primaryA = state.analyses.find(a => a.id === state.primaryAnalysisId);
  const q = primaryA?.question ?? '';

  // AI 摘要卡：stage 2 模式（沒做過分析）整張隱藏；有 analysis 時才顯示
  const summaryCard = document.getElementById('summary-card-toggle')?.parentElement;
  if (summaryCard) {
    summaryCard.classList.toggle('hidden', isStage2Mode || !primaryA);
  }

  document.getElementById('reader-analysis-label').textContent =
    q.length > 35 ? q.slice(0, 35) + '…' : q;
  document.getElementById('reader-score-badge').textContent =
    score !== null ? `${score}` : '';

  const matchMeta = {
    yes:     { cls: 'bg-emerald-50 text-emerald-700', label: '完全命中' },
    partial: { cls: 'bg-amber-50 text-amber-700',     label: '部分命中' },
    no:      { cls: 'bg-warm-100 text-warm-400',       label: '未命中' },
    error:   { cls: 'bg-red-50 text-red-600',          label: '錯誤' },
  };
  const mb = document.getElementById('reader-match-badge');
  if (match && matchMeta[match]) {
    mb.className = `inline-flex items-center text-xs font-mono px-2 py-0.5 rounded-sm ${matchMeta[match].cls}`;
    mb.textContent = matchMeta[match].label;
    mb.classList.remove('hidden');
  } else {
    mb.classList.add('hidden');
  }

  const excerptEl = document.getElementById('reader-excerpt-full');
  excerptEl.textContent = excerpt;
  excerptEl.style.display = excerpt ? '' : 'none';

  const reasonEl = document.getElementById('reader-reason');
  reasonEl.textContent = reason;
  reasonEl.style.display = reason ? '' : 'none';

  // ── Field tabs ──
  const FIELDS = [
    { key: 'reasoning',      label: '理由'    },
    { key: 'main_text',      label: '主文'    },
    { key: 'facts',          label: '事實'    },
    { key: 'cited_statutes', label: '引用法條' },
    { key: 'full_text',      label: '全文'    },
  ];
  const availTabs = FIELDS.filter(f => judgment[f.key] && judgment[f.key].length > 2);
  if (!availTabs.length) availTabs.push(FIELDS[0]);

  // Default to the AI-read field, else first available
  const aiField = (primaryA?.ai_read_field || '').split(',')[0].trim();
  _readerFieldTab = availTabs.find(t => t.key === aiField)?.key || availTabs[0].key;

  renderReaderFieldTabs(availTabs);
  renderReaderText(judgment);

  // ── Footer ──
  // prev/next 同樣依 stage 2 / legacy 模式選擇 items 來源
  const items = isStage2Mode ? applyStage2Filters() : filteredJudgments();
  const idx = items.findIndex(j => j.case_id === _readerCaseId);
  const prevBtn = document.getElementById('reader-prev');
  const nextBtn = document.getElementById('reader-next');
  prevBtn.disabled = idx <= 0;
  nextBtn.disabled = idx >= items.length - 1;
  // prev/next 用 replaceState — reader 內逐筆瀏覽不該污染 history（按上一頁應回清單）
  prevBtn.onclick = () => {
    if (idx > 0) navTo({ view: 'reader', taskId: state.currentTaskId, caseId: items[idx - 1].case_id }, true);
  };
  nextBtn.onclick = () => {
    if (idx < items.length - 1) navTo({ view: 'reader', taskId: state.currentTaskId, caseId: items[idx + 1].case_id }, true);
  };

  // Copy citation
  document.getElementById('reader-copy-cite').onclick = () => {
    const cite = formatCitation(judgment);
    navigator.clipboard.writeText(cite).then(() => {
      const btn = document.getElementById('reader-copy-cite');
      const orig = btn.textContent;
      btn.textContent = '✓ 已複製';
      setTimeout(() => { btn.textContent = orig; }, 1400);
    }).catch(() => {});
  };

  // Source link
  const srcLink = document.getElementById('reader-source-link');
  if (judgment.source_url) {
    srcLink.href = judgment.source_url;
    srcLink.classList.remove('hidden');
  } else {
    srcLink.classList.add('hidden');
  }

  document.getElementById('reader-content').classList.remove('hidden');
  document.getElementById('reader-content').classList.add('fade-in');
  document.getElementById('reader-empty').classList.add('hidden');
}

function renderReaderFieldTabs(tabs) {
  document.getElementById('reader-field-tabs').innerHTML = tabs.map(t => `
    <button role="tab" aria-selected="${t.key === _readerFieldTab}"
            class="reader-field-tab text-xs font-mono px-4 py-2 border-b-2 transition-colors
                   ${t.key === _readerFieldTab ? 'border-seal text-seal' : 'border-transparent text-warm-400 hover:text-ink'}"
            data-field="${t.key}" onclick="switchReaderField('${t.key}')">
      ${escHtml(t.label)}
    </button>`).join('');
}

function switchReaderField(key) {
  _readerFieldTab = key;
  document.querySelectorAll('.reader-field-tab').forEach(btn => {
    const active = btn.dataset.field === key;
    btn.className = `reader-field-tab text-xs font-mono px-4 py-2 border-b-2 transition-colors
                     ${active ? 'border-seal text-seal' : 'border-transparent text-warm-400 hover:text-ink'}`;
    btn.setAttribute('aria-selected', active);
  });
  if (_readerData) renderReaderText(_readerData);
}

function renderReaderText(judgment) {
  const el = document.getElementById('reader-text');
  const taskKw = state.tasks.find(t => t.id === state.currentTaskId)?.keyword || '';

  if (_readerFieldTab === 'cited_statutes') {
    try {
      const list = JSON.parse(judgment.cited_statutes || '[]');
      el.innerHTML = list.length
        ? list.map(s => `<div class="py-1 border-b border-warm-100 text-sm font-serif">${escHtml(s)}</div>`).join('')
        : '<span class="text-warm-400 font-mono text-xs">無引用法條資料</span>';
    } catch {
      el.textContent = judgment.cited_statutes || '';
    }
    return;
  }

  const text = judgment[_readerFieldTab] || '';
  if (!text) {
    el.innerHTML = '<span class="text-warm-400 font-mono text-xs">此欄位無資料</span>';
    return;
  }
  el.innerHTML = highlightKeywords(text, taskKw);
}

function highlightKeywords(text, keyword) {
  const escaped = escHtml(text);
  if (!keyword) return escaped;
  const terms = keyword.split(/\s+/).filter(k => k.length >= 2)
    .map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!terms.length) return escaped;
  return escaped.replace(new RegExp(`(${terms.join('|')})`, 'g'),
    '<mark class="hit-mark">$1</mark>');
}

function formatCitation(judgment) {
  const court = judgment.court || '';
  const parts = (judgment.case_id || '').split(',');
  const year = parts[1] || '';
  const type = parts[2] || '';
  const num  = parts[3] || '';
  if (!year || !type || !num) return `${court} ${judgment.case_id || ''}`;
  return `${court}${year}年度${type}字第${num}號判決意旨參照`;
}

function toggleSummaryCard() {
  _summaryOpen = !_summaryOpen;
  document.getElementById('reader-summary-body').style.display = _summaryOpen ? '' : 'none';
  document.getElementById('summary-chevron').style.transform = _summaryOpen ? '' : 'rotate(180deg)';
  document.getElementById('summary-card-toggle').setAttribute('aria-expanded', _summaryOpen);
}

// Back to list button — 走 history.back() 以便瀏覽器前進按鈕能再回到 reader
document.getElementById('reader-back-list').addEventListener('click', () => {
  // 若 history 沒得回（直接深連結），就主動 nav 回 results
  if (history.state && history.state.view === 'reader' && state.currentTaskId) {
    history.back();
  } else if (state.currentTaskId) {
    navTo({ view: 'results', taskId: state.currentTaskId });
  } else {
    navTo({ view: 'home' });
  }
});

// ── Single-judgment Q&A ──
document.getElementById('reader-qa-submit').addEventListener('click', handleReaderQA);
document.getElementById('reader-qa-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) handleReaderQA();
});

async function handleReaderQA() {
  const input  = document.getElementById('reader-qa-input');
  const thread = document.getElementById('reader-qa-thread');
  const q = input.value.trim();
  if (!q || !_readerCaseId || !state.currentTaskId) return;
  input.value = '';

  thread.insertAdjacentHTML('beforeend', `
    <div class="text-right">
      <span class="inline-block font-serif text-xs bg-warm-100 border border-warm-200 px-3 py-1.5 max-w-xs text-left">${escHtml(q)}</span>
    </div>`);

  const lid = `qa-${Date.now()}`;
  thread.insertAdjacentHTML('beforeend', `
    <div id="${lid}" class="text-left">
      <span class="inline-block font-mono text-xs text-warm-400 px-2 py-1.5">思考中…</span>
    </div>`);
  thread.scrollTop = thread.scrollHeight;

  try {
    const res = await apiFetch(
      `/api/tasks/${state.currentTaskId}/judgments/${encodeURIComponent(_readerCaseId)}/ask`,
      { method: 'POST', body: JSON.stringify({ question: q, field: _readerFieldTab }) }
    );
    if (!res.ok) throw new Error(await res.text());
    const { answer } = await res.json();
    document.getElementById(lid).innerHTML = `
      <span class="inline-block font-serif text-xs border border-warm-200 bg-warm-50 px-3 py-2 max-w-sm leading-relaxed">${escHtml(answer)}</span>`;
  } catch (err) {
    const msg = err.message.includes('404') ? '（此功能需要後端支援，即將推出）' : escHtml(err.message);
    document.getElementById(lid).innerHTML = `
      <span class="inline-block font-mono text-xs text-warm-400 px-2 py-1.5">${msg}</span>`;
  }
  thread.scrollTop = thread.scrollHeight;
}

// ─── Escape 關閉所有 modal ────────────────────────
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!document.getElementById('followup-overlay').classList.contains('hidden'))
    document.getElementById('followup-overlay').classList.add('hidden');
  if (!document.getElementById('expand-overlay').classList.contains('hidden')) {
    document.getElementById('expand-overlay').classList.add('hidden');
    document.getElementById('expand-overlay').classList.remove('flex');
  }
  // Settings drawer Escape is handled in its own listener above
});

// ─── 刪除任務 ────────────────────────────────────
async function deleteTask(taskId, event) {
  event.stopPropagation();
  const t = state.tasks.find(x => x.id === taskId);
  const isRunning = t && (t.status === 'running' || t.status === 'pending');
  const msg = isRunning
    ? '此任務正在執行，刪除後將強制中斷運算（已抓取的判決資料會一併清除）。確定刪除？'
    : '確定要刪除此任務？此操作不可復原。';
  if (!confirm(msg)) return;
  try {
    const res = await apiFetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || res.status);
    }
    // 若正在訂閱這個任務的 SSE，先關掉避免持續收到 worker 送出的事件
    if (state.sse && state.currentTaskId === taskId) {
      try { state.sse.close(); } catch {}
      state.sse = null;
    }
    state.tasks = state.tasks.filter(t => t.id !== taskId);
    if (state.currentTaskId === taskId) {
      state.currentTaskId = null;
      state.analyses = [];
      navTo({ view: 'home' });   // 走 navTo 才會 push history entry
    }
    updateTaskDropdownLabel();
    renderTaskDropdown();
    renderTaskLists();
  } catch (err) {
    alert(`刪除失敗：${err.message}`);
  }
}

