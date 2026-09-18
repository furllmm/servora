from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any


class AIPlanError(ValueError):
    pass


class AIProvider:
    """Provider interface. Providers return a decoded JSON object, never shell commands."""

    name = "unknown"

    def generate(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError


class MockAIProvider(AIProvider):
    name = "mock"

    def generate(self, prompt: str) -> dict[str, Any]:
        return {
            "action": "create_container",
            "name": "example",
            "image": "nginx:alpine",
            "ports": [{"host": 8080, "container": 80, "protocol": "tcp"}],
            "environment": {},
            "resources": {},
            "launch": False,
        }


def _extract_json(text: str) -> dict[str, Any]:
    text = str(text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise AIPlanError("AI response did not contain valid JSON")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise AIPlanError("AI response did not contain valid JSON") from exc
    if not isinstance(value, dict):
        raise AIPlanError("AI response must be a JSON object")
    return value


class OpenAICompatibleProvider(AIProvider):
    """Minimal dependency-free client for OpenAI-compatible /v1/chat/completions APIs.

    Works with local servers such as Ollama-compatible gateways, LM Studio, llama.cpp
    servers, or cloud providers exposing the same API shape. API keys stay in memory and
    are never included in returned diagnostics.
    """

    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        if not self.base_url or not self.model:
            raise ValueError("base_url and model are required")
        if not re.match(r"^https?://", self.base_url, re.I):
            raise ValueError("AI base_url must use HTTP(S)")

    @classmethod
    def from_env(cls) -> "OpenAICompatibleProvider":
        return cls(
            os.environ.get("SERVORA_AI_BASE_URL", "http://127.0.0.1:11434/v1"),
            os.environ.get("SERVORA_AI_MODEL", "llama3.2:1b"),
            os.environ.get("SERVORA_AI_API_KEY"),
            float(os.environ.get("SERVORA_AI_TIMEOUT", "60")),
        )

    def generate(self, prompt: str) -> dict[str, Any]:
        system = (
            "You are Servora's container planning assistant. Return ONLY valid JSON. "
            "Never return shell commands. Never request privileged host access unless the "
            "user explicitly asks; represent configuration as structured data."
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read(512).decode("utf-8", "replace")
            raise AIPlanError(f"AI provider HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AIPlanError(f"AI provider connection failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AIPlanError("AI provider returned invalid JSON") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIPlanError("AI provider response has no message content") from exc
        return _extract_json(content)


def provider_from_env() -> AIProvider:
    """Select a provider without making a network request."""
    provider = os.environ.get("SERVORA_AI_PROVIDER", "mock").lower()
    if provider == "mock":
        return MockAIProvider()
    if provider in {"openai", "openai-compatible", "local"}:
        return OpenAICompatibleProvider.from_env()
    raise AIPlanError(f"Unsupported AI provider: {provider}")


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("action") != "create_container":
        raise AIPlanError("Unsupported plan")
    for key in ("name", "image"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise AIPlanError(f"Missing {key}")
    if len(plan["name"]) > 128 or any(c in plan["name"] for c in "\x00\n\r"):
        raise AIPlanError("Invalid container name")
    if len(plan["image"]) > 512 or any(c.isspace() for c in plan["image"]):
        raise AIPlanError("Invalid image reference")
    return plan


def create_container_plan(provider: AIProvider, prompt: str) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16_384:
        raise AIPlanError("Prompt must be between 1 and 16384 characters")
    return validate_plan(provider.generate(prompt))


@dataclass(frozen=True)
class AIPlanResponse:
    provider: str
    plan: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "plan": self.plan}
