"""Events Arcade can't see: prompts, catalog changes, denials, calls blocked on authorization.

Tool calls themselves are recorded by Arcade (see executions.py); a `call` event only carries
the keys to find that record. Events go to OpenTelemetry when `otel` is on (extra `otel`,
configured by the standard OTEL_EXPORTER_OTLP_* variables), else to a JSONL file.
"""

import json
import time
from pathlib import Path
from typing import Any


class Audit:
    def __init__(self, path: Path, otel: bool = False):
        self.path = path
        self._provider = _otel_provider() if otel else None
        self._logger = self._provider.get_logger("arcade_tool_selector.audit") if self._provider else None

    def write(self, event: str, **fields: Any) -> None:
        if self._logger:
            attributes = {k: v for k, v in fields.items() if v is not None}
            self._logger.emit(event_name=f"ats.{event}", body=event, attributes=attributes)
            return
        line = json.dumps({"ts": round(time.time(), 3), "event": event, **fields}, default=str)
        with self.path.open("a") as f:
            f.write(line + "\n")

    def close(self) -> None:
        """Flush buffered OpenTelemetry records."""
        if self._provider:
            self._provider.shutdown()


def _otel_provider():
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    provider = LoggerProvider(resource=Resource.create({"service.name": "arcade-tool-selector"}))
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    return provider
