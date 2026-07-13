import logging
import os
import re
import unicodedata
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

# from respuestas import REGLAS, RESPUESTA_NO_ENCONTRADA
from motor_conocimientos import (
    ReglaConocimiento,
    buscar_mejor_regla,
    cargar_motor_conocimientos,
    construir_respuesta,
)

load_dotenv()

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

REGLAS_CONOCIMIENTO = cargar_motor_conocimientos()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")

# Usa la versión que aparezca como vigente en el panel/documentación de Meta.
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "")

PORT = int(os.getenv("PORT", "5000"))


def validar_configuracion() -> None:
    """Comprueba que las variables obligatorias estén configuradas."""
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


def normalizar_texto(texto: str) -> str:
    """
    Convierte el texto a minúsculas, elimina tildes y limpia símbolos.

    Ejemplo:
        '¡Háblame de la ANEMIA!' → 'hablame de la anemia'
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


def contiene_palabra_clave(texto: str, palabra_clave: str) -> bool:
    """
    Comprueba palabras o frases completas.

    Evita, por ejemplo, que 'hola' coincida dentro de una palabra más larga.
    """
    texto_normalizado = normalizar_texto(texto)
    clave_normalizada = normalizar_texto(palabra_clave)

    patron = rf"(?<!\w){re.escape(clave_normalizada)}(?!\w)"

    return re.search(patron, texto_normalizado) is not None


RESPUESTA_NO_ENCONTRADA = (
    "No logré identificar tu consulta. "
    "Puedes preguntarme sobre anemia, hierro, alimentación, "
    "suplementos o cuidados durante el embarazo."
)


def obtener_respuesta(texto_usuario: str) -> str:
    regla = buscar_mejor_regla(
        texto_usuario,
        REGLAS_CONOCIMIENTO,
    )

    if regla is None:
        return RESPUESTA_NO_ENCONTRADA

    logging.info(
        "Intención detectada | categoría=%s | subcategoría=%s",
        regla.categoria,
        regla.subcategoria,
    )

    return construir_respuesta(regla)


def es_saludo_bienvenida(regla: ReglaConocimiento | None) -> bool:
    """Indica si una regla corresponde al saludo de bienvenida."""
    if regla is None:
        return False

    return (
        normalizar_texto(regla.categoria) == "bienvenida"
        and normalizar_texto(regla.subcategoria) == "saludo"
    )


def enviar_mensaje(
    numero_destino: str,
    payload: dict[str, Any],
) -> bool:
    """Envía un payload mediante WhatsApp Cloud API."""
    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
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

        if respuesta.ok:
            logging.info(
                "Mensaje enviado correctamente a %s",
                numero_destino,
            )
            return True

        logging.error(
            "Meta respondió con error %s: %s",
            respuesta.status_code,
            respuesta.text,
        )
        return False

    except requests.RequestException:
        logging.exception(
            "Error de conexión al enviar mensaje a Meta"
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


def enviar_menu_bienvenida(numero_destino: str) -> bool:
    """Envía las tres opciones de la bienvenida como botones."""
    return enviar_mensaje(
        numero_destino,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero_destino,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "Elige una opción:"},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": "menu_alimentos",
                                "title": "Alimentos",
                            },
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": "menu_salud",
                                "title": "Salud",
                            },
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": "menu_bebidas",
                                "title": "Bebidas",
                            },
                        },
                    ],
                },
            },
        },
    )


def extraer_mensajes_texto(payload: dict[str, Any]) -> list[dict[str, str]]:
    """
    Extrae los mensajes de texto contenidos en un webhook de Meta.

    Devuelve elementos con:
    - id
    - numero
    - texto
    """
    mensajes_extraidos: list[dict[str, str]] = []

    entries = payload.get("entry", [])

    for entry in entries:
        cambios = entry.get("changes", [])

        for cambio in cambios:
            value = cambio.get("value", {})
            mensajes = value.get("messages", [])

            for mensaje in mensajes:
                if mensaje.get("type") != "text":
                    continue

                numero = mensaje.get("from")
                mensaje_id = mensaje.get("id")
                texto = mensaje.get("text", {}).get("body")

                if numero and mensaje_id and texto:
                    mensajes_extraidos.append(
                        {
                            "id": mensaje_id,
                            "numero": numero,
                            "texto": texto,
                        }
                    )

    return mensajes_extraidos


@app.get("/")
def inicio() -> tuple[dict[str, str], int]:
    return {
        "estado": "activo",
        "servicio": "WhatsApp Keyword Bot",
    }, 200


@app.get("/webhook")
def verificar_webhook() -> Response:
    """
    Meta ejecuta este GET cuando configuras el webhook.

    Debemos:
    1. Verificar hub.mode.
    2. Comparar hub.verify_token.
    3. Retornar hub.challenge.
    """
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
    return Response("Token de verificación incorrecto", status=403)


@app.post("/webhook")
def recibir_webhook() -> tuple[Response, int]:
    """
    Recibe los eventos enviados por Meta.

    Es importante devolver 200 rápidamente para confirmar la recepción.
    """
    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({"estado": "payload_invalido"}), 400

    # Los eventos de estado de entrega no contienen necesariamente mensajes.
    mensajes = extraer_mensajes_texto(payload)

    for mensaje in mensajes:
        numero = mensaje["numero"]
        texto = mensaje["texto"]

        logging.info(
            "Mensaje recibido de %s: %s",
            numero,
            texto,
        )

        regla = buscar_mejor_regla(texto, REGLAS_CONOCIMIENTO)
        respuesta_bot = (
            construir_respuesta(regla)
            if regla is not None
            else RESPUESTA_NO_ENCONTRADA
        )
        enviar_mensaje_texto(numero, respuesta_bot)

        if es_saludo_bienvenida(regla):
            enviar_menu_bienvenida(numero)

    return jsonify({"estado": "recibido"}), 200


if __name__ == "__main__":
    validar_configuracion()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
