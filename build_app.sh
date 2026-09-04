#!/bin/bash
# 构建 macOS 原生 App：DJiPhone Kit.app
# 产物：~/Applications/DJiPhone Kit.app（自包含，双击即用；旧 DjiPhone.app 自动清理）
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="DJiPhone Kit"
APP_DIR="$HOME/Applications/$APP_NAME.app"
RES="$APP_DIR/Contents/Resources/app"

# 清理历史遗留的旧包名
rm -rf "$HOME/Applications/DjiPhone.app"

echo "构建 $APP_DIR ..."
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$RES"

# --- 复制运行所需的代码文件 ---
cp "$REPO_DIR/sms_server.py" "$REPO_DIR/voice_runtime.py" "$REPO_DIR/menubar.py" \
   "$REPO_DIR/index.html" "$REPO_DIR/mobile.html" "$REPO_DIR/manifest.webmanifest" \
   "$REPO_DIR/app.py" "$RES/"
mkdir -p "$RES/assets"
cp "$REPO_DIR/assets/icon.png" "$RES/assets/icon.png"

# --- 图标（优先仓库 assets，退回 /tmp/appicon.icns）---
if [ -f "$REPO_DIR/assets/icon.icns" ]; then
  cp "$REPO_DIR/assets/icon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
else
  cp /tmp/appicon.icns "$APP_DIR/Contents/Resources/AppIcon.icns"
fi

# --- 可执行启动器 ---
cat > "$APP_DIR/Contents/MacOS/DJiPhone Kit" << 'LAUNCH'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources/app" && pwd)"

# libusb 路径自动适配
if [ -d "/opt/homebrew/lib" ]; then
  export DYLD_LIBRARY_PATH=/opt/homebrew/lib
elif [ -d "/usr/local/lib" ]; then
  export DYLD_LIBRARY_PATH=/usr/local/lib
fi

# Python venv 自动探测
if [ -x "$HOME/.workbuddy/binaries/python/envs/default/bin/python3" ]; then
  PYTHON="$HOME/.workbuddy/binaries/python/envs/default/bin/python3"
else
  PYTHON=python3
fi

exec "$PYTHON" "$DIR/app.py"
LAUNCH
chmod +x "$APP_DIR/Contents/MacOS/DJiPhone Kit"

# --- Info.plist ---
cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>DJiPhone Kit</string>
    <key>CFBundleDisplayName</key>       <string>DJiPhone Kit</string>
    <key>CFBundleIdentifier</key>        <string>local.idoer.djiphone</string>
    <key>CFBundleExecutable</key>        <string>DJiPhone Kit</string>
    <key>CFBundleIconFile</key>          <string>AppIcon</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleVersion</key>           <string>4.0</string>
    <key>CFBundleShortVersionString</key><string>4.0</string>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
    <key>LSMinimumSystemVersion</key>    <string>10.13</string>
</dict>
</plist>
PLIST

echo "完成：$APP_DIR"
echo "启动测试：open \"$APP_DIR\""
