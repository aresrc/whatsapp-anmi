"""Redacción conservadora de datos personales antes de persistir o usar IA."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TextoRedactado:
    texto: str
    tipos: tuple[str, ...]


_PATRONES = (
    ("CORREO", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b", re.I)),
    ("TELÉFONO", re.compile(r"(?<!\w)(?:\+?\d[\s.-]?){7,15}\d(?!\w)")),
    ("DOCUMENTO", re.compile(r"\b(?:dni|ce|documento)\s*[:#-]?\s*\d{7,12}\b", re.I)),
    ("TARJETA", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("URL", re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)),
    ("DIRECCIÓN", re.compile(r"\b(?:calle|jr\.?|jiron|avenida|av\.?|urb\.?|mz\.?|lote)\s+[^,.;\n]{2,80}", re.I)),
    (
        "NOMBRE",
        re.compile(
            r"\b(?i:me llamo|mi nombre es|soy)\s+"
            r"[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ'-]*"
            r"(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ'-]*){0,3}"
        ),
    ),
)


def redactar_datos_personales(texto: str) -> TextoRedactado:
    """Sustituye PII frecuente sin borrar datos clínicos o alimentos útiles."""
    if not isinstance(texto, str):
        raise TypeError("texto debe ser str")
    tipos: list[str] = []
    resultado = texto
    for tipo, patron in _PATRONES:
        resultado, reemplazos = patron.subn(f"[{tipo}]", resultado)
        if reemplazos:
            tipos.append(tipo)
    resultado = re.sub(r"\s+", " ", resultado).strip()
    return TextoRedactado(resultado, tuple(tipos))
