# SPDX-License-Identifier: AGPL-3.0-only
"""ASR 语音识别（faster-whisper，懒加载）。

首次识别时初始化模型并自动从 HuggingFace 下载（可配置模型大小），
之后复用实例。faster-whisper 未安装时 asr 功能不可用，voice_ask 返回
asr_unavailable 错误码（服务端不崩）。

模型选择：tiny（~75MB，快）/ base（~145MB，均衡）/ small（~484MB，准）。
"""

import asyncio
import io
import logging

logger = logging.getLogger("fairy_voice.asr")


class WhisperASR:
    """faster-whisper 封装：懒加载 + 异步识别（CPU 推理放线程池）。"""

    def __init__(
        self,
        model: str = "tiny",
        device: str = "auto",
        compute_type: str = "auto",
    ):
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._init_lock = asyncio.Lock()

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "faster-whisper 未安装，请先 pip install faster-whisper"
            ) from e
        logger.info(f"加载 Whisper 模型 {self.model_name} ...")
        self._model = WhisperModel(
            self.model_name, device=self.device, compute_type=self.compute_type
        )
        logger.info(f"Whisper 模型 {self.model_name} 就绪")
        return self._model

    async def recognize(self, wav_bytes: bytes, lang: str = "zh-CN") -> str:
        """识别 16kHz 单声道 WAV 字节，返回文本。首次调用会下载/加载模型。"""
        async with self._init_lock:
            model = await asyncio.to_thread(self._ensure_model)
            lang_code = lang.split("-")[0] if lang else "zh"
            segments, _info = await asyncio.to_thread(
                lambda: model.transcribe(
                    io.BytesIO(wav_bytes),
                    language=lang_code,
                    vad_filter=True,
                )
            )
            texts = [seg.text.strip() for seg in segments]
            return "".join(texts).strip()
