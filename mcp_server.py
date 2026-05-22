"""
mcp_server.py — MCP-server för dansk riksdags- och rättsdata (ström 14).

Datakällor:
  - Folketing ODA (oda.ft.dk): sager, dokument, afstemninger, ledamöter
  - Retsinformation (api.retsinformation.dk): dansk lagstiftning via harvest-API

Prefix: dk_
Schema: danmark
"""

import os
import sys
import json
import logging
import contextlib
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Ladda .env relativt skriptets mapp
_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

# Loggning till fil (MCP stdio kräver ren stdout)
_LOG_DIR = _SCRIPT_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_LOG_DIR / "mcp_server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

import db
import importlib.util as _iutil, pathlib as _pl

# Lazy-import av hela chunka/embedda-modulen. Filnamnet börjar med en siffra
# och kan inte importeras direkt — importlib används istället. Modulen laddas
# en gång och cachas; därefter exponeras enskilda funktioner via wrappers.
_chunka_modul = None


def _hamta_chunka_modul():
    """Laddar 04_chunka_och_embedda.py och returnerar modulobjektet (lazy)."""
    global _chunka_modul
    if _chunka_modul is None:
        modul_vag = _pl.Path(__file__).parent / "04_chunka_och_embedda.py"
        spec = _iutil.spec_from_file_location("chunka_embedda", modul_vag)
        modul = _iutil.module_from_spec(spec)
        spec.loader.exec_module(modul)
        _chunka_modul = modul
    return _chunka_modul


def _hamta_semantisk_sok():
    return _hamta_chunka_modul().semantisk_sok


def _hamta_semantisk_sok_i_dokument():
    return _hamta_chunka_modul().semantisk_sok_i_dokument

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ODA_BAS_URL = "https://oda.ft.dk/api"
RETSINFORMATION_BAS_URL = "https://api.retsinformation.dk/v1"

QUERY_EXPANSION_BASE_URL = os.getenv("QUERY_EXPANSION_BASE_URL", "http://localhost:11434/v1")
QUERY_EXPANSION_API_KEY  = os.getenv("QUERY_EXPANSION_API_KEY", "ollama")
QUERY_EXPANSION_MODEL    = os.getenv("QUERY_EXPANSION_MODEL", "llama3")
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST      = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT", "8714"))
MCP_API_KEY   = os.getenv("MCP_API_KEY", "")

PDF_CACHE_DIR = Path(os.getenv("PDF_CACHE_DIR", str(_SCRIPT_DIR / "pdf_cache")))
if not PDF_CACHE_DIR.is_absolute():
    PDF_CACHE_DIR = _SCRIPT_DIR / PDF_CACHE_DIR
PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)

PDF_CACHE_TTL_DAGAR = int(os.getenv("PDF_CACHE_TTL_DAGAR", "1"))

# ---------------------------------------------------------------------------
# HTTP-klient (curl-cffi för ft.dk PDFer, httpx för övriga)
# ---------------------------------------------------------------------------

try:
    from curl_cffi import requests as cf_requests
    _CURL_CFFI_TILLGANGLIG = True
except ImportError:
    _CURL_CFFI_TILLGANGLIG = False
    logger.warning("curl-cffi saknas — ft.dk PDF-nedladdning ej tillgänglig")

import httpx

_HTTPX_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "mcp-for-folketinget-retsinformation/1.0 (+https://github.com/MagnusKolsjo/mcp-for-folketinget-retsinformation)",
}


def _oda_get(endpoint: str, params: dict = None) -> dict:
    """GET-anrop mot Folketing ODA. Lägger till $format=json automatiskt."""
    if params is None:
        params = {}
    params.setdefault("$format", "json")
    url = f"{ODA_BAS_URL}/{endpoint}"
    logger.info("ODA GET %s %s", url, params)
    resp = httpx.get(url, params=params, headers=_HTTPX_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _retsinformation_get(endpoint: str, params: dict = None) -> dict:
    """GET-anrop mot Retsinformation harvest-API."""
    if params is None:
        params = {}
    url = f"{RETSINFORMATION_BAS_URL}/{endpoint}"
    logger.info("Retsinformation GET %s %s", url, params)
    resp = httpx.get(url, params=params, headers=_HTTPX_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


@contextlib.contextmanager
def _tysta_subprocess_stdout():
    """OS-nivå redirigering av FD 1 under bullriga C-bindningsanrop."""
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
            with _tysta_subprocess_stdout():
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

server = Server("danmark")


@server.list_tools()
async def lista_verktyg():
    return [
        types.Tool(
            name="dk_sok",
            description=(
                "Söker i alla danska källor (Folketing ODA + Retsinformation) via lokal databas. "
                "Accepterar kommaseparerade söktermer (OR-logik). "
                "Stöder termexpansion till dansk parlamentarisk och juridisk terminologi."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sokterm": {
                        "type": "string",
                        "description": "Sökterm eller kommaseparerade termer (OR-logik), t.ex. 'klima, CO2, drivhusgas'",
                    },
                    "typ": {
                        "type": "string",
                        "description": "Filtrera på dokumenttyp: 'lovforslag', 'lov', 'bekendtgorelse', 'betaenkning' m.fl.",
                    },
                    "periode": {
                        "type": "string",
                        "description": "Filtrera på valperiod, t.ex. '20242' (2024-25)",
                    },
                    "max_traffar": {
                        "type": "integer",
                        "description": "Max antal resultat (standard 20)",
                        "default": 20,
                    },
                    "expandera": {
                        "type": "boolean",
                        "description": "Expandera söktermen med juridisk terminologi (standard true)",
                        "default": True,
                    },
                },
                "required": ["sokterm"],
            },
        ),
        types.Tool(
            name="dk_sok_folketing",
            description=(
                "Söker i Folketing ODA — lovforslag, beslutningsforslag, betænkninger, "
                "forespørgsler. Sökning mot lokal databas (FTS + pgvector). "
                "Returnerar sagid, typ, titel, resume, paragrafnummer m.fl. "
                "Stöder filtrering på paragrafnummer för direkt §-koppling."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sokterm": {
                        "type": "string",
                        "description": "Sökterm eller kommaseparerade termer",
                    },
                    "typ": {
                        "type": "string",
                        "description": "Dokumenttyp: 'lovforslag', 'beslutningsforslag', 'foresporgsel', 'betaenkning'",
                    },
                    "periode": {
                        "type": "string",
                        "description": "Valperiod, t.ex. '20242'",
                    },
                    "paragrafnummer": {
                        "type": "string",
                        "description": "Filtrera på paragrafnummer, t.ex. '15' för § 15",
                    },
                    "max_traffar": {
                        "type": "integer",
                        "default": 20,
                    },
                },
                "required": ["sokterm"],
            },
        ),
        types.Tool(
            name="dk_sok_lovgivning",
            description=(
                "Söker i dansk lagstiftning från Retsinformation — "
                "love (LOV), lovbekendtgørelser (LBK), bekendtgørelser (BEK), "
                "cirkulærer (CIR), vejledninger (VEJ). Sökning mot lokal databas.\n\n"
                "Som standard returneras endast GÆLDENDE (gällande) lagstiftning — "
                "dokument med status 'Valid' i Retsinformations Lex Dania-system. "
                "Historiska lagar (HISTORISK/notInForce) är upphävda och inte längre "
                "gällande rätt; de inkluderas inte i standardsökningen. "
                "Sätt inkludera_historiska=true om användaren explicit efterfrågar "
                "historiska eller upphävda regler. Historiska träffar markeras med "
                "historisk=true i svaret."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sokterm": {
                        "type": "string",
                        "description": "Sökterm eller kommaseparerade termer",
                    },
                    "typ": {
                        "type": "string",
                        "description": "Lagtyp: 'lov', 'lovbekendtgorelse', 'bekendtgorelse', 'cirkular', 'vejledning'",
                    },
                    "inkludera_historiska": {
                        "type": "boolean",
                        "default": False,
                        "description": "Inkludera historiska/upphävda lagar (HISTORISK). Sätt true endast om användaren explicit efterfrågar det.",
                    },
                    "max_traffar": {
                        "type": "integer",
                        "default": 20,
                    },
                },
                "required": ["sokterm"],
            },
        ),
        types.Tool(
            name="dk_hamta_dokument",
            description=(
                "Hämtar fulltext och metadata för ett danskt dokument via dess interna id "
                "eller ODA sagid. Om fulltexten inte finns i cache hämtas PDF:en från ft.dk "
                "med curl-cffi och extraheras med pymupdf4llm. "
                "OBS: Vid sagid-uppslag returneras hela sagen plus listan över kopplade "
                "dokument (upp till 50). Äldre dokument (typiskt före 2015) saknar "
                "Fil-records i ODA, så fil_url kan vara null. För antagna lagar kan "
                "lagtexten ändå nås via retsinformationsurl eller via dk_sok_lovgivning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dok_id": {
                        "type": "integer",
                        "description": "Internt databas-id (matchar `dok_id`-fältet i dk_sok-resultat)",
                    },
                    "sagid": {
                        "type": "integer",
                        "description": "ODA sagid (matchar `sagid`-fältet i dk_sok_folketing-resultat)",
                    },
                },
            },
        ),
        types.Tool(
            name="dk_lista_perioder",
            description=(
                "Returnerar tillgängliga valperioder från Folketing ODA. "
                "Period-ID-format: '20242' (fyrsiffrigt år + ettciffrigt löpnummer)."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="dk_hamta_afstemning",
            description=(
                "Hämtar voteringsresultat för ett ärende (sag) via ODA. "
                "Returnerar totalresultat (for/imod/hverken/fraværende) och "
                "per-ledamot-röstning via Stemme-entiteten. "
                "Navigerar via Sagstrin → Afstemning → Stemme. "
                "OBS: Per-ledamot-svaret innehåller aktørid och typeid (1=For, 2=Imod, "
                "3=Fravær, 4=Hverken) — inget namnuppslag sker automatiskt. "
                "Slå upp aktørnamn och parti via ODA Aktør({aktørid}) vid behov."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sagid": {
                        "type": "integer",
                        "description": "ODA sagid för ärendet",
                    },
                    "inkludera_per_ledamot": {
                        "type": "boolean",
                        "description": "Inkludera per-ledamot-röstning (standard true)",
                        "default": True,
                    },
                },
                "required": ["sagid"],
            },
        ),
        types.Tool(
            name="dk_sok_semantisk",
            description=(
                "Semantisk sökning över hela den danska korpusen via pgvector (cosinus-likhet) — "
                "returnerar topp-N olika dokument (avduplicerade på dok_id). "
                "Använd detta verktyg för dokumentupptäckt på begreppsfrågor. "
                "För sökning inom ett enskilt cachat dokument, använd dk_sok_i_dokument. "
                "Kräver att 04_chunka_och_embedda.py körts och embeddings finns i databasen. "
                "Modell: intfloat/multilingual-e5-base (768 dim). "
                "Termexpansion körs inte här — vektorsökning hittar synonymer via semantisk likhet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sokterm": {
                        "type": "string",
                        "description": "Sökfråga på danska (eller svenska/engelska) — formuleras som en mening för bäst resultat",
                    },
                    "max_traffar": {
                        "type": "integer",
                        "description": "Max antal resultat (standard 20)",
                        "default": 20,
                    },
                },
                "required": ["sokterm"],
            },
        ),
        types.Tool(
            name="dk_sok_i_dokument",
            description=(
                "Semantisk sökning inom ett enskilt cachat dokument via pgvector (cosinus-likhet). "
                "Returnerar topp-N chunk-träffar sorterade efter relevans, med chunk_nr och text. "
                "Använd när du behöver hitta specifika passager i ett dokument du redan identifierat "
                "(t.ex. via dk_sok eller dk_sok_lovgivning). "
                "dok_id är det interna databas-id:t som returneras av sökverktygen. "
                "Kräver PostgreSQL med pgvector — SQLite-läge stöds inte. "
                "Modell: intfloat/multilingual-e5-base (768 dim)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dok_id": {
                        "type": "integer",
                        "description": "Internt databas-id för dokumentet (matchar dok_id-fältet i dk_sok-resultat)",
                    },
                    "fraga": {
                        "type": "string",
                        "description": "Sökfråga på danska (eller svenska/engelska) — formuleras som en mening eller fras för bäst resultat",
                    },
                    "max_treff": {
                        "type": "integer",
                        "description": "Max antal chunk-träffar att returnera (standard 5)",
                        "default": 5,
                    },
                },
                "required": ["dok_id", "fraga"],
            },
        ),
        types.Tool(
            name="dk_hamta_aktor",
            description=(
                "Hämtar metadata för en eller flera aktörer (ledamöter, ministrar, partier, "
                "ministerier, utskott, m.fl.) från Folketing ODA via Aktør-entiteten. "
                "Använd för att översätta aktørid:n från dk_hamta_afstemning till läsbara namn "
                "och partitillhörighet. "
                "Ange antingen aktorid (enskilt uppslag) eller aktorider (lista, batch-uppslag — "
                "rekommenderas vid uppslag av många aktörer från en votering, t.ex. 179 ledamöter). "
                "Returnerar typeid som anger aktörstyp (vanligast 1=Ministerium, 2=Folketinget, "
                "3=Udvalg, 4=Folketingsgruppe/parti, 5=Person; andra typer förekommer och returneras "
                "transparent — den kompletta listan finns i ODA-entiteten /Aktørtype), gruppenavnkort "
                "(parti) och biografi-fält. "
                "Anropas live mot ODA — ingen cache."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "aktorid": {
                        "type": "integer",
                        "description": "ODA aktørid för enskilt uppslag",
                    },
                    "aktorider": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Lista av ODA aktørid:n för batch-uppslag (t.ex. från dk_hamta_afstemning)",
                    },
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Verktygsimplementationer
# ---------------------------------------------------------------------------

@server.call_tool()
async def anropa_verktyg(namn: str, arguments: dict):
    try:
        if namn == "dk_sok":
            return await _dk_sok(arguments)
        elif namn == "dk_sok_folketing":
            return await _dk_sok_folketing(arguments)
        elif namn == "dk_sok_lovgivning":
            return await _dk_sok_lovgivning(arguments)
        elif namn == "dk_hamta_dokument":
            return await _dk_hamta_dokument(arguments)
        elif namn == "dk_lista_perioder":
            return await _dk_lista_perioder(arguments)
        elif namn == "dk_hamta_afstemning":
            return await _dk_hamta_afstemning(arguments)
        elif namn == "dk_sok_semantisk":
            return await _dk_sok_semantisk(arguments)
        elif namn == "dk_sok_i_dokument":
            return await _dk_sok_i_dokument(arguments)
        elif namn == "dk_hamta_aktor":
            return await _dk_hamta_aktor(arguments)
        else:
            return [types.TextContent(type="text", text=f"Okänt verktyg: {namn}")]
    except Exception as e:
        logger.error("Fel i %s: %s", namn, e, exc_info=True)
        return [types.TextContent(type="text", text=f"Fel: {e}")]


def _formatera_treff(dok: dict) -> dict:
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


async def _dk_sok(args: dict):
    sokterm     = args["sokterm"]
    typ         = args.get("typ")
    periode     = args.get("periode")
    max_traffar = int(args.get("max_traffar", 20))
    expandera   = args.get("expandera", True)

    expansion = None
    if expandera:
        expansion = _expandera_fraga(sokterm)
        effektiv_term = expansion
    else:
        effektiv_term = sokterm

    # Hämta alla termer (kommaseparerade → OR-logik)
    termer = [t.strip() for t in effektiv_term.split(",") if t.strip()]
    resultat = []
    sett_ids = set()
    for term in termer:
        for dok in db.sok_dokument_fts(term, limit=max_traffar):
            if dok["id"] not in sett_ids:
                if typ and dok.get("typ", "").lower() != typ.lower():
                    continue
                if periode and dok.get("periode") != periode:
                    continue
                sett_ids.add(dok["id"])
                resultat.append(_formatera_treff(dok))

    svar = {
        "sokterm": sokterm,
        "expansion": expansion,
        "antal_traffar": len(resultat),
        "traffar": resultat[:max_traffar],
    }
    return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]


async def _dk_sok_folketing(args: dict):
    sokterm       = args["sokterm"]
    typ           = args.get("typ")
    periode       = args.get("periode")
    paragrafnr    = args.get("paragrafnummer")
    max_traffar   = int(args.get("max_traffar", 20))

    termer = [t.strip() for t in sokterm.split(",") if t.strip()]
    resultat = []
    sett_ids = set()
    for term in termer:
        for dok in db.sok_dokument_fts(term, limit=max_traffar * 2, kalla="oda"):
            if dok["id"] not in sett_ids:
                if typ and dok.get("typ", "").lower() != typ.lower():
                    continue
                if periode and dok.get("periode") != periode:
                    continue
                if paragrafnr and dok.get("paragrafnummer") != paragrafnr:
                    continue
                sett_ids.add(dok["id"])
                resultat.append(_formatera_treff(dok))

    svar = {
        "kalla": "Folketing ODA",
        "sokterm": sokterm,
        "antal_traffar": len(resultat),
        "traffar": resultat[:max_traffar],
    }
    return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]


async def _dk_sok_lovgivning(args: dict):
    sokterm              = args["sokterm"]
    typ                  = args.get("typ")
    max_traffar          = int(args.get("max_traffar", 20))
    inkludera_historiska = bool(args.get("inkludera_historiska", False))

    termer = [t.strip() for t in sokterm.split(",") if t.strip()]
    resultat = []
    sett_ids = set()
    for term in termer:
        for dok in db.sok_dokument_fts(
            term,
            limit=max_traffar * 2,
            kalla="retsinformation",
            inkludera_ersatta=inkludera_historiska,
        ):
            if dok["id"] not in sett_ids:
                if typ and dok.get("typ", "").lower() != typ.lower():
                    continue
                sett_ids.add(dok["id"])

                # Formatera träff — dölj giltig_till, exponera ikraftträdandedatum.
                # `dok_id` matchar parameternamnet i dk_hamta_dokument(dok_id=...);
                # `retsinformation_id` exponeras när källan har en ELI-id så att
                # kedjning sök → hämta kan göras via extern identifierare också.
                ar_historisk = dok.get("status") == "Historic"
                treff = {
                    "dok_id":           dok["id"],
                    "beteckning":       dok.get("beteckning"),
                    "typ":              dok.get("typ"),
                    "titel":            dok.get("titel"),
                    "titelkort":        dok.get("titelkort"),
                    "ikrafttraedelsesdato": dok.get("datum"),
                    "url":              dok.get("retsinformationsurl") or dok.get("url"),
                    "lovnummer":        dok.get("lovnummer"),
                    "resume":           dok.get("resume"),
                }
                if dok.get("extern_id"):
                    treff["retsinformation_id"] = dok["extern_id"]
                if ar_historisk:
                    treff["historisk"] = True
                    treff["advarsel"] = "Historisk lag — inte längre gällande rätt"
                resultat.append(treff)

    svar = {
        "kalla": "Retsinformation",
        "sokterm": sokterm,
        "inkludera_historiska": inkludera_historiska,
        "antal_traffar": len(resultat[:max_traffar]),
        "traffar": resultat[:max_traffar],
    }
    return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]


async def _dk_hamta_dokument(args: dict):
    dok_id = args.get("dok_id")
    sagid  = args.get("sagid")

    if dok_id is not None:
        dok = db.hamta_dokument_med_id(int(dok_id))
        if not dok:
            return [types.TextContent(type="text", text=f"Dokument {dok_id} hittades inte i databasen.")]
    elif sagid:
        # sagid pekar alltid mot en sag i ODA — gå direkt till ODA och returnera
        # sagen med dokumentlistan. Tidigare slogs detta först i lokal
        # dokument-tabell på `extern_id`, men där lagras både dokumentid och
        # sagid med samma kolumnnamn, vilket gav kollisioner där ett sagid
        # råkade matcha extern_id för ett orelaterat dokument. Sagor hämtas
        # alltid live från ODA — dokumentcachen påverkar inte den vägen.
        return await _hamta_sag_fran_oda(int(sagid))
    else:
        return [types.TextContent(type="text", text="Ange dok_id eller sagid.")]

    # Om fulltext saknas och det finns en URL — hämta PDF
    if not dok.get("fulltext_md") and dok.get("url"):
        logger.info("Hämtar PDF för dok %s: %s", dok.get("id"), dok.get("url"))
        pdf_bytes = _hamta_pdf_bytes(dok["url"])
        if pdf_bytes:
            text = _extrahera_pdf_text(pdf_bytes)
            if text:
                dok["fulltext_md"] = text
                # Uppdatera databasen
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

    # Bygg svar — dölj giltig_till, exponera ikraftträdandedatum
    svar = {
        "id":                   dok.get("id"),
        "kalla":                dok.get("kalla"),
        "beteckning":           dok.get("beteckning"),
        "typ":                  dok.get("typ"),
        "titel":                dok.get("titel"),
        "titelkort":            dok.get("titelkort"),
        "ikrafttraedelsesdato": dok.get("datum"),
        "url":                  dok.get("retsinformationsurl") or dok.get("url"),
        "lovnummer":            dok.get("lovnummer"),
        "resume":               dok.get("resume"),
        "fulltext_md":          dok.get("fulltext_md"),
        "status":               dok.get("status"),
    }

    if dok.get("status") == "Historic":
        svar["historisk"] = True
        svar["advarsel"] = "Historisk lag — inte längre gällande rätt"

    # Slå upp ändringslagar från relations-tabellen
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

    return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]


async def _hamta_sag_fran_oda(sagid: int):
    """Hämtar ett ärende direkt från ODA (inte via cache)."""
    try:
        sag_data = _oda_get(f"Sag({sagid})")
        sag = sag_data.get("value", sag_data)

        # Hämta kopplade dokument via SagDokument-relationen.
        # Höjt från tidigare tak om 3 dokument till 50 så hela ärendetråden
        # (lovforslag, betænkninger, ændringsforslag, slutligt antagen lov)
        # kommer med — nödvändigt för komparativ utredning där hela processen
        # är intressant.
        sd_data = _oda_get("SagDokument", {"$filter": f"sagid eq {sagid}", "$top": "50"})
        dokument_lista = []
        for sd in sd_data.get("value", []):
            dok_id_oda = sd.get("dokumentid")
            if not dok_id_oda:
                continue
            try:
                dok_data = _oda_get(f"Dokument({dok_id_oda})")
                dok = dok_data.get("value", dok_data)
                # Hämta fil-URL
                fil_data = _oda_get("Fil", {"$filter": f"dokumentid eq {dok_id_oda}", "$top": "1"})
                filer = fil_data.get("value", [])
                fil_url = filer[0].get("filurl") if filer else None
                dokument_lista.append({
                    "dokumentid": dok_id_oda,
                    "titel":      dok.get("titel"),
                    # typeid låter användaren särskilja lovforslag, betænkning,
                    # ændringsforslag, lovvedtagelse osv. — nödvändigt för att
                    # tråda processen rätt.
                    "typeid":     dok.get("typeid"),
                    "dato":       dok.get("dato"),
                    "fil_url":    fil_url,
                })
            except Exception as e:
                logger.warning("Kunde inte hämta dokument %s: %s", dok_id_oda, e)

        svar = {
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
        return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Fel vid hämtning av sag {sagid}: {e}")]


async def _dk_lista_perioder(args: dict):
    try:
        data = _oda_get("Periode", {"$orderby": "id desc", "$top": "20"})
        perioder = [
            {
                "id": p.get("id"),
                "kod": p.get("kode"),
                "titel": p.get("titel"),
                "startdatum": p.get("startdato"),
                "slutdatum": p.get("slutdato"),
            }
            for p in data.get("value", [])
        ]
        return [types.TextContent(type="text", text=json.dumps(perioder, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Fel vid hämtning av perioder: {e}")]


async def _dk_hamta_afstemning(args: dict):
    sagid                = int(args["sagid"])
    inkludera_per_ledamot = args.get("inkludera_per_ledamot", True)

    try:
        # Steg 1: hämta sagstrin
        st_data = _oda_get("Sagstrin", {"$filter": f"sagid eq {sagid}", "$format": "json"})
        sagstrin_lista = st_data.get("value", [])

        if not sagstrin_lista:
            return [types.TextContent(type="text", text=f"Inga sagstrin hittades för sagid {sagid}.")]

        afstemningar = []
        for strin in sagstrin_lista:
            strinid = strin.get("id")
            strintyp = strin.get("typeid")

            # Steg 2: hämta afstemning för detta sagstrin
            try:
                af_data = _oda_get("Afstemning", {"$filter": f"sagstrinid eq {strinid}"})
                for af in af_data.get("value", []):
                    afstemningid = af.get("id")
                    afstemning_post = {
                        "afstemningid": afstemningid,
                        "sagstrinid": strinid,
                        "sagstrin_typeid": strintyp,
                        "konklusion": af.get("konklusion"),
                        "for": af.get("for"),
                        "imod": af.get("imod"),
                        "hverken": af.get("hverken"),
                        "fravaerende": af.get("fravaerende"),
                        "vedtaget": af.get("vedtaget"),
                    }

                    # Steg 3: per-ledamot om begärt
                    if inkludera_per_ledamot and afstemningid:
                        try:
                            stemme_data = _oda_get(
                                "Stemme",
                                {"$filter": f"afstemningid eq {afstemningid}", "$top": "500"}
                            )
                            stemmer = [
                                {
                                    "aktørid": s.get("aktørid"),
                                    "typeid": s.get("typeid"),
                                    # 1=For, 2=Imod, 3=Fravær, 4=Hverken
                                }
                                for s in stemme_data.get("value", [])
                            ]
                            afstemning_post["stemmer"] = stemmer
                        except Exception as e:
                            logger.warning("Stemme-hämtning misslyckades (afstemningid=%s): %s", afstemningid, e)
                            afstemning_post["stemmer"] = []

                    afstemningar.append(afstemning_post)
            except Exception as e:
                logger.warning("Afstemning-hämtning misslyckades (sagstrinid=%s): %s", strinid, e)

        svar = {
            "sagid": sagid,
            "antal_afstemninger": len(afstemningar),
            "afstemninger": afstemningar,
        }
        return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]

    except Exception as e:
        return [types.TextContent(type="text", text=f"Fel vid hämtning av afstemning för sagid {sagid}: {e}")]


async def _dk_sok_semantisk(args: dict):
    sokterm     = args["sokterm"]
    max_traffar = int(args.get("max_traffar", 20))

    if not db._ar_postgres():
        return [types.TextContent(
            type="text",
            text="Semantisk sökning kräver PostgreSQL med pgvector — SQLite-läge stöds inte."
        )]

    try:
        fn = _hamta_semantisk_sok()
        resultat = fn(sokterm, limit=max_traffar)
    except Exception as e:
        logger.error("Semantisk sökning misslyckades: %s", e, exc_info=True)
        return [types.TextContent(type="text", text=f"Semantisk sökning misslyckades: {e}")]

    if not resultat:
        return [types.TextContent(
            type="text",
            text="Inga semantiska träffar. Kontrollera att 04_chunka_och_embedda.py körts och att embeddings finns i databasen."
        )]

    svar = {
        "sokterm": sokterm,
        "metod": "pgvector cosinus-likhet (intfloat/multilingual-e5-base, 768 dim)",
        "antal_traffar": len(resultat),
        "traffar": resultat,
    }
    return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]


async def _dk_sok_i_dokument(args: dict):
    dok_id    = int(args["dok_id"])
    fraga     = args["fraga"]
    max_treff = int(args.get("max_treff", 5))

    if not db._ar_postgres():
        return [types.TextContent(
            type="text",
            text="Semantisk sökning kräver PostgreSQL med pgvector — SQLite-läge stöds inte."
        )]

    try:
        fn = _hamta_semantisk_sok_i_dokument()
        resultat = fn(dok_id, fraga, limit=max_treff)
    except Exception as e:
        logger.error("dk_sok_i_dokument misslyckades: %s", e, exc_info=True)
        return [types.TextContent(type="text", text=f"Inom-dokument-sökning misslyckades: {e}")]

    # semantisk_sok_i_dokument returnerar metadata + ev. fel-fält som dict
    return [types.TextContent(type="text", text=json.dumps(resultat, ensure_ascii=False, indent=2))]


async def _dk_hamta_aktor(args: dict):
    aktorid   = args.get("aktorid")
    aktorider = args.get("aktorider")

    if aktorid is None and not aktorider:
        return [types.TextContent(
            type="text",
            text="Ange antingen aktorid (enskilt uppslag) eller aktorider (lista för batch-uppslag)."
        )]
    if aktorid is not None and aktorider:
        return [types.TextContent(
            type="text",
            text="Ange antingen aktorid eller aktorider, inte båda."
        )]

    if aktorid is not None:
        ids = [int(aktorid)]
    else:
        ids = [int(i) for i in aktorider]
        if not ids:
            return [types.TextContent(type="text", text="aktorider är tom — inga id:n att slå upp.")]

    aktorer = []
    fel = []

    # ODA $filter har URL-längdgränser (~2000 tecken). Batcha i grupper om 50
    # — "id eq 99999 or " är ~15 tecken, så 50 ger marginal under gränsen.
    BATCH = 50
    for start in range(0, len(ids), BATCH):
        batch = ids[start:start + BATCH]
        filter_uttryck = " or ".join(f"id eq {aid}" for aid in batch)
        try:
            data = _oda_get("Aktør", {"$filter": filter_uttryck, "$top": str(BATCH)})
        except Exception as e:
            logger.warning("Aktör-batch %s misslyckades: %s", batch, e)
            fel.append({"batch": batch, "fel": str(e)})
            continue

        for rad in data.get("value", []):
            aktorer.append({
                "aktørid":          rad.get("id"),
                "typeid":           rad.get("typeid"),
                # 1=Ministerium, 2=Folketinget, 3=Udvalg,
                # 4=Folketingsgruppe (parti), 5=Person
                "navn":             rad.get("navn"),
                "fornavn":          rad.get("fornavn"),
                "efternavn":        rad.get("efternavn"),
                "gruppenavnkort":   rad.get("gruppenavnkort"),
                "biografi":         rad.get("biografi"),
                "startdato":        rad.get("startdato"),
                "slutdato":         rad.get("slutdato"),
                "opdateringsdato":  rad.get("opdateringsdato"),
            })

    # Enskilt uppslag — returnera objektet direkt så svaret blir lätt att läsa
    if aktorid is not None:
        if not aktorer:
            return [types.TextContent(
                type="text",
                text=f"Ingen aktör hittades med aktørid={aktorid}."
            )]
        return [types.TextContent(
            type="text",
            text=json.dumps(aktorer[0], ensure_ascii=False, indent=2)
        )]

    # Batch-uppslag — returnera lista plus räknare så användaren ser om något saknas
    svar = {
        "begart_antal":     len(ids),
        "returnerat_antal": len(aktorer),
        "aktorer":          aktorer,
    }
    if fel:
        svar["fel"] = fel
    return [types.TextContent(type="text", text=json.dumps(svar, ensure_ascii=False, indent=2))]


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

async def _starta_server():
    if MCP_TRANSPORT == "http":
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import Response
        from mcp.server.sse import SseServerTransport
        import uvicorn

        class ApiNyckelMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, anropa_nasta):
                if MCP_API_KEY:
                    auth = request.headers.get("Authorization", "")
                    if auth != f"Bearer {MCP_API_KEY}":
                        return Response("Ej auktoriserad", status_code=401)
                return await anropa_nasta(request)

        sse = SseServerTransport("/messages/")

        async def hantera_sse(request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await server.run(streams[0], streams[1], server.create_initialization_options())

        app = Starlette(
            middleware=[ApiNyckelMiddleware],
            routes=[Route("/sse", endpoint=hantera_sse)],
        )
        logger.info("Startar HTTP-server på %s:%s", MCP_HOST, MCP_PORT)
        await uvicorn.Server(uvicorn.Config(app, host=MCP_HOST, port=MCP_PORT)).serve()
    else:
        logger.info("Startar stdio-server (Danmark MCP)")
        async with stdio_server() as (las, skriv):
            await server.run(las, skriv, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    # Databasinitiering: fel loggas men kraschar inte servern. Detta gör att
    # MCP-servern startar även om PostgreSQL-containern råkar vara nere vid
    # Claude Desktops uppstart. Verktygsanrop kommer att fela tills DB är uppe,
    # men servern överlever och behöver inte startas om manuellt.
    try:
        db.initialisera_schema()
        logger.info("Databasschema initialiserat (schema: danmark)")
    except Exception as e:
        logger.warning("Databasinitiering misslyckades: %s — fortsätter utan DB", e)
    asyncio.run(_starta_server())
