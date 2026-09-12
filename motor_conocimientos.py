"""Motor de conocimiento basado en reglas para las consultas de ANMI.

El módulo mantiene deliberadamente separadas la carga de contenido revisado y
la clasificación. La clasificación no genera texto: únicamente selecciona una
regla del CSV cuando existe evidencia suficiente en el mensaje actual.
"""

from __future__ import annotations

import csv
import logging
import math
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ReglaConocimiento:
    """Contenido revisado y metadatos de una fila del CSV."""

    categoria: str
    subcategoria: str
    palabras_clave: tuple[str, ...]
    respuesta: str
    disclaimer: str
    paginas: str = ""
    documento: str = ""
    enlace: str = ""
    id_regla: str = ""
    ingredientes_receta: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngredienteReceta:
    """Ingrediente normalizado usado para comparar gustos y recetas."""

    clave: str
    nombre: str
    grupo: str
    alias: tuple[str, ...]


@dataclass(frozen=True)
class PerfilAlimentario:
    """Preferencias declaradas por la persona cuidadora."""

    aceptados: tuple[str, ...]
    rechazados: tuple[str, ...]
    excluidos: tuple[str, ...]

    @property
    def reconocido(self) -> bool:
        return bool(self.aceptados or self.rechazados or self.excluidos)


@dataclass(frozen=True)
class PerfilReceta:
    """Relación controlada entre una receta revisada y sus ingredientes."""

    regla: ReglaConocimiento
    ingredientes: tuple[str, ...]


@dataclass(frozen=True)
class ResultadoReceta:
    """Resultado del ranking local de recetas compatibles con la edad."""

    perfil_receta: PerfilReceta
    puntaje: float
    aceptados: tuple[str, ...]
    rechazados: tuple[str, ...]
    desconocidos: tuple[str, ...]

    @property
    def regla(self) -> ReglaConocimiento:
        return self.perfil_receta.regla


@dataclass(frozen=True)
class CoincidenciaAproximada:
    """Corrección ortográfica conservadora aplicada a un término del mensaje."""

    original: str
    corregida: str
    distancia: int
    similitud: float


@dataclass(frozen=True)
class CandidatoBusqueda:
    """Resumen de un candidato alternativo útil para diagnóstico y simulación."""

    categoria: str
    subcategoria: str
    puntaje: float
    coincidencias_exactas: tuple[str, ...]
    coincidencias_aproximadas: tuple[CoincidenciaAproximada, ...]


@dataclass(frozen=True)
class ResultadoBusqueda:
    """Resultado aceptado por el motor junto con su evidencia y confianza."""

    regla: ReglaConocimiento
    puntaje: float
    margen: float
    coincidencias_exactas: tuple[str, ...]
    coincidencias_aproximadas: tuple[CoincidenciaAproximada, ...]
    motivo: str
    candidatos_descartados: tuple[CandidatoBusqueda, ...] = ()

    # Estas propiedades conservan una compatibilidad razonable con el retorno
    # anterior, que era directamente una ``ReglaConocimiento``.
    @property
    def categoria(self) -> str:
        """Categoría de la regla seleccionada."""

        return self.regla.categoria

    @property
    def subcategoria(self) -> str:
        """Subcategoría de la regla seleccionada."""

        return self.regla.subcategoria

    @property
    def respuesta(self) -> str:
        """Respuesta revisada de la regla seleccionada."""

        return self.regla.respuesta

    @property
    def disclaimer(self) -> str:
        """Disclaimer obligatorio de la regla seleccionada."""

        return self.regla.disclaimer

    @property
    def paginas(self) -> str:
        """Páginas de la fuente asociada."""

        return self.regla.paginas

    @property
    def documento(self) -> str:
        """Documento fuente asociado."""

        return self.regla.documento

    @property
    def enlace(self) -> str:
        """Enlace de consulta asociado."""

        return self.regla.enlace

    @property
    def coincidencias_fuzzy(self) -> tuple[CoincidenciaAproximada, ...]:
        """Alias legible para integraciones que denominan *fuzzy* al ajuste."""

        return self.coincidencias_aproximadas

    @property
    def margen_porcentual(self) -> float:
        """Margen frente al segundo candidato expresado de 0 a 100."""

        return self.margen * 100


RUTA_CSV = (
    Path(__file__).resolve().parent
    / "data"
    / "motor_conocimientos_v2.csv"
)

COLUMNA_CATEGORIA = "Categoría"
COLUMNA_ID = "ID"
COLUMNA_SUBCATEGORIA = "Subcategoría"
COLUMNA_PALABRAS = "Palabras Clave (Para el bot)"
COLUMNA_RESPUESTA = (
    "Respuesta Verificada (Lenguaje sencillo para el chat)"
)
COLUMNA_DISCLAIMER = "Disclaimer Obligatorio (¡Crítico!)"
COLUMNA_PAGINAS = "Paginas"
COLUMNA_DOCUMENTO = "Documento"
COLUMNA_ENLACE = "Enlace"
COLUMNA_INGREDIENTES_RECETA = "Ingredientes de la receta"

MARGEN_MINIMO = 0.12
PUNTAJE_MINIMO = 6.0
IDF_TERMINO_ESPECIFICO = 4.5
MAXIMO_CANDIDATOS_DESCARTADOS = 5

_CONECTORES = frozenset(
    {
        "a",
        "al",
        "algo",
        "como",
        "con",
        "cual",
        "cuando",
        "de",
        "del",
        "donde",
        "e",
        "el",
        "ella",
        "en",
        "es",
        "esa",
        "ese",
        "eso",
        "esta",
        "este",
        "esto",
        "la",
        "las",
        "le",
        "les",
        "lo",
        "los",
        "me",
        "mi",
        "mis",
        "no",
        "o",
        "para",
        "pero",
        "por",
        "porque",
        "que",
        "se",
        "si",
        "sin",
        "son",
        "su",
        "sus",
        "te",
        "tu",
        "tus",
        "u",
        "un",
        "una",
        "unas",
        "unos",
        "y",
        "ya",
        "yo",
    }
)

# Aunque aparezcan pocas veces en el CSV, aisladas no expresan una intención
# suficientemente segura. Sí pueden sumar como una segunda evidencia.
_TERMINOS_NO_ESPECIFICOS = _CONECTORES | frozenset(
    {
        "alta",
        "antes",
        "baja",
        "bebe",
        "bueno",
        "comer",
        "comida",
        "cuanto",
        "darle",
        "despues",
        "fuerte",
        "gracias",
        "hijo",
        "hola",
        "hoy",
        "mucho",
        "nada",
        "nino",
        "puede",
        "puedo",
        "quiero",
        "sirve",
        "tiene",
        "tomar",
    }
)

# Una coincidencia aislada solo puede activar prioridad de emergencia cuando
# el propio término describe inequívocamente una alarma. El resto de reglas de
# emergencia conserva el requisito general de dos evidencias actuales.
_TERMINOS_EMERGENCIA_ESPECIFICOS = frozenset(
    {
        "convulsion",
        "desmayado",
        "golpea",
        "maltrato",
        "morado",
        "sangrando",
        "violencia",
    }
)

_SENALES_EMERGENCIA = frozenset(
    {
        "alta",
        "calentura",
        "convulsion",
        "desmayado",
        "despierta",
        "diarrea",
        "diarreas",
        "fiebre",
        "fuerte",
        "golpea",
        "lacta",
        "liquido",
        "lucecitas",
        "maltrato",
        "morado",
        "mueve",
        "orina",
        "pierdo",
        "rechaza",
        "respira",
        "sangrado",
        "sangrando",
        "sangre",
        "seca",
        "vaginal",
        "violencia",
        "vientre",
        "volando",
        "vomitar",
        "zumbido",
    }
)


@dataclass(frozen=True)
class _IndiceMotor:
    """Índice inmutable derivado de un conjunto de reglas."""

    terminos_por_regla: tuple[frozenset[str], ...]
    etiquetas_por_regla: tuple[frozenset[str], ...]
    idf: Mapping[str, float]
    frecuencia: Mapping[str, int]
    vocabulario: tuple[str, ...]
    emergencias: tuple[bool, ...]


@dataclass(frozen=True)
class _CandidatoEvaluado:
    """Candidato interno con los datos necesarios para ordenar y filtrar."""

    indice_regla: int
    puntaje: float
    exactas: tuple[str, ...]
    aproximadas: tuple[CoincidenciaAproximada, ...]
    termino_especifico: bool
    emergencia: bool


def normalizar_texto(texto: str) -> str:
    """Normaliza mayúsculas, tildes, puntuación, espacios y alias mínimos."""

    if not isinstance(texto, str):
        return ""

    normalizado = unicodedata.normalize("NFD", texto.lower().strip())
    normalizado = "".join(
        caracter
        for caracter in normalizado
        if unicodedata.category(caracter) != "Mn"
    )
    normalizado = re.sub(r"[^a-z0-9\s]", " ", normalizado)
    normalizado = re.sub(r"\s+", " ", normalizado).strip()

    # Alias de escritura frecuentes y patrones mínimos de intención.
    normalizado = re.sub(r"\bbb\b", "bebe", normalizado)
    normalizado = re.sub(r"\bque\s+es\b", "definicion", normalizado)
    normalizado = re.sub(
        r"\bque\s+significa\b",
        "significa",
        normalizado,
    )

    return re.sub(r"\s+", " ", normalizado).strip()


def tokenizar(texto: str) -> tuple[str, ...]:
    """Devuelve tokens normalizados, en orden y sin duplicados."""

    tokens = normalizar_texto(texto).split()
    return tuple(dict.fromkeys(tokens))


def _quitar_comillas_externas(texto: str) -> str:
    """Retira como máximo una pareja de comillas externas accidentales."""

    limpio = texto.strip()
    if len(limpio) >= 2 and limpio[0] == limpio[-1] == '"':
        return limpio[1:-1]
    return limpio


def separar_palabras_clave(celda: str) -> tuple[str, ...]:
    """Separa la lista de términos delimitada por comas del CSV."""

    if not celda or not celda.strip():
        return ()

    palabras: list[str] = []
    for fragmento in celda.split(","):
        palabra = _quitar_comillas_externas(fragmento.strip())
        if palabra:
            palabras.append(palabra)
    return tuple(palabras)


def separar_ingredientes_receta(celda: str) -> tuple[str, ...]:
    """Lee claves canónicas de receta separadas por ``|`` desde el CSV."""
    if not celda or not celda.strip():
        return ()
    ingredientes = tuple(parte.strip() for parte in celda.split("|") if parte.strip())
    if len(ingredientes) != len(set(ingredientes)):
        raise ValueError("La receta contiene ingredientes duplicados")
    return ingredientes


def cargar_motor_conocimientos(
    ruta_csv: str | Path = RUTA_CSV,
) -> tuple[ReglaConocimiento, ...]:
    """Carga y valida las reglas desde el CSV v2 codificado como UTF-8 BOM."""

    ruta = Path(ruta_csv)
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el archivo CSV: {ruta}")

    columnas_requeridas = {
        COLUMNA_ID,
        COLUMNA_CATEGORIA,
        COLUMNA_SUBCATEGORIA,
        COLUMNA_PALABRAS,
        COLUMNA_RESPUESTA,
        COLUMNA_DISCLAIMER,
        COLUMNA_PAGINAS,
        COLUMNA_DOCUMENTO,
        COLUMNA_ENLACE,
        COLUMNA_INGREDIENTES_RECETA,
    }
    reglas: list[ReglaConocimiento] = []
    ids_vistos: set[str] = set()

    with ruta.open(mode="r", encoding="utf-8-sig", newline="") as archivo:
        lector = csv.DictReader(archivo)
        columnas_encontradas = set(lector.fieldnames or ())
        faltantes = columnas_requeridas - columnas_encontradas
        if faltantes:
            raise ValueError(
                "Faltan columnas obligatorias en el CSV: "
                + ", ".join(sorted(faltantes))
            )

        for numero_fila, fila in enumerate(lector, start=2):
            id_regla = (fila.get(COLUMNA_ID, "") or "").strip()
            palabras_clave = separar_palabras_clave(
                fila.get(COLUMNA_PALABRAS, "") or ""
            )
            respuesta = _quitar_comillas_externas(
                fila.get(COLUMNA_RESPUESTA, "") or ""
            )
            disclaimer = _quitar_comillas_externas(
                fila.get(COLUMNA_DISCLAIMER, "") or ""
            )
            ingredientes_receta = separar_ingredientes_receta(
                fila.get(COLUMNA_INGREDIENTES_RECETA, "") or ""
            )

            if not re.fullmatch(r"ANMI-\d{4}", id_regla):
                raise ValueError(
                    f"La fila {numero_fila} tiene un ID inválido: {id_regla!r}"
                )
            if id_regla in ids_vistos:
                raise ValueError(f"ID duplicado en el CSV: {id_regla}")
            ids_vistos.add(id_regla)

            es_receta = normalizar_texto(
                (fila.get(COLUMNA_CATEGORIA, "") or "")
            ).startswith("recetas minsa")
            if es_receta and not ingredientes_receta:
                raise ValueError(
                    f"La receta {id_regla} no tiene ingredientes en el CSV"
                )
            if not es_receta and ingredientes_receta:
                raise ValueError(
                    f"La regla {id_regla} no es receta y tiene ingredientes"
                )
            desconocidos = [
                clave
                for clave in ingredientes_receta
                if clave not in _INGREDIENTES_POR_CLAVE
            ]
            if desconocidos:
                raise ValueError(
                    f"La receta {id_regla} tiene ingredientes desconocidos: "
                    + ", ".join(desconidos)
                )

            if not palabras_clave:
                logging.warning(
                    "Fila %s ignorada: no contiene palabras clave",
                    numero_fila,
                )
                continue
            if not respuesta:
                logging.warning(
                    "Fila %s ignorada: no contiene respuesta",
                    numero_fila,
                )
                continue

            reglas.append(
                ReglaConocimiento(
                    categoria=(fila.get(COLUMNA_CATEGORIA, "") or "").strip(),
                    subcategoria=(
                        fila.get(COLUMNA_SUBCATEGORIA, "") or ""
                    ).strip(),
                    palabras_clave=palabras_clave,
                    respuesta=respuesta,
                    disclaimer=disclaimer,
                    paginas=(fila.get(COLUMNA_PAGINAS, "") or "").strip(),
                    documento=(fila.get(COLUMNA_DOCUMENTO, "") or "").strip(),
                    enlace=(fila.get(COLUMNA_ENLACE, "") or "").strip(),
                    id_regla=id_regla,
                    ingredientes_receta=ingredientes_receta,
                )
            )

    logging.info("Motor cargado: %s reglas desde %s", len(reglas), ruta.name)
    return tuple(reglas)


def contiene_frase(texto_normalizado: str, frase_normalizada: str) -> bool:
    """Comprueba que una palabra o frase aparezca con límites completos."""

    patron = rf"(?<!\w){re.escape(frase_normalizada)}(?!\w)"
    return re.search(patron, texto_normalizado) is not None


def calcular_puntaje(
    texto_normalizado: str,
    palabra_clave: str,
    *,
    idf: float = 1.0,
) -> float:
    """Calcula el aporte exacto de una clave con la fórmula ``3 × idf``."""

    clave = normalizar_texto(palabra_clave)
    texto = normalizar_texto(texto_normalizado)
    if not clave or not contiene_frase(texto, clave):
        return 0.0
    return 3.0 * max(idf, 0.0)


@lru_cache(maxsize=8)
def _crear_indice(
    reglas: tuple[ReglaConocimiento, ...],
) -> _IndiceMotor:
    """Construye y memoriza frecuencias, IDF, etiquetas y tipos de regla."""

    terminos_por_regla: list[frozenset[str]] = []
    etiquetas_por_regla: list[frozenset[str]] = []
    frecuencia: Counter[str] = Counter()
    emergencias: list[bool] = []

    for regla in reglas:
        terminos = frozenset(
            token
            for palabra in regla.palabras_clave
            for token in tokenizar(palabra)
            if token
        )
        terminos_por_regla.append(terminos)
        frecuencia.update(terminos)

        etiqueta = frozenset(
            tokenizar(f"{regla.categoria} {regla.subcategoria}")
        )
        etiquetas_por_regla.append(etiqueta)
        emergencias.append(
            "emergencia" in normalizar_texto(regla.categoria)
        )

    total = len(reglas)
    idf = {
        termino: math.log((total + 1) / (apariciones + 1)) + 1
        for termino, apariciones in frecuencia.items()
    }
    return _IndiceMotor(
        terminos_por_regla=tuple(terminos_por_regla),
        etiquetas_por_regla=tuple(etiquetas_por_regla),
        idf=idf,
        frecuencia=dict(frecuencia),
        vocabulario=tuple(sorted(frecuencia)),
        emergencias=tuple(emergencias),
    )


def _distancia_levenshtein(
    origen: str,
    destino: str,
    limite: int,
) -> int:
    """Calcula Levenshtein y abandona temprano cuando excede ``limite``."""

    if origen == destino:
        return 0
    if abs(len(origen) - len(destino)) > limite:
        return limite + 1
    if len(origen) > len(destino):
        origen, destino = destino, origen

    anterior = list(range(len(origen) + 1))
    for indice_destino, caracter_destino in enumerate(destino, start=1):
        actual = [indice_destino]
        minimo_fila = indice_destino
        for indice_origen, caracter_origen in enumerate(origen, start=1):
            coste = 0 if caracter_origen == caracter_destino else 1
            valor = min(
                actual[-1] + 1,
                anterior[indice_origen] + 1,
                anterior[indice_origen - 1] + coste,
            )
            actual.append(valor)
            minimo_fila = min(minimo_fila, valor)
        if minimo_fila > limite:
            return limite + 1
        anterior = actual
    return anterior[-1]


def _corregir_token(
    token: str,
    indice: _IndiceMotor,
) -> CoincidenciaAproximada | None:
    """Busca una corrección global solo si es ortográfica y no ambigua."""

    if (
        token in indice.idf
        or token.isdigit()
        or token in _TERMINOS_NO_ESPECIFICOS
        or len(token) < 4
    ):
        return None

    limite = 1 if len(token) <= 5 else 2
    candidatos: list[tuple[str, int, float]] = []
    for termino in indice.vocabulario:
        if (
            termino.isdigit()
            or termino in _CONECTORES
            or len(termino) < 4
            or abs(len(token) - len(termino)) > limite
        ):
            continue
        distancia = _distancia_levenshtein(token, termino, limite)
        if distancia > limite:
            continue
        similitud = 1.0 - distancia / max(len(token), len(termino))
        if similitud >= 0.80:
            candidatos.append((termino, distancia, similitud))

    if not candidatos:
        return None

    distancia_minima = min(candidato[1] for candidato in candidatos)
    empatados = [
        candidato
        for candidato in candidatos
        if candidato[1] == distancia_minima
    ]
    empatados.sort(
        key=lambda candidato: (
            -indice.frecuencia[candidato[0]],
            candidato[0],
        )
    )

    if len(empatados) > 1:
        frecuencia_primera = indice.frecuencia[empatados[0][0]]
        frecuencia_segunda = indice.frecuencia[empatados[1][0]]
        if frecuencia_primera < 2 * frecuencia_segunda:
            return None

    corregida, distancia, similitud = empatados[0]
    return CoincidenciaAproximada(
        original=token,
        corregida=corregida,
        distancia=distancia,
        similitud=similitud,
    )


def _corregir_tokens(
    tokens: frozenset[str],
    indice: _IndiceMotor,
) -> tuple[CoincidenciaAproximada, ...]:
    """Corrige tokens únicos con un vocabulario global compartido."""

    correcciones = (
        correccion
        for token in sorted(tokens)
        if (correccion := _corregir_token(token, indice)) is not None
    )
    return tuple(correcciones)


def _extraer_meses_explicitos(
    texto: str,
    correcciones: Sequence[CoincidenciaAproximada] = (),
) -> int | None:
    """Extrae una edad explícita en meses o años del mensaje actual."""

    normalizado = normalizar_texto(texto)
    for correccion in correcciones:
        if correccion.corregida in {"mes", "meses", "ano", "anos"}:
            normalizado = re.sub(
                rf"\b{re.escape(correccion.original)}\b",
                correccion.corregida,
                normalizado,
            )
    patron_meses = re.search(
        r"\b(\d{1,3})\s*(?:mes|meses|m)\b",
        normalizado,
    )
    if patron_meses:
        return int(patron_meses.group(1))

    patron_anos = re.search(
        r"\b(\d{1,2})\s*(?:ano|anos)\b",
        normalizado,
    )
    if patron_anos:
        return int(patron_anos.group(1)) * 12
    return None


def _rango_etario(categoria: str) -> tuple[int, int] | None:
    """Obtiene rangos como ``6-8m`` presentes en categorías etarias."""

    normalizada = normalizar_texto(categoria)
    coincidencia = re.search(
        r"\b(\d{1,2})\s+(\d{1,2})\s*m\b",
        normalizada,
    )
    if not coincidencia:
        return None
    return int(coincidencia.group(1)), int(coincidencia.group(2))


def _es_categoria_alimentaria(categoria: str) -> bool:
    """Indica si el contexto de alimentos es pertinente para la categoría."""

    normalizada = normalizar_texto(categoria)
    return normalizada.startswith(
        ("alimentacion", "alimentos", "recetas", "absorcion")
    )


def _es_termino_especifico(termino: str, idf: float) -> bool:
    """Decide si una sola evidencia puede identificar una intención."""

    return (
        len(termino) >= 4
        and termino not in _TERMINOS_NO_ESPECIFICOS
        and idf >= IDF_TERMINO_ESPECIFICO
    )


def _normalizar_alimentos(
    alimentos_contexto: str | Sequence[str] | None,
) -> frozenset[str]:
    """Convierte el perfil alimentario temporal en tokens comparables."""

    if alimentos_contexto is None:
        return frozenset()
    if isinstance(alimentos_contexto, str):
        return frozenset(tokenizar(alimentos_contexto))
    return frozenset(
        token
        for alimento in alimentos_contexto
        for token in tokenizar(str(alimento))
    )


def _normalizar_rango_edad(
    rango_edad_bebe: str | None,
) -> tuple[int, int] | None:
    return {
        "6-8": (6, 8),
        "9-11": (9, 11),
        "12-23": (12, 23),
        "6-12": (6, 12),
        "12-24": (12, 24),
        "24-36": (24, 36),
    }.get(rango_edad_bebe)


def _evaluar_candidato(
    numero: int,
    regla: ReglaConocimiento,
    indice: _IndiceMotor,
    tokens_mensaje: frozenset[str],
    correcciones: tuple[CoincidenciaAproximada, ...],
    categoria_anterior: str | None,
    meses: int | None,
    rango_edad: tuple[int, int] | None,
    alimentos: frozenset[str],
) -> _CandidatoEvaluado | None:
    """Puntúa una regla y descarta las que carecen de evidencia actual."""

    terminos = indice.terminos_por_regla[numero]
    exactas = tuple(sorted(terminos & tokens_mensaje))

    # Una clave corregida cuenta una sola vez aunque dos errores del mensaje
    # terminaran excepcionalmente en la misma palabra del vocabulario.
    aproximadas_por_destino: dict[str, CoincidenciaAproximada] = {}
    for correccion in correcciones:
        if correccion.corregida in terminos and correccion.corregida not in exactas:
            existente = aproximadas_por_destino.get(correccion.corregida)
            if existente is None or correccion.distancia < existente.distancia:
                aproximadas_por_destino[correccion.corregida] = correccion
    aproximadas = tuple(
        aproximadas_por_destino[termino]
        for termino in sorted(aproximadas_por_destino)
    )

    terminos_coincidentes = set(exactas) | set(aproximadas_por_destino)
    termino_especifico = any(
        _es_termino_especifico(termino, indice.idf[termino])
        for termino in terminos_coincidentes
    )
    if len(terminos_coincidentes) < 2 and not termino_especifico:
        return None
    if (
        indice.emergencias[numero]
        and len(terminos_coincidentes) == 1
        and not (
            terminos_coincidentes
            & _TERMINOS_EMERGENCIA_ESPECIFICOS
        )
    ):
        return None
    if (
        indice.emergencias[numero]
        and not (terminos_coincidentes & _SENALES_EMERGENCIA)
    ):
        return None

    puntaje_base = sum(3.0 * indice.idf[termino] for termino in exactas)
    puntaje_base += sum(
        3.0
        * indice.idf[correccion.corregida]
        * correccion.similitud
        * 0.80
        for correccion in aproximadas
    )
    if puntaje_base <= 0:
        return None

    cobertura = len(terminos_coincidentes) / max(len(terminos), 1)
    bono_cobertura = puntaje_base * 0.20 * cobertura

    tokens_efectivos = tokens_mensaje | frozenset(
        correccion.corregida for correccion in correcciones
    )
    etiquetas_coincidentes = (
        indice.etiquetas_por_regla[numero]
        & tokens_efectivos
        - _CONECTORES
    )
    bono_etiquetas = puntaje_base * min(
        0.10,
        0.03 * len(etiquetas_coincidentes),
    )

    bono_contexto = 0.0
    if (
        categoria_anterior
        and normalizar_texto(categoria_anterior)
        == normalizar_texto(regla.categoria)
    ):
        bono_contexto += puntaje_base * 0.05

    if alimentos and _es_categoria_alimentaria(regla.categoria):
        alimentos_coincidentes = alimentos & terminos
        bono_contexto += puntaje_base * min(
            0.15,
            0.05 * len(alimentos_coincidentes),
        )

    bono_edad = 0.0
    rango = _rango_etario(regla.categoria)
    if meses is not None and rango is not None:
        minimo, maximo = rango
        bono_edad = puntaje_base * (
            0.10 if minimo <= meses <= maximo else -0.20
        )
    elif rango_edad is not None and rango is not None:
        minimo, maximo = rango
        rango_minimo, rango_maximo = rango_edad
        coincide = minimo <= rango_maximo and rango_minimo <= maximo
        bono_edad = puntaje_base * (0.10 if coincide else -0.20)

    return _CandidatoEvaluado(
        indice_regla=numero,
        puntaje=(
            puntaje_base
            + bono_cobertura
            + bono_etiquetas
            + bono_contexto
            + bono_edad
        ),
        exactas=exactas,
        aproximadas=aproximadas,
        termino_especifico=termino_especifico,
        emergencia=indice.emergencias[numero],
    )


def buscar_mejor_regla(
    texto_usuario: str,
    reglas: Sequence[ReglaConocimiento],
    *,
    categoria_anterior: str | None = None,
    meses_bebe: int | None = None,
    rango_edad_bebe: str | None = None,
    alimentos_contexto: str | Sequence[str] | None = None,
) -> ResultadoBusqueda | None:
    """Selecciona una regla solo cuando el puntaje y el margen son seguros.

    El contexto nunca crea evidencia: únicamente ajusta el puntaje de reglas
    que ya coincidieron con el mensaje actual. Una edad escrita en el mensaje
    prevalece sobre ``meses_bebe``.
    """

    tokens_mensaje = frozenset(tokenizar(texto_usuario))
    if not tokens_mensaje or not reglas:
        return None

    reglas_inmutables = tuple(reglas)
    indice = _crear_indice(reglas_inmutables)
    correcciones = _corregir_tokens(tokens_mensaje, indice)
    meses_explicitos = _extraer_meses_explicitos(
        texto_usuario,
        correcciones,
    )
    meses = meses_explicitos if meses_explicitos is not None else meses_bebe
    if isinstance(meses, bool) or (meses is not None and meses < 0):
        meses = None
    alimentos = _normalizar_alimentos(alimentos_contexto)
    rango_edad = (
        None
        if meses_explicitos is not None or meses_bebe is not None
        else _normalizar_rango_edad(rango_edad_bebe)
    )

    candidatos = tuple(
        candidato
        for numero, regla in enumerate(reglas_inmutables)
        if (
            candidato := _evaluar_candidato(
                numero,
                regla,
                indice,
                tokens_mensaje,
                correcciones,
                categoria_anterior,
                meses,
                rango_edad,
                alimentos,
            )
        )
        is not None
    )
    if not candidatos:
        return None

    candidatos_emergencia = tuple(
        candidato for candidato in candidatos if candidato.emergencia
    )
    grupo_prioritario = candidatos_emergencia or candidatos
    ordenados = sorted(
        grupo_prioritario,
        key=lambda candidato: (-candidato.puntaje, candidato.indice_regla),
    )
    mejor = ordenados[0]
    segundo_puntaje = ordenados[1].puntaje if len(ordenados) > 1 else 0.0
    margen = (
        (mejor.puntaje - segundo_puntaje) / mejor.puntaje
        if mejor.puntaje > 0
        else 0.0
    )

    if mejor.puntaje < PUNTAJE_MINIMO or margen < MARGEN_MINIMO:
        return None

    regla = reglas_inmutables[mejor.indice_regla]
    todos_ordenados = sorted(
        (
            candidato
            for candidato in candidatos
            if candidato.indice_regla != mejor.indice_regla
        ),
        key=lambda candidato: (-candidato.puntaje, candidato.indice_regla),
    )
    descartados = tuple(
        CandidatoBusqueda(
            categoria=reglas_inmutables[candidato.indice_regla].categoria,
            subcategoria=(
                reglas_inmutables[candidato.indice_regla].subcategoria
            ),
            puntaje=candidato.puntaje,
            coincidencias_exactas=candidato.exactas,
            coincidencias_aproximadas=candidato.aproximadas,
        )
        for candidato in todos_ordenados[:MAXIMO_CANDIDATOS_DESCARTADOS]
    )

    if mejor.emergencia:
        motivo = "emergencia_prioritaria"
    elif mejor.termino_especifico and (
        len(mejor.exactas) + len(mejor.aproximadas) == 1
    ):
        motivo = "termino_especifico"
    else:
        motivo = "coincidencias_y_margen_suficientes"

    return ResultadoBusqueda(
        regla=regla,
        puntaje=mejor.puntaje,
        margen=margen,
        coincidencias_exactas=mejor.exactas,
        coincidencias_aproximadas=mejor.aproximadas,
        motivo=motivo,
        candidatos_descartados=descartados,
    )


def construir_respuesta(
    regla: ReglaConocimiento | ResultadoBusqueda,
) -> str:
    """Une sin reescribir la respuesta revisada y su disclaimer obligatorio."""

    regla_seleccionada = regla.regla if isinstance(regla, ResultadoBusqueda) else regla
    partes = [regla_seleccionada.respuesta.strip()]
    if regla_seleccionada.disclaimer.strip():
        partes.append(regla_seleccionada.disclaimer.strip())
    return "\n\n".join(partes)


GRUPO_BLANDOS = "Alimentos blandos o bases"
GRUPO_HIERRO = "Alimentos ricos en hierro"
GRUPO_OTROS = "Otros ingredientes"

INGREDIENTES_RECETAS: tuple[IngredienteReceta, ...] = (
    IngredienteReceta("papa", "papa", GRUPO_BLANDOS, ("papa", "papa amarilla")),
    IngredienteReceta("camote", "camote", GRUPO_BLANDOS, ("camote",)),
    IngredienteReceta("zapallo", "zapallo", GRUPO_BLANDOS, ("zapallo",)),
    IngredienteReceta("arroz", "arroz", GRUPO_BLANDOS, ("arroz",)),
    IngredienteReceta("semola", "sémola", GRUPO_BLANDOS, ("semola",)),
    IngredienteReceta(
        "fideos",
        "fideos o tallarines",
        GRUPO_BLANDOS,
        ("fideo", "fideos", "tallarin", "tallarines"),
    ),
    IngredienteReceta("habas", "habas", GRUPO_BLANDOS, ("haba", "habas")),
    IngredienteReceta("trigo", "trigo", GRUPO_BLANDOS, ("trigo",)),
    IngredienteReceta("yuca", "yuca", GRUPO_BLANDOS, ("yuca",)),
    IngredienteReceta(
        "arvejas",
        "arvejas",
        GRUPO_BLANDOS,
        ("arveja", "arvejas"),
    ),
    IngredienteReceta("bazo", "bazo", GRUPO_HIERRO, ("bazo",)),
    IngredienteReceta("bofe", "bofe", GRUPO_HIERRO, ("bofe",)),
    IngredienteReceta(
        "higado",
        "hígado",
        GRUPO_HIERRO,
        ("higado", "higado de pollo", "higado de res"),
    ),
    IngredienteReceta(
        "sangrecita",
        "sangrecita",
        GRUPO_HIERRO,
        ("sangrecita", "sangre de pollo"),
    ),
    IngredienteReceta(
        "pescado",
        "pescado",
        GRUPO_HIERRO,
        ("pescado", "bonito"),
    ),
    IngredienteReceta(
        "zanahoria",
        "zanahoria",
        GRUPO_OTROS,
        ("zanahoria",),
    ),
    IngredienteReceta(
        "espinaca",
        "espinaca",
        GRUPO_OTROS,
        ("espinaca",),
    ),
    IngredienteReceta("leche", "leche", GRUPO_OTROS, ("leche",)),
    IngredienteReceta(
        "cebolla",
        "cebolla",
        GRUPO_OTROS,
        ("cebolla",),
    ),
    IngredienteReceta("ajo", "ajo", GRUPO_OTROS, ("ajo", "ajos")),
    IngredienteReceta(
        "aceite",
        "aceite vegetal",
        GRUPO_OTROS,
        ("aceite", "aceite vegetal"),
    ),
    IngredienteReceta("tomate", "tomate", GRUPO_OTROS, ("tomate",)),
    IngredienteReceta(
        "albahaca",
        "albahaca",
        GRUPO_OTROS,
        ("albahaca",),
    ),
    IngredienteReceta(
        "culantro",
        "culantro",
        GRUPO_OTROS,
        ("culantro", "cilantro"),
    ),
    IngredienteReceta(
        "aji_amarillo",
        "ají amarillo",
        GRUPO_OTROS,
        ("aji amarillo", "aji"),
    ),
    IngredienteReceta(
        "pan_rallado",
        "pan rallado",
        GRUPO_OTROS,
        ("pan rallado", "pan"),
    ),
    IngredienteReceta("choclo", "choclo", GRUPO_OTROS, ("choclo",)),
    IngredienteReceta(
        "queso",
        "queso fresco",
        GRUPO_OTROS,
        ("queso", "queso fresco"),
    ),
    IngredienteReceta("huevo", "huevo", GRUPO_OTROS, ("huevo",)),
    IngredienteReceta(
        "harina",
        "harina de trigo",
        GRUPO_OTROS,
        ("harina", "harina de trigo"),
    ),
    IngredienteReceta(
        "pimiento",
        "pimiento",
        GRUPO_OTROS,
        ("pimiento",),
    ),
)

_INGREDIENTES_POR_CLAVE = {
    ingrediente.clave: ingrediente for ingrediente in INGREDIENTES_RECETAS
}

def _ingrediente_aparece_en_texto(
    ingrediente: IngredienteReceta,
    texto: str,
) -> bool:
    normalizado = normalizar_texto(texto)
    return any(
        contiene_frase(normalizado, normalizar_texto(alias))
        for alias in ingrediente.alias
    )


def obtener_perfiles_recetas(
    reglas: Sequence[ReglaConocimiento],
) -> tuple[PerfilReceta, ...]:
    """Construye perfiles únicamente para las recetas MINSA conocidas."""

    perfiles: list[PerfilReceta] = []
    for regla in reglas:
        claves = regla.ingredientes_receta
        es_receta = normalizar_texto(regla.categoria).startswith("recetas minsa")
        if not es_receta and claves:
            raise ValueError(
                f"{regla.id_regla} tiene ingredientes pero no es una receta"
            )
        if not es_receta:
            continue
        if not claves:
            raise ValueError(
                f"{regla.id_regla} no tiene ingredientes de receta"
            )
        for clave in claves:
            ingrediente = _INGREDIENTES_POR_CLAVE.get(clave)
            if ingrediente is None:
                raise ValueError(
                    f"{regla.id_regla} tiene un ingrediente desconocido: {clave}"
                )
            if not _ingrediente_aparece_en_texto(
                ingrediente,
                f"{regla.respuesta} {' '.join(regla.palabras_clave)}",
            ):
                raise ValueError(
                    f"{ingrediente.nombre!r} no aparece en {regla.id_regla}"
                )
        perfiles.append(PerfilReceta(regla=regla, ingredientes=claves))
    return tuple(perfiles)


def obtener_grupos_alimentos(
    perfiles: Sequence[PerfilReceta],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Agrupa los ingredientes usados por al menos una receta revisada."""

    usados = {
        clave for perfil in perfiles for clave in perfil.ingredientes
    }
    resultado: list[tuple[str, tuple[str, ...]]] = []
    for grupo in (GRUPO_BLANDOS, GRUPO_HIERRO, GRUPO_OTROS):
        nombres = tuple(
            ingrediente.nombre
            for ingrediente in INGREDIENTES_RECETAS
            if ingrediente.grupo == grupo and ingrediente.clave in usados
        )
        resultado.append((grupo, nombres))
    return tuple(resultado)


def nombres_ingredientes(claves: Sequence[str]) -> tuple[str, ...]:
    """Convierte claves internas en nombres seguros para mostrar al usuario."""

    return tuple(
        _INGREDIENTES_POR_CLAVE[clave].nombre
        for clave in claves
        if clave in _INGREDIENTES_POR_CLAVE
    )


_SEPARADOR_PREFERENCIAS = re.compile(
    r"\s*(?:\bseparadorpreferencia\b|\bpero\b|\baunque\b|\bsin embargo\b|"
    r"\by\s+(?=(?:no|come|acepta|tolera|puede|odia|rechaza|alerg|si|le\s+gusta)\b))\s*"
)
_SENALES_EXCLUSION = (
    "alerg",
    "intoler",
    "no puede",
    "le hace dano",
    "le cae mal",
    "prohibido",
)
_SENALES_RECHAZO = (
    "no ",
    "no le gusta",
    "no come",
    "odia",
    "rechaza",
    "detesta",
    "asco",
)
_SENALES_ACEPTACION = (
    "come",
    "gusta",
    "acepta",
    "tolera",
    "puede comer",
    "si come",
)


def _alias_en_fragmento(alias: str, fragmento: str) -> bool:
    alias_normalizado = normalizar_texto(alias)
    if contiene_frase(fragmento, alias_normalizado):
        return True
    if " " in alias_normalizado or len(alias_normalizado) < 4:
        return False
    limite = 1 if len(alias_normalizado) <= 5 else 2
    for token in fragmento.split():
        if abs(len(token) - len(alias_normalizado)) > limite:
            continue
        distancia = _distancia_levenshtein(
            token,
            alias_normalizado,
            limite,
        )
        similitud = 1.0 - distancia / max(len(token), len(alias_normalizado))
        if distancia <= limite and similitud >= 0.80:
            return True
    return False


def _claves_en_fragmento(fragmento: str) -> set[str]:
    encontradas: set[str] = set()
    for ingrediente in INGREDIENTES_RECETAS:
        if any(
            _alias_en_fragmento(alias, fragmento)
            for alias in ingrediente.alias
        ):
            encontradas.add(ingrediente.clave)
    return encontradas


def extraer_perfil_alimentario(texto: str) -> PerfilAlimentario:
    """Detecta alimentos aceptados, rechazados y excluidos por seguridad."""

    normalizado = normalizar_texto(texto)
    todas_las_claves = {ingrediente.clave for ingrediente in INGREDIENTES_RECETAS}
    if re.search(r"\b(?:come|acepta|tolera)\s+de\s+todo\b", normalizado):
        return PerfilAlimentario(tuple(sorted(todas_las_claves)), (), ())
    if re.search(r"\b(?:no\s+come|rechaza|odia)\s+(?:de\s+)?todo\b", normalizado):
        return PerfilAlimentario((), tuple(sorted(todas_las_claves)), ())

    aceptados: set[str] = set()
    rechazados: set[str] = set()
    excluidos: set[str] = set()
    estado_actual = "aceptado"
    texto_con_separadores = re.sub(
        r"[,;\n.]",
        " separadorpreferencia ",
        texto,
    )
    normalizado_con_separadores = normalizar_texto(texto_con_separadores)
    fragmentos = [
        fragmento.strip()
        for fragmento in _SEPARADOR_PREFERENCIAS.split(
            normalizado_con_separadores
        )
        if fragmento.strip()
    ]
    for fragmento in fragmentos:
        if any(senal in fragmento for senal in _SENALES_EXCLUSION):
            estado_actual = "excluido"
        elif any(senal in f"{fragmento} " for senal in _SENALES_RECHAZO):
            estado_actual = "rechazado"
        elif any(senal in fragmento for senal in _SENALES_ACEPTACION):
            estado_actual = "aceptado"

        claves = _claves_en_fragmento(fragmento)
        if estado_actual == "excluido":
            excluidos.update(claves)
        elif estado_actual == "rechazado":
            rechazados.update(claves)
        else:
            aceptados.update(claves)

    rechazados.difference_update(excluidos)
    aceptados.difference_update(rechazados | excluidos)
    return PerfilAlimentario(
        aceptados=tuple(sorted(aceptados)),
        rechazados=tuple(sorted(rechazados)),
        excluidos=tuple(sorted(excluidos)),
    )


def rango_receta_de_regla(regla: ReglaConocimiento) -> str | None:
    """Extrae el rango ``6-8``, ``9-11`` o ``12-23`` de una receta."""

    coincidencia = re.fullmatch(
        r"recetas minsa (6 8|9 11|12 23)m",
        normalizar_texto(regla.categoria),
    )
    return coincidencia.group(1).replace(" ", "-") if coincidencia else None


def filtrar_recetas_por_edad(
    perfiles: Sequence[PerfilReceta],
    *,
    meses_bebe: int | None = None,
    rango_edad_bebe: str | None = None,
) -> tuple[PerfilReceta, ...]:
    """Filtra recetas sin cruzar los rangos etarios revisados."""

    rango_objetivo: str | None = None
    if meses_bebe is not None:
        for rango in ("6-8", "9-11", "12-23"):
            minimo, maximo = (int(valor) for valor in rango.split("-"))
            if minimo <= meses_bebe <= maximo:
                rango_objetivo = rango
                break
    elif rango_edad_bebe in {"6-8", "9-11", "12-23"}:
        rango_objetivo = rango_edad_bebe

    if rango_objetivo is None:
        return ()
    return tuple(
        perfil
        for perfil in perfiles
        if rango_receta_de_regla(perfil.regla) == rango_objetivo
    )


def rankear_recetas(
    perfiles: Sequence[PerfilReceta],
    perfil_alimentario: PerfilAlimentario,
) -> ResultadoReceta | None:
    """Elige la mejor coincidencia local; una exclusión elimina la receta."""

    aceptados = set(perfil_alimentario.aceptados)
    rechazados = set(perfil_alimentario.rechazados)
    excluidos = set(perfil_alimentario.excluidos)
    resultados: list[ResultadoReceta] = []
    for perfil in perfiles:
        ingredientes = set(perfil.ingredientes)
        if ingredientes & excluidos:
            continue
        coincidentes = tuple(sorted(ingredientes & aceptados))
        no_gustan = tuple(sorted(ingredientes & rechazados))
        desconocidos = tuple(
            sorted(ingredientes - aceptados - rechazados - excluidos)
        )
        resultados.append(
            ResultadoReceta(
                perfil_receta=perfil,
                puntaje=5.0 * len(coincidentes) - 7.0 * len(no_gustan),
                aceptados=coincidentes,
                rechazados=no_gustan,
                desconocidos=desconocidos,
            )
        )
    if not resultados:
        return None
    return max(
        resultados,
        key=lambda resultado: (
            resultado.puntaje,
            len(resultado.aceptados),
            -len(resultado.rechazados),
        ),
    )


def seleccionar_receta_variada(
    perfiles: Sequence[PerfilReceta],
    perfil_alimentario: PerfilAlimentario,
    *,
    generador: random.Random | random.SystemRandom | None = None,
) -> ResultadoReceta | None:
    """Alterna solo entre recetas empatadas con la mejor compatibilidad segura."""
    mejor = rankear_recetas(perfiles, perfil_alimentario)
    if mejor is None:
        return None
    clave_mejor = (
        mejor.puntaje,
        len(mejor.aceptados),
        -len(mejor.rechazados),
    )
    empatadas = []
    for perfil in perfiles:
        candidata = rankear_recetas((perfil,), perfil_alimentario)
        if candidata is None:
            continue
        clave = (
            candidata.puntaje,
            len(candidata.aceptados),
            -len(candidata.rechazados),
        )
        if clave == clave_mejor:
            empatadas.append(candidata)
    if not empatadas:  # Protegido por ``mejor``.
        return mejor
    return (generador or random.SystemRandom()).choice(empatadas)
