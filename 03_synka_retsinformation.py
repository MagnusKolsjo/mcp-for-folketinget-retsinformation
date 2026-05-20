"""
03_synka_retsinformation.py — Synkskript för Retsinformation harvest-API (ström 14, Danmark).

Hämtar dansk lagstiftning (love, lovbekendtgørelser, bekendtgørelser m.fl.) via
det officiella harvest-API:et på api.retsinformation.dk.

API-egenskaper (verifierade 2026-05-16):
  - Endpoint: GET /v1/Documents?date=YYYY-MM-DD
  - Parametern "date" måste ligga inom de senaste 10 dagarna
  - Ingen paginering — returnerar alla poster för det dygnet i en lista
  - Kan bara kallas en gång per 10 sekunder
  - Öppen 03:00–23:45 dansk tid
  - Returnerar INTE titel eller fulltext — bara metadata + href till ELI-XML
  - ELI-XML hämtas separat och parsas för titel + fulltext

Delta-synk: skriptet itererar dag för dag bakåt och framåt.
Första körning: hämtar de senaste 10 dagarna (API-max för historik).
Därefter: daglig körning utan parametrar hämtar gårdagens ändringar.

Användning:
  python3 03_synka_retsinformation.py          # Delta-synk (gårdag + missade dagar)
  python3 03_synka_retsinformation.py --dagar 10  # Hämta de senaste N dagarna (max 10)
"""

import argparse
import logging
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
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
        logging.FileHandler(str(_LOG_DIR / "synk_retsinformation.log"), encoding="utf-8"),
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

API_BAS_URL      = "https://api.retsinformation.dk/v1"
FORDROJ_HARVEST  = 11.0   # sekunder mellan harvest-API-anrop (max 1/10 sek + marginal)
FORDROJ_XML      = 1.5    # sekunder mellan XML-hämtningar (generöst — separat server)
MAX_HISTORIK_DAGAR = 10   # API tillåter max 10 dagars historik

# Dokumenttyper vi bryr oss om (matchas mot documentType.shortName prefix)
INKLUDERA_TYPER = {"LOV", "LBK", "BEK", "CIR", "CIRK", "VEJ", "SKR"}

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "mcp-for-folketinget-retsinformation/1.0 (+https://github.com/MagnusKolsjo/mcp-for-folketinget-retsinformation)",
}

_XML_HEADERS = {
    "Accept": "application/xml, text/xml, */*",
    "User-Agent": "mcp-for-folketinget-retsinformation/1.0 (+https://github.com/MagnusKolsjo/mcp-for-folketinget-retsinformation)",
}


def _api_get_harvest(dato: str) -> list[dict]:
    """
    GET /v1/Documents?date=YYYY-MM-DD mot Retsinformation harvest-API.
    Returnerar lista med dokumentposter för det datumet.
    dato: ISO-datum som sträng, t.ex. '2026-05-14'
    """
    url = f"{API_BAS_URL}/Documents"
    params = {"date": dato}

    for forsok in range(3):
        try:
            resp = httpx.get(url, params=params, headers=_HEADERS, timeout=30)
            if resp.status_code == 429:
                vantetid = int(resp.headers.get("Retry-After", "30"))
                logger.warning("Rate-limit (429) — väntar %d sek", vantetid)
                time.sleep(vantetid)
                continue
            if resp.status_code == 400:
                # Troligtvis datum utanför 10-dagarsfönster
                logger.warning("400 för dato=%s — troligtvis utanför 10-dagarsfönster", dato)
                return []
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            if forsok == 2:
                logger.error("Harvest-anrop misslyckades för dato=%s: %s", dato, e)
                return []
            logger.warning("Harvest-anrop försök %d/3 misslyckades: %s", forsok + 1, e)
            time.sleep(2 ** forsok)
    return []


def _hamta_eli_xml(href: str) -> bytes | None:
    """Hämtar ELI-XML från retsinformation.dk. Följer redirect (http→https)."""
    # Ersätt http med https om nödvändigt
    url = href.replace("http://", "https://")
    try:
        resp = httpx.get(url, headers=_XML_HEADERS, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning("XML-hämtning misslyckades: %s — %s", url, e)
        return None


# ---------------------------------------------------------------------------
# XML-parsning (ELI LexDania-format)
# ---------------------------------------------------------------------------

def _text_fran_element(el) -> str:
    """Extraherar all text från ett XML-element rekursivt."""
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


def _parsa_eli_xml(xml_bytes: bytes) -> dict:
    """
    Parsare för Retsinformations ELI LexDania XML-format.
    Returnerar dict med: titel, fulltext_md, datum, lovnummer, typ.
    """
    resultat = {
        "titel": None,
        "fulltext_md": None,
        "datum": None,
        "lovnummer": None,
        "status": None,
        "giltig_till": None,
    }

    try:
        rot = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("XML-parsfel: %s", e)
        return resultat

    # Namespace-hantering — LexDania-XML har ibland namespace
    def _finn(element, tagg: str):
        """Hitta child utan hänsyn till namespace."""
        for barn in element:
            lokal = barn.tag.split("}")[-1] if "}" in barn.tag else barn.tag
            if lokal == tagg:
                yield barn

    def _finn_rek(element, tagg: str):
        """Rekursiv sökning efter tagg."""
        lokal = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if lokal == tagg:
            yield element
        for barn in element:
            yield from _finn_rek(barn, tagg)

    # Hämta metadata från <Meta>
    for meta in _finn(rot, "Meta"):
        for el in meta:
            lokal = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if lokal == "DocumentTitle" and el.text:
                resultat["titel"] = el.text.strip()
            elif lokal == "DiesSigni" and el.text:
                resultat["datum"] = el.text.strip()[:10]
            elif lokal == "Number" and el.text:
                resultat["lovnummer"] = el.text.strip()
            elif lokal == "Status" and el.text:
                resultat["status"] = el.text.strip()   # 'Valid', 'Invalid' etc.
            elif lokal == "EndDate" and el.text and not resultat["giltig_till"]:
                resultat["giltig_till"] = el.text.strip()[:10]

    # Bygg fulltext_md från <DokumentIndhold>
    rader = []
    for innehall in _finn_rek(rot, "DokumentIndhold"):
        _bygg_markdown(innehall, rader, 0)
        break  # Bara första DokumentIndhold

    if rader:
        resultat["fulltext_md"] = "\n\n".join(r for r in rader if r.strip())

    return resultat


def _bygg_markdown(element, rader: list, djup: int):
    """
    Rekursivt bygger markdown-text från LexDania XML.
    Hanterar kapitel, paragrafer, stycken och löptext.
    """
    lokal = element.tag.split("}")[-1] if "}" in element.tag else element.tag

    if lokal == "Kapitel":
        # Hämta kapitelnummer och rubrik
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
        pass  # Hanteras av förälderelement

    else:
        # Generellt: rekursera
        for barn in element:
            _bygg_markdown(barn, rader, djup + 1)


# ---------------------------------------------------------------------------
# Typnormalisering
# ---------------------------------------------------------------------------

_TYPNAMN = {
    "LOV":  "lov",
    "LBK":  "lovbekendtgorelse",
    "BEK":  "bekendtgorelse",
    "CIR":  "cirkular",
    "CIRK": "cirkular",
    "VEJ":  "vejledning",
    "SKR":  "skrivelse",
}


def _normalisera_typ(short_name: str | None) -> tuple[str | None, str | None]:
    """
    Normaliserar documentType.shortName (t.ex. 'BEK H') till (kod, typ).
    Returnerar (kortnamn, normaliserat typnamn).
    """
    if not short_name:
        return None, None
    # shortName kan vara 'BEK H', 'LOV', 'LBK H' etc.
    kod = short_name.split()[0].upper() if short_name else ""
    if INKLUDERA_TYPER and kod not in INKLUDERA_TYPER:
        return short_name, None  # Returnera None för typ → filtreringsbart
    return short_name, _TYPNAMN.get(kod, kod.lower())


# ---------------------------------------------------------------------------
# Synk
# ---------------------------------------------------------------------------

def synka_dato(dato: str) -> tuple[int, int]:
    """
    Hämtar och lagrar alla dokument som ändrades på ett givet datum.
    Returnerar (granskade, sparade).
    """
    logger.info("  Hämtar harvest för dato=%s", dato)
    poster = _api_get_harvest(dato)
    logger.info("  %d poster från harvest-API", len(poster))

    if not poster:
        return 0, 0

    granskade = 0
    sparade   = 0

    for post in poster:
        granskade += 1

        dok_id_ext  = post.get("documentId") or post.get("accessionsnummer") or ""
        accnr       = post.get("accessionsnummer") or ""
        href        = post.get("href") or ""
        change_date = post.get("changeDate") or dato
        short_name  = (post.get("documentType") or {}).get("shortName") or ""
        reason      = post.get("reasonForChange") or ""

        if not dok_id_ext:
            continue

        # Hantera RemovedDocument — markera som ersatt utan att radera
        if reason == "RemovedDocument":
            db.markera_ersatt(dok_id_ext)
            logger.info("  Ersatt: %s", dok_id_ext)
            sparade += 1
            continue

        if not href:
            continue

        beteckning, typ = _normalisera_typ(short_name)
        if typ is None:
            continue  # Inte en typ vi bryr oss om

        # Bygg läsbar webbadress från accessionsnummer
        web_url = f"https://www.retsinformation.dk/eli/accn/{accnr}" if accnr else href

        # Hämta och parsa ELI-XML för fulltext + titel
        time.sleep(FORDROJ_XML)
        xml_bytes = _hamta_eli_xml(href)

        parsed = {}
        if xml_bytes:
            parsed = _parsa_eli_xml(xml_bytes)

        titel      = parsed.get("titel") or ""
        fulltext   = parsed.get("fulltext_md")
        datum      = parsed.get("datum") or change_date[:10]
        lovnummer  = parsed.get("lovnummer")
        status     = parsed.get("status")      # 'Valid' etc. från XML
        giltig_till = parsed.get("giltig_till")

        if not titel:
            logger.warning("  Ingen titel för %s (%s)", dok_id_ext, href)
            continue

        db.upsert_dokument(
            kalla       = "retsinformation",
            extern_id   = dok_id_ext,
            beteckning  = beteckning,
            typ         = typ,
            titel       = titel,
            titelkort   = None,
            periode     = None,
            datum       = datum,
            url         = web_url,
            retsinformationsurl = web_url,
            lovnummer   = lovnummer,
            fulltext_md = fulltext,
            status      = status,
            giltig_till = giltig_till,
        )
        sparade += 1

        # Hämta och lagra ELI-relationer (eli:changes) för detta dokument
        # Viktigt för ändringslagar (LOVC) så att vi vet vilken baslag de ändrar
        time.sleep(FORDROJ_XML)
        eli_canonical = f"https://retsinformation.dk/eli/accn/{accnr}" if accnr else None
        if eli_canonical:
            changes = retsinformation_client.hamta_eli_json_relationer(eli_canonical)
            if changes:
                antal_rel = db.lagra_relationer(web_url, changes, relationstyp="changes")
                if antal_rel:
                    logger.info("  Relationer sparade för %s: %d st", dok_id_ext, antal_rel)

        logger.debug("  [%s] %s — %s", typ, dok_id_ext, titel[:60])

    return granskade, sparade


def synka(antal_dagar: int = 1):
    """
    Hämtar dokument för de senaste antal_dagar dagarna.
    Normalt: antal_dagar=1 (gårdag).
    Första körning: antal_dagar=10 (API-maximum).
    """
    idag = datetime.now(timezone.utc).date()
    totalt_granskade = 0
    totalt_sparade   = 0

    for dagar_bakåt in range(antal_dagar, 0, -1):
        dato = (idag - timedelta(days=dagar_bakåt)).isoformat()
        g, s = synka_dato(dato)
        totalt_granskade += g
        totalt_sparade   += s
        logger.info("  dato=%s: %d granskade, %d sparade", dato, g, s)

        if dagar_bakåt > 1:
            time.sleep(FORDROJ_HARVEST)

    nu = datetime.now(timezone.utc).isoformat()
    db.spara_sync_status("retsinformation_senaste_synk", nu)
    logger.info("Retsinformation-synk klar: %d granskade totalt, %d sparade", totalt_granskade, totalt_sparade)


# ---------------------------------------------------------------------------
# Huvudprogram
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Synkskript för Retsinformation")
    parser.add_argument(
        "--dagar", type=int, default=None,
        help=f"Antal dagar bakåt att hämta (max {MAX_HISTORIK_DAGAR}). "
             f"Standard: 1 (gårdag) om senaste synk är känd, annars {MAX_HISTORIK_DAGAR}."
    )
    args = parser.parse_args()

    db.initialisera_schema()

    if args.dagar is not None:
        antal = min(args.dagar, MAX_HISTORIK_DAGAR)
    else:
        # Delta-synk: kolla när vi senast körde
        senaste = db.hamta_sync_status("retsinformation_senaste_synk")
        if senaste:
            dt_senaste = datetime.fromisoformat(senaste)
            dagar_sedan = (datetime.now(timezone.utc) - dt_senaste).days
            antal = max(1, min(dagar_sedan + 1, MAX_HISTORIK_DAGAR))
            if antal > 1:
                logger.info("Missade %d dagar sedan senaste synk — hämtar alla", antal)
        else:
            antal = MAX_HISTORIK_DAGAR
            logger.info("Första körning — hämtar de senaste %d dagarna (API-max)", antal)

    logger.info("=== Retsinformation-synk startad (antal_dagar=%d) ===", antal)
    synka(antal_dagar=antal)
    logger.info("=== Retsinformation-synk avslutad ===")


if __name__ == "__main__":
    main()
