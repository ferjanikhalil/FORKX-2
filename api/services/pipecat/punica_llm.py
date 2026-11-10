from pipecat.services.openai.llm import OpenAILLMService


class PunicaLLMService(OpenAILLMService):
    """Punica AI LLM service wrapper.

    Punica's chat completions endpoint strictly requires at least one user message
    in the `messages` array. At the start of a voice call (such as during greeting
    generation), Dograh creates an initial completion request that contains only the
    system prompt. This wrapper injects an initial user prompt whenever no user turns
    exist yet in the context messages, preventing a 400 invalid_request_error.
    """

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
