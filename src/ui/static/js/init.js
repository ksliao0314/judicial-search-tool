'use strict';
// ─── Init ─────────────────────────────────────────
updateKeyStatus();
// 寫入起始 history entry，這樣第一次 popstate 才有 state 可讀
history.replaceState({ view: 'home' }, '', location.pathname);
// 第一次使用自動開設定頁（尚未設 API Key）
if (!localStorage.getItem(KEY_STORAGE)) {
  setTimeout(() => openSettings('api-key'), 600);
}

loadHomeTasks();
initStarredCases();
startWorkersPolling();

// ⌘K / Ctrl+K 聚焦首頁搜尋框（只在 home view 有效）
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    if (state.view !== 'home') return;
    e.preventDefault();
    const input = document.getElementById('main-search');
    if (input) input.focus();
  }
});

// 「查看全部 →」→ 切到歷史搜尋
document.getElementById('home-recent-view-all')?.addEventListener('click', () => {
  if (typeof showView === 'function') showView('history');
  setActiveNavTab('history');
});

// Header nav tab click 切 view — 用 navTo 透過 history state 走
document.querySelectorAll('[data-nav-target]').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.navTarget;
    // 關任何 overlay（搜尋卡片 / reader / tasklist）
    document.getElementById('search-card-backdrop')?.classList.add('hidden');
    document.getElementById('search-card')?.classList.add('hidden');
    document.getElementById('reader-card-backdrop')?.classList.add('hidden');
    document.getElementById('reader-card')?.classList.add('hidden');
    document.getElementById('tasklist-card')?.classList.add('hidden');
    if (typeof unlockBodyScroll === 'function') unlockBodyScroll();
    if (typeof showView === 'function') showView(target);
    setActiveNavTab(target);
  });
});

function setActiveNavTab(target) {
  document.querySelectorAll('.nav-tab').forEach(t => {
    const isActive = t.dataset.navTarget === target;
    t.classList.toggle('nav-tab-active', isActive);
  });
}
