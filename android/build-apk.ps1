# One-click APK build (Windows PowerShell). ASCII-only to avoid encoding issues.
# Prereqs: JDK 17, Android SDK command-line tools, Gradle 8.7 on PATH.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1. check local.properties
if (-not (Test-Path "local.properties")) {
    Write-Host "Missing local.properties. Copy local.properties.template and set sdk.dir." -ForegroundColor Red
    exit 1
}

# locate gradle: prefer PATH, fall back to D:\Gradle\gradle-8.7
$gradleCmd = "gradle"
if (-not (Get-Command gradle -ErrorAction SilentlyContinue)) {
    $fallback = "D:\Gradle\gradle-8.7\bin\gradle.bat"
    if (Test-Path $fallback) {
        $gradleCmd = $fallback
    } else {
        Write-Host "gradle not found on PATH nor at $fallback" -ForegroundColor Red
        exit 1
    }
}
Write-Host ("Using gradle: " + $gradleCmd) -ForegroundColor Cyan

# 2. generate gradle wrapper once
if (-not (Test-Path "gradle/wrapper/gradle-wrapper.jar")) {
    Write-Host "No gradle-wrapper.jar found. Generating wrapper..." -ForegroundColor Yellow
    & $gradleCmd wrapper --gradle-version 8.7
}

# 3. build debug APK
if (Test-Path "gradlew.bat") {
    .\gradlew.bat assembleDebug --no-daemon
} else {
    & $gradleCmd assembleDebug --no-daemon
}

$apk = "app/build/outputs/apk/debug/app-debug.apk"
if (Test-Path $apk) {
    Write-Host ""
    Write-Host ("APK built: " + (Resolve-Path $apk).Path) -ForegroundColor Green
    Write-Host ("Install to device: adb install -r " + $apk)
} else {
    Write-Host "Build finished but APK not found. Check the log above." -ForegroundColor Red
}
