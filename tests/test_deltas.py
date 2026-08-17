# SPDX-License-Identifier: AGPL-3.0-only
"""deltas.split_delta 纯函数单测：增量片段 / 累计快照 / 重复快照 / 空 chunk。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deltas import split_delta  # noqa: E402


def test_incremental_chunks():
    """增量片段：直接追加。"""
    full = ""
    for piece, expect in [("你", "你"), ("好", "好"), (" ", " "), ("世界", "世界")]:
        delta, full = split_delta(full, piece)
        assert delta == expect, (piece, delta)
    assert full == "你好 世界"


def test_cumulative_snapshots():
    """累计快照：只取增量部分。"""
    full = ""
    delta, full = split_delta(full, "你好")
    assert delta == "你好" and full == "你好"
    delta, full = split_delta(full, "你好世界")
    assert delta == "世界" and full == "你好世界"
    delta, full = split_delta(full, "你好世界！")
    assert delta == "！" and full == "你好世界！"


def test_repeated_snapshot_skipped():
    """重复快照：跳过，不产生 delta。"""
    full = "你好"
    delta, full = split_delta(full, "你好")
    assert delta == "" and full == "你好"


def test_empty_chunk():
    """空 chunk：安全跳过。"""
    full = "你好"
    delta, full = split_delta(full, "")
    assert delta == "" and full == "你好"


def test_mixed_sequence():
    """增量片段 + 末尾快照混合（OpenAI 流式真实形态）。"""
    full = ""
    for piece in ["今", "天", "天", "气"]:
        delta, full = split_delta(full, piece)
        assert delta == piece
    # 流末尾完整快照
    delta, full = split_delta(full, "今天天气")
    assert delta == "" and full == "今天天气"


if __name__ == "__main__":
    test_incremental_chunks()
    test_cumulative_snapshots()
    test_repeated_snapshot_skipped()
    test_empty_chunk()
    test_mixed_sequence()
    print("deltas 单测全部通过")
