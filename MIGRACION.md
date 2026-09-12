# Migración: contenido, Gemini y privacidad

## Objetivo

Reducir la navegación de 60 categorías internas a ocho secciones visibles y
usar Gemini únicamente como respaldo de clasificación y para una introducción
breve. Las respuestas médicas y los disclaimers siguen siendo los del CSV
revisado.

## Despliegue

1. Respaldar la base SQLite y los CSV. No modificar respuestas verificadas ni
   disclaimers durante la migración.
2. Configurar `ANMI_USER_HASH_SECRET` (32+ caracteres) y `API_GOOGLE` solo en
   el gestor de secretos. Nunca en CSV ni repositorio.
3. Desplegar esta versión y reiniciar el proceso. Los números de WhatsApp se
   convierten a HMAC antes de entrar al servicio; los textos se redáctan antes
   de persistirse o enviarse a Gemini.
4. Invalidar las conversaciones activas creadas por versiones anteriores:
   contienen transcripciones y el identificador temporal podía ser el número
   original. La retención normal es de 24 horas; para una limpieza inmediata,
   respaldar y borrar las sesiones activas mediante una operación controlada.
5. Revisar comentarios históricos antes de conservarlos: aplicar el mismo
   redactor o eliminarlos cuando contengan PII. No activar Gemini hasta cerrar
   este saneamiento.

## Taxonomía visible

El CSV mantiene sus columnas clínicas canónicas y añade `Ingredientes de la
receta` para las recetas MINSA. Contiene claves canónicas separadas por `|`;
el motor valida que sean ingredientes conocidos y aparezcan en la receta
revisada. La presentación se deriva de
`taxonomia.py`, evitando editar masivamente contenido revisado. Cada regla cae
en una sola sección:

- Anemia y señales
- Alimentación y hierro
- Suplementos
- Prevención y controles
- Tratamiento
- Embarazo y lactancia
- Recetas por edad
- Ayuda y seguridad

Los paréntesis de `Categoría` y `Subcategoría` son metadatos internos. El menú
usa el título visible sin esas aclaraciones y conserva el `ID` para seleccionar
la regla exacta. Cuando se complete una próxima curaduría del CSV, añadir las
columnas `Sección`, `Audiencia`, `Edad mínima (meses)`, `Edad máxima (meses)` y
`Título visible`; validar que reproduzcan exactamente este mapeo antes de hacer
que dejen de ser derivados.

### Alimentación en tres niveles

El menú organiza las 183 reglas alimentarias con la ruta `Alimentación y
hierro → categoría secundaria → categoría terciaria`. Por ejemplo: `Bebés →
6 a 8 meses`, `Alimentos con hierro → Origen animal` y `Absorción del hierro
→ Facilitadores`. Los títulos finales convierten los paréntesis en subtítulos
con raya (`Sangrecita — Preparación`), por lo que no se repiten dentro de la
misma ruta.

## Gemini y presupuesto

- Usar `gemini-3.1-flash-lite` ya configurado, `temperature=0.25`, salida JSON y
  máximo 80 tokens. Evaluar `gemini-2.5-flash-lite` mediante pruebas A/B si el
  costo es la prioridad.
- La búsqueda local se ejecuta primero. Gemini solo se invoca si no hay una
  regla local con evidencia suficiente, y siempre devuelve un `id_regla` que
  se valida contra el catálogo local.
- Gemini puede devolver `frase_inicial`; esta debe tener hasta 12 palabras y
  no puede incluir dosis, diagnóstico, receta ni instrucciones clínicas. Si
  falta o falla la validación, se usa una plantilla local.
- No habilitar Grounding/Search para este bot: el contenido médico procede de
  fuentes revisadas y el buscador añade gasto por consulta.
- Vigilar `tokens_prompt`, `tokens_salida`, `tokens_totales`, origen y fallos,
  pero nunca almacenar el texto original. Definir alertas de presupuesto en el
  proveedor antes de producción.

## Privacidad

`privacidad.py` aplica regex a correo, teléfono, documentos, tarjetas, URL,
direcciones y presentaciones de nombre. El filtro conserva datos necesarios
para nutrición, como meses y alimentos. spaCy puede añadirse como refuerzo para
entidades personales en una segunda fase, pero no sustituye estas reglas
deterministas. Probar falsos positivos con alimentos, ciudades y nombres de
recetas antes de habilitarlo.

## Verificación y rollback

Ejecutar `python -m unittest discover -s tests` y
`python -m compileall app.py motor_conocimientos.py servicio_conversacion.py`.
Comprobar que un texto con nombre, teléfono o correo llegue redactado a SQLite
y que una consulta médica conserve la respuesta y disclaimer exactos. Para
rollback, desactivar `API_GOOGLE`: el motor local y las frases de respaldo
continúan operando sin llamadas remotas.
