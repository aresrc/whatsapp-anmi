from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import app as webhook
import simulador
from servicio_conversacion import (
    MENSAJE_AGRADECIMIENTO,
    MENSAJE_BIENVENIDA,
    MENSAJE_MESES_INVALIDOS,
    OPCIONES_EDAD,
    OpcionRespuesta,
    RespuestaConversacion,
    ResultadoConversacion,
    ServicioConversacion,
)


def crear_payload(
    texto: str = "hola",
    *,
    numero: str = "51987654321",
    mensaje_id: str = "wamid.1",
) -> dict[str, object]:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": numero,
                                    "id": mensaje_id,
                                    "type": "text",
                                    "text": {"body": texto},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def crear_payload_boton(
    opcion_id: str = "edad_6_8",
    *,
    numero: str = "51987654321",
    mensaje_id: str = "wamid.boton",
) -> dict[str, object]:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": numero,
                                    "id": mensaje_id,
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": opcion_id,
                                            "title": "6 a 8 meses",
                                        },
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


class WebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directorio_temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio_temporal.cleanup)
        self.ruta_db = Path(self.directorio_temporal.name) / "webhook.sqlite3"
        self.aplicacion = webhook.crear_app(
            self.ruta_db,
            usar_google=False,
        )
        self.aplicacion.config.update(TESTING=True)
        self.cliente = self.aplicacion.test_client()

    def _instalar_servicio_mock(self, resultado: ResultadoConversacion):
        servicio = MagicMock(spec=ServicioConversacion)
        servicio.procesar_mensaje.return_value = resultado
        self.aplicacion.extensions["anmi_servicio"] = servicio
        return servicio

    def test_extraer_mensajes_texto_tolera_payloads_malformados(self) -> None:
        self.assertEqual(webhook.extraer_mensajes_texto({}), [])
        self.assertEqual(webhook.extraer_mensajes_texto({"entry": {}}), [])

        payload = {
            "entry": [
                None,
                {"changes": "incorrecto"},
                {
                    "changes": [
                        None,
                        {"value": None},
                        {"value": {"messages": "incorrecto"}},
                        {
                            "value": {
                                "messages": [
                                    None,
                                    {"type": "image", "from": "1", "id": "x"},
                                    {
                                        "type": "text",
                                        "from": "1",
                                        "id": "sin-texto",
                                        "text": {},
                                    },
                                    {
                                        "type": "text",
                                        "from": " ",
                                        "id": "vacio",
                                        "text": {"body": "hola"},
                                    },
                                    {
                                        "type": "text",
                                        "from": "51911111111",
                                        "id": "wamid.valido-1",
                                        "text": {"body": "primer mensaje"},
                                    },
                                ]
                            }
                        },
                    ]
                },
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "type": "text",
                                        "from": "51922222222",
                                        "id": "wamid.valido-2",
                                        "text": {"body": "segundo mensaje"},
                                    }
                                ]
                            }
                        }
                    ]
                },
            ]
        }

        self.assertEqual(
            webhook.extraer_mensajes_texto(payload),
            [
                {
                    "id": "wamid.valido-1",
                    "numero": "51911111111",
                    "texto": "primer mensaje",
                },
                {
                    "id": "wamid.valido-2",
                    "numero": "51922222222",
                    "texto": "segundo mensaje",
                },
            ],
        )

        boton = webhook.extraer_mensajes_texto(crear_payload_boton())
        self.assertEqual(
            boton,
            [
                {
                    "id": "wamid.boton",
                    "numero": "51987654321",
                    "texto": "edad_6_8",
                }
            ],
        )

        payload_lista = crear_payload_boton()
        mensaje_lista = payload_lista["entry"][0]["changes"][0]["value"][
            "messages"
        ][0]
        mensaje_lista["interactive"] = {
            "type": "list_reply",
            "list_reply": {
                "id": "menu_cat:3",
                "title": "Alimentación",
            },
        }
        lista = webhook.extraer_mensajes_texto(payload_lista)
        self.assertEqual(lista[0]["texto"], "menu_cat:3")

    def test_enviar_respuesta_construye_tres_botones_de_edad(self) -> None:
        respuesta = RespuestaConversacion(
            MENSAJE_BIENVENIDA,
            opciones=OPCIONES_EDAD,
        )
        with patch.object(
            webhook,
            "enviar_mensaje",
            return_value=True,
        ) as enviar:
            enviado = webhook.enviar_respuesta("51987654321", respuesta)

        self.assertTrue(enviado)
        payload = enviar.call_args.args[1]
        self.assertEqual(payload["type"], "interactive")
        self.assertEqual(payload["interactive"]["type"], "button")
        self.assertEqual(
            payload["interactive"]["action"]["buttons"],
            [
                {
                    "type": "reply",
                    "reply": {"id": opcion.id, "title": opcion.titulo},
                }
                for opcion in OPCIONES_EDAD
            ],
        )

    def test_enviar_respuesta_construye_lista_interactiva(self) -> None:
        respuesta = RespuestaConversacion(
            "Elige una categoría",
            opciones=(
                OpcionRespuesta(
                    "menu_cat:0",
                    "Definición",
                    "Definición y Síntomas",
                ),
                OpcionRespuesta("menu_general:1", "Siguiente"),
            ),
            tipo_opciones="lista",
            etiqueta_lista="Ver categorías",
        )
        with patch.object(webhook, "enviar_mensaje", return_value=True) as enviar:
            self.assertTrue(webhook.enviar_respuesta("51987654321", respuesta))

        payload = enviar.call_args.args[1]
        self.assertEqual(payload["interactive"]["type"], "list")
        action = payload["interactive"]["action"]
        self.assertEqual(action["button"], "Ver categorías")
        self.assertEqual(action["sections"][0]["rows"][0]["id"], "menu_cat:0")
        self.assertEqual(
            action["sections"][0]["rows"][0]["description"],
            "Definición y Síntomas",
        )

    def test_verificacion_webhook_y_payload_invalido(self) -> None:
        with patch.object(webhook, "VERIFY_TOKEN", "token-prueba"):
            correcta = self.cliente.get(
                "/webhook",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "token-prueba",
                    "hub.challenge": "reto-123",
                },
            )
            incorrecta = self.cliente.get(
                "/webhook",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "otro-token",
                    "hub.challenge": "reto-123",
                },
            )

        self.assertEqual(correcta.status_code, 200)
        self.assertEqual(correcta.get_data(as_text=True), "reto-123")
        self.assertEqual(correcta.mimetype, "text/plain")
        self.assertEqual(incorrecta.status_code, 403)

        for cuerpo, content_type in (
            ("no es json", "text/plain"),
            ("[]", "application/json"),
            ("null", "application/json"),
        ):
            with self.subTest(cuerpo=cuerpo):
                respuesta = self.cliente.post(
                    "/webhook", data=cuerpo, content_type=content_type
                )
                self.assertEqual(respuesta.status_code, 400)
                self.assertEqual(
                    respuesta.get_json(), {"estado": "payload_invalido"}
                )

    def test_webhook_confirma_respuestas_y_finaliza_solo_si_entrega_todo(self) -> None:
        resultado = ResultadoConversacion(
            conversacion_id=77,
            estado="esperando_calificacion",
            respuestas=(
                RespuestaConversacion("respuesta uno"),
                RespuestaConversacion(MENSAJE_AGRADECIMIENTO),
            ),
            finalizacion_pendiente=True,
        )
        servicio = self._instalar_servicio_mock(resultado)

        with patch.object(
            webhook, "enviar_mensaje_texto", return_value=True
        ) as enviar:
            respuesta = self.cliente.post(
                "/webhook", json=crear_payload("5")
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            respuesta.get_json(),
            {"estado": "recibido", "mensajes": 1, "errores_envio": 0},
        )
        servicio.procesar_mensaje.assert_called_once_with(
            "whatsapp",
            "51987654321",
            "5",
            mensaje_externo_id="wamid.1",
        )
        self.assertEqual(
            enviar.call_args_list,
            [
                call("51987654321", "respuesta uno"),
                call("51987654321", MENSAJE_AGRADECIMIENTO),
            ],
        )
        servicio.confirmar_respuestas.assert_called_once_with(resultado)
        servicio.confirmar_finalizacion.assert_called_once_with(77)

        servicio.reset_mock()
        servicio.procesar_mensaje.return_value = resultado
        with patch.object(
            webhook,
            "enviar_mensaje_texto",
            side_effect=(True, False),
        ) as enviar_fallido:
            fallida = self.cliente.post(
                "/webhook",
                json=crear_payload("5", mensaje_id="wamid.2"),
            )

        self.assertEqual(fallida.status_code, 200)
        self.assertEqual(fallida.get_json()["errores_envio"], 1)
        self.assertEqual(enviar_fallido.call_count, 2)
        servicio.confirmar_respuestas.assert_not_called()
        servicio.confirmar_finalizacion.assert_not_called()

    def test_webhook_ignora_resultado_duplicado_y_payload_sin_mensajes(self) -> None:
        duplicado = ResultadoConversacion(
            conversacion_id=12,
            estado="esperando_meses",
            respuestas=(),
            duplicado=True,
        )
        servicio = self._instalar_servicio_mock(duplicado)

        with patch.object(webhook, "enviar_mensaje_texto") as enviar:
            respuesta = self.cliente.post("/webhook", json=crear_payload())
            vacio = self.cliente.post("/webhook", json={"entry": []})

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(vacio.status_code, 200)
        self.assertEqual(vacio.get_json()["mensajes"], 0)
        enviar.assert_not_called()
        servicio.confirmar_respuestas.assert_not_called()
        servicio.confirmar_finalizacion.assert_not_called()

    def test_webhook_procesa_boton_y_guarda_rango(self) -> None:
        with patch.object(webhook, "enviar_mensaje", return_value=True) as enviar:
            bienvenida = self.cliente.post(
                "/webhook",
                json=crear_payload("hola", mensaje_id="wamid.inicio"),
            )
            edad = self.cliente.post(
                "/webhook",
                json=crear_payload_boton(
                    "edad_12_23",
                    mensaje_id="wamid.edad",
                ),
            )

        self.assertEqual(bienvenida.status_code, 200)
        self.assertEqual(edad.status_code, 200)
        self.assertEqual(enviar.call_args_list[0].args[1]["type"], "interactive")
        servicio = self.aplicacion.extensions["anmi_servicio"]
        conversacion = servicio.repositorio.obtener_conversacion(
            "whatsapp",
            "51987654321",
        )
        self.assertIsNone(conversacion.meses_bebe)
        self.assertEqual(conversacion.rango_edad_bebe, "12-23")

    def test_webhook_rechaza_cinco_meses_sin_error_interno(self) -> None:
        with patch.object(webhook, "enviar_mensaje", return_value=True) as enviar:
            self.cliente.post(
                "/webhook",
                json=crear_payload("hola", mensaje_id="wamid.inicio-menor"),
            )
            respuesta = self.cliente.post(
                "/webhook",
                json=crear_payload("5", mensaje_id="wamid.edad-menor"),
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            enviar.call_args_list[-1].args[1]["text"]["body"],
            MENSAJE_MESES_INVALIDOS,
        )
        servicio = self.aplicacion.extensions["anmi_servicio"]
        conversacion = servicio.repositorio.obtener_conversacion(
            "whatsapp",
            "51987654321",
        )
        self.assertEqual(conversacion.estado, "esperando_meses")
        self.assertIsNone(conversacion.meses_bebe)


class SimuladorWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directorio_temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio_temporal.cleanup)
        self.ruta_db = Path(self.directorio_temporal.name) / "simulador.sqlite3"
        self.aplicacion = simulador.crear_app_simulador(
            self.ruta_db,
            usar_google=False,
        )
        self.aplicacion.config.update(TESTING=True)
        self.cliente = self.aplicacion.test_client()
        self.servicio = self.aplicacion.extensions["anmi_servicio"]

    def test_get_post_y_reinicio_no_invocan_meta(self) -> None:
        usuario = "navegador-prueba-estable"
        with (
            patch.object(
                simulador.secrets, "token_urlsafe", return_value=usuario
            ),
            patch.object(webhook, "enviar_mensaje_texto") as enviar_meta,
            patch("requests.post") as solicitud_http,
        ):
            inicio = self.cliente.get("/")
            mensaje = self.cliente.post(
                "/mensaje", data={"mensaje": "hola"}
            )
            edad = self.cliente.post(
                "/mensaje", data={"mensaje": "edad_12_23"}
            )
            activa_antes_reinicio = (
                self.servicio.repositorio.obtener_conversacion(
                    "simulador", usuario
                )
            )
            reinicio = self.cliente.post("/reiniciar")

        self.assertEqual(inicio.status_code, 200)
        self.assertIn("Simulador ANMI", inicio.get_data(as_text=True))
        self.assertIn(simulador.COOKIE_USUARIO, inicio.headers["Set-Cookie"])
        self.assertEqual(mensaje.status_code, 200)
        self.assertIn("hola", mensaje.get_data(as_text=True))
        self.assertIn("ANMI", mensaje.get_data(as_text=True))
        self.assertIn("edad_6_8", mensaje.get_data(as_text=True))
        self.assertIn("12 a 23 meses", mensaje.get_data(as_text=True))
        self.assertEqual(edad.status_code, 200)
        self.assertIn("Qué alimentos", edad.get_data(as_text=True))
        self.assertIsNotNone(activa_antes_reinicio)
        self.assertEqual(activa_antes_reinicio.rango_edad_bebe, "12-23")
        self.assertEqual(reinicio.status_code, 302)
        self.assertEqual(reinicio.headers["Location"], "/")
        self.assertIn("Max-Age=0", reinicio.headers["Set-Cookie"])
        self.assertIsNone(
            self.servicio.repositorio.obtener_conversacion(
                "simulador", usuario
            )
        )
        enviar_meta.assert_not_called()
        solicitud_http.assert_not_called()

    def test_post_vacio_o_demasiado_largo_no_inicia_sesion(self) -> None:
        usuario = "navegador-validaciones"
        with patch.object(
            simulador.secrets, "token_urlsafe", return_value=usuario
        ):
            vacio = self.cliente.post("/mensaje", data={"mensaje": "   "})
            largo = self.cliente.post(
                "/mensaje", data={"mensaje": "x" * 4097}
            )

        self.assertEqual(vacio.status_code, 200)
        self.assertIn("Escribe un mensaje", vacio.get_data(as_text=True))
        self.assertEqual(largo.status_code, 200)
        self.assertIn("4096", largo.get_data(as_text=True))
        self.assertIsNone(
            self.servicio.repositorio.obtener_conversacion(
                "simulador", usuario
            )
        )

    def test_cinco_meses_muestra_validacion_y_conserva_el_estado(self) -> None:
        usuario = "navegador-edad-menor"
        with patch.object(
            simulador.secrets,
            "token_urlsafe",
            return_value=usuario,
        ):
            self.cliente.post("/mensaje", data={"mensaje": "hola"})
            respuesta = self.cliente.post(
                "/mensaje",
                data={"mensaje": "5"},
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(MENSAJE_MESES_INVALIDOS, respuesta.get_data(as_text=True))
        conversacion = self.servicio.repositorio.obtener_conversacion(
            "simulador",
            usuario,
        )
        self.assertEqual(conversacion.estado, "esperando_meses")
        self.assertIsNone(conversacion.meses_bebe)

    def test_salud_usa_instancia_aislada(self) -> None:
        respuesta = self.cliente.get("/salud")

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.get_json()["modo"], "simulador")
        self.assertGreater(respuesta.get_json()["reglas"], 0)
        self.assertEqual(self.servicio.repositorio.ruta_bd, self.ruta_db)


class PuertoSimuladorTest(unittest.TestCase):
    def test_bandera_true_usa_5002_y_false_usa_port(self) -> None:
        self.assertEqual(simulador.resolver_puerto(True, "invalido"), 5002)
        self.assertEqual(simulador.resolver_puerto(False, "5123"), 5123)
        with patch.dict(os.environ, {"PORT": "6123"}):
            self.assertEqual(simulador.resolver_puerto(False), 6123)

    def test_conversion_y_validacion_de_bandera(self) -> None:
        for valor in (True, "true", " TRUE "):
            with self.subTest(valor=valor):
                self.assertTrue(simulador.convertir_bandera(valor))
        for valor in (False, "false", " False "):
            with self.subTest(valor=valor):
                self.assertFalse(simulador.convertir_bandera(valor))
        for valor in ("1", "sí", "", "verdadero"):
            with self.subTest(valor=valor):
                with self.assertRaises(argparse.ArgumentTypeError):
                    simulador.convertir_bandera(valor)

    def test_port_invalido_es_rechazado(self) -> None:
        for valor in ("abc", "0", "65536", "-1"):
            with self.subTest(valor=valor):
                with self.assertRaises(ValueError):
                    simulador.resolver_puerto(False, valor)
        with patch.dict(os.environ, {"PORT": "abc"}):
            with self.assertRaises(ValueError):
                simulador.resolver_puerto(False)


if __name__ == "__main__":
    unittest.main()
