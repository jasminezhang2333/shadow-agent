# Shadow Agent · 幕后分身手机 Agent

在你正常用手机的同时，agent 在一块**你看不见的虚拟屏**上操作真实 App（淘宝/京东/微信…）替你办事。你的物理屏、焦点、键盘全程不受影响。它还能基于你前台看到的画面生成**记忆**，在你说「回忆/刚才看到」时检索，并对画面里的商品**比价**。

## 目录

- [1. 演示视频](#1-演示视频)
- [2. 架构设计](#2-架构设计)
- [3. 记忆与检索](#3-记忆与检索)
- [4. 快速开始](#4-快速开始)
- [5. 项目目录](#5-项目目录)

## 1. 演示视频

📹 **[点击观看 / 下载完整演示视频](https://github.com/jasminezhang2333/shadow-agent/releases/download/v1.0/demo.mp4)**

📱 **[下载 Android APK (v1.0)](https://github.com/jasminezhang2333/shadow-agent/releases/download/v1.0/ShadowAgent-v1.0.apk)**

> 演示视频展示了 Shadow Agent 的核心功能：虚拟屏分身操作、记忆检索和商品比价

---


## 2. 架构设计

### 2.1 三层结构

```
┌───────────────────────── Android 手机 ─────────────────────────┐
│  物理屏 display 0 (用户自己的)                                   │
│   ├─ 用户正常刷信息流 / 打字                                      │
│   ├─ CaptureService: MediaProjection 截前台屏 → 记忆流(被动)      │
│   ├─ AudioService:   麦克风 PCM → 语音流                          │
│   └─ Overlay: 一个不抢焦点的状态小窗 (FLAG_NOT_FOCUSABLE)         │
│                                                                 │
│  虚拟屏 display N (agent 的, 不可见)                             │
│   └─ 淘宝/京东 Activity ← agent 在这里点击/输入/比价              │
└────────────────┬──────────────────────────┬───────────────────┘
                 │ WebSocket(帧+音频+任务)    │ ADB (input/am --display N)
                 ▼                            ▲
┌──────────────────────── PC 后端 ────────────┴───────────────────┐
│  server.py (FastAPI)  ── 收 Android 的记忆流/语音/任务            │
│      │                                                           │
│      ├─ gateway_client ──► realtime-MLLM (:8080 HTTP / :8081 WS) │
│      │        推 用户语音+前台帧, 收 tool_call(回忆/比价)          │
│      │                                                           │
│      ├─ agent/ (langchain)  任务→看虚拟屏→决策→phone_act 循环      │
│      │                                                           │
│      └─ tools/                                                   │
│           ├─ memory_retrieval ─► memory/ (Chroma RAG)            │
│           ├─ comparison_prices ─► 电商比价                        │
│           └─ phone_act ─► bridge/ (ADB 虚拟屏操作)                │
└─────────────────────────────────────────────────────────────────┘
```

**数据流**：Android 把你的**前台画面+语音**推给后端 → 后端转推 realtime-MLLM → MLLM 理解画面(存记忆)、在语音里捕捉「回忆/比价」意图发 `tool_call` → 后端执行工具；另一条线，你下达自然语言任务 → agent 在**虚拟屏**上一步步操作并验证。

### 2.2 两条独立的截屏流（关键区分）

很多人会混淆，本项目刻意分开：

1. **记忆流（被动，物理屏）**：用户在前台看什么，MediaProjection 就截什么，低频（~1fps）送后端 → MLLM 生成帧描述 → 向量化入库。这是「帮用户理解视频帧、生成记忆」的来源。agent 不碰这块屏。
2. **感知流（主动，虚拟屏）**：agent 自己操作淘宝时，需要「看见」虚拟屏的当前状态来决定下一步。这路帧来自 `screencap -d N`（或 scrcpy 虚拟屏镜像），只喂给 agent 的决策循环，不入记忆库。

## 3. 记忆与检索

基于 langchain + RAG + Chroma 向量库实现：

- **ingest**：前台帧 → MLLM 生成结构化描述（画面里有什么商品/场景/文字）→ embedding → 存 Chroma，metadata 带 timestamp / app / 商品实体
- **retrieval**：当 MLLM 在用户语音里捕捉到「回忆/记得/刚才看到」等关键词（realtime-MLLM 已有 keyword router + memory_retrieval 工具描述），gateway 收到 `tool_call{name:"memory_retrieval"}`，路由到 `tools/memory_retrieval`，走 langchain RetrievalQA 返回「你刚才在淘宝看过的那双鞋」
- **商品比价记忆打通**：记忆里存了商品实体，`comparison_prices` 可直接拿实体去比价，不必用户重述

## 4. 快速开始

### 4.1 realtime-MLLM 网关（已整合）
```bash
cd D:/shadow-agent
python -m backend.realtime  # HTTP:8080 + WS:8081
```

### 4.2 后端（Python 3.10/3.11）
```bash
cd D:/shadow-agent
pip install -r requirements.txt

# 环境变量
set DASHSCOPE_API_KEY=sk-...        # 记忆描述/检索/决策 LLM & embedding
set ADB_SERIAL=<你的设备序列号>      # adb devices 里那个

# 启动 shadow-agent 后端
python -m uvicorn backend.server:app --host 0.0.0.0 --port 8000
```

### 4.3 Android 瘦客户端
用 Android Studio 打开 `android/`，连真机编译安装。打开 App → 填后端地址（如 `ws://<PC内网IP>:8000/client/stream`）→「启动分身」→ 授权录屏/悬浮窗/麦克风。之后正常用手机即可。

> 模拟器连本机后端用 `ws://10.0.2.2:8000/...`；真机用 PC 的局域网 IP。

### 4.4 下任务
```bash
curl -X POST http://localhost:8000/task -H "Content-Type: application/json" \
  -d '{"goal":"在淘宝搜索 ANKER 充电宝，挑销量最高的加入购物车","package":"com.taobao.taobao"}'
curl http://localhost:8000/task/<task_id>   # 查进度/交接边界
```


## 5. 项目目录
```
shadow-agent/
├── backend/
│   ├── bridge/             ADB 虚拟屏管理 + 输入注入 + 截帧
│   ├── memory/             Chroma 向量库 + ingest + RAG 检索
│   ├── tools/              比价 / phone_act / langchain 工具封装
│   ├── gateway/            对接 realtime-MLLM 网关
│   ├── agent/              任务编排闭环（看屏→决策→动作→验证→交接）
│   ├── server.py           FastAPI 入口
│   └── tests/test_pure.py  纯逻辑测试（无需真机/网络）
└── android/                Kotlin 瘦客户端（截屏+麦克风+状态小窗+上传）
```

