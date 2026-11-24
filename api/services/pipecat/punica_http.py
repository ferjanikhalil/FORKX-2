"""Shared keep-alive HTTP connection pool and call-scoped warm-up for Punica AI."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import openai
from loguru import logger

from api.services.configuration.registry import ServiceProviders

DEFAULT_PUNICA_BASE_URL = "https://punica.tn/api/v1"
_KEEPALIVE_INTERVAL_SECONDS = 10.0


class _NonClosingSharedAsyncHttpxClient(openai.DefaultAsyncHttpxClient):
    """Shared AsyncClient whose pool survives per-visit AsyncOpenAI.close() calls."""

    async def aclose(self) -> None:
        # Individual STT/LLM/TTS service instances may close their AsyncOpenAI
        # wrapper when an agent visit ends; keep the underlying TLS pool warm
        # for active and subsequent calls on the same worker event loop.
        return

    async def close_pool(self) -> None:
        await super().aclose()


_SHARED_CLIENTS: dict[tuple[int, str], _NonClosingSharedAsyncHttpxClient] = {}


def _normalize_base_url(base_url: str | None) -> str:
    url = (base_url or DEFAULT_PUNICA_BASE_URL).strip().rstrip("/")
    return url or DEFAULT_PUNICA_BASE_URL


def _current_loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def is_punica_endpoint(provider: Any, base_url: str | None = None) -> bool:
    provider_val = getattr(provider, "value", provider)
    if str(provider_val or "").lower() == ServiceProviders.PUNICA.value:
        return True
    return bool(base_url and "punica.tn" in base_url.lower())


def get_shared_punica_http_client(
    base_url: str | None = None,
) -> _NonClosingSharedAsyncHttpxClient:
    """Return a loop-scoped persistent keep-alive HTTP client for Punica services."""
    normalized = _normalize_base_url(base_url)
    key = (_current_loop_key(), normalized)
    client = _SHARED_CLIENTS.get(key)
    if client is None or client.is_closed:
        client = _NonClosingSharedAsyncHttpxClient(
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=60.0,
            ),
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        _SHARED_CLIENTS[key] = client
    return client


def _extract_punica_targets(user_config: Any) -> list[tuple[str, str]]:
    """Collect unique (base_url, api_key) pairs for configured Punica services."""
    if user_config is None:
        return []
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for section_name in ("stt", "llm", "tts"):
        section = getattr(user_config, section_name, None)
        if section is None:
            continue
        provider = getattr(section, "provider", None)
        base_url = getattr(section, "base_url", None)
        if not is_punica_endpoint(provider, base_url):
            continue
        api_key = getattr(section, "api_key", None)
        if not api_key:
            continue
        norm_url = _normalize_base_url(base_url)
        if norm_url not in seen:
            seen.add(norm_url)
            targets.append((norm_url, api_key))
    return targets


async def _ping_punica_models(base_url: str, api_key: str) -> None:
    """Send a zero-token GET /models request to warm or retain TLS sockets."""
    client = get_shared_punica_http_client(base_url)
    try:
        await client.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug(f"[PunicaHTTP] Keep-alive ping skipped for {base_url}: {exc}")


def start_punica_call_keepalive(user_config: Any) -> asyncio.Task | None:
    """Warm Punica TLS connections immediately and keep them alive during a call."""
    targets = _extract_punica_targets(user_config)
    if not targets:
        return None

    async def _keepalive_loop() -> None:
        try:
            while True:
                await asyncio.gather(
                    *(_ping_punica_models(url, key) for url, key in targets),
                    return_exceptions=True,
                )
                await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    return asyncio.create_task(_keepalive_loop(), name="punica-http-keepalive")
