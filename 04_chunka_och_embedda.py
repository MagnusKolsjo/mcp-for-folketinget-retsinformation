"""
04_chunka_och_embedda.py — Chunkning och embedding för dansk riksdags- och rättsdata.

Läser fulltext_md (och resume) från dansk.dokument, delar upp i chunks och
genererar embeddings med intfloat/multilingual-e5-base (768 dim).

Kör efter 02_synka_oda.py fas 2 (när fulltext finns i DB).
Kan köras parallellt med fas 2 — hoppar över dokument utan fulltext.

Användning:
  python3 04_chunka_och_embedda.py            # Chunka + embedda allt som saknas
  python3 04_chunka_och_embedda.py --batchstorlek 64
  python3 04_chunka_och_embedda.py --bara-resume   # Embedda bara resume (snabbt test)
"""

import argparse
import hashlib
import logging
import os
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

_LOG_DIR = _SCRIPT_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(_LOG_DIR / "embedda.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

import db
from tyst_fd import tysta_fd

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

EMBEDDING_MODELL = os.getenv("EMBEDDING_MODELL", "intfloat/multilingual-e5-base")

# 1500 tecken är medvetet dubbla SKILL-defaulten (800) eftersom dansk lagtext
# har långa stycken som tappar sammanhang vid kortare chunks.
CHUNK_STORLEK  = int(os.getenv("CHUNK_MAX_TECKEN", "1500"))
CHUNK_OVERLAPP = int(os.getenv("CHUNK_OVERLAPP_TECKEN", "200"))
BATCH_STORLEK  = int(os.getenv("EMBEDDING_BATCH_STORLEK", "32"))

# ---------------------------------------------------------------------------
# Chunkning
# ---------------------------------------------------------------------------

def _chunka_text(text: str, storlek: int = CHUNK_STORLEK, overlapp: int = CHUNK_OVERLAPP) -> list[str]:
    """
    Delar upp text i överlappande chunks på styckenivå.
    Försöker bryta vid styckegränser (dubbla radbrytningar).
    """
    if not text or not text.strip():
        return []

    # Dela vid stycken
    stycken = [s.strip() for s in text.split("\n\n") if s.strip()]
    chunks  = []
    current = ""

    for stycke in stycken:
        if len(current) + len(stycke) + 2 <= storlek:
            current = (current + "\n\n" + stycke).strip()
        else:
            if current:
                chunks.append(current)
            # Börja nytt chunk med överlapp från föregående
            if chunks and overlapp > 0:
                overlapp_text = chunks[-1][-overlapp:] if len(chunks[-1]) > overlapp else chunks[-1]
                current = overlapp_text + "\n\n" + stycke
            else:
                current = stycke

    if current:
        chunks.append(current)

    # Säkerhetsnät: om ett enskilt stycke är längre än max, dela på ord
    resultat = []
    for chunk in chunks:
        if len(chunk) <= storlek:
            resultat.append(chunk)
        else:
            for i in range(0, len(chunk), storlek - overlapp):
                del_chunk = chunk[i:i + storlek]
                if del_chunk.strip():
                    resultat.append(del_chunk.strip())

    return resultat


# ---------------------------------------------------------------------------
# Embedding-modell (lat inläsning)
# ---------------------------------------------------------------------------

_modell = None
_modell_las = threading.Lock()


def _hamta_modell():
    """Laddar embeddingmodellen vid första anropet.

    MCP-servern anropar funktionen från flera arbetstrådar. Dubbelkontrollerad
    låsning gör att bara en tråd laddar modellen; övriga väntar på låset och
    ser sedan den färdiga modellen. Efter första inläsningen tas låset aldrig.
    """
    global _modell
    if _modell is None:
        with _modell_las:
            if _modell is None:
                logger.info("Laddar embeddingmodell: %s", EMBEDDING_MODELL)
                # tqdm och sentence-transformers skriver förbi sys.stdout
                with tysta_fd(_LOG_DIR / "modell_laddning.log"):
                    from sentence_transformers import SentenceTransformer
                    modell = SentenceTransformer(EMBEDDING_MODELL)
                # Metoden bytte namn i sentence-transformers 6; båda namnen stöds
                dimension = getattr(modell, "get_embedding_dimension", None) \
                    or modell.get_sentence_embedding_dimension
                logger.info("Modell laddad. Vektordimension: %d", dimension())
                _modell = modell
    return _modell


def _generera_embeddings(texter: list[str]) -> list[list[float]]:
    """Genererar embeddings för en lista texter."""
    modell = _hamta_modell()
    with tysta_fd(_LOG_DIR / "embedding.log"):
        vektorer = modell.encode(texter, batch_size=BATCH_STORLEK, show_progress_bar=False)
    return vektorer.tolist()


# ---------------------------------------------------------------------------
# Spara chunks och embeddings
# ---------------------------------------------------------------------------

def _chunktext(resume: str | None, fulltext: str | None, bara_resume: bool = False) -> str:
    """Texten som chunkas: resume följt av fulltexten, eller den som finns.

    Samma regel som db.CHUNKTEXT_SQL; ändras den ena måste den andra följa,
    annars chunkas alla dokument om.
    """
    if bara_resume:
        return resume or ""
    if resume and fulltext:
        return resume + "\n\n" + fulltext
    return fulltext or resume or ""


def _text_hash(text: str) -> str:
    """md5 av texten i UTF-8, samma värde som PostgreSQL:s md5() ger."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _spara_chunks_och_embeddings(dok_id: int, chunks: list[str],
                                 vektorer: list[list[float]], text_hash: str) -> None:
    """Ersätter dokumentets chunks och embeddings och sparar textens hash.

    Allt sker i en transaktion: gamla chunks tas bort (embeddings följer med
    via ON DELETE CASCADE), nya skrivs, och chunk_hash sätts. Avbryts något
    står dokumentet kvar med sina gamla chunks och gamla hash och väljs igen
    vid nästa körning.
    """
    p  = db._prefix()
    pg = db._ar_postgres()
    ph = "%s" if pg else "?"
    with db._cursor() as cur:
        cur.execute(f"DELETE FROM {p}chunks WHERE dok_id = {ph}", (dok_id,))
        typ = db.vektortyp(cur) if pg else None   # vector före konverteringen, halfvec efter
        for nr, (text, vektor) in enumerate(zip(chunks, vektorer)):
            if pg:
                cur.execute(
                    f"INSERT INTO {p}chunks (dok_id, chunk_nr, text) VALUES (%s, %s, %s) RETURNING id",
                    (dok_id, nr, text)
                )
                chunk_id = cur.fetchone()[0]
                cur.execute(
                    f"INSERT INTO {p}embeddings (chunk_id, vektor) VALUES (%s, %s::{typ})",
                    (chunk_id, str(vektor))
                )
            else:
                # SQLite-installationen har ingen embeddings-tabell
                cur.execute(
                    f"INSERT INTO {p}chunks (dok_id, chunk_nr, text) VALUES (?, ?, ?)",
                    (dok_id, nr, text)
                )
        cur.execute(f"UPDATE {p}dokument SET chunk_hash = {ph} WHERE id = {ph}", (text_hash, dok_id))


# ---------------------------------------------------------------------------
# Huvudlogik
# ---------------------------------------------------------------------------

def _dokument_att_chunka() -> list[dict]:
    """Dokument vars text har ändrats sedan de chunkades, eller aldrig chunkats.

    chunk_hash är md5 av texten vid senaste chunkningen. I PostgreSQL görs
    jämförelsen i databasen; SQLite saknar md5(), och där jämförs i Python.

    Ett dokument med chunks men utan chunk_hash har chunkats före kolumnen
    fanns och antas vara aktuellt, så att inget embeddas om i onödan innan
    db.migrera_data() gett det en hash.
    """
    p = db._prefix()
    with db._cursor() as cur:
        if db._ar_postgres():
            cur.execute(
                f"""SELECT d.id, d.titel, d.resume, d.fulltext_md
                    FROM {p}dokument d
                    WHERE (NULLIF(d.resume, '') IS NOT NULL OR NULLIF(d.fulltext_md, '') IS NOT NULL)
                      AND CASE WHEN d.chunk_hash IS NULL
                               THEN NOT EXISTS (SELECT 1 FROM {p}chunks c WHERE c.dok_id = d.id)
                               ELSE d.chunk_hash <> md5({db.CHUNKTEXT_SQL}) END
                    ORDER BY d.id ASC"""
            )
            rader = cur.fetchall()
        else:
            cur.execute(
                f"""SELECT d.id, d.titel, d.resume, d.fulltext_md, d.chunk_hash,
                           EXISTS (SELECT 1 FROM {p}chunks c WHERE c.dok_id = d.id)
                    FROM {p}dokument d
                    WHERE COALESCE(d.resume, '') <> '' OR COALESCE(d.fulltext_md, '') <> ''
                    ORDER BY d.id ASC"""
            )
            rader = [r[:4] for r in cur.fetchall()
                     if ((not r[5]) if r[4] is None
                         else r[4] != _text_hash(_chunktext(r[2], r[3])))]
    return [{"id": r[0], "titel": r[1], "resume": r[2], "fulltext_md": r[3]} for r in rader]


def chunka_och_embedda(bara_resume: bool = False):
    """
    Chunkar och embeddar dokument som aldrig chunkats eller vars text ändrats
    sedan förra chunkningen, till exempel när fas 2 i ODA-synken ersatt ett
    ärendes resume med PDF-texten. Gamla chunks och embeddings ersätts.
    bara_resume=True: använder bara resume-fältet (snabbare, för test).
    """
    att_behandla = _dokument_att_chunka()

    totalt  = len(att_behandla)
    lyckade = 0
    logger.info("Chunkning: %d dokument att behandla (nya eller med ändrad text)", totalt)

    if totalt == 0:
        logger.info("Inga dokument att chunka — allt redan klart eller saknar text.")
        return

    # Ladda modellen nu (en gång, inte per dokument)
    _hamta_modell()

    for i, dok in enumerate(att_behandla, 1):
        dok_id = dok["id"]
        titel  = (dok["titel"] or "")[:60]
        text   = _chunktext(dok.get("resume"), dok.get("fulltext_md"), bara_resume)
        chunks = _chunka_text(text) if text.strip() else []

        try:
            # Ger texten inga chunks sparas ändå hashen (och gamla chunks tas
            # bort), så att dokumentet inte väljs vid varje körning.
            vektorer = _generera_embeddings(chunks) if chunks else []
            _spara_chunks_och_embeddings(dok_id, chunks, vektorer, _text_hash(text))
            lyckade += 1

            if i % 50 == 0 or i == totalt:
                logger.info("[%d/%d] %s — %d chunks", i, totalt, titel, len(chunks))

        except Exception as e:
            logger.error("[%d/%d] Fel för dok_id=%d (%s): %s", i, totalt, dok_id, titel, e)

    db.spara_sync_status("embedding_senast_kord", db._now())
    logger.info("Embedding klar: %d/%d lyckade", lyckade, totalt)


# ---------------------------------------------------------------------------
# Semantisk sökning (används av mcp_server.py)
# ---------------------------------------------------------------------------

def semantisk_sok(sokterm: str, limit: int = 20) -> list[dict]:
    """
    Semantisk sökning via pgvector — hittar chunks närmast söktermens embedding.
    Returnerar topp-N dokument (avduplicerade på dok_id), närmast först.
    """
    if not db._ar_postgres():
        return []
    return semantisk_sok_med_vektor(_generera_embeddings([sokterm])[0], limit)


# Kandidatchunks per sökt dokument. Ett dokument har ofta flera närliggande
# chunks, så fler chunks än dokument behövs för att få ihop limit dokument.
_KANDIDATER_PER_DOKUMENT = 10
_MAX_KANDIDATER = 1000


def semantisk_sok_med_vektor(vektor: list[float], limit: int = 20) -> list[dict]:
    """
    Dokumentsökning med en färdig frågevektor.

    Den inre frågan hämtar de närmaste chunkarna med ORDER BY avstånd och
    LIMIT, den form som HNSW-indexet kan besvara utan att läsa alla vektorer.
    Chunkarna grupperas sedan per dokument, och varje dokument får sin
    närmaste chunks avstånd. Räcker kandidaterna inte till limit olika
    dokument dubblas antalet, upp till _MAX_KANDIDATER. Utan index (före
    konverteringen) blir samma fråga en exakt genomläsning av alla vektorer.
    """
    p = db._prefix()
    vektor_str = str(vektor)
    kandidater = max(limit * _KANDIDATER_PER_DOKUMENT, 100)
    while True:
        with db._cursor() as cur:
            typ = db.vektortyp(cur)
            db.sokinstallningar(cur, kandidater)
            cur.execute(
                f"""WITH narmast AS (
                        SELECT e.chunk_id, (e.vektor <=> %s::{typ}) AS avstand
                        FROM {p}embeddings e
                        ORDER BY e.vektor <=> %s::{typ}
                        LIMIT %s
                    )
                    SELECT * FROM (
                        SELECT DISTINCT ON (d.id)
                            d.id, d.kalla, d.beteckning, d.typ, d.titel, d.titelkort,
                            d.periode, d.datum, d.url, d.retsinformationsurl,
                            d.lovnummer, d.resume, d.paragrafnummer,
                            n.avstand
                        FROM narmast n
                        JOIN {p}chunks c ON c.id = n.chunk_id
                        JOIN {p}dokument d ON d.id = c.dok_id
                        ORDER BY d.id, n.avstand ASC
                    ) narmaste_per_dokument
                    ORDER BY avstand ASC
                    LIMIT %s""",
                (vektor_str, vektor_str, kandidater, limit)
            )
            kolumner = [desc[0] for desc in cur.description]
            rader = [dict(zip(kolumner, rad)) for rad in cur.fetchall()]
        if len(rader) >= limit or kandidater >= _MAX_KANDIDATER:
            return rader
        kandidater = min(kandidater * 2, _MAX_KANDIDATER)


def semantisk_sok_i_dokument(dok_id: int, fraga: str, limit: int = 5) -> dict:
    """
    Semantisk sökning via pgvector inom ett enskilt cachat dokument.

    Returnerar ett dict med dokumentmetadata + topp-N chunk-träffar sorterade
    efter cosinus-avstånd. Om dokumentet inte finns eller saknar chunks
    returneras ett fel-fält. Kräver PostgreSQL med pgvector — SQLite-läge
    har inga embeddings och stöds inte.
    """
    if not db._ar_postgres():
        return {"fel": "Semantisk sökning kräver PostgreSQL med pgvector — SQLite-läge stöds inte."}

    p = db._prefix()

    # Verifiera att dokumentet finns och hämta metadata
    with db._cursor() as cur:
        cur.execute(
            f"SELECT id, kalla, beteckning, typ, titel FROM {p}dokument WHERE id = %s",
            (dok_id,)
        )
        rad = cur.fetchone()
        if not rad:
            return {"fel": f"Dokument med dok_id={dok_id} hittades inte i databasen."}
        dok_meta = {
            "dok_id":     rad[0],
            "kalla":      rad[1],
            "beteckning": rad[2],
            "typ":        rad[3],
            "titel":      rad[4],
        }

        # Räkna chunks så svaret blir transparent när dokumentet är litet
        cur.execute(f"SELECT COUNT(*) FROM {p}chunks WHERE dok_id = %s", (dok_id,))
        antal_chunks = cur.fetchone()[0]

    if antal_chunks == 0:
        return {**dok_meta, "antal_chunks": 0,
                "fel": "Dokumentet har inga chunks — fulltext saknas eller chunkning ej körd."}

    # Generera embedding för frågan och kör vektorsökningen scoped till dok_id
    vektor = _generera_embeddings([fraga])[0]

    with db._cursor() as cur:
        typ = db.vektortyp(cur)
        # "+ 0" hindrar planeraren från att använda HNSW-indexet: inom ett
        # dokument är en exakt sortering av dess chunks snabb, medan indexet
        # plus dokumentfiltret kan ge för få träffar.
        cur.execute(
            f"""SELECT c.chunk_nr, c.text,
                       (e.vektor <=> %s::{typ}) AS avstand
                FROM {p}embeddings e
                JOIN {p}chunks c ON c.id = e.chunk_id
                WHERE c.dok_id = %s
                ORDER BY (e.vektor <=> %s::{typ}) + 0
                LIMIT %s""",
            (str(vektor), dok_id, str(vektor), limit)
        )
        kolumner = [desc[0] for desc in cur.description]
        traffar = [dict(zip(kolumner, rad)) for rad in cur.fetchall()]

    return {
        **dok_meta,
        "fraga":         fraga,
        "antal_chunks":  antal_chunks,
        "antal_traffar": len(traffar),
        "traffar":       traffar,
    }


# ---------------------------------------------------------------------------
# Huvud
# ---------------------------------------------------------------------------

def main():
    global BATCH_STORLEK
    parser = argparse.ArgumentParser(description="Chunkning och embedding för dansk riksdags- och rättsdata")
    parser.add_argument("--batchstorlek", type=int, default=BATCH_STORLEK,
                        help=f"Embedding-batch-storlek (standard {BATCH_STORLEK})")
    parser.add_argument("--bara-resume", action="store_true",
                        help="Embedda bara resume-fältet (snabbt test utan fulltext)")
    args = parser.parse_args()

    BATCH_STORLEK = args.batchstorlek

    db.initialisera_schema()
    db.migrera_data()   # engångsuppdateringar efter uppgradering; snabb när de redan körts
    logger.info("=== Chunkning och embedding startad ===")
    chunka_och_embedda(bara_resume=args.bara_resume)
    logger.info("=== Klar ===")


if __name__ == "__main__":
    main()
