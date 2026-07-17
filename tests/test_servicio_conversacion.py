from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from clasificador_google import ErrorClasificacionGoogle, SeleccionGoogle
from conversaciones import RepositorioConversaciones
from motor_conocimientos import ReglaConocimiento, ResultadoBusqueda
from servicio_conversacion import (
    ESTADO_ESPERANDO_ALIMENTOS,
    ESTADO_ESPERANDO_CALIFICACION,
    ESTADO_ESPERANDO_MESES,
    ESTADO_LISTA,
    MENSAJE_ALIMENTOS,
    MENSAJE_ALIMENTOS_INVALIDOS,
    MENSAJE_AGRADECIMIENTO,
    MENSAJE_BIENVENIDA,
    MENSAJE_CALIFICACION,
    MENSAJE_CALIFICACION_INVALIDA,
    MENSAJE_LISTA,
    MENSAJE_LISTA_SIN_BEBE,
    MENSAJE_MESES_INVALIDOS,
    MENSAJE_RECORDATORIO_FIN,
    ServicioConversacion,
)


def crear_resultado(
    categoria: str = "Alimentación",
    subcategoria: str = "Hierro",
    respuesta: str = "Respuesta revisada",
) -> ResultadoBusqueda:
    regla = ReglaConocimiento(
        categoria=categoria,
        subcategoria=subcategoria,
        palabras_clave=("hierro", "alimentos"),
        respuesta=respuesta,
        disclaimer="Consulta con un profesional de salud.",
        paginas="10",
        documento="Guía de prueba",
        enlace="https://example.test/guia",
    )
    return ResultadoBusqueda(
        regla=regla,
        puntaje=18.5,
        margen=0.42,
        coincidencias_exactas=("hierro", "alimentos"),
        coincidencias_aproximadas=(),
        motivo="evidencia suficiente",
    )


class ServicioConversacionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directorio_temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio_temporal.cleanup)
        self.ruta_db = Path(self.directorio_temporal.name) / "servicio.sqlite3"
        self.repositorio = RepositorioConversaciones(self.ruta_db)
        self.reglas = (crear_resultado().regla,)
        self.servicio = ServicioConversacion(self.repositorio, self.reglas)
        self.servicio.inicializar()

    def _confirmar(self, resultado: object) -> int:
        return self.servicio.confirmar_respuestas(resultado)  # type: ignore[arg-type]

    def _iniciar(
        self,
        usuario: str = "usuario-1",
        texto: str = "¿Qué alimentos tienen hierro?",
        *,
        mensaje_externo_id: str | None = None,
    ):
        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            return_value=None,
        ):
            resultado = self.servicio.procesar_mensaje(
                "simulador",
                usuario,
                texto,
                mensaje_externo_id=mensaje_externo_id,
            )
        self._confirmar(resultado)
        return resultado

    def _completar_onboarding(
        self,
        usuario: str = "usuario-1",
        *,
        meses: str = "8",
        alimentos: str = "lentejas y pollo",
    ) -> int:
        inicio = self._iniciar(usuario)
        meses_resultado = self.servicio.procesar_mensaje(
            "simulador", usuario, meses
        )
        self._confirmar(meses_resultado)
        alimentos_resultado = self.servicio.procesar_mensaje(
            "simulador", usuario, alimentos
        )
        self._confirmar(alimentos_resultado)
        return inicio.conversacion_id

    def _contar(self, tabla: str) -> int:
        with closing(sqlite3.connect(self.ruta_db)) as conexion:
            return int(
                conexion.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
            )

    def test_onboarding_pide_perfil_y_no_responde_consulta_inicial(self) -> None:
        consulta_inicial = "¿Qué alimentos tienen hierro?"
        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            return_value=crear_resultado(),
        ) as buscar:
            inicio = self.servicio.procesar_mensaje(
                "simulador", "adaptativo", consulta_inicial
            )

        self.assertEqual(inicio.estado, ESTADO_ESPERANDO_MESES)
        self.assertEqual([r.texto for r in inicio.respuestas], [MENSAJE_BIENVENIDA])
        buscar.assert_called_once_with(consulta_inicial, self.reglas)
        self.assertEqual(self._confirmar(inicio), 1)

        meses = self.servicio.procesar_mensaje("simulador", "adaptativo", "7")
        self.assertEqual(meses.estado, ESTADO_ESPERANDO_ALIMENTOS)
        self.assertEqual(meses.respuestas[0].texto, MENSAJE_ALIMENTOS)
        self._confirmar(meses)

        alimentos = self.servicio.procesar_mensaje(
            "simulador", "adaptativo", "lentejas"
        )
        self.assertEqual(alimentos.estado, ESTADO_LISTA)
        self.assertEqual(alimentos.respuestas[0].texto, MENSAJE_LISTA)
        self.assertIn("vuelve a escribir", alimentos.respuestas[0].texto.lower())
        self._confirmar(alimentos)

        mensajes = self.servicio.obtener_mensajes("simulador", "adaptativo")
        self.assertEqual(mensajes[0].contenido, consulta_inicial)
        self.assertFalse(any(m.categoria for m in mensajes if m.rol == "bot"))

    def test_saludo_nuevo_y_repetido_muestra_la_bienvenida(self) -> None:
        with patch(
            "servicio_conversacion.buscar_mejor_regla"
        ) as buscar_regla:
            inicio = self.servicio.procesar_mensaje(
                "whatsapp",
                "usuario-nuevo",
                "Hola",
                mensaje_externo_id="wamid.saludo-1",
            )

        self.assertEqual(inicio.estado, ESTADO_ESPERANDO_MESES)
        self.assertEqual(
            [respuesta.texto for respuesta in inicio.respuestas],
            [MENSAJE_BIENVENIDA],
        )
        buscar_regla.assert_not_called()
        self._confirmar(inicio)

        saludo_repetido = self.servicio.procesar_mensaje(
            "whatsapp",
            "usuario-nuevo",
            "hola",
            mensaje_externo_id="wamid.saludo-2",
        )

        self.assertEqual(saludo_repetido.estado, ESTADO_ESPERANDO_MESES)
        self.assertEqual(
            [respuesta.texto for respuesta in saludo_repetido.respuestas],
            [MENSAJE_BIENVENIDA],
        )

    def test_no_aplica_omite_alimentos_y_deja_perfil_sin_meses(self) -> None:
        self._iniciar("sin-bebe", "hola")

        resultado = self.servicio.procesar_mensaje(
            "simulador", "sin-bebe", "No aplica"
        )

        self.assertEqual(resultado.estado, ESTADO_LISTA)
        self.assertEqual(resultado.respuestas[0].texto, MENSAJE_LISTA_SIN_BEBE)
        self._confirmar(resultado)
        conversacion = self.repositorio.obtener_conversacion(
            "simulador", "sin-bebe"
        )
        self.assertIsNotNone(conversacion)
        self.assertIsNone(conversacion.meses_bebe)
        self.assertIsNone(conversacion.alimentos_contexto)

    def test_meses_y_alimentos_invalidos_no_avanzan_el_estado(self) -> None:
        for indice, texto in enumerate(("", "-1", "60", "6 y medio", "dos")):
            with self.subTest(meses=texto):
                usuario = f"meses-invalidos-{indice}"
                self._iniciar(usuario, "hola")
                resultado = self.servicio.procesar_mensaje(
                    "simulador", usuario, texto
                )
                self.assertEqual(resultado.estado, ESTADO_ESPERANDO_MESES)
                self.assertEqual(
                    resultado.respuestas[0].texto, MENSAJE_MESES_INVALIDOS
                )
                self._confirmar(resultado)

        usuario = "alimentos-invalidos"
        self._iniciar(usuario, "hola")
        meses_validos = self.servicio.procesar_mensaje(
            "simulador", usuario, "tiene 6 meses"
        )
        self.assertEqual(meses_validos.estado, ESTADO_ESPERANDO_ALIMENTOS)
        self._confirmar(meses_validos)

        for texto in ("", "x" * 501):
            with self.subTest(alimentos=texto[:20]):
                resultado = self.servicio.procesar_mensaje(
                    "simulador", usuario, texto
                )
                self.assertEqual(resultado.estado, ESTADO_ESPERANDO_ALIMENTOS)
                self.assertEqual(
                    resultado.respuestas[0].texto,
                    MENSAJE_ALIMENTOS_INVALIDOS,
                )
                self._confirmar(resultado)

        valido = self.servicio.procesar_mensaje(
            "simulador", usuario, "avena y huevo"
        )
        self.assertEqual(valido.estado, ESTADO_LISTA)
        conversacion = self.repositorio.obtener_conversacion(
            "simulador", usuario
        )
        self.assertEqual(conversacion.meses_bebe, 6)
        self.assertEqual(conversacion.alimentos_contexto, "avena y huevo")

    def test_consulta_recibe_contexto_confirmado_de_categoria_y_perfil(self) -> None:
        self._completar_onboarding("contexto", meses="8")
        primer_resultado = crear_resultado(categoria="Alimentación")
        segundo_resultado = crear_resultado(categoria="Anemia")

        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            side_effect=(primer_resultado, segundo_resultado),
        ) as buscar:
            primera = self.servicio.procesar_mensaje(
                "simulador", "contexto", "alimentos con hierro"
            )
            self._confirmar(primera)
            segunda = self.servicio.procesar_mensaje(
                "simulador", "contexto", "y qué cantidad"
            )

        self.assertEqual(primera.respuestas[0].categoria, "Alimentación")
        self.assertEqual(segunda.respuestas[0].categoria, "Anemia")
        self.assertEqual(buscar.call_count, 2)
        _, segunda_llamada = buscar.call_args_list
        self.assertEqual(segunda_llamada.args, ("y qué cantidad", self.reglas))
        self.assertEqual(
            segunda_llamada.kwargs,
            {
                "categoria_anterior": "Alimentación",
                "meses_bebe": 8,
                "alimentos_contexto": "lentejas y pollo",
            },
        )

    def test_consulta_se_envia_en_seis_bloques_ordenados(self) -> None:
        self._completar_onboarding("bloques")
        resultado_motor = crear_resultado()

        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            return_value=resultado_motor,
        ):
            resultado = self.servicio.procesar_mensaje(
                "simulador", "bloques", "alimentos con hierro"
            )

        self.assertEqual(
            [respuesta.texto for respuesta in resultado.respuestas],
            [
                "Respuesta:\nRespuesta revisada",
                "Disclaimer:\nConsulta con un profesional de salud.",
                "Documento:\nGuía de prueba",
                "Página:\n10",
                "Enlace:\nhttps://example.test/guia",
                MENSAJE_RECORDATORIO_FIN,
            ],
        )
        self.assertEqual(resultado.respuestas[0].categoria, "Alimentación")
        self.assertTrue(resultado.respuestas[0].evidencia)
        self.assertTrue(
            all(
                respuesta.categoria is None and not respuesta.evidencia
                for respuesta in resultado.respuestas[1:]
            )
        )

    def test_consulta_muestra_enlace_no_disponible(self) -> None:
        self._completar_onboarding("sin-enlace")
        resultado_motor = crear_resultado()
        resultado_motor = replace(
            resultado_motor,
            regla=replace(resultado_motor.regla, enlace="  "),
        )

        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            return_value=resultado_motor,
        ):
            resultado = self.servicio.procesar_mensaje(
                "simulador", "sin-enlace", "alimentos con hierro"
            )

        self.assertEqual(resultado.respuestas[4].texto, "Enlace:\nNo disponible")

    def test_gemini_selecciona_regla_y_recibe_contexto_compacto(self) -> None:
        self._completar_onboarding("gemini", meses="8")
        primera_regla = crear_resultado(categoria="Alimentación").regla
        segunda_regla = crear_resultado(categoria="Anemia").regla
        clasificador = MagicMock()
        clasificador.seleccionar.side_effect = (
            SeleccionGoogle(
                id_regla="ANMI-0001",
                regla=primera_regla,
                evidencia={
                    "origen": "google",
                    "modelo": "gemini-3.1-flash-lite",
                    "modo_cache": "explicita",
                },
            ),
            SeleccionGoogle(
                id_regla="ANMI-0002",
                regla=segunda_regla,
                evidencia={"origen": "google"},
            ),
        )
        self.servicio.clasificador = clasificador

        primera = self.servicio.procesar_mensaje(
            "simulador", "gemini", "alimentos con hierro"
        )
        self._confirmar(primera)
        segunda = self.servicio.procesar_mensaje(
            "simulador", "gemini", "¿y qué cantidad?"
        )

        self.assertEqual(primera.respuestas[0].categoria, "Alimentación")
        self.assertIsNone(primera.respuestas[0].puntaje)
        self.assertEqual(
            primera.respuestas[0].evidencia["id_regla"],
            "ANMI-0001",
        )
        contexto = clasificador.seleccionar.call_args_list[1].args[1]
        self.assertEqual(contexto.meses_bebe, 8)
        self.assertEqual(contexto.alimentos_contexto, "lentejas y pollo")
        self.assertEqual(contexto.categoria_anterior, "Alimentación")
        self.assertEqual(contexto.subcategoria_anterior, "Hierro")
        self.assertEqual(
            contexto.consultas_anteriores,
            ("alimentos con hierro",),
        )
        self.assertEqual(segunda.respuestas[0].categoria, "Anemia")

    def test_error_gemini_usa_motor_anterior_como_fallback(self) -> None:
        self._completar_onboarding("fallback")
        clasificador = MagicMock()
        clasificador.seleccionar.side_effect = ErrorClasificacionGoogle(
            "servicio no disponible"
        )
        self.servicio.clasificador = clasificador
        resultado_local = crear_resultado(categoria="Anemia")

        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            return_value=resultado_local,
        ) as buscar:
            resultado = self.servicio.procesar_mensaje(
                "simulador", "fallback", "síntomas de anemia"
            )

        self.assertEqual(resultado.respuestas[0].categoria, "Anemia")
        self.assertEqual(
            resultado.respuestas[0].evidencia["motivo"],
            "evidencia suficiente",
        )
        buscar.assert_called_once()

    def test_gemini_prioriza_emergencia_antes_de_bienvenida(self) -> None:
        emergencia = crear_resultado(
            categoria="Emergencia",
            subcategoria="Signos de alarma",
            respuesta="Acude de inmediato a un centro de salud.",
        )
        clasificador = MagicMock()
        clasificador.seleccionar.return_value = SeleccionGoogle(
            id_regla="ANMI-0400",
            regla=emergencia.regla,
            evidencia={"origen": "google"},
        )
        self.servicio.clasificador = clasificador

        with patch("servicio_conversacion.buscar_mejor_regla") as buscar:
            resultado = self.servicio.procesar_mensaje(
                "whatsapp", "emergencia-google", "mi bebé no respira"
            )

        buscar.assert_not_called()
        self.assertEqual(resultado.respuestas[0].categoria, "Emergencia")
        self.assertEqual(resultado.respuestas[-1].texto, MENSAJE_BIENVENIDA)

    def test_fin_calificacion_finalizacion_dedupe_y_nueva_sesion(self) -> None:
        conversacion_id = self._completar_onboarding("cierre", meses="11")
        respuesta_clasificada = crear_resultado(categoria="Anemia")
        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            return_value=respuesta_clasificada,
        ):
            for consulta in ("síntomas de anemia", "más síntomas de anemia"):
                resultado = self.servicio.procesar_mensaje(
                    "simulador", "cierre", consulta
                )
                self._confirmar(resultado)

        fin = self.servicio.procesar_mensaje("simulador", "cierre", " FIN ")
        self.assertEqual(fin.estado, ESTADO_ESPERANDO_CALIFICACION)
        self.assertEqual(fin.respuestas[0].texto, MENSAJE_CALIFICACION)
        self.assertEqual(
            MENSAJE_CALIFICACION,
            "⭐ ¿Cómo calificarías la atención de ANMI?\n\n"
            "1. Mala ⭐\n"
            "2. Neutral ⭐⭐\n"
            "3. Buena ⭐⭐⭐\n"
            "4. Muy Buena ⭐⭐⭐⭐\n"
            "5. Excelente ⭐⭐⭐⭐⭐\n\n"
            "Responde solo con un número del 1 al 5.",
        )
        self._confirmar(fin)

        for valor in ("0", "6", "4.5", "cinco"):
            with self.subTest(calificacion=valor):
                invalida = self.servicio.procesar_mensaje(
                    "simulador", "cierre", valor
                )
                self.assertFalse(invalida.finalizacion_pendiente)
                self.assertEqual(
                    invalida.respuestas[0].texto,
                    MENSAJE_CALIFICACION_INVALIDA,
                )
                self._confirmar(invalida)

        calificacion = self.servicio.procesar_mensaje(
            "simulador", "cierre", "5"
        )
        self.assertTrue(calificacion.finalizacion_pendiente)
        self.assertEqual(
            calificacion.respuestas[0].texto,
            MENSAJE_AGRADECIMIENTO,
        )
        self.assertIsNotNone(
            self.repositorio.obtener_conversacion_por_id(conversacion_id)
        )
        self.assertEqual(self._confirmar(calificacion), 1)

        consulta_id = self.servicio.confirmar_finalizacion(conversacion_id)
        resumen = self.repositorio.obtener_consulta_finalizada(consulta_id)
        self.assertEqual(resumen.meses_bebe, 11)
        self.assertEqual(resumen.calificacion, 5)
        self.assertEqual(resumen.categorias, ("Anemia",))
        self.assertIsNone(
            self.repositorio.obtener_conversacion("simulador", "cierre")
        )
        self.assertEqual(self._contar("mensajes_activos"), 0)

        nueva = self._iniciar("cierre", "otra consulta")
        self.assertNotEqual(nueva.conversacion_id, conversacion_id)
        self.assertEqual(nueva.estado, ESTADO_ESPERANDO_MESES)
        self.assertEqual(self._contar("consultas_finalizadas"), 1)

    def test_id_externo_duplicado_no_genera_otra_respuesta(self) -> None:
        primero = self._iniciar(
            "duplicado",
            "hola",
            mensaje_externo_id="wamid.duplicado",
        )
        mensajes_antes = self.servicio.obtener_mensajes(
            "simulador", "duplicado"
        )

        segundo = self.servicio.procesar_mensaje(
            "simulador",
            "duplicado",
            "12",
            mensaje_externo_id="wamid.duplicado",
        )

        self.assertTrue(segundo.duplicado)
        self.assertEqual(segundo.respuestas, ())
        self.assertEqual(segundo.conversacion_id, primero.conversacion_id)
        self.assertEqual(
            self.servicio.obtener_mensajes("simulador", "duplicado"),
            mensajes_antes,
        )

    def test_limpieza_expira_transcript_sin_crear_resumen(self) -> None:
        instante_antiguo = datetime.now(UTC) - timedelta(hours=25)
        antigua = self.repositorio.crear_conversacion(
            "simulador",
            "expirada",
            ESTADO_LISTA,
            ahora=instante_antiguo,
        )
        self.repositorio.registrar_mensaje(
            antigua.id,
            "usuario",
            "mensaje antiguo",
            ahora=instante_antiguo,
        )

        self.assertEqual(self.servicio.limpiar_expiradas(), 1)
        self.assertIsNone(
            self.repositorio.obtener_conversacion("simulador", "expirada")
        )
        self.assertEqual(self._contar("mensajes_activos"), 0)
        self.assertEqual(self._contar("consultas_finalizadas"), 0)

    def test_emergencia_inicial_se_envia_antes_de_bienvenida(self) -> None:
        emergencia = crear_resultado(
            categoria="Emergencia",
            subcategoria="Signos de alarma",
            respuesta="Acude de inmediato a un centro de salud.",
        )
        with patch(
            "servicio_conversacion.buscar_mejor_regla",
            return_value=emergencia,
        ):
            resultado = self.servicio.procesar_mensaje(
                "whatsapp", "emergencia", "mi bebé no puede respirar"
            )

        self.assertEqual(len(resultado.respuestas), 7)
        self.assertEqual(resultado.respuestas[0].categoria, "Emergencia")
        self.assertEqual(
            resultado.respuestas[5].texto,
            MENSAJE_RECORDATORIO_FIN,
        )
        self.assertEqual(resultado.respuestas[6].texto, MENSAJE_BIENVENIDA)
        self.assertEqual(self._confirmar(resultado), 7)
        self.assertEqual(
            self.repositorio.listar_categorias(resultado.conversacion_id),
            ("Emergencia",),
        )


if __name__ == "__main__":
    unittest.main()
