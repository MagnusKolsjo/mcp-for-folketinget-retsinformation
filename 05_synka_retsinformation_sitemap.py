"""
05_synka_retsinformation_sitemap.py — Historisk fullharvest av Retsinformation via sitemap.

Problemet: harvest-API:et (03_synka_retsinformation.py) ger bara de senaste 10 dagarna.
Lösningen: retsinformation.dk exponerar en sitemap med 200 001 ELI-URL:er.
Varje URL + "/xml" ger direkt fulltext-XML (samma format som harvest-API:ets href).

Relevanta URL-typer:
  eli/lta  — 62 346 dokument (Lovtidende A: LOV, LBK, BEK, CIR, VEJ, SKR)
  eli/retsinfo — 82 244 (blandad, filtreras på DocumentType efter XML-hämtning)

Körning: förväntas ta 20–40 timmar totalt (0.5 sek/dok).
Kan stoppas och startas om — checkpointad via DB (hoppar över redan importerade).

Användning:
  python3 05_synka_retsinformation_sitemap.py              # Kör alla (lta + retsinfo)
  python3 05_synka_retsinformation_sitemap.py --bara-lta   # Bara eli/lta (snabbare, viktigare)
  python3 05_synka_retsinformation_sitemap.py --limit 1000 # Testning
  python3 05_synka_retsinformation_sitemap.py --trad 4     # 4 parallella trådar (snabbare)
"""

import argparse
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
        logging.FileHandler(str(_LOG_DIR / "sitemap_harvest.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

import db
import httpx
import retsinformation_client

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

SITEMAP_BAS = "https://retsinformation.dk/sitemap.xml"
SITEMAP_SIDOR = 21
FORDROJ_XML  = 0.5   # sekunder per request (ingen angiven rate-limit för denna server)

# URL-prefix vi bryr oss om (eli/ft hoppar vi — hanteras av ODA)
INKLUDERA_ELI_PREFIX = {"lta", "retsinfo", "ltb", "ltc"}

# DocumentType-koder vi vill spara (matchar meta-fältets prefix)
INKLUDERA_TYPER = {"LOV", "LBK", "BEK", "CIR", "CIRK", "VEJ", "SKR"}

_TYPNAMN = {
    "LOV":  "lov",
    "LBK":  "lovbekendtgorelse",
    "BEK":  "bekendtgorelse",
    "CIR":  "cirkular",
    "CIRK": "cirkular",
    "VEJ":  "vejledning",
    "SKR":  "skrivelse",
}

_XML_HEADERS = {
    "Accept": "application/xml, text/xml, */*",
    "User-Agent": "mcp-for-folketinget-retsinformation/1.0 (+https://github.com/MagnusKolsjo/mcp-for-folketinget-retsinformation)",
}

# ---------------------------------------------------------------------------
# Sitemap-inläsning
# ---------------------------------------------------------------------------

def _hamta_sitemap_urls(bara_lta: bool = False) -> list[str]:
    """
    Laddar alla sitemap-sidor och returnerar filtrerade ELI-URL:er.
    Cachas lokalt i logs/sitemap_urls.txt för snabb omstart.
    """
    cache_vag = _LOG_DIR / "sitemap_urls.txt"

    if cache_vag.exists():
        rader = cache_vag.read_text(encoding="utf-8").splitlines()
        urls = [r for r in rader if r.startswith("http")]
        if bara_lta:
            urls = [u for u in urls if "/eli/lta/" in u]
        logger.info("Sitemap-cache: %d URL:er", len(urls))
        return urls

    logger.info("Laddar sitemap (%d sidor)...", SITEMAP_SIDOR)
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    alla_urls = []

    with httpx.Client(headers={"User-Agent": "mcp-for-folketinget-retsinformation/1.0 (+https://github.com/MagnusKolsjo/mcp-for-folketinget-retsinformation)"}, timeout=30, follow_redirects=True) as klient:
        for sida in range(1, SITEMAP_SIDOR + 1):
            url = f"{SITEMAP_BAS}?page={sida}"
            try:
                resp = klient.get(url)
                resp.raise_for_status()
                tree = ET.fromstring(resp.content)
                sida_urls = [u.text for u in tree.findall('.//sm:loc', ns) if u.text]
                # Filtrera: behåll bara ELI-URL:er med relevanta prefix
                for u in sida_urls:
                    if '/eli/' in u:
                        prefix = u.split('/eli/')[-1].split('/')[0]
                        if prefix in INKLUDERA_ELI_PREFIX:
                            alla_urls.append(u)
                logger.info("  Sida %d: %d relevanta URL:er (totalt %d)", sida, len([u for u in sida_urls if '/eli/' in u and u.split('/eli/')[-1].split('/')[0] in INKLUDERA_ELI_PREFIX]), len(alla_urls))
                time.sleep(0.3)
            except Exception as e:
                logger.error("Sida %d misslyckades: %s", sida, e)

    # Spara cache
    cache_vag.write_text("\n".join(alla_urls), encoding="utf-8")
    logger.info("Sitemap laddad: %d URL:er (cachad)", len(alla_urls))

    if bara_lta:
        alla_urls = [u for u in alla_urls if "/eli/lta/" in u]
    return alla_urls


# ---------------------------------------------------------------------------
# Redan importerade — hoppa över
# ---------------------------------------------------------------------------

def _hamta_importerade_urls() -> set[str]:
    """Returnerar mängden web-URL:er som redan finns i databasen."""
    p = db._prefix()
    with db._cursor() as cur:
        cur.execute(f"SELECT url FROM {p}dokument WHERE kalla = 'retsinformation' AND url IS NOT NULL")
        return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# XML-hämtning och parsning (återanvänder logik från 03_synka_retsinformation.py)
# ---------------------------------------------------------------------------

def _text_fran_element(el) -> str:
    delar = []
    if el.text:
        delar.append(el.text.strip())
    for barn in el:
        barntext = _text_fran_element(barn)
        if barntext:
            delar.append(barntext)
    if el.tail:
        delar.append(el.tail.strip())
    return " ".join(d for d in delar if d)


def _bygg_markdown(element, rader: list, djup: int):
    lokal = element.tag.split("}")[-1] if "}" in element.tag else element.tag

    if lokal == "Kapitel":
        exp = ""
        rub = ""
        for barn in element:
            bl = barn.tag.split("}")[-1] if "}" in barn.tag else barn.tag
            if bl == "Explicatus" and barn.text:
                exp = barn.text.strip()
            elif bl == "Rubrica":
                rub = _text_fran_element(barn).strip()
        rubrik = f"{exp} {rub}".strip()
        if rubrik:
            rader.append(f"## {rubrik}")
        for barn in element:
            _bygg_markdown(barn, rader, djup + 1)

    elif lokal == "Paragraf":
        exp = ""
        for barn in element:
            bl = barn.tag.split("}")[-1] if "}" in barn.tag else barn.tag
            if bl == "Explicatus" and barn.text:
                exp = barn.text.strip()
        if exp:
            rader.append(f"### {exp}")
        for barn in element:
            _bygg_markdown(barn, rader, djup + 1)

    elif lokal == "Stk":
        exp = ""
        exitus = ""
        for barn in element:
            bl = barn.tag.split("}")[-1] if "}" in barn.tag else barn.tag
            if bl == "Explicatus" and barn.text:
                exp = barn.text.strip()
            elif bl == "Exitus":
                exitus = _text_fran_element(barn).strip()
        rad = (f"{exp} " if exp else "") + exitus
        if rad.strip():
            rader.append(rad.strip())

    elif lokal == "Linea":
        text = _text_fran_element(element).strip()
        if text:
            rader.append(text)

    elif lokal in ("Explicatus", "Rubrica"):
        pass

    else:
        for barn in element:
            _bygg_markdown(barn, rader, djup + 1)


def _parsa_eli_xml(xml_bytes: bytes) -> dict | None:
    """
    Parsar LexDania XML och returnerar dict med metadata + fulltext.
    Returnerar None om dokumenttypen inte är relevant.
    """
    try:
        rot = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    def _finn_rek(element, tagg: str):
        lokal = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if lokal == tagg:
            yield element
        for barn in element:
            yield from _finn_rek(barn, tagg)

    # Hämta metadata
    titel     = None
    datum     = None
    lovnummer = None
    dok_typ   = None
    accnr     = None
    beteckning = None

    for meta in _finn_rek(rot, "Meta"):
        for el in meta:
            lokal = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if lokal == "DocumentTitle" and el.text:
                titel = el.text.strip()
            elif lokal == "DiesSigni" and el.text:
                datum = el.text.strip()[:10]
            elif lokal == "Number" and el.text:
                lovnummer = el.text.strip()
            elif lokal == "DocumentType" and el.text:
                dok_typ = el.text.strip()   # t.ex. "BEK H#LOKDOK04"
            elif lokal == "AccessionNumber" and el.text:
                accnr = el.text.strip()
        break

    # Hämta status och giltighetstid separat (kan ligga utanför Meta i vissa scheman)
    xml_status    = None
    giltig_till   = None
    for meta in _finn_rek(rot, "Meta"):
        for el in meta:
            lokal = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if lokal == "Status" and el.text:
                xml_status = el.text.strip()
            elif lokal == "EndDate" and el.text and not giltig_till:
                giltig_till = el.text.strip()[:10]
        break

    if not titel:
        return None

    # Normalisera typ
    if dok_typ:
        kort_typ = dok_typ.split("#")[0].split()[0].upper()  # "BEK H#..." → "BEK"
        beteckning = dok_typ.split("#")[0].strip()           # "BEK H"
    else:
        kort_typ = ""
        beteckning = None

    if INKLUDERA_TYPER and kort_typ not in INKLUDERA_TYPER:
        return None  # Inte en relevant typ

    typ_namn = _TYPNAMN.get(kort_typ, kort_typ.lower() if kort_typ else None)

    # Bygg fulltext
    rader = []
    for innehall in _finn_rek(rot, "DokumentIndhold"):
        _bygg_markdown(innehall, rader, 0)
        break

    fulltext = "\n\n".join(r for r in rader if r.strip()) if rader else None

    return {
        "titel":      titel,
        "beteckning": beteckning,
        "typ":        typ_namn,
        "datum":      datum,
        "lovnummer":  lovnummer,
        "accnr":      accnr,
        "fulltext_md": fulltext,
        "status":     xml_status,
        "giltig_till": giltig_till,
    }


def _behandla_url(eli_url: str) -> str:
    """
    Hämtar och lagrar ett dokument från en ELI-URL.
    Returnerar: 'ok', 'skip' (fel typ), 'fel' eller 'finns' (redan i DB).
    """
    xml_url = eli_url.rstrip("/") + "/xml"
    web_url = eli_url  # Läsbar webbadress

    try:
        resp = httpx.get(
            xml_url,
            headers=_XML_HEADERS,
            timeout=30,
            follow_redirects=True,
        )
        if resp.status_code == 404:
            return "skip"
        resp.raise_for_status()
        xml_bytes = resp.content
    except Exception as e:
        logger.debug("XML-hämtning misslyckades: %s — %s", xml_url, e)
        return "fel"

    parsed = _parsa_eli_xml(xml_bytes)
    if parsed is None:
        return "skip"

    # Bygg extern_id från accnr eller URL-fragment
    extern_id = parsed.get("accnr") or eli_url.split("/eli/")[-1].replace("/", "_")

    try:
        db.upsert_dokument(
            kalla       = "retsinformation",
            extern_id   = extern_id,
            beteckning  = parsed.get("beteckning"),
            typ         = parsed.get("typ"),
            titel       = parsed["titel"],
            titelkort   = None,
            periode     = None,
            datum       = parsed.get("datum"),
            url         = web_url,
            retsinformationsurl = web_url,
            lovnummer   = parsed.get("lovnummer"),
            fulltext_md = parsed.get("fulltext_md"),
            status      = parsed.get("status"),
            giltig_till = parsed.get("giltig_till"),
        )
    except Exception as e:
        logger.warning("DB-fel för %s: %s", eli_url, e)
        return "fel"

    # Hämta och lagra ELI-relationer (eli:changes) för detta dokument
    try:
        changes = retsinformation_client.hamta_eli_json_relationer(eli_url)
        if changes:
            db.lagra_relationer(web_url, changes, relationstyp="changes")
    except Exception as e:
        logger.debug("Relationer misslyckades för %s: %s", eli_url, e)

    return "ok"


# ---------------------------------------------------------------------------
# Huvudlogik
# ---------------------------------------------------------------------------

def kör_harvest(bara_lta: bool = False, limit: int | None = None, antal_trådar: int = 1):
    """
    Kör sitemap-baserad historisk harvest.
    """
    alla_urls = _hamta_sitemap_urls(bara_lta=bara_lta)

    if limit:
        alla_urls = alla_urls[:limit]

    # Ta bort redan importerade
    importerade = _hamta_importerade_urls()
    att_behandla = [u for u in alla_urls if u not in importerade]

    totalt    = len(att_behandla)
    lyckade   = 0
    hoppade   = 0
    fel_antal = 0

    logger.info(
        "Sitemap-harvest: %d URL:er totalt, %d redan importerade, %d att behandla",
        len(alla_urls), len(importerade), totalt
    )

    if totalt == 0:
        logger.info("Inget att göra — alla URL:er redan importerade.")
        return

    startad = time.time()

    if antal_trådar > 1:
        # Parallell körning — OBS: db._cursor() måste vara trådsäker (psycopg2 är det inte per default)
        # Använd en lock för DB-skrivningar
        import threading
        db_lock = threading.Lock()
        original_upsert = db.upsert_dokument

        def trådsäker_upsert(**kwargs):
            with db_lock:
                return original_upsert(**kwargs)

        db.upsert_dokument = trådsäker_upsert

        with ThreadPoolExecutor(max_workers=antal_trådar) as pool:
            futures = {pool.submit(_behandla_url, u): u for u in att_behandla}
            for i, future in enumerate(as_completed(futures), 1):
                utfall = future.result()
                if utfall == "ok":
                    lyckade += 1
                elif utfall == "skip":
                    hoppade += 1
                else:
                    fel_antal += 1

                if i % 100 == 0 or i == totalt:
                    elapsed = time.time() - startad
                    per_sek = i / elapsed if elapsed > 0 else 0
                    aterstaende = (totalt - i) / per_sek if per_sek > 0 else 0
                    logger.info(
                        "[%d/%d] ok=%d skip=%d fel=%d — %.1f/sek — ~%.0f min kvar",
                        i, totalt, lyckade, hoppade, fel_antal, per_sek, aterstaende / 60
                    )

        db.upsert_dokument = original_upsert

    else:
        # Sekventiell körning
        for i, url in enumerate(att_behandla, 1):
            utfall = _behandla_url(url)
            if utfall == "ok":
                lyckade += 1
            elif utfall == "skip":
                hoppade += 1
            else:
                fel_antal += 1

            time.sleep(FORDROJ_XML)

            if i % 100 == 0 or i == totalt:
                elapsed = time.time() - startad
                per_sek = i / elapsed if elapsed > 0 else 0
                aterstaende = (totalt - i) / per_sek if per_sek > 0 else 0
                logger.info(
                    "[%d/%d] ok=%d skip=%d fel=%d — %.1f/sek — ~%.0f min kvar",
                    i, totalt, lyckade, hoppade, fel_antal, per_sek, aterstaende / 60
                )

    db.spara_sync_status("sitemap_harvest_senast_kord", datetime.now(timezone.utc).isoformat())
    logger.info(
        "Sitemap-harvest klar: %d ok, %d skip (fel typ), %d fel av %d totalt",
        lyckade, hoppade, fel_antal, totalt
    )


# ---------------------------------------------------------------------------
# Huvud
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Historisk fullharvest av Retsinformation via sitemap"
    )
    parser.add_argument(
        "--bara-lta", action="store_true",
        help="Hämta bara eli/lta (konsoliderade lagar) — snabbare, ~62 000 dokument"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max antal dokument att behandla (för test)"
    )
    parser.add_argument(
        "--trad", type=int, default=1,
        help="Antal parallella trådar (standard 1; 2–4 ger snabbare körning)"
    )
    parser.add_argument(
        "--rensa-cache", action="store_true",
        help="Tvinga omladdning av sitemap (ignorera cachad lista)"
    )
    args = parser.parse_args()

    if args.rensa_cache:
        cache_vag = _LOG_DIR / "sitemap_urls.txt"
        if cache_vag.exists():
            cache_vag.unlink()
            logger.info("Sitemap-cache rensad")

    db.initialisera_schema()
    logger.info("=== Sitemap-harvest startad ===")
    logger.info(
        "  bara-lta=%s, limit=%s, trådar=%d",
        args.bara_lta, args.limit, args.trad
    )
    kör_harvest(
        bara_lta    = args.bara_lta,
        limit       = args.limit,
        antal_trådar = args.trad,
    )
    logger.info("=== Sitemap-harvest avslutad ===")


if __name__ == "__main__":
    main()
