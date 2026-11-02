import os
# Gemini Live API 走 generativelanguage.googleapis.com，国内直连不通；
# 必须在 import google-genai 之前设好代理，否则 ws opening handshake 超时。
# 不影响国内厂商的请求（dashscope / 火山 / 智谱）—— urllib3 / aiohttp 默认会
# 把 NO_PROXY 里的域名直连，下面顺手把这些域名加进白名单。
os.environ.setdefault('HTTPS_PROXY', 'http://127.0.0.1:10090')
os.environ.setdefault('HTTP_PROXY', 'http://127.0.0.1:10090')
os.environ.setdefault('NO_PROXY',
    'localhost,127.0.0.1,'
    'dashscope.aliyuncs.com,'
    'open.bigmodel.cn,'
    'rtc.volcengineapi.com,'
    'ark.cn-beijing.volces.com'
)

import asyncio
import threading
from http.server import HTTPServer

from backend.realtime.server.http import HTTPHandler
from backend.realtime.server.websocket import handle_ws
from backend.realtime.utils.logger import logger
from backend.realtime.config import Settings

def run_http():
    server = HTTPServer(('0.0.0.0', 8080), HTTPHandler)
    logger.info("🚀 HTTP: http://0.0.0.0:8080")
    server.serve_forever()

async def main():
    threading.Thread(target=run_http, daemon=True).start()
    await asyncio.sleep(0.3)
    
    logger.info("🚀 WebSocket: ws://0.0.0.0:8081")
    logger.info(f"📦 默认模型：{Settings.MODEL_NAME} (provider={Settings.get_provider(Settings.MODEL_NAME)})")
    logger.info("✅ 等待 Agent 连接...")
    
    async with websockets.serve(handle_ws, '0.0.0.0', 8081, max_size=10*1024*1024):
        await asyncio.Future()

if __name__ == '__main__':
    try:
        import websockets
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 服务停止")