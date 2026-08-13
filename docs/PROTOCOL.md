# Fairy Voice 通信协议 v1.1.0

> 双端共享协议。`astrbot_plugin_fairy_voice`（B 端插件）与 `fairy-voice-app`（C 端 App）必须保持同步。

## 1. 概述

- **传输**：WebSocket（生产环境建议 wss/TLS）。
- **方向**：C 端（手机）**主动外连** B 端（AstrBot 插件服务端，穿透家庭 NAT），B 永不主动连 C。
- **帧格式**：UTF-8 JSON 文本帧。
- **角色**：C = 语音采集与播报终端（录音/TTS），B = AI 大脑（ASR 识别 + LLM 生成 + 记忆管理，v1.1.0 起 ASR 在 B 端）。
- **协议风格**：JSON-RPC 风格，请求/响应以 `id` 关联。
- **与 Cherry Remote 的区别**：Cherry 是 B→C 下发指令（C 为执行器）；本协议是 C→B 上送语音指令（C 为终端），B 处理后回传 AI 回复。

## 2. 连接握手

C 连接成功后，必须首先发送 `hello`：

```json
{"type": "hello", "token": "<auth_token>", "device_id": "my-phone", "client_version": "0.1.0"}
```

B 响应：

```json
{"type": "hello_ack", "ok": true, "session_id": "<uuid>", "server_version": "0.1.0"}
```

失败时 B 返回 `ok:false` 并立即断开：

```json
{"type": "hello_ack", "ok": false, "error": "invalid_token"}
```

握手失败错误码：`invalid_token` / `missing_device_id`。

## 3. 心跳

- C 每 `heartbeat_interval`（默认 15s）发送 `{"type": "ping"}`。
- B 收到即回 `{"type": "pong"}`。
- B 在 `heartbeat_timeout`（默认 60s）内未收到某设备**任何帧**，判定掉线并清理设备记录与记忆会话。
- C 若超过 `heartbeat_interval * 3` 未收到 B 任何帧，主动断开并重连。

## 4. 语音指令请求（C → B）

### 4.1 文本指令 ask

```json
{
  "type": "ask",
  "id": "<uuid>",
  "text": "帮我查一下明天的天气",
  "lang": "zh-CN"
}
```

- `text`：语音识别后的指令文本，必填，非空。
- `lang`：识别语言，可选，默认 `zh-CN`。
- 同一设备同时只处理一个 ask（B 端按 device_id 串行）。

### 4.2 语音指令 voice_ask（v1.1.0，M4-2）

```json
{
  "type": "voice_ask",
  "id": "<uuid>",
  "audio": "<WAV 文件 base64>",
  "lang": "zh-CN"
}
```

- `audio`：16kHz / 16bit / 单声道 WAV 录音的 base64，必填，非空。
- `lang`：识别语言，可选，默认 `zh-CN`。
- B 端流程：ASR 识别（faster-whisper，懒加载）→ 走与 ask 相同的记忆/LLM/工具链路
  → 响应携带 `recognized`（识别文本）与 `text`（AI 回复），C 端一跳完成语音问答。
- ASR 依赖未安装或未初始化时返回 `asr_unavailable`；音频解码失败返回 `bad_audio`。

## 5. 响应（B → C）

成功（ask）：

```json
{
  "type": "response",
  "id": "<uuid>",
  "ok": true,
  "data": {"text": "明天晴，23~31℃。"},
  "error": null
}
```

成功（voice_ask，v1.1.0 起 `data` 增加 `recognized` 字段）：

```json
{
  "type": "response",
  "id": "<uuid>",
  "ok": true,
  "data": {"text": "明天晴，23~31℃。", "recognized": "明天天气怎么样"},
  "error": null
}
```

失败：

```json
{
  "type": "response",
  "id": "<uuid>",
  "ok": false,
  "data": null,
  "error": {"code": "llm_error", "message": "..."}
}
```

## 6. 错误码

| code | 含义 |
|---|---|
| `invalid_token` | 认证 token 错误 |
| `missing_device_id` | 缺少设备标识 |
| `empty_text` | ask 的 text 为空 / voice_ask 未识别到内容 |
| `empty_audio` | voice_ask 的 audio 为空（v1.1.0） |
| `bad_audio` | voice_ask 音频 base64 解码失败（v1.1.0） |
| `asr_unavailable` | ASR 未初始化/依赖缺失（v1.1.0） |
| `busy` | 设备已有 ask 在处理 |
| `provider_not_found` | 未配置可用 LLM Provider |
| `llm_error` | LLM 生成失败 |
| `internal_error` | 内部异常 |

## 7. 记忆策略（B 端，按 device_id）

- 仅保留最近 **3 轮**对话原文（1 轮 = user + assistant 2 条消息）。
- 超出 3 轮后，若 **5 分钟**内再次对话：最早轮次交给 LLM 压缩为摘要，摘要注入 system_prompt，原文保留最近 3 轮。
- 超过 **5 分钟**未对话：抛弃全部记忆（含摘要），下次 ask 视为新会话。
- 记忆只存内存，B 端重启即清空。

## 8. 安全

- 握手必须携带有效 `token`，无效立即断开（close code 4001）。
- 同一 `device_id` 重复接入时，B 端关闭旧连接（close code 4000）。
- 传输层建议 wss/TLS。
- C 端为纯终端：只采集语音与播报回复，不做任何 AI 判断。
- token 在 C 端使用 EncryptedSharedPreferences 存储。
