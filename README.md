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
API_GOOGLE=...
PORT=5000
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

El flujo solicita la edad mediante botones para 6–12, 12–24 y 24–36 meses, tanto
en WhatsApp como en el simulador. También acepta una edad exacta escrita entre
6 y 36 meses o `no aplica`. Si el usuario precisa después la edad exacta, esta
reemplaza al rango elegido y se conserva como contexto. Luego solicita alimentos
cuando corresponde y
atiende las consultas en bloques separados de respuesta, disclaimer, documento,
página, enlace y recordatorio de cierre. Al escribir `fin`, solicita una
calificación del 1 al 5 y responde con un agradecimiento antes de cerrar la
conversación.

Las solicitudes de recetas se resuelven con una receta MINSA revisada según la
edad exacta. Si solo se eligió una franja amplia, ANMI solicita los meses antes
de recomendar; para edades sin cobertura no reutiliza contenido de otro rango.

## Clasificación y caché de Gemini

Los dos CSV se enlazan mediante IDs estables con formato `ANMI-0001`. Gemini
recibe únicamente el catálogo de ID, categoría y subcategoría, además del
mensaje actual y un contexto breve de la sesión. Nunca recibe las respuestas
médicas ni puede redactar el contenido final.

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
  respondidas.

Al confirmar la entrega del agradecimiento, el transcript y el identificador
se eliminan en la misma transacción. Las sesiones abandonadas se eliminan
después de 24 horas de inactividad. Las bases se crean en `instance/` y están
excluidas de Git.

## Pruebas

```powershell
python -m unittest discover -s tests -v
python -m compileall app.py clasificador_google.py motor_conocimientos.py conversaciones.py servicio_conversacion.py simulador.py
```
