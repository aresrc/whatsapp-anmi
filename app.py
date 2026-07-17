"""Webhook de WhatsApp para el asistente nutricional ANMI."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, Response, current_app, jsonify, request

from clasificador_google import crear_clasificador_google
from conversaciones import (
    ErrorPersistenciaConversacion,
    RepositorioConversaciones,
)
from motor_conocimientos import (
    buscar_mejor_regla,
    cargar_motor_conocimientos,
    construir_respuesta,
)
from servicio_conversacion import (
    LimpiezaPeriodica,
    MENSAJE_NO_ENCONTRADO,
    RespuestaConversacion,
    ServicioConversacion,
)


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
RUTA_DB_PREDETERMINADA = BASE_DIR / "instance" / "anmi.sqlite3"

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "")

REGLAS_CONOCIMIENTO = cargar_motor_conocimientos()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def _ruta_db_desde_entorno() -> Path:
    valor = os.getenv("ANMI_DB_PATH", "").strip()
    return Path(valor) if valor else RUTA_DB_PREDETERMINADA


def obtener_puerto_produccion() -> int:
    """Lee y valida el puerto normal configurado en el entorno."""
    valor = os.getenv("PORT", "5000")
    try:
        puerto = int(valor)
    except ValueError as error:
        raise RuntimeError("PORT debe ser un número entero") from error
    if not 1 <= puerto <= 65_535:
        raise RuntimeError("PORT debe estar entre 1 y 65535")
    return puerto


def validar_configuracion() -> None:
    """Comprueba las credenciales obligatorias para hablar con Meta."""
    variables = {
        "VERIFY_TOKEN": VERIFY_TOKEN,
        "WHATSAPP_TOKEN": WHATSAPP_TOKEN,
        "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "GRAPH_API_VERSION": GRAPH_API_VERSION,
    }
    faltantes = [nombre for nombre, valor in variables.items() if not valor]
    if faltantes:
        raise RuntimeError(
            f"Faltan variables de entorno: {', '.join(faltantes)}"
        )


def obtener_respuesta(texto_usuario: str) -> str:
    """Atajo sin estado conservado por compatibilidad con usos existentes."""
    resultado = buscar_mejor_regla(texto_usuario, REGLAS_CONOCIMIENTO)
    if resultado is None:
        return MENSAJE_NO_ENCONTRADO
    return construir_respuesta(resultado)


def _ocultar_identificador(valor: str) -> str:
    if len(valor) <= 4:
        return "****"
    return f"***{valor[-4:]}"


def enviar_mensaje(
    numero_destino: str,
    payload: dict[str, Any],
) -> bool:
    """Envía un payload mediante WhatsApp Cloud API."""
    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
    )
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        respuesta = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=15,
        )
    except requests.RequestException:
        logging.exception(
            "Error de conexión al enviar mensaje a %s",
            _ocultar_identificador(numero_destino),
        )
        return False

    if respuesta.ok:
        logging.info(
            "Mensaje aceptado por Meta para %s",
            _ocultar_identificador(numero_destino),
        )
        return True

    logging.error(
        "Meta respondió con error %s para %s",
        respuesta.status_code,
        _ocultar_identificador(numero_destino),
    )
    return False


def enviar_mensaje_texto(numero_destino: str, mensaje: str) -> bool:
    """Envía un mensaje de texto mediante WhatsApp Cloud API."""
    return enviar_mensaje(
        numero_destino,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero_destino,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": mensaje,
            },
        },
    )


def enviar_respuesta(
    numero_destino: str,
    respuesta: RespuestaConversacion,
) -> bool:
    """Envía texto simple o botones de respuesta según la salida del servicio."""
    if not respuesta.opciones:
        return enviar_mensaje_texto(numero_destino, respuesta.texto)
    return enviar_mensaje(
        numero_destino,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero_destino,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": respuesta.texto},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": opcion.id,
                                "title": opcion.titulo,
                            },
                        }
                        for opcion in respuesta.opciones
                    ]
                },
            },
        },
    )


def extraer_mensajes_texto(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extrae texto o la selección de un botón desde un webhook de Meta."""
    mensajes_extraidos: list[dict[str, str]] = []
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return mensajes_extraidos

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cambios = entry.get("changes", [])
        if not isinstance(cambios, list):
            continue
        for cambio in cambios:
            if not isinstance(cambio, dict):
                continue
            value = cambio.get("value", {})
            if not isinstance(value, dict):
                continue
            mensajes = value.get("messages", [])
            if not isinstance(mensajes, list):
                continue
            for mensaje in mensajes:
                if not isinstance(mensaje, dict):
                    continue
                numero = mensaje.get("from")
                mensaje_id = mensaje.get("id")
                tipo = mensaje.get("type")
                texto = None
                if tipo == "text":
                    bloque_texto = mensaje.get("text", {})
                    texto = (
                        bloque_texto.get("body")
                        if isinstance(bloque_texto, dict)
                        else None
                    )
                elif tipo == "interactive":
                    bloque_interactivo = mensaje.get("interactive", {})
                    if (
                        isinstance(bloque_interactivo, dict)
                        and bloque_interactivo.get("type") == "button_reply"
                    ):
                        boton = bloque_interactivo.get("button_reply", {})
                        texto = (
                            boton.get("id")
                            if isinstance(boton, dict)
                            else None
                        )
                if all(
                    isinstance(valor, str) and valor.strip()
                    for valor in (numero, mensaje_id, texto)
                ):
                    mensajes_extraidos.append(
                        {
                            "id": mensaje_id,
                            "numero": numero,
                            "texto": texto,
                        }
                    )
    return mensajes_extraidos


def crear_app(
    ruta_db: str | Path | None = None,
    *,
    iniciar_limpieza: bool = False,
    usar_google: bool = True,
) -> Flask:
    """Construye la aplicación y permite aislar la base en las pruebas."""
    aplicacion = Flask(__name__)
    repositorio = RepositorioConversaciones(
        Path(ruta_db) if ruta_db is not None else _ruta_db_desde_entorno()
    )
    servicio = ServicioConversacion(
        repositorio,
        REGLAS_CONOCIMIENTO,
        clasificador=(
            crear_clasificador_google(REGLAS_CONOCIMIENTO)
            if usar_google
            else None
        ),
    )
    servicio.inicializar()
    aplicacion.extensions["anmi_servicio"] = servicio

    if iniciar_limpieza:
        limpieza = LimpiezaPeriodica(servicio)
        limpieza.iniciar()
        aplicacion.extensions["anmi_limpieza"] = limpieza

    @aplicacion.get("/")
    def inicio() -> tuple[dict[str, Any], int]:
        return {
            "estado": "activo",
            "servicio": "ANMI WhatsApp Bot",
            "reglas": len(REGLAS_CONOCIMIENTO),
            "clasificador": servicio.modo_clasificador,
        }, 200

    @aplicacion.get("/webhook")
    def verificar_webhook() -> Response:
        modo = request.args.get("hub.mode")
        token_recibido = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if (
            modo == "subscribe"
            and token_recibido == VERIFY_TOKEN
            and challenge
        ):
            logging.info("Webhook verificado correctamente")
            return Response(challenge, status=200, mimetype="text/plain")
        logging.warning("Intento de verificación de webhook rechazado")
        return Response(
            "Token de verificación incorrecto",
            status=403,
            mimetype="text/plain",
        )

    @aplicacion.post("/webhook")
    def recibir_webhook() -> tuple[Response, int]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"estado": "payload_invalido"}), 400

        mensajes = extraer_mensajes_texto(payload)
        servicio_actual: ServicioConversacion = current_app.extensions[
            "anmi_servicio"
        ]
        errores_envio = 0

        for mensaje in mensajes:
            numero = mensaje["numero"]
            logging.info(
                "Mensaje recibido de %s (id=%s)",
                _ocultar_identificador(numero),
                _ocultar_identificador(mensaje["id"]),
            )
            resultado = servicio_actual.procesar_mensaje(
                "whatsapp",
                numero,
                mensaje["texto"],
                mensaje_externo_id=mensaje["id"],
            )
            if resultado.duplicado:
                logging.info(
                    "Mensaje duplicado ignorado (id=%s)",
                    _ocultar_identificador(mensaje["id"]),
                )
                continue

            entregado = True
            for respuesta in resultado.respuestas:
                if not enviar_respuesta(numero, respuesta):
                    entregado = False
                    errores_envio += 1
                    break

            if entregado:
                try:
                    servicio_actual.confirmar_respuestas(resultado)
                    if resultado.finalizacion_pendiente:
                        servicio_actual.confirmar_finalizacion(
                            resultado.conversacion_id
                        )
                except (
                    ErrorPersistenciaConversacion,
                    LookupError,
                    ValueError,
                ):
                    logging.exception(
                        "No se pudo confirmar la conversación %s",
                        resultado.conversacion_id,
                    )

        return jsonify(
            {
                "estado": "recibido",
                "mensajes": len(mensajes),
                "errores_envio": errores_envio,
            }
        ), 200

    return aplicacion


app = crear_app()


if __name__ == "__main__":
    validar_configuracion()
    limpieza = LimpiezaPeriodica(app.extensions["anmi_servicio"])
    limpieza.iniciar()
    app.extensions["anmi_limpieza"] = limpieza
    app.run(
        host="0.0.0.0",
        port=obtener_puerto_produccion(),
        debug=False,
    )
