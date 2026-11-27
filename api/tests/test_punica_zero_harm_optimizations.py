import asyncio
import io
import time
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    LLMContextFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.services.tts_service import TextAggregationMode
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.punica_http import (
    DEFAULT_PUNICA_BASE_URL,
    get_shared_punica_http_client,
)
from api.services.pipecat.punica_llm import PunicaLLMService
from api.services.pipecat.punica_tts import (
    PunicaTTSService,
    _OPENER_BRIDGE_IDS_UPPER,
    _PunicaTagAwareTextAggregator,
    _RECORDS_BY_ID_UPPER,
    _STANDALONE_TAG_IDS_UPPER,
    _STEMS_BY_TOKENS,
    _ensure_records_loaded,
    _parse_segments,
)
from api.services.pipecat.run_pipeline import (
    VADAwareSpeechTimeoutUserTurnStopStrategy,
    _create_non_realtime_user_turn_start_strategies,
    _create_non_realtime_user_turn_stop_strategies,
)
from api.services.pipecat.service_factory import create_stt_service


def _make_dummy_wav(sample_rate: int = 24000, duration_ms: int = 100) -> bytes:
    buf = io.BytesIO()
    n_samples = int(sample_rate * (duration_ms / 1000.0))
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x10\x00" * n_samples)
    return buf.getvalue()


async def test_segmented_stt_does_not_pay_second_post_stt_silence_timeout():
    """Once 0.6s has elapsed since VAD stop during STT upload, finalized transcript stops turn immediately."""
    context = LLMContext()
    user = LLMUserAggregator(
        context,
        params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=_create_non_realtime_user_turn_start_strategies(
                    {}, uses_external_turns=False
                ),
                stop=[VADAwareSpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.25)],
            ),
        ),
    )

    timestamps: dict[str, float] = {}

    @user.event_handler("on_before_process_frame")
    async def on_before(aggregator, frame):
        if isinstance(frame, TranscriptionFrame):
            timestamps["transcript_in"] = time.perf_counter()

    orig_push = user.push_frame

    async def recording_push(frame, *args, **kwargs):
        if isinstance(frame, LLMContextFrame) and "context_out" not in timestamps:
            timestamps["context_out"] = time.perf_counter()
        return await orig_push(frame, *args, **kwargs)

    user.push_frame = recording_push

    frames = [
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
        # Simulate segmented STT taking 0.30s (> 0.25s speech timeout)
        SleepFrame(sleep=0.30),
        TranscriptionFrame("تفضل", "user", "now", finalized=True),
        SleepFrame(sleep=0.05),
    ]
    down, _ = await run_test(user, frames_to_send=frames)

    assert any(isinstance(f, LLMContextFrame) for f in down)
    assert any(isinstance(f, UserStoppedSpeakingFrame) for f in down)
    assert [m["content"] for m in context.messages] == ["تفضل"]
    # Turn should finalize right on TranscriptionFrame arrival (< 80ms), NOT +250ms later
    post_stt_delay = timestamps["context_out"] - timestamps["transcript_in"]
    assert post_stt_delay < 0.08, f"Expected immediate stop after STT, got {post_stt_delay:.3f}s"


async def test_segmented_stt_preserves_barge_in_min_words_and_clean_next_turn():
    """A rejected 1-word noise during bot speech never leaks text or breaks the next turn."""
    context = LLMContext()
    user = LLMUserAggregator(
        context,
        params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=_create_non_realtime_user_turn_start_strategies(
                    {}, uses_external_turns=False
                ),
                stop=_create_non_realtime_user_turn_stop_strategies(
                    {}, uses_external_turns=False
                ),
            ),
        ),
    )

    frames = [
        BotStartedSpeakingFrame(),
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=0.65),
        # 1 word while bot speaking -> rejected by MinWords(2)
        TranscriptionFrame("آه", "user", "now", finalized=True),
        SleepFrame(sleep=0.05),
        # Real 2-word barge-in
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=0.65),
        TranscriptionFrame("لا بطلت", "user", "now", finalized=True),
        SleepFrame(sleep=0.05),
    ]
    down, _ = await run_test(user, frames_to_send=frames)
    assert [m["content"] for m in context.messages] == ["لا بطلت"]
    assert sum(isinstance(f, LLMContextFrame) for f in down) == 1


async def test_punica_services_share_non_closing_keepalive_http_pool():
    shared = get_shared_punica_http_client(DEFAULT_PUNICA_BASE_URL)
    user_config = SimpleNamespace(
        stt=SimpleNamespace(
            provider=ServiceProviders.PUNICA.value,
            api_key="stt-key",
            model="fennec",
            language="ar",
            base_url=DEFAULT_PUNICA_BASE_URL,
        )
    )
    audio_config = SimpleNamespace(
        transport_in_sample_rate=16000,
        transport_out_sample_rate=16000,
    )
    stt = create_stt_service(user_config, audio_config)
    llm = PunicaLLMService(api_key="llm-key", base_url=DEFAULT_PUNICA_BASE_URL)
    tts = PunicaTTSService(api_key="tts-key", base_url=DEFAULT_PUNICA_BASE_URL)

    assert stt._client._client is shared
    assert llm._client._client is shared
    assert tts._client._client is shared
    assert stt._client.auth_headers == {"Authorization": "Bearer stt-key"}
    assert llm._client.auth_headers == {"Authorization": "Bearer llm-key"}
    assert tts._client.auth_headers == {"Authorization": "Bearer tts-key"}

    await llm._client.close()
    assert not shared.is_closed

    # Verify full AsyncOpenAI request preparation (timeout + header math) succeeds end-to-end
    import importlib

    openai_httpx = importlib.import_module(type(shared._transport).__module__.split(".")[0])

    seen_auth: list[str] = []

    def handler(request):
        seen_auth.append(request.headers.get("Authorization", ""))
        return openai_httpx.Response(
            200, content=_make_dummy_wav(), headers={"content-type": "audio/wav"}
        )

    orig_transport = shared._transport
    shared._transport = openai_httpx.MockTransport(handler)
    try:
        resp = await tts._client.audio.speech.create(
            model="dido",
            voice="dido",
            input="سي خليل؟",
            response_format="wav",
        )
        wav_bytes = await resp.aread()
        assert len(wav_bytes) > 44
        assert seen_auth == ["Bearer tts-key"]
    finally:
        shared._transport = orig_transport


async def test_punica_tag_aggregator_flushes_standalone_bridges_immediately_not_stems():
    agg = _PunicaTagAwareTextAggregator(
        aggregation_type=TextAggregationMode.SENTENCE,
        language="en",
    )

    # Stream hybrid recap character by character: [BOX_02A] must flush BEFORE dynamic text arrives
    emitted_during_tag: list[str] = []
    for ch in "[BOX_02A]":
        async for item in agg.aggregate(ch):
            emitted_during_tag.append(item.text)
    assert emitted_during_tag == ["[BOX_02A]"]

    # Dynamic tail + closing standalone tag
    rest_emitted: list[str] = []
    for ch in " عندك آيباد عشرة بالتوصيل. [BOX_02D]":
        async for item in agg.aggregate(ch):
            rest_emitted.append(item.text)
    flushed = await agg.flush()
    if flushed:
        rest_emitted.append(flushed.text)

    combined = " ".join(rest_emitted)
    assert "عندك آيباد عشرة بالتوصيل." in combined
    assert "[BOX_02D]" in combined

    # STATIC STEM ([BOX_01A_stem]) must NOT flush prematurely on ']'
    stem_early: list[str] = []
    for ch in "[BOX_01A_stem]":
        async for item in agg.aggregate(ch):
            stem_early.append(item.text)
    assert stem_early == []
    for ch in " سي خليل؟":
        async for item in agg.aggregate(ch):
            stem_early.append(item.text)
    stem_flushed = await agg.flush()
    if stem_flushed:
        stem_early.append(stem_flushed.text)
    assert stem_early == ["[BOX_01A_stem] سي خليل؟"]


async def test_prewarm_spoken_slots_warms_only_greeting_and_hybrid_lines_with_lossless_wav(tmp_path):
    tts = PunicaTTSService(api_key="test-key", base_url=DEFAULT_PUNICA_BASE_URL)
    tts._cache_dir = tmp_path

    calls: list[dict] = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        resp = SimpleNamespace(aread=AsyncMock(return_value=_make_dummy_wav()))
        return resp

    tts._client.audio.speech.create = fake_create

    prompt = (
        "# تعليمات داخلية لا يجب نطقها\n"
        "1. إذا أكد أنه سي خليل جاوب بـ:\n"
        "[BOX_02A] عندك غالاكسي الترا بـ ألفين دينار بالتوصيل. [BOX_02D]\n"
        "2. إذا سأل على وقت التوصيل: رد بـ [BOX_11C]\n"
    )
    tts.prewarm_spoken_slots(greeting="[BOX_01A_stem] سي مراد؟", prompt=prompt)
    await asyncio.sleep(0.05)

    inputs = [c["input"] for c in calls]
    assert inputs == [
        "سي مراد؟",
        "عندك غالاكسي الترا بـ ألفين دينار بالتوصيل",
    ]
    assert all(c["response_format"] == "wav" for c in calls)


async def test_manifest_loads_all_84_clips_including_new_cod_studio_clips():
    _ensure_records_loaded()
    assert len(_RECORDS_BY_ID_UPPER) == 84
    assert len(_STEMS_BY_TOKENS) == 12
    assert len(_STANDALONE_TAG_IDS_UPPER) == 72
    assert len(_OPENER_BRIDGE_IDS_UPPER) == 12
    for expected_new_id in (
        "BOX_24D",
        "BOX_15B",
        "BOX_05C_STEM",
        "BOX_02F_PRICE_STEM",
        "BRIDGE_07",
        "BRIDGE_08",
    ):
        assert expected_new_id in _RECORDS_BY_ID_UPPER


async def test_cache_key_normalizes_edge_punctuation_and_honors_legacy_cache(tmp_path):
    tts = PunicaTTSService(api_key="test-key", base_url=DEFAULT_PUNICA_BASE_URL)
    tts._cache_dir = tmp_path

    # Trailing/leading punctuation and extra whitespace map to the same normalized key
    key_plain = tts._cache_key("عندك آيباد عشرة بالتوصيل")
    key_punct = tts._cache_key("  عندك   آيباد عشرة بالتوصيل.، ")
    assert key_plain == key_punct

    # Legacy cache files written before _normalize_cache_text remain readable
    legacy_text = "عنوانك أريانة النصر."
    legacy_path = tmp_path / f"{tts._legacy_cache_key(legacy_text)}.pcm"
    legacy_path.write_bytes(b"\x01\x00" * 240)
    assert tts._is_cached(legacy_text)
    pcm, hit = await tts._synthesize_live_pcm(legacy_text)
    assert hit is True
    assert pcm == b"\x01\x00" * 240


async def test_bridge_echo_stripping_and_consecutive_opener_bridge_dedup(tmp_path):
    tts = PunicaTTSService(api_key="test-key", base_url=DEFAULT_PUNICA_BASE_URL)
    tts._cache_dir = tmp_path

    calls: list[str] = []

    async def fake_create(**kwargs):
        calls.append(kwargs["input"])
        return SimpleNamespace(aread=AsyncMock(return_value=_make_dummy_wav()))

    tts._client.audio.speech.create = fake_create

    # 1. Inline bridge echo stripping: LLM echoes [BRIDGE_03] transcript right after the tag
    segs_inline = _parse_segments(
        "[BRIDGE_03] على عيني، هاني نقيد فيها عندك: العنوان الجديد حي النصر أريانة."
    )
    assert [(k, lbl) for k, lbl, _ in segs_inline] == [
        ("static", "BRIDGE_03"),
        ("live", "العنوان الجديد حي النصر أريانة"),
    ]

    # 2. Inline accidental double opener bridge: keeps single longer bridge clip
    segs_double_bridge = _parse_segments("[BRIDGE_04] [BOX_08A] السلعة متوفرة في الستوك.")
    assert [(k, lbl) for k, lbl, _ in segs_double_bridge] == [
        ("static", "BOX_08A"),
        ("live", "السلعة متوفرة في الستوك"),
    ]

    # 3. Legitimate paired static clips (NOT two opener bridges) remain untouched
    segs_valid_pair = _parse_segments("[BOX_15A] [BOX_02D]")
    assert [(k, lbl) for k, lbl, _ in segs_valid_pair] == [
        ("static", "BOX_15A"),
        ("static", "BOX_02D"),
    ]

    # 4. Cross-continuation bridge echo stripping across two run_tts calls in the same context_id
    tts.start_tts_usage_metrics = AsyncMock()
    tts.stop_ttfb_metrics = AsyncMock()

    async for _ in tts.run_tts("[BRIDGE_03]", "ctx-echo-1"):
        pass
    async for _ in tts.run_tts(
        "على عيني هاني نقيد فيها عندك: التوصيل لحي النصر.", "ctx-echo-1"
    ):
        pass

    assert calls == ["التوصيل لحي النصر"]


async def test_double_question_and_self_contained_prompt_safeguards(tmp_path):
    import json
    import re
    from pathlib import Path

    tts = PunicaTTSService(api_key="test-key", base_url=DEFAULT_PUNICA_BASE_URL)
    tts._cache_dir = tmp_path
    tts.start_tts_usage_metrics = AsyncMock()
    tts.stop_ttfb_metrics = AsyncMock()

    calls: list[str] = []

    async def fake_create(**kwargs):
        calls.append(kwargs["input"])
        return SimpleNamespace(aread=AsyncMock(return_value=_make_dummy_wav()))

    tts._client.audio.speech.create = fake_create

    # 1. Inline open question followed by [BOX_02D] drops redundant [BOX_02D]
    segs_double_q = _parse_segments(
        "[BRIDGE_06] الألوان المتوفرة رمادي وأزرق. شنوة اللون إلي تحب نبدلولو؟ [BOX_02D]"
    )
    assert [(k, lbl) for k, lbl, _ in segs_double_q] == [
        ("static", "BRIDGE_06"),
        ("live", "الألوان المتوفرة رمادي وأزرق. شنوة اللون إلي تحب نبدلولو؟"),
    ]

    # 2. Normal declarative statement followed by [BOX_02D] keeps [BOX_02D]
    segs_normal = _parse_segments(
        "[BOX_02B] عندك آيباد الجيل العاشر بـ ألف وثلاث مية وتسعين دينار لـ حي النصر ثنين أريانة. [BOX_02D]"
    )
    assert [(k, lbl) for k, lbl, _ in segs_normal] == [
        ("static", "BOX_02B"),
        ("live", "عندك آيباد الجيل العاشر بـ ألف وثلاث مية وتسعين دينار لـ حي النصر ثنين أريانة"),
        ("static", "BOX_02D"),
    ]

    # 3. Cross-continuation streaming: live question chunk then [BOX_02D] chunk in same context_id
    frames_q = [f async for f in tts.run_tts("شنوة اللون إلي تحب؟", "ctx-q-1")]
    frames_dup_tag = [f async for f in tts.run_tts("[BOX_02D]", "ctx-q-1")]
    assert len(frames_q) > 0
    assert frames_dup_tag == []

    # 3b. Compound objection with an earlier question clip ([BOX_19B]) + declarative ([BOX_17B]) drops trailing [BOX_02D]
    segs_compound_q = _parse_segments("[BOX_19B] [BOX_17B] [BOX_02D]")
    assert [(k, lbl) for k, lbl, _ in segs_compound_q] == [
        ("static", "BOX_19B"),
        ("static", "BOX_17B"),
    ]
    # Whereas declarative [BOX_19A] + [BOX_17B] + [BOX_02D] keeps the single closing question [BOX_02D]
    segs_compound_decl = _parse_segments("[BOX_19A] [BOX_17B] [BOX_02D]")
    assert [(k, lbl) for k, lbl, _ in segs_compound_decl] == [
        ("static", "BOX_19A"),
        ("static", "BOX_17B"),
        ("static", "BOX_02D"),
    ]

    # 4. Self-contained prompt clips ([BOX_10A], [BOX_30A]) drop redundant trailing live text
    segs_phone = _parse_segments("[BOX_10A] عطيني النومرو الثاني ونتسجلو مع التوصيل.")
    assert [(k, lbl) for k, lbl, _ in segs_phone] == [("static", "BOX_10A")]

    segs_fr = _parse_segments("[BOX_30A] Je vous appelle de la part de Tunis Tech.")
    assert [(k, lbl) for k, lbl, _ in segs_fr] == [("static", "BOX_30A")]

    # 5. Every [BOX_*] / [BRIDGE_*] tag in workflow_2_production_dynamic_cod.json exists in manifest
    wf_path = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "workflow_2_production_dynamic_cod.json"
    )
    wf_raw = wf_path.read_text(encoding="utf-8")
    json.loads(wf_raw)  # valid JSON
    wf_tags = {
        m.upper()
        for m in re.findall(r"\[((?:BOX|BRIDGE)_[A-Za-z0-9_]+)\]", wf_raw)
    }
    assert len(wf_tags) >= 50
    missing = wf_tags - set(_RECORDS_BY_ID_UPPER.keys())
    assert missing == set(), f"Workflow #2 references missing tags: {missing}"


