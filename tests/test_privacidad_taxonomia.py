from __future__ import annotations

import unittest
from collections import defaultdict

from motor_conocimientos import cargar_motor_conocimientos
from privacidad import redactar_datos_personales
from taxonomia import (
    SECCIONES,
    candidatas_para_texto,
    jerarquia_de_regla,
    seccion_de_regla,
    titulo_visible,
)


class PrivacidadTests(unittest.TestCase):
    def test_redacta_pii_y_conserva_dato_nutricional(self) -> None:
        resultado = redactar_datos_personales(
            "Me llamo Ana Perez, mi correo es ana@example.com y mi bebé tiene 8 meses"
        )
        self.assertIn("[NOMBRE]", resultado.texto)
        self.assertIn("[CORREO]", resultado.texto)
        self.assertIn("8 meses", resultado.texto)
        self.assertEqual(
            redactar_datos_personales("soy una gestante de 20 años").texto,
            "soy una gestante de 20 años",
        )


class TaxonomiaTests(unittest.TestCase):
    def test_toda_regla_tiene_una_seccion_y_recuperacion_acotada(self) -> None:
        reglas = cargar_motor_conocimientos()
        self.assertTrue(all(seccion_de_regla(regla) in SECCIONES for regla in reglas))
        self.assertLessEqual(len(candidatas_para_texto(reglas, "anemia bebé")), 8)

    def test_alimentacion_tiene_tres_niveles_y_titulos_no_repetidos(self) -> None:
        reglas = [
            regla
            for regla in cargar_motor_conocimientos()
            if seccion_de_regla(regla) == "Alimentación y hierro"
        ]
        self.assertEqual(len(reglas), 183)
        claves = defaultdict(list)
        for regla in reglas:
            jerarquia = jerarquia_de_regla(regla)
            self.assertLessEqual(len(jerarquia.niveles), 3)
            self.assertEqual(jerarquia.principal, "Alimentación y hierro")
            claves[(jerarquia.niveles, titulo_visible(regla))].append(regla.id_regla)
        repetidas = {
            clave: ids for clave, ids in claves.items() if len(ids) > 1
        }
        self.assertEqual(repetidas, {})


if __name__ == "__main__":
    unittest.main()
