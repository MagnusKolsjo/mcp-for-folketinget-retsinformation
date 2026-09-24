"""
db.py — Databashantering för dansk riksdags- och rättsdata.

Stöder PostgreSQL (primär, med pgvector) och SQLite (explicit val).
Schema: danmark

DATABASE_URL MÅSTE vara satt — antingen i .env eller som miljövariabel.
SQLite används bara om URL:en explicit börjar med "sqlite:///".
Om DATABASE_URL saknas kastas Konfigurationsfel med instruktioner.

Exempel i .env:
  postgresql://<ANVÄNDARE>@localhost:5432/riksdagstryck
  sqlite:///danmark_cache.db

Se config.example.env för fullständigt exempel.
"""

import os
import contextlib
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).parent.resolve()

# Ladda .env om den finns — påverkar INTE miljövariabler som redan är satta
# (t.ex. DATABASE_URL satt via kommandorad eller av backfill-skriptets getpass-flöde)
try:
    from dotenv import load_dotenv
    load_dotenv(_SCRIPT_DIR / ".env", override=False)
except ImportError:
    pass  # python-dotenv valfritt — .env-stöd uteblir men allt annat fungerar


class Konfigurationsfel(RuntimeError):
    """DATABASE_URL eller schemafilerna saknas eller är felaktiga.

    Ärver RuntimeError, så att befintliga anropare som fångar RuntimeError
    fungerar som förut, men kan skiljas från andra körtidsfel.
    """


def _hamta_url() -> str:
    """Hämtar och validerar DATABASE_URL. Kastar Konfigurationsfel om den saknas eller har fel format."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise Konfigurationsfel(
            "DATABASE_URL är inte satt.\n"
            "Skapa en .env-fil i samma mapp som db.py och lägg till:\n"
            "  DATABASE_URL=postgresql://anvandare@localhost:5432/riksdagstryck\n"
            "  # eller: DATABASE_URL=sqlite:///danmark_cache.db\n"
            "Se config.example.env för fullständigt exempel."
        )
    if not (url.startswith("postgresql") or url.startswith("sqlite:///")):
        raise Konfigurationsfel(
            f"Okänt DATABASE_URL-format: {url!r}\n"
            "Förväntade 'postgresql://...' eller 'sqlite:///...'."
        )
    return url


def _hamta_db():
    """Returnerar en ny databasanslutning (PostgreSQL eller SQLite).

    Läser DATABASE_URL vid varje anrop så att lösenord som injicerats
    via os.environ efter modulimport (t.ex. av getpass-flödet i
    backfill-skriptet) tas med korrekt.
    """
    url = _hamta_url()
    if url.startswith("postgresql"):
        import psycopg2
        return psycopg2.connect(url)
    else:
        import sqlite3
        db_path = url.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = str(_SCRIPT_DIR / db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn


@contextlib.contextmanager
def _cursor():
    """Kontexthanterare som ger en cursor och committar vid framgång."""
    conn = _hamta_db()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ar_postgres() -> bool:
    """Returnerar True om DATABASE_URL pekar mot PostgreSQL."""
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith("postgresql://") or url.startswith("postgres://")


def _prefix() -> str:
    """Schemaprefix för PostgreSQL, tomt för SQLite."""
    return "danmark." if _ar_postgres() else ""


def _now() -> str:
    """ISO-tidsstämpel för nu."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema-initiering
# ---------------------------------------------------------------------------

def _hamta_schema_ddl() -> str:
    """Läser schema-DDL från extern SQL-fil i db/-undermappen.

    PostgreSQL: db/schema_postgres.sql
    SQLite:     db/schema_sqlite.sql

    Filen väljs utifrån DATABASE_URL och läses in vid varje initiering så att
    schemafilen kan uppdateras utan att db.py rörs.
    """
    filnamn = "schema_postgres.sql" if _ar_postgres() else "schema_sqlite.sql"
    sokvag = _SCRIPT_DIR / "db" / filnamn
    try:
        return sokvag.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Konfigurationsfel(
            f"Schemafilen saknas: {sokvag}\n"
            "Kontrollera att db/schema_postgres.sql och db/schema_sqlite.sql finns."
        )


# Migration-block. Bas-schemat i db/schema_*.sql är låst sedan första
# publiceringen; schemaändringar läggs till här som idempotenta satser.
#
# fulltext_kalla: varifrån fulltext_md kommer.
#   'resume'    — ODA-ärendets resume, preliminär text tills PDF:en hämtats
#   'pdf'       — extraherad ur ft.dk-PDF:en
#   'ingen_pdf' — PDF-hämtningen är gjord utan resultat; texten är resume
#                 eller en platshållare
#   NULL        — okänd (Retsinformation-dokument och rader från före
#                 kolumnen, se _markera_fulltext_kalla)
#
# chunk_hash: md5 av texten när dokumentet senast chunkades
#   (se CHUNKTEXT_SQL). Avviker den från nuvarande text chunkas dokumentet om.
_MIGRATION_POSTGRES = """
ALTER TABLE danmark.dokument ADD COLUMN IF NOT EXISTS fulltext_kalla TEXT;
ALTER TABLE danmark.dokument ADD COLUMN IF NOT EXISTS chunk_hash TEXT
"""

# SQLite saknar ADD COLUMN IF NOT EXISTS; felet när kolumnen redan finns
# fångas i initialisera_schema().
_MIGRATION_SQLITE = """
ALTER TABLE dokument ADD COLUMN fulltext_kalla TEXT;
ALTER TABLE dokument ADD COLUMN chunk_hash TEXT
"""

# Texten som chunkas, uttryckt i SQL: resume följt av fulltexten, eller den
# som finns. Motsvarar _chunktext() i 04_chunka_och_embedda.py.
CHUNKTEXT_SQL = (
    "CASE WHEN NULLIF(d.resume, '') IS NOT NULL AND NULLIF(d.fulltext_md, '') IS NOT NULL "
    "THEN d.resume || E'\\n\\n' || d.fulltext_md "
    "ELSE COALESCE(NULLIF(d.fulltext_md, ''), d.resume, '') END"
)
_CHUNK_HASH_NYCKEL = "migrering_chunk_hash"


def _markera_chunk_hash() -> None:
    """Engångsmarkering: befintliga chunks antas höra till nuvarande text.

    Dokument som chunkades före kolumnen har ingen hash och antas vara
    aktuella, men då upptäcks inte heller senare textändringar. Markeringen
    ger dem hashen av sin nuvarande text, så att texter som ändras därefter
    chunkas om. Körs en gång per databas via migrera_data() och noteras i
    sync_status.
    """
    if hamta_sync_status(_CHUNK_HASH_NYCKEL):
        return
    p = _prefix()
    if _ar_postgres():
        with _cursor() as cur:
            cur.execute(
                f"""UPDATE {p}dokument d SET chunk_hash = md5({CHUNKTEXT_SQL})
                    WHERE d.chunk_hash IS NULL
                      AND EXISTS (SELECT 1 FROM {p}chunks c WHERE c.dok_id = d.id)"""
            )
            antal = cur.rowcount
    else:
        import hashlib
        with _cursor() as cur:
            cur.execute(
                """SELECT d.id, d.resume, d.fulltext_md FROM dokument d
                   WHERE d.chunk_hash IS NULL
                     AND EXISTS (SELECT 1 FROM chunks c WHERE c.dok_id = d.id)"""
            )
            rader = cur.fetchall()
            for dok_id, resume, fulltext in rader:
                text = (resume + "\n\n" + fulltext) if resume and fulltext else (fulltext or resume or "")
                cur.execute("UPDATE dokument SET chunk_hash = ? WHERE id = ?",
                            (hashlib.md5(text.encode("utf-8")).hexdigest(), dok_id))
            antal = len(rader)
    spara_sync_status(_CHUNK_HASH_NYCKEL, f"{_now()} ({antal} dokument antagna aktuella)")

_MARKERING_NYCKEL = "migrering_fulltext_kalla"


def _markera_fulltext_kalla() -> None:
    """Engångsmarkering av befintliga ODA-rader vars fulltext är resume.

    Rader från före kolumnen fulltext_kalla har NULL där. De ODA-ärenden vars
    fulltext_md är identisk med resume har aldrig fått sin PDF, och markeras
    'resume' så att fas 2 i ODA-synken tar dem. Körs en gång per databas
    via migrera_data() och noteras i sync_status.
    """
    if hamta_sync_status(_MARKERING_NYCKEL):
        return
    p = _prefix()
    with _cursor() as cur:
        cur.execute(
            f"""UPDATE {p}dokument SET fulltext_kalla = 'resume'
                WHERE kalla = 'oda' AND fulltext_kalla IS NULL
                  AND fulltext_md IS NOT NULL AND fulltext_md = resume"""
        )
        antal = cur.rowcount
    spara_sync_status(_MARKERING_NYCKEL, f"{_now()} ({antal} rader markerade 'resume')")


def initialisera_schema():
    """Skapar alla tabeller om de inte redan finns, och migrerar befintliga."""
    ddl = _hamta_schema_ddl()
    if _ar_postgres():
        import psycopg2
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            conn.autocommit = False
            # Skapa tabeller (IF NOT EXISTS — ingen effekt om de redan finns)
            cur.execute(ddl)
            # Lägg till nya kolumner om de saknas (idempotent)
            for sats in _MIGRATION_POSTGRES.strip().split(";"):
                sats = sats.strip()
                if sats:
                    cur.execute(sats)
            conn.commit()
        finally:
            conn.close()
    else:
        with _cursor() as cur:
            for sats in ddl.split(";"):
                sats = sats.strip()
                if sats:
                    cur.execute(sats)
        # SQLite: ALTER TABLE ADD COLUMN ignorerar fel om kolumnen redan finns
        import sqlite3
        conn = _hamta_db()
        try:
            for sats in _MIGRATION_SQLITE.strip().split(";"):
                sats = sats.strip()
                if sats:
                    try:
                        conn.execute(sats)
                    except sqlite3.OperationalError:
                        pass  # Kolumnen finns redan
            conn.commit()
        finally:
            conn.close()

    # Uppstarten gör bara snabba, idempotenta schemaändringar. MCP-klienten
    # väntar en begränsad tid på serverns svar, så
    # dataskrivningar över hela tabeller hör hemma i migrera_data(). Den
    # automatiska halfvec-konverteringen gäller bara tabeller med högst
    # AUTO_KONVERTERA_MAX_RADER vektorer och tar några sekunder.
    if _ar_postgres():
        _migrera_halfvec()


def migrera_data() -> None:
    """Engångsuppdateringar av befintliga rader efter en uppgradering.

    Körs uttryckligen (python3 db.py --migrera) och i början av
    02_synka_oda.py och 04_chunka_och_embedda.py, aldrig vid serverns
    uppstart: på en stor databas tar de mer än en minut. Varje steg noteras i
    sync_status och körs bara en gång; ett avbrutet steg rullas tillbaka och
    görs om nästa gång.

    Servern och skripten fungerar innan stegen körts: ett ODA-ärende utan
    fulltext_kalla väljs inte av fas 2 (som före uppgraderingen), och ett
    dokument med chunks men utan chunk_hash antas vara aktuellt.
    """
    _markera_fulltext_kalla()
    _markera_chunk_hash()


# ---------------------------------------------------------------------------
# Vektorlagring: vector eller halfvec
# ---------------------------------------------------------------------------
# Embeddings lagras som halfvec(768), 16-bitars flyttal: 1 540 byte per vektor
# i stället för 3 076. En vector(768) är större än PostgreSQL:s gräns för
# rader i tabellen och hamnar i TOAST, medan halfvec ryms i själva tabellen.
# Träffsäkerheten för cosinussökning påverkas inte mätbart.
#
# Bas-schemat skapar kolumnen som vector(768). Nya och små databaser
# konverteras vid uppstart; större med 08_konvertera_vektorer.py, eftersom
# omskrivningen tar tid och disk. Frågorna läser kolumntypen och castar
# frågevektorn därefter, så servern fungerar både före och efter.
#
# Index: HNSW (m=16, ef_construction=64). Utan vektorindex jämför varje
# sökning frågan med samtliga vektorer.

VEKTOR_DIM = 768
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64
# ef_search 400: mätt på 60 000 embeddings ur driften gav 100 recall@10 0,82
# (enstaka frågor 0), 400 gav 0,95 på 8 ms. Korpusen har många nästan
# identiska chunks (7 % dubblettvektorer), vilket gör grafen svårare att
# söka; en större kandidatlista kompenserar.
HNSW_EF_SEARCH = int(os.getenv("DK_HNSW_EF_SEARCH", "400"))
VEKTORINDEX_NAMN = "idx_danmark_embeddings_hnsw"

# Under den här storleken konverteras kolumnen automatiskt vid uppstart.
AUTO_KONVERTERA_MAX_RADER = 50_000


def vektortyp(cur=None) -> str:
    """'halfvec' eller 'vector' för danmark.embeddings.vektor."""
    sql = """SELECT format_type(a.atttypid, a.atttypmod)
             FROM pg_attribute a
             WHERE a.attrelid = 'danmark.embeddings'::regclass AND a.attname = 'vektor'"""
    if cur is not None:
        cur.execute(sql)
        rad = cur.fetchone()
    else:
        with _cursor() as c:
            c.execute(sql)
            rad = c.fetchone()
    return "halfvec" if rad and rad[0].startswith("halfvec") else "vector"


def sokinstallningar(cur, kandidater: int = 0) -> None:
    """Sökparametrar för HNSW, gäller bara transaktionen.

    ef_search är kandidatlistans storlek i grafsökningen och sätter taket för
    hur många rader en indexsökning ger; den höjs därför till minst det antal
    kandidater frågan ber om. Iterativ sökning fortsätter om ett filter eller
    en JOIN sållar bort rader, i stället för att ge för få träffar.
    """
    ef = max(HNSW_EF_SEARCH, kandidater)
    cur.execute(f"SET LOCAL hnsw.ef_search = {int(min(ef, 1000))}")
    cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")


def bygg_vektorindex(minne: Optional[str] = None, parallella: Optional[int] = None) -> None:
    """Bygger om vektorindexet som HNSW med operatorklass efter kolumntypen.

    HNSW-bygget går mycket snabbare när grafen ryms i maintenance_work_mem.
    """
    with _cursor() as cur:
        typ = vektortyp(cur)
        ops = "halfvec_cosine_ops" if typ == "halfvec" else "vector_cosine_ops"
        if minne:
            cur.execute("SET LOCAL maintenance_work_mem = %s", (minne,))
        if parallella is not None:
            cur.execute(f"SET LOCAL max_parallel_maintenance_workers = {int(parallella)}")
        cur.execute(f"DROP INDEX IF EXISTS danmark.{VEKTORINDEX_NAMN}")
        cur.execute(
            f"CREATE INDEX {VEKTORINDEX_NAMN} ON danmark.embeddings "
            f"USING hnsw (vektor {ops}) "
            f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
        )


def konvertera_till_halfvec(cur) -> None:
    """Byter kolumnen till halfvec(768). Skriver om hela danmark.embeddings.

    Ett befintligt vektorindex tas bort först, eftersom dess operatorklass
    gäller vector. Anroparen bygger nytt index efteråt (bygg_vektorindex).
    """
    cur.execute(f"DROP INDEX IF EXISTS danmark.{VEKTORINDEX_NAMN}")
    cur.execute(
        f"ALTER TABLE danmark.embeddings ALTER COLUMN vektor "
        f"TYPE halfvec({VEKTOR_DIM}) USING vektor::halfvec({VEKTOR_DIM})"
    )


def _migrera_halfvec() -> None:
    """Konverterar små tabeller vid uppstart; stora lämnas till skriptet."""
    import logging
    log = logging.getLogger(__name__)
    with _cursor() as cur:
        if vektortyp(cur) == "halfvec":
            return
        cur.execute(
            f"SELECT count(*) FROM (SELECT 1 FROM danmark.embeddings "
            f"LIMIT {AUTO_KONVERTERA_MAX_RADER + 1}) x"
        )
        if cur.fetchone()[0] > AUTO_KONVERTERA_MAX_RADER:
            log.info(
                "danmark.embeddings lagrar vektorer som vector och saknar HNSW-index. "
                "Kör 08_konvertera_vektorer.py för att byta till halfvec."
            )
            return
        konvertera_till_halfvec(cur)
    bygg_vektorindex()
    log.info("danmark.embeddings konverterad till halfvec(%d) med HNSW-index", VEKTOR_DIM)


def satt_resume_som_fulltext(dok_id: int) -> None:
    """Låter ett ODA-ärendes resume vara dess preliminära fulltext.

    Nya ärenden och ärenden som fortfarande bara har resume som text får
    fulltext_md = resume och fulltext_kalla = 'resume', så att en ändrad
    resume slår igenom. En PDF-text ('pdf') eller en rad vars text inte är
    resume rörs inte.
    """
    p = _prefix()
    ph = "%s" if _ar_postgres() else "?"
    with _cursor() as cur:
        cur.execute(
            f"""UPDATE {p}dokument
                SET fulltext_md = resume, fulltext_kalla = 'resume'
                WHERE id = {ph} AND resume IS NOT NULL
                  AND (fulltext_kalla = 'resume'
                       OR (fulltext_kalla IS NULL
                           AND (fulltext_md IS NULL OR fulltext_md = resume)))""",
            (dok_id,)
        )


# ---------------------------------------------------------------------------
# Synkstatus
# ---------------------------------------------------------------------------

def hamta_sync_status(nyckel: str) -> Optional[str]:
    """Hämtar ett synkstatus-värde, eller None om det inte finns."""
    p = _prefix()
    with _cursor() as cur:
        cur.execute(
            f"SELECT varde FROM {p}sync_status WHERE nyckel = {'%s' if _ar_postgres() else '?'}",
            (nyckel,)
        )
        rad = cur.fetchone()
        return rad[0] if rad else None


def spara_sync_status(nyckel: str, varde: str):
    """Upsertar ett synkstatus-värde."""
    p = _prefix()
    nu = _now()
    if _ar_postgres():
        with _cursor() as cur:
            cur.execute(
                f"""INSERT INTO {p}sync_status (nyckel, varde, uppdaterad)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (nyckel) DO UPDATE
                    SET varde = EXCLUDED.varde, uppdaterad = EXCLUDED.uppdaterad""",
                (nyckel, varde, nu)
            )
    else:
        with _cursor() as cur:
            cur.execute(
                f"""INSERT INTO {p}sync_status (nyckel, varde, uppdaterad)
                    VALUES (?, ?, ?)
                    ON CONFLICT (nyckel) DO UPDATE
                    SET varde = excluded.varde, uppdaterad = excluded.uppdaterad""",
                (nyckel, varde, nu)
            )


# ---------------------------------------------------------------------------
# Dokument
# ---------------------------------------------------------------------------

def upsert_dokument(
    kalla: str,
    extern_id: str,
    beteckning: Optional[str],
    typ: Optional[str],
    titel: Optional[str],
    periode: Optional[str],
    datum: Optional[str],
    url: Optional[str],
    fulltext_md: Optional[str] = None,
    titelkort: Optional[str] = None,
    retsinformationsurl: Optional[str] = None,
    lovnummer: Optional[str] = None,
    resume: Optional[str] = None,
    afstemningskonklusion: Optional[str] = None,
    paragrafnummer: Optional[str] = None,
    paragraf: Optional[str] = None,
    afgoerelse: Optional[str] = None,
    begrundelse: Optional[str] = None,
    baggrundsmateriale: Optional[str] = None,
    status: Optional[str] = None,
    giltig_till: Optional[str] = None,
    behall_fulltext: bool = False,
) -> int:
    """Infogar eller uppdaterar ett dokument. Returnerar dess id.

    behall_fulltext=True låter en befintlig fulltext_md stå kvar och använder
    det nya värdet bara när raden saknar text. ODA-synken behöver det: den
    skickar resume som preliminär text, och utan flaggan skulle varje
    uppdatering av ett ärende skriva över en redan extraherad PDF-text.
    """
    p = _prefix()
    nu = _now()
    kolumner = (
        "kalla, extern_id, beteckning, typ, titel, titelkort, periode, datum, "
        "url, retsinformationsurl, lovnummer, resume, afstemningskonklusion, "
        "paragrafnummer, paragraf, afgoerelse, begrundelse, baggrundsmateriale, "
        "fulltext_md, status, giltig_till, synkad"
    )
    varden = (
        kalla, extern_id, beteckning, typ, titel, titelkort, periode, datum,
        url, retsinformationsurl, lovnummer, resume, afstemningskonklusion,
        paragrafnummer, paragraf, afgoerelse, begrundelse, baggrundsmateriale,
        fulltext_md, status, giltig_till, nu
    )
    if _ar_postgres():
        fulltext_uttryck = (
            f"COALESCE({p}dokument.fulltext_md, EXCLUDED.fulltext_md)" if behall_fulltext
            else f"COALESCE(EXCLUDED.fulltext_md, {p}dokument.fulltext_md)"
        )
        platshallare = ", ".join(["%s"] * len(varden))
        with _cursor() as cur:
            cur.execute(
                f"""INSERT INTO {p}dokument ({kolumner})
                    VALUES ({platshallare})
                    ON CONFLICT (kalla, extern_id) DO UPDATE SET
                        beteckning            = EXCLUDED.beteckning,
                        typ                   = EXCLUDED.typ,
                        titel                 = EXCLUDED.titel,
                        titelkort             = EXCLUDED.titelkort,
                        periode               = EXCLUDED.periode,
                        datum                 = EXCLUDED.datum,
                        url                   = COALESCE(EXCLUDED.url, {p}dokument.url),
                        retsinformationsurl   = COALESCE(EXCLUDED.retsinformationsurl, {p}dokument.retsinformationsurl),
                        lovnummer             = COALESCE(EXCLUDED.lovnummer, {p}dokument.lovnummer),
                        resume                = COALESCE(EXCLUDED.resume, {p}dokument.resume),
                        afstemningskonklusion = COALESCE(EXCLUDED.afstemningskonklusion, {p}dokument.afstemningskonklusion),
                        paragrafnummer        = COALESCE(EXCLUDED.paragrafnummer, {p}dokument.paragrafnummer),
                        paragraf              = COALESCE(EXCLUDED.paragraf, {p}dokument.paragraf),
                        afgoerelse            = COALESCE(EXCLUDED.afgoerelse, {p}dokument.afgoerelse),
                        begrundelse           = COALESCE(EXCLUDED.begrundelse, {p}dokument.begrundelse),
                        baggrundsmateriale    = COALESCE(EXCLUDED.baggrundsmateriale, {p}dokument.baggrundsmateriale),
                        fulltext_md           = {fulltext_uttryck},
                        status                = COALESCE(EXCLUDED.status, {p}dokument.status),
                        giltig_till           = COALESCE(EXCLUDED.giltig_till, {p}dokument.giltig_till),
                        synkad                = EXCLUDED.synkad
                    RETURNING id""",
                varden
            )
            return cur.fetchone()[0]
    else:
        fulltext_uttryck = (
            "COALESCE(dokument.fulltext_md, excluded.fulltext_md)" if behall_fulltext
            else "COALESCE(excluded.fulltext_md, dokument.fulltext_md)"
        )
        platshallare = ", ".join(["?"] * len(varden))
        with _cursor() as cur:
            cur.execute(
                f"""INSERT INTO {p}dokument ({kolumner})
                    VALUES ({platshallare})
                    ON CONFLICT (kalla, extern_id) DO UPDATE SET
                        beteckning            = excluded.beteckning,
                        typ                   = excluded.typ,
                        titel                 = excluded.titel,
                        titelkort             = excluded.titelkort,
                        periode               = excluded.periode,
                        datum                 = excluded.datum,
                        url                   = COALESCE(excluded.url, dokument.url),
                        retsinformationsurl   = COALESCE(excluded.retsinformationsurl, dokument.retsinformationsurl),
                        lovnummer             = COALESCE(excluded.lovnummer, dokument.lovnummer),
                        resume                = COALESCE(excluded.resume, dokument.resume),
                        afstemningskonklusion = COALESCE(excluded.afstemningskonklusion, dokument.afstemningskonklusion),
                        paragrafnummer        = COALESCE(excluded.paragrafnummer, dokument.paragrafnummer),
                        paragraf              = COALESCE(excluded.paragraf, dokument.paragraf),
                        afgoerelse            = COALESCE(excluded.afgoerelse, dokument.afgoerelse),
                        begrundelse           = COALESCE(excluded.begrundelse, dokument.begrundelse),
                        baggrundsmateriale    = COALESCE(excluded.baggrundsmateriale, dokument.baggrundsmateriale),
                        fulltext_md           = {fulltext_uttryck},
                        status                = COALESCE(excluded.status, dokument.status),
                        giltig_till           = COALESCE(excluded.giltig_till, dokument.giltig_till),
                        synkad                = excluded.synkad""",
                varden
            )
            cur.execute(
                "SELECT id FROM dokument WHERE kalla = ? AND extern_id = ?",
                (kalla, extern_id)
            )
            return cur.fetchone()[0]


def markera_ersatt(extern_id: str):
    """
    Markerar ett retsinformation-dokument som ersatt (RemovedDocument från harvest-API).
    Sätter status='Ersatt' utan att radera dokumentet — historiken bevaras.
    """
    p = _prefix()
    ph = "%s" if _ar_postgres() else "?"
    nu = _now()
    with _cursor() as cur:
        cur.execute(
            f"""UPDATE {p}dokument
                SET status = 'Ersatt', synkad = {ph}
                WHERE kalla = 'retsinformation' AND extern_id = {ph}""",
            (nu, extern_id)
        )


def hamta_dokument_med_id(dok_id: int) -> Optional[dict]:
    """Hämtar ett dokument via dess interna id."""
    p = _prefix()
    with _cursor() as cur:
        cur.execute(
            f"SELECT * FROM {p}dokument WHERE id = {'%s' if _ar_postgres() else '?'}",
            (dok_id,)
        )
        rad = cur.fetchone()
        if not rad:
            return None
        kolumner = [desc[0] for desc in cur.description]
        return dict(zip(kolumner, rad))


def sok_dokument_fts(sokterm: str, limit: int = 20, inkludera_ersatta: bool = False,
                     kalla: str = None) -> list[dict]:
    """
    Fulltextsökning i titel och fulltext_md via PostgreSQL FTS (danish-konfiguration)
    eller SQLite LIKE (vid SQLite-installation).

    inkludera_ersatta=False (standard): filtrerar bort dokument med status='Ersatt'
    och dokument vars giltighetsperiod löpt ut (giltig_till < idag).
    kalla: begränsa till en specifik källa ('oda' eller 'retsinformation').
           Om None söks alla källor.
    """
    from datetime import date
    idag = date.today().isoformat()
    p = _prefix()
    resultat = []
    with _cursor() as cur:
        if _ar_postgres():
            giltighetsfilter = "" if inkludera_ersatta else """
                AND (status IS NULL OR status NOT IN ('Historic', 'HISTORISK', 'Ersatt', 'notInForce'))"""
            kallafilter = f"AND kalla = %s" if kalla else ""
            params = [sokterm, sokterm]
            if kalla:
                params.append(kalla)
            params.append(limit)
            cur.execute(
                f"""SELECT id, extern_id, kalla, beteckning, typ, titel, titelkort, periode, datum,
                           url, retsinformationsurl, lovnummer, resume, afstemningskonklusion,
                           paragrafnummer, paragraf, afgoerelse, begrundelse, baggrundsmateriale,
                           status, giltig_till,
                           ts_rank(to_tsvector('danish',
                               coalesce(titel,'') || ' ' || coalesce(titelkort,'') || ' ' ||
                               coalesce(resume,'') || ' ' || coalesce(fulltext_md,'')),
                               plainto_tsquery('danish', %s)) AS rank
                    FROM {p}dokument
                    WHERE to_tsvector('danish',
                              coalesce(titel,'') || ' ' || coalesce(titelkort,'') || ' ' ||
                              coalesce(resume,'') || ' ' || coalesce(fulltext_md,''))
                          @@ plainto_tsquery('danish', %s)
                    {kallafilter}
                    {giltighetsfilter}
                    ORDER BY rank DESC
                    LIMIT %s""",
                params
            )
        else:
            monstret = f"%{sokterm}%"
            giltighetsfilter = "" if inkludera_ersatta else f"""
                AND (status IS NULL OR status NOT IN ('Historic', 'HISTORISK', 'Ersatt', 'notInForce'))"""
            kallafilter = "AND kalla = ?" if kalla else ""
            params = [monstret, monstret, monstret]
            if kalla:
                params.append(kalla)
            params.append(limit)
            cur.execute(
                f"""SELECT id, extern_id, kalla, beteckning, typ, titel, titelkort, periode, datum,
                           url, retsinformationsurl, lovnummer, resume, afstemningskonklusion,
                           paragrafnummer, paragraf, afgoerelse, begrundelse, baggrundsmateriale,
                           status, giltig_till, 0 AS rank
                    FROM {p}dokument
                    WHERE (titel LIKE ? OR resume LIKE ? OR fulltext_md LIKE ?)
                    {kallafilter}
                    {giltighetsfilter}
                    LIMIT ?""",
                params
            )
        kolumner = [desc[0] for desc in cur.description]
        for rad in cur.fetchall():
            resultat.append(dict(zip(kolumner, rad)))
    return resultat


# ---------------------------------------------------------------------------
# ELI-relationer (eli:changes — ändringslagar → baslag)
# ---------------------------------------------------------------------------

def lagra_relationer(fran_eli_url: str, till_eli_urls: list, relationstyp: str = "changes") -> int:
    """
    Lagrar ELI-relationer för ett dokument.
    fran_eli_url: ELI-URL för ändringslagens (t.ex. LOVC)
    till_eli_urls: lista av ELI-URLs för de lagar som ändras
    Returnerar antal nya relationer som sparades.
    """
    if not till_eli_urls:
        return 0
    nu = _now()
    p = _prefix()
    sparade = 0
    with _cursor() as cur:
        for till_url in till_eli_urls:
            if not till_url:
                continue
            if _ar_postgres():
                cur.execute(
                    f"""INSERT INTO {p}relation (fran_eli_url, till_eli_url, relationstyp, synkad)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (fran_eli_url, till_eli_url, relationstyp) DO NOTHING""",
                    (fran_eli_url, till_url, relationstyp, nu)
                )
            else:
                cur.execute(
                    f"""INSERT OR IGNORE INTO {p}relation
                        (fran_eli_url, till_eli_url, relationstyp, synkad)
                        VALUES (?, ?, ?, ?)""",
                    (fran_eli_url, till_url, relationstyp, nu)
                )
            sparade += cur.rowcount
    return sparade


def hamta_andringar_for_lag(eli_url: str) -> list[dict]:
    """
    Returnerar alla ändringslagar (LOVC etc.) som har ändrat lagen med given ELI-URL.
    Söker i relations-tabellen och hämtar dokumentmetadata för varje träff.
    Filtrerar bort historiska ändringslagar.
    """
    p = _prefix()
    resultat = []
    with _cursor() as cur:
        if _ar_postgres():
            cur.execute(
                f"""SELECT d.id, d.beteckning, d.typ, d.titel, d.datum,
                           d.url, d.retsinformationsurl, d.lovnummer, d.status
                    FROM {p}dokument d
                    JOIN {p}relation r ON r.fran_eli_url = d.url
                    WHERE r.till_eli_url = %s
                      AND r.relationstyp = 'changes'
                      AND (d.status IS NULL OR d.status NOT IN ('Historic', 'HISTORISK', 'Ersatt', 'notInForce'))
                    ORDER BY d.datum DESC""",
                (eli_url,)
            )
        else:
            cur.execute(
                f"""SELECT d.id, d.beteckning, d.typ, d.titel, d.datum,
                           d.url, d.retsinformationsurl, d.lovnummer, d.status
                    FROM {p}dokument d
                    JOIN {p}relation r ON r.fran_eli_url = d.url
                    WHERE r.till_eli_url = ?
                      AND r.relationstyp = 'changes'
                      AND (d.status IS NULL OR d.status NOT IN ('Historic', 'HISTORISK', 'Ersatt', 'notInForce'))
                    ORDER BY d.datum DESC""",
                (eli_url,)
            )
        kolumner = [desc[0] for desc in cur.description]
        for rad in cur.fetchall():
            resultat.append(dict(zip(kolumner, rad)))
    return resultat


if __name__ == "__main__":
    import argparse
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Databasschema och engångsmigreringar")
    parser.add_argument("--migrera", action="store_true",
                        help="Initiera schemat och kör engångsuppdateringarna av befintliga rader")
    args = parser.parse_args()
    if not args.migrera:
        parser.print_help()
        raise SystemExit(0)
    initialisera_schema()
    migrera_data()
    logging.info("fulltext_kalla: %s", hamta_sync_status(_MARKERING_NYCKEL))
    logging.info("chunk_hash: %s", hamta_sync_status(_CHUNK_HASH_NYCKEL))
