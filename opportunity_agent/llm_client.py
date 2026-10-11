from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from contextvars import ContextVar
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request

from pydantic import BaseModel, ValidationError

from .modelscope_transport import open_modelscope_request
from .config import chat_completions_url, llm_api_base, llm_api_key, llm_model
from .llm_context import inject_conversation


T = TypeVar("T", bound=BaseModel)
Completion = Callable[[dict[str, Any]], Any]


@dataclass
class CompletionMessage:
    """The OpenAI assistant-message subset needed for local tool execution."""

    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


class LLMClient:
    """Single OpenAI-compatible ModelScope transport for extraction and planning."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 35,
        retries: int = 1,
        completion_fn: Completion | None = None,
        structured_output_mode: str | None = None,
    ) -> None:
        self.api_key = api_key or llm_api_key()
        self.model = model or llm_model()
        self.base_url = chat_completions_url(base_url or llm_api_base())
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.completion_fn = completion_fn
        self.tls_compatibility_retry = True
        self.last_error: str | None = None
        self.last_latency_ms = 0
        self.structured_output_mode = structured_output_mode or os.getenv("STRUCTURED_OUTPUT_MODE", "json_object")
        if self.structured_output_mode not in {"json_schema", "json_object", "prompt"}:
            raise ValueError("invalid structured output mode")
        self._completion_metadata: ContextVar[CompletionMessage | None] = ContextVar("completion_metadata", default=None)
        self._request_budget: ContextVar[tuple] = ContextVar("request_budget", default=(None, None))
        self.reasoning_effort: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.completion_fn or self.api_key)

    def complete_message(self, payload: dict[str, Any]) -> CompletionMessage:
        deadline, cancel_event = self._request_budget.get()
        _check_budget(deadline, cancel_event)
        payload = inject_conversation(payload)
        started = time.perf_counter()
        self.last_error = None
        try:
            if self.completion_fn:
                response = self.completion_fn(payload)
                message = _completion_message(response)
                self._completion_metadata.set(message)
                return message
            if not self.api_key:
                raise RuntimeError("MODELSCOPE_API_KEY is not configured")
            request = Request(
                self.base_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            transport_options = {} if self.tls_compatibility_retry else {"retry_handshake": False}
            timeout = min(self.timeout_seconds, max(.001, deadline - time.monotonic())) if deadline else self.timeout_seconds
            with open_modelscope_request(request, timeout=timeout, **transport_options) as response:
                body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            choice = body["choices"][0]
            message = _completion_message({"message": choice.get("message", {}), "finish_reason": choice.get("finish_reason"),
                                           "usage": body.get("usage")})
            self._completion_metadata.set(message)
            return message
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, KeyError, IndexError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.last_latency_ms = int((time.perf_counter() - started) * 1000)

    def complete_payload(self, payload: dict[str, Any]) -> str:
        """Backward-compatible text-only completion API."""
        return self.complete_message(payload).content

    def generate(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.15,
        max_tokens: int = 1800,
        thinking: bool = False,
        response_format: dict[str, Any] | None = None,
        deadline: float | None = None,
        cancel_event: Any = None,
        diagnostics: list[dict[str, Any]] | None = None,
        _retry_transport: bool = True,
    ) -> str:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        # enable_thinking is a Qwen extension and can make strict OpenAI
        # gateways reject GPT requests as an unknown field.
        if "qwen" in self.model.casefold():
            payload["enable_thinking"] = thinking
        if response_format:
            payload["response_format"] = response_format
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # ``retries`` applied only to generate_structured, so a plain text caller
        # such as the Synthesizer silently had a single attempt. Transport errors
        # are retried here too; a refusal or bad request is raised on first sight.
        budget_token = self._request_budget.set((deadline, cancel_event))
        try:
            for attempt in range(1, (self.retries if _retry_transport else 0) + 2):
                _check_budget(deadline, cancel_event)
                self._completion_metadata.set(None)
                started = time.monotonic()
                entry = {"attempt": attempt, "max_tokens": max_tokens, "usage": None}
                try:
                    content = self.complete_payload(payload)
                    _check_budget(deadline, cancel_event)
                    message = self._completion_metadata.get()
                    entry.update(status="ok", content_chars=len(content),
                                 finish_reason=message.finish_reason if message else None,
                                 usage=_safe_usage(message.usage) if message else None)
                    return content
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    entry.update(status="failed", error_code=type(exc).__name__)
                    if isinstance(exc, HTTPError):
                        entry["http_status"] = exc.code
                    if not _retry_transport or attempt > self.retries or _is_client_error(exc):
                        raise
                except Exception as exc:
                    entry.update(status="failed", error_code=type(exc).__name__)
                    raise
                finally:
                    entry["latency_ms"] = round((time.monotonic() - started) * 1000)
                    if diagnostics is not None:
                        diagnostics.append(entry)
                _retry_pause(deadline, cancel_event)
        finally:
            self._request_budget.reset(budget_token)

    def generate_stream(self, *, on_delta, **kwargs) -> str:
        """Real provider SSE. Retry only before visible text, never duplicate a draft."""
        if self.completion_fn:
            answer = self.generate(**kwargs)
            on_delta(answer)
            return answer
        if not self.api_key:
            raise RuntimeError("LLM API key is not configured")
        deadline, cancel = kwargs.get("deadline"), kwargs.get("cancel_event")
        payload = inject_conversation({"model": self.model, "stream": True,
            "temperature": kwargs.get("temperature", .15), "max_tokens": kwargs.get("max_tokens", 1800),
            "messages": [{"role": "system", "content": kwargs["system"]}, {"role": "user", "content": kwargs["user"]}]})
        if "qwen" in self.model.casefold():
            payload["enable_thinking"] = kwargs.get("thinking", False)
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if kwargs.get("response_format"):
            payload["response_format"] = kwargs["response_format"]
        diagnostics = kwargs.get("diagnostics")
        for attempt in range(1, self.retries + 2):
            _check_budget(deadline, cancel)
            started, parts, usage = time.monotonic(), [], None
            entry = {"attempt": attempt, "streaming": True}
            try:
                request = Request(self.base_url, data=json.dumps(payload, ensure_ascii=False).encode(),
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
                timeout = min(self.timeout_seconds, max(.001, deadline-time.monotonic())) if deadline else self.timeout_seconds
                with open_modelscope_request(request, timeout=timeout, retry_handshake=self.tls_compatibility_retry) as response:
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type:
                        body = json.loads(response.read(2 * 1024 * 1024).decode())
                        message = _completion_message({"message": body["choices"][0].get("message", {}), "usage": body.get("usage")})
                        if body["choices"][0].get("finish_reason") not in {None, "stop"}:
                            raise ValueError("buffered model output did not finish normally")
                        _check_budget(deadline, cancel)
                        parts.append(message.content)
                        on_delta(message.content)
                        usage = message.usage
                        entry["provider_buffered"] = True
                    else:
                        ended = False
                        while True:
                            _check_budget(deadline, cancel)
                            line = response.readline(262145)
                            _check_budget(deadline, cancel)
                            if not line:
                                break
                            if len(line) > 262144:
                                raise ValueError("model stream frame exceeds limit")
                            if not line.startswith(b"data:"):
                                continue
                            frame = line[5:].strip()
                            if frame == b"[DONE]":
                                ended = True
                                break
                            if not frame:
                                continue
                            body = json.loads(frame)
                            if body.get("error"):
                                raise RuntimeError("model stream returned an error")
                            usage = body.get("usage") or usage
                            for choice in body.get("choices", []):
                                if choice.get("index", 0) != 0:
                                    continue
                                delta = choice.get("delta", {}).get("content")
                                if isinstance(delta, str) and delta:
                                    if not parts:
                                        entry["first_token_ms"] = round((time.monotonic()-started)*1000)
                                    parts.append(delta)
                                    if sum(map(len, parts)) > 100000:
                                        raise ValueError("model stream text exceeds limit")
                                    on_delta(delta)  # Never emit reasoning/tool-call deltas.
                                if choice.get("finish_reason"):
                                    reason = choice["finish_reason"]
                                    if reason != "stop":
                                        raise ValueError("model stream did not finish normally")
                                    ended = True
                        if not ended:
                            raise RuntimeError("model stream disconnected before completion")
                _check_budget(deadline, cancel)
                entry.update(status="ok", usage=_safe_usage(usage), content_chars=sum(map(len, parts)))
                return "".join(parts)
            except Exception as exc:
                entry.update(status="failed", error_code=type(exc).__name__)
                if parts or attempt > self.retries or not isinstance(exc, (HTTPError, URLError, TimeoutError, OSError)) or _is_client_error(exc):
                    raise
            finally:
                entry["latency_ms"] = round((time.monotonic()-started)*1000)
                if diagnostics is not None:
                    diagnostics.append(entry)
            _retry_pause(deadline, cancel)

    def generate_structured(
        self,
        model_type: type[T],
        *,
        system: str,
        context: dict[str, Any],
        temperature: float = 0.1,
        max_tokens: int = 1800,
        thinking: bool = False,
        diagnostics: list[dict[str, Any]] | None = None,
        cancel_event: Any = None,
        deadline: float | None = None,
        allow_format_fallback: bool = True,
    ) -> T:
        last_error: Exception | None = None
        user = json.dumps({"context": context, "output_schema": model_type.model_json_schema()}, ensure_ascii=False)
        mode, token_budget, attempt = self.structured_output_mode, max_tokens, 0
        retries_left = self.retries
        while True:
            _check_budget(deadline, cancel_event)
            attempt += 1
            self._completion_metadata.set(None)
            entry = {"attempt": attempt, "output_mode": mode, "max_tokens": token_budget}
            try:
                content = self.generate(
                    system=system, user=user, temperature=temperature,
                    max_tokens=token_budget, thinking=thinking,
                    response_format=_structured_format(mode, model_type),
                    deadline=deadline, cancel_event=cancel_event, _retry_transport=False,
                )
                message = self._completion_metadata.get()
                entry.update(content_chars=len(content), finish_reason=message.finish_reason if message else None,
                             usage=_safe_usage(message.usage) if message else None)
                if not content.strip():
                    entry["error_code"] = "empty_content"
                    raise ValueError("structured output is empty")
                if message and message.finish_reason in {"length", "max_tokens"}:
                    entry["error_code"] = "truncated_output"
                    raise ValueError("structured output reached token limit")
                result = model_type.model_validate(_load_json_content(content))
                entry["status"] = "ok"
                if diagnostics is not None:
                    diagnostics.append(entry)
                return result
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError, RuntimeError, HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
                entry.setdefault("error_code", "invalid_json" if isinstance(exc, json.JSONDecodeError)
                    else "schema_validation" if isinstance(exc, ValidationError) else type(exc).__name__)
                entry["status"] = "failed"
                if isinstance(exc, HTTPError):
                    entry["http_status"] = exc.code
                if diagnostics is not None:
                    diagnostics.append(entry)
                if allow_format_fallback and mode != "prompt" and _unsupported_output_format(exc):
                    mode = "json_object" if mode == "json_schema" else "prompt"
                    entry["error_code"] = "unsupported_output_format"
                    continue  # Each mode is tried at most once before normal retry.
                if _is_client_error(exc):
                    break
                if retries_left <= 0:
                    break
                retries_left -= 1
                if isinstance(exc, (HTTPError, URLError, TimeoutError, OSError)):
                    _retry_pause(deadline, cancel_event)
                if entry["error_code"] in {"empty_content", "truncated_output", "invalid_json"}:
                    token_budget = min(max(max_tokens, 4096), token_budget * 2)
                user += "\nPrevious output/request failed. Return one complete JSON object only; keep reasons and rewritten queries concise. Exactly match the schema."
        if last_error:
            self.last_error = f"{type(last_error).__name__}: {last_error}"
            raise last_error
        raise RuntimeError("structured generation failed")


def _check_budget(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("generation cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("generation budget exhausted")


def safe_error_details(exc):
    """Expose error classes/status codes, never provider bodies or credentials."""
    detail = {"code": type(exc).__name__}
    if isinstance(exc, HTTPError):
        detail["http_status"] = exc.code
    elif type(exc).__name__ == "HTTPStatusError":
        detail["http_status"] = exc.response.status_code
    reasons = {"Page left verified official domains": "official_domain_boundary",
        "Extract URL left verified official domains": "official_domain_boundary",
        "Official page exceeds 2 MiB": "page_size_limit",
        "Too many official page redirects": "redirect_limit",
        "structured output is empty": "empty_content", "structured output reached token limit": "truncated_output"}
    if isinstance(exc, ValueError) and exc.args and exc.args[0] in reasons:
        detail["reason"] = reasons[exc.args[0]]
    if isinstance(exc, BaseExceptionGroup):
        detail["causes"] = [safe_error_details(e) for e in exc.exceptions[:5]]
    return detail


def _retry_pause(deadline, cancel_event):
    _check_budget(deadline, cancel_event)
    delay = random.uniform(1, 2)
    if deadline is not None:
        if deadline - time.monotonic() <= delay:
            raise TimeoutError("insufficient budget for retry")
    if cancel_event is not None:
        cancel_event.wait(delay)
    else:
        time.sleep(delay)
    _check_budget(deadline, cancel_event)


def _strip_json_fence(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _completion_message(response: Any) -> CompletionMessage:
    """Accept live OpenAI responses and deliberately small test doubles."""
    if isinstance(response, str):
        return CompletionMessage(content=response)
    if not isinstance(response, dict):
        raise ValueError("completion response must be a string or object")
    if isinstance(response.get("choices"), list) and response["choices"]:
        choice = response["choices"][0]
        response = {"message": choice.get("message", {}), "finish_reason": choice.get("finish_reason"), "usage": response.get("usage")}
    message = response.get("message", response)
    if not isinstance(message, dict):
        raise ValueError("completion message must be an object")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ValueError("tool_calls must be a list")
    return CompletionMessage(content=str(message.get("content") or ""), tool_calls=calls,
                             finish_reason=response.get("finish_reason"), usage=response.get("usage"))


def _structured_format(mode: str, model_type: type[BaseModel]) -> dict | None:
    if mode == "prompt":
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    schema = model_type.model_json_schema()
    def strict(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            node.pop("default", None)
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)
    strict(schema)
    return {"type": "json_schema", "json_schema": {"name": model_type.__name__, "strict": True, "schema": schema}}


def _is_client_error(exc: Exception) -> bool:
    """A 4xx other than 408/429 will not succeed on an identical retry."""
    return isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code not in {408, 429}


def _unsupported_output_format(exc: Exception) -> bool:
    if not isinstance(exc, HTTPError) or exc.code not in {400, 422}:
        return False
    text = exc.read(4096).decode("utf-8", errors="replace").casefold()
    return any(x in text for x in ("response_format", "json_schema", "json_object")) and any(
        x in text for x in ("unsupported", "not supported", "unknown", "not allowed", "invalid"))


def _safe_usage(usage: Any) -> dict | None:
    if not isinstance(usage, dict):
        return None
    result = {k: v for k, v in usage.items() if k in {
        "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"} and type(v) is int}
    for key in ("completion_tokens_details", "prompt_tokens_details", "output_tokens_details", "input_tokens_details"):
        if isinstance(usage.get(key), dict):
            result[key] = {k: v for k, v in usage[key].items() if k in {"reasoning_tokens", "cached_tokens"} and type(v) is int}
    return result


def _load_json_content(content: str) -> Any:
    """Read the first complete JSON value without retaining model prose.

    Compatible gateways sometimes wrap JSON in a sentence/code fence or add a
    trailing comma.  Those are safe syntax repairs.  A genuinely truncated
    response still raises JSONDecodeError and is retried by the caller.
    """
    cleaned = _strip_json_fence(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original:
        decoder = json.JSONDecoder()
        variants = [cleaned]
        without_trailing_commas = _remove_json_trailing_commas(cleaned)
        if without_trailing_commas != cleaned:
            variants.append(without_trailing_commas)
        for candidate in variants:
            for index, char in enumerate(candidate):
                if char not in "[{":
                    continue
                try:
                    value, _ = decoder.raw_decode(candidate, index)
                    return value
                except json.JSONDecodeError:
                    continue
        raise original


def _remove_json_trailing_commas(value: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(value):
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            output.append(char)
            continue
        if char == ",":
            following = index + 1
            while following < len(value) and value[following].isspace():
                following += 1
            if following < len(value) and value[following] in "}]":
                continue
        output.append(char)
    return "".join(output)
