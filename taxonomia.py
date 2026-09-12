"""Taxonomía de presentación; las etiquetas históricas del CSV no son UI."""

from __future__ import annotations

from dataclasses import dataclass
import re

from motor_conocimientos import ReglaConocimiento, normalizar_texto


SECCIONES = (
    "Anemia y señales",
    "Alimentación y hierro",
    "Suplementos",
    "Prevención y controles",
    "Tratamiento",
    "Embarazo y lactancia",
    "Recetas por edad",
    "Ayuda y seguridad",
)


@dataclass(frozen=True)
class JerarquiaCategoria:
    """Ruta visible de hasta tres niveles para el menú."""

    principal: str
    secundaria: str | None = None
    terciaria: str | None = None

    @property
    def niveles(self) -> tuple[str, ...]:
        return tuple(
            nivel
            for nivel in (self.principal, self.secundaria, self.terciaria)
            if nivel
        )


def jerarquia_de_regla(regla: ReglaConocimiento) -> JerarquiaCategoria:
    """Clasifica alimentación en tema, público y subtema sin usar paréntesis."""
    categoria = normalizar_texto(regla.categoria)
    titulo = normalizar_texto(regla.subcategoria)
    principal = seccion_de_regla(regla)
    if principal != "Alimentación y hierro":
        return JerarquiaCategoria(principal, "Temas")
    if "bebes 6 8" in categoria:
        return JerarquiaCategoria(principal, "Bebés", "6 a 8 meses")
    if "bebes 9 11" in categoria:
        return JerarquiaCategoria(principal, "Bebés", "9 a 11 meses")
    if "bebes 12 23" in categoria:
        return JerarquiaCategoria(principal, "Bebés", "12 a 23 meses")
    if "alimentacion bebes" in categoria:
        if "receta" in titulo or "preparacion" in titulo:
            terciaria = "Preparación y recetas"
        elif any(palabra in titulo for palabra in ("cantidad", "cuchara", "plato")):
            terciaria = "Cantidades y porciones"
        elif any(palabra in titulo for palabra in ("hierro", "suplemento", "gotas")):
            terciaria = "Hierro y suplementos"
        else:
            terciaria = "Inicio y hábitos"
        return JerarquiaCategoria(principal, "Bebés", terciaria)
    if "gestantes" in categoria:
        if "evitar" in titulo or "precaucion" in titulo:
            terciaria = "Qué evitar"
        elif "postparto" in titulo or "hito" in titulo:
            terciaria = "Postparto y controles"
        else:
            terciaria = "Plan de alimentación"
        return JerarquiaCategoria(principal, "Gestantes", terciaria)
    if "madre lactante" in categoria:
        return JerarquiaCategoria(principal, "Madre lactante", "Plan de alimentación")
    if "hierro animal" in categoria:
        return JerarquiaCategoria(principal, "Alimentos con hierro", "Origen animal")
    if "hierro vegetal" in categoria:
        return JerarquiaCategoria(principal, "Alimentos con hierro", "Origen vegetal")
    if "absorcion" in categoria:
        terciaria = "Facilitadores" if "facilitadores" in categoria else "Inhibidores"
        return JerarquiaCategoria(principal, "Absorción del hierro", terciaria)
    if "mitos" in categoria:
        return JerarquiaCategoria(principal, "Mitos de alimentación", "Creencias frecuentes")
    if "general" in categoria:
        return JerarquiaCategoria(principal, "Alimentación general", "Hábitos y nutrientes")
    return JerarquiaCategoria(principal, "Otros temas de alimentación")


def seccion_de_regla(regla: ReglaConocimiento) -> str:
    categoria = normalizar_texto(regla.categoria)
    if "recetas minsa" in categoria:
        return "Recetas por edad"
    if "limite" in categoria or "emergencia" in categoria:
        return "Ayuda y seguridad"
    if any(
        termino in categoria
        for termino in ("alimentacion", "alimentos", "absorcion")
    ):
        return "Alimentación y hierro"
    if "tratamiento" in categoria:
        return "Tratamiento"
    if "suplementacion" in categoria or "micronutrientes" in categoria:
        return "Suplementos"
    if "gestante" in categoria or "lactante" in categoria or "mujer edad fertil" in categoria:
        return "Embarazo y lactancia"
    if "prevencion" in categoria or "controles" in categoria:
        return "Prevención y controles"
    return "Anemia y señales"


def titulo_visible(regla: ReglaConocimiento) -> str:
    """Convierte aclaraciones entre paréntesis en un subtítulo no redundante."""
    titulo = regla.subcategoria.strip()
    titulo = titulo.replace("¿", "").replace("?", "")
    titulo = re.sub(r"\s*\(([^)]+)\)", r" — \1", titulo)
    titulo = re.sub(r"\s+", " ", titulo).strip(" —")
    return titulo or "Información revisada"


def candidatas_para_texto(
    reglas: tuple[ReglaConocimiento, ...], texto: str, limite: int = 8
) -> tuple[str, ...]:
    """Recuperación local barata para no exponer el catálogo entero al modelo."""
    tokens = set(normalizar_texto(texto).split())
    puntuadas = []
    for regla in reglas:
        referencia = normalizar_texto(
            " ".join((regla.categoria, regla.subcategoria, *regla.palabras_clave))
        )
        puntaje = len(tokens & set(referencia.split()))
        if puntaje:
            puntuadas.append((puntaje, regla.id_regla))
    puntuadas.sort(key=lambda item: (-item[0], item[1]))
    return tuple(id_regla for _, id_regla in puntuadas[:limite])
