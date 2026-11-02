"""Shadow Agent 一键启动脚本（简化版）

同时启动：
1. realtime-MLLM 网关服务（HTTP:8080 + WS:8081）
2. shadow-agent 后端服务（HTTP:8000）

使用方法:
  python start.py

环境变量可以写在 .env 文件中，会自动加载。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

def load_env_file():
    """从 .env 文件加载环境变量"""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return

    print("📄 加载 .env 文件...")
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            # 解析 KEY=VALUE
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # 去除引号
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                # 只在环境变量未设置时才加载
                if not os.getenv(key):
                    os.environ[key] = value
                    print(f"  ✅ {key} = {value[:10]}..." if len(value) > 10 else f"  ✅ {key} = {value}")

# 首先加载 .env 文件
load_env_file()

def check_env():
    """检查必需的环境变量"""
    print("🔍 环境检查...")

    required = {
        "DASHSCOPE_API_KEY": "通义千问 API Key",
    }

    missing = []
    for key, desc in required.items():
        if not os.getenv(key):
            print(f"  ❌ {key} - {desc}")
            missing.append(key)
        else:
            masked = os.getenv(key)[:10] + "..."
            print(f"  ✅ {key} = {masked}")

    if not os.getenv("ADB_SERIAL"):
        print("  ⚠️  ADB_SERIAL 未设置，将使用默认设备")
    else:
        print(f"  ✅ ADB_SERIAL = {os.getenv('ADB_SERIAL')}")

    if missing:
        print(f"\n❌ 缺少环境变量: {', '.join(missing)}")
        print("\nWindows 设置方法:")
        print("  set DASHSCOPE_API_KEY=sk-xxx")
        print("  set ADB_SERIAL=你的设备序列号")
        return False

    return True

def main():
    print("""
╔══════════════════════════════════════════════════════════╗
║         Shadow Agent · 幕后分身手机 Agent                ║
╚══════════════════════════════════════════════════════════╝
    """)

    if not check_env():
        sys.exit(1)

    shadow_root = Path(__file__).parent

    print("\n🚀 启动服务...")
    print("  [1/2] realtime-MLLM 网关 (8080/8081)")
    print("  [2/2] shadow-agent 后端 (8000)")
    print()

    # 使用 subprocess 启动两个服务
    # 方案：用 python -m 方式避免导入问题

    gateway_cmd = [
        sys.executable, "-m", "backend.realtime.main"
    ]

    agent_cmd = [
        sys.executable, "-m", "uvicorn",
        "backend.server:app",
        "--host", "0.0.0.0",
        "--port", "8000"
    ]

    procs = []

    try:
        # 启动 gateway
        print("▶️  启动 realtime 网关...")
        gateway_proc = subprocess.Popen(
            gateway_cmd,
            cwd=str(shadow_root),
            env=os.environ.copy()
        )
        procs.append(("gateway", gateway_proc))
        time.sleep(3)  # 等待网关启动

        # 启动 agent（增加日志级别）
        print("▶️  启动 shadow-agent 后端...")
        agent_cmd_with_log = agent_cmd + ["--log-level", "info"]  # info 级别日志
        agent_proc = subprocess.Popen(
            agent_cmd_with_log,
            cwd=str(shadow_root),
            env=os.environ.copy()
        )
        procs.append(("agent", agent_proc))

        print("\n✅ 服务已启动!")
        print("\n📡 服务端点:")
        print("  - realtime 网关: http://localhost:8080, ws://localhost:8081")
        print("  - shadow 后端:   http://localhost:8000")
        print("\n📱 Android 端配置:")
        print(f"  ws://<你的PC局域网IP>:8000/client/stream")
        print("\n💡 测试命令:")
        print('  curl -X POST http://localhost:8000/task ^')
        print('    -H "Content-Type: application/json" ^')
        print('    -d "{\\"goal\\":\\"在淘宝搜充电宝\\",\\"package\\":\\"com.taobao.taobao\\"}"')
        print("\n按 Ctrl+C 停止")

        # 等待
        for name, proc in procs:
            proc.wait()

    except KeyboardInterrupt:
        print("\n\n⏹️  停止服务...")
        for name, proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("👋 已停止")

if __name__ == "__main__":
    main()
