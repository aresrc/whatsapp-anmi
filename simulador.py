"""Interfaz web local para probar ANMI sin llamar al webhook ni a Meta."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, make_response, redirect, render_template, request, url_for

from clasificador_google import crear_clasificador_google
from conversaciones import RepositorioConversaciones
from motor_conocimientos import cargar_motor_conocimientos
from servicio_conversacion import (
    ESTADO_ESPERANDO_MESES,
    OPCIONES_EDAD,
    LimpiezaPeriodica,
    ServicioConversacion,
)


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
PUERTO_PRUEBA = 5002
RUTA_DB_SIMULADOR = BASE_DIR / "instance" / "anmi_simulador.sqlite3"
COOKIE_USUARIO = "anmi_simulador_usuario"


def convertir_bandera(valor: str | bool) -> bool:
    """Convierte únicamente los literales true/false aceptados por la CLI."""
    if isinstance(valor, bool):
        return valor
    normalizado = valor.strip().lower()
    if normalizado == "true":
        return True
    if normalizado == "false":
        return False
    raise argparse.ArgumentTypeError("la bandera debe ser true o false")


def resolver_puerto(
    usar_puerto_prueba: bool,
    puerto_entorno: str | None = None,
) -> int:
    """Selecciona 5002 o el PORT normal, según la bandera explícita."""
    if usar_puerto_prueba:
        return PUERTO_PRUEBA
    valor = puerto_entorno if puerto_entorno is not None else os.getenv("PORT", "5000")
    try:
        puerto = int(valor)
    except (TypeError, ValueError) as error:
        raise ValueError("PORT debe ser un número entero") from error
    if not 1 <= puerto <= 65_535:
        raise ValueError("PORT debe estar entre 1 y 65535")
    return puerto


def _usuario_desde_request() -> tuple[str, bool]:
    usuario = request.cookies.get(COOKIE_USUARIO, "").strip()
    if usuario:
        return usuario, False
    return secrets.token_urlsafe(18), True


def _preparar_depuracion(depuracion: dict[str, Any] | None) -> dict[str, Any] | None:
    if not depuracion:
        return None
    datos = dict(depuracion)
    exactas = [
        {"tipo": "exacta", "palabra": palabra}
        for palabra in datos.get("coincidencias_exactas", [])
    ]
    aproximadas = []
    for coincidencia in datos.get("coincidencias_aproximadas", []):
        if isinstance(coincidencia, dict):
            aproximadas.append({"tipo": "aproximada", **coincidencia})
        else:
            aproximadas.append(coincidencia)
    datos["coincidencias"] = exactas + aproximadas
    return datos


def crear_app_simulador(
    ruta_db: str | Path | None = None,
    *,
    iniciar_limpieza: bool = False,
    usar_google: bool = True,
) -> Flask:
    """Crea una instancia aislada y configurable para pruebas."""
    aplicacion = Flask(__name__)
    repositorio = RepositorioConversaciones(
        Path(ruta_db) if ruta_db is not None else RUTA_DB_SIMULADOR
    )
    reglas = cargar_motor_conocimientos()
    servicio = ServicioConversacion(
        repositorio,
        reglas,
        clasificador=(
            crear_clasificador_google(reglas) if usar_google else None
        ),
    )
    servicio.inicializar()
    aplicacion.extensions["anmi_servicio"] = servicio

    if iniciar_limpieza:
        limpieza = LimpiezaPeriodica(servicio)
        limpieza.iniciar()
        aplicacion.extensions["anmi_limpieza"] = limpieza

    def renderizar(
        usuario: str,
        *,
        mensajes: list[Any] | None = None,
        estado: str | None = None,
        depuracion: dict[str, Any] | None = None,
        error: str | None = None,
        nueva_cookie: bool = False,
    ) -> Response:
        if mensajes is None:
            mensajes = servicio.obtener_mensajes("simulador", usuario)
        if estado is None:
            estado = servicio.obtener_estado("simulador", usuario)
        respuesta = make_response(
            render_template(
                "simulador.html",
                mensajes=mensajes,
                estado=estado or "sin iniciar",
                depuracion=_preparar_depuracion(depuracion),
                error=error,
                usuario_id=usuario[-8:],
                estado_esperando_meses=ESTADO_ESPERANDO_MESES,
                opciones_edad=OPCIONES_EDAD,
            )
        )
        if nueva_cookie:
            respuesta.set_cookie(
                COOKIE_USUARIO,
                usuario,
                max_age=24 * 60 * 60,
                httponly=True,
                samesite="Lax",
            )
        return respuesta

    @aplicacion.get("/")
    def pagina_simulador() -> Response:
        usuario, nueva_cookie = _usuario_desde_request()
        return renderizar(usuario, nueva_cookie=nueva_cookie)

    @aplicacion.post("/mensaje")
    def enviar_mensaje_simulador() -> Response:
        usuario, nueva_cookie = _usuario_desde_request()
        mensaje = request.form.get("mensaje", "").strip()
        if not mensaje:
            return renderizar(
                usuario,
                error="Escribe un mensaje antes de enviarlo.",
                nueva_cookie=nueva_cookie,
            )
        if len(mensaje) > 4096:
            return renderizar(
                usuario,
                error="El mensaje no puede superar 4096 caracteres.",
                nueva_cookie=nueva_cookie,
            )

        try:
            resultado = servicio.procesar_mensaje(
                "simulador",
                usuario,
                mensaje,
            )
            servicio.confirmar_respuestas(resultado)
            mensajes = servicio.obtener_mensajes("simulador", usuario)
            if resultado.finalizacion_pendiente:
                servicio.confirmar_finalizacion(resultado.conversacion_id)
            return renderizar(
                usuario,
                mensajes=mensajes,
                estado=resultado.estado,
                depuracion=resultado.depuracion,
                nueva_cookie=nueva_cookie,
            )
        except Exception:
            logging.exception("Error al procesar el mensaje del simulador")
            return renderizar(
                usuario,
                error="Ocurrió un error interno; la sesión se conservó.",
                nueva_cookie=nueva_cookie,
            )

    @aplicacion.post("/reiniciar")
    def reiniciar_simulador() -> Response:
        usuario, _ = _usuario_desde_request()
        servicio.reiniciar("simulador", usuario)
        respuesta = make_response(redirect(url_for("pagina_simulador")))
        respuesta.delete_cookie(COOKIE_USUARIO)
        return respuesta

    @aplicacion.get("/salud")
    def salud_simulador() -> tuple[dict[str, Any], int]:
        return {
            "estado": "activo",
            "modo": "simulador",
            "reglas": len(servicio.reglas),
            "clasificador": servicio.modo_clasificador,
        }, 200

    return aplicacion


def crear_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simulador web local de ANMI")
    parser.add_argument(
        "--usar-puerto-prueba",
        type=convertir_bandera,
        default=True,
        metavar="true|false",
        help="true usa 5002; false usa PORT del .env (predeterminado: true)",
    )
    return parser


app = crear_app_simulador()


if __name__ == "__main__":
    argumentos = crear_parser().parse_args()
    limpieza = LimpiezaPeriodica(app.extensions["anmi_servicio"])
    limpieza.iniciar()
    app.extensions["anmi_limpieza"] = limpieza
    app.run(
        host="127.0.0.1",
        port=resolver_puerto(argumentos.usar_puerto_prueba),
        debug=False,
    )
