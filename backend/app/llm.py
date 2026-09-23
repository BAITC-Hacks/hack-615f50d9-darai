"""Client for the local OpenAI-compatible LLM endpoint (Ollama / vLLM).

Refuses endpoints that do not resolve to loopback/private addresses and
Ollama ``:cloud`` models, which proxy prompts to a remote service.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx

from .config import Settings


class LLMError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def _is_local_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%")[0])
    return ip.is_loopback or ip.is_private or ip.is_link_local


def check_endpoint(settings: Settings) -> None:
    model = settings.llm_model.strip()
    if not model:
        raise LLMError("LLM_UNAVAILABLE", "LLM_MODEL не задан")
    lowered = model.lower()
    if lowered.endswith(":cloud") or lowered.endswith("-cloud"):
        raise LLMError("LLM_FORBIDDEN_ENDPOINT", "Облачные модели Ollama запрещены: данные покинули бы контур")
    parsed = urlparse(settings.llm_base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise LLMError("LLM_FORBIDDEN_ENDPOINT", "Некорректный LLM_BASE_URL")
    if host in settings.extra_llm_hosts:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise LLMError("LLM_UNAVAILABLE", f"Не удаётся разрешить адрес LLM: {host}")
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_local_ip(a) for a in addresses):
        raise LLMError("LLM_FORBIDDEN_ENDPOINT", f"LLM_BASE_URL указывает на внешний адрес: {host}")


def thinking_params() -> dict:
    """Runtime-specific switch that disables reasoning output for compact JSON.

    Parameters are NOT interchangeable between runtimes (checked on Ollama 0.33.2:
    /v1/chat/completions honours ``reasoning_effort: "none"`` and ignores ``think``).
    LLM_THINKING_CONTROL: ollama (default) | vllm | off.
    """
    mode = (os.environ.get("LLM_THINKING_CONTROL") or "ollama").strip().lower()
    if mode == "ollama":
        return {"reasoning_effort": "none"}
    if mode == "vllm":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._transport = transport

    def chat_json(self, messages: list[dict]) -> str:
        """Return raw assistant content; JSON parsing/validation is the caller's job."""
        if self._transport is None:
            check_endpoint(self.settings)
        headers = {}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "response_format": {"type": "json_object"},
            "stream": False,
            **thinking_params(),
        }
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            with httpx.Client(timeout=self.settings.llm_timeout_seconds, transport=self._transport,
                              trust_env=False) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise LLMError("LLM_TIMEOUT", "LLM не ответила за отведённое время")
        except httpx.HTTPError as exc:
            raise LLMError("LLM_UNAVAILABLE", f"LLM недоступна: {type(exc).__name__}")
        if resp.status_code >= 400:
            raise LLMError("LLM_UNAVAILABLE", f"LLM вернула HTTP {resp.status_code}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("LLM_INVALID_RESPONSE", "Ответ LLM не соответствует протоколу chat/completions")
        if not isinstance(content, str):
            raise LLMError("LLM_INVALID_RESPONSE", "Пустой ответ LLM")
        return content

    def ping(self) -> tuple[bool, str | None]:
        try:
            check_endpoint(self.settings)
            url = self.settings.llm_base_url.rstrip("/") + "/models"
            with httpx.Client(timeout=5, trust_env=False) as client:
                resp = client.get(url)
            return resp.status_code < 400, None if resp.status_code < 400 else f"HTTP {resp.status_code}"
        except LLMError as exc:
            return False, exc.message
        except httpx.HTTPError as exc:
            return False, type(exc).__name__
