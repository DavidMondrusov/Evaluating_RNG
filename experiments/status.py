"""Phase timing log for long experiment runs."""

import time
from contextlib import contextmanager


class StatusLog:
    def __init__(self, path=None):
        self.path = path
        self.started = time.monotonic()
        if path:
            self._handle = path.open("a", encoding="utf-8")
        else:
            self._handle = None

    def note(self, message):
        line = f"[{time.monotonic() - self.started:7.1f}s] {message}"
        print(line, flush=True)
        if self._handle:
            self._handle.write(line + "\n")
            self._handle.flush()

    @contextmanager
    def phase(self, label, **fields):
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        self.note(f"start {label}" + (f" ({detail})" if detail else ""))
        began = time.monotonic()
        try:
            yield
        except BaseException:
            self.note(f"fail  {label} after {time.monotonic() - began:.1f}s")
            raise
        self.note(f"done  {label} in {time.monotonic() - began:.1f}s")

    def close(self):
        if self._handle:
            self._handle.close()
            self._handle = None
