"""Error locations without exception messages, URLs, HTML or SQL parameter values."""

from pathlib import Path
from typing import Any


def failure_context(exc: BaseException) -> dict[str, Any]:
    trace = exc.__traceback__
    while trace and trace.tb_next:
        trace = trace.tb_next
    return {
        "exception_type": type(exc).__name__,
        "file": Path(trace.tb_frame.f_code.co_filename).name if trace else None,
        "line": trace.tb_lineno if trace else None,
    }
