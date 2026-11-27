from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.services.openai.realtime import events

from api.services.pipecat.realtime.openai_realtime import (
    DograhOpenAIRealtimeLLMService,
)
from api.services.workflow.pipecat_engine_callbacks import handle_user_idle


@pytest.mark.asyncio
async def test_openai_realtime_messages_append_frame_sends_conversation_item():
    service = DograhOpenAIRealtimeLLMService(api_key="test")
    service._api_session_ready = True
    service.send_client_event = AsyncMock()
    service._send_manual_response_create = AsyncMock()

    await service._handle_messages_append(
        LLMMessagesAppendFrame(
            [{"role": "user", "content": "Are you still there?"}],
            run_llm=True,
        )
    )

    service.send_client_event.assert_awaited_once()
    event = service.send_client_event.await_args.args[0]
    assert isinstance(event, events.ConversationItemCreateEvent)
    assert event.item.role == "user"
    assert event.item.type == "message"
    assert event.item.content == [
        events.ItemContent(type="input_text", text="Are you still there?")
    ]
    service._send_manual_response_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_idle_handler_uses_realtime_append_path():
    engine = SimpleNamespace(
        llm=SimpleNamespace(),
        end_call_with_reason=AsyncMock(),
    )
    aggregator = SimpleNamespace(push_frame=AsyncMock())
    await handle_user_idle(engine, aggregator, 1)

    aggregator.push_frame.assert_awaited_once()
    frame = aggregator.push_frame.await_args.args[0]
    assert isinstance(frame, LLMMessagesAppendFrame)
    assert frame.run_llm is True
    assert frame.messages == [
        {
            "role": "user",
            "content": "The user has been quiet. Politely and briefly ask if they're still there in the language that the user has been speaking so far.",
        }
    ]


@pytest.mark.asyncio
async def test_user_idle_second_attempt_waits_for_farewell_speech_before_end_call():
    order: list[str] = []

    async def wait_speech():
        order.append("farewell_played")

    closing_speech = SimpleNamespace(wait=AsyncMock(side_effect=wait_speech))
    speech_playback = SimpleNamespace(
        expect_response=lambda source: closing_speech
    )
    active_agent = SimpleNamespace(llm=object())

    async def end_call(reason):
        order.append(f"end_call:{reason}")

    async def push_frame(frame):
        order.append("push_frame")

    engine = SimpleNamespace(
        speech_playback=speech_playback,
        active_agent=active_agent,
        end_call_with_reason=AsyncMock(side_effect=end_call),
    )
    aggregator = SimpleNamespace(push_frame=AsyncMock(side_effect=push_frame))

    await handle_user_idle(engine, aggregator, 2)

    assert order == [
        "push_frame",
        "farewell_played",
        "end_call:user_idle_max_duration_exceeded",
    ]

