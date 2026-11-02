# 命令行出 APK（路线 B）

这台开发机当前**没有** Android SDK / Gradle / adb，只有 JDK 17。本文讲从零装环境到出包。

## 0. 你已有
- JDK 17（`java -version` 已确认）

## 1. 装 Android SDK command-line tools（无需装整个 Android Studio）
1. 下载 commandline-tools：https://developer.android.com/studio#command-line-tools-only
2. 解压到 `D:\Android\Sdk\cmdline-tools\latest\`（注意必须有 `latest` 这层）。
   解压出来默认是 `cmdline-tools\bin\...`，要手动把那层改名/移动成
   `cmdline-tools\latest\bin\...`，否则 sdkmanager 报 warning。
3. 用 sdkmanager 装编译所需组件（compileSdk=34、AGP 8.5 需要 build-tools 34）：
   ```powershell
   cd D:\Android\Sdk\cmdline-tools\latest\bin
   .\sdkmanager.bat --sdk_root=D:\Android\Sdk "platform-tools" "platforms;android-34" "build-tools;34.0.0"
   .\sdkmanager.bat --sdk_root=D:\Android\Sdk --licenses   # 一路 y 接受协议
   ```
   `platform-tools` 里就带 `adb`。

## 2. 装 Gradle（只为首次生成 wrapper）
下载 Gradle 8.7 binary-only 解压到 `D:\Gradle\gradle-8.7`，把 `D:\Gradle\gradle-8.7\bin`
加进 PATH：https://gradle.org/releases/
验证：`gradle -v` 应显示 8.7 + JVM 17。

> 为什么需要系统 gradle：仓库里没有 `gradle-wrapper.jar`（二进制，没法用文本写入提供）。
> 首次构建用系统 gradle 生成 wrapper，之后就能用 `./gradlew` 脱离系统 gradle。

## 3. 配 SDK 路径
已帮你建好 `android/local.properties`，内容：
```
sdk.dir=D:/Android/Sdk
```
SDK 装在别处就改这一行。

## 3.5（可选）把 Gradle 缓存也放 D 盘
Gradle 默认把下载的 AGP/Kotlin 依赖缓存到 `C:\Users\你\.gradle`，会占 C 盘。
想全放 D 盘，设一个环境变量（当前 PowerShell 会话临时生效）：
```powershell
$env:GRADLE_USER_HOME = "D:\Gradle\.gradle"
```
要永久生效就在「系统环境变量」里加 `GRADLE_USER_HOME = D:\Gradle\.gradle`。

## 4. 出包
在 `android/` 目录：
```powershell
powershell -File build-apk.ps1
```
脚本会：没有 wrapper 就用系统 gradle 生成 → `assembleDebug` → 打出
`app/build/outputs/apk/debug/app-debug.apk`。

或手动：
```powershell
gradle wrapper --gradle-version 8.7   # 仅首次
.\gradlew.bat assembleDebug --no-daemon
```

## 5. 装到真机
```powershell
C:\Android\Sdk\platform-tools\adb.exe install -r app\build\outputs\apk\debug\app-debug.apk
```

## 常见问题
- **SDK location not found**：`local.properties` 的 `sdk.dir` 没配或路径错。
- **Failed to find Build Tools / platform 34**：第 1 步的 sdkmanager 组件没装全。
- **licenses not accepted**：跑 `sdkmanager --licenses` 全部接受。
- **首次构建很慢**：gradle 要下 AGP 8.5 / Kotlin 1.9.24 等依赖，联网等它下完。
- **gradle wrapper 生成失败**：确认系统 gradle 在 PATH 且 `gradle -v` 正常。

## 这个 APK 能单独跑吗？
能装、能开 app、能采集截屏+录音并连后端，但**完整能力需要 PC 后端 + scrcpy 虚拟屏 + adb**
那一套（见根目录 README §3）。单独 APK 不包含 agent 操作淘宝的部分——那在 PC 侧。
