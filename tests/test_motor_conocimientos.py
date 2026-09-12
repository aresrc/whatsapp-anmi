from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import unittest
from pathlib import Path

import motor_conocimientos as motor


ENCABEZADOS_V2 = [
    "ID",
    "Categor\u00eda",
    "Subcategor\u00eda",
    "Palabras Clave (Para el bot)",
    "Respuesta Verificada (Lenguaje sencillo para el chat)",
    "Disclaimer Obligatorio (\u00a1Cr\u00edtico!)",
    "Paginas",
    "Documento",
    "Enlace",
    "Ingredientes de la receta",
]

PREFIJOS_VISUALES = (
    "🆘 ",
    "⚠️ ",
    "🍲 ",
    "🍊 ",
    "🥗 ",
    "💊 ",
    "🥄 ",
    "🛡️ ",
    "🩺 ",
    "ℹ️ ",
)
HUELLA_RESPUESTAS_ANTES_DEL_FORMATO = (
    "0f363cf1772145061ed73e21d81c45bd650e026292627c383d4fd9c5ea8d1514"
)


def _sin_una_pareja_de_comillas(texto: str) -> str:
    """Replica de forma independiente el contrato de limpieza del CSV."""

    limpio = texto.strip()
    if len(limpio) >= 2 and limpio[0] == limpio[-1] == '"':
        return limpio[1:-1]
    return limpio


def _retirar_formato_visual(texto: str) -> str:
    """Retira exclusivamente la presentacion agregada a las respuestas."""

    limpio = _sin_una_pareja_de_comillas(texto)
    for prefijo in PREFIJOS_VISUALES:
        if limpio.startswith(prefijo):
            limpio = limpio.removeprefix(prefijo)
            break
    return f'"{limpio.replace("*", "").replace(chr(10), " ")}"'


class CsvMotorConocimientosV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ruta_csv = Path(motor.RUTA_CSV)
        with cls.ruta_csv.open(encoding="utf-8-sig", newline="") as archivo:
            lector = csv.DictReader(archivo)
            cls.encabezados = list(lector.fieldnames or ())
            cls.filas = tuple(lector)
        cls.reglas = motor.cargar_motor_conocimientos(cls.ruta_csv)

    def test_usa_csv_v2_con_bom_diez_encabezados_y_412_filas(self) -> None:
        self.assertEqual(self.ruta_csv.name, "motor_conocimientos_v2.csv")
        self.assertEqual(self.ruta_csv.read_bytes()[:3], b"\xef\xbb\xbf")
        self.assertEqual(self.encabezados, ENCABEZADOS_V2)
        self.assertEqual(len(self.filas), 412)
        self.assertEqual(len(self.reglas), 412)

    def test_recetas_cargan_ingredientes_desde_csv(self) -> None:
        recetas = [
            regla
            for regla in self.reglas
            if motor.normalizar_texto(regla.categoria).startswith("recetas minsa")
        ]
        self.assertEqual(len(recetas), 14)
        self.assertTrue(all(regla.ingredientes_receta for regla in recetas))
        pure = next(regla for regla in recetas if regla.id_regla == "ANMI-0159")
        self.assertEqual(pure.ingredientes_receta, ("bazo", "camote", "arroz"))
        self.assertEqual(len(motor.obtener_perfiles_recetas(self.reglas)), 14)

    def test_receta_variada_solo_alterna_entre_empates_seguros(self) -> None:
        recetas = motor.filtrar_recetas_por_edad(
            motor.obtener_perfiles_recetas(self.reglas),
            rango_edad_bebe="6-8",
        )
        perfil = motor.extraer_perfil_alimentario("come bazo")
        seleccionadas = {
            motor.seleccionar_receta_variada(
                recetas,
                perfil,
                generador=random.Random(semilla),
            ).regla.id_regla
            for semilla in range(10)
        }
        self.assertEqual(seleccionadas, {"ANMI-0157", "ANMI-0159"})

    def test_carga_metadatos_de_la_fuente_sin_perderlos(self) -> None:
        primera = self.reglas[0]

        self.assertEqual(primera.paginas, "6")
        self.assertEqual(
            primera.documento,
            "RESO-5440166-resolucion-ministerial-n-251-2024-minsa.pdf",
        )
        self.assertEqual(
            primera.enlace,
            "https://drive.google.com/file/d/"
            "1hZSXK8y7sbgNLRXKrxErAFSkjh8kfkW0/view?usp=drive_link",
        )
        self.assertTrue(any(regla.paginas for regla in self.reglas))
        self.assertTrue(any(regla.documento for regla in self.reglas))
        self.assertTrue(any(regla.enlace for regla in self.reglas))

    def test_elimina_exactamente_una_pareja_de_comillas_externas(self) -> None:
        fila = self.filas[0]
        respuesta_csv = fila[ENCABEZADOS_V2[4]]
        disclaimer_csv = fila[ENCABEZADOS_V2[5]]

        self.assertTrue(respuesta_csv.startswith('"'))
        self.assertTrue(respuesta_csv.endswith('"'))
        self.assertEqual(
            self.reglas[0].respuesta,
            _sin_una_pareja_de_comillas(respuesta_csv),
        )
        self.assertEqual(
            self.reglas[0].disclaimer,
            _sin_una_pareja_de_comillas(disclaimer_csv),
        )
        self.assertEqual(
            motor._quitar_comillas_externas('""contenido revisado""'),
            '"contenido revisado"',
        )

    def test_preserva_todos_los_disclaimers_sin_reescribirlos(self) -> None:
        for numero, (fila, regla) in enumerate(
            zip(self.filas, self.reglas, strict=True),
            start=2,
        ):
            with self.subTest(fila=numero):
                esperado = _sin_una_pareja_de_comillas(
                    fila[ENCABEZADOS_V2[5]]
                )
                self.assertEqual(regla.disclaimer, esperado)

        self.assertIn(
            "El diagn\u00f3stico solo lo puede hacer un profesional de la salud",
            self.reglas[0].disclaimer,
        )

    def test_respuestas_sobre_hemoglobina_estan_etiquetadas(self) -> None:
        filas = [
            fila
            for fila in self.filas
            if "hemoglobina"
            in motor.normalizar_texto(fila[ENCABEZADOS_V2[4]])
        ]

        self.assertEqual(len(filas), 29)
        for fila in filas:
            with self.subTest(id=fila["ID"]):
                etiquetas = motor.normalizar_texto(
                    f"{fila[ENCABEZADOS_V2[1]]} "
                    f"{fila[ENCABEZADOS_V2[2]]}"
                )
                self.assertIn("hemoglobina", etiquetas)

    def test_conserva_exactamente_dos_emojis_de_alerta(self) -> None:
        alerta = "\U0001f6a8"
        reglas_con_alerta = [
            regla for regla in self.reglas if alerta in regla.respuesta
        ]

        self.assertEqual(
            sum(regla.respuesta.count(alerta) for regla in self.reglas),
            2,
        )
        self.assertEqual(len(reglas_con_alerta), 2)
        self.assertTrue(
            all("emergencia" in motor.normalizar_texto(regla.categoria)
                for regla in reglas_con_alerta)
        )

    def test_aplica_formato_visual_a_la_mayoria_de_respuestas(self) -> None:
        respuestas_con_negrita = [
            regla.respuesta
            for regla in self.reglas
            if re.search(r"\*[^*\n]+\*", regla.respuesta)
        ]

        self.assertTrue(
            all(
                regla.respuesta.startswith(PREFIJOS_VISUALES)
                for regla in self.reglas
            )
        )
        self.assertGreaterEqual(len(respuestas_con_negrita), 300)

    def test_recetas_minsa_tienen_emoji_negrita_y_pasos_separados(self) -> None:
        recetas = [
            regla
            for regla in self.reglas
            if "recetas minsa" in motor.normalizar_texto(regla.categoria)
        ]

        self.assertEqual(len(recetas), 14)
        for receta in recetas:
            with self.subTest(subcategoria=receta.subcategoria):
                self.assertTrue(receta.respuesta.startswith("🍲 "))
                self.assertRegex(receta.respuesta, r"\*'[^']+'\*")
                self.assertIn("\n*1.*", receta.respuesta)
                self.assertIn("\n*2.*", receta.respuesta)

    def test_listas_numeradas_usan_saltos_de_linea_reales(self) -> None:
        respuestas_con_lista = [
            regla.respuesta
            for regla in self.reglas
            if "\n" in regla.respuesta
        ]

        self.assertEqual(len(respuestas_con_lista), 35)
        for respuesta in respuestas_con_lista:
            with self.subTest(respuesta=respuesta[:80]):
                self.assertNotIn(r"\n", respuesta)
                self.assertRegex(respuesta, r"\n\*(?:\d+\.|Paso \d+:)\*")

    def test_formato_no_cambia_el_contenido_medico_de_las_respuestas(self) -> None:
        respuestas_sin_formato = [
            {
                "ID": fila["ID"],
                "respuesta": _retirar_formato_visual(
                    fila[ENCABEZADOS_V2[4]]
                ),
            }
            for fila in self.filas
        ]
        contenido = json.dumps(
            respuestas_sin_formato,
            ensure_ascii=False,
            sort_keys=True,
        ).encode()

        self.assertEqual(
            hashlib.sha256(contenido).hexdigest(),
            HUELLA_RESPUESTAS_ANTES_DEL_FORMATO,
        )


class NormalizacionYBusquedaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reglas = motor.cargar_motor_conocimientos()

    def test_normaliza_tildes_puntuacion_espacios_y_alias(self) -> None:
        self.assertEqual(
            motor.normalizar_texto("  \u00a1QU\u00c9 es el BB?!  "),
            "definicion el bebe",
        )
        self.assertEqual(
            motor.normalizar_texto("\u00bfQu\u00e9 significa la ANEMIA?"),
            "significa la anemia",
        )
        self.assertEqual(
            motor.normalizar_texto("NI\u00d1O,\t \u00c1RBOL"),
            "nino arbol",
        )
        self.assertEqual(motor.tokenizar("BB, beb\u00e9; bb"), ("bebe",))

    def test_puntaje_exacto_respeta_limites_de_palabra(self) -> None:
        self.assertEqual(
            motor.calcular_puntaje("anemia infantil", "anemia", idf=2.0),
            6.0,
        )
        self.assertEqual(
            motor.calcular_puntaje("antianemia", "anemia", idf=2.0),
            0.0,
        )

    def test_consulta_exacta_que_es_la_anemia(self) -> None:
        resultado = motor.buscar_mejor_regla(
            "que es la anemia",
            self.reglas,
        )

        self.assertIsNotNone(resultado)
        assert resultado is not None
        self.assertEqual(resultado.categoria, "Definici\u00f3n y S\u00edntomas")
        self.assertEqual(
            resultado.subcategoria,
            "\u00bfQu\u00e9 es la Anemia? (B\u00e1sico)",
        )
        self.assertEqual(
            set(resultado.coincidencias_exactas),
            {"anemia", "definicion"},
        )
        self.assertEqual(resultado.coincidencias_aproximadas, ())
        self.assertEqual(
            resultado.motivo,
            "coincidencias_y_margen_suficientes",
        )

    def test_tolera_errores_ortograficos_representativos(self) -> None:
        casos = (
            (
                "que es la anemiia",
                ("anemiia", "anemia"),
                "\u00bfQu\u00e9 es la Anemia? (B\u00e1sico)",
            ),
            (
                "cuanta comida debe comer mi bebe de 6 meces",
                ("meces", "meses"),
                "Cantidad de Comida",
            ),
            (
                "como dar gotaz hierro bebe",
                ("gotaz", "gotas"),
                "Dosis (Preventiva)",
            ),
            (
                "mi hijo esta cansdo y duerme mucho",
                ("cansdo", "cansado"),
                "S\u00edntoma: Sue\u00f1o/Cansancio",
            ),
        )

        for mensaje, correccion_esperada, subcategoria in casos:
            with self.subTest(mensaje=mensaje):
                resultado = motor.buscar_mejor_regla(mensaje, self.reglas)
                self.assertIsNotNone(resultado)
                assert resultado is not None
                correcciones = {
                    (coincidencia.original, coincidencia.corregida)
                    for coincidencia in resultado.coincidencias_aproximadas
                }
                self.assertIn(correccion_esperada, correcciones)
                self.assertEqual(resultado.subcategoria, subcategoria)

    def test_terminos_genericos_no_generan_respuesta(self) -> None:
        for mensaje in (
            "hola",
            "bebe",
            "comida",
            "hierro",
            "anemia",
            "gracias",
        ):
            with self.subTest(mensaje=mensaje):
                self.assertIsNone(
                    motor.buscar_mejor_regla(mensaje, self.reglas)
                )

    def test_empate_ambiguo_retorna_none(self) -> None:
        reglas_ambiguas = (
            motor.ReglaConocimiento(
                "Categoria Uno",
                "Alternativa A",
                ("intencion", "detalle"),
                "Respuesta A",
                "Disclaimer A",
            ),
            motor.ReglaConocimiento(
                "Categoria Dos",
                "Alternativa B",
                ("intencion", "detalle"),
                "Respuesta B",
                "Disclaimer B",
            ),
        )

        self.assertIsNone(
            motor.buscar_mejor_regla("intencion detalle", reglas_ambiguas)
        )

    def test_contexto_de_categoria_solo_incrementa_evidencia_existente(self) -> None:
        regla = motor.ReglaConocimiento(
            "Prevencion",
            "Continuidad",
            ("sangrecita", "limon"),
            "Respuesta",
            "Disclaimer",
        )
        reglas = (regla,)

        sin_contexto = motor.buscar_mejor_regla(
            "sangrecita limon",
            reglas,
        )
        mismo_contexto = motor.buscar_mejor_regla(
            "sangrecita limon",
            reglas,
            categoria_anterior="Prevencion",
        )
        otro_contexto = motor.buscar_mejor_regla(
            "sangrecita limon",
            reglas,
            categoria_anterior="Otra categoria",
        )

        self.assertIsNotNone(sin_contexto)
        self.assertIsNotNone(mismo_contexto)
        self.assertIsNotNone(otro_contexto)
        assert sin_contexto and mismo_contexto and otro_contexto
        self.assertGreater(mismo_contexto.puntaje, sin_contexto.puntaje)
        self.assertAlmostEqual(otro_contexto.puntaje, sin_contexto.puntaje)
        self.assertIsNone(
            motor.buscar_mejor_regla(
                "hola",
                reglas,
                categoria_anterior="Prevencion",
            )
        )

    def test_edad_ajusta_categoria_y_edad_explicita_prevalece(self) -> None:
        reglas = (
            motor.ReglaConocimiento(
                "Alimentacion (Bebes 6-8m)",
                "Papilla",
                ("papilla", "espesa"),
                "Respuesta 6-8",
                "Disclaimer",
            ),
            motor.ReglaConocimiento(
                "Alimentacion (Bebes 9-11m)",
                "Papilla",
                ("papilla", "espesa"),
                "Respuesta 9-11",
                "Disclaimer",
            ),
        )

        para_siete = motor.buscar_mejor_regla(
            "papilla espesa",
            reglas,
            meses_bebe=7,
        )
        para_diez = motor.buscar_mejor_regla(
            "papilla espesa",
            reglas,
            meses_bebe=10,
        )
        edad_explicita = motor.buscar_mejor_regla(
            "papilla espesa 10 meses",
            reglas,
            meses_bebe=7,
        )
        para_rango = motor.buscar_mejor_regla(
            "papilla espesa",
            reglas,
            rango_edad_bebe="9-11",
        )

        self.assertIsNotNone(para_siete)
        self.assertIsNotNone(para_diez)
        self.assertIsNotNone(edad_explicita)
        self.assertIsNotNone(para_rango)
        assert para_siete and para_diez and edad_explicita and para_rango
        self.assertEqual(para_siete.categoria, "Alimentacion (Bebes 6-8m)")
        self.assertEqual(para_diez.categoria, "Alimentacion (Bebes 9-11m)")
        self.assertEqual(
            edad_explicita.categoria,
            "Alimentacion (Bebes 9-11m)",
        )
        self.assertEqual(
            para_rango.categoria,
            "Alimentacion (Bebes 9-11m)",
        )

    def test_alimentos_solo_bonifican_categoria_pertinente_con_evidencia(self) -> None:
        regla = motor.ReglaConocimiento(
            "Alimentacion (Bebes)",
            "Combinacion",
            ("receta", "hierro", "lentejas", "huevo", "carne"),
            "Respuesta",
            "Disclaimer",
        )
        reglas = (regla,)

        sin_alimentos = motor.buscar_mejor_regla("receta hierro", reglas)
        con_alimentos = motor.buscar_mejor_regla(
            "receta hierro",
            reglas,
            alimentos_contexto="lentejas, huevo y carne",
        )

        self.assertIsNotNone(sin_alimentos)
        self.assertIsNotNone(con_alimentos)
        assert sin_alimentos and con_alimentos
        self.assertGreater(con_alimentos.puntaje, sin_alimentos.puntaje)
        self.assertIsNone(
            motor.buscar_mejor_regla(
                "hola",
                reglas,
                alimentos_contexto="lentejas, huevo y carne",
            )
        )

    def test_detecta_emergencia_medica_y_social_sin_falsos_positivos(self) -> None:
        emergencia_medica = motor.buscar_mejor_regla(
            "mi bebe esta morado",
            self.reglas,
        )
        emergencia_social = motor.buscar_mejor_regla(
            "mi pareja me golpea",
            self.reglas,
        )

        self.assertIsNotNone(emergencia_medica)
        self.assertIsNotNone(emergencia_social)
        assert emergencia_medica and emergencia_social
        self.assertEqual(emergencia_medica.categoria, "Emergencia (Beb\u00e9)")
        self.assertEqual(emergencia_medica.motivo, "emergencia_prioritaria")
        self.assertEqual(
            emergencia_social.categoria,
            'L\u00edmite: "Emergencia" Social',
        )
        self.assertEqual(emergencia_social.motivo, "emergencia_prioritaria")

        for mensaje in ("fiebre", "mi bebe esta bien", "mi pareja"):
            with self.subTest(mensaje=mensaje):
                self.assertIsNone(
                    motor.buscar_mejor_regla(
                        mensaje,
                        self.reglas,
                        categoria_anterior="Emergencia (Beb\u00e9)",
                    )
                )


class ResultadoEstructuradoTests(unittest.TestCase):
    def test_resultado_incluye_evidencia_candidatos_y_aliases(self) -> None:
        reglas = (
            motor.ReglaConocimiento(
                "Categoria Uno",
                "Principal",
                ("clave", "extra", "detallada"),
                "Respuesta principal",
                "Disclaimer principal",
                "10",
                "documento.pdf",
                "https://example.test/documento",
            ),
            motor.ReglaConocimiento(
                "Categoria Dos",
                "Alterna",
                ("clave", "extra"),
                "Respuesta alterna",
                "Disclaimer alterno",
            ),
        )

        resultado = motor.buscar_mejor_regla(
            "clave extra detallada",
            reglas,
        )

        self.assertIsInstance(resultado, motor.ResultadoBusqueda)
        assert resultado is not None
        self.assertIsInstance(resultado.regla, motor.ReglaConocimiento)
        self.assertGreater(resultado.puntaje, 0)
        self.assertGreaterEqual(resultado.margen, motor.MARGEN_MINIMO)
        self.assertEqual(
            set(resultado.coincidencias_exactas),
            {"clave", "extra", "detallada"},
        )
        self.assertEqual(resultado.coincidencias_fuzzy, ())
        self.assertAlmostEqual(
            resultado.margen_porcentual,
            resultado.margen * 100,
        )
        self.assertEqual(resultado.categoria, "Categoria Uno")
        self.assertEqual(resultado.paginas, "10")
        self.assertEqual(resultado.documento, "documento.pdf")
        self.assertEqual(
            resultado.enlace,
            "https://example.test/documento",
        )
        self.assertEqual(len(resultado.candidatos_descartados), 1)
        descartado = resultado.candidatos_descartados[0]
        self.assertIsInstance(descartado, motor.CandidatoBusqueda)
        self.assertEqual(descartado.categoria, "Categoria Dos")
        self.assertEqual(
            set(descartado.coincidencias_exactas),
            {"clave", "extra"},
        )

    def test_construir_respuesta_preserva_texto_y_disclaimer(self) -> None:
        regla = motor.ReglaConocimiento(
            "Categoria",
            "Subcategoria",
            ("clave", "detalle"),
            "  Respuesta revisada.  ",
            "  Disclaimer obligatorio.  ",
        )
        resultado = motor.buscar_mejor_regla("clave detalle", (regla,))

        self.assertIsNotNone(resultado)
        assert resultado is not None
        esperado = "Respuesta revisada.\n\nDisclaimer obligatorio."
        self.assertEqual(motor.construir_respuesta(regla), esperado)
        self.assertEqual(motor.construir_respuesta(resultado), esperado)


class RecetasYPreferenciasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reglas = motor.cargar_motor_conocimientos()
        cls.perfiles = motor.obtener_perfiles_recetas(cls.reglas)

    def test_ids_e_inventario_provienen_de_recetas_revisadas(self) -> None:
        self.assertEqual(self.reglas[0].id_regla, "ANMI-0001")
        self.assertEqual(len({regla.id_regla for regla in self.reglas}), 412)
        self.assertEqual(len(self.perfiles), 14)
        self.assertEqual(
            [grupo for grupo, _ in motor.obtener_grupos_alimentos(self.perfiles)],
            [motor.GRUPO_BLANDOS, motor.GRUPO_HIERRO, motor.GRUPO_OTROS],
        )
        inventario = {
            alimento
            for _, alimentos in motor.obtener_grupos_alimentos(self.perfiles)
            for alimento in alimentos
        }
        self.assertTrue({"papa", "hígado", "huevo"} <= inventario)

    def test_extrae_gustos_rechazos_exclusiones_y_frases_generales(self) -> None:
        perfil = motor.extraer_perfil_alimentario(
            "Come papa, arroz y huevo; no le gusta el hígado ni la "
            "sangrecita; es alérgico al pescado."
        )
        self.assertEqual(set(perfil.aceptados), {"papa", "arroz", "huevo"})
        self.assertEqual(set(perfil.rechazados), {"higado", "sangrecita"})
        self.assertEqual(perfil.excluidos, ("pescado",))
        self.assertTrue(
            motor.extraer_perfil_alimentario("come de todo").aceptados
        )
        self.assertFalse(
            motor.extraer_perfil_alimentario("solo toma agua").reconocido
        )

    def test_ranking_respeta_edad_y_elimina_alergias(self) -> None:
        recetas = motor.filtrar_recetas_por_edad(
            self.perfiles,
            rango_edad_bebe="12-23",
        )
        resultado = motor.rankear_recetas(
            recetas,
            motor.extraer_perfil_alimentario(
                "come huevo, bazo, harina y espinaca"
            ),
        )
        self.assertIsNotNone(resultado)
        assert resultado is not None
        self.assertEqual(resultado.regla.id_regla, "ANMI-0168")

        sin_huevo = motor.rankear_recetas(
            recetas,
            motor.extraer_perfil_alimentario(
                "es alérgico al huevo; come sangrecita y trigo"
            ),
        )
        self.assertIsNotNone(sin_huevo)
        assert sin_huevo is not None
        self.assertNotIn("huevo", sin_huevo.perfil_receta.ingredientes)
        self.assertEqual(
            motor.filtrar_recetas_por_edad(self.perfiles, meses_bebe=30),
            (),
        )


if __name__ == "__main__":
    unittest.main()
