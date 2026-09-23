# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
mcp_server.py — MCP-server för dansk riksdags- och rättsdata.

Datakällor:
  - Folketing ODA (oda.ft.dk): sager, dokument, afstemninger, ledamöter
  - Retsinformation (api.retsinformation.dk): dansk lagstiftning via harvest-API

Sökverktygen läser den lokala databasen, som fylls av synkskripten.
Verktygen för ärenden, voteringar, aktörer och valperioder hämtar live
från ODA. Servern gör inga anrop mot Retsinformations API; det sköter
synken, som också respekterar källans anropsgräns.

Prefix: dk_
Schema: danmark

Transport väljs med MCP_TRANSPORT (stdio eller http), se mcp_transport.py.
"""

import contextlib
import importlib.util
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Annotated, NotRequired, Optional, Required, TypedDict

from dotenv import load_dotenv

# Ladda .env relativt skriptets mapp. Servern ärver inte klientens
# shell-miljö, så .env måste läsas innan konfigurationen nedan.
_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

# Loggning till fil. I stdio-läget är stdout protokollkanalen.
_LOG_DIR = _SCRIPT_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_LOG_DIR / "mcp_server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

import db
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from mcp_transport import starta
from tyst_fd import tysta_fd

try:
    import psycopg2
    _DB_FEL: tuple[type[Exception], ...] = (psycopg2.Error, sqlite3.Error)
except ImportError:
    _DB_FEL = (sqlite3.Error,)

try:
    from curl_cffi import requests as cf_requests
    _CURL_CFFI_TILLGANGLIG = True
except ImportError:
    _CURL_CFFI_TILLGANGLIG = False
    logger.warning("curl-cffi saknas — ft.dk PDF-nedladdning ej tillgänglig")


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ODA_BAS_URL = "https://oda.ft.dk/api"

QUERY_EXPANSION_BASE_URL = os.getenv("QUERY_EXPANSION_BASE_URL", "http://localhost:11434/v1")
QUERY_EXPANSION_API_KEY  = os.getenv("QUERY_EXPANSION_API_KEY", "ollama")
QUERY_EXPANSION_MODEL    = os.getenv("QUERY_EXPANSION_MODEL", "llama3")

# Standardtak för fulltext i dk_hamta_dokument. Danska lagtexter och
# betænkninger når nära en miljon tecken; anroparen kan höja taket upp
# till DK_MAX_TECKEN_TAK.
DK_MAX_TECKEN = int(os.getenv("DK_MAX_TECKEN", "60000"))

# Absolut tak per svar. Ett verktygssvar skickas både som JSON-text och som
# strukturerat innehåll, alltså två gånger. 400 000 tecken ger drygt 800 000
# tecken totalt, med marginal under MCP-klienternas gräns på 1 MiB även när
# danska tecken tar två byte i UTF-8. Längre texter läses i flera anrop med
# fran_tecken.
DK_MAX_TECKEN_TAK = 400_000

PDF_CACHE_DIR = Path(os.getenv("PDF_CACHE_DIR", str(_SCRIPT_DIR / "pdf_cache")))
if not PDF_CACHE_DIR.is_absolute():
    PDF_CACHE_DIR = _SCRIPT_DIR / PDF_CACHE_DIR
PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_HTTPX_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "mcp-for-folketinget-retsinformation/1.0 (+https://github.com/MagnusKolsjo/mcp-for-folketinget-retsinformation)",
}

_SQLITE_EJ_VEKTOR = (
    "Semantisk sökning kräver PostgreSQL med pgvector. Servern kör mot SQLite, "
    "som saknar embeddings. Använd dk_sok, dk_sok_folketing eller dk_sok_lovgivning."
)


# ---------------------------------------------------------------------------
# Returtyper
#
# Fälten speglar databasens kolumner (alla TEXT utom id) och ODA:s entiteter.
# Allt som kan saknas i källdata är `| None`: äldre ODA-ärenden och äldre
# Retsinformation-dokument har ofta tomma fält, och ett None där typen säger
# str får hela anropet att misslyckas.
# ---------------------------------------------------------------------------

class Traff(TypedDict, total=False):
    """En träff i dk_sok och dk_sok_folketing: en rad ur dokumenttabellen."""
    dok_id: Required[int]
    kalla: str | None
    beteckning: str | None
    typ: str | None
    titel: str | None
    titelkort: str | None
    periode: str | None
    datum: str | None
    url: str | None
    retsinformationsurl: str | None
    lovnummer: str | None
    resume: str | None
    afstemningskonklusion: str | None
    paragrafnummer: str | None
    paragraf: str | None
    afgoerelse: str | None
    begrundelse: str | None
    baggrundsmateriale: str | None
    status: str | None
    giltig_till: str | None
    rank: float | None
    sagid: str
    retsinformation_id: str


class SokSvar(TypedDict):
    sokterm: str
    expansion: str | None
    antal_traffar: int
    traffar: list[Traff]


class FolketingSvar(TypedDict):
    kalla: str
    sokterm: str
    antal_traffar: int
    traffar: list[Traff]


class LovTraff(TypedDict):
    dok_id: int
    beteckning: str | None
    typ: str | None
    titel: str | None
    titelkort: str | None
    ikrafttraedelsesdato: str | None
    url: str | None
    lovnummer: str | None
    resume: str | None
    retsinformation_id: NotRequired[str]
    historisk: NotRequired[bool]
    advarsel: NotRequired[str]


class LovSvar(TypedDict):
    kalla: str
    sokterm: str
    inkludera_historiska: bool
    antal_traffar: int
    traffar: list[LovTraff]


class Andringslag(TypedDict):
    beteckning: str | None
    typ: str | None
    titel: str | None
    ikrafttraedelsesdato: str | None
    url: str | None
    lovnummer: str | None


class Dokument(TypedDict):
    """dk_hamta_dokument med dok_id: ett dokument ur den lokala databasen."""
    id: int
    kalla: str | None
    beteckning: str | None
    typ: str | None
    titel: str | None
    titelkort: str | None
    ikrafttraedelsesdato: str | None
    url: str | None
    lovnummer: str | None
    resume: str | None
    status: str | None
    fulltext_md: str | None
    tecken_totalt: NotRequired[int]
    tecken_visade: NotRequired[int]
    trunkerad: NotRequired[bool]
    fortsatt_fran_tecken: NotRequired[int | None]
    historisk: NotRequired[bool]
    advarsel: NotRequired[str]
    andringar_efter_senaste_lbk: NotRequired[list[Andringslag]]
    advarsel_andringar: NotRequired[str]


class SagDokument(TypedDict):
    dokumentid: int
    titel: str | None
    typeid: int | None
    dato: str | None
    fil_url: str | None


class Sag(TypedDict):
    """dk_hamta_dokument med sagid: ett ärende hämtat live från ODA."""
    sagid: int
    beteckning: str | None
    titel: str | None
    titelkort: str | None
    typeid: int | None
    statusid: int | None
    periodeid: int | None
    resume: str | None
    afstemningskonklusion: str | None
    lovnummer: str | None
    retsinformationsurl: str | None
    paragrafnummer: str | None
    paragraf: str | None
    afgoerelse: str | None
    begrundelse: str | None
    baggrundsmateriale: str | None
    dokument: list[SagDokument]


class Periode(TypedDict):
    id: int
    kod: str | None
    titel: str | None
    startdatum: str | None
    slutdatum: str | None


# Funktionell syntax: nycklarna "aktørid" och "for" går inte att skriva
# som klassattribut ("for" är ett reserverat ord).
Stemme = TypedDict("Stemme", {
    "aktørid": int | None,
    "typeid": int | None,
})

Afstemning = TypedDict("Afstemning", {
    "afstemningid": int | None,
    "sagstrinid": int | None,
    "sagstrin_typeid": int | None,
    "konklusion": str | None,
    "for": int | None,
    "imod": int | None,
    "hverken": int | None,
    "fravaerende": int | None,
    "vedtaget": bool | None,
    "stemmer": NotRequired[list[Stemme]],
})


class AfstemningSvar(TypedDict):
    sagid: int
    antal_afstemninger: int
    afstemninger: list[Afstemning]


class SemantiskTraff(TypedDict):
    id: int
    kalla: str | None
    beteckning: str | None
    typ: str | None
    titel: str | None
    titelkort: str | None
    periode: str | None
    datum: str | None
    url: str | None
    retsinformationsurl: str | None
    lovnummer: str | None
    resume: str | None
    paragrafnummer: str | None
    avstand: float | None


class SemantiskSvar(TypedDict):
    sokterm: str
    metod: str
    antal_traffar: int
    traffar: list[SemantiskTraff]


class ChunkTraff(TypedDict):
    chunk_nr: int
    text: str
    avstand: float | None


class SokIDokumentSvar(TypedDict):
    dok_id: int
    kalla: str | None
    beteckning: str | None
    typ: str | None
    titel: str | None
    fraga: str
    antal_chunks: int
    antal_traffar: int
    traffar: list[ChunkTraff]


Aktor = TypedDict("Aktor", {
    "aktørid": int | None,
    "typeid": int | None,
    "navn": str | None,
    "fornavn": str | None,
    "efternavn": str | None,
    "gruppenavnkort": str | None,
    "biografi": str | None,
    "startdato": str | None,
    "slutdato": str | None,
    "opdateringsdato": str | None,
})


class AktorBatchFel(TypedDict):
    batch: list[int]
    fel: str


class AktorBatch(TypedDict):
    begart_antal: int
    returnerat_antal: int
    aktorer: list[Aktor]
    fel: NotRequired[list[AktorBatchFel]]


# ---------------------------------------------------------------------------
# Felöversättning
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _som_toolerror(vad: str):
    """Översätter förväntade fel från ODA och databasen till ToolError.

    Utan översättningen får klienten bara "Error executing tool" utan
    orsak. Oväntade fel (programfel) släpps igenom; de loggas med spår av
    SDK:n och ska inte döljas bakom ett till synes begripligt meddelande.
    """
    try:
        yield
    except ToolError:
        raise
    except httpx.HTTPStatusError as fel:
        logger.warning("ODA-fel vid %s: %s", vad, fel)
        raise ToolError(
            f"Folketingets ODA svarade med HTTP {fel.response.status_code} vid {vad}. "
            "Kontrollera id:t eller försök igen senare."
        ) from fel
    except httpx.RequestError as fel:
        logger.warning("ODA onåbar vid %s: %s", vad, fel)
        raise ToolError(
            f"Folketingets ODA (oda.ft.dk) gick inte att nå vid {vad} "
            f"({type(fel).__name__}). Försök igen senare."
        ) from fel
    except _DB_FEL as fel:
        logger.error("Databasfel vid %s: %s", vad, fel, exc_info=True)
        raise ToolError(
            f"Databasfel vid {vad}: {fel}. Kontrollera att databasen är igång "
            "och att DATABASE_URL i .env pekar rätt."
        ) from fel
    except RuntimeError as fel:
        # db._hamta_url() kastar RuntimeError när DATABASE_URL saknas eller är fel
        logger.error("Konfigurationsfel vid %s: %s", vad, fel)
        raise ToolError(f"Konfigurationsfel vid {vad}: {fel}") from fel


# ---------------------------------------------------------------------------
# HTTP och PDF
# ---------------------------------------------------------------------------

def _oda_get(endpoint: str, params: Optional[dict] = None) -> dict:
    """GET-anrop mot Folketing ODA. Lägger till $format=json automatiskt."""
    params = dict(params or {})
    params.setdefault("$format", "json")
    url = f"{ODA_BAS_URL}/{endpoint}"
    logger.info("ODA GET %s %s", url, params)
    resp = httpx.get(url, params=params, headers=_HTTPX_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _hamta_pdf_bytes(url: str) -> Optional[bytes]:
    """
    Laddar ned en PDF från ft.dk med curl-cffi (kringgår Cloudflare managed challenge).
    Returnerar PDF-bytes eller None vid fel.
    """
    if not _CURL_CFFI_TILLGANGLIG:
        logger.error("curl-cffi saknas — kan inte ladda ned ft.dk PDF: %s", url)
        return None
    try:
        resp = cf_requests.get(url, impersonate="chrome", timeout=60)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error("PDF-nedladdning misslyckades: %s — %s", url, e)
        return None


def _extrahera_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extraherar text från PDF-bytes med pymupdf4llm."""
    try:
        import tempfile
        import pymupdf4llm
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_vag = f.name
        try:
            # Låset i tysta_fd serialiserar också pymupdf-anropen, som inte
            # tål att köras från flera trådar samtidigt.
            with tysta_fd(_LOG_DIR / "subprocess.log"):
                text = pymupdf4llm.to_markdown(tmp_vag)
            return text if text.strip() else None
        finally:
            os.unlink(tmp_vag)
    except ImportError:
        logger.warning("pymupdf4llm saknas — PDF-extraktion ej tillgänglig")
        return None
    except Exception as e:
        logger.error("PDF-extraktion misslyckades: %s", e)
        return None


# ---------------------------------------------------------------------------
# Textutdrag
# ---------------------------------------------------------------------------

def _skar_ut(text, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Trunkering utan markering är ett tyst datafel — svaret ser ut att vara hela
    innehållet. max_tecken <= 0 betyder ingen trunkering. Klipper på ordgräns.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    if max_tecken and max_tecken > 0 and len(rest) > max_tecken:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        utdrag    = utdrag.rstrip()
        trunkerad = True
    else:
        utdrag    = rest
        trunkerad = False

    slut = start + len(utdrag)
    return {
        "text":                 utdrag,
        "tecken_totalt":        totalt,
        "tecken_visade":        len(utdrag),
        "trunkerad":            trunkerad,
        "fortsatt_fran_tecken": slut if slut < totalt else None,
    }


# ---------------------------------------------------------------------------
# Chunk- och embeddingmodulen (lat inläsning)
# ---------------------------------------------------------------------------

# Filnamnet börjar med en siffra och kan inte importeras direkt, så modulen
# laddas med importlib. Den laddas en gång och bär embeddingmodellen.
_chunka_modul = None
_chunka_modul_las = threading.Lock()


def _hamta_chunka_modul():
    """Laddar 04_chunka_och_embedda.py och returnerar modulobjektet.

    Dubbelkontrollerad låsning: två samtidiga verktygsanrop får samma
    modulobjekt och därmed samma embeddingmodell.
    """
    global _chunka_modul
    if _chunka_modul is None:
        with _chunka_modul_las:
            if _chunka_modul is None:
                modul_vag = _SCRIPT_DIR / "04_chunka_och_embedda.py"
                spec = importlib.util.spec_from_file_location("chunka_embedda", modul_vag)
                modul = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(modul)
                _chunka_modul = modul
    return _chunka_modul


def _forvarm_embedding() -> None:
    """Laddar embeddingmodellen före första anropet i http-läget.

    Ett fel här ska inte hindra uppstarten: de övriga verktygen fungerar
    utan modellen, och de semantiska verktygen försöker igen vid anrop.
    """
    if not db._ar_postgres():
        return
    try:
        _hamta_chunka_modul()._hamta_modell()
        logger.info("Embeddingmodellen förvärmd")
    except Exception as fel:
        logger.warning("Förvärmning av embeddingmodellen misslyckades: %s", fel)


# ---------------------------------------------------------------------------
# Termexpansion
# ---------------------------------------------------------------------------

def _expandera_fraga(fraga: str) -> str:
    """
    Expanderar en sökfråga till dansk parlamentarisk och juridisk terminologi
    via OpenAI-kompatibel LLM. Returnerar kommaseparerade söktermer (OR-logik).
    """
    prompt_vag = _SCRIPT_DIR / "prompts" / "expansion_prompt.txt"
    if not prompt_vag.exists():
        return fraga

    system_prompt = prompt_vag.read_text(encoding="utf-8")

    try:
        import openai
        klient = openai.OpenAI(base_url=QUERY_EXPANSION_BASE_URL, api_key=QUERY_EXPANSION_API_KEY)
        svar = klient.chat.completions.create(
            model=QUERY_EXPANSION_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": fraga},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        return svar.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("Termexpansion misslyckades: %s", e)
        return fraga


# ---------------------------------------------------------------------------
# MCP-server
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "danmark",
    instructions=(
        "MCP-server för dansk riksdags- och rättsdata: Folketingets öppna data (ODA) "
        "och konsoliderad lagstiftning från Retsinformation. Verktygen har prefixet dk_. "
        "LOKALT OCH LIVE: dk_sok, dk_sok_folketing, dk_sok_lovgivning, dk_sok_semantisk "
        "och dk_sok_i_dokument läser en lokal databas som synkas dagligen. "
        "dk_hamta_afstemning, dk_hamta_aktor, dk_lista_perioder och dk_hamta_dokument "
        "med sagid hämtar live från ODA. "
        "KEDJOR: sök → dk_hamta_dokument(dok_id) → dk_sok_i_dokument(dok_id, fraga) för "
        "enskilda passager. För ett ärende: sagid ur dk_sok_folketing → "
        "dk_hamta_dokument(sagid) eller dk_hamta_afstemning(sagid) → "
        "dk_hamta_aktor(aktorider) för namn och parti. "
        "GÄLLANDE RÄTT: dk_sok_lovgivning visar som standard bara gällande lagstiftning; "
        "historiska träffar märks med historisk=true. dk_hamta_dokument varnar när "
        "ändringslagar tillkommit efter den konsoliderade versionen. "
        "SVARSSTORLEK: dk_hamta_dokument kapar fulltexten vid max_tecken. Ett kapat svar "
        "bär trunkerad, tecken_totalt och fortsatt_fran_tecken; citera aldrig ordagrant "
        "ur ett kapat svar utan att läsa vidare med fran_tecken."
    ),
    version="1.2.0",
    cache_hints=CACHE_HINTAR,
)


def _formatera_treff(dok: dict) -> Traff:
    """
    Formaterar ett råt dokument-dict från DB för MCP-svar.

    Döper om internt `id` → `dok_id` och exponerar `extern_id` med
    källspecifikt namn:
      - kalla='oda'             → sagid (ODA sagid, ingång till dk_hamta_dokument/dk_hamta_afstemning)
      - kalla='retsinformation' → retsinformation_id
    Övriga fält bevaras oförändrade. Dedup i anropande funktioner sker på
    det råa `dok["id"]` innan denna funktion anropas — ingen risk för
    dubblett-kollision vid rename.
    """
    kalla = dok.get("kalla", "")
    treff = {k: v for k, v in dok.items() if k not in ("id", "extern_id")}
    treff["dok_id"] = dok["id"]
    if kalla == "oda" and dok.get("extern_id"):
        treff["sagid"] = dok["extern_id"]
    elif kalla == "retsinformation" and dok.get("extern_id"):
        treff["retsinformation_id"] = dok["extern_id"]
    return treff


def _typ_matchar(dok: dict, typ: Optional[str]) -> bool:
    """Jämför dokumenttyp skiftlägesokänsligt. NULL i databasen matchar aldrig ett filter."""
    return not typ or (dok.get("typ") or "").lower() == typ.lower()


@mcp.tool(title="Sök i alla danska källor", annotations=LASNING_DB)
def dk_sok(
    sokterm: Annotated[str, Field(description="Sökterm eller kommaseparerade termer (OR-logik), t.ex. 'klima, CO2, drivhusgas'")],
    typ: Annotated[Optional[str], Field(description="Filtrera på dokumenttyp: 'lovforslag', 'lov', 'bekendtgorelse', 'betaenkning' m.fl.")] = None,
    periode: Annotated[Optional[str], Field(description="Filtrera på valperiod, t.ex. '20242' (2024-25)")] = None,
    max_traffar: Annotated[int, Field(description="Max antal resultat (standard 20)")] = 20,
    expandera: Annotated[bool, Field(description="Expandera söktermen med juridisk terminologi (standard true)")] = True,
) -> SokSvar:
    """Söker i alla danska källor (Folketing ODA + Retsinformation) via lokal databas. Accepterar kommaseparerade söktermer (OR-logik). Stöder termexpansion till dansk parlamentarisk och juridisk terminologi."""
    expansion = _expandera_fraga(sokterm) if expandera else None
    effektiv_term = expansion if expansion is not None else sokterm

    termer = [t.strip() for t in effektiv_term.split(",") if t.strip()]
    resultat: list[Traff] = []
    sett_ids = set()
    with _som_toolerror("sökningen"):
        for term in termer:
            for dok in db.sok_dokument_fts(term, limit=max_traffar):
                if dok["id"] in sett_ids:
                    continue
                if not _typ_matchar(dok, typ):
                    continue
                if periode and dok.get("periode") != periode:
                    continue
                sett_ids.add(dok["id"])
                resultat.append(_formatera_treff(dok))

    return {
        "sokterm": sokterm,
        "expansion": expansion,
        "antal_traffar": len(resultat),
        "traffar": resultat[:max_traffar],
    }


@mcp.tool(title="Sök i Folketingets ärenden", annotations=LASNING_DB)
def dk_sok_folketing(
    sokterm: Annotated[str, Field(description="Sökterm eller kommaseparerade termer")],
    typ: Annotated[Optional[str], Field(description="Dokumenttyp: 'lovforslag', 'beslutningsforslag', 'foresporgsel', 'betaenkning'")] = None,
    periode: Annotated[Optional[str], Field(description="Valperiod, t.ex. '20242'")] = None,
    paragrafnummer: Annotated[Optional[str], Field(description="Filtrera på paragrafnummer, t.ex. '15' för § 15")] = None,
    max_traffar: int = 20,
) -> FolketingSvar:
    """Söker i Folketing ODA — lovforslag, beslutningsforslag, betænkninger, forespørgsler. Sökning mot lokal databas (FTS + pgvector). Returnerar sagid, typ, titel, resume, paragrafnummer m.fl. Stöder filtrering på paragrafnummer för direkt §-koppling."""
    termer = [t.strip() for t in sokterm.split(",") if t.strip()]
    resultat: list[Traff] = []
    sett_ids = set()
    with _som_toolerror("sökningen"):
        for term in termer:
            for dok in db.sok_dokument_fts(term, limit=max_traffar * 2, kalla="oda"):
                if dok["id"] in sett_ids:
                    continue
                if not _typ_matchar(dok, typ):
                    continue
                if periode and dok.get("periode") != periode:
                    continue
                if paragrafnummer and dok.get("paragrafnummer") != paragrafnummer:
                    continue
                sett_ids.add(dok["id"])
                resultat.append(_formatera_treff(dok))

    return {
        "kalla": "Folketing ODA",
        "sokterm": sokterm,
        "antal_traffar": len(resultat),
        "traffar": resultat[:max_traffar],
    }


@mcp.tool(title="Sök i dansk lagstiftning", annotations=LASNING_DB)
def dk_sok_lovgivning(
    sokterm: Annotated[str, Field(description="Sökterm eller kommaseparerade termer")],
    typ: Annotated[Optional[str], Field(description="Lagtyp: 'lov', 'lovbekendtgorelse', 'bekendtgorelse', 'cirkular', 'vejledning'")] = None,
    inkludera_historiska: Annotated[bool, Field(description="Inkludera historiska/upphävda lagar (HISTORISK). Sätt true endast om användaren explicit efterfrågar det.")] = False,
    max_traffar: int = 20,
) -> LovSvar:
    """Söker i dansk lagstiftning från Retsinformation — love (LOV), lovbekendtgørelser (LBK), bekendtgørelser (BEK), cirkulærer (CIR), vejledninger (VEJ). Sökning mot lokal databas.

    Som standard returneras endast GÆLDENDE (gällande) lagstiftning — dokument med status 'Valid' i Retsinformations Lex Dania-system. Historiska lagar (HISTORISK/notInForce) är upphävda och inte längre gällande rätt; de inkluderas inte i standardsökningen. Sätt inkludera_historiska=true om användaren explicit efterfrågar historiska eller upphävda regler. Historiska träffar markeras med historisk=true i svaret.
    """
    termer = [t.strip() for t in sokterm.split(",") if t.strip()]
    resultat: list[LovTraff] = []
    sett_ids = set()
    with _som_toolerror("sökningen"):
        for term in termer:
            for dok in db.sok_dokument_fts(
                term,
                limit=max_traffar * 2,
                kalla="retsinformation",
                inkludera_ersatta=inkludera_historiska,
            ):
                if dok["id"] in sett_ids:
                    continue
                if not _typ_matchar(dok, typ):
                    continue
                sett_ids.add(dok["id"])

                # giltig_till döljs; ikraftträdandedatum exponeras. `dok_id`
                # matchar parametern i dk_hamta_dokument, och
                # `retsinformation_id` gör kedjning via extern identifierare möjlig.
                treff: LovTraff = {
                    "dok_id":               dok["id"],
                    "beteckning":           dok.get("beteckning"),
                    "typ":                  dok.get("typ"),
                    "titel":                dok.get("titel"),
                    "titelkort":            dok.get("titelkort"),
                    "ikrafttraedelsesdato": dok.get("datum"),
                    "url":                  dok.get("retsinformationsurl") or dok.get("url"),
                    "lovnummer":            dok.get("lovnummer"),
                    "resume":               dok.get("resume"),
                }
                if dok.get("extern_id"):
                    treff["retsinformation_id"] = dok["extern_id"]
                if dok.get("status") == "Historic":
                    treff["historisk"] = True
                    treff["advarsel"] = "Historisk lag — inte längre gällande rätt"
                resultat.append(treff)

    return {
        "kalla": "Retsinformation",
        "sokterm": sokterm,
        "inkludera_historiska": inkludera_historiska,
        "antal_traffar": len(resultat[:max_traffar]),
        "traffar": resultat[:max_traffar],
    }


@mcp.tool(title="Hämta danskt dokument eller ärende", annotations=LASNING_EXTERN)
def dk_hamta_dokument(
    dok_id: Annotated[Optional[int], Field(description="Internt databas-id (matchar `dok_id`-fältet i dk_sok-resultat)")] = None,
    sagid: Annotated[Optional[int], Field(description="ODA sagid (matchar `sagid`-fältet i dk_sok_folketing-resultat)")] = None,
    max_tecken: Annotated[int, Field(description=(
        f"Teckentak för fulltexten (standard {DK_MAX_TECKEN}, högst {DK_MAX_TECKEN_TAK}; "
        f"0 = så mycket som ryms, alltså {DK_MAX_TECKEN_TAK}). Danska lagtexter når nära "
        "en miljon tecken och läses då i flera anrop. Ett kapat svar bär trunkerad, "
        "tecken_totalt och fortsatt_fran_tecken."
    ))] = DK_MAX_TECKEN,
    fran_tecken: Annotated[int, Field(description="Börja texten vid denna teckenposition — för att läsa vidare.")] = 0,
) -> Dokument | Sag:
    """Hämtar fulltext och metadata för ett danskt dokument via dess interna id eller ODA sagid. Om fulltexten inte finns i cache hämtas PDF:en från ft.dk med curl-cffi och extraheras med pymupdf4llm. OBS: Vid sagid-uppslag returneras hela sagen plus listan över kopplade dokument (upp till 50). Äldre dokument (typiskt före 2015) saknar Fil-records i ODA, så fil_url kan vara null. För antagna lagar kan lagtexten ändå nås via retsinformationsurl eller via dk_sok_lovgivning."""
    if max_tecken <= 0 or max_tecken > DK_MAX_TECKEN_TAK:
        max_tecken = DK_MAX_TECKEN_TAK
    fran_tecken = max(0, fran_tecken)

    if dok_id is None:
        if sagid:
            # sagid pekar alltid mot en sag i ODA och hämtas live därifrån.
            # Den lokala dokumenttabellen lagrar både dokumentid och sagid i
            # extern_id, så ett uppslag där kunde kollidera med ett orelaterat
            # dokument.
            return _hamta_sag_fran_oda(sagid)
        raise ToolError("Ange dok_id (från sökverktygen) eller sagid (från dk_sok_folketing).")

    with _som_toolerror(f"hämtning av dokument {dok_id}"):
        dok = db.hamta_dokument_med_id(dok_id)
    if not dok:
        raise ToolError(
            f"Dokument {dok_id} hittades inte i databasen. dok_id kommer ur "
            "sökverktygen; ett ODA-ärende hämtas med sagid."
        )

    # Saknas fulltext men finns en URL hämtas PDF:en och sparas i databasen
    if not dok.get("fulltext_md") and dok.get("url"):
        logger.info("Hämtar PDF för dok %s: %s", dok.get("id"), dok.get("url"))
        pdf_bytes = _hamta_pdf_bytes(dok["url"])
        if pdf_bytes:
            text = _extrahera_pdf_text(pdf_bytes)
            if text:
                dok["fulltext_md"] = text
                try:
                    with db._cursor() as cur:
                        p = db._prefix()
                        ph = "%s" if db._ar_postgres() else "?"
                        cur.execute(
                            f"UPDATE {p}dokument SET fulltext_md = {ph} WHERE id = {ph}",
                            (text, dok["id"])
                        )
                except Exception as e:
                    logger.warning("Kunde inte spara fulltext: %s", e)

    # giltig_till döljs; ikraftträdandedatum exponeras
    svar: Dokument = {
        "id":                   dok["id"],
        "kalla":                dok.get("kalla"),
        "beteckning":           dok.get("beteckning"),
        "typ":                  dok.get("typ"),
        "titel":                dok.get("titel"),
        "titelkort":            dok.get("titelkort"),
        "ikrafttraedelsesdato": dok.get("datum"),
        "url":                  dok.get("retsinformationsurl") or dok.get("url"),
        "lovnummer":            dok.get("lovnummer"),
        "resume":               dok.get("resume"),
        "status":               dok.get("status"),
        "fulltext_md":          None,
    }

    # Databasen har alltid hela texten — trunkeringen gäller bara svaret.
    if dok.get("fulltext_md"):
        utdrag = _skar_ut(dok["fulltext_md"], max_tecken, fran_tecken)
        svar["fulltext_md"]          = utdrag["text"]
        svar["tecken_totalt"]        = utdrag["tecken_totalt"]
        svar["tecken_visade"]        = utdrag["tecken_visade"]
        svar["trunkerad"]            = utdrag["trunkerad"]
        svar["fortsatt_fran_tecken"] = utdrag["fortsatt_fran_tecken"]

    if dok.get("status") == "Historic":
        svar["historisk"] = True
        svar["advarsel"] = "Historisk lag — inte längre gällande rätt"

    # Ändringslagar ur relationstabellen
    eli_url = dok.get("retsinformationsurl") or dok.get("url")
    if eli_url and dok.get("kalla") == "retsinformation":
        try:
            andringar = db.hamta_andringar_for_lag(eli_url)
            if andringar:
                svar["andringar_efter_senaste_lbk"] = [
                    {
                        "beteckning":           a.get("beteckning"),
                        "typ":                  a.get("typ"),
                        "titel":                a.get("titel"),
                        "ikrafttraedelsesdato": a.get("datum"),
                        "url":                  a.get("retsinformationsurl") or a.get("url"),
                        "lovnummer":            a.get("lovnummer"),
                    }
                    for a in andringar
                ]
                svar["advarsel_andringar"] = (
                    f"OBS: {len(andringar)} ändringslag(ar) har tillkommit efter denna version. "
                    "Texten i denna LBK/LOV kan vara delvis inaktuell. "
                    "Se andringar_efter_senaste_lbk för detaljer."
                )
        except Exception as e:
            logger.warning("Kunde inte hämta relationer för %s: %s", eli_url, e)

    return svar


def _hamta_sag_fran_oda(sagid: int) -> Sag:
    """Hämtar ett ärende direkt från ODA (inte via cache)."""
    with _som_toolerror(f"hämtning av sag {sagid}"):
        try:
            sag_data = _oda_get(f"Sag({sagid})")
        except httpx.HTTPStatusError as fel:
            if fel.response.status_code == 404:
                raise ToolError(f"Ärendet med sagid {sagid} finns inte i ODA.") from fel
            raise
        sag = sag_data.get("value", sag_data)

        # Upp till 50 kopplade dokument, så att hela ärendetråden kommer med
        # (lovforslag, betænkninger, ændringsforslag, slutligt antagen lov).
        sd_data = _oda_get("SagDokument", {"$filter": f"sagid eq {sagid}", "$top": "50"})

    dokument_lista: list[SagDokument] = []
    for sd in sd_data.get("value", []):
        dok_id_oda = sd.get("dokumentid")
        if not dok_id_oda:
            continue
        try:
            dok_data = _oda_get(f"Dokument({dok_id_oda})")
            dok = dok_data.get("value", dok_data)
            fil_data = _oda_get("Fil", {"$filter": f"dokumentid eq {dok_id_oda}", "$top": "1"})
            filer = fil_data.get("value", [])
            fil_url = filer[0].get("filurl") if filer else None
            dokument_lista.append({
                "dokumentid": dok_id_oda,
                "titel":      dok.get("titel"),
                # typeid skiljer lovforslag, betænkning, ændringsforslag,
                # lovvedtagelse osv. — nödvändigt för att tråda processen rätt.
                "typeid":     dok.get("typeid"),
                "dato":       dok.get("dato"),
                "fil_url":    fil_url,
            })
        except Exception as e:
            logger.warning("Kunde inte hämta dokument %s: %s", dok_id_oda, e)

    return {
        "sagid": sagid,
        "beteckning": sag.get("nummer"),
        "titel": sag.get("titel"),
        "titelkort": sag.get("titelkort"),
        "typeid": sag.get("typeid"),
        "statusid": sag.get("statusid"),
        "periodeid": sag.get("periodeid"),
        "resume": sag.get("resume") or None,
        "afstemningskonklusion": sag.get("afstemningskonklusion") or None,
        "lovnummer": sag.get("lovnummer") or None,
        "retsinformationsurl": sag.get("retsinformationsurl") or None,
        "paragrafnummer": str(sag.get("paragrafnummer") or "").strip() or None,
        "paragraf": sag.get("paragraf") or None,
        "afgoerelse": sag.get("afgørelse") or None,
        "begrundelse": sag.get("begrundelse") or None,
        "baggrundsmateriale": sag.get("baggrundsmateriale") or None,
        "dokument": dokument_lista,
    }


@mcp.tool(title="Lista Folketingets valperioder", annotations=LASNING_EXTERN)
def dk_lista_perioder() -> list[Periode]:
    """Returnerar tillgängliga valperioder från Folketing ODA. Period-ID-format: '20242' (fyrsiffrigt år + ettciffrigt löpnummer)."""
    with _som_toolerror("hämtning av perioder"):
        data = _oda_get("Periode", {"$orderby": "id desc", "$top": "20"})
    return [
        {
            "id": p.get("id"),
            "kod": p.get("kode"),
            "titel": p.get("titel"),
            "startdatum": p.get("startdato"),
            "slutdatum": p.get("slutdato"),
        }
        for p in data.get("value", [])
    ]


@mcp.tool(title="Hämta voteringsresultat för ett ärende", annotations=LASNING_EXTERN)
def dk_hamta_afstemning(
    sagid: Annotated[int, Field(description="ODA sagid för ärendet")],
    inkludera_per_ledamot: Annotated[bool, Field(description="Inkludera per-ledamot-röstning (standard true)")] = True,
) -> AfstemningSvar:
    """Hämtar voteringsresultat för ett ärende (sag) via ODA. Returnerar totalresultat (for/imod/hverken/fraværende) och per-ledamot-röstning via Stemme-entiteten. Navigerar via Sagstrin → Afstemning → Stemme. OBS: Per-ledamot-svaret innehåller aktørid och typeid (1=For, 2=Imod, 3=Fravær, 4=Hverken) — inget namnuppslag sker automatiskt. Slå upp aktørnamn och parti via ODA Aktør({aktørid}) vid behov."""
    with _som_toolerror(f"hämtning av sagstrin för sagid {sagid}"):
        st_data = _oda_get("Sagstrin", {"$filter": f"sagid eq {sagid}"})
    sagstrin_lista = st_data.get("value", [])
    if not sagstrin_lista:
        raise ToolError(
            f"Inga sagstrin hittades för sagid {sagid}. Kontrollera sagid med "
            "dk_sok_folketing; ärenden utan behandling i salen har inga voteringar."
        )

    afstemningar: list[Afstemning] = []
    for strin in sagstrin_lista:
        strinid = strin.get("id")
        try:
            af_data = _oda_get("Afstemning", {"$filter": f"sagstrinid eq {strinid}"})
        except Exception as e:
            logger.warning("Afstemning-hämtning misslyckades (sagstrinid=%s): %s", strinid, e)
            continue
        for af in af_data.get("value", []):
            afstemningid = af.get("id")
            post: Afstemning = {
                "afstemningid": afstemningid,
                "sagstrinid": strinid,
                "sagstrin_typeid": strin.get("typeid"),
                "konklusion": af.get("konklusion"),
                "for": af.get("for"),
                "imod": af.get("imod"),
                "hverken": af.get("hverken"),
                "fravaerende": af.get("fravaerende"),
                "vedtaget": af.get("vedtaget"),
            }
            if inkludera_per_ledamot and afstemningid:
                try:
                    stemme_data = _oda_get(
                        "Stemme",
                        {"$filter": f"afstemningid eq {afstemningid}", "$top": "500"},
                    )
                    # typeid: 1=For, 2=Imod, 3=Fravær, 4=Hverken
                    post["stemmer"] = [
                        {"aktørid": s.get("aktørid"), "typeid": s.get("typeid")}
                        for s in stemme_data.get("value", [])
                    ]
                except Exception as e:
                    logger.warning("Stemme-hämtning misslyckades (afstemningid=%s): %s", afstemningid, e)
                    post["stemmer"] = []
            afstemningar.append(post)

    return {
        "sagid": sagid,
        "antal_afstemninger": len(afstemningar),
        "afstemninger": afstemningar,
    }


@mcp.tool(title="Semantisk sökning i dansk korpus", annotations=LASNING_DB)
def dk_sok_semantisk(
    sokterm: Annotated[str, Field(description="Sökfråga på danska (eller svenska/engelska) — formuleras som en mening för bäst resultat")],
    max_traffar: Annotated[int, Field(description="Max antal resultat (standard 20)")] = 20,
) -> SemantiskSvar:
    """Semantisk sökning över hela den danska korpusen via pgvector (cosinus-likhet) — returnerar topp-N olika dokument (avduplicerade på dok_id). Använd detta verktyg för dokumentupptäckt på begreppsfrågor. För sökning inom ett enskilt cachat dokument, använd dk_sok_i_dokument. Kräver att 04_chunka_och_embedda.py körts och embeddings finns i databasen. Modell: intfloat/multilingual-e5-base (768 dim). Termexpansion körs inte här — vektorsökning hittar synonymer via semantisk likhet."""
    if not db._ar_postgres():
        raise ToolError(_SQLITE_EJ_VEKTOR)

    with _som_toolerror("den semantiska sökningen"):
        resultat = _hamta_chunka_modul().semantisk_sok(sokterm, limit=max_traffar)

    if not resultat:
        raise ToolError(
            "Inga semantiska träffar. Embeddings saknas troligen i databasen; "
            "kör 04_chunka_och_embedda.py."
        )

    return {
        "sokterm": sokterm,
        "metod": "pgvector cosinus-likhet (intfloat/multilingual-e5-base, 768 dim)",
        "antal_traffar": len(resultat),
        "traffar": resultat,
    }


@mcp.tool(title="Semantisk sökning i ett dokument", annotations=LASNING_DB)
def dk_sok_i_dokument(
    dok_id: Annotated[int, Field(description="Internt databas-id för dokumentet (matchar dok_id-fältet i dk_sok-resultat)")],
    fraga: Annotated[str, Field(description="Sökfråga på danska (eller svenska/engelska) — formuleras som en mening eller fras för bäst resultat")],
    max_treff: Annotated[int, Field(description="Max antal chunk-träffar att returnera (standard 5)")] = 5,
) -> SokIDokumentSvar:
    """Semantisk sökning inom ett enskilt cachat dokument via pgvector (cosinus-likhet). Returnerar topp-N chunk-träffar sorterade efter relevans, med chunk_nr och text. Använd när du behöver hitta specifika passager i ett dokument du redan identifierat (t.ex. via dk_sok eller dk_sok_lovgivning). dok_id är det interna databas-id:t som returneras av sökverktygen. Kräver PostgreSQL med pgvector — SQLite-läge stöds inte. Modell: intfloat/multilingual-e5-base (768 dim)."""
    if not db._ar_postgres():
        raise ToolError(_SQLITE_EJ_VEKTOR)

    with _som_toolerror(f"sökningen i dokument {dok_id}"):
        resultat = _hamta_chunka_modul().semantisk_sok_i_dokument(dok_id, fraga, limit=max_treff)

    # semantisk_sok_i_dokument signalerar okänt dokument och saknade chunks med ett fel-fält
    if "fel" in resultat:
        raise ToolError(resultat["fel"])
    return resultat


@mcp.tool(title="Hämta aktör ur Folketingets data", annotations=LASNING_EXTERN)
def dk_hamta_aktor(
    aktorid: Annotated[Optional[int], Field(description="ODA aktørid för enskilt uppslag")] = None,
    aktorider: Annotated[Optional[list[int]], Field(description="Lista av ODA aktørid:n för batch-uppslag (t.ex. från dk_hamta_afstemning)")] = None,
) -> Aktor | AktorBatch:
    """Hämtar metadata för en eller flera aktörer (ledamöter, ministrar, partier, ministerier, utskott, m.fl.) från Folketing ODA via Aktør-entiteten. Använd för att översätta aktørid:n från dk_hamta_afstemning till läsbara namn och partitillhörighet. Ange antingen aktorid (enskilt uppslag) eller aktorider (lista, batch-uppslag — rekommenderas vid uppslag av många aktörer från en votering, t.ex. 179 ledamöter). Returnerar typeid som anger aktörstyp (vanligast 1=Ministerium, 2=Folketinget, 3=Udvalg, 4=Folketingsgruppe/parti, 5=Person; andra typer förekommer och returneras transparent — den kompletta listan finns i ODA-entiteten /Aktørtype), gruppenavnkort (parti) och biografi-fält. Anropas live mot ODA — ingen cache."""
    if aktorid is None and not aktorider:
        raise ToolError("Ange antingen aktorid (enskilt uppslag) eller aktorider (lista för batch-uppslag).")
    if aktorid is not None and aktorider:
        raise ToolError("Ange antingen aktorid eller aktorider, inte båda.")

    ids = [aktorid] if aktorid is not None else list(aktorider)

    aktorer: list[Aktor] = []
    fel: list[AktorBatchFel] = []

    # ODA:s $filter har en URL-längdgräns (~2000 tecken). "id eq 99999 or "
    # är ~15 tecken, så 50 per anrop ger marginal.
    BATCH = 50
    for start in range(0, len(ids), BATCH):
        batch = ids[start:start + BATCH]
        filter_uttryck = " or ".join(f"id eq {aid}" for aid in batch)
        try:
            data = _oda_get("Aktør", {"$filter": filter_uttryck, "$top": str(BATCH)})
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Aktör-batch %s misslyckades: %s", batch, e)
            fel.append({"batch": batch, "fel": str(e)})
            continue

        for rad in data.get("value", []):
            aktorer.append({
                "aktørid":          rad.get("id"),
                # 1=Ministerium, 2=Folketinget, 3=Udvalg,
                # 4=Folketingsgruppe (parti), 5=Person
                "typeid":           rad.get("typeid"),
                "navn":             rad.get("navn"),
                "fornavn":          rad.get("fornavn"),
                "efternavn":        rad.get("efternavn"),
                "gruppenavnkort":   rad.get("gruppenavnkort"),
                "biografi":         rad.get("biografi"),
                "startdato":        rad.get("startdato"),
                "slutdato":         rad.get("slutdato"),
                "opdateringsdato":  rad.get("opdateringsdato"),
            })

    # Enskilt uppslag — objektet direkt, så att svaret blir lätt att läsa
    if aktorid is not None:
        if fel:
            raise ToolError(
                f"Folketingets ODA gick inte att fråga om aktørid={aktorid}: {fel[0]['fel']}. "
                "Försök igen senare."
            )
        if not aktorer:
            raise ToolError(f"Ingen aktör hittades med aktørid={aktorid}.")
        return aktorer[0]

    # Batch-uppslag — lista plus räknare, så att det syns om något saknas
    svar: AktorBatch = {
        "begart_antal":     len(ids),
        "returnerat_antal": len(aktorer),
        "aktorer":          aktorer,
    }
    if fel:
        svar["fel"] = fel
    return svar


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def _initiera() -> None:
    """Initierar databasschemat. Fel fångas av starta(), så att servern går
    upp även när Postgres är nere; verktygsanropen felar då begripligt."""
    db.initialisera_schema()
    logger.info("Databasschema initialiserat (schema: danmark)")


if __name__ == "__main__":
    starta(mcp, standardport=8714, initiera=_initiera, forvarm_http=_forvarm_embedding)
