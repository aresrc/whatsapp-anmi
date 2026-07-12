import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReglaConocimiento:
    categoria: str
    subcategoria: str
    palabras_clave: tuple[str, ...]
    respuesta: str
    disclaimer: str


RUTA_CSV = (
    Path(__file__).resolve().parent
    / "data"
    / "motor_conocimientos.csv"
)

COLUMNA_CATEGORIA = "Categoría"
COLUMNA_SUBCATEGORIA = "Subcategoría"
COLUMNA_PALABRAS = "Palabras Clave (Para el bot)"
COLUMNA_RESPUESTA = (
    "Respuesta Verificada (Lenguaje sencillo para el chat)"
)
COLUMNA_DISCLAIMER = "Disclaimer Obligatorio (¡Crítico!)"


def normalizar_texto(texto: str) -> str:
    """
    Normaliza el texto para que las coincidencias no dependan de:
    - mayúsculas;
    - tildes;
    - signos de puntuación;
    - espacios repetidos.
    """
    texto = texto.lower().strip()

    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )

    texto = re.sub(r"[^a-z0-9ñ\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()


def separar_palabras_clave(celda: str) -> tuple[str, ...]:
    """
    Convierte una celda como:

    "qué es la anemia", "definición anemia", "por qué da anemia"

    en:

    (
        "qué es la anemia",
        "definición anemia",
        "por qué da anemia"
    )
    """
    if not celda or not celda.strip():
        return tuple()

    # Busca primero textos delimitados por comillas.
    palabras_entre_comillas = re.findall(
        r'"([^"]+)"',
        celda,
    )

    if palabras_entre_comillas:
        palabras = palabras_entre_comillas
    else:
        # Respaldo para filas que no tengan comillas.
        palabras = celda.split(",")

    palabras_limpias = []

    for palabra in palabras:
        palabra = palabra.strip().strip('"').strip()

        if palabra:
            palabras_limpias.append(palabra)

    return tuple(palabras_limpias)


def cargar_motor_conocimientos(
    ruta_csv: Path = RUTA_CSV,
) -> tuple[ReglaConocimiento, ...]:
    """
    Lee todas las filas del CSV y las convierte en reglas del bot.
    """
    if not ruta_csv.exists():
        raise FileNotFoundError(
            f"No se encontró el archivo CSV: {ruta_csv}"
        )

    reglas: list[ReglaConocimiento] = []

    with ruta_csv.open(
        mode="r",
        encoding="utf-8-sig",
        newline="",
    ) as archivo:
        lector = csv.DictReader(archivo)

        columnas_requeridas = {
            COLUMNA_CATEGORIA,
            COLUMNA_SUBCATEGORIA,
            COLUMNA_PALABRAS,
            COLUMNA_RESPUESTA,
            COLUMNA_DISCLAIMER,
        }

        columnas_encontradas = set(lector.fieldnames or [])
        columnas_faltantes = (
            columnas_requeridas - columnas_encontradas
        )

        if columnas_faltantes:
            raise ValueError(
                "Faltan columnas obligatorias en el CSV: "
                + ", ".join(sorted(columnas_faltantes))
            )

        for numero_fila, fila in enumerate(lector, start=2):
            palabras_clave = separar_palabras_clave(
                fila.get(COLUMNA_PALABRAS, "")
            )

            respuesta = fila.get(
                COLUMNA_RESPUESTA,
                "",
            ).strip()

            disclaimer = fila.get(
                COLUMNA_DISCLAIMER,
                "",
            ).strip()

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
                    categoria=fila.get(
                        COLUMNA_CATEGORIA,
                        "",
                    ).strip(),
                    subcategoria=fila.get(
                        COLUMNA_SUBCATEGORIA,
                        "",
                    ).strip(),
                    palabras_clave=palabras_clave,
                    respuesta=respuesta,
                    disclaimer=disclaimer,
                )
            )

    logging.info(
        "Motor cargado: %s reglas desde %s",
        len(reglas),
        ruta_csv.name,
    )

    return tuple(reglas)

def contiene_frase(
    texto_normalizado: str,
    frase_normalizada: str,
) -> bool:
    """
    Comprueba que una frase aparezca completa dentro del mensaje.
    """
    patron = (
        rf"(?<!\w)"
        rf"{re.escape(frase_normalizada)}"
        rf"(?!\w)"
    )

    return re.search(
        patron,
        texto_normalizado,
    ) is not None


def calcular_puntaje(
    texto_normalizado: str,
    palabra_clave: str,
) -> int:
    """
    Da mayor puntuación a las coincidencias más específicas.

    Ejemplo:
    - 'anemia' obtiene menos puntaje.
    - 'qué es la anemia' obtiene más puntaje.
    """
    palabra_normalizada = normalizar_texto(
        palabra_clave
    )

    if not palabra_normalizada:
        return 0

    if texto_normalizado == palabra_normalizada:
        # Coincidencia exacta: máxima prioridad.
        return 10_000 + len(palabra_normalizada)

    if contiene_frase(
        texto_normalizado,
        palabra_normalizada,
    ):
        cantidad_palabras = len(
            palabra_normalizada.split()
        )

        return (
            cantidad_palabras * 100
            + len(palabra_normalizada)
        )

    return 0


def buscar_mejor_regla(
    texto_usuario: str,
    reglas: tuple[ReglaConocimiento, ...],
) -> ReglaConocimiento | None:
    """
    Busca la regla cuya palabra clave tenga el mayor puntaje.
    """
    texto_normalizado = normalizar_texto(
        texto_usuario
    )

    if not texto_normalizado:
        return None

    mejor_regla: ReglaConocimiento | None = None
    mejor_puntaje = 0

    for regla in reglas:
        puntaje_regla = max(
            (
                calcular_puntaje(
                    texto_normalizado,
                    palabra,
                )
                for palabra in regla.palabras_clave
            ),
            default=0,
        )

        if puntaje_regla > mejor_puntaje:
            mejor_puntaje = puntaje_regla
            mejor_regla = regla

    return mejor_regla


def construir_respuesta(
    regla: ReglaConocimiento,
) -> str:
    """
    Une la respuesta verificada y el disclaimer obligatorio.
    """
    partes = [regla.respuesta.strip()]

    if regla.disclaimer.strip():
        partes.append(regla.disclaimer.strip())

    return "\n\n".join(partes)