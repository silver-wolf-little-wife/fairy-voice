# SPDX-License-Identifier: AGPL-3.0-only
"""ws_server.py 端到端冒烟测试（协议 v2.0 流式）：hello 握手 / ping / ask / 流式全流程 / voice_ask 弃用。"""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402

from ws_server import FairyWsServer  # noqa: E402

PORT = 8877
TOKEN = "test-token-123"


async def fake_ask_handler(device_id: str, text: str) -> str:
    return f"echo[{device_id}]: {text}"


async def fake_ask_stream_handler(device_id: str, text: str):
    # 模拟流式：分段 yield 增量
    for part in ["你好", "，", "世界", "！"]:
        yield part


async def _boom(device_id: str, text: str) -> str:
    raise RuntimeError("boom")


async def _boom_stream(device_id: str, text: str):
    yield "前半段"
    raise RuntimeError("boom")


async def _recv_stream(ws, expect_begin_recognized=None) -> dict:
    """收取一轮完整流式响应，返回 {recognized, deltas, end}。"""
    first = await ws.receive_json()
    assert first["type"] == "stream_begin", first
    if expect_begin_recognized is not None:
        assert first.get("recognized") == expect_begin_recognized, first
    deltas = []
    while True:
        frame = await ws.receive_json()
        if frame["type"] == "stream_delta":
            deltas.append(frame["delta"])
            continue
        assert frame["type"] == "stream_end", frame
        return {"recognized": first.get("recognized"), "deltas": deltas, "end": frame}


async def main() -> None:
    server = FairyWsServer(
        port=PORT,
        token=TOKEN,
        heartbeat_timeout=60,
        ask_handler=fake_ask_handler,
        ask_stream_handler=fake_ask_stream_handler,
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as sess:
            # 1) 错误 token：hello_ack ok:false 且断开
            async with sess.ws_connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
                await ws.send_json({"type": "hello", "token": "wrong", "device_id": "phone-1"})
                ack = await ws.receive_json()
                assert ack["ok"] is False and ack["error"] == "invalid_token", ack
                print("PASS 错误 token 被拒绝")

            # 2) 正确握手 + 全流程
            async with sess.ws_connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
                await ws.send_json(
                    {"type": "hello", "token": TOKEN, "device_id": "phone-1", "client_version": "0.2.0"}
                )
                ack = await ws.receive_json()
                assert ack["ok"] is True and ack.get("session_id"), ack
                assert ack.get("server_version") == "0.2.0", ack
                print("PASS hello 握手成功（server_version 0.2.0）")

                # 3) 心跳
                await ws.send_json({"type": "ping"})
                pong = await ws.receive_json()
                assert pong == {"type": "pong"}, pong
                print("PASS ping/pong")

                # 4) ask 流式：begin → delta×4 → end(ok, text 拼接完整)
                req_id = str(uuid.uuid4())
                await ws.send_json({"type": "ask", "id": req_id, "text": "你好"})
                got = await _recv_stream(ws)
                assert got["deltas"] == ["你好", "，", "世界", "！"], got
                end = got["end"]
                assert end["ok"] is True and end["data"]["text"] == "你好，世界！", end
                assert end["id"] == req_id, end
                print("PASS ask 流式（begin/delta×4/end，完整文本正确）")

                # 5) 空文本：未发 begin，直接 response 错误帧
                await ws.send_json({"type": "ask", "id": str(uuid.uuid4()), "text": "   "})
                resp = await ws.receive_json()
                assert resp["type"] == "response" and resp["ok"] is False, resp
                assert resp["error"]["code"] == "empty_text", resp
                print("PASS 空文本报错（单帧 response）")

                # 6) 流中异常：已发 begin → stream_end(ok:false, code llm_error)
                server.ask_stream_handler = _boom_stream
                await ws.send_json({"type": "ask", "id": str(uuid.uuid4()), "text": "boom"})
                got = await _recv_stream(ws)
                assert got["deltas"] == ["前半段"], got
                assert got["end"]["ok"] is False, got["end"]
                assert got["end"]["error"]["code"] == "llm_error", got["end"]
                print("PASS 流中异常 → stream_end(ok:false, llm_error)")
                server.ask_stream_handler = fake_ask_stream_handler

                # 7) voice_ask 已弃用：返回 asr_unavailable 错误码
                await ws.send_json(
                    {"type": "voice_ask", "id": str(uuid.uuid4()), "audio": "dGVzdA==", "lang": "zh-CN"}
                )
                resp = await ws.receive_json()
                assert resp["type"] == "response" and resp["ok"] is False, resp
                assert resp["error"]["code"] == "asr_unavailable", resp
                print("PASS voice_ask 已弃用 → asr_unavailable")

                # 8) 连接存活时设备列表应包含本机
                devs = server.device_summary()
                assert any(d["device_id"] == "phone-1" for d in devs), devs
                print("PASS device_summary")
    finally:
        await server.stop()
    print("冒烟测试全部通过")


async def legacy_main() -> None:
    """非流式回退：仅 ask_handler（无 ask_stream_handler）时仍回单帧 response。"""
    server = FairyWsServer(
        port=PORT + 1,
        token=TOKEN,
        heartbeat_timeout=60,
        ask_handler=fake_ask_handler,
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"ws://127.0.0.1:{PORT + 1}/ws") as ws:
                await ws.send_json(
                    {"type": "hello", "token": TOKEN, "device_id": "phone-legacy", "client_version": "0.1.0"}
                )
                ack = await ws.receive_json()
                assert ack["ok"] is True, ack
                await ws.send_json({"type": "ask", "id": str(uuid.uuid4()), "text": "你好"})
                resp = await ws.receive_json()
                assert resp["type"] == "response" and resp["ok"] is True, resp
                assert resp["data"]["text"] == "echo[phone-legacy]: 你好", resp
                print("PASS 非流式回退：ask_handler 单帧 response")
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(legacy_main())
    print("全部冒烟测试通过（含非流式回退）")
