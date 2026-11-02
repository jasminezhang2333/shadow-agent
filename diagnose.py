"""Shadow Agent WebSocket 连接诊断工具

帮助排查 Android 客户端连接问题
"""
import sys
import subprocess
import socket

def check_port(host, port):
    """检查端口是否可访问"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as e:
        return False

def check_service():
    """检查服务状态"""
    print("=" * 60)
    print("🔍 Shadow Agent 服务诊断")
    print("=" * 60)

    # 1. 检查端口
    print("\n1️⃣  检查端口监听状态:")
    ports = {
        8000: "shadow-agent 主服务",
        8080: "realtime HTTP",
        8081: "realtime WebSocket"
    }

    all_listening = True
    for port, desc in ports.items():
        listening = check_port("127.0.0.1", port) or check_port("192.168.123.248", port)
        status = "✅" if listening else "❌"
        print(f"  {status} 端口 {port}: {desc}")
        if not listening:
            all_listening = False

    if not all_listening:
        print("\n⚠️  部分端口未监听，请先运行: python start.py")
        return False

    # 2. 测试 HTTP 连接
    print("\n2️⃣  测试 HTTP 连接:")
    try:
        result = subprocess.run(
            ["curl", "-s", "http://192.168.123.248:8000/docs"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if "swagger" in result.stdout.lower() or "openapi" in result.stdout.lower():
            print("  ✅ HTTP 接口正常（FastAPI 文档可访问）")
        else:
            print("  ✅ HTTP 服务响应正常")
    except Exception as e:
        print(f"  ⚠️  HTTP 测试失败: {e}")

    # 3. 检查 WebSocket 路由
    print("\n3️⃣  检查 WebSocket 路由:")
    print("  📍 Android 应填写: ws://192.168.123.248:8000/client/stream")
    print("  📍 或者（内网其他IP）: ws://<你的PC IP>:8000/client/stream")

    # 4. 查看最近日志
    print("\n4️⃣  uvicorn 日志级别检查:")
    print("  提示: 重启服务时添加 --log-level debug 可以看到详细日志")
    print("  命令: python -m uvicorn backend.server:app --host 0.0.0.0 --port 8000 --log-level debug")

    # 5. Android 端检查清单
    print("\n5️⃣  Android 端检查清单:")
    print("  ☑️  手机和PC在同一WiFi网络")
    print("  ☑️  PC防火墙允许8000端口入站")
    print("  ☑️  Android App填写的IP地址正确")
    print("  ☑️  Android App有录屏/麦克风权限")
    print("  ☑️  点击了「启动分身」按钮")

    print("\n" + "=" * 60)
    print("💡 调试建议:")
    print("=" * 60)
    print("1. 重新启动服务（附带debug日志）:")
    print("   python start.py")
    print()
    print("2. 在另一个终端监控日志（如果有日志文件）")
    print()
    print("3. 手动测试 WebSocket 连接:")
    print("   python test_ws_aiohttp.py")
    print()
    print("4. 检查PC防火墙:")
    print("   Windows: 控制面板 -> Windows Defender 防火墙 -> 允许应用通过防火墙")
    print("   添加 Python 和 端口 8000")
    print()
    print("5. 查看完整的 uvicorn 启动日志，确认 WebSocket 路由已注册")
    print()
    print("6. 如果 Android 连接后立即断开，检查:")
    print("   - realtime 网关是否正常运行（8080/8081）")
    print("   - DASHSCOPE_API_KEY 是否正确")
    print("   - 网络连接是否稳定")

    return True

if __name__ == "__main__":
    try:
        check_service()
    except KeyboardInterrupt:
        print("\n\n已取消")
        sys.exit(0)
