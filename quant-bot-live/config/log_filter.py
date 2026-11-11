"""Log sanitization filter to mask API keys, secrets, tokens, and passwords."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Set


class SensitiveDataFilter(logging.Filter):
    """Masks API keys, secrets, tokens, and passwords from log records."""

    PATTERNS = [
        re.compile(r"sk-or-v1-[a-zA-Z0-9_-]{10,}", re.IGNORECASE),
        re.compile(r"\b\d{8,12}:[a-zA-Z0-9_-]{30,}\b"),  # Telegram tokens
        re.compile(r"(api[_-]?secret|password|secret|api[_-]?key)\s*[:=]\s*['\"]?([^'\"\s,%]+)['\"]?", re.IGNORECASE),
    ]

    def __init__(self, sensitive_strings: Optional[Set[str]] = None) -> None:
        super().__init__()
        self.sensitive_strings = {s.strip() for s in (sensitive_strings or set()) if s and len(s.strip()) >= 6}

    def mask_text(self, text: str) -> str:
        for s in self.sensitive_strings:
            text = text.replace(s, "[MASKED_SECRET]")
        text = self.PATTERNS[0].sub("[MASKED_SECRET]", text)
        text = self.PATTERNS[1].sub("[MASKED_SECRET]", text)
        text = self.PATTERNS[2].sub(r"\1=[MASKED_SECRET]", text)
        return text

    def _mask_arg(self, a: Any) -> Any:
        return self.mask_text(a) if isinstance(a, str) else a

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.mask_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._mask_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._mask_arg(a) for a in record.args)
            elif isinstance(record.args, list):
                record.args = [self._mask_arg(a) for a in record.args]
        return True
