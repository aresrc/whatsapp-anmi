PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS conversaciones_activas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canal TEXT NOT NULL CHECK (length(trim(canal)) > 0),
    usuario_temporal TEXT NOT NULL
        CHECK (length(trim(usuario_temporal)) > 0),
    estado TEXT NOT NULL CHECK (
        estado IN (
            'esperando_meses',
            'esperando_alimentos',
            'esperando_edad_receta',
            'lista',
            'menu_general',
            'menu_especifico',
            'esperando_calificacion'
        )
    ),
    fecha_inicio_utc TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    fecha_ultima_actividad_utc TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    meses_bebe INTEGER
        CHECK (meses_bebe IS NULL OR meses_bebe BETWEEN 6 AND 36),
    rango_edad_bebe TEXT CHECK (
        rango_edad_bebe IS NULL
        OR rango_edad_bebe IN (
            '6-8', '9-11', '12-23',
            '6-12', '12-24', '24-36'
        )
    ),
    alimentos_contexto TEXT,
    categoria_menu TEXT
        CHECK (
            categoria_menu IS NULL
            OR length(trim(categoria_menu)) > 0
        ),
    pagina_menu INTEGER NOT NULL DEFAULT 0 CHECK (pagina_menu >= 0),
    calificacion_pendiente INTEGER
        CHECK (
            calificacion_pendiente IS NULL
            OR calificacion_pendiente BETWEEN 1 AND 5
        ),
    UNIQUE (canal, usuario_temporal)
);

CREATE TABLE IF NOT EXISTS mensajes_activos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversacion_id INTEGER NOT NULL,
    id_externo TEXT,
    rol TEXT NOT NULL CHECK (rol IN ('usuario', 'bot')),
    contenido TEXT NOT NULL,
    categoria TEXT,
    subcategoria TEXT,
    puntaje REAL,
    evidencia_json TEXT
        CHECK (evidencia_json IS NULL OR json_valid(evidencia_json)),
    fecha_hora_utc TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (conversacion_id)
        REFERENCES conversaciones_activas (id)
        ON DELETE CASCADE
);

-- Meta puede reenviar el mismo evento. La unicidad parcial evita duplicarlo
-- sin obligar al simulador ni a los mensajes del bot a inventar un id externo.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mensajes_activos_id_externo
    ON mensajes_activos (id_externo)
    WHERE id_externo IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_mensajes_activos_conversacion
    ON mensajes_activos (conversacion_id, id);

CREATE INDEX IF NOT EXISTS ix_conversaciones_ultima_actividad
    ON conversaciones_activas (fecha_ultima_actividad_utc);

CREATE TABLE IF NOT EXISTS consultas_finalizadas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha_hora_cierre TEXT NOT NULL,
    meses_bebe INTEGER
        CHECK (meses_bebe IS NULL OR meses_bebe BETWEEN 6 AND 36),
    rango_edad_bebe TEXT CHECK (
        rango_edad_bebe IS NULL
        OR rango_edad_bebe IN (
            '6-8', '9-11', '12-23',
            '6-12', '12-24', '24-36'
        )
    ),
    calificacion INTEGER NOT NULL CHECK (calificacion BETWEEN 1 AND 5)
);

CREATE TABLE IF NOT EXISTS categorias_consulta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consulta_finalizada_id INTEGER NOT NULL,
    categoria TEXT NOT NULL CHECK (length(trim(categoria)) > 0),
    categoria_normalizada TEXT NOT NULL
        CHECK (length(trim(categoria_normalizada)) > 0),
    FOREIGN KEY (consulta_finalizada_id)
        REFERENCES consultas_finalizadas (id)
        ON DELETE CASCADE,
    UNIQUE (consulta_finalizada_id, categoria_normalizada)
);

CREATE INDEX IF NOT EXISTS ix_categorias_consulta_normalizada
    ON categorias_consulta (categoria_normalizada);
