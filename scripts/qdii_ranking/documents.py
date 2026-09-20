"""PDF text extraction used by legal documents, reports, and quota notices."""

from __future__ import annotations

import io

from .errors import DataError

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise SystemExit(
        "Missing dependency: pypdf. Run this script with the Codex bundled Python runtime."
    ) from exc


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf exposes several parser-specific exceptions
        raise DataError(f"Could not parse PDF: {exc}") from exc
    if not text.strip():
        raise DataError("PDF contains no extractable text")
    return text


__all__ = ["extract_pdf_text"]
