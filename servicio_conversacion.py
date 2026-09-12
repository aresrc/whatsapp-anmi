"""Orquestación del diálogo de ANMI independiente del canal de entrega."""

from __future__ import annotations

import hashlib
import hmac
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
from conversaciones import (
    LONGITUD_MAXIMA_COMENTARIO,
    ConversacionActiva,
    RepositorioConversaciones,
)
from motor_conocimientos import (
    PerfilAlimentario,
    PerfilReceta,
    ReglaConocimiento,
    ResultadoBusqueda,
    buscar_mejor_regla,
    extraer_perfil_alimentario,
    filtrar_recetas_por_edad,
    nombres_ingredientes,
    normalizar_texto,
    obtener_grupos_alimentos,
    obtener_perfiles_recetas,
    rankear_recetas,
    seleccionar_receta_variada,
)
from taxonomia import (
    SECCIONES,
    candidatas_para_texto,
    jerarquia_de_regla,
    seccion_de_regla,
    titulo_visible,
)
from privacidad import redactar_datos_personales


ESTADO_ESPERANDO_MESES = "esperando_meses"
ESTADO_ESPERANDO_ALIMENTOS = "esperando_alimentos"
ESTADO_ESPERANDO_EDAD_RECETA = "esperando_edad_receta"
ESTADO_MENU_GENERAL = "menu_general"
ESTADO_MENU_ESPECIFICO = "menu_especifico"
ESTADO_LISTA = ESTADO_MENU_GENERAL  # Alias conservado para integraciones previas.
ESTADO_ESPERANDO_CALIFICACION = "esperando_calificacion"
ESTADO_ESPERANDO_DECISION_COMENTARIO = "esperando_decision_comentario"
ESTADO_ESPERANDO_COMENTARIO = "esperando_comentario"


@dataclass(frozen=True)
class OpcionRespuesta:
    """Opción interactiva que un adaptador puede presentar como botón."""

    id: str
    titulo: str
    descripcion: str = ""


OPCIONES_EDAD = (
    OpcionRespuesta("edad_6_8", "6 a 8 meses"),
    OpcionRespuesta("edad_9_11", "9 a 11 meses"),
    OpcionRespuesta("edad_12_23", "12 a 23 meses"),
)
OPCIONES_COMENTARIO = (
    OpcionRespuesta("comentario_si", "Sí"),
    OpcionRespuesta("comentario_no", "No"),
)
RANGOS_EDAD_POR_OPCION = {
    "edad_6_8": "6-8",
    "edad_9_11": "9-11",
    "edad_12_23": "12-23",
    # IDs previos aceptados únicamente para conversaciones ya iniciadas.
    "edad_6_12": "6-12",
    "edad_12_24": "12-24",
    "edad_24_36": "24-36",
}
TITULOS_OPCIONES = {opcion.id: opcion.titulo for opcion in OPCIONES_EDAD}
TITULOS_OPCIONES.update(
    {
        "edad_6_12": "6 a 12 meses",
        "edad_12_24": "12 a 24 meses",
        "edad_24_36": "24 a 36 meses",
    }
)

MENSAJE_BIENVENIDA = (
    "👋 Hola, soy ANMI, tu Asistente Nutricional Materno infantil. 👶🥗\n\n"
    "¿Qué edad tiene tu bebé? Elige un rango o responde con su edad exacta "
    "en meses (del 6 al 24). También puedes escribir «no aplica»."
)
MENSAJE_PRESENTACION_BETA = (
    "🧡 ANMI es un asistente nutricional materno-infantil que brinda información "
    "revisada sobre alimentación, nutrición y anemia para bebés y sus cuidadores. "
    "Actualmente se encuentra en fase beta y continúa mejorando. ANMI no "
    "reemplaza la evaluación ni las indicaciones de un profesional de salud."
)
MENSAJE_MESES_INVALIDOS = (
    "Por favor, elige un rango, indica un solo número del 6 al 36 para los "
    "meses de tu bebé, o escribe «no aplica»."
)
MENSAJE_ALIMENTOS = "🍽️ ¿Qué alimentos suele comer actualmente tu bebé?"
MENSAJE_ALIMENTOS_INVALIDOS = (
    "No logré reconocer alimentos de la lista. Cuéntame cuáles come, cuáles "
    "no le gustan y cuáles no puede consumir (máximo 500 caracteres)."
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
    "1. ⭐Mala\n"
    "2. ⭐⭐Neutral\n"
    "3. ⭐⭐⭐Buena\n"
    "4. ⭐⭐⭐⭐Muy Buena\n"
    "5. ⭐⭐⭐⭐⭐Excelente\n\n"
    "Responde solo con un número del 1 al 5."
)
MENSAJE_CALIFICACION_INVALIDA = (
    "La calificación debe ser un número entero del 1 al 5."
)
MENSAJE_DECISION_COMENTARIO = (
    "💬 ¿Deseas dejarnos un comentario sobre tu experiencia? Tus comentarios "
    "nos ayudarían a mejorar ANMI."
)
MENSAJE_DECISION_COMENTARIO_INVALIDA = (
    "Por favor, elige «Sí» o «No» para indicar si deseas dejar un comentario."
)
MENSAJE_SOLICITUD_COMENTARIO = (
    "Cuéntanos qué podríamos mejorar. Escribe tu comentario en un solo mensaje "
    f"(máximo {LONGITUD_MAXIMA_COMENTARIO} caracteres)."
)
MENSAJE_COMENTARIO_INVALIDO = (
    "El comentario debe tener entre 1 y "
    f"{LONGITUD_MAXIMA_COMENTARIO} caracteres. También puedes escribir «no» "
    "o «fin» para omitirlo."
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
    "🙏✨ ¡Gracias por calificar tu experiencia con ANMI! 💚\n\n"
    "ANMI se encuentra en fase beta y estamos trabajando para asegurarnos de "
    "que tengas la mejor experiencia posible.\n\n"
    "Puedes volver a usar este chat cuando lo necesites. 👋😊"
)
MENSAJE_RECETA_SIN_EDAD = (
    "Para recomendar una receta MINSA revisada necesito conocer la edad del "
    "bebé con precisión. Responde con un solo número de meses, entre 6 y 36."
)
MENSAJE_RECETA_SIN_PERFIL = (
    "Para elegir una receta compatible necesito saber qué alimentos come, "
    "cuáles no le gustan y cuáles no puede consumir."
)
MENSAJE_RECETA_SIN_COBERTURA = (
    "No tengo una receta MINSA revisada en la base de conocimientos para esa "
    "edad. No recomendaré una receta de otro rango. Consulta con un "
    "profesional de salud para recibir una indicación adecuada."
)
MENSAJE_RECETA_EXCLUIDA = (
    "No encontré una receta revisada para ese rango que evite todos los "
    "ingredientes que indicaste como alergia, intolerancia o alimento que no "
    "puede consumir. No recomendaré una receta insegura."
)
MENSAJE_MENU_GENERAL = (
    "También puedes explorar la información revisada. Elige un tema "
    "general o escribe directamente tu pregunta."
)
MENSAJE_MENU_ESPECIFICO = (
    "Elige un tema específico para recibir la información revisada. "
    "También puedes escribir directamente tu pregunta."
)
MENSAJE_EDAD_ACTUALIZADA = (
    "He actualizado la edad del bebé a {meses} meses. "
    "Ahora escribe tu consulta sobre nutrición o anemia."
)

SALUDOS = {
    "hola",
    "buen dia",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
}

TAMANO_PAGINA_CATEGORIAS = 8
TAMANO_PAGINA_SUBCATEGORIAS = 7
PREFIJO_CATEGORIA = "menu_cat:"
PREFIJO_REGLA = "menu_regla:"
PREFIJO_PAGINA_GENERAL = "menu_general:"
PREFIJO_PAGINA_ESPECIFICA = "menu_especifico:"
ID_VOLVER_CATEGORIAS = "menu_volver_categorias"
PREFIJO_RECETA_COMPATIBLE = "receta_compatible:"
ID_COMENTARIO_SI = "comentario_si"
ID_COMENTARIO_NO = "comentario_no"


@dataclass(frozen=True)
class RespuestaConversacion:
    """Mensaje que el adaptador debe entregar al usuario."""

    texto: str
    categoria: str | None = None
    subcategoria: str | None = None
    puntaje: float | None = None
    evidencia: dict[str, Any] = field(default_factory=dict)
    opciones: tuple[OpcionRespuesta, ...] = ()
    tipo_opciones: str = "botones"
    etiqueta_lista: str = "Ver opciones"


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
        "id_regla": resultado.regla.id_regla,
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
    return (6 <= meses <= 36), meses


def _extraer_rango_edad(texto: str) -> str | None:
    if texto in RANGOS_EDAD_POR_OPCION:
        return RANGOS_EDAD_POR_OPCION[texto]
    normalizado = normalizar_texto(texto)
    for opcion in OPCIONES_EDAD:
        if normalizado == normalizar_texto(opcion.titulo):
            return RANGOS_EDAD_POR_OPCION[opcion.id]
    return None


def _respuesta_bienvenida() -> RespuestaConversacion:
    return RespuestaConversacion(
        MENSAJE_BIENVENIDA,
        opciones=OPCIONES_EDAD,
    )


def _resultado_forzado(
    regla: ReglaConocimiento,
    motivo: str,
    coincidencias: tuple[str, ...],
) -> ResultadoBusqueda:
    return ResultadoBusqueda(
        regla=regla,
        puntaje=100.0,
        margen=1.0,
        coincidencias_exactas=coincidencias,
        coincidencias_aproximadas=(),
        motivo=motivo,
    )


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
        secreto_identidad: str | None = None,
    ) -> None:
        if secreto_identidad and len(secreto_identidad) < 32:
            raise ValueError(
                "secreto_identidad debe tener al menos 32 caracteres"
            )
        self.repositorio = repositorio
        self.reglas = reglas
        self.horas_retencion = horas_retencion
        self.clasificador = clasificador
        self._secreto_identidad = (
            secreto_identidad.encode("utf-8")
            if isinstance(secreto_identidad, str) and secreto_identidad
            else None
        )
        self.perfiles_recetas = obtener_perfiles_recetas(reglas)
        self.grupos_alimentos = obtener_grupos_alimentos(
            self.perfiles_recetas
        )
        self.reglas_por_id = {
            regla.id_regla: regla for regla in reglas if regla.id_regla
        }
        self.categorias = SECCIONES
        rutas_por_regla = {
            regla.id_regla: jerarquia_de_regla(regla).niveles
            for regla in reglas
        }
        self.hijos_por_categoria: dict[str, tuple[str, ...]] = {}
        reglas_por_ruta: dict[str, list[ReglaConocimiento]] = {}
        for regla in reglas:
            ruta = rutas_por_regla[regla.id_regla]
            for indice in range(1, len(ruta)):
                padre = "|".join(ruta[:indice])
                hijo = "|".join(ruta[: indice + 1])
                existentes = list(self.hijos_por_categoria.get(padre, ()))
                if hijo not in existentes:
                    existentes.append(hijo)
                    self.hijos_por_categoria[padre] = tuple(existentes)
            reglas_por_ruta.setdefault("|".join(ruta), []).append(regla)
        self.reglas_por_categoria = {
            ruta: tuple(reglas_ruta)
            for ruta, reglas_ruta in reglas_por_ruta.items()
        }
        self.nodos_categoria = tuple(
            dict.fromkeys(
                nodo
                for nodo in (*self.hijos_por_categoria, *self.reglas_por_categoria)
                if "|" in nodo
            )
        )

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
        # Defensa en profundidad: también protege simulador e integraciones
        # que no pasan por el adaptador Flask.
        texto = redactar_datos_personales(texto).texto
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
            if normalizar_texto(texto) == "fin":
                return self._solicitar_cierre(conversacion)
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
        if conversacion.estado == ESTADO_ESPERANDO_CALIFICACION:
            if texto_normalizado == "fin":
                return self._solicitar_cierre(conversacion)
            return self._procesar_calificacion(conversacion, texto)
        if conversacion.estado == ESTADO_ESPERANDO_DECISION_COMENTARIO:
            return self._procesar_decision_comentario(conversacion, texto)
        if conversacion.estado == ESTADO_ESPERANDO_COMENTARIO:
            return self._procesar_comentario(conversacion, texto)
        if texto_normalizado == "fin":
            return self._solicitar_cierre(conversacion)

        if conversacion.estado == ESTADO_ESPERANDO_MESES:
            return self._procesar_meses(conversacion, texto)
        if conversacion.estado == ESTADO_ESPERANDO_ALIMENTOS:
            return self._procesar_alimentos(conversacion, texto)
        if conversacion.estado == ESTADO_ESPERANDO_EDAD_RECETA:
            return self._procesar_edad_receta(conversacion, texto)
        texto_opcion = texto.strip()
        if texto_opcion.startswith(PREFIJO_RECETA_COMPATIBLE):
            return self._procesar_receta_compatible(
                conversacion,
                texto_opcion,
            )
        if texto_opcion.startswith("menu_"):
            return self._procesar_menu(conversacion, texto_opcion)

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
            comentario=conversacion.comentario_pendiente,
        )
        return consulta.id

    def _registrar_primera_interaccion(
        self,
        conversacion: ConversacionActiva,
    ) -> bool:
        if self._secreto_identidad is None:
            raise RuntimeError(
                "ANMI_USER_HASH_SECRET es obligatorio para reconocer usuarios"
            )
        identidad = (
            f"{conversacion.canal}\0{conversacion.usuario_temporal}"
        ).encode("utf-8")
        huella = hmac.new(
            self._secreto_identidad,
            identidad,
            hashlib.sha256,
        ).hexdigest()
        return self.repositorio.registrar_usuario_conocido(huella)

    def _registrar_usuario(
        self,
        conversacion: ConversacionActiva,
        texto: str,
        mensaje_externo_id: str | None,
    ) -> bool:
        contenido = self._texto_visible_opcion(texto)
        return self.repositorio.registrar_mensaje(
            conversacion.id,
            "usuario",
            contenido,
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

    def _respuesta_alimentos(self) -> RespuestaConversacion:
        return RespuestaConversacion(
            "\n\n".join(
                (
                    MENSAJE_ALIMENTOS,
                    "Por ejemplo: una base como papa o camote, un alimento "
                    "con hierro como sangrecita o bazo, y un acompañamiento "
                    "como zapallo o zanahoria.",
                    "También dime si hay algo que no le gusta o no puede "
                    "consumir.",
                )
            )
        )

    @staticmethod
    def _respuesta_decision_comentario(
        texto: str = MENSAJE_DECISION_COMENTARIO,
    ) -> RespuestaConversacion:
        return RespuestaConversacion(
            texto,
            opciones=OPCIONES_COMENTARIO,
        )

    @staticmethod
    def _acortar_titulo(texto: str, limite: int = 24) -> str:
        texto = texto.strip()
        if len(texto) <= limite:
            return texto
        return texto[: limite - 1].rstrip() + "…"

    def _respuesta_menu_general(self, pagina: int) -> RespuestaConversacion:
        total_paginas = max(
            1,
            (len(self.categorias) + TAMANO_PAGINA_CATEGORIAS - 1)
            // TAMANO_PAGINA_CATEGORIAS,
        )
        pagina = min(max(pagina, 0), total_paginas - 1)
        inicio = pagina * TAMANO_PAGINA_CATEGORIAS
        opciones = [
            OpcionRespuesta(
                id=f"{PREFIJO_CATEGORIA}{indice}",
                titulo=self._acortar_titulo(self.categorias[indice]),
                descripcion=f"{self._contar_temas_nodo(self.categorias[indice])} temas",
            )
            for indice in range(
                inicio,
                min(inicio + TAMANO_PAGINA_CATEGORIAS, len(self.categorias)),
            )
        ]
        if pagina > 0:
            opciones.append(
                OpcionRespuesta(
                    f"{PREFIJO_PAGINA_GENERAL}{pagina - 1}",
                    "⬅ Anterior",
                )
            )
        if pagina + 1 < total_paginas:
            opciones.append(
                OpcionRespuesta(
                    f"{PREFIJO_PAGINA_GENERAL}{pagina + 1}",
                    "Siguiente ➡",
                )
            )
        return RespuestaConversacion(
            texto=(
                f"{MENSAJE_MENU_GENERAL}\n\n"
                f"Página {pagina + 1} de {total_paginas}."
            ),
            opciones=tuple(opciones),
            tipo_opciones="lista",
            etiqueta_lista="Ver categorías",
        )

    def _respuesta_menu_especifico(
        self,
        categoria: str,
        pagina: int,
    ) -> RespuestaConversacion:
        hijos = self.hijos_por_categoria.get(categoria, ())
        if hijos:
            opciones = tuple(
                OpcionRespuesta(
                    id=f"menu_nodo:{self.nodos_categoria.index(hijo)}",
                    titulo=self._acortar_titulo(hijo.rsplit("|", 1)[-1]),
                    descripcion=f"{self._contar_temas_nodo(hijo)} temas",
                )
                for hijo in hijos
            )
            return RespuestaConversacion(
                texto=f"{MENSAJE_MENU_ESPECIFICO}\n\n*{categoria.rsplit('|', 1)[-1]}*",
                opciones=opciones,
                tipo_opciones="lista",
                etiqueta_lista="Ver categorías",
            )
        reglas = self.reglas_por_categoria.get(categoria, ())
        total_paginas = max(
            1,
            (len(reglas) + TAMANO_PAGINA_SUBCATEGORIAS - 1)
            // TAMANO_PAGINA_SUBCATEGORIAS,
        )
        pagina = min(max(pagina, 0), total_paginas - 1)
        indice_categoria = self.nodos_categoria.index(categoria)
        inicio = pagina * TAMANO_PAGINA_SUBCATEGORIAS
        opciones = [
            OpcionRespuesta(
                id=f"{PREFIJO_REGLA}{regla.id_regla}",
                titulo=self._acortar_titulo(titulo_visible(regla)),
                descripcion=(
                    titulo_visible(regla)
                    if len(titulo_visible(regla)) > 24
                    else categoria
                ),
            )
            for regla in reglas[
                inicio : inicio + TAMANO_PAGINA_SUBCATEGORIAS
            ]
        ]
        opciones.append(
            OpcionRespuesta(ID_VOLVER_CATEGORIAS, "↩ Categorías")
        )
        if pagina > 0:
            opciones.append(
                OpcionRespuesta(
                    (
                        f"{PREFIJO_PAGINA_ESPECIFICA}"
                        f"{indice_categoria}:{pagina - 1}"
                    ),
                    "⬅ Anterior",
                )
            )
        if pagina + 1 < total_paginas:
            opciones.append(
                OpcionRespuesta(
                    (
                        f"{PREFIJO_PAGINA_ESPECIFICA}"
                        f"{indice_categoria}:{pagina + 1}"
                    ),
                    "Siguiente ➡",
                )
            )
        return RespuestaConversacion(
            texto=(
                f"{MENSAJE_MENU_ESPECIFICO}\n\n"
                f"*{categoria}* — página {pagina + 1} de {total_paginas}."
            ),
            opciones=tuple(opciones),
            tipo_opciones="lista",
            etiqueta_lista="Ver temas",
        )

    def obtener_respuesta_interactiva(
        self,
        canal: str,
        usuario_temporal: str,
    ) -> RespuestaConversacion | None:
        """Reconstruye las opciones vigentes para adaptadores con recarga."""

        conversacion = self.repositorio.obtener_conversacion(
            canal,
            usuario_temporal,
        )
        if conversacion is None:
            return None
        if conversacion.estado == ESTADO_ESPERANDO_MESES:
            return _respuesta_bienvenida()
        if conversacion.estado == ESTADO_ESPERANDO_DECISION_COMENTARIO:
            return self._respuesta_decision_comentario()
        if conversacion.estado == ESTADO_MENU_GENERAL:
            return self._respuesta_menu_general(conversacion.pagina_menu)
        if (
            conversacion.estado == ESTADO_MENU_ESPECIFICO
            and conversacion.categoria_menu in (
                self.reglas_por_categoria | self.hijos_por_categoria
            )
        ):
            assert conversacion.categoria_menu is not None
            return self._respuesta_menu_especifico(
                conversacion.categoria_menu,
                conversacion.pagina_menu,
            )
        return None

    def _texto_visible_opcion(self, texto: str) -> str:
        if texto in TITULOS_OPCIONES:
            return TITULOS_OPCIONES[texto]
        if texto == ID_COMENTARIO_SI:
            return "Sí"
        if texto == ID_COMENTARIO_NO:
            return "No"
        if texto == ID_VOLVER_CATEGORIAS:
            return "Volver a categorías"
        if texto.startswith(PREFIJO_CATEGORIA):
            try:
                indice = int(texto.removeprefix(PREFIJO_CATEGORIA))
                return self.categorias[indice]
            except (ValueError, IndexError):
                return texto
        if texto.startswith(PREFIJO_REGLA):
            regla = self.reglas_por_id.get(texto.removeprefix(PREFIJO_REGLA))
            return regla.subcategoria if regla else texto
        if texto.startswith(PREFIJO_PAGINA_GENERAL) or texto.startswith(
            PREFIJO_PAGINA_ESPECIFICA
        ):
            return "Cambiar página del menú"
        return texto

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
        if self._registrar_primera_interaccion(conversacion):
            respuestas.append(RespuestaConversacion(MENSAJE_PRESENTACION_BETA))
        respuestas.append(_respuesta_bienvenida())
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_MESES,
            tuple(respuestas),
        )

    def _solicitar_cierre(
        self,
        conversacion: ConversacionActiva,
    ) -> ResultadoConversacion:
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
                (_respuesta_bienvenida(),),
            )

        rango_edad = _extraer_rango_edad(texto)
        if rango_edad is not None:
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_ESPERANDO_ALIMENTOS,
                meses_bebe=None,
                rango_edad_bebe=rango_edad,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_ESPERANDO_ALIMENTOS,
                (self._respuesta_alimentos(),),
            )

        valido, meses = _extraer_meses(texto)
        if not valido:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (RespuestaConversacion(MENSAJE_MESES_INVALIDOS),),
            )

        if meses is None:
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_MENU_GENERAL,
                meses_bebe=None,
                rango_edad_bebe=None,
                categoria_menu=None,
                pagina_menu=0,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_MENU_GENERAL,
                (
                    RespuestaConversacion(MENSAJE_LISTA_SIN_BEBE),
                    self._respuesta_menu_general(0),
                ),
            )

        self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado=ESTADO_ESPERANDO_ALIMENTOS,
            meses_bebe=meses,
            rango_edad_bebe=None,
        )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_ALIMENTOS,
            (self._respuesta_alimentos(),),
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

        perfil = extraer_perfil_alimentario(alimentos)
        if not perfil.reconocido:
            return self._resultado(
                conversacion.id,
                conversacion.estado,
                (
                    RespuestaConversacion(MENSAJE_ALIMENTOS_INVALIDOS),
                    self._respuesta_alimentos(),
                ),
            )

        if conversacion.rango_edad_bebe in {"6-12", "12-24", "24-36"}:
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_ESPERANDO_EDAD_RECETA,
                alimentos_contexto=alimentos,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_ESPERANDO_EDAD_RECETA,
                (RespuestaConversacion(MENSAJE_RECETA_SIN_EDAD),),
            )

        conversacion = self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado=ESTADO_MENU_GENERAL,
            alimentos_contexto=alimentos,
            categoria_menu=None,
            pagina_menu=0,
        )
        respuestas = list(
            self._respuestas_receta_personalizada(
                conversacion,
                alimentos,
                perfil,
            )
        )
        respuestas.append(self._respuesta_menu_general(0))
        return self._resultado(
            conversacion.id,
            ESTADO_MENU_GENERAL,
            tuple(respuestas),
        )

    def _procesar_edad_receta(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        valido, meses = _extraer_meses(texto)
        if not valido or meses is None:
            return self._resultado(
                conversacion.id,
                ESTADO_ESPERANDO_EDAD_RECETA,
                (RespuestaConversacion(MENSAJE_RECETA_SIN_EDAD),),
            )

        conversacion = self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado=(
                ESTADO_MENU_GENERAL
                if conversacion.alimentos_contexto
                else ESTADO_ESPERANDO_ALIMENTOS
            ),
            meses_bebe=meses,
            rango_edad_bebe=None,
        )
        if conversacion.alimentos_contexto:
            perfil = extraer_perfil_alimentario(
                conversacion.alimentos_contexto
            )
            if perfil.reconocido:
                respuestas = list(
                    self._respuestas_receta_personalizada(
                        conversacion,
                        conversacion.alimentos_contexto,
                        perfil,
                    )
                )
                respuestas.append(self._respuesta_menu_general(0))
                return self._resultado(
                    conversacion.id,
                    ESTADO_MENU_GENERAL,
                    tuple(respuestas),
                )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_ALIMENTOS,
            (self._respuesta_alimentos(),),
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
            estado=ESTADO_ESPERANDO_DECISION_COMENTARIO,
            calificacion_pendiente=calificacion,
            comentario_pendiente=None,
        )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_DECISION_COMENTARIO,
            (self._respuesta_decision_comentario(),),
        )

    def _procesar_decision_comentario(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        texto_normalizado = normalizar_texto(texto)
        if texto == ID_COMENTARIO_SI or texto_normalizado == "si":
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_ESPERANDO_COMENTARIO,
                comentario_pendiente=None,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_ESPERANDO_COMENTARIO,
                (RespuestaConversacion(MENSAJE_SOLICITUD_COMENTARIO),),
            )
        if (
            texto == ID_COMENTARIO_NO
            or texto_normalizado in {"no", "fin"}
        ):
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                comentario_pendiente=None,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_ESPERANDO_DECISION_COMENTARIO,
                (RespuestaConversacion(MENSAJE_AGRADECIMIENTO),),
                finalizacion_pendiente=True,
            )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_DECISION_COMENTARIO,
            (
                self._respuesta_decision_comentario(
                    MENSAJE_DECISION_COMENTARIO_INVALIDA
                ),
            ),
        )

    def _procesar_comentario(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        texto_normalizado = normalizar_texto(texto)
        if texto_normalizado in {"no", "fin"}:
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                comentario_pendiente=None,
            )
        else:
            comentario = texto.strip()
            if (
                not comentario
                or len(comentario) > LONGITUD_MAXIMA_COMENTARIO
            ):
                return self._resultado(
                    conversacion.id,
                    ESTADO_ESPERANDO_COMENTARIO,
                    (RespuestaConversacion(MENSAJE_COMENTARIO_INVALIDO),),
                )
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                comentario_pendiente=comentario,
            )
        return self._resultado(
            conversacion.id,
            ESTADO_ESPERANDO_COMENTARIO,
            (RespuestaConversacion(MENSAJE_AGRADECIMIENTO),),
            finalizacion_pendiente=True,
        )

    def _procesar_menu(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        if texto == ID_VOLVER_CATEGORIAS:
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_MENU_GENERAL,
                categoria_menu=None,
                pagina_menu=0,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_MENU_GENERAL,
                (self._respuesta_menu_general(0),),
            )

        if texto.startswith(PREFIJO_PAGINA_GENERAL):
            try:
                pagina = int(texto.removeprefix(PREFIJO_PAGINA_GENERAL))
            except ValueError:
                return self._respuesta_opcion_invalida(conversacion)
            ultima_pagina = max(
                0,
                (len(self.categorias) - 1) // TAMANO_PAGINA_CATEGORIAS,
            )
            if pagina < 0 or pagina > ultima_pagina:
                return self._respuesta_opcion_invalida(conversacion)
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_MENU_GENERAL,
                categoria_menu=None,
                pagina_menu=pagina,
            )
            respuesta = self._respuesta_menu_general(pagina)
            return self._resultado(
                conversacion.id,
                ESTADO_MENU_GENERAL,
                (respuesta,),
            )

        if texto.startswith(PREFIJO_CATEGORIA):
            try:
                indice = int(texto.removeprefix(PREFIJO_CATEGORIA))
            except ValueError:
                return self._respuesta_opcion_invalida(conversacion)
            if not 0 <= indice < len(self.categorias):
                return self._respuesta_opcion_invalida(conversacion)
            categoria = self.categorias[indice]
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_MENU_ESPECIFICO,
                categoria_menu=categoria,
                pagina_menu=0,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_MENU_ESPECIFICO,
                (self._respuesta_menu_especifico(categoria, 0),),
            )

        if texto.startswith("menu_nodo:"):
            try:
                indice = int(texto.removeprefix("menu_nodo:"))
                categoria = self.nodos_categoria[indice]
            except (ValueError, IndexError):
                return self._respuesta_opcion_invalida(conversacion)
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_MENU_ESPECIFICO,
                categoria_menu=categoria,
                pagina_menu=0,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_MENU_ESPECIFICO,
                (self._respuesta_menu_especifico(categoria, 0),),
            )

        if texto.startswith(PREFIJO_PAGINA_ESPECIFICA):
            valores = texto.removeprefix(PREFIJO_PAGINA_ESPECIFICA).split(":")
            try:
                indice, pagina = (int(valor) for valor in valores)
            except (ValueError, TypeError):
                return self._respuesta_opcion_invalida(conversacion)
            if (
                len(valores) != 2
                or not 0 <= indice < len(self.nodos_categoria)
                or pagina < 0
            ):
                return self._respuesta_opcion_invalida(conversacion)
            categoria = self.nodos_categoria[indice]
            ultima_pagina = max(
                0,
                (len(self.reglas_por_categoria.get(categoria, ())) - 1)
                // TAMANO_PAGINA_SUBCATEGORIAS,
            )
            if pagina > ultima_pagina:
                return self._respuesta_opcion_invalida(conversacion)
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_MENU_ESPECIFICO,
                categoria_menu=categoria,
                pagina_menu=pagina,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_MENU_ESPECIFICO,
                (self._respuesta_menu_especifico(categoria, pagina),),
            )

        if texto.startswith(PREFIJO_REGLA):
            id_regla = texto.removeprefix(PREFIJO_REGLA)
            regla = self.reglas_por_id.get(id_regla)
            if (
                regla is None
                or conversacion.estado != ESTADO_MENU_ESPECIFICO
                or "|".join(jerarquia_de_regla(regla).niveles)
                != conversacion.categoria_menu
            ):
                return self._respuesta_opcion_invalida(conversacion)
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_MENU_GENERAL,
                categoria_menu=None,
                pagina_menu=0,
            )
            resultado = _resultado_forzado(
                regla,
                "seleccion_menu",
                (regla.id_regla,),
            )
            respuestas = list(self._respuestas_desde_regla(resultado))
            respuestas.append(self._respuesta_menu_general(0))
            return self._resultado(
                conversacion.id,
                ESTADO_MENU_GENERAL,
                tuple(respuestas),
            )

        return self._respuesta_opcion_invalida(conversacion)

    def _respuesta_opcion_invalida(
        self,
        conversacion: ConversacionActiva,
    ) -> ResultadoConversacion:
        respuesta_actual = self.obtener_respuesta_interactiva(
            conversacion.canal,
            conversacion.usuario_temporal,
        )
        respuestas = [
            RespuestaConversacion(
                "La opción del menú no es válida o ya venció. Elige una "
                "opción vigente o escribe tu pregunta."
            )
        ]
        if respuesta_actual is not None:
            respuestas.append(respuesta_actual)
        return self._resultado(
            conversacion.id,
            conversacion.estado,
            tuple(respuestas),
        )

    def _procesar_consulta(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        if normalizar_texto(texto) in SALUDOS:
            return self._resultado_con_menu(
                conversacion,
                (RespuestaConversacion(MENSAJE_SALUDO_ACTIVO),),
            )

        edad_valida, meses = _extraer_meses(texto)
        if edad_valida and meses is not None:
            conversacion = self.repositorio.actualizar_perfil(
                conversacion.id,
                meses_bebe=meses,
                rango_edad_bebe=None,
            )
            return self._resultado_con_menu(
                conversacion,
                (
                    RespuestaConversacion(
                        MENSAJE_EDAD_ACTUALIZADA.format(meses=meses)
                    ),
                ),
            )

        conversacion = self._actualizar_edad_explicita(
            conversacion,
            texto,
        )

        perfil_actualizacion = extraer_perfil_alimentario(texto)
        if perfil_actualizacion.reconocido:
            contexto_anterior = conversacion.alimentos_contexto or ""
            contexto_actualizado = "\n".join(
                parte for parte in (contexto_anterior, texto.strip()) if parte
            )
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                alimentos_contexto=contexto_actualizado[-1000:],
            )

        if self._es_solicitud_receta(texto):
            meses_receta = self._extraer_meses_consulta(
                normalizar_texto(texto)
            )
            if meses_receta is not None and 6 <= meses_receta <= 36:
                conversacion = self.repositorio.actualizar_perfil(
                    conversacion.id,
                    meses_bebe=meses_receta,
                    rango_edad_bebe=None,
                )
            if (
                conversacion.meses_bebe is None
                and conversacion.rango_edad_bebe
                not in {"6-8", "9-11", "12-23"}
            ):
                return self._resultado_con_menu(
                    conversacion,
                    (RespuestaConversacion(MENSAJE_RECETA_SIN_EDAD),),
                )
            perfil = extraer_perfil_alimentario(
                conversacion.alimentos_contexto or ""
            )
            if perfil.reconocido:
                return self._resultado_con_menu(
                    conversacion,
                    self._respuestas_receta_personalizada(
                        conversacion,
                        conversacion.alimentos_contexto or texto,
                        perfil,
                    ),
                )
            conversacion = self.repositorio.actualizar_conversacion(
                conversacion.id,
                estado=ESTADO_ESPERANDO_ALIMENTOS,
                categoria_menu=None,
                pagina_menu=0,
            )
            return self._resultado(
                conversacion.id,
                ESTADO_ESPERANDO_ALIMENTOS,
                (
                    RespuestaConversacion(MENSAJE_RECETA_SIN_PERFIL),
                    self._respuesta_alimentos(),
                ),
            )

        resultado_hemoglobina = self._seleccionar_hemoglobina_general(texto)
        if resultado_hemoglobina is not None:
            return self._resultado_con_menu(
                conversacion,
                self._respuestas_desde_regla(resultado_hemoglobina),
            )

        resultado = self._seleccionar_regla(
            conversacion,
            texto,
            usar_contexto=True,
        )
        if resultado is None:
            return self._resultado_con_menu(
                conversacion,
                (RespuestaConversacion(MENSAJE_NO_ENCONTRADO),),
            )

        return self._resultado_con_menu(
            conversacion,
            self._respuestas_desde_regla(resultado),
        )

    def _resultado_con_menu(
        self,
        conversacion: ConversacionActiva,
        respuestas: tuple[RespuestaConversacion, ...],
    ) -> ResultadoConversacion:
        conversacion = self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado=ESTADO_MENU_GENERAL,
            categoria_menu=None,
            pagina_menu=0,
        )
        return self._resultado(
            conversacion.id,
            ESTADO_MENU_GENERAL,
            respuestas + (self._respuesta_menu_general(0),),
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
        # La búsqueda local es gratuita y suficiente para la mayoría de
        # consultas. Gemini queda reservado para ambigüedad o texto libre que
        # no alcanza evidencia revisada.
        resultado_local = (
            buscar_mejor_regla(texto, self.reglas)
            if not usar_contexto
            else buscar_mejor_regla(
                texto,
                self.reglas,
                categoria_anterior=categoria_anterior,
                meses_bebe=conversacion.meses_bebe,
                rango_edad_bebe=conversacion.rango_edad_bebe,
                alimentos_contexto=conversacion.alimentos_contexto,
            )
        )

    def _contar_temas_nodo(self, nodo: str) -> int:
        return sum(
            len(reglas)
            for ruta, reglas in self.reglas_por_categoria.items()
            if ruta == nodo or ruta.startswith(f"{nodo}|")
        )
        if resultado_local is not None:
            return resultado_local

        if self.clasificador is not None:
            ids_candidatos = candidatas_para_texto(self.reglas, texto)
            if not ids_candidatos:
                return None
            contexto = self._crear_contexto_clasificacion(
                conversacion,
                categoria_anterior,
                ids_candidatos=ids_candidatos,
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

        return None

    def _crear_contexto_clasificacion(
        self,
        conversacion: ConversacionActiva,
        categoria_anterior: str | None,
        *,
        perfil_alimentario: PerfilAlimentario | None = None,
        recetas_permitidas: tuple[PerfilReceta, ...] = (),
        ids_candidatos: tuple[str, ...] = (),
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
            rango_edad_bebe=conversacion.rango_edad_bebe,
            alimentos_contexto=conversacion.alimentos_contexto,
            categoria_anterior=categoria_anterior,
            subcategoria_anterior=subcategoria_anterior,
            consultas_anteriores=tuple(consultas_clasificadas[-2:]),
            alimentos_aceptados=(
                perfil_alimentario.aceptados if perfil_alimentario else ()
            ),
            alimentos_rechazados=(
                perfil_alimentario.rechazados if perfil_alimentario else ()
            ),
            alimentos_excluidos=(
                perfil_alimentario.excluidos if perfil_alimentario else ()
            ),
            ids_permitidos=tuple(
                perfil.regla.id_regla for perfil in recetas_permitidas
            ),
            ids_candidatos=ids_candidatos,
            ingredientes_por_id={
                perfil.regla.id_regla: nombres_ingredientes(
                    perfil.ingredientes
                )
                for perfil in recetas_permitidas
            },
        )

    def _seleccionar_hemoglobina_general(
        self,
        texto: str,
    ) -> ResultadoBusqueda | None:
        normalizado = normalizar_texto(texto)
        tokens = set(normalizado.split())
        if not ({"hemoglobina", "hb"} & tokens):
            return None
        senales_interpretacion = {
            "alta",
            "alto",
            "analisis",
            "baja",
            "bajo",
            "diagnostico",
            "examen",
            "nivel",
            "resultado",
            "salio",
            "valor",
        }
        if tokens & senales_interpretacion or any(
            token.isdigit() for token in tokens
        ):
            return None
        regla = next(
            (
                candidata
                for candidata in self.reglas
                if normalizar_texto(candidata.subcategoria).startswith(
                    "definicion la hemoglobina"
                )
            ),
            None,
        )
        if regla is None:
            return None
        return _resultado_forzado(
            regla,
            "seleccion_forzada_hemoglobina_general",
            ("hemoglobina",),
        )

    def _respuestas_receta_personalizada(
        self,
        conversacion: ConversacionActiva,
        texto_alimentos: str,
        perfil_alimentario: PerfilAlimentario,
    ) -> tuple[RespuestaConversacion, ...]:
        recetas_edad = filtrar_recetas_por_edad(
            self.perfiles_recetas,
            meses_bebe=conversacion.meses_bebe,
            rango_edad_bebe=conversacion.rango_edad_bebe,
        )
        if not recetas_edad:
            return (RespuestaConversacion(MENSAJE_RECETA_SIN_COBERTURA),)

        excluidos = set(perfil_alimentario.excluidos)
        recetas_permitidas = tuple(
            perfil
            for perfil in recetas_edad
            if not (set(perfil.ingredientes) & excluidos)
        )
        if not recetas_permitidas:
            return (RespuestaConversacion(MENSAJE_RECETA_EXCLUIDA),)

        evaluacion_local = rankear_recetas(
            recetas_permitidas,
            perfil_alimentario,
        )
        if evaluacion_local is None:  # Protegido por recetas_permitidas.
            return (RespuestaConversacion(MENSAJE_RECETA_EXCLUIDA),)
        if not evaluacion_local.aceptados:
            return self._respuestas_recetas_compatibles(
                recetas_permitidas,
                perfil_alimentario,
            )

        recetas_con_coincidencia = tuple(
            perfil
            for perfil in recetas_permitidas
            if set(perfil.ingredientes) & set(perfil_alimentario.aceptados)
        )

        perfiles_por_id = {
            perfil.regla.id_regla: perfil for perfil in recetas_con_coincidencia
        }
        seleccion: SeleccionGoogle | None = None
        origen = "reglas"
        if self.clasificador is not None:
            contexto = self._crear_contexto_clasificacion(
                conversacion,
                self.repositorio.ultima_categoria(conversacion.id),
                perfil_alimentario=perfil_alimentario,
                recetas_permitidas=recetas_con_coincidencia,
            )
            try:
                candidata = self.clasificador.seleccionar(
                    texto_alimentos,
                    contexto,
                )
            except ErrorClasificacionGoogle as error:
                logging.warning(
                    "Gemini no pudo elegir receta; se usarán reglas: %s",
                    error,
                )
            else:
                if (
                    candidata is not None
                    and candidata.regla.id_regla in perfiles_por_id
                ):
                    seleccion = candidata
                    origen = "google"
                elif candidata is not None:
                    logging.warning(
                        "Gemini devolvió una receta fuera del conjunto permitido"
                    )

        if seleccion is not None:
            perfil_elegido = perfiles_por_id[seleccion.regla.id_regla]
            evaluacion = rankear_recetas(
                (perfil_elegido,),
                perfil_alimentario,
            )
            resultado: ResultadoSeleccion = seleccion
        else:
            evaluacion = seleccionar_receta_variada(
                recetas_con_coincidencia,
                perfil_alimentario,
            )
            if evaluacion is None:  # Protegido por recetas_permitidas.
                return (RespuestaConversacion(MENSAJE_RECETA_EXCLUIDA),)
            perfil_elegido = evaluacion.perfil_receta
            resultado = ResultadoBusqueda(
                regla=perfil_elegido.regla,
                puntaje=evaluacion.puntaje,
                margen=1.0,
                coincidencias_exactas=evaluacion.aceptados,
                coincidencias_aproximadas=(),
                motivo="seleccion_receta_variada_por_preferencias",
            )

        assert evaluacion is not None
        adicionales = {
            "origen_receta": origen,
            "id_receta": perfil_elegido.regla.id_regla,
            "alimentos_aceptados": list(
                nombres_ingredientes(perfil_alimentario.aceptados)
            ),
            "alimentos_rechazados": list(
                nombres_ingredientes(perfil_alimentario.rechazados)
            ),
            "alimentos_excluidos": list(
                nombres_ingredientes(perfil_alimentario.excluidos)
            ),
            "ingredientes_coincidentes": list(
                nombres_ingredientes(evaluacion.aceptados)
            ),
            "ingredientes_rechazados_receta": list(
                nombres_ingredientes(evaluacion.rechazados)
            ),
            "puntaje_compatibilidad": evaluacion.puntaje,
        }
        respuestas: list[RespuestaConversacion] = []
        if evaluacion.rechazados:
            rechazados = ", ".join(
                nombres_ingredientes(evaluacion.rechazados)
            )
            respuestas.append(
                RespuestaConversacion(
                    "La mejor coincidencia disponible incluye "
                    f"{rechazados}, que indicaste que no le gusta. Te "
                    "mostraré la receta revisada sin cambiar sus ingredientes; "
                    "no la uses si además existe alergia o intolerancia."
                )
            )
        respuestas.extend(
            self._respuestas_desde_regla(
                resultado,
                evidencia_adicional=adicionales,
            )
        )
        return tuple(respuestas)

    def _respuestas_recetas_compatibles(
        self,
        recetas_permitidas: tuple[PerfilReceta, ...],
        perfil_alimentario: PerfilAlimentario,
    ) -> tuple[RespuestaConversacion, ...]:
        """Muestra alternativas seguras, sin fingir afinidad alimentaria."""
        aceptados = ", ".join(nombres_ingredientes(perfil_alimentario.aceptados))
        opciones = tuple(
            OpcionRespuesta(
                id=f"{PREFIJO_RECETA_COMPATIBLE}{perfil.regla.id_regla}",
                titulo=self._acortar_titulo(titulo_visible(perfil.regla)),
                descripcion=", ".join(nombres_ingredientes(perfil.ingredientes))[:72],
            )
            for perfil in recetas_permitidas[:3]
        )
        return (
            RespuestaConversacion(
                "No encuentro una receta MINSA revisada para esta edad que "
                f"incluya {aceptados}. Estas opciones sí respetan los "
                "alimentos que indicaste que no puede consumir.",
                opciones=opciones,
                tipo_opciones="lista",
                etiqueta_lista="Ver opciones",
            ),
        )

    def _procesar_receta_compatible(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ResultadoConversacion:
        id_regla = texto.removeprefix(PREFIJO_RECETA_COMPATIBLE)
        perfil_alimentario = extraer_perfil_alimentario(
            conversacion.alimentos_contexto or ""
        )
        permitidas = {
            perfil.regla.id_regla: perfil
            for perfil in filtrar_recetas_por_edad(
                self.perfiles_recetas,
                meses_bebe=conversacion.meses_bebe,
                rango_edad_bebe=conversacion.rango_edad_bebe,
            )
            if not (set(perfil.ingredientes) & set(perfil_alimentario.excluidos))
        }
        perfil = permitidas.get(id_regla)
        if perfil is None:
            return self._respuesta_opcion_invalida(conversacion)
        evaluacion = rankear_recetas((perfil,), perfil_alimentario)
        assert evaluacion is not None
        resultado = ResultadoBusqueda(
            regla=perfil.regla,
            puntaje=evaluacion.puntaje,
            margen=1.0,
            coincidencias_exactas=evaluacion.aceptados,
            coincidencias_aproximadas=(),
            motivo="seleccion_receta_compatible_sin_coincidencia",
        )
        respuestas = self._respuestas_desde_regla(
            resultado,
            evidencia_adicional={
                "origen_receta": "seleccion_usuario",
                "id_receta": perfil.regla.id_regla,
                "ingredientes_receta": list(
                    nombres_ingredientes(perfil.ingredientes)
                ),
            },
        )
        return self._resultado_con_menu(conversacion, respuestas)

    @staticmethod
    def _es_solicitud_receta(texto: str) -> bool:
        tokens = set(normalizar_texto(texto).split())
        verbos_recomendacion = {
            "dame",
            "quiero",
            "recomienda",
            "recomendacion",
            "recomendar",
            "recomiendas",
            "recomiendame",
            "sugiere",
            "sugiereme",
            "sugerencia",
        }
        return bool(
            {"receta", "recetas"} & tokens
            and tokens & verbos_recomendacion
        )

    def _actualizar_edad_explicita(
        self,
        conversacion: ConversacionActiva,
        texto: str,
    ) -> ConversacionActiva:
        texto_normalizado = normalizar_texto(texto)
        senales_edad = {
            "bebe",
            "edad",
            "hija",
            "hijo",
            "nina",
            "nino",
            "tiene",
        }
        if not set(texto_normalizado.split()) & senales_edad:
            return conversacion
        meses = self._extraer_meses_consulta(texto_normalizado)
        if meses is None or not 6 <= meses <= 36:
            return conversacion
        if (
            conversacion.meses_bebe == meses
            and conversacion.rango_edad_bebe is None
        ):
            return conversacion
        return self.repositorio.actualizar_perfil(
            conversacion.id,
            meses_bebe=meses,
            rango_edad_bebe=None,
        )

    @staticmethod
    def _extraer_meses_consulta(texto_normalizado: str) -> int | None:
        coincidencia = re.search(
            r"\b(\d{1,2})\s*(?:mes|meses|m)\b",
            texto_normalizado,
        )
        if coincidencia:
            return int(coincidencia.group(1))
        coincidencia = re.search(
            r"\b(\d{1,2})\s*(?:ano|anos)\b",
            texto_normalizado,
        )
        return int(coincidencia.group(1)) * 12 if coincidencia else None

    def _respuestas_desde_regla(
        self,
        resultado: ResultadoSeleccion,
        *,
        evidencia_adicional: dict[str, Any] | None = None,
    ) -> tuple[RespuestaConversacion, ...]:
        regla = resultado.regla
        enlace = regla.enlace.strip() or "No disponible"
        puntaje = (
            resultado.puntaje
            if isinstance(resultado, ResultadoBusqueda)
            else None
        )
        evidencia = _serializar_resultado(resultado)
        if evidencia_adicional:
            evidencia.update(evidencia_adicional)
        frase = ""
        if isinstance(resultado, SeleccionGoogle):
            posible = resultado.evidencia.get("frase_inicial", "")
            frase = posible if isinstance(posible, str) else ""
        if not frase:
            frase = self._frase_local(regla)
        return (
            RespuestaConversacion(
                texto=f"{frase}\n\n{regla.respuesta.strip()}",
                categoria=regla.categoria,
                subcategoria=regla.subcategoria,
                puntaje=puntaje,
                evidencia=evidencia,
            ),
            RespuestaConversacion(
                texto=f"{regla.disclaimer.strip()}"
            ),
            RespuestaConversacion(texto=f"Fuente:\n {regla.documento.strip()} pag. {regla.paginas.strip()}\n {enlace}"),
            RespuestaConversacion(texto=MENSAJE_RECORDATORIO_FIN),
        )

    @staticmethod
    def _frase_local(regla: ReglaConocimiento) -> str:
        """Respaldo sin IA: cálido, breve y sin contenido clínico nuevo."""
        seccion = seccion_de_regla(regla)
        frases = {
            "Anemia y señales": "Claro, te lo explico de forma sencilla.",
            "Alimentación y hierro": "Veamos una opción práctica para ti.",
            "Suplementos": "Te comparto la información revisada.",
            "Prevención y controles": "Es una buena consulta para cuidar su salud.",
            "Tratamiento": "Te comparto la orientación revisada.",
            "Embarazo y lactancia": "Te acompaño con esta información revisada.",
            "Recetas por edad": "Revisemos esta alternativa según su etapa.",
            "Ayuda y seguridad": "Gracias por contarlo; revisa esta orientación.",
        }
        return frases[seccion]


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
