"""Motor de conocimiento basado en reglas para las consultas de ANMI.

El módulo mantiene deliberadamente separadas la carga de contenido revisado y
la clasificación. La clasificación no genera texto: únicamente selecciona una
regla del CSV cuando existe evidencia suficiente en el mensaje actual.
"""

from __future__ import annotations

import csv
import logging
import math
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
COLUMNA_SUBCATEGORIA = "Subcategoría"
COLUMNA_PALABRAS = "Palabras Clave (Para el bot)"
COLUMNA_RESPUESTA = (
    "Respuesta Verificada (Lenguaje sencillo para el chat)"
)
COLUMNA_DISCLAIMER = "Disclaimer Obligatorio (¡Crítico!)"
COLUMNA_PAGINAS = "Paginas"
COLUMNA_DOCUMENTO = "Documento"
COLUMNA_ENLACE = "Enlace"

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


def cargar_motor_conocimientos(
    ruta_csv: str | Path = RUTA_CSV,
) -> tuple[ReglaConocimiento, ...]:
    """Carga y valida las reglas desde el CSV v2 codificado como UTF-8 BOM."""

    ruta = Path(ruta_csv)
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el archivo CSV: {ruta}")

    columnas_requeridas = {
        COLUMNA_CATEGORIA,
        COLUMNA_SUBCATEGORIA,
        COLUMNA_PALABRAS,
        COLUMNA_RESPUESTA,
        COLUMNA_DISCLAIMER,
        COLUMNA_PAGINAS,
        COLUMNA_DOCUMENTO,
        COLUMNA_ENLACE,
    }
    reglas: list[ReglaConocimiento] = []

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
            palabras_clave = separar_palabras_clave(
                fila.get(COLUMNA_PALABRAS, "") or ""
            )
            respuesta = _quitar_comillas_externas(
                fila.get(COLUMNA_RESPUESTA, "") or ""
            )
            disclaimer = _quitar_comillas_externas(
                fila.get(COLUMNA_DISCLAIMER, "") or ""
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


def _evaluar_candidato(
    numero: int,
    regla: ReglaConocimiento,
    indice: _IndiceMotor,
    tokens_mensaje: frozenset[str],
    correcciones: tuple[CoincidenciaAproximada, ...],
    categoria_anterior: str | None,
    meses: int | None,
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
