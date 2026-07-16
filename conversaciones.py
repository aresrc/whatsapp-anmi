"""Persistencia temporal y resúmenes anónimos de las conversaciones.

El repositorio mantiene el transcript y la identidad solamente mientras una
conversación está activa. ``finalizar_conversacion`` crea el resumen mínimo y
elimina la conversación activa (junto con sus mensajes) en una transacción.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python 3.9+ incluye zoneinfo.
    ZoneInfo = None  # type: ignore[assignment,misc]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]


DIRECTORIO_PROYECTO = Path(__file__).resolve().parent
RUTA_ESQUEMA_PREDETERMINADA = DIRECTORIO_PROYECTO / "schema.sql"
RUTA_BD_PREDETERMINADA = DIRECTORIO_PROYECTO / "instance" / "anmi.sqlite3"
ESTADO_INICIAL = "esperando_meses"
ESTADOS_CONVERSACION = frozenset(
    {
        "esperando_meses",
        "esperando_alimentos",
        "lista",
        "esperando_calificacion",
    }
)

_SIN_CAMBIO = object()


class ErrorPersistenciaConversacion(Exception):
    """Error base de la capa de persistencia de conversaciones."""


class ConversacionNoEncontradaError(ErrorPersistenciaConversacion):
    """La conversación activa solicitada ya no existe."""


class MensajeExternoDuplicadoError(ErrorPersistenciaConversacion):
    """El proveedor ya había entregado un mensaje con ese id externo."""


@dataclass(frozen=True)
class ConversacionActiva:
    id: int
    canal: str
    usuario_temporal: str
    estado: str
    fecha_inicio_utc: datetime
    fecha_ultima_actividad_utc: datetime
    meses_bebe: int | None
    alimentos_contexto: str | None
    calificacion_pendiente: int | None


@dataclass(frozen=True)
class MensajeActivo:
    id: int
    conversacion_id: int
    id_externo: str | None
    rol: str
    contenido: str
    categoria: str | None
    subcategoria: str | None
    puntaje: float | None
    evidencia: dict[str, Any] | None
    fecha_hora_utc: datetime


@dataclass(frozen=True)
class ConsultaFinalizada:
    id: int
    fecha_hora_cierre: datetime
    meses_bebe: int | None
    calificacion: int
    categorias: tuple[str, ...]


def normalizar_categoria(categoria: str) -> str:
    """Genera la clave estable usada para deduplicar categorías."""
    texto = unicodedata.normalize("NFD", categoria.strip().lower())
    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )
    texto = re.sub(r"[^a-z0-9ñ]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _zona_horaria_lima() -> tzinfo:
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/Lima")
        except ZoneInfoNotFoundError:
            pass

    # Lima no observa horario de verano actualmente. El respaldo evita que
    # una instalación de Windows sin la base IANA impida cerrar sesiones.
    return timezone(timedelta(hours=-5), name="America/Lima")


ZONA_HORARIA_LIMA = _zona_horaria_lima()


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _asegurar_fecha_con_zona(fecha: datetime, nombre: str) -> datetime:
    if fecha.tzinfo is None or fecha.utcoffset() is None:
        raise ValueError(f"{nombre} debe incluir zona horaria")
    return fecha


def _serializar_utc(fecha: datetime) -> str:
    fecha = _asegurar_fecha_con_zona(fecha, "La fecha UTC")
    return (
        fecha.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _serializar_lima(fecha: datetime) -> str:
    fecha = _asegurar_fecha_con_zona(fecha, "La fecha de cierre")
    return fecha.astimezone(ZONA_HORARIA_LIMA).isoformat(
        timespec="milliseconds"
    )


def _parsear_fecha(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def _validar_texto_no_vacio(valor: str, nombre: str) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError(f"{nombre} no puede estar vacío")
    return valor.strip()


def _validar_meses(meses_bebe: int | None) -> None:
    if meses_bebe is not None and (
        isinstance(meses_bebe, bool)
        or not isinstance(meses_bebe, int)
        or not 0 <= meses_bebe <= 59
    ):
        raise ValueError("meses_bebe debe ser un entero entre 0 y 59 o None")


def _validar_calificacion(calificacion: int | None) -> None:
    if calificacion is not None and (
        isinstance(calificacion, bool)
        or not isinstance(calificacion, int)
        or not 1 <= calificacion <= 5
    ):
        raise ValueError("La calificación debe ser un entero entre 1 y 5")


def _validar_estado(estado: str) -> str:
    estado = _validar_texto_no_vacio(estado, "estado")
    if estado not in ESTADOS_CONVERSACION:
        permitidos = ", ".join(sorted(ESTADOS_CONVERSACION))
        raise ValueError(f"estado debe ser uno de: {permitidos}")
    return estado


class RepositorioConversaciones:
    """Repository SQLite con conexiones cortas y operaciones atómicas."""

    def __init__(
        self,
        ruta_bd: str | Path = RUTA_BD_PREDETERMINADA,
        ruta_esquema: str | Path = RUTA_ESQUEMA_PREDETERMINADA,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms no puede ser negativo")

        self.ruta_bd = Path(ruta_bd)
        self.ruta_esquema = Path(ruta_esquema)
        self.busy_timeout_ms = busy_timeout_ms

    def inicializar(self) -> None:
        """Crea idempotentemente el directorio, tablas, índices y pragmas."""
        if not self.ruta_esquema.is_file():
            raise FileNotFoundError(
                f"No se encontró el esquema SQL: {self.ruta_esquema}"
            )

        self.ruta_bd.parent.mkdir(parents=True, exist_ok=True)
        esquema = self.ruta_esquema.read_text(encoding="utf-8")

        with self._conexion() as conexion:
            conexion.executescript(esquema)

    @contextmanager
    def _conexion(self) -> Iterator[sqlite3.Connection]:
        conexion = sqlite3.connect(
            self.ruta_bd,
            timeout=self.busy_timeout_ms / 1_000,
        )
        conexion.row_factory = sqlite3.Row
        conexion.execute("PRAGMA foreign_keys = ON")
        conexion.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")

        try:
            yield conexion
        finally:
            conexion.close()

    @contextmanager
    def _transaccion(self) -> Iterator[sqlite3.Connection]:
        with self._conexion() as conexion:
            conexion.execute("BEGIN IMMEDIATE")
            try:
                yield conexion
            except Exception:
                conexion.rollback()
                raise
            else:
                conexion.commit()

    def obtener_conversacion(
        self,
        canal: str,
        usuario_temporal: str,
    ) -> ConversacionActiva | None:
        canal = _validar_texto_no_vacio(canal, "canal")
        usuario_temporal = _validar_texto_no_vacio(
            usuario_temporal,
            "usuario_temporal",
        )

        with self._conexion() as conexion:
            fila = conexion.execute(
                """
                SELECT *
                FROM conversaciones_activas
                WHERE canal = ? AND usuario_temporal = ?
                """,
                (canal, usuario_temporal),
            ).fetchone()

        return self._conversacion_desde_fila(fila) if fila else None

    def obtener_conversacion_por_id(
        self,
        conversacion_id: int,
    ) -> ConversacionActiva | None:
        with self._conexion() as conexion:
            fila = conexion.execute(
                "SELECT * FROM conversaciones_activas WHERE id = ?",
                (conversacion_id,),
            ).fetchone()

        return self._conversacion_desde_fila(fila) if fila else None

    def obtener_o_crear_conversacion(
        self,
        canal: str,
        usuario_temporal: str,
        estado_inicial: str = ESTADO_INICIAL,
        ahora: datetime | None = None,
    ) -> ConversacionActiva:
        canal = _validar_texto_no_vacio(canal, "canal")
        usuario_temporal = _validar_texto_no_vacio(
            usuario_temporal,
            "usuario_temporal",
        )
        estado_inicial = _validar_estado(estado_inicial)
        fecha = _serializar_utc(ahora or _ahora_utc())

        with self._transaccion() as conexion:
            conexion.execute(
                """
                INSERT INTO conversaciones_activas (
                    canal,
                    usuario_temporal,
                    estado,
                    fecha_inicio_utc,
                    fecha_ultima_actividad_utc
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (canal, usuario_temporal) DO NOTHING
                """,
                (canal, usuario_temporal, estado_inicial, fecha, fecha),
            )
            fila = conexion.execute(
                """
                SELECT *
                FROM conversaciones_activas
                WHERE canal = ? AND usuario_temporal = ?
                """,
                (canal, usuario_temporal),
            ).fetchone()

        if fila is None:  # pragma: no cover - protegido por la transacción.
            raise ErrorPersistenciaConversacion(
                "No se pudo obtener ni crear la conversación"
            )
        return self._conversacion_desde_fila(fila)

    def crear_conversacion(
        self,
        canal: str,
        usuario_temporal: str,
        estado: str = ESTADO_INICIAL,
        *,
        ahora: datetime | None = None,
    ) -> ConversacionActiva:
        """Crea la sesión o devuelve la existente para el mismo usuario."""
        return self.obtener_o_crear_conversacion(
            canal,
            usuario_temporal,
            estado_inicial=estado,
            ahora=ahora,
        )

    def actualizar_conversacion(
        self,
        conversacion_id: int,
        *,
        estado: str | object = _SIN_CAMBIO,
        meses_bebe: int | None | object = _SIN_CAMBIO,
        alimentos_contexto: str | None | object = _SIN_CAMBIO,
        calificacion_pendiente: int | None | object = _SIN_CAMBIO,
        ahora: datetime | None = None,
    ) -> ConversacionActiva:
        """Actualiza uno o varios campos; pasar ``None`` borra un opcional."""
        asignaciones: list[str] = []
        parametros: list[Any] = []

        if estado is not _SIN_CAMBIO:
            if not isinstance(estado, str):
                raise ValueError("estado debe ser texto")
            asignaciones.append("estado = ?")
            parametros.append(_validar_estado(estado))
        if meses_bebe is not _SIN_CAMBIO:
            _validar_meses(meses_bebe)  # type: ignore[arg-type]
            asignaciones.append("meses_bebe = ?")
            parametros.append(meses_bebe)
        if alimentos_contexto is not _SIN_CAMBIO:
            if (
                alimentos_contexto is not None
                and not isinstance(alimentos_contexto, str)
            ):
                raise ValueError("alimentos_contexto debe ser texto o None")
            asignaciones.append("alimentos_contexto = ?")
            parametros.append(
                alimentos_contexto.strip()
                if isinstance(alimentos_contexto, str)
                else None
            )
        if calificacion_pendiente is not _SIN_CAMBIO:
            _validar_calificacion(  # type: ignore[arg-type]
                calificacion_pendiente
            )
            asignaciones.append("calificacion_pendiente = ?")
            parametros.append(calificacion_pendiente)

        asignaciones.append("fecha_ultima_actividad_utc = ?")
        parametros.append(_serializar_utc(ahora or _ahora_utc()))
        parametros.append(conversacion_id)
        return self._actualizar_y_obtener(
            conversacion_id,
            asignaciones,
            parametros,
        )

    def actualizar_perfil(
        self,
        conversacion_id: int,
        *,
        meses_bebe: int | None | object = _SIN_CAMBIO,
        alimentos_contexto: str | None | object = _SIN_CAMBIO,
        ahora: datetime | None = None,
    ) -> ConversacionActiva:
        if meses_bebe is not _SIN_CAMBIO:
            _validar_meses(meses_bebe)  # type: ignore[arg-type]
        if (
            alimentos_contexto is not _SIN_CAMBIO
            and alimentos_contexto is not None
            and not isinstance(alimentos_contexto, str)
        ):
            raise ValueError("alimentos_contexto debe ser texto o None")

        asignaciones: list[str] = []
        parametros: list[Any] = []
        if meses_bebe is not _SIN_CAMBIO:
            asignaciones.append("meses_bebe = ?")
            parametros.append(meses_bebe)
        if alimentos_contexto is not _SIN_CAMBIO:
            asignaciones.append("alimentos_contexto = ?")
            parametros.append(
                alimentos_contexto.strip()
                if isinstance(alimentos_contexto, str)
                else None
            )

        asignaciones.append("fecha_ultima_actividad_utc = ?")
        parametros.append(_serializar_utc(ahora or _ahora_utc()))
        parametros.append(conversacion_id)

        return self._actualizar_y_obtener(
            conversacion_id,
            asignaciones,
            parametros,
        )

    def actualizar_estado(
        self,
        conversacion_id: int,
        estado: str,
        *,
        calificacion_pendiente: int | None | object = _SIN_CAMBIO,
        ahora: datetime | None = None,
    ) -> ConversacionActiva:
        estado = _validar_estado(estado)
        if calificacion_pendiente is not _SIN_CAMBIO:
            _validar_calificacion(  # type: ignore[arg-type]
                calificacion_pendiente
            )

        asignaciones = ["estado = ?"]
        parametros: list[Any] = [estado]
        if calificacion_pendiente is not _SIN_CAMBIO:
            asignaciones.append("calificacion_pendiente = ?")
            parametros.append(calificacion_pendiente)
        asignaciones.append("fecha_ultima_actividad_utc = ?")
        parametros.append(_serializar_utc(ahora or _ahora_utc()))
        parametros.append(conversacion_id)

        return self._actualizar_y_obtener(
            conversacion_id,
            asignaciones,
            parametros,
        )

    def _actualizar_y_obtener(
        self,
        conversacion_id: int,
        asignaciones: list[str],
        parametros: list[Any],
    ) -> ConversacionActiva:
        with self._transaccion() as conexion:
            cursor = conexion.execute(
                f"""
                UPDATE conversaciones_activas
                SET {', '.join(asignaciones)}
                WHERE id = ?
                """,
                parametros,
            )
            if cursor.rowcount != 1:
                raise ConversacionNoEncontradaError(
                    f"No existe la conversación activa {conversacion_id}"
                )
            fila = conexion.execute(
                "SELECT * FROM conversaciones_activas WHERE id = ?",
                (conversacion_id,),
            ).fetchone()

        return self._conversacion_desde_fila(fila)

    def guardar_mensaje(
        self,
        conversacion_id: int,
        rol: str,
        contenido: str,
        *,
        id_externo: str | None = None,
        categoria: str | None = None,
        subcategoria: str | None = None,
        puntaje: float | None = None,
        evidencia: Mapping[str, Any] | None = None,
        ahora: datetime | None = None,
    ) -> MensajeActivo:
        """Inserta un mensaje o lanza ``MensajeExternoDuplicadoError``."""
        if rol not in {"usuario", "bot"}:
            raise ValueError("rol debe ser 'usuario' o 'bot'")
        if not isinstance(contenido, str):
            raise ValueError("contenido debe ser texto")
        if id_externo is not None:
            id_externo = _validar_texto_no_vacio(id_externo, "id_externo")
        if categoria is not None:
            categoria = _validar_texto_no_vacio(categoria, "categoria")
        if subcategoria is not None:
            subcategoria = _validar_texto_no_vacio(
                subcategoria,
                "subcategoria",
            )
        if puntaje is not None and (
            isinstance(puntaje, bool)
            or not isinstance(puntaje, (int, float))
        ):
            raise ValueError("puntaje debe ser numérico o None")

        evidencia_json = (
            json.dumps(
                dict(evidencia),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if evidencia is not None
            else None
        )
        fecha = _serializar_utc(ahora or _ahora_utc())

        try:
            with self._transaccion() as conexion:
                if not self._existe_conversacion(conexion, conversacion_id):
                    raise ConversacionNoEncontradaError(
                        f"No existe la conversación activa {conversacion_id}"
                    )
                cursor = conexion.execute(
                    """
                    INSERT INTO mensajes_activos (
                        conversacion_id,
                        id_externo,
                        rol,
                        contenido,
                        categoria,
                        subcategoria,
                        puntaje,
                        evidencia_json,
                        fecha_hora_utc
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversacion_id,
                        id_externo,
                        rol,
                        contenido,
                        categoria,
                        subcategoria,
                        puntaje,
                        evidencia_json,
                        fecha,
                    ),
                )
                conexion.execute(
                    """
                    UPDATE conversaciones_activas
                    SET fecha_ultima_actividad_utc = ?
                    WHERE id = ?
                    """,
                    (fecha, conversacion_id),
                )
                fila = conexion.execute(
                    "SELECT * FROM mensajes_activos WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
        except sqlite3.IntegrityError as error:
            if id_externo is not None and self.existe_id_externo(id_externo):
                raise MensajeExternoDuplicadoError(
                    f"El mensaje externo {id_externo!r} ya fue registrado"
                ) from error
            raise

        return self._mensaje_desde_fila(fila)

    def registrar_mensaje(
        self,
        conversacion_id: int,
        rol: str,
        contenido: str,
        *,
        mensaje_externo_id: str | None = None,
        id_externo: str | None = None,
        categoria: str | None = None,
        subcategoria: str | None = None,
        puntaje: float | None = None,
        evidencia: Mapping[str, Any] | None = None,
        ahora: datetime | None = None,
    ) -> bool:
        """Registra un mensaje; devuelve ``False`` si el id externo se repite."""
        if mensaje_externo_id is not None and id_externo is not None:
            if mensaje_externo_id != id_externo:
                raise ValueError(
                    "mensaje_externo_id e id_externo no pueden ser distintos"
                )
        id_externo = mensaje_externo_id or id_externo
        try:
            self.guardar_mensaje(
                conversacion_id,
                rol,
                contenido,
                id_externo=id_externo,
                categoria=categoria,
                subcategoria=subcategoria,
                puntaje=puntaje,
                evidencia=evidencia,
                ahora=ahora,
            )
        except MensajeExternoDuplicadoError:
            return False
        return True

    def existe_id_externo(self, id_externo: str) -> bool:
        id_externo = _validar_texto_no_vacio(id_externo, "id_externo")
        with self._conexion() as conexion:
            fila = conexion.execute(
                """
                SELECT 1
                FROM mensajes_activos
                WHERE id_externo = ?
                LIMIT 1
                """,
                (id_externo,),
            ).fetchone()
        return fila is not None

    def listar_mensajes(
        self,
        conversacion_id: int,
    ) -> tuple[MensajeActivo, ...]:
        with self._conexion() as conexion:
            filas = conexion.execute(
                """
                SELECT *
                FROM mensajes_activos
                WHERE conversacion_id = ?
                ORDER BY id
                """,
                (conversacion_id,),
            ).fetchall()
        return tuple(self._mensaje_desde_fila(fila) for fila in filas)

    def listar_categorias(
        self,
        conversacion_id: int,
    ) -> tuple[str, ...]:
        """Lista categorías de respuestas del bot, únicas y en orden de uso."""
        with self._conexion() as conexion:
            if not self._existe_conversacion(conexion, conversacion_id):
                raise ConversacionNoEncontradaError(
                    f"No existe la conversación activa {conversacion_id}"
                )
            filas = conexion.execute(
                """
                SELECT categoria
                FROM mensajes_activos
                WHERE conversacion_id = ?
                  AND rol = 'bot'
                  AND categoria IS NOT NULL
                ORDER BY id
                """,
                (conversacion_id,),
            ).fetchall()
        return self._deduplicar_categorias(
            fila["categoria"] for fila in filas
        )

    def ultima_categoria(self, conversacion_id: int) -> str | None:
        """Devuelve la categoría de la respuesta clasificada más reciente."""
        with self._conexion() as conexion:
            if not self._existe_conversacion(conexion, conversacion_id):
                raise ConversacionNoEncontradaError(
                    f"No existe la conversación activa {conversacion_id}"
                )
            fila = conexion.execute(
                """
                SELECT categoria
                FROM mensajes_activos
                WHERE conversacion_id = ?
                  AND rol = 'bot'
                  AND categoria IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (conversacion_id,),
            ).fetchone()
        return fila["categoria"] if fila else None

    def finalizar_conversacion(
        self,
        conversacion_id: int,
        calificacion: int,
        *,
        fecha_hora: datetime | None = None,
    ) -> ConsultaFinalizada:
        """Guarda el resumen mínimo y elimina transcript e identidad."""
        _validar_calificacion(calificacion)
        fecha_lima = _serializar_lima(fecha_hora or _ahora_utc())

        with self._transaccion() as conexion:
            conversacion = conexion.execute(
                "SELECT * FROM conversaciones_activas WHERE id = ?",
                (conversacion_id,),
            ).fetchone()
            if conversacion is None:
                raise ConversacionNoEncontradaError(
                    f"No existe la conversación activa {conversacion_id}"
                )

            filas_categorias = conexion.execute(
                """
                SELECT categoria
                FROM mensajes_activos
                WHERE conversacion_id = ?
                  AND rol = 'bot'
                  AND categoria IS NOT NULL
                ORDER BY id
                """,
                (conversacion_id,),
            ).fetchall()
            categorias = self._deduplicar_categorias(
                fila["categoria"] for fila in filas_categorias
            )

            cursor = conexion.execute(
                """
                INSERT INTO consultas_finalizadas (
                    fecha_hora_cierre,
                    meses_bebe,
                    calificacion
                )
                VALUES (?, ?, ?)
                """,
                (
                    fecha_lima,
                    conversacion["meses_bebe"],
                    calificacion,
                ),
            )
            consulta_id = cursor.lastrowid

            for categoria in categorias:
                conexion.execute(
                    """
                    INSERT INTO categorias_consulta (
                        consulta_finalizada_id,
                        categoria,
                        categoria_normalizada
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        consulta_id,
                        categoria,
                        normalizar_categoria(categoria),
                    ),
                )

            conexion.execute(
                "DELETE FROM conversaciones_activas WHERE id = ?",
                (conversacion_id,),
            )

        return ConsultaFinalizada(
            id=int(consulta_id),
            fecha_hora_cierre=_parsear_fecha(fecha_lima),
            meses_bebe=conversacion["meses_bebe"],
            calificacion=calificacion,
            categorias=categorias,
        )

    def obtener_consulta_finalizada(
        self,
        consulta_id: int,
    ) -> ConsultaFinalizada | None:
        with self._conexion() as conexion:
            fila = conexion.execute(
                "SELECT * FROM consultas_finalizadas WHERE id = ?",
                (consulta_id,),
            ).fetchone()
            if fila is None:
                return None
            filas_categorias = conexion.execute(
                """
                SELECT categoria
                FROM categorias_consulta
                WHERE consulta_finalizada_id = ?
                ORDER BY id
                """,
                (consulta_id,),
            ).fetchall()

        return ConsultaFinalizada(
            id=fila["id"],
            fecha_hora_cierre=_parsear_fecha(fila["fecha_hora_cierre"]),
            meses_bebe=fila["meses_bebe"],
            calificacion=fila["calificacion"],
            categorias=tuple(
                categoria["categoria"] for categoria in filas_categorias
            ),
        )

    def borrar_conversacion_activa(self, conversacion_id: int) -> bool:
        """Borra una sesión activa sin generar un resumen (p. ej. reinicio)."""
        with self._transaccion() as conexion:
            cursor = conexion.execute(
                "DELETE FROM conversaciones_activas WHERE id = ?",
                (conversacion_id,),
            )
        return cursor.rowcount == 1

    def borrar_conversacion_de_usuario(
        self,
        canal: str,
        usuario_temporal: str,
    ) -> bool:
        canal = _validar_texto_no_vacio(canal, "canal")
        usuario_temporal = _validar_texto_no_vacio(
            usuario_temporal,
            "usuario_temporal",
        )
        with self._transaccion() as conexion:
            cursor = conexion.execute(
                """
                DELETE FROM conversaciones_activas
                WHERE canal = ? AND usuario_temporal = ?
                """,
                (canal, usuario_temporal),
            )
        return cursor.rowcount == 1

    def eliminar_conversacion(
        self,
        canal: str,
        usuario_temporal: str,
    ) -> bool:
        """Alias explícito para reiniciar la sesión de un usuario."""
        return self.borrar_conversacion_de_usuario(canal, usuario_temporal)

    def caducar_conversaciones(
        self,
        horas_inactividad: float = 24,
        *,
        ahora: datetime | None = None,
    ) -> int:
        """Elimina sesiones que exceden el límite, sin generar resúmenes."""
        if horas_inactividad <= 0:
            raise ValueError("horas_inactividad debe ser mayor que cero")
        fecha_actual = _asegurar_fecha_con_zona(
            ahora or _ahora_utc(),
            "ahora",
        )
        limite = _serializar_utc(
            fecha_actual.astimezone(timezone.utc)
            - timedelta(hours=horas_inactividad)
        )

        with self._transaccion() as conexion:
            cursor = conexion.execute(
                """
                DELETE FROM conversaciones_activas
                WHERE fecha_ultima_actividad_utc < ?
                """,
                (limite,),
            )
        return cursor.rowcount

    def eliminar_expiradas(
        self,
        horas: float = 24,
        *,
        ahora: datetime | None = None,
    ) -> int:
        """Alias para la limpieza de sesiones inactivas."""
        return self.caducar_conversaciones(
            horas_inactividad=horas,
            ahora=ahora,
        )

    @staticmethod
    def _existe_conversacion(
        conexion: sqlite3.Connection,
        conversacion_id: int,
    ) -> bool:
        return (
            conexion.execute(
                "SELECT 1 FROM conversaciones_activas WHERE id = ?",
                (conversacion_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _deduplicar_categorias(
        categorias: Iterable[str],
    ) -> tuple[str, ...]:
        resultado: list[str] = []
        vistas: set[str] = set()
        for categoria in categorias:
            normalizada = normalizar_categoria(categoria)
            if normalizada and normalizada not in vistas:
                vistas.add(normalizada)
                resultado.append(categoria.strip())
        return tuple(resultado)

    @staticmethod
    def _conversacion_desde_fila(
        fila: sqlite3.Row,
    ) -> ConversacionActiva:
        return ConversacionActiva(
            id=fila["id"],
            canal=fila["canal"],
            usuario_temporal=fila["usuario_temporal"],
            estado=fila["estado"],
            fecha_inicio_utc=_parsear_fecha(fila["fecha_inicio_utc"]),
            fecha_ultima_actividad_utc=_parsear_fecha(
                fila["fecha_ultima_actividad_utc"]
            ),
            meses_bebe=fila["meses_bebe"],
            alimentos_contexto=fila["alimentos_contexto"],
            calificacion_pendiente=fila["calificacion_pendiente"],
        )

    @staticmethod
    def _mensaje_desde_fila(fila: sqlite3.Row) -> MensajeActivo:
        return MensajeActivo(
            id=fila["id"],
            conversacion_id=fila["conversacion_id"],
            id_externo=fila["id_externo"],
            rol=fila["rol"],
            contenido=fila["contenido"],
            categoria=fila["categoria"],
            subcategoria=fila["subcategoria"],
            puntaje=fila["puntaje"],
            evidencia=(
                json.loads(fila["evidencia_json"])
                if fila["evidencia_json"] is not None
                else None
            ),
            fecha_hora_utc=_parsear_fecha(fila["fecha_hora_utc"]),
        )
