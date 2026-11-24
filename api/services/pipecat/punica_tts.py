import array
import asyncio
import hashlib
import io
import json
import math
import os
import re
import time
import wave
from pathlib import Path
from typing import AsyncGenerator

import openai
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

_SAMPLE_RATE = 24000
_CHUNK_BYTES = 4800  # 2400 samples = 100ms at 24kHz 16-bit mono
_STITCH_GAP_BYTES = b"\x00\x00" * int(_SAMPLE_RATE * 0.065)  # 65ms controlled breath gap

_TAG_PATTERN = re.compile(r"(\[?(?:BOX|BRIDGE)_[A-Za-z0-9_]+\]?)", re.IGNORECASE)
_TAG_ONLY_PATTERN = re.compile(r"^\[?((?:BOX|BRIDGE)_[A-Za-z0-9_]+)\]?$", re.IGNORECASE)
_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_PUNCT_RE = re.compile(r"[،؛؟?!.:,\-\"'«»()\[\]{}…]")

_RECORDS_LOADED = False
_RECORDS_BY_ID_UPPER: dict[str, tuple[str, bytes]] = {}
_RECORDS_BY_NORM_TEXT: dict[str, tuple[str, bytes]] = {}
_STEMS_BY_TOKENS: list[tuple[list[str], str, bytes]] = []

_INFLIGHT_SYNTHESIS: dict[str, asyncio.Task[bytes]] = {}


def _resolve_records_dir() -> Path:
    docker_path = Path("/app/records")
    if (docker_path / "manifest.json").exists():
        return docker_path
    return Path(__file__).resolve().parents[3] / "records"


def _resolve_cache_dir() -> Path:
    if Path("/app").exists() and os.access("/app", os.W_OK):
        cache_dir = Path("/app/dograh_pcm_cache")
    else:
        cache_dir = Path(__file__).resolve().parents[3] / "dograh_pcm_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _normalize_text(text: str) -> str:
    s = _DIACRITICS_RE.sub("", text)
    s = re.sub(r"[إأآا]", "ا", s)
    s = _PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _ensure_records_loaded() -> None:
    """Load pre-recorded 24kHz 16-bit mono PCM files from records/ into RAM once."""
    global _RECORDS_LOADED
    if _RECORDS_LOADED:
        return
    _RECORDS_LOADED = True

    records_dir = _resolve_records_dir()
    manifest_path = records_dir / "manifest.json"
    if not manifest_path.exists():
        logger.warning(f"[PunicaTTS] manifest.json not found at {manifest_path}; running live-only mode")
        return

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stems_tmp: list[tuple[list[str], str, bytes]] = []
        for entry in manifest:
            clip_id = entry["id"]
            wav_path = records_dir / entry["filename"]
            if not wav_path.exists():
                logger.warning(f"[PunicaTTS] Missing static clip {wav_path}")
                continue

            with wave.open(str(wav_path), "rb") as w:
                pcm = w.readframes(w.getnframes())

            _RECORDS_BY_ID_UPPER[clip_id.upper()] = (clip_id, pcm)
            norm = _normalize_text(entry["text"])
            if norm:
                _RECORDS_BY_NORM_TEXT[norm] = (clip_id, pcm)
                if entry.get("type") == "STATIC STEM":
                    tokens = norm.split()
                    if tokens:
                        stems_tmp.append((tokens, clip_id, pcm))

        stems_tmp.sort(key=lambda item: len(item[0]), reverse=True)
        _STEMS_BY_TOKENS.extend(stems_tmp)
        logger.info(
            f"[PunicaTTS] Loaded {len(_RECORDS_BY_ID_UPPER)} static audio clips "
            f"({len(_STEMS_BY_TOKENS)} stems) from {records_dir}"
        )
    except Exception as e:
        logger.error(f"[PunicaTTS] Failed loading static records from {records_dir}: {e}")


def _trim_and_fade_pcm(
    pcm_bytes: bytes,
    sample_rate: int = _SAMPLE_RATE,
    threshold_dbfs: float = -40.0,
    pad_start_ms: int = 10,
    pad_end_ms: int = 45,
) -> bytes:
    """Strip Punica trailing/leading silence on live TTS audio and taper edges cleanly."""
    if not pcm_bytes or len(pcm_bytes) < 64:
        return pcm_bytes

    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    n = len(samples)
    win = max(1, int(sample_rate * 0.010))
    nw = (n + win - 1) // win

    first_active = None
    last_active = None
    for i in range(nw):
        seg = samples[i * win : (i + 1) * win]
        if not seg:
            continue
        rms = math.sqrt(sum(s * s for s in seg) / len(seg))
        db = -96.0 if rms <= 1e-9 else 20.0 * math.log10(rms / 32768.0)
        if db >= threshold_dbfs:
            if first_active is None:
                first_active = i
            last_active = i

    if first_active is None or last_active is None:
        return pcm_bytes

    start_idx = max(0, first_active * win - int(sample_rate * (pad_start_ms / 1000.0)))
    end_idx = min(n, (last_active + 1) * win + int(sample_rate * (pad_end_ms / 1000.0)))
    trimmed = samples[start_idx:end_idx]
    m = len(trimmed)
    if m == 0:
        return pcm_bytes

    fi = min(m // 4, int(sample_rate * 0.010))
    fo = min(m // 4, int(sample_rate * 0.015))
    for i in range(fi):
        g = 0.5 * (1.0 - math.cos(math.pi * i / max(1, fi)))
        trimmed[i] = int(round(trimmed[i] * g))
    for i in range(fo):
        idx = m - 1 - i
        g = 0.5 * (1.0 - math.cos(math.pi * i / max(1, fo)))
        trimmed[idx] = int(round(trimmed[idx] * g))

    return trimmed.tobytes()


def _match_stem_prefix(raw_text: str) -> tuple[str, bytes, str] | None:
    """If raw_text starts with a pre-recorded STATIC STEM, return (stem_id, pcm, live_tail)."""
    raw_tokens = raw_text.strip().split()
    norm_tokens = [_normalize_text(t) for t in raw_tokens]
    # Keep index mapping so we slice original raw_tokens cleanly without losing punctuation on tail
    non_empty_indices = [i for i, nt in enumerate(norm_tokens) if nt]
    flat_norm: list[tuple[str, int]] = []
    for raw_idx in non_empty_indices:
        for sub in norm_tokens[raw_idx].split():
            flat_norm.append((sub, raw_idx))

    if not flat_norm:
        return None

    flat_words = [w for w, _ in flat_norm]
    for stem_words, stem_id, stem_pcm in _STEMS_BY_TOKENS:
        k = len(stem_words)
        if len(flat_words) >= k and flat_words[:k] == stem_words:
            if len(flat_words) == k:
                return stem_id, stem_pcm, ""
            next_raw_idx = flat_norm[k][1]
            tail = " ".join(raw_tokens[next_raw_idx:]).strip()
            return stem_id, stem_pcm, tail
    return None


def _parse_segments(text: str) -> list[tuple[str, str, bytes | None]]:
    """Split utterance into ordered ('static', box_id, pcm) and ('live', text, None) segments."""
    _ensure_records_loaded()
    segments: list[tuple[str, str, bytes | None]] = []

    for part in _TAG_PATTERN.split(text):
        if not part or not part.strip():
            continue

        tag_match = _TAG_ONLY_PATTERN.match(part.strip())
        if tag_match:
            key = tag_match.group(1).upper()
            hit = _RECORDS_BY_ID_UPPER.get(key)
            if hit:
                clip_id, pcm = hit
                segments.append(("static", clip_id, pcm))
                continue

        # Clean stray leading/trailing punctuation left around inline [BOX_...] tags
        cleaned = re.sub(r"^[،؛؟?!.\s:\-]+|[،؛.\s:\-]+$", "", part).strip()
        if not cleaned or not _normalize_text(cleaned):
            continue

        norm = _normalize_text(cleaned)
        if norm in _RECORDS_BY_NORM_TEXT:
            clip_id, pcm = _RECORDS_BY_NORM_TEXT[norm]
            segments.append(("static", clip_id, pcm))
            continue

        stem_hit = _match_stem_prefix(cleaned)
        if stem_hit is not None:
            stem_id, stem_pcm, tail = stem_hit
            segments.append(("static", stem_id, stem_pcm))
            if tail and _normalize_text(tail):
                segments.append(("live", tail, None))
            continue

        segments.append(("live", cleaned, None))

    return segments


class PunicaTTSService(TTSService):
    """Punica AI Text-to-Speech service with static audio playback and parallel lookahead stitching.

    - Serves pre-recorded clips from /app/records in <1ms (0 Punica API cost).
    - Launches concurrent live dido synthesis for dynamic tails while streaming static stems/bridges.
    - Persists trimmed live syntheses to disk cache to avoid redundant API calls.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://punica.tn/api/v1",
        model: str = "dido",
        voice: str = "dido",
        sample_rate: int = _SAMPLE_RATE,
        text_filters: list | None = None,
        **kwargs,
    ):
        resolved_model = model or "dido"
        resolved_voice = voice or "dido"
        default_settings = TTSSettings(
            model=resolved_model,
            voice=resolved_voice,
            language=None,
        )
        stop_timeout = kwargs.pop("stop_frame_timeout_s", 15.0)
        push_start = kwargs.pop("push_start_frame", True)
        push_stop = kwargs.pop("push_stop_frames", True)
        super().__init__(
            sample_rate=sample_rate,
            text_filters=text_filters,
            push_start_frame=push_start,
            push_stop_frames=push_stop,
            stop_frame_timeout_s=stop_timeout,
            settings=default_settings,
            **kwargs,
        )
        self._model = resolved_model
        self._voice = resolved_voice
        self._sample_rate = sample_rate
        self._cache_dir = _resolve_cache_dir()
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        _ensure_records_loaded()

    def _cache_key(self, text: str) -> str:
        raw = f"{self._model}|{self._voice}|{self._sample_rate}|{text.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _synthesize_live_pcm(self, text: str) -> tuple[bytes, bool]:
        """Return (pcm_bytes, was_disk_cache_hit) for a dynamic sentence."""
        key = self._cache_key(text)
        pcm_path = self._cache_dir / f"{key}.pcm"
        if pcm_path.exists():
            return pcm_path.read_bytes(), True

        async def _fetch_and_cache() -> bytes:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="wav",
            )
            raw_wav = await response.aread()
            with wave.open(io.BytesIO(raw_wav), "rb") as w:
                pcm_raw = w.readframes(w.getnframes())
                sr = w.getframerate()
            pcm_clean = _trim_and_fade_pcm(pcm_raw, sample_rate=sr)
            try:
                pcm_path.write_bytes(pcm_clean)
            except Exception as write_err:
                logger.warning(f"[PunicaTTS] Failed writing disk cache {pcm_path}: {write_err}")
            return pcm_clean

        task = _INFLIGHT_SYNTHESIS.get(key)
        if task is None or task.done():
            task = asyncio.create_task(_fetch_and_cache())
            _INFLIGHT_SYNTHESIS[key] = task

        try:
            pcm = await task
            return pcm, False
        finally:
            if _INFLIGHT_SYNTHESIS.get(key) is task and task.done():
                _INFLIGHT_SYNTHESIS.pop(key, None)

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        if not text or not text.strip():
            return

        t0 = time.perf_counter()
        segments = _parse_segments(text)
        if not segments:
            return

        # Pre-launch all live segments concurrently BEFORE yielding the first static audio chunk
        live_tasks: dict[int, tuple[asyncio.Task[tuple[bytes, bool]], float]] = {}
        for idx, (kind, label, _) in enumerate(segments):
            if kind == "live":
                live_tasks[idx] = (asyncio.create_task(self._synthesize_live_pcm(label)), time.perf_counter())

        if live_tasks:
            # Yield once to the event loop so outbound HTTP requests start immediately
            await asyncio.sleep(0)

        metrics_started = False
        has_static = any(kind == "static" for kind, _, _ in segments)
        has_live = any(kind == "live" for kind, _, _ in segments)

        try:
            for idx, (kind, label, static_pcm) in enumerate(segments):
                if idx > 0:
                    yield TTSAudioRawFrame(
                        audio=_STITCH_GAP_BYTES,
                        sample_rate=self._sample_rate,
                        num_channels=1,
                        context_id=context_id,
                    )

                if kind == "static":
                    assert static_pcm is not None
                    if not metrics_started:
                        await self.start_tts_usage_metrics(text)
                        await self.stop_ttfb_metrics()
                        metrics_started = True
                    for pos in range(0, len(static_pcm), _CHUNK_BYTES):
                        chunk = static_pcm[pos : pos + _CHUNK_BYTES]
                        if chunk:
                            yield TTSAudioRawFrame(
                                audio=chunk,
                                sample_rate=self._sample_rate,
                                num_channels=1,
                                context_id=context_id,
                            )
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    if not has_live:
                        logger.info(f"[PunicaTTS] STATIC HIT: {label} ({elapsed_ms:.1f}ms)")
                    await asyncio.sleep(0)
                else:
                    task, task_start = live_tasks[idx]
                    t_wait_start = time.perf_counter()
                    live_pcm, cache_hit = await task
                    wait_gap_ms = (time.perf_counter() - t_wait_start) * 1000.0
                    live_total_ms = (time.perf_counter() - task_start) * 1000.0

                    if not metrics_started:
                        await self.start_tts_usage_metrics(text)
                        await self.stop_ttfb_metrics()
                        metrics_started = True

                    for pos in range(0, len(live_pcm), _CHUNK_BYTES):
                        chunk = live_pcm[pos : pos + _CHUNK_BYTES]
                        if chunk:
                            yield TTSAudioRawFrame(
                                audio=chunk,
                                sample_rate=self._sample_rate,
                                num_channels=1,
                                context_id=context_id,
                            )

                    if has_static:
                        static_labels = "+".join(lbl for k, lbl, _ in segments if k == "static")
                        logger.info(
                            f"[PunicaTTS] HYBRID STITCH: {static_labels} + live '{label}' "
                            f"(live_ms={live_total_ms:.1f}ms, wait_gap={wait_gap_ms:.1f}ms, cache={'hit' if cache_hit else 'miss'})"
                        )
                    else:
                        logger.info(
                            f"[PunicaTTS] LIVE TTS: '{label}' ({live_total_ms:.1f}ms, cache={'hit' if cache_hit else 'miss'})"
                        )
        except Exception as e:
            for task, _ in live_tasks.values():
                if not task.done():
                    task.cancel()
            logger.error(f"Punica TTS error: {e}")
            yield ErrorFrame(error=f"Punica TTS error: {e}")
