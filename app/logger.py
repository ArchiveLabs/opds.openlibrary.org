from __future__ import annotations

import logging
import os
import sys

# Worker label shown in every log line. Defaults to the PID until a friendly
# ordinal ("1", "2", …) is assigned at startup (see app/main.py).
_worker_label: str = f"pid{os.getpid()}"


def set_worker_label(label: str | int) -> None:
    """Set the short worker label used in log lines (e.g. ``"1"``)."""
    global _worker_label
    _worker_label = str(label)


class _WorkerFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.worker = _worker_label
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  [worker %(worker)s]  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handler.addFilter(_WorkerFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
