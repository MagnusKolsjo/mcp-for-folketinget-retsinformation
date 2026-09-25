"""
02_synka_oda.py — Synkskript för Folketing ODA.

Fas 1: Hämtar metadata för alla sager i ODA och lagrar i DB.
Fas 2: Laddar hem och extraherar fulltext-PDF för lovforslag (typeid=3)
        och beslutningsforslag (typeid=4).

Kör manuellt vid initial laddning (tar ett tag). Daglig delta-synk via launchd.

Fas 1 är inkrementell: den hämtar sager vars opdateringsdato ligger efter
förra lyckade körningen (checkpoint i sync_status), alltså både nya och
ändrade ärenden. Utan checkpoint, eller med --full, hämtas alla sager.

Användning:
  python3 02_synka_oda.py              # Kör fas 1 + 2
  python3 02_synka_oda.py --full       # Fas 1 hämtar alla sager
  python3 02_synka_oda.py --sedan 2026-09-22 --fas 1   # Sager ändrade sedan ett datum
  python3 02_synka_oda.py --fas 1      # Bara metadata
  python3 02_synka_oda.py --fas 2      # Bara fulltext (förutsätter fas 1 klar)
  python3 02_synka_oda.py --installera-schema  # Installerar launchd-jobb
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

from pdftext_skydd import extrahera_pdf

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


def _extrahera_text(pdf_bytes: bytes, *, kalla_id: str, kalla_url: str = "") -> str | None:
    """Extraherar text från PDF-bytes under minnes- och tidsvakt, med dansk OCR."""
    try:
        res = extrahera_pdf(pdf_bytes, prefix="DK", standardsprak="dan+eng",
                            kalla_id=kalla_id, kalla_url=kalla_url)
        return res.text.strip() or None
    except Exception as e:
        logger.error("Textextraktion misslyckades: %s", e)
        return None


# ---------------------------------------------------------------------------
# Fas 1 — Metadata för sager
# ---------------------------------------------------------------------------

# Synkstatusnyckel för den senaste opdateringsdato som en lyckad körning såg.
# Värdet är ODA:s egen tidsstämpel (dansk lokaltid utan zon), oförändrad, så
# att den kan skickas tillbaka i ett datetime-filter utan omräkning.
CHECKPOINT_NYCKEL = "oda_senaste_opdateringsdato"

_SAG_FALT = (
    "id,typeid,statusid,periodeid,titel,titelkort,opdateringsdato,nummer,"
    "nummerprefix,nummernumerisk,nummerpostfix,resume,afstemningskonklusion,"
    "lovnummer,retsinformationsurl,paragrafnummer,paragraf,afgørelse,"
    "begrundelse,baggrundsmateriale"
)


def _spara_sag(sag: dict) -> None:
    """Upsertar en sag i dokumenttabellen."""
    sagid     = sag.get("id")
    typeid    = sag.get("typeid")
    periodeid = sag.get("periodeid")
    # Ärendets bästa tillgängliga datum (afgørelsesdato → lovnummerdato →
    # rådsmødedato → opdateringsdato), inte bara senast uppdaterad i ODA.
    dato = oda_lib.basta_datum(sag)
    # Beteckning: t.ex. "L 183" — bygg från prefix + nummer
    nummer_prefix  = (sag.get("nummerprefix") or "").strip()
    nummer_num     = (sag.get("nummernumerisk") or "").strip()
    nummer_postfix = (sag.get("nummerpostfix") or "").strip()
    if nummer_prefix and nummer_num:
        beteckning = f"{nummer_prefix} {nummer_num}{nummer_postfix}".strip()
    else:
        beteckning = sag.get("nummer") or None

    resume_text = sag.get("resume") or None
    dok_id = db.upsert_dokument(
        kalla                 = "oda",
        extern_id             = str(sagid),
        beteckning            = beteckning,
        typ                   = _typeid_till_navn(typeid),
        titel                 = sag.get("titel") or "",
        titelkort             = sag.get("titelkort") or None,
        periode               = str(periodeid) if periodeid else None,
        datum                 = dato,
        url                   = None,           # PDF-URL sätts i fas 2
        retsinformationsurl   = sag.get("retsinformationsurl") or None,
        lovnummer             = sag.get("lovnummer") or None,
        resume                = resume_text,
        afstemningskonklusion = sag.get("afstemningskonklusion") or None,
        paragrafnummer        = str(sag.get("paragrafnummer") or "").strip() or None,
        paragraf              = sag.get("paragraf") or None,
        afgoerelse            = sag.get("afgørelse") or None,
        begrundelse           = sag.get("begrundelse") or None,
        baggrundsmateriale    = sag.get("baggrundsmateriale") or None,
        # En redan hämtad fulltext (PDF) får inte skrivas över när ett
        # ärende uppdateras; resume sätts som preliminär text nedan.
        fulltext_md           = None,
        behall_fulltext       = True,
    )
    # resume är sökbar text tills fas 2 hämtat PDF:en; fulltext_kalla
    # säger att den är preliminär.
    db.satt_resume_som_fulltext(dok_id)


def synka_sager_metadata(full: bool = False, sedan: str | None = None) -> bool:
    """
    Hämtar sager från ODA och lagrar dem i databasen. Returnerar True om
    körningen gick igenom utan fel.

    Inkrementellt (standard): sager med opdateringsdato efter checkpointen,
    alltså både nya och ändrade ärenden. Utan checkpoint görs en full synk.
    full=True: alla sager, oavsett checkpoint.
    sedan: starttidpunkt ('YYYY-MM-DD' eller ODA-tidsstämpel) i stället för
    checkpointen.

    Pagineringen sker på nyckel (opdateringsdato, id) respektive id, inte med
    $skip. Med $skip förskjuts sidorna när ett ärende uppdateras under
    körningen, och ett ärende kan då hoppas över utan att det märks.

    Checkpointen sätts till den största opdateringsdato som körningen såg och
    flyttas bara fram när hela körningen lyckats. Nästa körning börjar på
    samma tidsstämpel (inklusive), så ett ärende med exakt den tidsstämpeln
    hämtas hellre två gånger än ingen. Upserten är idempotent.
    """
    befintlig = db.hamta_sync_status(CHECKPOINT_NYCKEL)
    fran = sedan or (None if full else befintlig)
    inkrementell = fran is not None
    if inkrementell:
        logger.info("Fas 1: inkrementell synk av sager med opdateringsdato >= %s", fran)
    else:
        logger.info("Fas 1: full synk av alla sager%s",
                    "" if full else " (ingen checkpoint finns ännu)")

    antal = 0
    senaste_tid = fran
    senaste_id = 0
    hogsta_opdatering: str | None = None

    while True:
        params = {"$top": str(SIDSTORLEK), "$select": _SAG_FALT}
        if inkrementell:
            params["$orderby"] = "opdateringsdato asc,id asc"
            params["$filter"] = (
                f"opdateringsdato gt datetime'{senaste_tid}' or "
                f"(opdateringsdato eq datetime'{senaste_tid}' and id gt {senaste_id})"
            )
        else:
            params["$orderby"] = "id asc"
            params["$filter"] = f"id gt {senaste_id}"

        try:
            data = _oda_get("Sag", params)
        except Exception as e:
            logger.error("ODA Sag-hämtning misslyckades efter %d sager: %s. "
                         "Checkpointen flyttas inte.", antal, e)
            return False

        poster = data.get("value", [])
        for sag in poster:
            _spara_sag(sag)
            antal += 1
            opd = sag.get("opdateringsdato")
            if opd and (hogsta_opdatering is None or opd > hogsta_opdatering):
                hogsta_opdatering = opd

        if poster:
            sista = poster[-1]
            senaste_id = sista.get("id")
            if inkrementell:
                senaste_tid = sista.get("opdateringsdato")
            logger.info("  Hämtat %d sager (totalt %d, senast id=%s, opdateringsdato=%s)",
                        len(poster), antal, senaste_id, sista.get("opdateringsdato"))
        if len(poster) < SIDSTORLEK:
            break
        time.sleep(FORDROJ_ODA)

    # ODA:s tidsstämplar börjar alltid med YYYY-MM-DDTHH:MM:SS, så
    # strängjämförelse ger samma ordning som tidsjämförelse.
    #
    # Med --sedan flyttas checkpointen bara om intervallet ansluter till den
    # befintliga; annars skulle ärenden mellan checkpointen och --sedan aldrig
    # hämtas. Checkpointen flyttas aldrig bakåt.
    tacker_gapet = sedan is None or (befintlig is not None and sedan <= befintlig)
    ny = max(x for x in (befintlig, hogsta_opdatering, fran if inkrementell else None) if x) \
        if (befintlig or hogsta_opdatering) else None
    if tacker_gapet and ny and ny != befintlig:
        db.spara_sync_status(CHECKPOINT_NYCKEL, ny)
    elif not tacker_gapet:
        logger.info("  --sedan ansluter inte till checkpointen (%s); den lämnas orörd", befintlig)
    logger.info("Fas 1 klar: %d nya eller ändrade sager. Checkpoint: %s",
                antal, ny if tacker_gapet else befintlig)
    return True


# ---------------------------------------------------------------------------
# Fas 2 — Fulltext för lovforslag + beslutningsforslag
# ---------------------------------------------------------------------------

def synka_fulltext():
    """
    Hämtar fulltext-PDF för sager av typ lovforslag och beslutningsforslag
    som saknar fulltext, eller vars fulltext bara är resume
    (fulltext_kalla = 'resume').
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
            f"""SELECT id, extern_id, typ, titel, fulltext_kalla
                FROM {p}dokument
                WHERE kalla = {ph}
                  AND (fulltext_md IS NULL OR fulltext_kalla = 'resume')
                  AND typ IN ({typer_sql})
                ORDER BY id ASC""",
            ("oda",)
        )
        att_behandla = [
            {"id": r[0], "extern_id": r[1], "typ": r[2], "titel": r[3], "kalla": r[4]}
            for r in cur.fetchall()
        ]

    totalt = len(att_behandla)
    logger.info("Fas 2: %d sager saknar fulltext eller har bara resume — hämtar dokument och PDF", totalt)

    lyckade = 0
    misslyckade = 0

    for i, sag in enumerate(att_behandla, 1):
        sagid    = int(sag["extern_id"])
        dok_id   = sag["id"]

        logger.info("[%d/%d] sagid=%d: %s", i, totalt, sagid, (sag["titel"] or "")[:60])

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
            _markera_ingen_pdf(dok_id)
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

            text = _extrahera_text(pdf_bytes, kalla_id=str(dok_id), kalla_url=fil_url)
            if not text:
                logger.warning("  Tom text för %s", fil_url)
                continue

            # Lagra i DB — uppdatera url och fulltext
            with db._cursor() as cur:
                p = db._prefix()
                ph = "%s" if db._ar_postgres() else "?"
                cur.execute(
                    f"UPDATE {p}dokument SET url = {ph}, fulltext_md = {ph}, "
                    f"fulltext_kalla = 'pdf' WHERE id = {ph}",
                    (fil_url, text, dok_id)
                )

            logger.info("  ✓ %d tecken extraherade", len(text))
            lyckade += 1
            text_hittad = True
            break

        if not text_hittad:
            logger.warning("  Ingen PDF med text hittades för sagid=%d", sagid)
            misslyckade += 1
            _markera_ingen_pdf(dok_id)

        # Spara checkpoint var 100:e sag
        if i % 100 == 0:
            db.spara_sync_status("oda_fulltext_progress", str(i))
            logger.info("  Checkpoint sparad: %d/%d behandlade", i, totalt)

    db.spara_sync_status("oda_fulltext_klar", datetime.now(timezone.utc).isoformat())
    logger.info(
        "Fas 2 klar: %d lyckade, %d misslyckade av %d sager",
        lyckade, misslyckade, totalt
    )


def _markera_ingen_pdf(dok_id: int):
    """Markerar att PDF-hämtningen är gjord utan resultat, så att fas 2 inte
    försöker igen.

    Ett ärende som har resume behåller den som sökbar text; övriga får en
    platshållare, eftersom NULL skulle välja ärendet igen.
    """
    with db._cursor() as cur:
        p = db._prefix()
        ph = "%s" if db._ar_postgres() else "?"
        cur.execute(
            f"""UPDATE {p}dokument
                SET fulltext_md = COALESCE(fulltext_md, {ph}), fulltext_kalla = 'ingen_pdf'
                WHERE id = {ph}""",
            ("(ingen PDF hittad)", dok_id)
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
    parser.add_argument("--full", action="store_true",
                        help="Fas 1 hämtar alla sager i stället för bara de som ändrats sedan förra lyckade körningen")
    parser.add_argument("--sedan", metavar="DATUM",
                        help="Fas 1 hämtar sager ändrade från DATUM (YYYY-MM-DD) i stället för från checkpointen")
    parser.add_argument("--installera-schema", action="store_true",
                        help="Installerar launchd-jobb för daglig synk")
    args = parser.parse_args()
    if args.full and args.sedan:
        parser.error("--full och --sedan kan inte kombineras")

    if args.installera_schema:
        installera_launchd()
        return

    logger.info("=== ODA-synk startad ===")
    db.initialisera_schema()
    db.migrera_data()   # engångsuppdateringar efter uppgradering; snabb när de redan körts

    lyckad = True
    if args.fas is None or args.fas == 1:
        sedan = f"{args.sedan}T00:00:00" if args.sedan and len(args.sedan) == 10 else args.sedan
        lyckad = synka_sager_metadata(full=args.full, sedan=sedan)

    if args.fas is None or args.fas == 2:
        synka_fulltext()

    logger.info("=== ODA-synk avslutad ===")
    if not lyckad:
        sys.exit(1)


if __name__ == "__main__":
    main()
