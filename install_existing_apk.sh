#!/bin/bash
# 把已编译好的 debug APK 安装到已连接的 Android 手机。
# 配合 build_with_local_gradle.sh：先编译，再跑本脚本安装。

set -e

# 本地 platform-tools 加入 PATH（与 check_android.sh 保持一致）
export PATH=$PATH:/d/shadow-agent/platform-tools

APK_PATH="/d/shadow-agent/android/app/build/outputs/apk/debug/app-debug.apk"
PACKAGE="com.shadowagent"

echo "================================"
echo "安装 Shadow Agent APK 到手机"
echo "================================"
echo ""

# 1. 检查 adb
if ! command -v adb &> /dev/null; then
    echo "❌ adb 命令不存在"
    echo "   请把 platform-tools 放到 /d/shadow-agent/platform-tools，或装好 adb"
    exit 1
fi

# 2. 检查 APK 是否存在
if [ ! -f "$APK_PATH" ]; then
    echo "❌ 找不到 APK：$APK_PATH"
    echo "   请先编译：cd /d/shadow-agent && bash build_with_local_gradle.sh"
    exit 1
fi

APK_SIZE=$(du -h "$APK_PATH" | cut -f1)
echo "✅ 找到 APK：$APK_PATH ($APK_SIZE)"
echo ""

# 3. 检查设备
devices=$(adb devices | grep -v "List of devices" | grep -w "device" | wc -l)
if [ "$devices" -eq 0 ]; then
    echo "❌ 没有检测到 Android 设备"
    echo "   USB：手机启用「开发者选项 → USB 调试」后用数据线连接"
    echo "   无线：先 adb pair <ip:port> <配对码>，再 adb connect <ip:port>"
    exit 1
fi
echo "✅ 检测到 $devices 台设备："
adb devices | grep -w "device" | grep -v "List of devices"
echo ""

# 4. 安装（-r 覆盖安装，-d 允许降级版本号，-g 预授予运行时权限）
echo "================================"
echo "开始安装（覆盖安装）"
echo "================================"
echo ""
adb install -r -d -g "$APK_PATH"

echo ""
echo "================================"
echo "✅ 安装完成！"
echo "================================"
echo ""

# 5. 自动拉起 App（失败不致命，可能是 launcher activity 名变化）
echo "尝试启动 App…"
if adb shell monkey -p "$PACKAGE" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; then
    echo "✅ 已拉起 $PACKAGE"
else
    echo "⚠️  自动启动失败，请在手机上手动打开 Shadow"
fi
echo ""
echo "查看日志：cd /d/shadow-agent && bash check_android.sh"
echo ""
