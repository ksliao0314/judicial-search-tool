#!/bin/bash
# 雙擊此檔即可啟動 server monitor（macOS 選單列顯示綠/紅燈）
# 退出：從選單列 icon 點「結束 Monitor」
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python tools/server_monitor.py
