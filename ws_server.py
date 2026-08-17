# SPDX-License-Identifier: AGPL-3.0-only
"""B 端 WebSocket 服务端（仅依赖 aiohttp，独立于 AstrBot，便于测试）。

协议见 docs/PROTOCOL.md（v2.0 流式）：hello 握手 / 心跳 / ask / voice_ask / 流式响应。
- ask（C→B）：{"type":"ask","id":...,"text":"...","lang":"zh-CN"}
- voice_ask（C→B）：{"type":"voice_ask","id":...,"audio":"<base64 WAV>","lang":"zh-CN"}
  B 端 ASR 识别后走同一 ask 链路（记忆/LLM），stream_begin 携带 recognized。
- 流式响应（B→C，v2.0）：
  stream_begin → stream_delta × N → stream_end
  - {"type":"stream_begin","id":...,"recognized":...}
  - {"type":"stream_delta","id":...,"delta":"..."}
  - {"type":"stream_end","id":...,"ok":true,"data":{"text":"完整回复"}}
  请求校验失败（未发 begin）回 v1.1.0 单帧 response(ok=false)；
  流中途失败回 stream_end(ok=false)+error。
- 非流式 ask_handler 仍受支持（单帧 response 兜底）。

与 Cherry Remote 相反：本服务端不主动下发指令，只接收语音指令并流式回传 AI 回复。
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable

from aiohttp import WSMsgType, web

logger = logging.getLogger("fairy_voice.ws_server")

SERVER_VERSION = "0.2.0"


class FairyWsServer:
    """B 端 WebSocket 服务端：接受并管理手机 App 的长连接。"""

    def __init__(
        self,
        port: int,
        token: str,
        heartbeat_timeout: int = 60,
        ask_handler=None,
        ask_stream_handler=None,
        asr_fn=None,
    ):
        """
        Args:
            port: 监听端口。
            token: 握手认证 token。
            heartbeat_timeout: 心跳超时秒数。
            ask_handler: async (device_id: str, text: str) -> str，返回 AI 回复文本（非流式兜底）。
            ask_stream_handler: async (device_id: str, text: str) -> AsyncGenerator[str, None]，
                逐增量 yield AI 回复文本；存在时优先于 ask_handler。
            asr_fn: async (wav_bytes: bytes, lang: str) -> str，语音识别，返回文本。
        """
        self.port = port
        self.token = token
        self.heartbeat_timeout = heartbeat_timeout
        self.ask_handler = ask_handler
        self.ask_stream_handler = ask_stream_handler
        self.asr_fn = asr_fn
        self.devices: dict[str, dict] = {}  # device_id -> {ws, session_id, last_seen}
        self._locks: dict[str, asyncio.Lock] = {}  # device_id -> 串行锁
        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_ws)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._hb_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="0.0.0.0", port=self.port)
        await self._site.start()
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Fairy Voice WebSocket 服务已启动 ws://0.0.0.0:{self.port}/ws")

    async def stop(self) -> None:
        if self._hb_task:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self.devices.clear()
        self._locks.clear()

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        device_id: str | None = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if device_id and device_id in self.devices:
                    self.devices[device_id]["last_seen"] = time.monotonic()
                mtype = data.get("type")
                if mtype == "hello":
                    device_id = await self._handle_hello(ws, data)
                    if not device_id:
                        break  # 认证失败，已关闭
                elif mtype == "ping":
                    await ws.send_json({"type": "pong"})
                elif mtype == "ask":
                    if device_id:
                        await self._handle_ask(ws, device_id, data)
                elif mtype == "voice_ask":
                    if device_id:
                        await self._handle_voice_ask(ws, device_id, data)
        finally:
            if device_id and self.devices.get(device_id, {}).get("ws") is ws:
                self.devices.pop(device_id, None)
                self._locks.pop(device_id, None)
                logger.info(f"设备 {device_id} 已断开，当前在线 {len(self.devices)} 台")
        return ws

    async def _handle_hello(self, ws: web.WebSocketResponse, data: dict) -> str | None:
        token = data.get("token", "")
        device_id = data.get("device_id", "")
        if token != self.token:
            await ws.send_json({"type": "hello_ack", "ok": False, "error": "invalid_token"})
            await ws.close(code=4001, message=b"invalid token")
            return None
        if not device_id:
            await ws.send_json({"type": "hello_ack", "ok": False, "error": "missing_device_id"})
            await ws.close(code=4002, message=b"missing device_id")
            return None
        # 同一 device_id 重复接入：关闭旧连接，避免双实例路由混乱
        old = self.devices.get(device_id)
        if old and old.get("ws") is not ws:
            try:
                await old["ws"].close(code=4000, message=b"device reconnected")
            except Exception:
                pass
            logger.info(f"设备 {device_id} 旧连接已关闭（重复接入）")
        session_id = str(uuid.uuid4())
        self.devices[device_id] = {
            "ws": ws,
            "session_id": session_id,
            "last_seen": time.monotonic(),
        }
        await ws.send_json(
            {
                "type": "hello_ack",
                "ok": True,
                "session_id": session_id,
                "server_version": SERVER_VERSION,
            }
        )
        logger.info(f"设备接入: {device_id}（session={session_id[:8]}），在线 {len(self.devices)} 台")
        return device_id

    async def _handle_ask(self, ws: web.WebSocketResponse, device_id: str, data: dict) -> None:
        """处理语音指令：串行调用 AI，优先流式推送（v2.0）。"""
        text = str(data.get("text") or "").strip()
        req_id = data.get("id") or str(uuid.uuid4())
        if not text:
            await self._send_error(ws, req_id, "empty_text", "指令文本为空")
            return
        if self.ask_stream_handler is None and self.ask_handler is None:
            await self._send_error(ws, req_id, "internal_error", "插件未初始化")
            return
        lock = self._locks.setdefault(device_id, asyncio.Lock())
        try:
            # 同一设备串行处理，保证记忆会话无竞态
            async with lock:
                if self.ask_stream_handler is not None:
                    await self._stream_ask(ws, req_id, device_id, text, recognized=None)
                else:
                    reply = await self.ask_handler(device_id, text)
                    await ws.send_json(
                        {
                            "type": "response",
                            "id": req_id,
                            "ok": True,
                            "data": {"text": reply},
                            "error": None,
                        }
                    )
        except asyncio.TimeoutError:
            await self._send_error(ws, req_id, "llm_error", "AI 处理超时")
        except Exception as e:  # noqa: BLE001 —— 错误码回传，不中断连接
            logger.exception(f"ask 处理失败 device={device_id}")
            await self._send_error(ws, req_id, "llm_error", str(e))

    async def _handle_voice_ask(
        self, ws: web.WebSocketResponse, device_id: str, data: dict
    ) -> None:
        """语音指令：ASR 识别 → 同一 ask 链路 → 流式回传（stream_begin 携带 recognized）。
        在同一设备串行锁内完成识别与 LLM 调用，保证记忆会话无竞态；
        失败按错误码回传，不中断连接。
        """
        audio_b64 = data.get("audio") or ""
        req_id = data.get("id") or str(uuid.uuid4())
        lang = data.get("lang") or "zh-CN"
        if not audio_b64:
            await self._send_error(ws, req_id, "empty_audio", "音频为空")
            return
        if self.asr_fn is None:
            await self._send_error(ws, req_id, "asr_unavailable", "语音识别未初始化")
            return
        if self.ask_stream_handler is None and self.ask_handler is None:
            await self._send_error(ws, req_id, "internal_error", "插件未初始化")
            return
        try:
            audio = base64.b64decode(audio_b64)
        except Exception:
            await self._send_error(ws, req_id, "bad_audio", "音频解码失败")
            return
        lock = self._locks.setdefault(device_id, asyncio.Lock())
        try:
            async with lock:
                recognized = await self.asr_fn(audio, lang)
                text = (recognized or "").strip()
                if not text:
                    await self._send_error(ws, req_id, "empty_text", "未能识别到语音内容")
                    return
                if self.ask_stream_handler is not None:
                    await self._stream_ask(ws, req_id, device_id, text, recognized=text)
                else:
                    reply = await self.ask_handler(device_id, text)
                    await ws.send_json(
                        {
                            "type": "response",
                            "id": req_id,
                            "ok": True,
                            "data": {"text": reply, "recognized": text},
                            "error": None,
                        }
                    )
        except asyncio.TimeoutError:
            await self._send_error(ws, req_id, "llm_error", "AI 处理超时")
        except Exception as e:  # noqa: BLE001 —— 错误码回传，不中断连接
            logger.exception(f"voice_ask 处理失败 device={device_id}")
            await self._send_error(ws, req_id, "llm_error", str(e))

    async def _stream_ask(
        self,
        ws: web.WebSocketResponse,
        req_id: str,
        device_id: str,
        text: str,
        recognized: str | None,
    ) -> None:
        """v2.0 流式推送：stream_begin → stream_delta × N → stream_end。

        生成中途失败（LLM 错误/超时）回 stream_end(ok=false)+error；
        send 本身失败（连接断开）向上抛出，由调用方兜底。
        """
        await ws.send_json(
            {"type": "stream_begin", "id": req_id, "recognized": recognized}
        )
        full = ""
        try:
            async for delta in self.ask_stream_handler(device_id, text):
                if delta:
                    full += delta
                    await ws.send_json(
                        {"type": "stream_delta", "id": req_id, "delta": delta}
                    )
        except asyncio.TimeoutError:
            await ws.send_json(
                {
                    "type": "stream_end",
                    "id": req_id,
                    "ok": False,
                    "error": {"code": "llm_error", "message": "AI 处理超时"},
                }
            )
            return
        except Exception as e:  # noqa: BLE001 —— 错误码回传，不中断连接
            logger.exception(f"流式生成失败 device={device_id}")
            await ws.send_json(
                {
                    "type": "stream_end",
                    "id": req_id,
                    "ok": False,
                    "error": {"code": "llm_error", "message": str(e)},
                }
            )
            return
        await ws.send_json(
            {
                "type": "stream_end",
                "id": req_id,
                "ok": True,
                "data": {"text": full},
            }
        )

    async def _send_error(self, ws: web.WebSocketResponse, req_id: str, code: str, message: str) -> None:
        await ws.send_json(
            {
                "type": "response",
                "id": req_id,
                "ok": False,
                "data": None,
                "error": {"code": code, "message": message},
            }
        )

    def device_summary(self) -> list[dict]:
        return [
            {"device_id": did, "session_id": info["session_id"]}
            for did, info in self.devices.items()
        ]

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_timeout)
            now = time.monotonic()
            for did in list(self.devices.keys()):
                info = self.devices.get(did)
                if not info:
                    continue
                if now - info["last_seen"] > self.heartbeat_timeout:
                    logger.info(f"设备 {did} 心跳超时，清理下线")
                    self.devices.pop(did, None)
                    self._locks.pop(did, None)
                    try:
                        await info["ws"].close()
                    except Exception:
                        pass
