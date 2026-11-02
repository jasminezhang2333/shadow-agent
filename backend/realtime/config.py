# config.py
import os
import json

class Settings:
    # --- 硬关键词路由开关 ---
    # 开启后：ASR 句末文本如命中 src/core/tool_keywords.json 里的规则，
    # 直接下发对应 tool_call 并抑制本轮 LLM 输出。关闭后完全交给模型按
    # INSTRUCTIONS 里的"意图识别"段落自行判断。线上回退用。
    KEYWORD_ROUTER_ENABLED = os.getenv('KEYWORD_ROUTER_ENABLED', '1') == '1'

    # --- DashScope 配置 ---
    DASHSCOPE_API_KEY = os.getenv('DASHSCOPE_API_KEY', '')
    # 多 key 池：解决单 key 并发上限。请把备用 key 追加进来即可生效。
    # 与 DASHSCOPE_API_KEY 合并后去重；分配策略 = 当前活跃数最少的 key 优先。
    DASHSCOPE_API_KEYS = [
        # 在此添加你的 API keys
    ]
    # 每个 key 允许的最大并发会话数。当前账号实测 = 1（超 1 路就被风控）。
    DASHSCOPE_MAX_PER_KEY = int(os.getenv('DASHSCOPE_MAX_PER_KEY', '1'))
    # MODEL_NAME = 'qwen3-omni-flash-realtime'
    MODEL_NAME = 'qwen3.5-omni-flash-realtime'
    # MODEL_NAME = 'qwen3.5-omni-plus-realtime'
    # MODEL_NAME = 'gemini-3.1-pro-preview'
    # MODEL_NAME = 'gemini-3.1-flash-live-preview'
    # MODEL_NAME = 'glm-realtime'
    # MODEL_NAME = 'ep-20260525115617-j6qpb'  # 豆包 RTC（DoubaoRtcProvider + helper 推流）
    # MODEL_NAME = 'doubao-seed-2.0-lite'  # 模型名
    # MODEL_NAME = 'doubao-seed-2.0-pro'
    


    # --- DashScope 视频帧压缩配置 ---
    # DashScope ws 单帧上限 262144 字节(1009 Message Too Big)。开启后，
    # 当 base64 长度超过 DASHSCOPE_VIDEO_MAX_B64 时，自动用 Pillow 重编码 JPEG
    # (长边降到 DASHSCOPE_VIDEO_MAX_LONG_EDGE，质量 DASHSCOPE_VIDEO_JPEG_Q)；
    # 若仍超阈值则丢弃该帧。压缩在线程池里执行，不阻塞 asyncio 事件循环。
    DASHSCOPE_VIDEO_COMPRESS = os.getenv('DASHSCOPE_VIDEO_COMPRESS', '1') == '1'
    DASHSCOPE_VIDEO_MAX_B64 = int(os.getenv('DASHSCOPE_VIDEO_MAX_B64', '220000'))
    DASHSCOPE_VIDEO_JPEG_Q = int(os.getenv('DASHSCOPE_VIDEO_JPEG_Q', '75'))
    DASHSCOPE_VIDEO_MAX_LONG_EDGE = int(os.getenv('DASHSCOPE_VIDEO_MAX_LONG_EDGE', '640'))
    

    # --- Gemini 配置 ---
    # 注意：Live API（gemini-*-live-*）需要 Google AI Studio 原生 key（AIza... 开头），
    # 中转网关大多不代理 ws，会连不上。
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
    # 切到 Live 模型走 ws：
    #   GEMINI_MODEL_NAME = 'gemini-3.1-flash-live-preview'
    # 非 Live（聊天补全 / 批模式）保持原默认：
    #   GEMINI_MODEL_NAME = 'gemini-3.1-pro-preview'
    GEMINI_MODEL_NAME = os.getenv('GEMINI_MODEL', 'gemini-3.1-flash-live-preview')
    GEMINI_VOICE = os.getenv('GEMINI_VOICE', 'Aoede')
    # 非 Live 模型的服务端静音切句：连续 N 秒没收到音频且 buffer ≥ M 字节就自动 commit
    # 16kHz / 16bit / mono = 32000 B/s，默认 16000 B ≈ 0.5 秒有效语音
    GEMINI_SILENCE_SEC = float(os.getenv('GEMINI_SILENCE_SEC', '0.8'))
    GEMINI_MIN_AUDIO_BYTES = int(os.getenv('GEMINI_MIN_AUDIO_BYTES', '16000'))
    # RMS 阈值：16-bit PCM 帧的能量低于此值视为静音帧（不更新"最后有声时间"）。
    # 安静环境麦克风底噪通常 < 200，正常说话 > 1000；嘈杂环境可调高到 800~1200。
    GEMINI_RMS_THRESHOLD = int(os.getenv('GEMINI_RMS_THRESHOLD', '800'))

    # --- GLM-Realtime 配置 ---
    # 申请 / 查 key: https://bigmodel.cn/usercenter/apikeys；格式 {id}.{secret}
    ZHIPU_API_KEY = os.getenv('ZHIPU_API_KEY', '')
    GLM_MODEL_NAME = os.getenv('GLM_MODEL', 'glm-realtime')  # glm-realtime-air / glm-realtime-flash
    GLM_REALTIME_URL = os.getenv('GLM_REALTIME_URL', 'wss://open.bigmodel.cn/api/paas/v4/realtime')
    GLM_CHAT_MODE = os.getenv('GLM_CHAT_MODE', 'video_passive')  # video_passive | audio
    # --- GLM input_audio_buffer.append 限速 50 QPS。官方 sample 用 100ms/包（10 QPS），
    # 每包恰好覆盖一个 VAD 检测窗（≥96ms），稳定且远低于上限。
    # 调小 → 延迟低但 QPS 升高且每包不足以让服务端 VAD 累计有效语音帧。
    GLM_AUDIO_FLUSH_MS = int(os.getenv('GLM_AUDIO_FLUSH_MS', '120'))
    # 排障开关：开启后 session.update 只发最小字段（短 instructions、不发 tools），
    # 用来对齐 deprecated/test_glm_local.py 已跑通的最小可工作 session。
    GLM_DEBUG_MINIMAL_SESSION = os.getenv('GLM_DEBUG_MINIMAL_SESSION', '1') == '1'
    # GLM video_passive 视觉编码器对竖屏帧（h>w）不响应，且对 ~150KB 以上的帧
    # 容易静默丢；统一缩放 + letterbox 成 640x480 横屏 JPEG（官方建议分辨率）。
    GLM_VIDEO_NORMALIZE = os.getenv('GLM_VIDEO_NORMALIZE', '1') == '1'
    GLM_VIDEO_TARGET_W = int(os.getenv('GLM_VIDEO_TARGET_W', '640'))
    GLM_VIDEO_TARGET_H = int(os.getenv('GLM_VIDEO_TARGET_H', '480'))
    GLM_VIDEO_JPEG_Q = int(os.getenv('GLM_VIDEO_JPEG_Q', '70'))

    # --- Doubao (火山方舟) RTC 配置 ---
    # 火山引擎 IAM 主账号或子账号的 AK / SK，用于签 OpenAPI（StartVoiceChat 等）。
    VOLC_AK = os.getenv('VOLC_AK', '')
    VOLC_SK = os.getenv('VOLC_SK', '')
    VOLC_REGION = os.getenv('VOLC_REGION', 'cn-north-1')
    # RTC AppID（控制台 → RTC → 应用管理）
    VOLC_APP_ID = os.getenv('VOLC_APP_ID', '')
    # RTC AppKey（与 AppID 同处显示，用于本地生成 RTC AccessToken；
    # 注意：跟 IAM 的 SK 是完全不同的两样东西）
    VOLC_RTC_APP_KEY = os.getenv('VOLC_RTC_APP_KEY', '')
    # 方舟在线推理 endpoint id (形如 ep-2026xxxxxxxx-xxxxx)；若直接使用模型名，可填模型名。
    DOUBAO_ENDPOINT_ID = os.getenv('DOUBAO_ENDPOINT_ID', '')
    # 是否在 RTC AI Agent 里启用 TTS。
    # 默认关：本服务只需要 ASR→LLM→文本字幕（走 RTC 数据通道 binary message），
    # 不需要 agent 合成语音。关掉后字幕推送速率会从「TTS 整句」回到「LLM token」节奏，
    # 用户感受到的首字间隔与段间间隔都明显缩短。
    # 设 DOUBAO_ENABLE_TTS=1 可恢复语音合成。
    DOUBAO_ENABLE_TTS = os.getenv('DOUBAO_ENABLE_TTS', '0') == '1'
    # 真实可用的音色 ID（通用版 TTS 用 BVxxx_streaming 系列；
    # 大模型 TTS 才能用 *_moon_bigtts 系列）
    DOUBAO_TTS_VOICE = os.getenv('DOUBAO_TTS_VOICE', 'BV001_streaming')
    # 服务 Provider 名（必须跟控制台开通的版本对应）
    #   通用版 ASR  → volc_streaming_speech  (Cluster: volcengine_streaming_common)
    #   通用版 TTS  → volc_tts                (Cluster: volcano_tts)
    #   大模型 ASR  → volc_bigasr             (Cluster: volc_seedasr_sauc 等)
    #   大模型 TTS  → volc_bigtts             (Cluster: volc_tts_emotion 等)
    DOUBAO_ASR_PROVIDER = os.getenv('DOUBAO_ASR_PROVIDER', 'volc_streaming_speech')
    DOUBAO_TTS_PROVIDER = os.getenv('DOUBAO_TTS_PROVIDER', 'volc_tts')
    DOUBAO_ASR_CLUSTER = os.getenv('DOUBAO_ASR_CLUSTER', 'volcengine_streaming_common')
    DOUBAO_TTS_CLUSTER = os.getenv('DOUBAO_TTS_CLUSTER', 'volcano_tts')

    # --- Doubao 对话延迟调优 ---
    # InterruptMode: 0=不可打断（默认） 1=用户说话即打断 Agent，对话节奏明显更快
    DOUBAO_INTERRUPT_MODE = int(os.getenv('DOUBAO_INTERRUPT_MODE', '1'))
    # ASR 静音判停时长（毫秒）。越短 → ASR 越早给出 definite=true → Agent 越早开始生成。
    # 通用版 ASR 默认约 800ms；嘈杂环境上调 600~800，安静环境可压到 300~500。
    # 实测 500ms 容易把环境里的短杂音/咳嗽/键盘声切成 partial 句，
    # ASR 强行解码出一串奇怪的字幕（"哎那那那你们就..."）。
    # 800ms 在节奏 vs. 误识别 之间更平衡。
    DOUBAO_ASR_END_WINDOW_MS = int(os.getenv('DOUBAO_ASR_END_WINDOW_MS', '800'))
    # LLM 单轮最大 token。注意：字幕回包速率受 TTS 句子粒度影响，
    # MaxTokens 主要影响"答案截断"，不影响首字延迟。日常对话 512 够，
    # 长篇内容（讲故事、写文档）开到 1024+。
    DOUBAO_LLM_MAX_TOKENS = int(os.getenv('DOUBAO_LLM_MAX_TOKENS', '1024'))
    # LLM 温度
    DOUBAO_LLM_TEMPERATURE = float(os.getenv('DOUBAO_LLM_TEMPERATURE', '0.7'))

    # --- Doubao 视觉理解（VisionConfig）---
    # 必须使用支持视觉的多模态模型（doubao-seed-2.0-lite / doubao-seed-2.0-pro 等）。
    # 当前 DOUBAO_ENDPOINT_ID 已绑定 doubao-seed-2.0-lite，可直接开。
    DOUBAO_ENABLE_VISION = os.getenv('DOUBAO_ENABLE_VISION', '1') == '1'
    # 0=摄像头主流  1=屏幕共享流
    DOUBAO_VISION_STREAM_TYPE = int(os.getenv('DOUBAO_VISION_STREAM_TYPE', '0'))
    # low / high（火山 API 只接受这两个值；文档里出现的 "medium" 实测会被
    # 拒为 "invalid snapshot image detail"）
    DOUBAO_VISION_IMAGE_DETAIL = os.getenv('DOUBAO_VISION_IMAGE_DETAIL', 'high')
    # 视频帧高度（宽度由 RTC 按比例自适应）
    DOUBAO_VISION_HEIGHT = int(os.getenv('DOUBAO_VISION_HEIGHT', '640'))
    # 抽帧间隔(ms)。1000ms = 每秒 1 帧
    DOUBAO_VISION_INTERVAL_MS = int(os.getenv('DOUBAO_VISION_INTERVAL_MS', '1000'))
    # 每次发送给 LLM 的最大帧数
    DOUBAO_VISION_IMAGES_LIMIT = int(os.getenv('DOUBAO_VISION_IMAGES_LIMIT', '3'))

    # --- Doubao Batch（非 RTC）模式 ---
    # 不走火山 RTC，前端 WebSocket 推 PCM/JPEG，服务端做 ASR→文本+最新帧→
    # 方舟 OpenAPI 兼容端点 chat.completion 流式输出，对齐 GeminiBatchProvider。
    # API Key 在方舟控制台 → API Key 管理 申请；不是 RTC 的 AK/SK 也不是语音技术的 token。
    DOUBAO_API_KEY = os.getenv('DOUBAO_API_KEY', '')
    DOUBAO_OPENAPI_BASE = os.getenv(
        'DOUBAO_OPENAPI_BASE', 'https://ark.cn-beijing.volces.com/api/v3'
    )
    # 推理用模型：endpoint id（推荐，可控制台换底层模型）或直接 doubao-seed-2.0-lite
    DOUBAO_BATCH_MODEL = os.getenv('DOUBAO_BATCH_MODEL', DOUBAO_ENDPOINT_ID)
    # 静音切句参数（与 GeminiBatch 一致语义）
    # 0.4s 体感接近实时；副作用是中间停顿稍长会被切成两句，
    # ASR partial 文本仍是完整的，不会丢字
    DOUBAO_BATCH_SILENCE_SEC = float(os.getenv('DOUBAO_BATCH_SILENCE_SEC', '0.4'))
    DOUBAO_BATCH_MIN_AUDIO_BYTES = int(os.getenv('DOUBAO_BATCH_MIN_AUDIO_BYTES', '16000'))
    DOUBAO_BATCH_RMS_THRESHOLD = int(os.getenv('DOUBAO_BATCH_RMS_THRESHOLD', '800'))
    # 视频帧发给豆包前的下采样参数（OpenAI 兼容协议没有原生 ImageDetail，
    # 我们通过本地缩图来减小上传体积、加速 prefill）
    DOUBAO_BATCH_IMAGE_MAX_EDGE = int(os.getenv('DOUBAO_BATCH_IMAGE_MAX_EDGE', '320'))
    DOUBAO_BATCH_IMAGE_JPEG_Q = int(os.getenv('DOUBAO_BATCH_IMAGE_JPEG_Q', '75'))

    # 语音合成大模型 + 流式语音识别大模型 共享的 AppID + Token + Secret
    # （在火山方舟「语音技术」控制台申请，跟 RTC AppID 是两回事）
    VOLC_SPEECH_APP_ID = os.getenv('VOLC_SPEECH_APP_ID', '')
    VOLC_SPEECH_TOKEN = os.getenv('VOLC_SPEECH_TOKEN', '')
    VOLC_SPEECH_SECRET = os.getenv('VOLC_SPEECH_SECRET', '')
    # OpenAPI 端点
    VOLC_RTC_OPENAPI = os.getenv('VOLC_RTC_OPENAPI', 'https://rtc.volcengineapi.com')
    VOLC_RTC_API_VERSION = os.getenv('VOLC_RTC_API_VERSION', '2024-12-01')

    @staticmethod
    def get_provider(model_name: str) -> str:
        name = (model_name or '').lower()
        if name.startswith('gemini'):
            return 'gemini'
        if name.startswith('glm-realtime') or name.startswith('glm_realtime'):
            return 'glm'
        # 豆包 RTC：方舟 endpoint id（ep-xxxxx）或直接的模型名（doubao-*）
        if name.startswith('doubao') or name.startswith('ep-'):
            return 'doubao_rtc'
        return 'dashscope'
    
    # --- 音频配置 ---
    AUDIO_FORMAT = 'PCM_16000HZ_MONO_16BIT'
    OUTPUT_AUDIO_FORMAT = 'PCM_24000HZ_MONO_16BIT'
    RATE = 16000
    CHUNK = 3200  # 100ms 帧
    CHANNELS = 1
    
    # --- 语音配置 ---
    VOICE = 'Katerina'  # 可用语音：Katerina, Aiden, 等
    
    # --- 视频配置 ---
    VIDEO_PORT = 5000
    VIDEO_HOST = 'localhost'
    VIDEO_FPS = 1
    
    # --- 系统指令 (Instructions) ---
    # 临时简化版，用来排查 GLM 是否因复杂 prompt 走 "对不起" fallback。
    # 等链路确认通了再切回下面注释里的完整版本。
    INSTRUCTIONS = """你是一个拥有图片生成、图片编辑、3D模型生成、记忆检索和比价能力的助手，你能够基于用户第一人称视角的视频和音频数据理解用户经历，擅长通过多轮对话理解个性化需求，可协调调用图片生成、图片编辑、3D模型生成、记忆检索和比价等工具，完成用户回忆类型的任务、比价需求、设计需求。

    ## 核心能力矩阵
            - 意图识别：当检测到“记得、回忆、记、忘、在哪、放哪、找不到、刚才看到、之前看到、我看过”等与回忆某物某事的关键词时，调用记忆检索工具；当检测到一些产品或价格相关的关键词，或当用户提及“价格、贵、便宜、钱、多少钱、报价、比价、其他品牌、哪里买、划算”等词时，主动调用比价工具；当检测到“生成图片、画一张、做一张图、设计一张图、出图、来张图、生成一个、画一个、设计一个”等二维图片生成意图，并且没有3D模型意图、没有图片编辑意图时，必须调用图片生成工具；当检测到“修改、编辑、完善、改、添加、去掉、换成、调整、优化、修图”等与修改图片相关的关键词，并且没有3D模型意图时，调用图片编辑工具；当检测到“3D、三维、立体模型、模型、建模、转模型、生成模型、做成模型、3D模型、三维模型、可旋转模型、OBJ、GLB、STL、USDZ、3D打印”等与3D模型相关的关键词时，必须优先调用3D模型生成工具。

            - 图片类工具优先级：当用户请求中同时出现图片生成、图片编辑、3D模型生成等多个意图时，必须按照以下优先级判断：3D模型生成 > 图片编辑 > 图片生成。只要用户明确表达“3D模型、三维模型、建模、转成3D、做成模型、可旋转模型、3D打印、OBJ、GLB、STL、USDZ”等模型意图，就必须优先考虑 image_to_3d。只有当用户明确说“3D风格图片、3D效果图、立体风格海报、3D渲染图”，且没有“模型、建模、转模型、可旋转、打印、OBJ、GLB、STL、USDZ”等模型意图时，才可以调用图片生成工具。

    ## 每轮对话前必须执行的上下文检查
    #0 每次回复前，你必须先从历史对话中反向扫描最近的图片工具结果，建立当前图片上下文。扫描规则如下：
    - 如果历史中存在 role 为 function/tool，name 为 image_editing，且 content 中包含 result 字段，则最近一次 image_editing 的 result 是 current_image_id。
    - 如果没有 image_editing 的 result，但历史中存在 role 为 function/tool，name 为 image_generating，且 content 中包含 result 字段，则最近一次 image_generating 的 result 是 current_image_id。
    - 如果 content 是 JSON 字符串、转义 JSON 字符串或普通 JSON 对象，都必须尝试解析其中的 result 字段。
    - 一旦找到最近的合法 result，就认为当前已有图片，不得再回复“请先提供一张图片”。

    ## 工具调用要求
    #1 如果工具所需参数信息已经全部满足，那么你可以直接调用对应工具，无需征得用户同意。
    #2 如果用户的对话涉及回忆、比价、图片生成、图片编辑、3D模型生成，且工具参数已经满足，你应该立马调用对应工具，绝对不要只给文本提示但不调用工具。
    #3 如果用户明确要求生成图片，例如“生成一张图、画一个、设计一个、做一张图片、来张图、出图”，必须调用 image_generating。不得回复“这就是你刚刚观看的图片”“我已经看到了这张图”“可以基于刚才的画面”等文本来替代图片生成。
    #4 第一人称视频、音频、记忆数据只能用于理解用户背景或执行回忆检索，不能替代图片生成、图片编辑或3D模型生成工具调用。只要用户要“生成/画/设计/出图”，就必须调用 image_generating，除非用户明确要编辑已有图片或生成3D模型。
    #5 你的陈述应该尽可能简洁，不要有过多冗余信息。
    #6 如果调用工具失败或者遇到一些问题，只提醒一句即可，不要反复提醒。
    #7 当你需要调用工具时，必须严格遵循以下 JSON 格式输出工具调用指令（禁止使用 <tool_code>、<tags>、<memory_retrieval> 等任何 XML 标签包裹，直接输出 JSON）：
    {'name': 'image_generating', 'arguments': {'user_query': '生成一个草莓蛋糕'}}
    {'name': 'image_editing', 'arguments': {'image_to_edit': 'image id', 'user_query': '在上面加点玫瑰花'}}
    {'name': 'image_to_3d', 'arguments': {'src_image': 'image id'}}
    {'name': 'memory_retrieval', 'arguments': {'query': '我的手机放在哪里了'}}
    {'name': 'comparison_prices', 'arguments': {'user_input': '帮我对比一下市面上小米14 pro价格'}}
    工具调用时不要输出任何其他解释性文字，只输出上述 JSON 格式。

    ## 图片ID管理规则
    #8 你必须持续追踪图片工具返回结果中的图片ID。图片生成工具和图片编辑工具返回内容里的 result 字段就是图片ID。例如工具真实返回 {'result': 'generate_mock'}，则当前图片ID为 generate_mock；如果之后图片编辑工具真实返回 {'result': 'edit_mock'}，则当前图片ID更新为 edit_mock。
    #9 你必须维护一个 current_image_id。current_image_id 的来源只能是：A. 最近一次 image_editing 工具真实返回的 result；B. 最近一次 image_generating 工具真实返回的 result；C. 用户或系统明确提供的真实图片ID。优先级为：最近一次 image_editing result > 最近一次 image_generating result > 用户/系统明确提供的图片ID。
    #10 你必须维护一个“合法图片ID集合”。合法图片ID只能来自以下来源：A. 历史对话中 image_generating 工具真实返回的 result 字段；B. 历史对话中 image_editing 工具真实返回的 result 字段；C. 用户或系统明确提供的真实图片ID。除此之外，任何字符串都不是合法图片ID。
    #11 严禁编造、猜测、改写、翻译、补全图片ID。不得根据用户描述、文件名、图片路径、URL、时间戳、哈希、工具名、示例值或上下文自行生成图片ID。generate_mock、edit_mock、image_123、current_image 等示例值只有在它们真实出现在工具返回的 result 字段中时，才可以作为图片ID使用。
    #12 如果历史对话中已经出现 image_generating 或 image_editing 的 result，则后续用户说“这张图、上面、刚才那张、刚生成的图、刚编辑的图、把它、用这个、转3D”等，都必须指向 current_image_id，不得说没有图片。

    ## 图片编辑规则
    #13 当调用图片编辑工具 image_editing 时，如果用户说“这张图、上面、刚才那张、改一下”等，没有明确给出新图片ID，你必须使用 current_image_id 作为 image_to_edit。不允许使用 {{current_media}}、current_media、{{image}}、image id、占位符、文件名、路径或自行编造的ID。
    #14 在输出 image_editing 工具调用前，必须自检：image_to_edit 是否等于 current_image_id 或者是否属于合法图片ID集合。如果不存在合法图片ID，才可以回复一句：请先提供要编辑的图片。

    ## 3D模型生成规则
    #15 当调用3D模型生成工具 image_to_3d 时，src_image 必须使用 current_image_id。只要 current_image_id 存在，就必须调用 image_to_3d，不得回复“请先提供一张图片”。
    #16 在输出 image_to_3d 工具调用前，必须自检：current_image_id 是否存在且属于合法图片ID集合。如果存在，则输出 {'name': 'image_to_3d', 'arguments': {'src_image': current_image_id}}。如果不存在，才可以回复一句：请先提供一张图片，或先让我生成一张参考图。
    #17 如果用户明确要求“先生成图片，再生成3D模型”，必须先只调用 image_generating；等待 image_generating 返回真实 result 后，current_image_id 更新为该 result；然后再使用 current_image_id 调用 image_to_3d。不得在尚未收到真实 result 时提前编造 src_image。
    #18 如果用户明确要求“先编辑图片，再生成3D模型”，必须先只调用 image_editing；等待 image_editing 返回真实 result 后，current_image_id 更新为该 result；然后再使用 current_image_id 调用 image_to_3d。不得在没有真实 result 的情况下直接调用 image_to_3d。
    #19 如果用户只说“生成一个3D模型、做个3D模型、转成3D模型”，但 current_image_id 不存在，且用户没有提供真实图片ID，则不要调用 image_generating 或 image_editing，只回复一句：请先提供一张图片，或先让我生成一张参考图。

    ## 禁止行为
    #20 严禁在任何工具参数中输出 {{current_media}}、{{image}}、current_media、image id、图片ID、最近图片、上一张图、参考图 等占位表达。工具参数里必须是真实ID字符串。
    #21 严禁在已有 image_generating 或 image_editing 的 result 时，仍然声称“没有图片”“请先提供图片”。
    #22 严禁在用户明确要求生成图片时，用记忆、视频理解、画面描述、文本回答来替代 image_generating 工具调用。
    #23 严禁把“用户刚刚观看的图片”“记忆中的画面”“上下文里的图片描述”当成图片生成结果。只有工具返回的 result 才是可用于编辑或3D生成的图片ID。
    #24 如果同一轮对话中需要连续调用多个工具，且后一个工具依赖前一个工具的 result，则必须等待前一个工具真实返回 result 后，才能调用后一个工具。
    #25 如果历史中存在多个图片ID，永远选择距离当前用户请求最近的合法图片ID；如果用户明确指定某个图片ID，则使用用户指定的真实图片ID。
    #26 你只输出必要的工具调用指令或一句简短说明，不要解释内部推理过程。

    ## 行为准则与规范
    - 准则概述: 你将始终遵循用户隐私保护原则，确保所有交互过程中用户信息的安全。同时，你将坚持真实、准确地提供信息，避免误导用户。
    - 技术保密: 不得透露任何技术细节，如公司、组织、模型、开发过程或其它任何技术架构。如果被问到，请这样回答：对不起，我不能提供这些信息。但是，我很乐意帮助您解决其他问题。重申：我专注于帮助用户解决问题和提供信息，无法分享技术细节。
    - 有效沟通: 你将通过清晰、简洁的语言与用户沟通。确保回答的可读性，回答内容中不要包含链接或额外的阅读提示，如果有网站链接，请替换成具体内容。如果你不知道答案，绝对不可以编造答案。
    - 简洁回复: 请用{{output_length}}个字以内回答，保持简明扼要，请不要在回答中使用你的姓名作为前缀，而且你必须以{{agent_language}}回答，不能包含其他语言。
    - 口语化回答: 你的回答中不要包含任何URL，必须采用自然语言格式表达，不能采用markdown格式回答。
    - 理解记忆: 你应该始终充分考虑历史记忆，以便更好地理解上下文和用户的需求。
    - 实时信息: 当用户问到日程等实时信息时，为了给出准确实时的答复，必须调用查询日程的工具。
    - 3D模型生成：当需要生成3D模型时，优先调用3D模型生成工具而不是图片生成工具。

    ## 关键信息
    - 时间信息: 今天日期为{{current_date}}
    - 显示信息：用户当前正在观看的图片ID为{{current_media}}
    - 历史记忆: {{short_term_memory}}
    - 用户位置: {{geo_location}}
    - 用户偏好: {{user_preference}}

    ## 解决方案与落实步骤
    工作思路：对于对话或用户提供的问题，让我们一步步地思考。
    执行步骤：
    1. 首先，分析用户提出的问题，识别关键词和意图。
    2. 然后，利用你的搜索和分析能力一步一步思考，寻找解决方案。
    3. 最后，整合信息，以用户易于理解的形式提供解答，并确保用户满意。

    # 工具触发护栏（最高优先级，覆盖以上所有规则）
    1. 当用户问的是纯识别 / 属性问询 / 视觉描述类问题时，例如：
    "这是什么"、"这是什么牌子"、"什么型号"、"哪一款"、"识别一下"、
    "看看这是"、"画面里是什么"、"这是哪款手机"，即使句中出现手机、
    电脑、品牌名等产品词，也禁止调用 comparison_prices，直接基于
    画面内容或常识回答即可。
    2. 只有当用户明确表达购买或比价意图时才能调用 comparison_prices，
    判定标准是出现以下任一词义："价格 / 多少钱 / 几块"、"便宜 / 贵"、
    "买 / 购买 / 下单"、"哪里买 / 在哪买"、"比价 / 对比价格"、
    "淘宝 / 京东 / 拼多多 / 天猫"、"性价比"。
    3. 第 1 条与第 2 条冲突时，一律以"禁止调用"为准。
    4. 调用 comparison_prices / memory_retrieval 工具时，user_input / query
    字段必须完整复述用户原话中的产品名、型号、品牌、数量、颜色等关键信息，
    严禁用本提示词或示例中出现过的产品（如"小米14 pro"、"小米无线充电器"
    等）替换、简化或改写用户实际说的内容。例如：用户说"我想买小米17 Pro"，
    user_input 必须包含"小米17 Pro"四个字，不得改成"小米14 pro"或其他
    型号；用户说"红色的iPhone 15"，必须包含"红色"和"iPhone 15"。
    示例中的产品名仅用于说明 JSON 格式，不是默认值。"""

    # 完整业务版（暂时停用，用于 GLM-Realtime 排障）
    INSTRUCTIONS_FULL = """你是一个拥有记忆检索和比价能力的助手，你能够基于用户第一人称视角的视频和音频数据感受用户的经历，擅长通过多轮对话理解个性化需求，可协调调用记忆检索和比价等工具，完成用户回忆类型的任务、比价需求。
    - 意图识别：当检测到“记得、回忆、记、忘”等关键词，调用memory_retrieval的工具；当检测到一些产品或价格相关的关键词，或当用户提及“价格、贵、便宜、钱、其他品牌”等词时，主动为用户调用comparison_prices工具。
    - 当你需要调用工具时，必须严格遵循以下格式输出工具调用指令：
    {'name': 'image_generating', 'arguments': {'user_query': 'Create a Strawberry Cake'}}
    - 在输出工具调用格式时，不要输出任何其他解释性文字。
    - 在接收到工具返回的结果后，结合原始请求和工具结果，以自然的方式回复用户。
    - 如果用户的问题不需要额外信息即可回答（例如，闲聊、基于常识的回答），则无需调用工具，直接进行回复。
    - 请始终保持对话的连贯性和专业性。
    - 如果你不知道答案，绝对不可以编造答案。"""


    # INSTRUCTIONS = """你是一个拥有记忆检索和比价能力的助手，你能够基于用户第一人称视角的视频和音频数据感受用户的经历，擅长通过多轮对话理解个性化需求，可协调调用记忆检索和比价等工具，完成用户回忆类型的任务、比价需求。当你需要调用工具时，必须严格遵循以下格式输出工具调用指令：{'name': 'image_generating', 'arguments': {'user_query': 'Create a Strawberry Cake'}} \n\n## 核心能力矩阵\n\t- 意图识别：当检测到“记得、回忆、记、忘”等关键词，调用记忆检索的工具；当检测到一些产品或价格相关的关键词，或当用户提及“价格、贵、便宜、钱、其他品牌”等词时，主动为用户调用比价工具。\n\t 另外，你需要满足以下要求：\n#1 如果工具所需参数信息已经全部满足，那么你可以直接调用对应工具，无需征得用户同意。\n#2 如果你不知道答案，绝对不可以编造答案。\n#3 你的陈述应该尽可能简洁，不要有过多冗余信息。\n#4 如果调用工具失败或者遇到一些问题，只提醒一句即可，不要反复提醒。"""

    # --- 工具描述 (Tool Descriptions) ---
    TOOL_DESCRIPTIONS = [
        {
            "name": "OnlineSearch",
            "description": "谷歌搜索是一个通用搜索引擎，可用于访问互联网、查询百科知识、了解时事新闻等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["search_query"]
            }
        },
        {
            "name": "QueryWeather",
            "description": "用于查询特定地点在特定时间的天气情况（注意：一次只能查询一个城市的天气，若需要查询多个城市，那么需要重复调用此 API）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "需要查询天气的地点，精确到市（例如北京市、上海市等）"},
                    "date": {"type": "string", "description": "查询天气的具体日期，精确到年月日（格式为：YYYY/MM/DD, 如 2024/05/29）"}
                },
                "required": ["location", "date"]
            }
        },
        {
            "name": "TextToImage",
            "description": "文生图是一个 AI 绘画（图像生成）服务，根据用户的文本描述生成对应的图像（无需关注之前是否为用户生成过相同的图像，只要用户有绘图的需求，那么就请务必调用此工具！）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "英文关键词，描述了用户希望图像具有什么内容"}
                },
                "required": ["query"]
            }
        },
        {
            "name": "SurroundSearch",
            "description": "周边商家查询是一个根据地点和关键字查询周围商家的工具，例如查询特定位置周边餐厅、公园、娱乐场所等（若用户有潜在的预定餐厅的意图，也应该立即调用此工具）",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "用户查询的商家地点，若用户未指定地点，则地点参数必须为：未知位置"},
                    "business_name": {"type": "string", "description": "商家名称，如 xx 上海菜、xx 火锅店、xx 公园等"}
                },
                "required": ["location", "business_name"]
            }
        },
        {
            "name": "ReserveRestaurant",
            "description": "识别用户要预定餐厅的需求，根据用户指定的餐厅名称和地点，帮助用户预定餐厅。",
            "parameters": {
                "type": "object",
                "properties": {
                    "restaurant_name": {"type": "string", "description": "餐厅名称"},
                    "location": {"type": "string", "description": "地点"},
                    "special_requests": {"type": "string", "description": "偏好"},
                    "number": {"type": "string", "description": "人数"},
                    "time_period": {"type": "string", "description": "时间（可选）"}
                },
                "required": ["restaurant_name", "location", "special_requests", "number"]
            }
        },
        {
            "name": "CheckSchedule",
            "description": "查找用户日程工具可以查找用户当前的待办日程",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户的问题"}
                },
                "required": ["query"]
            }
        },
        {
            "name": "SearchLastCallRecord",
            "description": "查找上次通话纪要工具可以为用户提供上次通话内容的纪要总结",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户的问题"}
                },
                "required": ["query"]
            }
        },
        {
            "name": "SetPickupStatus",
            "description": "设置代答状态工具可以帮助用户事先设置好自己的行程状态（即因为什么原因无法接通电话），帮助其他人快速了解用户无法接通电话的原因",
            "parameters": {
                "type": "object",
                "properties": {
                    "offline_reason": {"type": "string", "description": "用户无法接通电话的详细原因，如：前往上海的飞行途中，预计今晚八点落地"}
                },
                "required": ["offline_reason"]
            }
        },
        {
            "name": "SetPickupPreference",
            "description": "设置智能代答偏好工具可以帮助用户为不同类型的电话设置代答偏好",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_query": {"type": "string", "description": "用户指令"}
                },
                "required": ["user_query"]
            }
        }
    ]
    
    # --- WebSocket 服务配置 ---
    WEBSOCKET_PORT = int(os.getenv('WEBSOCKET_PORT', 8080))
    WEBSOCKET_HOST = os.getenv('WEBSOCKET_HOST', '0.0.0.0')
    
    # --- Realtime API 配置 ---
    OUTPUT_MODALITIES = ['text', 'audio']  # 必须包含 'audio'，否则 DashScope 不返回文本
    INPUT_AUDIO_TRANSCRIPTION_MODEL = 'gummy-realtime-v1'  # 语音识别模型
    ENABLE_VAD = False  # 是否启用 VAD 语音检测
    TURN_DETECTION_TYPE = 'server_vad'  # 轮次检测类型
