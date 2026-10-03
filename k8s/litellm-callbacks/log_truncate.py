"""Truncate oversized log records before they reach stdout.

Problem: LiteLLM router/proxy exception logs embed entire SSE response
bodies in the exception message (350KB+ per record, e.g. the chatgpt
empty-output -> RateLimitError elevation path). At 10MB x 5 files container
log rotation this leaves ~20 minutes of log retention, destroying incident
forensics.

Fix: attach a logging.Filter to the LiteLLM loggers that caps any single
log record. Pure logging-layer change; request handling is untouched.

Activation: imported via the litellm callbacks list (module-level side
effect installs the filter in every worker process).
"""
import logging

_MAX = 4000


class _TruncateFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
            if len(msg) > _MAX:
                record.msg = msg[:_MAX] + (
                    " ...[log_truncate: %d chars dropped]" % (len(msg) - _MAX)
                )
                record.args = ()
        except Exception:
            pass
        return True


_filter = _TruncateFilter()
for _name in ("LiteLLM Router", "LiteLLM Proxy", "LiteLLM"):
    logging.getLogger(_name).addFilter(_filter)

print("[log_truncate] installed log record truncation filter (max %d chars)" % _MAX)

from litellm.integrations.custom_logger import CustomLogger


class LogTruncate(CustomLogger):
    """No-op callback: registration only forces module import per worker."""

    pass


log_truncate = LogTruncate()
