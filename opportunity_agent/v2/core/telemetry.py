"""Small, privacy-conscious OpenTelemetry boundary used by every V2 run."""
from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Iterator

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.propagate import inject, extract

_configured = False


def configure_telemetry() -> None:
    global _configured
    if not _configured:
        provider = trace.get_tracer_provider()
        # openJiuwen can initialize the global SDK first. Reuse it instead of
        # attaching an exporter to an unregistered provider (lost traces).
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "opportunity-agent")}))
            trace.set_tracer_provider(provider)
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if endpoint:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
        _configured = True


@contextmanager
def span(name: str, *, carrier=None, **attributes: object) -> Iterator[object]:
    """Never attach message bodies, resume text, tokens, or other secrets."""
    configure_telemetry()
    tracer = trace.get_tracer("opportunity-agent.v2")
    with tracer.start_as_current_span(name, context=extract(carrier) if carrier else None,
                                      record_exception=False, set_status_on_exception=False) as current:
        for key, value in attributes.items():
            if not any(sensitive in key.casefold() for sensitive in ("message", "content", "token", "resume", "api_key", "authorization", "query", "url")):
                current.set_attribute(key, str(value)[:200])
        try:
            yield current
        except BaseException as exc:
            mark_error(current, type(exc).__name__)
            raise


def mark_error(current, code):
    """Report handled failures too, without exception bodies or provider secrets."""
    current.set_attribute("error.code", code)
    current.set_status(Status(StatusCode.ERROR, code))
    current.add_event("exception", {"exception.type": code})


def trace_carrier():
    carrier = {}
    inject(carrier)
    return {k: v for k, v in carrier.items() if k in {"traceparent", "tracestate"}}
