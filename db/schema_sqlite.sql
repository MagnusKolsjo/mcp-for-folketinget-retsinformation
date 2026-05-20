-- schema_sqlite.sql — SQLite-schema för ström 14 (Danmark).
-- SQLite-installation: filbaserad databas utan pgvector.
-- Vektorsökning (embeddings-tabellen) och pgvector-index saknas.
-- Körs av db.initialisera_schema() — idempotent (CREATE ... IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS dokument (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    kalla                   TEXT NOT NULL,
    extern_id               TEXT,
    beteckning              TEXT,
    typ                     TEXT,
    titel                   TEXT,
    titelkort               TEXT,
    periode                 TEXT,
    datum                   TEXT,
    url                     TEXT,
    retsinformationsurl     TEXT,
    lovnummer               TEXT,
    resume                  TEXT,
    afstemningskonklusion   TEXT,
    paragrafnummer          TEXT,
    paragraf                TEXT,
    afgoerelse              TEXT,
    begrundelse             TEXT,
    baggrundsmateriale      TEXT,
    fulltext_md             TEXT,
    status                  TEXT,
    giltig_till             TEXT,
    synkad                  TEXT,
    UNIQUE (kalla, extern_id)
);

CREATE INDEX IF NOT EXISTS idx_dok_typ    ON dokument (typ);
CREATE INDEX IF NOT EXISTS idx_dok_period ON dokument (periode);
CREATE INDEX IF NOT EXISTS idx_dok_datum  ON dokument (datum);
CREATE INDEX IF NOT EXISTS idx_dok_status ON dokument (status);
CREATE INDEX IF NOT EXISTS idx_dok_giltig ON dokument (giltig_till);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dok_id      INTEGER REFERENCES dokument(id) ON DELETE CASCADE,
    chunk_nr    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    UNIQUE (dok_id, chunk_nr)
);

CREATE TABLE IF NOT EXISTS sync_status (
    nyckel      TEXT PRIMARY KEY,
    varde       TEXT,
    uppdaterad  TEXT
);

CREATE TABLE IF NOT EXISTS relation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fran_eli_url TEXT NOT NULL,
    till_eli_url TEXT NOT NULL,
    relationstyp TEXT NOT NULL,
    synkad       TEXT,
    UNIQUE (fran_eli_url, till_eli_url, relationstyp)
);

CREATE INDEX IF NOT EXISTS idx_rel_fran ON relation (fran_eli_url);
CREATE INDEX IF NOT EXISTS idx_rel_till ON relation (till_eli_url);
