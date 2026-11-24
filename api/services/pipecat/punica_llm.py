import openai
from pipecat.services.openai.llm import OpenAILLMService

from api.services.pipecat.punica_http import (
    DEFAULT_PUNICA_BASE_URL,
    get_shared_punica_http_client,
)


class PunicaLLMService(OpenAILLMService):
    """Punica AI LLM service wrapper.

    Punica's chat completions endpoint strictly requires at least one user message
    in the `messages` array. At the start of a voice call (such as during greeting
    generation), Dograh creates an initial completion request that contains only the
    system prompt. This wrapper injects an initial user prompt whenever no user turns
    exist yet in the context messages, preventing a 400 invalid_request_error.

    Uses a shared persistent HTTP/TLS connection pool so LLM turns reuse warm
    sockets across STT/LLM/TTS and across calls.
    """

    def create_client(
        self,
        api_key=None,
        base_url=None,
        organization=None,
        project=None,
        default_headers=None,
        **kwargs,
    ):
        resolved_base_url = base_url or DEFAULT_PUNICA_BASE_URL
        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url=resolved_base_url,
            organization=organization,
            project=project,
            http_client=get_shared_punica_http_client(resolved_base_url),
            default_headers=default_headers,
        )

    def build_chat_completion_params(self, params_from_context) -> dict:
        params = super().build_chat_completion_params(params_from_context)
        messages = params.get("messages")
        if messages is not None:
            has_user = any(
                isinstance(m, dict) and m.get("role") == "user" for m in messages
            )
            if not has_user:
                # Append a starter user message so Punica can generate the opening turn
                params["messages"] = list(messages) + [{"role": "user", "content": " "}]
        return params
