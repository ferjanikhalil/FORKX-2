import io
import wave
from typing import AsyncGenerator

from loguru import logger
import openai
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts


class PunicaTTSService(TTSService):
    """Punica AI Text-to-Speech service for Tunisian Derja and regional voices.

    Synthesizes speech via Punica's OpenAI-compatible /audio/speech endpoint
    using response_format='wav', then decodes raw PCM audio frames at 24kHz.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://punica.tn/api/v1",
        model: str = "dido",
        voice: str = "dido",
        sample_rate: int = 24000,
        text_filters: list | None = None,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            text_filters=text_filters,
            **kwargs,
        )
        self._model = model or "dido"
        self._voice = voice or "dido"
        self._sample_rate = sample_rate
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        if not text or not text.strip():
            return

        try:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="wav",
            )
            raw_wav = await response.aread()

            with wave.open(io.BytesIO(raw_wav), "rb") as w:
                pcm_data = w.readframes(w.getnframes())
                sr = w.getframerate()
                channels = w.getnchannels()

            await self.start_tts_usage_metrics(text)
            await self.stop_ttfb_metrics()

            # 4800 bytes = 2400 samples = 100ms at 24kHz 16-bit mono
            chunk_size = 4800
            for i in range(0, len(pcm_data), chunk_size):
                chunk = pcm_data[i : i + chunk_size]
                if len(chunk) > 0:
                    yield TTSAudioRawFrame(
                        audio=chunk,
                        sample_rate=sr,
                        num_channels=channels,
                        context_id=context_id,
                    )
        except Exception as e:
            logger.error(f"Punica TTS error: {e}")
            yield ErrorFrame(error=f"Punica TTS error: {e}")
