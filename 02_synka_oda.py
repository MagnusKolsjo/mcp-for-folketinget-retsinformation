"""
02_synka_oda.py — Synkskript för Folketing ODA (ström 14, Danmark).

Fas 1: Hämtar metadata för alla sager i ODA och lagrar i DB.
Fas 2: Laddar hem och extraherar fulltext-PDF för lovforslag (typeid=3)
        och beslutningsforslag (typeid=4).

Kör manuellt vid initial laddning (tar ett tag). Daglig delta-synk via launchd.

Användning:
  python3 02_synka_oda.py              # Kör fas 1 + 2
  python3 02_synka_oda.py --fas 1      # Bara metadata
  python3 02_synka_oda.py --fas 2      # Bara fulltext (förutsätter fas 1 klar)
  python3 02_synka_oda.py --installera-schema  # Installerar launchd-jobb
"""

import argparse
import contextlib
import logging
import os
import sys
import time
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

# Loggning till fil och stderr
_LOG_DIR = _SCRIPT_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(_LOG_DIR / "synk_oda.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

import db
import oda_lib

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ODA_BAS_URL = "https://oda.ft.dk/api"

PDF_CACHE_DIR = Path(os.getenv("PDF_CACHE_DIR", str(_SCRIPT_DIR / "pdf_cache")))
if not PDF_CACHE_DIR.is_absolute():
    PDF_CACHE_DIR = _SCRIPT_DIR / PDF_CACHE_DIR
PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Typer att ladda hem fulltext för
FULLTEXT_TYPER = {3, 4}   # 3=lovforslag, 4=beslutningsforslag
SIDSTORLEK     = 100      # ODA-paginering: poster per anrop
FORDROJ_ODA    = 0.15     # sekunder mellan ODA-API-anrop
FORDROJ_PDF    = 1.0      # sekunder mellan PDF-nedladdningar (vara snäll mot ft.dk)

# ---------------------------------------------------------------------------
# HTTP-hjälpare
# ---------------------------------------------------------------------------

import httpx

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "mcp-for-folketinget-retsinformation/1.0 (+https://github.com/MagnusKolsjo/mcp-for-folketinget-retsinformation)",
}


def _oda_get(endpoint: str, params: dict = None) -> dict:
    """GET mot ODA med automatisk $format=json."""
    if params is None:
        params = {}
    params.setdefault("$format", "json")
    url = f"{ODA_BAS_URL}/{endpoint}"
    for forsok in range(3):
        try:
            resp = httpx.get(url, params=params, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if forsok == 2:
                raise
            logger.warning("ODA GET misslyckades (försök %d/3): %s — %s", forsok + 1, url, e)
            time.sleep(2 ** forsok)


# ---------------------------------------------------------------------------
# PDF-pipeline
# ---------------------------------------------------------------------------

try:
    from curl_cffi import requests as cf_requests
    _CURL_CFFI_OK = True
except ImportError:
    _CURL_CFFI_OK = False
    logger.error("curl-cffi saknas — installera med: pip install curl-cffi")


@contextlib.contextmanager
def _tysta_stdout():
    """OS-nivå redirigering av FD 1+2 till loggfil (skyddar MCP-protokollet)."""
    log_vag = _LOG_DIR / "subprocess.log"
    save_out = os.dup(1)
    save_err = os.dup(2)
    log_fd   = os.open(str(log_vag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        yield
    finally:
        os.dup2(save_out, 1)
        os.dup2(save_err, 2)
        os.close(save_out)
        os.close(save_err)
        os.close(log_fd)


def _ladda_ned_pdf(url: str) -> bytes | None:
    """Laddar ned PDF från ft.dk med curl-cffi (kringgår Cloudflare)."""
    if not _CURL_CFFI_OK:
        return None
    try:
        resp = cf_requests.get(url, impersonate="chrome", timeout=60)
        resp.raise_for_status()
        if "application/pdf" not in resp.headers.get("content-type", ""):
            # Ibland returneras en HTML-felsida
            logger.warning("Oväntat Content-Type för %s: %s", url, resp.headers.get("content-type"))
            return None
        return resp.content
    except Exception as e:
        logger.warning("PDF-nedladdning misslyckades: %s — %s", url, e)
        return None


def _extrahera_text(pdf_bytes: bytes) -> str | None:
    """Extraherar text från PDF-bytes med pymupdf4llm."""
    try:
        import pymupdf4llm
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp = f.name
        try:
            with _tysta_stdout():
                text = pymupdf4llm.to_markdown(tmp)
            return text.strip() or None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except ImportError:
        logger.error("pymupdf4llm saknas")
        return None
    except Exception as e:
        logger.error("Textextraktion misslyckades: %s", e)
        return None


# ---------------------------------------------------------------------------
# Fas 1 — Metadata för alla sager
# ---------------------------------------------------------------------------

def synka_sager_metadata():
    """
    Hämtar metadata för alla sager i ODA och lagrar i DB.
    Delta-synk: hämtar sager med id > senaste kända id.
    """
    senaste_id = int(db.hamta_sync_status("oda_senaste_sagid") or "0")
    logger.info("Fas 1: Hämtar sager med id > %d", senaste_id)

    totalt_nya  = 0
    hogsta_id   = senaste_id
    skip        = 0

    # ODA stöder inte filter på id direkt i alla fall — vi paginerar från senaste id
    filtrering = f"id gt {senaste_id}" if senaste_id > 0 else None

    while True:
        params = {
            "$top":     str(SIDSTORLEK),
            "$skip":    str(skip),
            "$orderby": "id asc",
            "$select":  "id,typeid,statusid,periodeid,titel,titelkort,opdateringsdato,nummerprefix,nummernumerisk,nummerpostfix,resume,afstemningskonklusion,lovnummer,retsinformationsurl,paragrafnummer,paragraf,afgørelse,begrundelse,baggrundsmateriale",
        }
        if filtrering:
            params["$filter"] = filtrering

        try:
            data = _oda_get("Sag", params)
        except Exception as e:
            logger.error("ODA Sag-hämtning misslyckades vid skip=%d: %s", skip, e)
            break

        poster = data.get("value", [])
        if not poster:
            break

        for sag in poster:
            sagid    = sag.get("id")
            typeid   = sag.get("typeid")
            periodeid = sag.get("periodeid")
            titel    = sag.get("titel", "")
            # Använd ärendets bästa tillgängliga datum (afgørelsesdato →
            # lovnummerdato → rådsmødedato → opdateringsdato). Tidigare användes
            # bara opdateringsdato (senast-synkad i ODA) vilket gav missvisande
            # synktid istället för ärendets verkliga datum för historiska sager.
            dato     = oda_lib.basta_datum(sag)
            # Beteckning: t.ex. "L 183" — bygg från prefix + nummer
            nummer_prefix  = sag.get("nummerprefix", "").strip()
            nummer_num     = sag.get("nummernumerisk", "").strip()
            nummer_postfix = sag.get("nummerpostfix", "").strip()
            if nummer_prefix and nummer_num:
                beteckning = f"{nummer_prefix} {nummer_num}{nummer_postfix}".strip()
            else:
                beteckning = sag.get("nummer") or None

            resume_text     = sag.get("resume") or None
            afstemning_text = sag.get("afstemningskonklusion") or None
            lovnummer_text  = sag.get("lovnummer") or None
            rin_url         = sag.get("retsinformationsurl") or None
            titelkort       = sag.get("titelkort") or None
            paragrafnummer  = str(sag.get("paragrafnummer") or "").strip() or None
            paragraf_text   = sag.get("paragraf") or None
            afgoerelse_text = sag.get("afgørelse") or None
            begrundelse_text = sag.get("begrundelse") or None
            baggrund_text   = sag.get("baggrundsmateriale") or None

            db.upsert_dokument(
                kalla                 = "oda",
                extern_id             = str(sagid),
                beteckning            = beteckning,
                typ                   = _typeid_till_navn(typeid),
                titel                 = titel,
                titelkort             = titelkort,
                periode               = str(periodeid) if periodeid else None,
                datum                 = dato,
                url                   = None,           # PDF-URL sätts i fas 2
                retsinformationsurl   = rin_url,
                lovnummer             = lovnummer_text,
                resume                = resume_text,
                afstemningskonklusion = afstemning_text,
                paragrafnummer        = paragrafnummer,
                paragraf              = paragraf_text,
                afgoerelse            = afgoerelse_text,
                begrundelse           = begrundelse_text,
                baggrundsmateriale    = baggrund_text,
                fulltext_md           = resume_text,    # resume som initialt sökbart fält
            )

            if sagid > hogsta_id:
                hogsta_id = sagid
            totalt_nya += 1

        logger.info("  Hämtat %d sager (skip=%d, högsta id=%d)", len(poster), skip, hogsta_id)
        skip += SIDSTORLEK
        time.sleep(FORDROJ_ODA)

        if len(poster) < SIDSTORLEK:
            break

    if hogsta_id > senaste_id:
        db.spara_sync_status("oda_senaste_sagid", str(hogsta_id))

    logger.info("Fas 1 klar: %d nya/uppdaterade sager. Högsta sagid: %d", totalt_nya, hogsta_id)
    return totalt_nya


# ---------------------------------------------------------------------------
# Fas 2 — Fulltext för lovforslag + beslutningsforslag
# ---------------------------------------------------------------------------

def synka_fulltext():
    """
    Hämtar fulltext-PDF för sager av typ lovforslag och beslutningsforslag
    som saknar fulltext i databasen.
    """
    if not _CURL_CFFI_OK:
        logger.error("curl-cffi saknas — avbryter fas 2")
        return

    p = db._prefix()
    ph = "%s" if db._ar_postgres() else "?"

    # Hämta sager utan fulltext och av rätt typ
    with db._cursor() as cur:
        typer_sql = ", ".join([f"'{_typeid_till_navn(t)}'" for t in FULLTEXT_TYPER])
        cur.execute(
            f"""SELECT id, extern_id, typ, titel
                FROM {p}dokument
                WHERE kalla = {ph}
                  AND fulltext_md IS NULL
                  AND typ IN ({typer_sql})
                ORDER BY id ASC""",
            ("oda",)
        )
        att_behandla = [
            {"id": r[0], "extern_id": r[1], "typ": r[2], "titel": r[3]}
            for r in cur.fetchall()
        ]

    totalt = len(att_behandla)
    logger.info("Fas 2: %d sager saknar fulltext — hämtar dokument och PDF", totalt)

    lyckade = 0
    misslyckade = 0

    for i, sag in enumerate(att_behandla, 1):
        sagid    = int(sag["extern_id"])
        dok_id   = sag["id"]

        logger.info("[%d/%d] sagid=%d: %s", i, totalt, sagid, sag["titel"][:60])

        # Steg 1: hämta SagDokument
        try:
            sd_data = _oda_get("SagDokument", {
                "$filter": f"sagid eq {sagid}",
                "$orderby": "dokumentid asc",
            })
            time.sleep(FORDROJ_ODA)
        except Exception as e:
            logger.warning("  SagDokument misslyckades: %s", e)
            misslyckade += 1
            continue

        sagdokument = sd_data.get("value", [])
        if not sagdokument:
            logger.info("  Inga dokument kopplade till sagid=%d", sagid)
            # Markera som behandlad med tom text för att inte försöka igen
            _uppdatera_fulltext(dok_id, "(ingen PDF hittad)")
            continue

        # Försök med de tre första dokumenten — ta det första som ger text
        text_hittad = False
        for sd in sagdokument[:5]:
            dokumentid = sd.get("dokumentid")
            if not dokumentid:
                continue

            # Steg 2: hämta fil-URL
            try:
                fil_data = _oda_get("Fil", {
                    "$filter": f"dokumentid eq {dokumentid}",
                    "$top": "1",
                })
                time.sleep(FORDROJ_ODA)
            except Exception as e:
                logger.warning("  Fil-hämtning misslyckades (dokumentid=%d): %s", dokumentid, e)
                continue

            filer = fil_data.get("value", [])
            if not filer:
                continue

            fil_url = filer[0].get("filurl")
            if not fil_url or not fil_url.endswith(".pdf"):
                continue

            # Steg 3: ladda ned PDF och extrahera text
            logger.info("  Hämtar PDF: %s", fil_url[-60:])
            pdf_bytes = _ladda_ned_pdf(fil_url)
            time.sleep(FORDROJ_PDF)

            if not pdf_bytes:
                continue

            text = _extrahera_text(pdf_bytes)
            if not text:
                logger.warning("  Tom text för %s", fil_url)
                continue

            # Lagra i DB — uppdatera url och fulltext
            with db._cursor() as cur:
                p = db._prefix()
                ph = "%s" if db._ar_postgres() else "?"
                cur.execute(
                    f"UPDATE {p}dokument SET url = {ph}, fulltext_md = {ph} WHERE id = {ph}",
                    (fil_url, text, dok_id)
                )

            logger.info("  ✓ %d tecken extraherade", len(text))
            lyckade += 1
            text_hittad = True
            break

        if not text_hittad:
            logger.warning("  Ingen PDF med text hittades för sagid=%d", sagid)
            misslyckade += 1
            _uppdatera_fulltext(dok_id, "(ingen PDF hittad)")

        # Spara checkpoint var 100:e sag
        if i % 100 == 0:
            db.spara_sync_status("oda_fulltext_progress", str(i))
            logger.info("  Checkpoint sparad: %d/%d behandlade", i, totalt)

    db.spara_sync_status("oda_fulltext_klar", datetime.now(timezone.utc).isoformat())
    logger.info(
        "Fas 2 klar: %d lyckade, %d misslyckade av %d sager",
        lyckade, misslyckade, totalt
    )


def _uppdatera_fulltext(dok_id: int, text: str):
    """Uppdaterar fulltext_md för ett dokument."""
    with db._cursor() as cur:
        p = db._prefix()
        ph = "%s" if db._ar_postgres() else "?"
        cur.execute(
            f"UPDATE {p}dokument SET fulltext_md = {ph} WHERE id = {ph}",
            (text, dok_id)
        )


# ---------------------------------------------------------------------------
# Hjälpfunktioner för typid-mappning
# ---------------------------------------------------------------------------

_TYPID_NAMN = {
    3:  "lovforslag",
    4:  "beslutningsforslag",
    5:  "foresporgsel",
    6:  "redegoerelse",
    7:  "aktueldebat",
    17: "ministersporgsmaal",
    20: "forhandlinger",
    31: "betaenkning",
}

_TYPID_BOKSTAV = {
    3:  "L",
    4:  "B",
    5:  "F",
    17: "§ 20",
}


def _typeid_till_navn(typeid: int | None) -> str | None:
    if typeid is None:
        return None
    return _TYPID_NAMN.get(typeid, f"type_{typeid}")


def _typeid_till_bokstav(typeid: int | None) -> str | None:
    if typeid is None:
        return None
    return _TYPID_BOKSTAV.get(typeid)


# ---------------------------------------------------------------------------
# Launchd-installation
# ---------------------------------------------------------------------------

def installera_launchd():
    """
    Installerar ett launchd-jobb som kör synk_daglig.sh kl. 04:30 varje dag.
    Shell-skriptet kör ODA + Retsinformation + embedding i rätt ordning.
    """
    plist_label = "se.magnuskolsjo.mcp-danmark-synk"
    plist_vag   = Path.home() / "Library" / "LaunchAgents" / f"{plist_label}.plist"
    shell_skript = _SCRIPT_DIR / "synk_daglig.sh"
    logg_vag     = _SCRIPT_DIR / "logs" / "launchd.log"

    plist_inneh = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{plist_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{shell_skript}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>4</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{logg_vag}</string>
    <key>StandardErrorPath</key>
    <string>{logg_vag}</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""
    plist_vag.parent.mkdir(parents=True, exist_ok=True)
    (_SCRIPT_DIR / "logs").mkdir(parents=True, exist_ok=True)
    plist_vag.write_text(plist_inneh, encoding="utf-8")
    print(f"Plist skapad: {plist_vag}")
    print(f"\nAktivera med:")
    print(f"  chmod +x {shell_skript}")
    print(f"  launchctl load {plist_vag}")
    print(f"\nKontrollera status:")
    print(f"  launchctl list | grep Danmark")
    print(f"\nAvaktivera med:")
    print(f"  launchctl unload {plist_vag}")


# ---------------------------------------------------------------------------
# Huvudprogram
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Synkskript för Folketing ODA")
    parser.add_argument("--fas", type=int, choices=[1, 2],
                        help="Kör bara fas 1 (metadata) eller fas 2 (fulltext)")
    parser.add_argument("--installera-schema", action="store_true",
                        help="Installerar launchd-jobb för daglig synk")
    args = parser.parse_args()

    if args.installera_schema:
        installera_launchd()
        return

    logger.info("=== ODA-synk startad ===")
    db.initialisera_schema()

    if args.fas is None or args.fas == 1:
        synka_sager_metadata()

    if args.fas is None or args.fas == 2:
        synka_fulltext()

    logger.info("=== ODA-synk avslutad ===")


if __name__ == "__main__":
    main()
