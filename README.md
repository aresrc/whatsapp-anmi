# ANMI WhatsApp Bot

ANMI es un asistente nutricional materno-infantil basado en reglas. Clasifica
mensajes con el contenido revisado de `data/motor_conocimientos_v2.csv`, lleva
el estado temporal de cada conversación en SQLite y conserva únicamente un
resumen anónimo cuando el usuario termina.

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
PORT=5000
```

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

El flujo solicita meses, alimentos cuando corresponde y atiende las consultas
en bloques separados de respuesta, disclaimer, documento, página, enlace y
recordatorio de cierre. Al escribir `fin`, solicita una calificación del 1 al 5
y responde con un agradecimiento antes de cerrar la conversación.

## Persistencia y privacidad

`schema.sql` crea dos grupos de datos:

- `conversaciones_activas` y `mensajes_activos` contienen temporalmente el
  identificador y el transcript necesarios para continuar el diálogo.
- `consultas_finalizadas` y `categorias_consulta` conservan solo la fecha de
  cierre en hora de Lima, meses, calificación y categorías respondidas.

Al confirmar la entrega del agradecimiento, el transcript y el identificador
se eliminan en la misma transacción. Las sesiones abandonadas se eliminan
después de 24 horas de inactividad. Las bases se crean en `instance/` y están
excluidas de Git.

## Pruebas

```powershell
python -m unittest discover -s tests -v
python -m compileall app.py motor_conocimientos.py conversaciones.py servicio_conversacion.py simulador.py
```
