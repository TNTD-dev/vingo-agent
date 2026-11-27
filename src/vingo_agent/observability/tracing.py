"""Trace theo mục 9; nội dung và lỗi thô không được xuất ra ngoài."""

import logging
from contextlib import contextmanager
from urllib.parse import urlsplit

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

from vingo_agent.config import get_settings

ALLOWED_ATTRIBUTES = frozenset(
    {"turn_id", "action_id", "request_id", "role", "operation", "outcome", "error_code", "session.id"}
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_provider = TracerProvider()


def include_content(settings) -> bool:
    """Cờ dev OTEL_INCLUDE_CONTENT chỉ có hiệu lực khi trace gửi về localhost."""
    if not settings.otel_include_content:
        return False
    if urlsplit(settings.otel_exporter_endpoint).hostname in LOCAL_HOSTS:
        return True
    logging.getLogger(__name__).warning("OTEL_INCLUDE_CONTENT bị bỏ qua: endpoint không phải localhost")
    return False


class FilteringExporter(SpanExporter):
    """Bọc mọi exporter để loại exception event và status description."""

    def __init__(self, exporter):
        self.exporter = exporter

    def export(self, spans):
        safe = [
            ReadableSpan(
                name=s.name,
                context=s.context,
                parent=s.parent,
                resource=s.resource,
                attributes=s.attributes,
                events=[e for e in s.events if e.name != "exception"],
                links=s.links,
                kind=s.kind,
                status=Status(s.status.status_code),
                start_time=s.start_time,
                end_time=s.end_time,
                instrumentation_scope=s.instrumentation_scope,
            )
            for s in spans
        ]
        return self.exporter.export(safe)

    def shutdown(self):
        return self.exporter.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.exporter.force_flush(timeout_millis)


def configure_tracing():
    global _provider
    settings = get_settings()
    _provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "vingo-agent",
                "openinference.project.name": settings.otel_project_name,
            }
        )
    )
    if endpoint := settings.otel_exporter_endpoint.rstrip("/"):
        headers = {"Authorization": f"Bearer {settings.phoenix_api_key}"} if settings.phoenix_api_key else {}
        _provider.add_span_processor(
            BatchSpanProcessor(FilteringExporter(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)))
        )
    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=_provider,
            include_content=include_content(settings),
            include_binary_content=False,
        )
    )
    return _provider


@contextmanager
def span(name, **attrs):
    with _provider.get_tracer("vingo-agent").start_as_current_span(
        name,
        attributes={k: v for k, v in attrs.items() if k in ALLOWED_ATTRIBUTES},
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as exc:
            current.set_attribute("error_code", type(exc).__name__)
            current.set_status(StatusCode.ERROR)
            raise
