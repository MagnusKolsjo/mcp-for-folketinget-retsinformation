"""
04_chunka_och_embedda.py — Chunkning och embedding för ström 14 (Danmark).

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
import logging
import os
import sys
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
# Embedding-modell (lazy-laddning)
# ---------------------------------------------------------------------------

_modell = None


def _hamta_modell():
    """Laddar embeddingmodellen vid första anropet."""
    global _modell
    if _modell is None:
        logger.info("Laddar embeddingmodell: %s", EMBEDDING_MODELL)
        import contextlib, os
        # Tysta tqdm och eventuell FD1-output från sentence-transformers
        log_vag = str(_LOG_DIR / "modell_laddning.log")
        save_out = os.dup(1)
        save_err = os.dup(2)
        log_fd = os.open(log_vag, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            from sentence_transformers import SentenceTransformer
            _modell = SentenceTransformer(EMBEDDING_MODELL)
        finally:
            os.dup2(save_out, 1)
            os.dup2(save_err, 2)
            os.close(save_out)
            os.close(save_err)
            os.close(log_fd)
        logger.info("Modell laddad. Vektordimension: %d", _modell.get_sentence_embedding_dimension())
    return _modell


def _generera_embeddings(texter: list[str]) -> list[list[float]]:
    """Genererar embeddings för en lista texter."""
    modell = _hamta_modell()
    import os
    log_vag = str(_LOG_DIR / "embedding.log")
    save_out = os.dup(1)
    save_err = os.dup(2)
    log_fd = os.open(log_vag, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        vektorer = modell.encode(texter, batch_size=BATCH_STORLEK, show_progress_bar=False)
    finally:
        os.dup2(save_out, 1)
        os.dup2(save_err, 2)
        os.close(save_out)
        os.close(save_err)
        os.close(log_fd)
    return vektorer.tolist()


# ---------------------------------------------------------------------------
# Spara chunks och embeddings
# ---------------------------------------------------------------------------

def _spara_chunks(dok_id: int, chunks: list[str]) -> list[int]:
    """Sparar chunks i DB och returnerar deras id:n."""
    p  = db._prefix()
    ph = "%s" if db._ar_postgres() else "?"
    chunk_ids = []
    with db._cursor() as cur:
        # Radera gamla chunks för detta dokument
        cur.execute(f"DELETE FROM {p}chunks WHERE dok_id = {ph}", (dok_id,))
        for nr, text in enumerate(chunks):
            if db._ar_postgres():
                cur.execute(
                    f"INSERT INTO {p}chunks (dok_id, chunk_nr, text) VALUES (%s, %s, %s) RETURNING id",
                    (dok_id, nr, text)
                )
                chunk_ids.append(cur.fetchone()[0])
            else:
                cur.execute(
                    f"INSERT OR REPLACE INTO {p}chunks (dok_id, chunk_nr, text) VALUES (?, ?, ?)",
                    (dok_id, nr, text)
                )
                cur.execute(
                    f"SELECT id FROM {p}chunks WHERE dok_id = {ph} AND chunk_nr = {ph}",
                    (dok_id, nr)
                )
                chunk_ids.append(cur.fetchone()[0])
    return chunk_ids


def _spara_embeddings(chunk_ids: list[int], vektorer: list[list[float]]):
    """Sparar embeddings i danmark.embeddings (bara PostgreSQL)."""
    if not db._ar_postgres():
        return  # SQLite-installation har ingen pgvector
    p = db._prefix()
    with db._cursor() as cur:
        for chunk_id, vektor in zip(chunk_ids, vektorer):
            cur.execute(
                f"""INSERT INTO {p}embeddings (chunk_id, vektor)
                    VALUES (%s, %s::vector)
                    ON CONFLICT (chunk_id) DO UPDATE SET vektor = EXCLUDED.vektor""",
                (chunk_id, str(vektor))
            )


# ---------------------------------------------------------------------------
# Huvudlogik
# ---------------------------------------------------------------------------

def chunka_och_embedda(bara_resume: bool = False):
    """
    Hämtar dokument utan chunks, chunkar deras text och genererar embeddings.
    bara_resume=True: använder bara resume-fältet (snabbare, för test).
    """
    p  = db._prefix()
    ph = "%s" if db._ar_postgres() else "?"

    # Hämta dokument som saknar chunks
    with db._cursor() as cur:
        cur.execute(
            f"""SELECT d.id, d.titel, d.resume, d.fulltext_md
                FROM {p}dokument d
                LEFT JOIN {p}chunks c ON c.dok_id = d.id
                WHERE c.id IS NULL
                  AND (d.resume IS NOT NULL OR d.fulltext_md IS NOT NULL)
                ORDER BY d.id ASC"""
        )
        att_behandla = [
            {"id": r[0], "titel": r[1], "resume": r[2], "fulltext_md": r[3]}
            for r in cur.fetchall()
        ]

    totalt  = len(att_behandla)
    lyckade = 0
    logger.info("Chunkning: %d dokument att behandla", totalt)

    if totalt == 0:
        logger.info("Inga dokument att chunka — allt redan klart eller saknar text.")
        return

    # Ladda modellen nu (en gång, inte per dokument)
    _hamta_modell()

    for i, dok in enumerate(att_behandla, 1):
        dok_id = dok["id"]
        titel  = (dok["titel"] or "")[:60]

        # Välj text: fulltext_md om tillgängligt, annars resume
        if bara_resume or not dok.get("fulltext_md"):
            text = dok.get("resume") or ""
        else:
            text = dok["fulltext_md"]

        # Lägg alltid till resume i början om det finns och vi kör fulltext
        if not bara_resume and dok.get("resume") and dok.get("fulltext_md"):
            text = dok["resume"] + "\n\n" + dok["fulltext_md"]

        if not text.strip():
            continue

        chunks = _chunka_text(text)
        if not chunks:
            continue

        try:
            vektorer  = _generera_embeddings(chunks)
            chunk_ids = _spara_chunks(dok_id, chunks)
            if db._ar_postgres():
                _spara_embeddings(chunk_ids, vektorer)
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
    Returnerar topp-N dokument (avduplicerade på dok_id).
    """
    if not db._ar_postgres():
        return []

    vektor = _generera_embeddings([sokterm])[0]
    p = db._prefix()

    with db._cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT ON (c.dok_id)
                    d.id, d.kalla, d.beteckning, d.typ, d.titel, d.titelkort,
                    d.periode, d.datum, d.url, d.retsinformationsurl,
                    d.lovnummer, d.resume, d.paragrafnummer,
                    (e.vektor <=> %s::vector) AS avstand
                FROM {p}embeddings e
                JOIN {p}chunks c ON c.id = e.chunk_id
                JOIN {p}dokument d ON d.id = c.dok_id
                ORDER BY c.dok_id, avstand ASC
                LIMIT %s""",
            (str(vektor), limit)
        )
        kolumner = [desc[0] for desc in cur.description]
        return [dict(zip(kolumner, rad)) for rad in cur.fetchall()]


# ---------------------------------------------------------------------------
# Huvud
# ---------------------------------------------------------------------------

def main():
    global BATCH_STORLEK
    parser = argparse.ArgumentParser(description="Chunkning och embedding för ström 14")
    parser.add_argument("--batchstorlek", type=int, default=BATCH_STORLEK,
                        help=f"Embedding-batch-storlek (standard {BATCH_STORLEK})")
    parser.add_argument("--bara-resume", action="store_true",
                        help="Embedda bara resume-fältet (snabbt test utan fulltext)")
    args = parser.parse_args()

    BATCH_STORLEK = args.batchstorlek

    db.initialisera_schema()
    logger.info("=== Chunkning och embedding startad ===")
    chunka_och_embedda(bara_resume=args.bara_resume)
    logger.info("=== Klar ===")


if __name__ == "__main__":
    main()
