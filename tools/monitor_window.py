"""判決檢索 server Monitor — 司法文件室小窗。

設計語言：印章壓紙的精緻極簡。
  · 色系：米紙 #F7F7F5 底 + 墨黑 #111110 文 + 暖銅 #6D5A41 印
  · 版型：左側印章燈（外銅環 + 中心墨點）+ 右側狀態文 + 細分隔線 + 書簽按鈕
  · 字型：宋體 Songti TC（狀態 / 按鈕）+ 蘋方 PingFang TC（進度數字）
  · 無 emoji / 無邊框按鈕 / 無漸變 / 無陰影 / 無交通燈警示色

設計原則：
  · 狀態燈不搶注意力 — 不閃、不脈衝、不變色飽和度
  · 按鈕是文字本身 — hover 時才顯示暖銅下底線（書簽隱喻）
  · 最少層級 — 一行狀態、一行進度、一條線、一排標籤

行為原則（與 server_monitor.py 一致）：
  · 不動 DB / log / settings、任務安全
  · 重啟走 pkill + Popen、既有 task 由 backend recovery 接回
"""
from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox

import requests  # type: ignore[import-not-found]

# ── 配置（跟 server_monitor.py 對齊）────────────────
PROJECT_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
LOG_PATH = PROJECT_DIR / "logs" / "server.log"
HEALTH_URL = "http://127.0.0.1:8765/"
TASKS_URL = "http://127.0.0.1:8765/api/tasks"
UVICORN_PATTERN = "uvicorn src.main:app"
POLL_INTERVAL_SEC = 5
STARTUP_WAIT_SEC = 12

logger = logging.getLogger(__name__)

# ── Design tokens ─────────────────────────────────
# 米紙 + 墨黑 + 暖銅 三色調、加兩個中性過渡色、加一個暗紅警示
PARCHMENT = "#F7F7F5"   # bg
INK       = "#111110"   # 主文
SEAL      = "#6D5A41"   # 暖銅 accent
MUTED     = "#8A847C"   # 次要文字（暖灰）
LINE      = "#E0DDD7"   # 細分隔
GHOST     = "#D8D1C7"   # 狀態未知（暖銅極淡）
ERROR_INK = "#8B2E1F"   # 暗紅、非亮紅（文人感）

# 視窗尺寸
W, H = 288, 136
PAD = 16
SEAL_OUTER = 52  # 印章外徑
SEAL_INNER = 14  # 內點直徑


def _font(family_candidates: list[str], size: int, weight: str = "normal") -> tuple:
    """試多個 font family、回第一個 macOS 有裝的；都沒有 fallback 到 Tk default"""
    try:
        available = set(tkfont.families())
    except tk.TclError:
        available = set()
    for fam in family_candidates:
        if fam in available:
            return (fam, size, weight) if weight != "normal" else (fam, size)
    return ("TkDefaultFont", size, weight) if weight != "normal" else ("TkDefaultFont", size)


def _notify(title: str, subtitle: str, message: str) -> None:
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{message}" '
                f'with title "{title}" subtitle "{subtitle}"',
            ],
            check=False, timeout=2,
        )
    except Exception:
        pass


# ── 印章狀狀態燈 ──────────────────────────────────
class SealLight(tk.Canvas):
    """暖銅細環 + 中心墨點 — 像蓋過的印章、不閃不跳。"""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent, width=SEAL_OUTER, height=SEAL_OUTER,
            bg=PARCHMENT, highlightthickness=0, bd=0,
        )
        cx = cy = SEAL_OUTER // 2
        r_out = (SEAL_OUTER - 4) // 2
        r_in = SEAL_INNER // 2

        # 外銅環（細、內斂）
        self.outer = self.create_oval(
            cx - r_out, cy - r_out, cx + r_out, cy + r_out,
            outline=SEAL, width=1.4,
        )
        # 中心墨點
        self.inner = self.create_oval(
            cx - r_in, cy - r_in, cx + r_in, cy + r_in,
            outline="", fill=SEAL,
        )

    def set_state(self, state: str) -> None:
        """state ∈ {'up', 'down', 'unknown'}"""
        if state == "up":
            self.itemconfig(self.outer, outline=SEAL, width=1.4)
            self.itemconfig(self.inner, fill=SEAL)
        elif state == "down":
            self.itemconfig(self.outer, outline=ERROR_INK, width=1.4)
            self.itemconfig(self.inner, fill=ERROR_INK)
        else:  # unknown — 極淡暖銅、像印章未蓋
            self.itemconfig(self.outer, outline=GHOST, width=1.4)
            self.itemconfig(self.inner, fill=GHOST)


# ── 無框按鈕（書簽標籤風）──────────────────────────
class LinkButton(tk.Label):
    """hover 時暖銅下底線 + 文字變色、點擊 flash 暖銅。"""

    def __init__(
        self, parent: tk.Misc, text: str, command, disabled_text: str | None = None
    ) -> None:
        self._font_normal = _font(["Songti TC", "PingFang TC"], 12)
        self._font_hover = _font(["Songti TC", "PingFang TC"], 12) + ("underline",)
        super().__init__(
            parent, text=text,
            font=self._font_normal,
            bg=PARCHMENT, fg=INK,
            cursor="hand2", padx=0, pady=4,
        )
        self._command = command
        self._disabled = False
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def set_label(self, text: str) -> None:
        self.config(text=text)

    def set_disabled(self, disabled: bool, text: str | None = None) -> None:
        self._disabled = disabled
        self.config(
            fg=MUTED if disabled else INK,
            cursor="arrow" if disabled else "hand2",
        )
        if text is not None:
            self.config(text=text)

    def _on_enter(self, _e) -> None:
        if self._disabled:
            return
        self.config(font=self._font_hover, fg=SEAL)

    def _on_leave(self, _e) -> None:
        if self._disabled:
            return
        self.config(font=self._font_normal, fg=INK)

    def _on_click(self, _e) -> None:
        if self._disabled or not self._command:
            return
        # 點擊 micro-flash：暖銅 120ms 後回墨黑
        self.config(fg=SEAL)
        self.after(120, lambda: self.config(
            fg=SEAL if self._disabled_or_hover() else INK
        ))
        self._command()

    def _disabled_or_hover(self) -> bool:
        return self._disabled


# ── 主視窗 ──────────────────────────────────────
class MonitorWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("判 · Monitor")
        self.root.geometry(f"{W}x{H}+100+100")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.configure(bg=PARCHMENT)

        # 1. 印章燈（左）
        self.seal = SealLight(self.root)
        self.seal.place(x=PAD, y=PAD + 4)

        # 2. 狀態文字（右上大字）
        text_x = PAD + SEAL_OUTER + 16
        text_w = W - text_x - PAD
        self.status_label = tk.Label(
            self.root, text="檢查中",
            font=_font(["Songti TC", "PingFang TC"], 15, "bold"),
            bg=PARCHMENT, fg=INK, anchor="w",
        )
        self.status_label.place(x=text_x, y=PAD + 2, width=text_w, height=24)

        # 3. 進度 / 副資訊（右下小字）
        self.meta_label = tk.Label(
            self.root, text="—",
            font=_font(["PingFang TC", "Songti TC"], 10),
            bg=PARCHMENT, fg=MUTED, anchor="w",
        )
        self.meta_label.place(x=text_x, y=PAD + 28, width=text_w, height=20)

        # 4. 印章字號（右下最小、只在 UP 時出）
        self.port_label = tk.Label(
            self.root, text="",
            font=_font(["JetBrains Mono", "Menlo", "Monaco"], 8),
            bg=PARCHMENT, fg=MUTED, anchor="w",
        )
        self.port_label.place(x=text_x, y=PAD + 48, width=text_w, height=14)

        # 5. 細分隔線
        sep = tk.Frame(self.root, bg=LINE, height=1)
        sep.place(x=PAD, y=H - 36, width=W - 2 * PAD)

        # 6. 書簽按鈕列（置中分三欄）
        # Label 用「動詞+名詞」完整表達、避免「啟」「開」等單字產生歧義
        btn_y = H - 28
        col_w = (W - 2 * PAD) // 3
        self.action_btn = LinkButton(self.root, "啟動伺服器", self._on_action)
        self.action_btn.place(x=PAD, y=btn_y, width=col_w, height=20)
        self.action_btn.config(anchor="center")

        self.open_btn = LinkButton(self.root, "開啟網頁", self._on_open)
        self.open_btn.place(x=PAD + col_w, y=btn_y, width=col_w, height=20)
        self.open_btn.config(anchor="center")

        self.log_btn = LinkButton(self.root, "紀錄檔", self._on_log)
        self.log_btn.place(x=PAD + 2 * col_w, y=btn_y, width=col_w, height=20)
        self.log_btn.config(anchor="center")

        # 7. 按鈕間的細豎分隔（次要裝飾、凸顯 3 個獨立動作）
        for i in range(1, 3):
            vline = tk.Frame(self.root, bg=LINE, width=1)
            vline.place(x=PAD + i * col_w, y=btn_y + 2, height=16)

        # ── Polling 背景 thread + UI queue ───────────
        self._ui_queue: queue.Queue = queue.Queue()
        self._last_alive: bool | None = None
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(80, self._drain_queue)

    # ── Probing ────────────────────────────────

    def _probe(self) -> bool:
        try:
            r = requests.get(HEALTH_URL, timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def _fetch_active_progress(self) -> str | None:
        try:
            r = requests.get(TASKS_URL, timeout=3)
            if r.status_code != 200:
                return None
            tasks = r.json()
        except Exception:
            return None
        for t in tasks:
            for a in t.get("analyses") or []:
                if a.get("status") in ("running", "pending"):
                    total = a.get("total") or 0
                    completed = a.get("completed") or 0
                    match = a.get("match_count") or 0
                    if total >= 40:
                        total, completed = total // 2, completed // 2
                    if total == 0:
                        return "準備中"
                    return f"{completed}/{total}   命中 {match}"
        return None

    def _poll_loop(self) -> None:
        while True:
            alive = self._probe()
            progress = self._fetch_active_progress() if alive else None
            self._ui_queue.put(("status", alive, progress))
            time.sleep(POLL_INTERVAL_SEC)

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            tag = item[0]
            if tag == "status":
                _, alive, progress = item
                self._apply_status(alive, progress)
            elif tag == "restart_done":
                _, ok = item
                self.action_btn.set_disabled(False)
                # 下一 tick 會 refresh label
            elif tag == "alert":
                _, title, msg = item
                self.action_btn.set_disabled(False)
                messagebox.showerror(title, msg)
        self.root.after(80, self._drain_queue)

    # ── UI 更新 ────────────────────────────────

    def _apply_status(self, alive: bool, progress: str | None) -> None:
        if alive:
            self.seal.set_state("up")
            self.status_label.config(text="伺服器  正常", fg=INK)
            self.meta_label.config(text=progress if progress else "無進行中任務")
            self.port_label.config(text="port 8765")
            self.action_btn.set_label("重啟伺服器")
        else:
            self.seal.set_state("down")
            self.status_label.config(text="伺服器  離線", fg=ERROR_INK)
            self.meta_label.config(text="按「啟動伺服器」恢復")
            self.port_label.config(text="")
            self.action_btn.set_label("啟動伺服器")

        # 狀態 transition 發 notification
        if self._last_alive is not None and self._last_alive != alive:
            if alive:
                _notify("判 Monitor", "伺服器上線", "port 8765 正常回應")
            else:
                _notify("判 Monitor", "伺服器離線", "按視窗「啟動」恢復")
        self._last_alive = alive

    # ── Actions ────────────────────────────────

    def _on_action(self) -> None:
        is_alive = self._last_alive is True
        if is_alive:
            if not messagebox.askyesno(
                "重啟伺服器",
                "將 kill 現有 uvicorn 並重啟。\n\n"
                "· 已寫入 DB 的任務與分析：完全保留\n"
                "· running 任務：backend 自動 resume\n"
                "· partial 任務：保留、等你在 UI 按續跑\n\n"
                "確定繼續？",
            ):
                return
        self.action_btn.set_disabled(True, "處理中")
        threading.Thread(target=self._do_restart, daemon=True).start()

    def _do_restart(self) -> None:
        subprocess.run(["pkill", "-f", UVICORN_PATTERN], check=False)
        time.sleep(2)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_fp = open(LOG_PATH, "a", buffering=1)
            log_fp.write(f"\n─── Monitor restart at {time.ctime()} ───\n")
            subprocess.Popen(
                [
                    str(VENV_PYTHON), "-m", "uvicorn", "src.main:app",
                    "--host", "127.0.0.1", "--port", "8765", "--workers", "1",
                ],
                cwd=str(PROJECT_DIR),
                stdout=log_fp, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            self._ui_queue.put(("alert", "啟動失敗", f"找不到 python：{exc}"))
            return

        for _ in range(STARTUP_WAIT_SEC):
            time.sleep(1)
            if self._probe():
                _notify("判 Monitor", "重啟完成", "可以回瀏覽器繼續")
                self._ui_queue.put(("restart_done", True))
                return
        self._ui_queue.put((
            "alert", "重啟後沒起來",
            f"等 {STARTUP_WAIT_SEC}s 仍無回應、查看「紀錄」",
        ))

    def _on_open(self) -> None:
        subprocess.run(["open", HEALTH_URL], check=False)

    def _on_log(self) -> None:
        if LOG_PATH.exists():
            subprocess.run(["open", str(LOG_PATH)], check=False)
        else:
            messagebox.showinfo("紀錄", f"Log 檔尚未建立\n{LOG_PATH}")

    # ── Run ──────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    MonitorWindow().run()
