-- schema_postgres.sql — PostgreSQL-schema för ström 14 (Danmark).
-- Schema: danmark
-- Körs av db.initialisera_schema() via CREATE ... IF NOT EXISTS — idempotent.
-- OBS: CREATE EXTENSION vector körs separat med autocommit i db.py.

CREATE SCHEMA IF NOT EXISTS danmark;

CREATE TABLE IF NOT EXISTS danmark.dokument (
    id                      SERIAL PRIMARY KEY,
    kalla                   TEXT NOT NULL,          -- 'oda' eller 'retsinformation'
    extern_id               TEXT,                   -- sagid (ODA) eller retsinformationId
    beteckning              TEXT,                   -- t.ex. 'L 183'
    typ                     TEXT,                   -- 'lovforslag', 'lov', 'bekendtgorelse' etc.
    titel                   TEXT,
    titelkort               TEXT,
    periode                 TEXT,                   -- t.ex. '20242'
    datum                   TEXT,                   -- ISO-datum
    url                     TEXT,                   -- fil-URL (PDF) eller retsinformation-URL
    retsinformationsurl     TEXT,                   -- direktlänk till retsinformation.dk
    lovnummer               TEXT,                   -- lagbeteckning efter antagande
    resume                  TEXT,                   -- ODA-sammanfattning (2-6 meningar)
    afstemningskonklusion   TEXT,                   -- röstningstext i klartext
    paragrafnummer          TEXT,                   -- paragrafnummer (direktreferens till lagparagraf)
    paragraf                TEXT,                   -- paragraftextcitat
    afgoerelse              TEXT,                   -- beslutets lydelse
    begrundelse             TEXT,                   -- motivering till förslaget
    baggrundsmateriale      TEXT,                   -- bakgrundsmaterial (URL eller text)
    fulltext_md             TEXT,                   -- extraherad PDF-text (Markdown)
    status                  TEXT,                   -- 'Valid', 'Ersatt' (satt av harvest-API RemovedDocument)
    giltig_till             TEXT,                   -- ISO-datum för slutet av giltighetsperiod (EndDate i XML)
    synkad                  TEXT,
    UNIQUE (kalla, extern_id)
);

CREATE INDEX IF NOT EXISTS idx_dok_typ    ON danmark.dokument (typ);
CREATE INDEX IF NOT EXISTS idx_dok_period ON danmark.dokument (periode);
CREATE INDEX IF NOT EXISTS idx_dok_datum  ON danmark.dokument (datum);
CREATE INDEX IF NOT EXISTS idx_dok_status ON danmark.dokument (status);
CREATE INDEX IF NOT EXISTS idx_dok_giltig ON danmark.dokument (giltig_till);
CREATE INDEX IF NOT EXISTS idx_dok_fts    ON danmark.dokument
    USING GIN (to_tsvector('danish',
        coalesce(titel, '') || ' ' || coalesce(titelkort, '') || ' ' ||
        coalesce(resume, '') || ' ' || coalesce(fulltext_md, '')));

CREATE TABLE IF NOT EXISTS danmark.chunks (
    id          SERIAL PRIMARY KEY,
    dok_id      INTEGER REFERENCES danmark.dokument(id) ON DELETE CASCADE,
    chunk_nr    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    UNIQUE (dok_id, chunk_nr)
);

CREATE TABLE IF NOT EXISTS danmark.embeddings (
    chunk_id    INTEGER PRIMARY KEY REFERENCES danmark.chunks(id) ON DELETE CASCADE,
    vektor      vector(768)
);

CREATE TABLE IF NOT EXISTS danmark.sync_status (
    nyckel      TEXT PRIMARY KEY,
    varde       TEXT,
    uppdaterad  TEXT
);

CREATE TABLE IF NOT EXISTS danmark.relation (
    id           SERIAL PRIMARY KEY,
    fran_eli_url TEXT NOT NULL,   -- ELI-URL för dokumentet som ändrar (t.ex. LOVC)
    till_eli_url TEXT NOT NULL,   -- ELI-URL för det dokument som ändras (t.ex. LBKH)
    relationstyp TEXT NOT NULL,   -- 'changes' (eli:changes)
    synkad       TEXT,
    UNIQUE (fran_eli_url, till_eli_url, relationstyp)
);

CREATE INDEX IF NOT EXISTS idx_rel_fran ON danmark.relation (fran_eli_url);
CREATE INDEX IF NOT EXISTS idx_rel_till ON danmark.relation (till_eli_url);
