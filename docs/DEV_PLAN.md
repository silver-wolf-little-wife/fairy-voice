# fairy-voice 开发计划

> 目标：按钮触发式语音助手，通过 WebSocket 接入 AstrBot，由 AstrBot 的 LLM/Agent 处理语音指令并回传结果。
> C 端为独立仓库 fairy-voice-app（Python 语音终端），本仓库仅含 B 端插件。
> 架构参考：cherry-astrbot（B 端插件） + cherry-remote-app（C 端执行器）的双仓库协作模式。

## 整体架构

```
C端（fairy-voice-app，Python）                 B端（AstrBot 插件，Python）
┌──────────────────────────┐                 ┌──────────────────────────┐
│ 热键/托盘触发录音         │                 │ aiohttp WS 服务端         │
│   ↓ 按下                  │   WS 主动外连    │   ↓ hello/token 握手      │
│ 录音 + 语音识别           │ ──────────────▶ │   ↓ 收到 ask 请求         │
│   ↓ 识别文本               │    JSON 帧      │ llm_generate /           │
│ WS 客户端发送 ask          │                 │ tool_loop_agent          │
│   ↓                       │ ◀────────────── │   ↓ AI 结果回传           │
│ TTS 播报                  │    response     │                          │
└──────────────────────────┘                 └──────────────────────────┘
```

- 传输：WebSocket，C 端主动外连 B 端（穿透 NAT），复用 cherry 的 hello/心跳/request-response 帧模式
- 角色：C = 语音采集与播报（纯终端），B = AI 大脑（LLM 生成 + Agent 工具循环）
- 与 cherry 的区别：cherry 的 C 端是"执行器"，本项目的 C 端是"语音终端"；B 端新增"把手机指令喂给 AstrBot LLM"的环节

## 里程碑

### M1 协议定稿（第 1 周）
- [x] 设计 fairy-voice 协议 v1.0.0：hello 握手（token + device_id）、心跳、ask 请求/响应
- [x] 帧格式：`{"type":"ask","id":...,"text":"...","lang":"zh-CN"}` → `{"type":"response","id":...,"ok":true,"data":{"text":"AI回复","audio_hint":true}}`
- [x] 文档写入 docs/PROTOCOL.md，双端同步

### M2 B 端插件骨架（第 2 周）
- [x] `astrbot_plugin_fairy_voice/`：metadata.yaml + _conf_schema.json（ws_port / auth_token / heartbeat_timeout）
- [x] aiohttp WS 服务端：连接管理、token 校验、心跳超时清理（参考 cherry-astrbot/ws_server.py）
- [x] 核心：收到 ask → 按 device_id 维护内存会话（见记忆策略实现），`llm_generate(contexts=recent)` 带上下文生成 → 回复回写 recent
- [x] 记忆策略：仅保留最近 3 轮；超 3 轮且 5 分钟内再对话 → LLM 摘要压缩旧记忆注入 system_prompt；超 5 分钟未对话 → 清空记忆重开
- [x] 进阶：`tool_loop_agent` 工具调用（伪造最小 event，enable_tools 配置开启，默认关，待实机验证）
- [x] 命令：`/fairy` 查看在线设备与状态

### M3 C 端语音终端骨架（第 3 周）—— 独立仓库 [fairy-voice-app](https://github.com/silver-wolf-little-wife/fairy-voice-app)
- [x] Python 常驻应用（仿 cherry-remote-app 形态）：`src/fairy_voice_app` 包
- [x] WS 客户端（aiohttp）：hello 握手 / 心跳 / ask 请求响应 / 断线指数退避重连
- [x] 配置：config.example.yaml（server_url / auth_token / device_id / 心跳 / ask 超时）
- [x] 测试：tests/test_ws_client.py（本地模拟 B 端，5 用例全绿）
- [x] 联调入口：`python -m fairy_voice_app -c config.yaml`（当前命令行输入指令）

### M4 语音闭环（第 4 周）
- [ ] 录音（sounddevice）+ 语音识别（本地 faster-whisper 或系统 API）
- [ ] TTS 播报（edge-tts / pyttsx3），热键/托盘触发录音
- [ ] 完整链路：触发 → 录音 → 识别 → WS 发送 → AI 回复 → TTS 播报
- [ ] 不做连续问答 UI（按一次答一次），AI 侧保留 3 轮内上下文记忆（超出按记忆策略压缩/抛弃）
- [ ] 状态机：空闲/录音中/识别中/等待AI/播报中（托盘/气泡显示状态）
- [ ] 唤醒词预留：后续可在录音链路前加轻量唤醒（可选项）

### M5 打磨与安全（第 5 周）
- [ ] TTS 语速/音量设置、识别语言切换
- [ ] token 本地加密存储、WS 支持 wss（TLS 部署见 fairy-voice-app/docs/DEPLOY.md）
- [ ] PyInstaller 打包独立 exe（Release 分发）
- [ ] 开机自启 / 托盘常驻

## B 端关键技术点（已确认，来自 AstrBot 官方文档）

```python
# 获取当前会话的 provider
provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)

# 直接调用 LLM（v4.5.7+）
llm_resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt="...")
text = llm_resp.completion_text

# 带工具的 Agent 循环
resp = await self.context.tool_loop_agent(
    event=event, chat_provider_id=provider_id,
    prompt="...", tools=ToolSet([...]), max_steps=30,
)
```

> 已确认（M2 源码验证）：WS 请求无真实 event，但 `AstrMessageEvent`（ABC 无抽象方法）、`AstrBotMessage()`、`PlatformMetadata(name,desc,id)` 均可无平台构造，插件内伪造最小 event 即可调用 `tool_loop_agent` 完整工具循环；umo = fairy_voice:friend:{device_id}，工具注册表全局共享。

## 记忆策略实现（B 端，按 device_id 维护内存会话）

```python
# session = {"summary": None, "recent": [], "last_active": 0}
# recent 为最近 <=3 轮消息（1 轮 = user+assistant 2 条）
if now - session["last_active"] > 300:      # 超 5 分钟 → 抛弃记忆
    session = {"summary": None, "recent": [], "last_active": now}

session["recent"].append({"role": "user", "content": text})

# 超出 3 轮 → 把最早 1 轮压缩进 summary（LLM 摘要）
if len(session["recent"]) > 6:
    old = session["recent"][:2]
    session["summary"] = await llm_generate(prompt=f"把以下对话压缩成要点摘要：{old}")
    session["recent"] = session["recent"][2:]

resp = await llm_generate(
    chat_provider_id=... ,
    system_prompt=session["summary"],  # 压缩记忆注入
    contexts=session["recent"],         # 最近 3 轮原文
)
session["recent"].append({"role": "assistant", "content": resp.completion_text})
session["last_active"] = now
```

## 验收标准

- 按热键 → 3 秒内开始录音，识别文本 3 秒内返回
- WS 断线 15 秒内自动重连，重连后状态同步
- AI 回复从发送到 TTS 播报 < 10 秒（视模型速度）
- 常驻挂机内存 < 150MB，空闲 CPU < 1%
- 超 5 分钟未对话后再次指令，AI 无上一段记忆（验证抛弃逻辑）
- token 错误握手即断，无明文存储

## 参考

- C 端语音终端：`D:\project\fairy-voice-app`（GitHub: silver-wolf-little-wife/fairy-voice-app）
- 插件架构与 WS 服务端：`D:\project\cherry-astrbot`
- 协议模式：`cherry-astrbot/docs/PROTOCOL.md`
- AstrBot 插件 API：`D:\project\AstrBot\docs\zh\dev\star\guides\ai.md`
