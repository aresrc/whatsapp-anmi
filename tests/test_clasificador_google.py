from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from clasificador_google import (
    CatalogoClasificacion,
    ClasificadorGoogle,
    ContextoClasificacion,
    ErrorClasificacionGoogle,
    ID_SIN_COINCIDENCIA,
    MODELO_GOOGLE,
    RUTA_CATALOGO_PREDETERMINADA,
    cargar_catalogo_clasificacion,
    crear_clasificador_google,
)
from motor_conocimientos import (
    RUTA_CSV,
    ReglaConocimiento,
    cargar_motor_conocimientos,
)


def crear_regla() -> ReglaConocimiento:
    return ReglaConocimiento(
        categoria="Definición y Síntomas",
        subcategoria="¿Qué es la Anemia?",
        palabras_clave=("anemia",),
        respuesta="Respuesta revisada",
        disclaimer="Disclaimer revisado",
        paginas="6",
        documento="guia.pdf",
        enlace="https://example.test/guia",
    )


def crear_catalogo() -> CatalogoClasificacion:
    regla = crear_regla()
    return CatalogoClasificacion(
        reglas_por_id={"ANMI-0001": regla},
        texto_para_modelo=(
            "ID,Categoría,Subcategoría\n"
            "ANMI-0001,Definición y Síntomas,¿Qué es la Anemia?\n"
        ),
        huella="abc123",
        cantidad=1,
    )


class CachesFalsas:
    def __init__(self, *, fallar: bool = False) -> None:
        self.fallar = fallar
        self.existentes: list[object] = []
        self.creaciones: list[object] = []
        self.actualizaciones: list[object] = []

    def list(self):
        return self.existentes

    def create(self, *, model: str, config: object):
        self.creaciones.append((model, config))
        if self.fallar:
            raise PermissionError("caché no disponible")
        return SimpleNamespace(
            name="cachedContents/anmi",
            display_name=getattr(config, "display_name"),
            expire_time=datetime.now(UTC) + timedelta(hours=24),
        )

    def update(self, *, name: str, config: object):
        self.actualizaciones.append((name, config))
        return SimpleNamespace(
            name=name,
            display_name="anmi",
            expire_time=datetime.now(UTC) + timedelta(hours=24),
        )


class ModelosFalsos:
    def __init__(self, ids: tuple[str, ...]) -> None:
        self.ids = list(ids)
        self.llamadas: list[dict[str, object]] = []

    def generate_content(self, *, model: str, contents: str, config: object):
        self.llamadas.append(
            {"model": model, "contents": contents, "config": config}
        )
        id_regla = self.ids.pop(0)
        return SimpleNamespace(
            parsed={"id_regla": id_regla},
            usage_metadata=SimpleNamespace(
                prompt_token_count=120,
                cached_content_token_count=100,
                candidates_token_count=5,
                total_token_count=125,
            ),
        )


class ClienteFalso:
    def __init__(
        self,
        ids: tuple[str, ...],
        *,
        fallar_cache: bool = False,
    ) -> None:
        self.caches = CachesFalsas(fallar=fallar_cache)
        self.models = ModelosFalsos(ids)


class CatalogoRealTest(unittest.TestCase):
    def test_catalogo_real_enlaza_412_ids_y_preserva_bom(self) -> None:
        reglas = cargar_motor_conocimientos()
        catalogo = cargar_catalogo_clasificacion(reglas)

        self.assertEqual(catalogo.cantidad, 412)
        self.assertEqual(
            list(catalogo.reglas_por_id)[:2],
            ["ANMI-0001", "ANMI-0002"],
        )
        self.assertEqual(Path(RUTA_CSV).read_bytes()[:3], b"\xef\xbb\xbf")
        self.assertEqual(
            Path(RUTA_CATALOGO_PREDETERMINADA).read_bytes()[:3],
            b"\xef\xbb\xbf",
        )
        with Path(RUTA_CATALOGO_PREDETERMINADA).open(
            encoding="utf-8-sig",
            newline="",
        ) as archivo:
            lector = csv.DictReader(archivo)
            self.assertEqual(
                list(lector.fieldnames or ()),
                ["ID", "Categoría", "Subcategoría"],
            )
            self.assertEqual(len(tuple(lector)), 412)

    def test_catalogo_rechaza_id_inconsistente(self) -> None:
        with tempfile.TemporaryDirectory() as temporal:
            directorio = Path(temporal)
            principal = directorio / "principal.csv"
            catalogo = directorio / "catalogo.csv"
            principal.write_text(
                "ID,Categoría,Subcategoría\n"
                "ANMI-0001,Definición y Síntomas,¿Qué es la Anemia?\n",
                encoding="utf-8-sig",
            )
            catalogo.write_text(
                "ID,Categoría,Subcategoría\n"
                "ANMI-9999,Definición y Síntomas,¿Qué es la Anemia?\n",
                encoding="utf-8-sig",
            )

            with self.assertRaisesRegex(ValueError, "inexistente"):
                cargar_catalogo_clasificacion(
                    (crear_regla(),),
                    ruta_principal=principal,
                    ruta_catalogo=catalogo,
                )


class ClasificadorGoogleTest(unittest.TestCase):
    def test_cache_explicita_se_crea_una_vez_y_se_reutiliza(self) -> None:
        cliente = ClienteFalso(("ANMI-0001", "ANMI-0001"))
        clasificador = ClasificadorGoogle(
            "clave-prueba",
            crear_catalogo(),
            cliente=cliente,
        )
        contexto = ContextoClasificacion(
            meses_bebe=8,
            alimentos_contexto="lentejas",
            consultas_anteriores=("consulta anterior",),
        )

        primera = clasificador.seleccionar("¿Qué es la anemia?", contexto)
        segunda = clasificador.seleccionar("¿Y sus síntomas?", contexto)

        self.assertIsNotNone(primera)
        self.assertIsNotNone(segunda)
        self.assertEqual(len(cliente.caches.creaciones), 1)
        self.assertEqual(len(cliente.models.llamadas), 2)
        for llamada in cliente.models.llamadas:
            self.assertEqual(llamada["model"], MODELO_GOOGLE)
            self.assertEqual(
                llamada["config"].cached_content,
                "cachedContents/anmi",
            )
            self.assertNotIn("CATÁLOGO DE IDS", llamada["contents"])
        assert primera is not None
        self.assertEqual(primera.evidencia["modo_cache"], "explicita")
        self.assertEqual(primera.evidencia["tokens_cacheados"], 100)

    def test_fallo_cache_explicita_usa_prefijo_implicito(self) -> None:
        cliente = ClienteFalso(
            ("ANMI-0001", "ANMI-0001"),
            fallar_cache=True,
        )
        clasificador = ClasificadorGoogle(
            "clave-prueba",
            crear_catalogo(),
            cliente=cliente,
        )

        primera = clasificador.seleccionar(
            "¿Qué es la anemia?",
            ContextoClasificacion(),
        )
        segunda = clasificador.seleccionar(
            "¿Qué síntomas tiene?",
            ContextoClasificacion(),
        )

        self.assertEqual(len(cliente.caches.creaciones), 1)
        self.assertEqual(len(cliente.models.llamadas), 2)
        self.assertTrue(
            all(
                "CATÁLOGO DE IDS" in llamada["contents"]
                for llamada in cliente.models.llamadas
            )
        )
        assert primera is not None and segunda is not None
        self.assertEqual(primera.evidencia["modo_cache"], "implicita")
        self.assertEqual(segunda.evidencia["modo_cache"], "implicita")

    def test_cache_vigente_de_otro_proceso_se_recupera_por_huella(self) -> None:
        cliente = ClienteFalso(("ANMI-0001",))
        clasificador = ClasificadorGoogle(
            "clave-prueba",
            crear_catalogo(),
            cliente=cliente,
        )
        cliente.caches.existentes.append(
            SimpleNamespace(
                name="cachedContents/existente",
                display_name=clasificador.nombre_visible_cache,
                expire_time=datetime.now(UTC) + timedelta(hours=12),
            )
        )

        seleccion = clasificador.seleccionar(
            "¿Qué es la anemia?",
            ContextoClasificacion(),
        )

        self.assertIsNotNone(seleccion)
        self.assertEqual(cliente.caches.creaciones, [])
        self.assertEqual(
            cliente.models.llamadas[0]["config"].cached_content,
            "cachedContents/existente",
        )

    def test_sin_coincidencia_e_id_invalido(self) -> None:
        cliente = ClienteFalso(
            (ID_SIN_COINCIDENCIA, "ANMI-9999")
        )
        clasificador = ClasificadorGoogle(
            "clave-prueba",
            crear_catalogo(),
            cliente=cliente,
        )

        self.assertIsNone(
            clasificador.seleccionar("texto ajeno", ContextoClasificacion())
        )
        with self.assertRaisesRegex(ErrorClasificacionGoogle, "desconocido"):
            clasificador.seleccionar(
                "texto inválido",
                ContextoClasificacion(),
            )

    def test_sin_api_google_no_intenta_cargar_catalogos(self) -> None:
        self.assertIsNone(
            crear_clasificador_google(
                (),
                api_key="  ",
                ruta_principal="inexistente.csv",
                ruta_catalogo="inexistente.csv",
            )
        )


if __name__ == "__main__":
    unittest.main()
