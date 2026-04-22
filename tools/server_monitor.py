"""macOS 選單列 server monitor — 即時狀態 + 一鍵重啟 + 任務進度。

用途：
  * 顯示 judgment-search server（port 8765）是否活著
  * 顯示當前進行中分析的進度（N/M 筆、命中 K）於選單列標題
  * 伺服器死掉時一鍵重啟，既有任務會走 backend recovery 流程自動復原
  * 伺服器狀態變化時發 macOS notification，不用盯著看
  * 「開機自動啟動」選項（透過 osascript 註冊 Login Items）

設計原則：
  * 不重啟時絕不碰 server：純 HTTP HEAD 探測，不傳送任何 mutation
  * Restart 流程只動 uvicorn process，不動 SQLite / log / data 任何檔案
  * 已 persist 到 DB 的 task / analysis 完全不受影響；partial 狀態的分析
    會保留等律師 resume（不會自動打 retry、因為需要 API key）

依賴：
  * rumps（menu bar framework、pip install rumps）
  * requests（HTTP 探測、通常已裝；沒的話 pip install requests）

啟動：
  /path/to/judgment-search/.venv/bin/python tools/server_monitor.py
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import rumps  # type: ignore[import-not-found]
import requests  # type: ignore[import-not-found]

# ── 配置 ────────────────────────────────────────────
# 當此腳本被 .app bundle 執行時、__file__ 仍指向原始 tools/ 路徑（bundle 的
# run shell script 用絕對路徑 exec python tools/server_monitor.py）、所以
# Path(__file__).parents[1] 可以正確 resolve 到專案根。
PROJECT_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
LOG_PATH = PROJECT_DIR / "logs" / "server.log"
HEALTH_URL = "http://127.0.0.1:8765/"
TASKS_URL = "http://127.0.0.1:8765/api/tasks"
UVICORN_PATTERN = "uvicorn src.main:app"  # pgrep / pkill 用、匹配完整 arg
POLL_INTERVAL_SEC = 5
STARTUP_WAIT_SEC = 12  # 重啟後等多久判定「真的起來」

# Login Items 註冊用的 .app 路徑（build_app.sh 產出的位置）
APP_BUNDLE_PATH = Path.home() / "Applications" / "判決檢索 Monitor.app"
LOGIN_ITEM_NAME = "判決檢索 Monitor"

logger = logging.getLogger(__name__)


class ServerMonitor(rumps.App):
    def __init__(self) -> None:
        # quit_button=None：防止誤按 Cmd+Q；要退出從 menu「Quit Monitor」
        super().__init__("⚪", quit_button=None)

        self.status_item = rumps.MenuItem("狀態偵測中…")
        self.progress_item = rumps.MenuItem("—")
        self.progress_item.set_callback(None)  # disable click（只顯示資訊）
        # Restart / Start label 依狀態動態切換、callback 不變
        self.action_item = rumps.MenuItem("⟳ 重啟伺服器", callback=self._on_restart)
        self.login_item = rumps.MenuItem(
            "開機自動啟動", callback=self._on_toggle_login
        )
        self._refresh_login_state()

        self.menu = [
            self.status_item,
            self.progress_item,
            None,  # separator
            self.action_item,
            rumps.MenuItem("🌐 開啟瀏覽器", callback=self._on_open_web),
            rumps.MenuItem("📋 查看 Log", callback=self._on_view_logs),
            None,
            self.login_item,
            None,
            rumps.MenuItem("結束 Monitor", callback=rumps.quit_application),
        ]
        self._last_alive: bool | None = None

    def _probe(self) -> bool:
        """HTTP 探測、回 True/False、不丟 exception"""
        try:
            r = requests.get(HEALTH_URL, timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def _fetch_active_progress(self) -> str | None:
        """查 /api/tasks 找正在跑的 analysis、回「N/M 筆 · 命中 K」字串；沒有回 None"""
        try:
            r = requests.get(TASKS_URL, timeout=3)
            if r.status_code != 200:
                return None
            tasks = r.json()
        except Exception:
            return None

        # 找 status running 或 pending 的 analysis（不含 partial —— 律師還沒點續跑）
        for t in tasks:
            for a in t.get("analyses") or []:
                if a.get("status") in ("running", "pending"):
                    total = a.get("total") or 0
                    completed = a.get("completed") or 0
                    match = a.get("match_count") or 0
                    # two-pass 計數（total >= 40 代表走兩輪 scoring、顯示要除 2）
                    if total >= 40:
                        total_disp = total // 2
                        completed_disp = completed // 2
                    else:
                        total_disp, completed_disp = total, completed
                    if total_disp == 0:
                        return "準備中…"
                    return f"{completed_disp}/{total_disp} 筆 · 命中 {match}"
        return None  # 沒有活躍任務

    @rumps.timer(POLL_INTERVAL_SEC)
    def _on_tick(self, _sender) -> None:  # noqa: ANN001  rumps API 簽名
        alive = self._probe()
        progress = self._fetch_active_progress() if alive else None

        # ── 選單列標題：綠燈 + progress pct（有進度時）/ 紅燈（DOWN）
        if alive:
            if progress and "/" in progress:
                # e.g. "580/807 筆 · 命中 371" → 72%
                try:
                    frac = progress.split(" ")[0]  # "580/807"
                    done, tot = frac.split("/")
                    pct = int(int(done) / int(tot) * 100) if int(tot) > 0 else 0
                    self.title = f"🟢 {pct}%"
                except Exception:
                    self.title = "🟢"
            else:
                self.title = "🟢"
        else:
            self.title = "🔴"

        # ── 選單第一列：狀態 ─────────────
        if alive:
            self.status_item.title = "狀態：UP（port 8765）"
            self.action_item.title = "⟳ 重啟伺服器"
        else:
            self.status_item.title = "狀態：DOWN"
            self.action_item.title = "▶ 啟動伺服器"

        # ── 選單第二列：進度 ─────────────
        if progress:
            self.progress_item.title = f"進行中：{progress}"
        elif alive:
            self.progress_item.title = "無進行中任務"
        else:
            self.progress_item.title = "—"

        # 狀態 transition 才發 notification（避免每 5 秒狂發）
        if self._last_alive is not None and self._last_alive != alive:
            if alive:
                rumps.notification(
                    title="判決檢索 server",
                    subtitle="已上線",
                    message="port 8765 正常回應",
                )
            else:
                rumps.notification(
                    title="判決檢索 server",
                    subtitle="離線",
                    message="建議點「啟動伺服器」恢復",
                )
        self._last_alive = alive

    # ── Actions ─────────────────────────────────────

    def _on_restart(self, _sender) -> None:
        # Server 在跑 → 重啟流程要 confirm（避免誤按砍掉活動中的 session）
        # Server 沒跑 → 啟動流程不用 confirm（沒東西可砍、反正是恢復服務）
        is_alive = self._last_alive is True
        if is_alive:
            confirm = rumps.alert(
                title="重啟判決檢索 server",
                message=(
                    "將 kill 現有 uvicorn process、重啟一個新的。\n\n"
                    "· 已寫入 DB 的任務 / 分析結果：完全保留\n"
                    "· status=running/pending 的任務：backend 會自動 resume\n"
                    "· status=partial 的分析（如你剛中止的）：保留、等你在 UI 按續跑\n\n"
                    "繼續？"
                ),
                ok="重啟",
                cancel="取消",
            )
            if not confirm:
                return

        # 1. 砍舊
        subprocess.run(
            ["pkill", "-f", UVICORN_PATTERN],
            check=False,  # 沒舊的就算了
        )
        time.sleep(2)

        # 2. 起新 — 背景 detach、log 導向檔案
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(LOG_PATH, "a", buffering=1)
        log_fp.write(f"\n─── Monitor restart at {time.ctime()} ───\n")
        try:
            subprocess.Popen(
                [
                    str(VENV_PYTHON), "-m", "uvicorn",
                    "src.main:app",
                    "--host", "127.0.0.1",
                    "--port", "8765",
                    "--workers", "1",
                ],
                cwd=str(PROJECT_DIR),
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 不跟本 process 綁在一起（close monitor 不砍 server）
            )
        except FileNotFoundError as exc:
            rumps.alert(
                title="啟動失敗",
                message=(
                    f"找不到 python：{exc}\n\n"
                    f"預期路徑：{VENV_PYTHON}\n"
                    "請確認 venv 存在、或改 server_monitor.py 裡的 VENV_PYTHON 常數"
                ),
            )
            return

        # 3. 等到 healthy OR timeout
        rumps.notification(
            title="判決檢索 server",
            subtitle="重啟中…",
            message=f"等最多 {STARTUP_WAIT_SEC} 秒檢查健康狀態",
        )
        for _ in range(STARTUP_WAIT_SEC):
            time.sleep(1)
            if self._probe():
                rumps.notification(
                    title="判決檢索 server",
                    subtitle="重啟完成",
                    message="可以回瀏覽器繼續使用",
                )
                self._last_alive = True
                return
        # 沒起來
        rumps.alert(
            title="重啟後沒起來",
            message=(
                f"等了 {STARTUP_WAIT_SEC} 秒仍無回應，請查看 log：\n{LOG_PATH}"
            ),
        )

    def _on_open_web(self, _sender) -> None:
        subprocess.run(["open", HEALTH_URL], check=False)

    def _on_view_logs(self, _sender) -> None:
        if LOG_PATH.exists():
            subprocess.run(["open", str(LOG_PATH)], check=False)
        else:
            rumps.alert(title="Log 檔不存在", message=str(LOG_PATH))

    # ── Login Items（開機自動啟動）────────────────

    def _refresh_login_state(self) -> None:
        """查 macOS Login Items 看本 app 有沒有註冊、更新 menu item 勾選狀態"""
        registered = self._login_item_exists()
        self.login_item.state = 1 if registered else 0

    def _login_item_exists(self) -> bool:
        """osascript 查 Login Items 清單"""
        try:
            r = subprocess.run(
                [
                    "osascript", "-e",
                    'tell application "System Events" to get the name of every login item',
                ],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            return LOGIN_ITEM_NAME in r.stdout
        except Exception:
            return False

    def _on_toggle_login(self, sender) -> None:
        currently_on = self._login_item_exists()
        if currently_on:
            # 移除
            script = (
                f'tell application "System Events" '
                f'to delete login item "{LOGIN_ITEM_NAME}"'
            )
        else:
            # 註冊：需要 .app bundle 存在
            if not APP_BUNDLE_PATH.exists():
                rumps.alert(
                    title="找不到 App bundle",
                    message=(
                        f"預期路徑：{APP_BUNDLE_PATH}\n\n"
                        "請先執行 tools/build_app.sh 建置 .app 後再試"
                    ),
                )
                return
            script = (
                f'tell application "System Events" '
                f'to make login item at end with properties '
                f'{{path:"{APP_BUNDLE_PATH}", hidden:false, name:"{LOGIN_ITEM_NAME}"}}'
            )

        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                rumps.alert(
                    title="設定失敗",
                    message=(
                        "osascript 回錯：\n" + (r.stderr or r.stdout or "unknown") +
                        "\n\n首次執行會要求「系統事件 / System Events」權限、請到\n"
                        "System Settings → Privacy & Security → Automation 允許"
                    ),
                )
                return
        except subprocess.TimeoutExpired:
            rumps.alert(title="設定逾時", message="osascript 無回應")
            return

        # 刷新 state
        self._refresh_login_state()
        status_msg = "已設定開機自動啟動" if not currently_on else "已取消開機自動啟動"
        rumps.notification(title="判決檢索 Monitor", subtitle="Login Items", message=status_msg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ServerMonitor().run()
