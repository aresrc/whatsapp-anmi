# ANMI WhatsApp Bot

ANMI es un asistente nutricional materno-infantil que usa Gemini para elegir
una respuesta revisada. El modelo solo devuelve un identificador del catálogo
`data/catalogo_clasificacion.csv`; el texto médico, disclaimer y fuentes se
obtienen localmente de `data/motor_conocimientos_v2.csv`. El motor anterior de
palabras permanece disponible como respaldo cuando Google no está configurado
o no puede clasificar.

## Preparación

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Para WhatsApp configura en `.env`:

```dotenv
VERIFY_TOKEN=...
WHATSAPP_TOKEN=...
PHONE_NUMBER_ID=...
GRAPH_API_VERSION=...
ANMI_USER_HASH_SECRET=...
API_GOOGLE=...
PORT=5000
```

`ANMI_USER_HASH_SECRET` debe ser un secreto estable de al menos 32 caracteres.
Se usa únicamente para generar una huella HMAC irreversible que permite mostrar
la presentación inicial una sola vez por usuario y canal. Si cambia, los
usuarios volverán a ser considerados nuevos. Puede generarse con:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

La integración usa el modelo fijo `gemini-3.1-flash-lite`. Si `API_GOOGLE`
está vacío, la aplicación inicia normalmente con el clasificador local.

La aplicación de producción se inicia con:

```powershell
python app.py
```

## Simulador local

El simulador usa el mismo motor y servicio conversacional, pero una base
separada y ninguna llamada a Meta:

```powershell
python simulador.py --usar-puerto-prueba true
```

La interfaz queda disponible en `http://127.0.0.1:5002`. Para usar el valor
`PORT` del `.env` en lugar de 5002:

```powershell
python simulador.py --usar-puerto-prueba false
```

El flujo solicita la edad mediante botones para 6–8, 9–11 y 12–23 meses, que
coinciden con las recetas revisadas del CSV. También acepta una edad exacta
escrita entre 6 y 36 meses o `no aplica`. Después muestra, mediante bullet
points, alimentos blandos o bases, alimentos ricos en hierro y otros
ingredientes presentes en las recetas. La persona cuidadora puede indicar qué
come el bebé, qué rechaza y qué no puede consumir por alergia o intolerancia.

Gemini elige una receta únicamente entre los IDs compatibles con la edad y las
exclusiones. Si Google falla o no devuelve una selección válida, un ranking
local premia ingredientes aceptados y penaliza los rechazados. Las alergias e
intolerancias siempre excluyen la receta; los ingredientes que simplemente no
gustan se muestran en una advertencia y nunca se inventan sustituciones.

Después de la recomendación se presenta una lista paginada de categorías y,
luego, de subcategorías de toda la base. Los menús respetan el límite de diez
filas de WhatsApp. En cualquier nivel se puede escribir una consulta libre, que
vuelve a pasar por Gemini y por el fallback local. Al escribir `fin`, incluso
durante un menú, se solicita una calificación del 1 al 5. Después se pregunta
mediante botones `Sí` y `No` si la persona desea dejar un comentario opcional
de hasta 1000 caracteres antes de cerrar.

## Clasificación y caché de Gemini

Los dos CSV se enlazan mediante IDs estables con formato `ANMI-0001`. Gemini
recibe únicamente el catálogo de ID, categoría y subcategoría, además del
mensaje actual y un contexto breve de la sesión. Nunca recibe las respuestas
médicas ni puede redactar el contenido final.

Durante la elección inicial de receta, el contexto añade la lista explícita de
IDs permitidos y los ingredientes locales asociados a esos IDs. Toda selección
se valida otra vez en el proceso antes de recuperar el texto revisado.

El catálogo estático se intenta conservar durante 24 horas en una caché
explícita compartida. Cuando la cuenta de Google no admite esa modalidad, se
usa un prefijo estable para aprovechar la caché implícita. Las métricas de
tokens y el modo de caché quedan en la evidencia de clasificación, sin guardar
la clave ni el prompt.

Para probar de forma optativa una clave real y observar las métricas:

```powershell
python -m clasificador_google --verificar-cache
```

## Persistencia y privacidad

`schema.sql` crea dos grupos de datos:

- `conversaciones_activas` y `mensajes_activos` contienen temporalmente el
  identificador y el transcript necesarios para continuar el diálogo.
- `consultas_finalizadas` y `categorias_consulta` conservan solo la fecha de
  cierre en hora de Lima, edad exacta o rango, calificación y categorías
  respondidas, además del comentario opcional sin vínculo al usuario.
- `usuarios_conocidos` conserva únicamente una huella HMAC de 64 caracteres y
  la fecha del primer contacto. No guarda el teléfono ni la cookie original.

Al confirmar la entrega del agradecimiento, el transcript y el identificador
temporal se eliminan en la misma transacción. La huella HMAC permanece para no
repetir la presentación beta. Las sesiones abandonadas se eliminan después de
24 horas de inactividad. Las bases se crean en `instance/` y están excluidas de
Git.

## Pruebas

```powershell
python -m unittest discover -s tests -v
python -m compileall app.py clasificador_google.py motor_conocimientos.py conversaciones.py servicio_conversacion.py simulador.py
```
