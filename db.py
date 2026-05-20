"""
db.py — Databashantering för ström 14 (Danmark).

Stöder PostgreSQL (primär, med pgvector) och SQLite (explicit val).
Schema: danmark

DATABASE_URL MÅSTE vara satt — antingen i .env eller som miljövariabel.
SQLite används bara om URL:en explicit börjar med "sqlite:///".
Om DATABASE_URL saknas kastas ett ConfigurationError med instruktioner.

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


def _hamta_url() -> str:
    """Hämtar och validerar DATABASE_URL. Kastar ConfigurationError om den saknas."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL är inte satt.\n"
            "Skapa en .env-fil i samma mapp som db.py och lägg till:\n"
            "  DATABASE_URL=postgresql://anvandare@localhost:5432/riksdagstryck\n"
            "  # eller: DATABASE_URL=sqlite:///danmark_cache.db\n"
            "Se config.example.env för fullständigt exempel."
        )
    if not (url.startswith("postgresql") or url.startswith("sqlite:///")):
        raise RuntimeError(
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
        raise RuntimeError(
            f"Schemafilen saknas: {sokvag}\n"
            "Kontrollera att db/schema_postgres.sql och db/schema_sqlite.sql finns."
        )


# Migration-block — töms inför första GitHub-publicering (alla kolumner och index
# finns redan i bas-schemat). Framtida schemaändringar läggs till här som
# ALTER TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
_MIGRATION_POSTGRES = ""

_MIGRATION_SQLITE = ""


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
) -> int:
    """Infogar eller uppdaterar ett dokument. Returnerar dess id."""
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
                        fulltext_md           = COALESCE(EXCLUDED.fulltext_md, {p}dokument.fulltext_md),
                        status                = COALESCE(EXCLUDED.status, {p}dokument.status),
                        giltig_till           = COALESCE(EXCLUDED.giltig_till, {p}dokument.giltig_till),
                        synkad                = EXCLUDED.synkad
                    RETURNING id""",
                varden
            )
            return cur.fetchone()[0]
    else:
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
                        fulltext_md           = COALESCE(excluded.fulltext_md, dokument.fulltext_md),
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


