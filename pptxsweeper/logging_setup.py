"""Structured JSON-lines logging with per-stage rotating files."""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("url", "domain", "stage", "batch_id", "sha256", "status", "detail"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(stage: str, logs_dir: Path, level: str = "INFO",
                  json_lines: bool = True, rotate_max_bytes: int = 50 * 2**20,
                  rotate_backup_count: int = 10) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Reset handlers so repeated setup in tests doesn't duplicate output.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / f"{stage}.jsonl", maxBytes=rotate_max_bytes,
        backupCount=rotate_backup_count, encoding="utf-8",
    )
    file_handler.setFormatter(
        JsonLineFormatter() if json_lines
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    return logging.getLogger(f"pptxsweeper.{stage}")
