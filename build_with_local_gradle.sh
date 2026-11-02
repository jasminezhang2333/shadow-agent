#!/bin/bash
# 使用手动下载的 Gradle 编译 APK

set -e

cd /d/shadow-agent/android

echo "================================"
echo "检查 Gradle zip 文件"
echo "================================"
echo ""

GRADLE_ZIP="gradle/wrapper/gradle-8.7-bin.zip"

if [ ! -f "$GRADLE_ZIP" ]; then
    echo "❌ 找不到 Gradle zip 文件"
    echo ""
    echo "请把下载的 gradle-8.7-bin.zip 放到："
    echo "  D:\\shadow-agent\\android\\gradle\\wrapper\\gradle-8.7-bin.zip"
    echo ""
    echo "下载地址："
    echo "  https://gradle.org/next-steps/?version=8.7&format=bin"
    echo "  或: https://mirrors.cloud.tencent.com/gradle/gradle-8.7-bin.zip"
    exit 1
fi

FILE_SIZE=$(du -h "$GRADLE_ZIP" | cut -f1)
echo "✅ 找到 Gradle zip: $FILE_SIZE"
echo ""

echo "================================"
echo "开始编译 APK"
echo "================================"
echo ""

./gradlew assembleDebug --no-daemon

APK_PATH="app/build/outputs/apk/debug/app-debug.apk"

if [ ! -f "$APK_PATH" ]; then
    echo ""
    echo "❌ APK 编译失败"
    exit 1
fi

APK_SIZE=$(du -h "$APK_PATH" | cut -f1)
echo ""
echo "================================"
echo "✅ APK 编译成功！"
echo "================================"
echo ""
echo "APK 大小: $APK_SIZE"
echo "位置: $APK_PATH"
echo ""
echo "下一步安装到手机："
echo "  cd /d/shadow-agent && bash install_existing_apk.sh"
echo ""
