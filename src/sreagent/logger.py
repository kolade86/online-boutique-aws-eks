"""JSON logging to stdout, in the same shape as the other Python services.

Each Python service keeps its own copy of this module (see emailservice and
recommendationservice); there is no shared library between services.
"""

import logging
import sys

from pythonjsonlogger.json import JsonFormatter


class CustomJsonFormatter(JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        if not log_record.get("timestamp"):
            log_record["timestamp"] = record.created
        if log_record.get("severity"):
            log_record["severity"] = log_record["severity"].upper()
        else:
            log_record["severity"] = record.levelname


def configure(level=logging.INFO):
    """Send every `sreagent.*` logger to stdout as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CustomJsonFormatter("%(timestamp)s %(severity)s %(name)s %(message)s"))
    root = logging.getLogger("sreagent")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
