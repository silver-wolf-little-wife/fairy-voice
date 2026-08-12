# SPDX-License-Identifier: AGPL-3.0-only
"""ws_server.py 端到端冒烟测试：hello 握手 / ping / ask 全流程。"""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot_plugin_fairy_voice"))

import aiohttp  # noqa: E402

from ws_server import FairyWsServer  # noqa: E402

PORT = 8877
TOKEN = "test-token-123"


async def fake_ask_handler(device_id: str, text: str) -> str:
    return f"echo[{device_id}]: {text}"


async def _boom(device_id: str, text: str) -> str:
    raise RuntimeError("boom")


async def main() -> None:
    server = FairyWsServer(port=PORT, token=TOKEN, heartbeat_timeout=60, ask_handler=fake_ask_handler)
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
                    {"type": "hello", "token": TOKEN, "device_id": "phone-1", "client_version": "0.1.0"}
                )
                ack = await ws.receive_json()
                assert ack["ok"] is True and ack.get("session_id"), ack
                print("PASS hello 握手成功")

                # 3) 心跳
                await ws.send_json({"type": "ping"})
                pong = await ws.receive_json()
                assert pong == {"type": "pong"}, pong
                print("PASS ping/pong")

                # 4) ask 正常流程
                req_id = str(uuid.uuid4())
                await ws.send_json({"type": "ask", "id": req_id, "text": "你好"})
                resp = await ws.receive_json()
                assert resp["ok"] is True and resp["data"]["text"] == "echo[phone-1]: 你好", resp
                print("PASS ask → AI 回复")

                # 5) 空文本
                await ws.send_json({"type": "ask", "id": str(uuid.uuid4()), "text": "   "})
                resp = await ws.receive_json()
                assert resp["ok"] is False and resp["error"]["code"] == "empty_text", resp
                print("PASS 空文本报错")

                # 6) handler 抛异常 → llm_error
                server.ask_handler = _boom
                await ws.send_json({"type": "ask", "id": str(uuid.uuid4()), "text": "boom"})
                resp = await ws.receive_json()
                assert resp["ok"] is False and resp["error"]["code"] == "llm_error", resp
                print("PASS handler 异常 → llm_error")
                server.ask_handler = fake_ask_handler

                # 7) 连接存活时设备列表应包含本机
                devs = server.device_summary()
                assert any(d["device_id"] == "phone-1" for d in devs), devs
                print("PASS device_summary")
    finally:
        await server.stop()
    print("冒烟测试全部通过")


if __name__ == "__main__":
    asyncio.run(main())
