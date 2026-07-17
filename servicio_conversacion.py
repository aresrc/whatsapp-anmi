"""Orquestación del diálogo de ANMI independiente del canal de entrega."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from clasificador_google import (
    ClasificadorConsultas,
    ContextoClasificacion,
    ErrorClasificacionGoogle,
    SeleccionGoogle,
)
from conversaciones import ConversacionActiva, RepositorioConversaciones
from motor_conocimientos import (
    ReglaConocimiento,
    ResultadoBusqueda,
    buscar_mejor_regla,
    normalizar_texto,
)


ESTADO_ESPERANDO_MESES = "esperando_meses"
ESTADO_ESPERANDO_ALIMENTOS = "esperando_alimentos"
ESTADO_LISTA = "lista"
ESTADO_ESPERANDO_CALIFICACION = "esperando_calificacion"

MENSAJE_BIENVENIDA = (
    "👋 Hola, soy ANMI, tu Asistente Nutricional Materno infantil. 👶🥗\n\n"
    "¿Cuántos meses tiene tu bebé? Responde con un número del 6 al 24 "
    "o «no aplica»."
)
MENSAJE_MESES_INVALIDOS = (
    "Por favor, indica un solo número del 6 al 24 para los meses de tu "
    "bebé, o escribe «no aplica»."
)
MENSAJE_ALIMENTOS = "🍽️ ¿Qué alimentos logra comer actualmente tu bebé?"
MENSAJE_ALIMENTOS_INVALIDOS = (
    "Cuéntame brevemente qué alimentos logra comer tu bebé "
    "(máximo 500 caracteres)."
)
MENSAJE_LISTA = (
    "Gracias. Ahora vuelve a escribir tu consulta sobre nutrición o anemia. "
    "Cuando quieras terminar, escribe «fin»."
)
MENSAJE_LISTA_SIN_BEBE = (
    "Gracias. Ahora vuelve a escribir tu consulta sobre nutrición o anemia. "
    "Cuando quieras terminar, escribe «fin»."
)
MENSAJE_CALIFICACION = (
    "⭐ ¿Cómo calificarías la atención de ANMI?\n\n"
    "1. Mala ⭐\n"
    "2. Neutral ⭐⭐\n"
    "3. Buena ⭐⭐⭐\n"
    "4. Muy Buena ⭐⭐⭐⭐\n"
    "5. Excelente ⭐⭐⭐⭐⭐\n\n"
    "Responde solo con un número del 1 al 5."
)
MENSAJE_CALIFICACION_INVALIDA = (
    "La calificación debe ser un número entero del 1 al 5."
)
MENSAJE_NO_ENCONTRADO = (
    "No logré identificar tu consulta con suficiente seguridad. "
    "Por favor, reformúlala con más detalle indicando el tema, la edad "
    "y qué deseas saber."
)
MENSAJE_SALUDO_ACTIVO = (
    "Hola de nuevo 👋. Escribe tu consulta sobre nutrición o anemia, "
    "o «fin» para terminar."
)
MENSAJE_RECORDATORIO_FIN = (
    "📝 Recuerda: cuando quieras terminar la conversación, escribe «fin»."
)
MENSAJE_AGRADECIMIENTO = (
    "🙏✨ ¡Gracias por calificar tu experiencia con ANMI! 💚 "
    "Puedes volver a usar este chat cuando lo necesites. 👋😊"
)

SALUDOS = {
    "hola",
    "buen dia",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
}


@dataclass(frozen=True)
class RespuestaConversacion:
    """Mensaje que el adaptador debe entregar al usuario."""

    texto: str
    categoria: str | None = None
    subcategoria: str | None = None
    puntaje: float | None = None
    evidencia: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultadoConversacion:
    """Resultado de procesar una entrada en la máquina conversacional."""

    conversacion_id: int
    estado: str
    respuestas: tuple[RespuestaConversacion, ...]
    finalizacion_pendiente: bool = False
    duplicado: bool = False
    id_entrega: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def depuracion(self) -> dict[str, Any] | None:
        """Devuelve la evidencia de la última respuesta clasificada."""
        for respuesta in reversed(self.respuestas):
            if respuesta.evidencia:
                return respuesta.evidencia
        return None


ResultadoSeleccion = ResultadoBusqueda | SeleccionGoogle


def _serializar_resultado(resultado: ResultadoSeleccion) -> dict[str, Any]:
    """Convierte un resultado del motor en datos seguros para UI/SQLite."""
    if isinstance(resultado, SeleccionGoogle):
        regla = resultado.regla
        return {
            "categoria": regla.categoria,
            "subcategoria": regla.subcategoria,
            "puntaje": None,
            "margen": None,
            "coincidencias_exactas": [],
            "coincidencias_aproximadas": [],
            "candidatos": [],
            "motivo": "seleccion_google",
            "documento": regla.documento,
            "paginas": regla.paginas,
            "enlace": regla.enlace,
            **dict(resultado.evidencia),
            "origen": "google",
            "id_regla": resultado.id_regla,
        }

    aproximadas = [
        asdict(coincidencia)
        for coincidencia in resultado.coincidencias_aproximadas
    ]
    candidatos = [
        asdict(candidato)
        for candidato in resultado.candidatos_descartados
    ]

    return {
        "categoria": resultado.categoria,
        "subcategoria": resultado.subcategoria,
        "puntaje": round(resultado.puntaje, 4),
        "margen": round(resultado.margen, 4),
        "coincidencias_exactas": list(resultado.coincidencias_exactas),
        "coincidencias_aproximadas": aproximadas,
        "candidatos": candidatos,
        "motivo": resultado.motivo,
        "documento": resultado.documento,
        "paginas": resultado.paginas,
        "enlace": resultado.enlace,
    }


def _es_emergencia(resultado: ResultadoSeleccion | None) -> bool:
    if resultado is None:
        return False
    return "emergencia" in normalizar_texto(resultado.regla.categoria)


def _extraer_meses(texto: str) -> tuple[bool, int | None]:
    """Valida meses; ``None`` representa la respuesta «no aplica»."""
    if "+" in texto or "-" in texto:
        return False, None
    texto_normalizado = normalizar_texto(texto)
    if texto_normalizado == "no aplica":
        return True, None

    coincidencia = re.fullmatch(
        r"(?:tiene\s+)?(\d{1,2})(?:\s+mes(?:es)?)?",
        texto_normalizado,
    )
    if coincidencia is None:
        return False, None

    meses = int(coincidencia.group(1))
    return (0 <= meses <= 24), meses


def _extraer_calificacion(texto: str) -> int | None:
    texto_normalizado = normalizar_texto(texto)
    if not re.fullmatch(r"[1-5]", texto_normalizado):
        return None
    return int(texto_normalizado)


class ServicioConversacion:
    """Máquina de estados compartida por WhatsApp y el simulador."""

    def __init__(
        self,
        repositorio: RepositorioConversaciones,
        reglas: tuple[ReglaConocimiento, ...],
        *,
        horas_retencion: int = 24,
        clasificador: ClasificadorConsultas | None = None,
    ) -> None:
        self.repositorio = repositorio
        self.reglas = reglas
        self.horas_retencion = horas_retencion
        self.clasificador = clasificador

    @property
    def modo_clasificador(self) -> str:
        return "google" if self.clasificador is not None else "reglas"

    def inicializar(self) -> None:
        self.repositorio.inicializar()
        self.limpiar_expiradas()

    def limpiar_expiradas(self) -> int:
        return self.repositorio.eliminar_expiradas(self.horas_retencion)

    def obtener_mensajes(
        self,
        canal: str,
        usuario_temporal: str,
    ) -> list[Any]:
        conversacion = self.repositorio.obtener_conversacion(
            canal,
            usuario_temporal,
        )
        if conversacion is None:
            return []
        return self.repositorio.listar_mensajes(conversacion.id)

    def obtener_estado(
        self,
        canal: str,
        usuario_temporal: str,
    ) -> str | None:
        conversacion = self.repositorio.obtener_conversacion(
            canal,
            usuario_temporal,
        )
        return conversacion.estado if conversacion else None

    def reiniciar(self, canal: str, usuario_temporal: str) -> bool:
        return self.repositorio.eliminar_conversacion(
            canal,
            usuario_temporal,
        )

    def procesar_mensaje(
        self,
        canal: str,
        usuario_temporal: str,
        texto: str,
        *,
        mensaje_externo_id: str | None = None,
    ) -> ResultadoConversacion:
        """Procesa una entrada sin depender de Flask ni de Meta."""
        self.limpiar_expiradas()
        conversacion = self.repositorio.obtener_conversacion(
            canal,
            usuario_temporal,
        )

        if conversacion is None:
            conversacion = self.repositorio.crear_conversacion(
                canal,
                usuario_temporal,
                ESTADO_ESPERANDO_MESES,
            )
            registrado = self._registrar_usuario(
                conversacion,
                texto,
                mensaje_externo_id,
            )
            if not registrado:
                return ResultadoConversacion(
                    conversacion.id,
                    conversacion.estado,
                    tuple(),
                    duplicado=True,
                )
            return self._iniciar_conversacion(conversacion, texto)

        registrado = self._registrar_usuario(
            conversacion,
            texto,
            mensaje_externo_id,
        )
        if not registrado:
            return ResultadoConversacion(
                conversacion.id,
                conversacion.estado,
                tuple(),
                duplicado=True,
            )

        texto_normalizado = normalizar_texto(texto)
        if texto_normalizado == "fin":
            return self._solicitar_cierre(conversacion)

        if conversacion.estado == ESTADO_ESPERANDO_MESES:
            return self._procesar_meses(conversacion, texto)
        if conversacion.estado == ESTADO_ESPERANDO_ALIMENTOS:
            return self._procesar_alimentos(conversacion, texto)
        if conversacion.estado == ESTADO_ESPERANDO_CALIFICACION:
            return self._procesar_calificacion(conversacion, texto)

        return self._procesar_consulta(conversacion, texto)

    def confirmar_finalizacion(self, conversacion_id: int) -> int:
        """Compacta y elimina la sesión tras entregar el agradecimiento."""
        conversacion = self.repositorio.obtener_conversacion_por_id(
            conversacion_id
        )
        if conversacion is None:
            raise LookupError("La conversación ya no está activa")
        if conversacion.calificacion_pendiente is None:
            raise ValueError("La conversación no tiene calificación pendiente")
        consulta = self.repositorio.finalizar_conversacion(
            conversacion_id,
            conversacion.calificacion_pendiente,
        )
        return consulta.id

    def _registrar_usuario(
        self,
        conversacion: ConversacionActiva,
        texto: str,
        mensaje_externo_id: str | None,
    ) -> bool:
        return self.repositorio.registrar_mensaje(
            conversacion.id,
            "usuario",
            texto,
            mensaje_externo_id=mensaje_externo_id,
        )

    def confirmar_respuestas(
        self,
        resultado: ResultadoConversacion,
    ) -> int:
        """Registra únicamente las salidas confirmadas por el adaptador."""
        registradas = 0
        for indice, respuesta in enumerate(resultado.respuestas):
            registrada = self.repositorio.registrar_mensaje(
                resultado.conversacion_id,
                "bot",
                respuesta.texto,
                mensaje_externo_id=(
                    f"anmi-bot:{resultado.id_entrega}:{indice}"
                ),
                categoria=respuesta.categoria,
                subcategoria=respuesta.subcategoria,
                puntaje=respuesta.puntaje,
                evidencia=respuesta.evidencia or None,
            )
            if registrada:
                registradas += 1
        return registradas

    def _resultado(
        self,
        conversacion_id: int,
        estado: str,
        respuestas: tuple[RespuestaConversacion, ...],
        *,
        finalizacion_pendiente: bool = False,
    ) -> ResultadoConversacion:
        return ResultadoConversacion(
            conversacion_id=conversacion_id,
            estado=estado,
            respuestas=respuestas,
            finalizacion_pendiente=finalizacion_pendiente,
        )

    def _iniciar_conversacion(
        self,
        conversacion: ConversacionActiva,
        texto_inicial: str,
    ) -> ResultadoConversacion:
        respuestas: list[RespuestaConversacion] = []
        if normalizar_texto(texto_inicial) not in SALUDOS:
            resultado = self._seleccionar_regla(
                conversacion,
                texto_inicial,
                usar_contexto=False,
            )
            if _es_emergencia(resultado):
                assert resultado is not None
                respuestas.extend(self._respuestas_desde_regla(resultado))
        respuestas.append(RespuestaConversacion(MENSAJE_BIENVENIDA))
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_MESES,
            tuple(respuestas),
        )

    def _solicitar_cierre(
        self,
        conversacion: ConversacionActiva,
    ) -> ResultadoConversacion:
        if conversacion.estado == ESTADO_ESPERANDO_MESES:
            respuesta = RespuestaConversacion(
                "Antes de terminar necesito registrar los meses del bebé. "
                + MENSAJE_MESES_INVALIDOS
            )
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (respuesta,),
            )

        self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado=ESTADO_ESPERANDO_CALIFICACION,
        )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_CALIFICACION,
            (RespuestaConversacion(MENSAJE_CALIFICACION),),
        )

    def _procesar_meses(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        if normalizar_texto(texto) in SALUDOS:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (RespuestaConversacion(MENSAJE_BIENVENIDA),),
            )

        valido, meses = _extraer_meses(texto)
        if not valido:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (RespuestaConversacion(MENSAJE_MESES_INVALIDOS),),
            )

        if meses is None:
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_LISTA,
                meses_bebe=None,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_LISTA,
                (RespuestaConversacion(MENSAJE_LISTA_SIN_BEBE),),
            )

        self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado=ESTADO_ESPERANDO_ALIMENTOS,
            meses_bebe=meses,
        )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_ALIMENTOS,
            (RespuestaConversacion(MENSAJE_ALIMENTOS),),
        )

    def _procesar_alimentos(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        alimentos = texto.strip()
        if not alimentos or len(alimentos) > 500:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (RespuestaConversacion(MENSAJE_ALIMENTOS_INVALIDOS),),
            )

        self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado=ESTADO_LISTA,
            alimentos_contexto=alimentos,
        )
        return self._resultado(
            conversacion.id,
            ESTADO_LISTA,
            (RespuestaConversacion(MENSAJE_LISTA),),
        )

    def _procesar_calificacion(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        calificacion = _extraer_calificacion(texto)
        if calificacion is None:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (RespuestaConversacion(MENSAJE_CALIFICACION_INVALIDA),),
            )

        self.repositorio.actualizar_conversacion(
            conversacion.id,
            calificacion_pendiente=calificacion,
        )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_CALIFICACION,
            (RespuestaConversacion(MENSAJE_AGRADECIMIENTO),),
            finalizacion_pendiente=True,
        )

    def _procesar_consulta(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        if normalizar_texto(texto) in SALUDOS:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (RespuestaConversacion(MENSAJE_SALUDO_ACTIVO),),
            )

        resultado = self._seleccionar_regla(
            conversacion,
            texto,
            usar_contexto=True,
        )
        if resultado is None:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (RespuestaConversacion(MENSAJE_NO_ENCONTRADO),),
            )

        return self._resultado(
            conversacion.id,
            conversacion.estado,
            self._respuestas_desde_regla(resultado),
        )

    def _seleccionar_regla(
        self,
        conversacion: ConversacionActiva,
        texto: str,
        *,
        usar_contexto: bool,
    ) -> ResultadoSeleccion | None:
        categoria_anterior = (
            self.repositorio.ultima_categoria(conversacion.id)
            if usar_contexto
            else None
        )
        if self.clasificador is not None:
            contexto = self._crear_contexto_clasificacion(
                conversacion,
                categoria_anterior,
            )
            try:
                seleccion = self.clasificador.seleccionar(texto, contexto)
            except ErrorClasificacionGoogle as error:
                logging.warning(
                    "Gemini no pudo clasificar; se usarán reglas locales: %s",
                    error,
                )
            else:
                if seleccion is not None:
                    return seleccion

        if not usar_contexto:
            return buscar_mejor_regla(texto, self.reglas)
        return buscar_mejor_regla(
            texto,
            self.reglas,
            categoria_anterior=categoria_anterior,
            meses_bebe=conversacion.meses_bebe,
            alimentos_contexto=conversacion.alimentos_contexto,
        )

    def _crear_contexto_clasificacion(
        self,
        conversacion: ConversacionActiva,
        categoria_anterior: str | None,
    ) -> ContextoClasificacion:
        mensajes = self.repositorio.listar_mensajes(conversacion.id)
        consultas_clasificadas: list[str] = []
        usuario_pendiente: str | None = None
        subcategoria_anterior: str | None = None

        for mensaje in mensajes:
            if mensaje.rol == "usuario":
                usuario_pendiente = mensaje.contenido
                continue
            if mensaje.rol != "bot" or not mensaje.categoria:
                continue
            subcategoria_anterior = mensaje.subcategoria
            if usuario_pendiente is not None:
                consultas_clasificadas.append(usuario_pendiente[:1000])
                usuario_pendiente = None

        return ContextoClasificacion(
            meses_bebe=conversacion.meses_bebe,
            alimentos_contexto=conversacion.alimentos_contexto,
            categoria_anterior=categoria_anterior,
            subcategoria_anterior=subcategoria_anterior,
            consultas_anteriores=tuple(consultas_clasificadas[-2:]),
        )

    @staticmethod
    def _respuestas_desde_regla(
        resultado: ResultadoSeleccion,
    ) -> tuple[RespuestaConversacion, ...]:
        regla = resultado.regla
        enlace = regla.enlace.strip() or "No disponible"
        puntaje = (
            resultado.puntaje
            if isinstance(resultado, ResultadoBusqueda)
            else None
        )
        return (
            RespuestaConversacion(
                texto=f"{regla.respuesta.strip()}",
                categoria=regla.categoria,
                subcategoria=regla.subcategoria,
                puntaje=puntaje,
                evidencia=_serializar_resultado(resultado),
            ),
            RespuestaConversacion(
                texto=f"{regla.disclaimer.strip()}"
            ),
            RespuestaConversacion(
                texto=f"Documento:\n{regla.documento.strip()} pag. {regla.paginas.strip()}"
            ),
            RespuestaConversacion(texto=f"Fuente:\n{enlace}"),
            RespuestaConversacion(texto=MENSAJE_RECORDATORIO_FIN),
        )


class LimpiezaPeriodica:
    """Ejecuta la purga de sesiones vencidas mientras el proceso vive."""

    def __init__(
        self,
        servicio: ServicioConversacion,
        intervalo_segundos: int = 15 * 60,
    ) -> None:
        self.servicio = servicio
        self.intervalo_segundos = intervalo_segundos
        self._detener = threading.Event()
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> None:
        if self._hilo is not None and self._hilo.is_alive():
            return
        self._hilo = threading.Thread(
            target=self._ejecutar,
            name="anmi-limpieza-sesiones",
            daemon=True,
        )
        self._hilo.start()

    def detener(self) -> None:
        self._detener.set()

    def _ejecutar(self) -> None:
        while not self._detener.wait(self.intervalo_segundos):
            try:
                self.servicio.limpiar_expiradas()
            except Exception:
                logging.exception(
                    "No se pudieron limpiar las conversaciones expiradas"
                )
