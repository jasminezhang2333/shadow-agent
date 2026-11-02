#!/bin/bash
# Shadow Agent 启动脚本 (Bash/Git Bash)

echo "╔══════════════════════════════════════════════════════════╗"
echo "║         Shadow Agent · 一键启动                          ║"
echo "╚══════════════════════════════════════════════════════════╝"

# 检查环境变量
if [ -z "$DASHSCOPE_API_KEY" ]; then
    echo "❌ 缺少 DASHSCOPE_API_KEY"
    echo "设置方法: export DASHSCOPE_API_KEY=sk-xxx"
    exit 1
fi

echo "✅ 环境检查通过"
echo ""
echo "🚀 启动服务..."

# 启动 realtime 网关（后台）
python -m backend.realtime &
GATEWAY_PID=$!
echo "  ▶️  realtime 网关 (PID: $GATEWAY_PID)"

# 等待网关启动
sleep 3

# 启动 shadow-agent 后端
python -m uvicorn backend.server:app --host 0.0.0.0 --port 8000 &
AGENT_PID=$!
echo "  ▶️  shadow-agent 后端 (PID: $AGENT_PID)"

echo ""
echo "✅ 服务已启动"
echo ""
echo "按 Ctrl+C 停止"

# 捕获中断信号
trap "echo ''; echo '⏹️  停止服务...'; kill $GATEWAY_PID $AGENT_PID 2>/dev/null; exit 0" INT TERM

# 等待
wait
