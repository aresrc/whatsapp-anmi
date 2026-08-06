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
            "usuarios_conocidos",
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

    def test_usuario_conocido_se_registra_una_sola_vez_por_huella(self) -> None:
        huella = "a" * 64
        self.assertTrue(self.repositorio.registrar_usuario_conocido(huella))
        self.assertFalse(self.repositorio.registrar_usuario_conocido(huella))
        self.assertEqual(
            self._valor_sql("SELECT COUNT(*) FROM usuarios_conocidos"),
            1,
        )
        for invalida in ("a" * 63, "G" * 64, "telefono-directo"):
            with self.subTest(huella=invalida):
                with self.assertRaises(ValueError):
                    self.repositorio.registrar_usuario_conocido(invalida)

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

        for meses in (5, 37, -1, 60, 3.5, True):
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

        for comentario in ("", " " * 3, "x" * 1001):
            with self.subTest(comentario=comentario[:20]):
                with self.assertRaises(ValueError):
                    self.repositorio.actualizar_conversacion(
                        conversacion.id,
                        comentario_pendiente=comentario,
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
                    ) VALUES ('sql', 'menor-de-seis', 'lista', 5)
                    """
                )
            conexion.rollback()
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

    def test_rango_edad_es_exclusivo_y_se_conserva_en_resumen(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "simulador",
            "rango-edad",
        )
        con_rango = self.repositorio.actualizar_perfil(
            conversacion.id,
            rango_edad_bebe="12-24",
        )
        self.assertIsNone(con_rango.meses_bebe)
        self.assertEqual(con_rango.rango_edad_bebe, "12-24")

        with self.assertRaises(ValueError):
            self.repositorio.actualizar_perfil(
                conversacion.id,
                meses_bebe=10,
                rango_edad_bebe="6-12",
            )
        with self.assertRaises(ValueError):
            self.repositorio.actualizar_perfil(
                conversacion.id,
                rango_edad_bebe="0-5",
            )

        resumen = self.repositorio.finalizar_conversacion(
            conversacion.id,
            5,
        )
        self.assertIsNone(resumen.meses_bebe)
        self.assertEqual(resumen.rango_edad_bebe, "12-24")
        self.assertEqual(
            self.repositorio.obtener_consulta_finalizada(resumen.id),
            resumen,
        )

    def test_migracion_amplia_resumen_sin_perder_datos(self) -> None:
        ruta_legacy = Path(self.directorio_temporal.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(ruta_legacy)) as conexion:
            conexion.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE consultas_finalizadas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fecha_hora_cierre TEXT NOT NULL,
                    meses_bebe INTEGER CHECK (
                        meses_bebe IS NULL OR meses_bebe BETWEEN 0 AND 24
                    ),
                    calificacion INTEGER NOT NULL
                        CHECK (calificacion BETWEEN 1 AND 5)
                );
                CREATE TABLE categorias_consulta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    consulta_finalizada_id INTEGER NOT NULL,
                    categoria TEXT NOT NULL,
                    categoria_normalizada TEXT NOT NULL,
                    FOREIGN KEY (consulta_finalizada_id)
                        REFERENCES consultas_finalizadas (id)
                        ON DELETE CASCADE
                );
                INSERT INTO consultas_finalizadas (
                    id, fecha_hora_cierre, meses_bebe, calificacion
                ) VALUES (7, '2026-07-16T12:00:00.000-05:00', 11, 5);
                INSERT INTO categorias_consulta (
                    consulta_finalizada_id, categoria, categoria_normalizada
                ) VALUES (7, 'Anemia', 'anemia');
                """
            )

        repositorio = RepositorioConversaciones(ruta_legacy)
        repositorio.inicializar()
        anterior = repositorio.obtener_consulta_finalizada(7)
        self.assertEqual(anterior.meses_bebe, 11)
        self.assertIsNone(anterior.rango_edad_bebe)
        self.assertEqual(anterior.categorias, ("Anemia",))

        activa = repositorio.crear_conversacion("simulador", "30-meses")
        repositorio.actualizar_perfil(activa.id, meses_bebe=30)
        nueva = repositorio.finalizar_conversacion(activa.id, 4)
        self.assertEqual(nueva.meses_bebe, 30)

    def test_migracion_recupera_edad_manual_menor_de_seis(self) -> None:
        ruta_legacy = Path(self.directorio_temporal.name) / "edad-invalida.sqlite3"
        with closing(sqlite3.connect(ruta_legacy)) as conexion:
            conexion.executescript(
                """
                CREATE TABLE conversaciones_activas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canal TEXT NOT NULL,
                    usuario_temporal TEXT NOT NULL,
                    estado TEXT NOT NULL,
                    fecha_inicio_utc TEXT NOT NULL,
                    fecha_ultima_actividad_utc TEXT NOT NULL,
                    meses_bebe INTEGER CHECK (
                        meses_bebe IS NULL OR meses_bebe BETWEEN 0 AND 59
                    ),
                    alimentos_contexto TEXT,
                    calificacion_pendiente INTEGER,
                    UNIQUE (canal, usuario_temporal)
                );
                INSERT INTO conversaciones_activas (
                    canal, usuario_temporal, estado,
                    fecha_inicio_utc, fecha_ultima_actividad_utc,
                    meses_bebe, alimentos_contexto
                ) VALUES (
                    'simulador', 'manual', 'lista',
                    '2026-07-16T12:00:00.000Z',
                    '2026-07-16T12:00:00.000Z',
                    3, 'avena'
                );
                """
            )

        repositorio = RepositorioConversaciones(ruta_legacy)
        repositorio.inicializar()
        recuperada = repositorio.obtener_conversacion(
            "simulador",
            "manual",
        )

        self.assertIsNotNone(recuperada)
        self.assertEqual(recuperada.estado, "esperando_meses")
        self.assertIsNone(recuperada.meses_bebe)
        self.assertIsNone(recuperada.rango_edad_bebe)
        self.assertIsNone(recuperada.alimentos_contexto)
        esperando_receta = repositorio.actualizar_estado(
            recuperada.id,
            "esperando_edad_receta",
        )
        self.assertEqual(esperando_receta.estado, "esperando_edad_receta")

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
            comentario="  La respuesta fue clara.  ",
            fecha_hora=cierre,
        )

        self.assertEqual(resumen.meses_bebe, 11)
        self.assertEqual(resumen.calificacion, 5)
        self.assertEqual(resumen.comentario, "La respuesta fue clara.")
        self.assertEqual(resumen.categorias, ("Nutrición", "Anemia"))
        self.assertIsNone(
            self.repositorio.obtener_conversacion_por_id(conversacion.id)
        )
        self.assertEqual(self.repositorio.listar_mensajes(conversacion.id), ())
        recuperado = self.repositorio.obtener_consulta_finalizada(resumen.id)
        self.assertEqual(recuperado, resumen)

        self.assertEqual(
            self._columnas("consultas_finalizadas"),
            {
                "id",
                "fecha_hora_cierre",
                "meses_bebe",
                "rango_edad_bebe",
                "calificacion",
                "comentario",
            },
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

    def test_persistencia_de_categoria_y_pagina_del_menu(self) -> None:
        conversacion = self.repositorio.crear_conversacion(
            "simulador",
            "usuario-menu",
        )
        actualizada = self.repositorio.actualizar_conversacion(
            conversacion.id,
            estado="menu_especifico",
            categoria_menu="Alimentación (Bebés)",
            pagina_menu=3,
        )
        self.assertEqual(actualizada.estado, "menu_especifico")
        self.assertEqual(actualizada.categoria_menu, "Alimentación (Bebés)")
        self.assertEqual(actualizada.pagina_menu, 3)

        with self.assertRaisesRegex(ValueError, "pagina_menu"):
            self.repositorio.actualizar_conversacion(
                conversacion.id,
                pagina_menu=-1,
            )


if __name__ == "__main__":
    unittest.main()
