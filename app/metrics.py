"""Prometheus metrics on the default registry. Defined once at import; import this module, never redefine."""
from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

tts_queue_length = Gauge("tts_queue_length", "Items waiting in the TTS queue")
tts_overlay_clients = Gauge("tts_overlay_clients", "Connected overlay WebSocket clients")

tts_events_received_total = Counter(
    "tts_events_received_total", "Kick webhook events accepted (signature ok, not duplicate)", ["type"]
)
tts_items_enqueued_total = Counter("tts_items_enqueued_total", "Items added to the queue", ["kind"])
tts_items_dropped_total = Counter("tts_items_dropped_total", "Items dropped before or after enqueue", ["reason"])
tts_webhook_rejected_total = Counter("tts_webhook_rejected_total", "Webhook requests rejected", ["reason"])

tts_reader_latency_seconds = Histogram(
    "tts_reader_latency_seconds",
    "Reader (LLM or rules) latency",
    ["backend"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 5.0),
)
tts_reader_fallback_total = Counter("tts_reader_fallback_total", "LLM reader fell back to rules", ["reason"])

tts_synth_latency_seconds = Histogram(
    "tts_synth_latency_seconds",
    "Engine synth latency",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0),
)
tts_failures_total = Counter("tts_failures_total", "Unexpected failures by pipeline stage", ["stage"])


def get_value(metric, **labels) -> float:
    """Test helper: current value of a Counter/Gauge (or the sample count of a Histogram) from REGISTRY."""
    name = metric._name
    kind = metric._type
    if kind == "counter":
        sample = f"{name}_total"
    elif kind == "histogram":
        sample = f"{name}_count"
    else:
        sample = name
    value = REGISTRY.get_sample_value(sample, labels or None)
    return float(value) if value is not None else 0.0
