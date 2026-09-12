# Documentación del proyecto ANMI WhatsApp Bot

## 1. Resumen ejecutivo

ANMI es un asistente conversacional de nutrición materno-infantil que atiende consultas mediante WhatsApp y dispone de un simulador web para pruebas locales. Su objetivo es entregar contenido previamente revisado desde una base de conocimientos, manteniendo el modelo de inteligencia artificial fuera de la redacción de respuestas médicas.

La aplicación combina dos mecanismos de selección:

1. **Clasificación semántica con Gemini:** el modelo recibe un catálogo limitado a identificadores, categorías y subcategorías, y devuelve únicamente el ID de la regla que considera pertinente.
2. **Motor local de reglas:** actúa como respaldo cuando Gemini no está configurado, falla o no entrega una selección válida. Normaliza el texto, tolera errores tipográficos y puntúa las coincidencias con las palabras clave.

En ambos casos, la respuesta, las fuentes y el aviso obligatorio se recuperan localmente desde `data/motor_conocimientos_v2.csv`. De esta manera, el contenido sanitario mostrado al usuario no es generado libremente por el modelo.

## 2. Objetivos y alcance

El proyecto permite:

- Recibir y verificar webhooks de Meta WhatsApp Cloud API.
- Mantener una conversación con estado por usuario y canal.
- Solicitar la edad del bebé y su perfil alimentario.
- Recomendar recetas compatibles con la edad y las exclusiones alimentarias.
- Resolver consultas libres mediante un catálogo de respuestas revisadas.
- Navegar la base por categorías y subcategorías con listas interactivas.
- Recoger una calificación y un comentario opcional al cerrar la sesión.
- Conservar únicamente un resumen anónimo cuando finaliza la conversación.
- Probar el flujo completo en una interfaz web sin enviar mensajes a Meta.

ANMI es una herramienta informativa. Los avisos médicos obligatorios almacenados en la base de conocimientos forman parte de cada respuesta y no deben eliminarse ni debilitarse.

## 3. Arquitectura general

```text
Usuario de WhatsApp                  Usuario del simulador
        |                                      |
        v                                      v
   Meta Cloud API                         simulador.py
        |                                      |
        +--------------+  +--------------------+
                       v  v
              servicio_conversacion.py
                  |             |
                  v             v
       clasificador_google.py   motor_conocimientos.py
                  |             |
                  +------+------+ 
                         |
                         v
          data/motor_conocimientos_v2.csv
                         |
                         v
              conversaciones.py
                         |
                         v
                SQLite / instance/
```

`app.py` y `simulador.py` son adaptadores de entrada. La lógica de negocio reside en `ServicioConversacion`, por lo que ambos canales comparten las mismas validaciones, estados y reglas de respuesta.

## 4. Estructura del repositorio

| Ruta | Responsabilidad |
|---|---|
| `app.py` | Aplicación Flask de producción, endpoints del webhook, extracción de eventos y envío a WhatsApp Cloud API. |
| `servicio_conversacion.py` | Máquina de estados, menús, consultas, recetas, cierre y coordinación entre clasificación y persistencia. |
| `motor_conocimientos.py` | Carga y validación del CSV, normalización, búsqueda local, corrección aproximada y selección de recetas. |
| `clasificador_google.py` | Clasificación con Gemini, validación del catálogo, contexto mínimo y gestión de caché. |
| `conversaciones.py` | Repositorio SQLite, migraciones, deduplicación, retención y anonimización al finalizar. |
| `simulador.py` | Aplicación Flask local para probar el servicio sin conectarse a Meta. |
| `templates/simulador.html` | Interfaz del simulador y visualización de datos de depuración. |
| `schema.sql` | Esquema relacional, restricciones e índices de SQLite. |
| `data/motor_conocimientos_v2.csv` | Base activa de conocimiento revisado, con 412 reglas al momento de redactar este documento. |
| `data/catalogo_clasificacion.csv` | Vista reducida de 412 IDs, categorías y subcategorías que puede recibir Gemini. |
| `data/motor_conocimientos.csv` | Base anterior conservada por compatibilidad o referencia; no es la fuente activa. |
| `tests/` | Pruebas unitarias e integradas de clasificación, persistencia, motor, conversación y adaptadores web. |
| `requirements.txt` | Dependencias de ejecución. |

## 5. Flujo funcional de una conversación

### 5.1 Inicio y perfil

1. El usuario envía un saludo o una primera consulta.
2. El sistema crea una conversación temporal y registra una huella HMAC para saber si corresponde mostrar la presentación beta.
3. Se solicita la edad mediante botones para los rangos **6–8**, **9–11** y **12–23 meses**. También se admite una edad exacta de 6 a 36 meses o la respuesta `no aplica`.
4. Cuando existe una edad compatible, se solicita el perfil alimentario: alimentos que el bebé consume, rechaza o no puede consumir por alergia o intolerancia.
5. El servicio intenta ofrecer una receta segura para la edad y el perfil indicado.

### 5.2 Recomendación de recetas

Las recetas se filtran primero por edad. Luego se excluyen aquellas que contienen un ingrediente declarado como alergia o intolerancia. Gemini, si está disponible, solo puede seleccionar entre los IDs previamente permitidos por este filtro.

Si la clasificación remota falla, el ranking local premia ingredientes aceptados y penaliza ingredientes rechazados. Un alimento que simplemente no gusta genera una advertencia; una exclusión médica impide seleccionar la receta. El sistema no inventa sustituciones.

### 5.3 Consultas y menús

Después de la recomendación se presenta un menú paginado de categorías y subcategorías. Las páginas respetan el máximo de diez filas admitido por las listas interactivas de WhatsApp: hasta ocho categorías o siete subcategorías, reservando espacio para controles de navegación.

El usuario también puede escribir una consulta libre en cualquier nivel. El servicio utiliza la edad, el perfil alimentario y un contexto conversacional breve para mejorar la selección, sin permitir que ese contexto cree por sí solo evidencia médica.

### 5.4 Cierre

El comando `fin` inicia el cierre desde cualquier estado ordinario:

1. Se solicita una calificación del 1 al 5.
2. Se pregunta si el usuario desea dejar un comentario.
3. El comentario es opcional y admite hasta 1000 caracteres.
4. Solo después de confirmar la entrega del agradecimiento se compacta la información y se elimina la conversación activa.

## 6. Estados conversacionales

| Estado | Propósito |
|---|---|
| `esperando_meses` | Espera una edad exacta, un rango ofrecido o `no aplica`. |
| `esperando_alimentos` | Recoge alimentos aceptados, rechazados y excluidos. |
| `esperando_edad_receta` | Solicita la edad cuando una petición de receta no cuenta con ella. |
| `menu_general` | Muestra categorías de la base de conocimientos. |
| `menu_especifico` | Muestra las subcategorías de una categoría elegida. |
| `lista` | Alias persistido por compatibilidad con versiones anteriores. |
| `esperando_calificacion` | Valida una calificación entre 1 y 5. |
| `esperando_decision_comentario` | Espera la elección de dejar o no un comentario. |
| `esperando_comentario` | Recibe el comentario final opcional. |

## 7. Selección y construcción de respuestas

### 7.1 Motor local

El motor local:

- Convierte el texto a minúsculas, elimina tildes y puntuación, y normaliza espacios.
- Aplica algunos alias frecuentes y separa tokens sin duplicados.
- Corrige de forma aproximada ciertos errores tipográficos mediante distancia de Levenshtein.
- Calcula relevancia según frecuencia inversa de términos, cobertura, etiquetas, contexto y edad.
- Prioriza reglas de emergencia cuando hay señales suficientes.
- Exige un puntaje mínimo y un margen frente al segundo candidato para evitar respuestas ambiguas.

Una vez seleccionada una regla, `construir_respuesta` concatena literalmente la respuesta revisada y su disclaimer obligatorio.

### 7.2 Clasificador Gemini

El modelo configurado en el código es `gemini-3.1-flash-lite`. Su salida usa JSON estructurado y contiene un único `id_regla`. La temperatura y el presupuesto de razonamiento se mantienen en cero para reducir variabilidad y costo.

Controles relevantes:

- Gemini no recibe el texto de las respuestas médicas.
- Todo ID devuelto debe existir en el catálogo local.
- Cuando hay una lista de IDs permitidos, cualquier ID fuera de ella se rechaza.
- Los valores aportados por el usuario se tratan como datos y no como instrucciones.
- Ante un error de red, formato o validación se utiliza el motor local.
- El catálogo puede conservarse en una caché explícita durante 24 horas; si esa función no está disponible se usa un prefijo estable para favorecer la caché implícita.
- La evidencia registra origen, modelo y métricas de tokens, pero no guarda la clave ni el prompt completo.

## 8. Base de conocimientos

La fuente activa es `data/motor_conocimientos_v2.csv`, codificada como **UTF-8 con BOM**. Sus columnas obligatorias son:

| Columna | Uso |
|---|---|
| `ID` | Identificador estable con formato `ANMI-0000`. |
| `Categoría` | Agrupación temática principal. |
| `Subcategoría` | Intención o tema específico. |
| `Palabras Clave (Para el bot)` | Términos separados por comas para la selección local. |
| `Respuesta Verificada (Lenguaje sencillo para el chat)` | Contenido que se entrega al usuario. |
| `Disclaimer Obligatorio (¡Crítico!)` | Aviso revisado que acompaña la respuesta. |
| `Paginas` | Referencia de páginas de la fuente. |
| `Documento` | Nombre de la fuente documental. |
| `Enlace` | URL de consulta de la fuente. |

El archivo `catalogo_clasificacion.csv` debe contener exactamente `ID`, `Categoría` y `Subcategoría`. Sus filas deben coincidir uno a uno con el CSV principal. Al iniciar con Gemini se verifican IDs vacíos o duplicados, formato, cantidad e igualdad de categorías.

Para añadir o modificar conocimiento:

1. Conservar los encabezados y la codificación UTF-8 con BOM.
2. Mantener un ID único con el formato exigido.
3. Actualizar la fila correspondiente del catálogo reducido.
4. No reescribir silenciosamente contenido sanitario revisado ni su disclaimer.
5. Ejecutar las pruebas del motor y del clasificador antes de desplegar.

## 9. API y adaptadores

### 9.1 Aplicación de WhatsApp

| Método y ruta | Función |
|---|---|
| `GET /` | Comprobación básica de estado; informa número de reglas y modo de clasificación. |
| `GET /webhook` | Verifica la suscripción de Meta comparando `hub.verify_token`. |
| `POST /webhook` | Recibe eventos, extrae mensajes de texto o respuestas interactivas y devuelve el resultado del procesamiento. |

Respuesta normal del webhook:

```json
{
  "estado": "recibido",
  "mensajes": 1,
  "errores_envio": 0
}
```

Los eventos sin mensajes procesables se aceptan con cero mensajes. Un cuerpo que no sea un objeto JSON devuelve HTTP 400. Los IDs externos cuentan con una restricción única para ignorar reintentos duplicados de Meta.

Las respuestas salientes pueden ser texto, botones o listas. Antes de enviarlas se respetan los límites relevantes de la API: identificadores, títulos, descripciones, etiqueta de lista y número máximo de opciones.

### 9.2 Simulador local

| Método y ruta | Función |
|---|---|
| `GET /` | Muestra la conversación y crea una cookie temporal si es necesario. |
| `POST /mensaje` | Procesa un mensaje de hasta 4096 caracteres con el mismo servicio que WhatsApp. |
| `POST /reiniciar` | Borra la conversación del simulador y elimina su cookie. |
| `GET /salud` | Informa estado, modo, cantidad de reglas y clasificador activo. |

El simulador utiliza `instance/anmi_simulador.sqlite3`, separado de la base de producción, y no invoca la API de Meta.

## 10. Persistencia, privacidad y retención

SQLite funciona con claves foráneas, modo WAL y un tiempo de espera de cinco segundos. La información se divide en datos temporales y resúmenes finales:

| Tabla | Contenido y ciclo de vida |
|---|---|
| `conversaciones_activas` | Usuario temporal, estado, edad, perfil alimentario, menú y datos pendientes de cierre. Se elimina al finalizar o caducar. |
| `mensajes_activos` | Transcript temporal, clasificación y evidencia. Se elimina en cascada con la conversación. |
| `usuarios_conocidos` | Huella HMAC irreversible y fecha del primer contacto; evita repetir la presentación beta. |
| `consultas_finalizadas` | Fecha de cierre en hora de Lima, edad o rango, calificación y comentario opcional, sin vínculo con el usuario. |
| `categorias_consulta` | Categorías atendidas durante una consulta finalizada, deduplicadas mediante una forma normalizada. |

Las conversaciones con más de 24 horas de inactividad se eliminan. La limpieza ocurre al procesar mensajes y puede ejecutarse además mediante un hilo periódico cada 15 minutos. Los archivos de `instance/`, las bases SQLite y `.env` están excluidos de Git.

## 11. Configuración

Crear un archivo `.env` local sin incorporarlo al repositorio:

```dotenv
VERIFY_TOKEN=token_de_verificacion_de_meta
WHATSAPP_TOKEN=token_de_acceso_de_whatsapp
PHONE_NUMBER_ID=identificador_del_numero
GRAPH_API_VERSION=vXX.X
ANMI_USER_HASH_SECRET=secreto_estable_de_al_menos_32_caracteres
API_GOOGLE=clave_opcional_de_google
ANMI_DB_PATH=ruta_opcional_a_la_base.sqlite3
PORT=5000
```

| Variable | Obligatoria | Descripción |
|---|---:|---|
| `VERIFY_TOKEN` | Producción | Secreto compartido para verificar el webhook. |
| `WHATSAPP_TOKEN` | Producción | Credencial para enviar mensajes mediante Meta. |
| `PHONE_NUMBER_ID` | Producción | ID del número de WhatsApp Business. |
| `GRAPH_API_VERSION` | Producción | Versión explícita de Meta Graph API. |
| `ANMI_USER_HASH_SECRET` | Producción | Secreto estable de 32 caracteres o más para generar huellas HMAC. |
| `API_GOOGLE` | No | Habilita Gemini; sin ella se usa únicamente el clasificador local. |
| `ANMI_DB_PATH` | No | Sobrescribe `instance/anmi.sqlite3` para la aplicación principal. |
| `PORT` | No | Puerto del servicio; el valor predeterminado es 5000. |

Un secreto de identidad puede generarse con:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Cambiar ese secreto hará que las huellas futuras sean diferentes y que los usuarios vuelvan a considerarse nuevos.

## 12. Instalación y ejecución

Requisito recomendado: Python 3.10 o posterior, debido al uso de anotaciones modernas y `zoneinfo`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Ejecutar el webhook:

```powershell
python app.py
```

Ejecutar el simulador en `http://127.0.0.1:5002`:

```powershell
python simulador.py --usar-puerto-prueba true
```

Para usar el valor de `PORT`:

```powershell
python simulador.py --usar-puerto-prueba false
```

Diagnosticar de forma opcional una clave real de Gemini y sus métricas de caché:

```powershell
python -m clasificador_google --verificar-cache
```

## 13. Pruebas y control de calidad

La suite actual cubre:

- Carga y consistencia del catálogo de Gemini.
- Clasificación estructurada, IDs permitidos, caché y fallback.
- Normalización, validación del CSV, puntuación y errores tipográficos.
- Filtros de edad, preferencias, alergias e intolerancias en recetas.
- Persistencia, migraciones, deduplicación, cierre y caducidad.
- Transiciones del servicio conversacional y menús paginados.
- Extracción de payloads, construcción de mensajes interactivos y errores web.
- Aislamiento del simulador respecto de Meta.

Comandos de verificación:

```powershell
python -m unittest discover -s tests -v
python -m compileall app.py clasificador_google.py motor_conocimientos.py conversaciones.py servicio_conversacion.py simulador.py
```

Las pruebas que usan Gemini inyectan dobles de prueba y no requieren una llamada real. La comprobación `--verificar-cache` sí utiliza la clave configurada y es deliberadamente opcional.

## 14. Despliegue y operación

Para publicar el webhook se necesita un endpoint HTTPS accesible por Meta y configurar en la plataforma la ruta `/webhook` junto con el mismo `VERIFY_TOKEN`. El proceso debe disponer de escritura sobre el directorio que contiene la base SQLite.

Antes de desplegar:

1. Ejecutar toda la suite y la compilación sintáctica.
2. Validar que los dos CSV están sincronizados y conservan su codificación.
3. Confirmar que ninguna credencial está versionada.
4. Probar `GET /` y la verificación `GET /webhook`.
5. Verificar permisos y persistencia del volumen de `instance/`.
6. Confirmar que la versión de Graph API configurada continúa vigente.
7. Revisar los logs ante errores de Meta o del clasificador remoto.

Los logs ocultan los identificadores de usuario salvo sus últimos cuatro caracteres. La entrega de respuestas se confirma en la base solo cuando todas las salidas asociadas fueron aceptadas por el adaptador; de forma análoga, la conversación no se finaliza si el mensaje de cierre no pudo entregarse.

## 15. Limitaciones y líneas de mejora

- SQLite es adecuado para una instancia o una carga moderada; un despliegue con varios procesos o mayor concurrencia debe evaluar una base de datos compartida y una estrategia de coordinación.
- La aplicación usa el servidor integrado de Flask al ejecutarse directamente. Para producción conviene incorporar un servidor WSGI y documentar su operación.
- No se valida una firma criptográfica del cuerpo entrante de Meta; añadir verificación de autenticidad reforzaría el webhook.
- No existen todavía métricas operativas centralizadas, panel de administración ni exportación controlada de resúmenes.
- La versión de Graph API se configura manualmente y requiere seguimiento de su ciclo de vida.
- Los cambios de contenido sanitario deben pasar siempre por revisión experta y pruebas de regresión.

## 16. Convenciones de mantenimiento

- Utilizar cuatro espacios, anotaciones de tipo, nombres `snake_case` y clases `PascalCase`.
- Mantener funciones pequeñas y mensajes visibles en español.
- Incorporar pruebas enfocadas en `tests/test_<modulo>.py` por cada cambio de comportamiento.
- Mantener los commits breves, imperativos y acotados, por ejemplo: `Agregar validacion de webhook`.
- No versionar `.env`, tokens, IDs telefónicos, bases de datos ni otros secretos.
- Tratar las respuestas y disclaimers del CSV como material revisado; cualquier cambio debe ser explícito y trazable.
