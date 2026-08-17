# SPDX-License-Identifier: AGPL-3.0-only
"""流式文本增量切割（纯函数，无 AstrBot 依赖，便于单测）。

上游 Provider.text_chat_stream 的 chunk 语义不统一：
- OpenAI 系：每个 chunk 是增量片段（delta.content），流末尾再回一次完整快照；
- 部分 Provider：chunk 直接是累计文本快照。

split_delta 兼容两种语义：增量片段直接追加，累计快照取增量部分，重复快照跳过。
"""


def split_delta(full: str, t: str) -> tuple[str, str]:
    """计算下一个增量片段。

    Args:
        full: 已累计的完整文本。
        t: 当前 chunk 的 completion_text（增量片段或累计快照）。

    Returns:
        (delta, new_full)：delta 为要下发的增量（可为空串），new_full 为更新后的累计文本。
    """
    if not t:
        return "", full
    if t.startswith(full) and len(t) > len(full):
        # 累计快照：只取新增部分
        return t[len(full):], t
    if t == full:
        # 重复快照：跳过
        return "", full
    # 增量片段：直接追加
    return t, full + t
