#!/bin/bash
# 把 server_monitor.py 包成 macOS .app bundle
# - 雙擊啟動
# - LSUIElement=true → 沒 Dock icon、只出現在選單列
# - 依賴 PROJECT_DIR 裡的 venv、不包 Python runtime（輕量）
#
# 使用：./tools/build_app.sh
# 產出：~/Applications/判決檢索 Monitor.app

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="判決檢索 Monitor"
APP_DIR="$HOME/Applications/${APP_NAME}.app"

# Venv 檢查
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "❌ 找不到 venv：$PROJECT_DIR/.venv/bin/python"
    exit 1
fi

# Rumps / requests 檢查
if ! "$PROJECT_DIR/.venv/bin/python" -c "import rumps, requests" 2>/dev/null; then
    echo "❌ venv 缺 rumps / requests、請先跑："
    echo "   $PROJECT_DIR/.venv/bin/python -m pip install rumps requests"
    exit 1
fi

echo "→ PROJECT_DIR: $PROJECT_DIR"
echo "→ 目標: $APP_DIR"

# 清掉舊 bundle
rm -rf "$APP_DIR"

mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# 1. 可執行 script — 雙擊會跑這個
cat > "$APP_DIR/Contents/MacOS/run" << EOF
#!/bin/bash
cd "$PROJECT_DIR" || exit 1
# exec 換殼：不留 bash parent process
exec "$PROJECT_DIR/.venv/bin/python" tools/server_monitor.py
EOF
chmod +x "$APP_DIR/Contents/MacOS/run"

# 2. Icon（若 tools/AppIcon.icns 不存在則跑生成器產生）
if [[ ! -f "$PROJECT_DIR/tools/AppIcon.icns" ]]; then
    echo "→ 先生成 AppIcon.icns..."
    "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/tools/make_icon.py"
fi
cp "$PROJECT_DIR/tools/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"

# 2. Info.plist
cat > "$APP_DIR/Contents/Info.plist" << 'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>判決檢索 Monitor</string>
    <key>CFBundleDisplayName</key>
    <string>判決檢索 Monitor</string>
    <key>CFBundleIdentifier</key>
    <string>com.lawyer.judgment-search-monitor</string>
    <key>CFBundleExecutable</key>
    <string>run</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <!-- LSUIElement=true: 不在 Dock 顯示、不出現在 App Switcher、只活在選單列 -->
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <!-- 阻止進入 App Nap（省電模式會拖慢 polling、影響狀態即時性）-->
    <key>NSAppSleepDisabled</key>
    <true/>
</dict>
</plist>
PLIST_EOF

echo ""
echo "✅ 建置完成：$APP_DIR"
echo ""
echo "接下來："
echo "  1. Finder 裡開 ~/Applications 會看到「判決檢索 Monitor」"
echo "  2. 雙擊啟動（首次可能被 Gatekeeper 擋、右鍵→開啟 → 允許）"
echo "  3. 選單列會出現 ⚪/🟢/🔴 icon"
echo "  4. 想要登入時自動啟動："
echo "     System Settings → General → Login Items → Open at Login → + → 選這個 App"
