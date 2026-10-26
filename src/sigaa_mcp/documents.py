"""Arquivos baixados do SIGAA: salvar no disco e extrair texto de PDFs."""

from __future__ import annotations

import io
import mimetypes
from pathlib import Path

from pypdf import PdfReader


def save(name: str, data: bytes, ctype: str, folder: Path) -> Path:
    """Salva o arquivo sem sobrescrever outro de mesmo nome."""
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(name).name or "arquivo"
    if not Path(name).suffix:
        name += mimetypes.guess_extension(ctype.split(";")[0].strip()) or ""
    path = folder / name
    n = 1
    while path.exists():
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    path.write_bytes(data)
    return path


def is_pdf(data: bytes, ctype: str) -> bool:
    return "pdf" in ctype.lower() or data[:5] == b"%PDF-"


def pdf_text(data: bytes, max_chars: int) -> str:
    """Texto do PDF (histórico, boletim, declarações...), página a página."""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = [
            f"--- página {i} ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages, 1)
        ]
    except Exception as e:  # PDF corrompido ou criptografado
        return f"[não foi possível extrair o texto do PDF: {e}]"
    text = "\n\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncado em {max_chars} caracteres]"
    return text
