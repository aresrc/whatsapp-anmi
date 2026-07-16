from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conversaciones import (
    ConversacionNoEncontradaError,
    MensajeExternoDuplicadoError,
    RepositorioConversaciones,
)


UTC = timezone.utc


class RepositorioConversacionesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directorio_temporal = tempfile.TemporaryDirectory()
        self.ruta_bd = Path(self.directorio_temporal.name) / "anmi-test.sqlite3"
        self.ruta_esquema = Path(__file__).resolve().parents[1] / "schema.sql"
        self.repositorio = RepositorioConversaciones(
            ruta_bd=self.ruta_bd,
            ruta_esquema=self.ruta_esquema,
        )
        self.repositorio.inicializar()

    def tearDown(self) -> None:
        self.directorio_temporal.cleanup()

    def _valor_sql(self, consulta: str, parametros: tuple[object, ...] = ()):
        with closing(sqlite3.connect(self.ruta_bd)) as conexion:
            fila = conexion.execute(consulta, parametros).fetchone()
        self.assertIsNotNone(fila)
        return fila[0]

    def _columnas(self, tabla: str) -> set[str]:
        with closing(sqlite3.connect(self.ruta_bd)) as conexion:
            filas = conexion.execute(f"PRAGMA table_info({tabla})").fetchall()
        return {fila[1] for fila in filas}

    def test_inicializacion_es_idempotente_y_preserva_datos(self) -> None:
        creada = self.repositorio.crear_conversacion(
            "whatsapp",
            "usuario-1",
            ahora=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        )

        self.repositorio.inicializar()
        self.repositorio.inicializar()

        recuperada = self.repositorio.obtener_conversacion(
            "whatsapp",
            "usuario-1",
        )
        self.assertIsNotNone(recuperada)
        self.assertEqual(recuperada.id, creada.id)
        self.assertEqual(
            self._valor_sql("PRAGMA journal_mode").lower(),
            "wal",
        )
        tablas = {
            "conversaciones_activas",
            "mensajes_activos",
            "consultas_finalizadas",
            "categorias_consulta",
        }
        with closing(sqlite3.connect(self.ruta_bd)) as conexion:
            encontradas = {
                fila[0]
                for fila in conexion.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(tablas.issubset(encontradas))

    def test_crear_actualizar_y_listar_conversacion_y_mensajes(self) -> None:
        inicio = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
        conversacion = self.repositorio.obtener_o_crear_conversacion(
            "simulador",
            "navegador-1",
            ahora=inicio,
        )
        repetida = self.repositorio.crear_conversacion(
            "simulador",
            "navegador-1",
            estado="lista",
            ahora=inicio + timedelta(minutes=1),
        )

        self.assertEqual(repetida.id, conversacion.id)
        self.assertEqual(repetida.estado, "esperando_meses")
        self.assertEqual(repetida.fecha_inicio_utc, inicio)

        actualizada = self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado="lista",
            meses_bebe=8,
            alimentos_contexto="  lentejas y huevo  ",
            calificacion_pendiente=4,
            ahora=inicio + timedelta(minutes=2),
        )
        self.assertEqual(actualizada.estado, "lista")
        self.assertEqual(actualizada.meses_bebe, 8)
        self.assertEqual(actualizada.alimentos_contexto, "lentejas y huevo")
        self.assertEqual(actualizada.calificacion_pendiente, 4)
        self.assertEqual(
            actualizada.fecha_ultima_actividad_utc,
            inicio + timedelta(minutes=2),
        )

        usuario = self.repositorio.guardar_mensaje(
            conversacion.id,
            "usuario",
            "Mi bebe esta cansado",
            id_externo="wamid.1",
            ahora=inicio + timedelta(minutes=3),
        )
        bot = self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "Respuesta revisada",
            categoria="Anemia",
            subcategoria="Síntomas",
            puntaje=17.25,
            evidencia={"exactas": ["cansado"], "aceptada": True},
            ahora=inicio + timedelta(minutes=4),
        )

        mensajes = self.repositorio.listar_mensajes(conversacion.id)
        self.assertEqual([mensaje.id for mensaje in mensajes], [usuario.id, bot.id])
        self.assertEqual([mensaje.rol for mensaje in mensajes], ["usuario", "bot"])
        self.assertEqual(mensajes[1].categoria, "Anemia")
        self.assertEqual(mensajes[1].subcategoria, "Síntomas")
        self.assertEqual(mensajes[1].puntaje, 17.25)
        self.assertEqual(self.repositorio.ultima_categoria(conversacion.id), "Anemia")
        self.assertEqual(
            self.repositorio.obtener_conversacion_por_id(conversacion.id),
            self.repositorio.obtener_conversacion("simulador", "navegador-1"),
        )

    def test_id_externo_se_deduplica_globalmente(self) -> None:
        primera = self.repositorio.crear_conversacion("whatsapp", "usuario-a")
        segunda = self.repositorio.crear_conversacion("whatsapp", "usuario-b")

        self.assertTrue(
            self.repositorio.registrar_mensaje(
                primera.id,
                "usuario",
                "primer envío",
                mensaje_externo_id="wamid.repetido",
            )
        )
        self.assertFalse(
            self.repositorio.registrar_mensaje(
                primera.id,
                "usuario",
                "reenvío",
                mensaje_externo_id="wamid.repetido",
            )
        )
        self.assertFalse(
            self.repositorio.registrar_mensaje(
                segunda.id,
                "usuario",
                "reenvío asociado a otro usuario",
                mensaje_externo_id="wamid.repetido",
            )
        )
        with self.assertRaises(MensajeExternoDuplicadoError):
            self.repositorio.guardar_mensaje(
                segunda.id,
                "usuario",
                "duplicado estricto",
                id_externo="wamid.repetido",
            )

        self.assertTrue(self.repositorio.existe_id_externo("wamid.repetido"))
        self.assertEqual(len(self.repositorio.listar_mensajes(primera.id)), 1)
        self.assertEqual(self.repositorio.listar_mensajes(segunda.id), ())

    def test_usuarios_y_canales_permanecen_aislados(self) -> None:
        usuario_a = self.repositorio.crear_conversacion("whatsapp", "usuario-a")
        usuario_b = self.repositorio.crear_conversacion("whatsapp", "usuario-b")
        otro_canal = self.repositorio.crear_conversacion("simulador", "usuario-a")

        self.repositorio.actualizar_perfil(usuario_a.id, meses_bebe=6)
        self.repositorio.actualizar_perfil(usuario_b.id, meses_bebe=18)
        self.repositorio.guardar_mensaje(usuario_a.id, "usuario", "mensaje A")
        self.repositorio.guardar_mensaje(usuario_b.id, "usuario", "mensaje B")
        self.repositorio.guardar_mensaje(otro_canal.id, "usuario", "mensaje web")

        self.assertEqual(
            self.repositorio.obtener_conversacion("whatsapp", "usuario-a").id,
            usuario_a.id,
        )
        self.assertEqual(
            self.repositorio.obtener_conversacion("simulador", "usuario-a").id,
            otro_canal.id,
        )
        self.assertEqual(
            [mensaje.contenido for mensaje in self.repositorio.listar_mensajes(usuario_a.id)],
            ["mensaje A"],
        )
        self.assertEqual(
            [mensaje.contenido for mensaje in self.repositorio.listar_mensajes(usuario_b.id)],
            ["mensaje B"],
        )
        self.assertEqual(
            [mensaje.contenido for mensaje in self.repositorio.listar_mensajes(otro_canal.id)],
            ["mensaje web"],
        )

    def test_restricciones_de_meses_y_calificacion_en_api_y_sql(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "simulador",
            "restricciones",
        )

        for meses in (-1, 60, 3.5, True):
            with self.subTest(meses=meses):
                with self.assertRaises(ValueError):
                    self.repositorio.actualizar_perfil(
                        conversacion.id,
                        meses_bebe=meses,
                    )

        for calificacion in (0, 6, 2.5, True):
            with self.subTest(calificacion=calificacion):
                with self.assertRaises(ValueError):
                    self.repositorio.finalizar_conversacion(
                        conversacion.id,
                        calificacion,
                    )

        self.assertIsNotNone(
            self.repositorio.obtener_conversacion_por_id(conversacion.id)
        )

        conexion = sqlite3.connect(self.ruta_bd)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conexion.execute(
                    """
                    INSERT INTO conversaciones_activas (
                        canal, usuario_temporal, estado, meses_bebe
                    ) VALUES ('sql', 'meses-invalidos', 'activo', 60)
                    """
                )
            conexion.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conexion.execute(
                    """
                    INSERT INTO consultas_finalizadas (
                        fecha_hora_cierre, meses_bebe, calificacion
                    ) VALUES ('2026-07-16T12:00:00.000-05:00', 5, 0)
                    """
                )
        finally:
            conexion.close()

    def test_evidencia_se_serializa_y_recupera_sin_perdidas(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "simulador",
            "evidencia",
        )
        evidencia = {
            "aceptada": True,
            "coincidencias": {
                "exactas": ["anemia", "niño"],
                "fuzzy": [{"original": "anemiia", "destino": "anemia"}],
            },
            "margen": 0.24,
        }

        guardado = self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta",
            evidencia=evidencia,
        )

        self.assertEqual(guardado.evidencia, evidencia)
        self.assertEqual(
            self.repositorio.listar_mensajes(conversacion.id)[0].evidencia,
            evidencia,
        )
        with closing(sqlite3.connect(self.ruta_bd)) as conexion:
            evidencia_json, es_valido = conexion.execute(
                """
                SELECT evidencia_json, json_valid(evidencia_json)
                FROM mensajes_activos
                WHERE id = ?
                """,
                (guardado.id,),
            ).fetchone()
        self.assertEqual(es_valido, 1)
        self.assertEqual(json.loads(evidencia_json), evidencia)
        self.assertIn("niño", evidencia_json)

        with self.assertRaises(ValueError):
            self.repositorio.guardar_mensaje(
                conversacion.id,
                "bot",
                "evidencia no JSON",
                evidencia={"puntaje": float("nan")},
            )
        self.assertEqual(len(self.repositorio.listar_mensajes(conversacion.id)), 1)

    def test_categorias_son_unicas_normalizadas_y_solo_del_bot(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "simulador",
            "categorias",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta 1",
            categoria="Alimentación",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "usuario",
            "texto de usuario",
            categoria="Emergencia",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta 2",
            categoria="alimentacion",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta 3",
            categoria="ANEMIA",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta 4",
            categoria="Anémia",
        )

        self.assertEqual(
            self.repositorio.listar_categorias(conversacion.id),
            ("Alimentación", "ANEMIA"),
        )
        self.assertEqual(
            self.repositorio.ultima_categoria(conversacion.id),
            "Anémia",
        )

    def test_finalizacion_borra_identidad_y_transcript_y_conserva_resumen(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "whatsapp",
            "telefono-super-secreto",
        )
        self.repositorio.actualizar_perfil(
            conversacion.id,
            meses_bebe=11,
            alimentos_contexto="alimento-super-secreto",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "usuario",
            "transcript-super-secreto",
            id_externo="wamid.super-secreto",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta 1",
            categoria="Nutrición",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta duplicada",
            categoria="nutricion",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta 2",
            categoria="Anemia",
        )

        cierre = datetime(2026, 7, 16, 18, 45, tzinfo=UTC)
        resumen = self.repositorio.finalizar_conversacion(
            conversacion.id,
            5,
            fecha_hora=cierre,
        )

        self.assertEqual(resumen.meses_bebe, 11)
        self.assertEqual(resumen.calificacion, 5)
        self.assertEqual(resumen.categorias, ("Nutrición", "Anemia"))
        self.assertIsNone(
            self.repositorio.obtener_conversacion_por_id(conversacion.id)
        )
        self.assertEqual(self.repositorio.listar_mensajes(conversacion.id), ())
        recuperado = self.repositorio.obtener_consulta_finalizada(resumen.id)
        self.assertEqual(recuperado, resumen)

        self.assertEqual(
            self._columnas("consultas_finalizadas"),
            {"id", "fecha_hora_cierre", "meses_bebe", "calificacion"},
        )
        self.assertEqual(
            self._columnas("categorias_consulta"),
            {
                "id",
                "consulta_finalizada_id",
                "categoria",
                "categoria_normalizada",
            },
        )
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM conversaciones_activas"),
            0,
        )
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM mensajes_activos"),
            0,
        )
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM consultas_finalizadas"),
            1,
        )
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM categorias_consulta"),
            2,
        )

    def test_fecha_de_cierre_se_guarda_en_hora_de_lima(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "simulador",
            "zona-horaria",
        )
        instante_utc = datetime(2026, 7, 16, 2, 30, tzinfo=UTC)

        resumen = self.repositorio.finalizar_conversacion(
            conversacion.id,
            4,
            fecha_hora=instante_utc,
        )

        self.assertEqual(
            resumen.fecha_hora_cierre,
            datetime(
                2026,
                7,
                15,
                21,
                30,
                tzinfo=timezone(timedelta(hours=-5)),
            ),
        )
        self.assertEqual(
            self._valor_sql(
                "SELECT fecha_hora_cierre FROM consultas_finalizadas WHERE id = ?",
                (resumen.id,),
            ),
            "2026-07-15T21:30:00.000-05:00",
        )

    def test_reinicio_borra_sesion_sin_crear_resumen(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "simulador",
            "reinicio",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "usuario",
            "mensaje temporal",
        )

        self.assertTrue(
            self.repositorio.borrar_conversacion_activa(conversacion.id)
        )
        self.assertFalse(
            self.repositorio.borrar_conversacion_activa(conversacion.id)
        )
        self.assertIsNone(
            self.repositorio.obtener_conversacion("simulador", "reinicio")
        )
        self.assertEqual(self.repositorio.listar_mensajes(conversacion.id), ())
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM consultas_finalizadas"),
            0,
        )
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM categorias_consulta"),
            0,
        )

    def test_caducidad_elimina_solo_sesiones_mayores_de_24_horas(self) -> None:
        ahora = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)
        antigua = self.repositorio.crear_conversacion(
            "simulador",
            "antigua",
            ahora=ahora - timedelta(hours=24, milliseconds=1),
        )
        limite = self.repositorio.crear_conversacion(
            "simulador",
            "en-limite",
            ahora=ahora - timedelta(hours=24),
        )
        reciente = self.repositorio.crear_conversacion(
            "simulador",
            "reciente",
            ahora=ahora - timedelta(hours=23),
        )
        self.repositorio.guardar_mensaje(
            antigua.id,
            "usuario",
            "debe caducar",
            ahora=ahora - timedelta(hours=24, milliseconds=1),
        )

        eliminadas = self.repositorio.caducar_conversaciones(
            horas_inactividad=24,
            ahora=ahora,
        )

        self.assertEqual(eliminadas, 1)
        self.assertIsNone(self.repositorio.obtener_conversacion_por_id(antigua.id))
        self.assertIsNotNone(self.repositorio.obtener_conversacion_por_id(limite.id))
        self.assertIsNotNone(self.repositorio.obtener_conversacion_por_id(reciente.id))
        self.assertEqual(self.repositorio.listar_mensajes(antigua.id), ())
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM consultas_finalizadas"),
            0,
        )
        self.assertEqual(
            self.repositorio.caducar_conversaciones(24, ahora=ahora),
            0,
        )
        with self.assertRaises(ValueError):
            self.repositorio.caducar_conversaciones(0, ahora=ahora)

    def test_finalizacion_hace_rollback_completo_si_falla_el_borrado(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "whatsapp",
            "rollback-usuario",
        )
        self.repositorio.actualizar_perfil(conversacion.id, meses_bebe=9)
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "usuario",
            "transcript que debe sobrevivir al fallo",
        )
        self.repositorio.guardar_mensaje(
            conversacion.id,
            "bot",
            "respuesta clasificada",
            categoria="Anemia",
        )
        with closing(sqlite3.connect(self.ruta_bd)) as conexion:
            conexion.execute(
                """
                CREATE TRIGGER impedir_borrado_en_prueba
                BEFORE DELETE ON conversaciones_activas
                BEGIN
                    SELECT RAISE(ABORT, 'fallo simulado');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repositorio.finalizar_conversacion(
                conversacion.id,
                5,
                fecha_hora=datetime(2026, 7, 16, 18, 0, tzinfo=UTC),
            )

        activa = self.repositorio.obtener_conversacion_por_id(conversacion.id)
        self.assertIsNotNone(activa)
        self.assertEqual(activa.usuario_temporal, "rollback-usuario")
        self.assertEqual(activa.meses_bebe, 9)
        self.assertEqual(len(self.repositorio.listar_mensajes(conversacion.id)), 2)
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM consultas_finalizadas"),
            0,
        )
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM categorias_consulta"),
            0,
        )

    def test_operaciones_sobre_conversacion_inexistente_fallan(self) -> None:
        with self.assertRaises(ConversacionNoEncontradaError):
            self.repositorio.actualizar_estado(999_999, "lista")
        with self.assertRaises(ConversacionNoEncontradaError):
            self.repositorio.guardar_mensaje(999_999, "usuario", "texto")
        with self.assertRaises(ConversacionNoEncontradaError):
            self.repositorio.listar_categorias(999_999)
        with self.assertRaises(ConversacionNoEncontradaError):
            self.repositorio.finalizar_conversacion(999_999, 5)


if __name__ == "__main__":
    unittest.main()
