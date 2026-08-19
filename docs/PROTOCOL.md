# Fairy Voice 通信协议 v2.0（流式）

> 双端共享协议。`astrbot_plugin_fairy_voice`（B 端插件）与 `fairy-voice-app`（C 端 App）必须保持同步。
> v2.0（2026-08-17）：在 v1.1.0 基础上新增**流式响应**（stream_begin / stream_delta / stream_end），
> ask 与 voice_ask 默认走流式；无 TTS。v1.1.0 的单帧 response 仅作为 B 端非流式兜底保留。

## 1. 概述

- **传输**：WebSocket（生产环境建议 wss/TLS）。
- **方向**：C 端（手机）**主动外连** B 端（AstrBot 插件服务端，穿透家庭 NAT），B 永不主动连 C。
- **帧格式**：UTF-8 JSON 文本帧。
- **角色**：C = 语音采集与本地识别终端（录音 + sherpa-onnx ASR + 文本展示），B = AI 大脑（LLM 流式生成 + 记忆管理 + 工具调用）。
- **协议风格**：JSON-RPC 风格，请求/响应以 `id` 关联；流式响应以 `stream_begin` 开始、`stream_end` 结束。

## 2. 连接握手

C 连接成功后，必须首先发送 `hello`：

```json
{"type": "hello", "token": "<auth_token>", "device_id": "my-phone", "client_version": "0.2.0"}
```

B 响应：

```json
{"type": "hello_ack", "ok": true, "session_id": "<uuid>", "server_version": "0.2.0"}
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
- C 若超过 `heartbeat_interval * 3` 未收到 B 任何帧，主动断开并重连。流式期间 B 持续下发 delta，天然满足。

## 4. 语音指令请求（C → B）

### 4.1 文本指令 ask

```json
{"type": "ask", "id": "<uuid>", "text": "帮我查一下明天的天气", "lang": "zh-CN"}
```

### 4.2 语音指令 voice_ask（已弃用）

> **已弃用**：ASR 已移至 C 端（sherpa-onnx 本地识别），C 端应直接发 ask 文本帧。
> B 端收到 voice_ask 会返回 `asr_unavailable` 错误码。

```json
{"type": "voice_ask", "id": "<uuid>", "audio": "<WAV 文件 base64>", "lang": "zh-CN"}
```

- `audio`：16kHz / 16bit / 单声道 WAV 录音的 base64，必填，非空。
- B 端流程：~~ASR 识别 → 走与 ask 相同的记忆/LLM 链路 → 流式响应，`stream_begin` 携带 `recognized`~~。
- B 端返回 `asr_unavailable` 错误码，提示 C 端使用本地 ASR 后发 ask。

## 5. 流式响应（B → C，v2.0）

B 收到 ask 后，按序发送三类帧，同一 `id` 关联：

```json
{"type": "stream_begin", "id": "<uuid>", "recognized": null}
{"type": "stream_delta", "id": "<uuid>", "delta": "增量文本片段"}
...
{"type": "stream_end", "id": "<uuid>", "ok": true, "data": {"text": "完整回复文本"}}
```

- `stream_begin`：流开始，标志该 id 进入流式状态。**必须先于任何 delta**。
- `recognized`：始终为 `null`（ASR 已移至 C 端，B 端不再做识别）。
- `stream_delta`：增量文本，C 端直接追加展示；`delta` 非空。
- `stream_end`：流结束。
  - `ok: true`：正常完成，`data.text` 为完整回复（C 端以此为准落历史/记忆，可修复丢帧）。
  - `ok: false`：流中途失败，`error` 结构与 v1.1.0 response 错误一致（见 §6）。
- **错误语义**：
  - 请求校验失败（空文本）：**未发 stream_begin**，直接回 v1.1.0 单帧 response（ok=false）。
  - voice_ask 已弃用：直接回 `asr_unavailable` 错误码。
  - 生成中途失败（LLM 错误/超时）：已发 stream_begin，回 `stream_end` 且 `ok:false` + `error`。
- 超时：流式生成同样受 `ask_timeout` 约束，C 端超时未收到 stream_end 可视为失败并断开重连。

## 6. 错误码

| code | 含义 |
|---|---|
| `invalid_token` | 认证 token 错误 |
| `missing_device_id` | 缺少设备标识 |
| `empty_text` | ask 的 text 为空 |
| `empty_audio` | ~~voice_ask 的 audio 为空~~（已弃用） |
| `bad_audio` | ~~voice_ask 音频 base64 解码失败~~（已弃用） |
| `asr_unavailable` | ASR 已移至 C 端，voice_ask 已弃用 |
| `busy` | 设备已有 ask 在处理 |
| `provider_not_found` | 未配置可用 LLM Provider |
| `llm_error` | LLM 生成失败（含流式中途失败） |
| `internal_error` | 内部异常 |

## 7. 记忆策略（B 端，按 device_id）

- 仅保留最近 **3 轮**对话原文（1 轮 = user + assistant 2 条消息）。
- 超出 3 轮后，若 **5 分钟**内再次对话：最早轮次交给 LLM 压缩为摘要，摘要注入 system_prompt，原文保留最近 3 轮。
- 超过 **5 分钟**未对话：抛弃全部记忆（含摘要），下次 ask 视为新会话。
- 记忆只存内存，B 端重启即清空。
- **流式适配**：assistant 原文以 `stream_end` 的完整文本写入，不采用 delta 拼接。

## 8. 安全

- 握手必须携带有效 `token`，无效立即断开（close code 4001）。
- 同一 `device_id` 重复接入时，B 端关闭旧连接（close code 4000）。
- 传输层建议 wss/TLS。
- C 端为纯终端：只采集语音与展示回复，不做任何 AI 判断。
- token 在 C 端使用 EncryptedSharedPreferences 存储。

## 9. 变更记录

- v2.0（2026-08-17）：新增流式响应三类帧；client_version / server_version 升 0.2.0；无 TTS。
- v1.1.0：新增 voice_ask 与 response.recognized 字段。
- v1.0.0：hello 握手、心跳、ask/response 单帧模型。
