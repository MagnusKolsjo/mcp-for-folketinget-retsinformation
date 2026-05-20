# MCP-server för Folketing och Retsinformation

MCP-server för dansk parlamentarisk data och rättslig information — sök i Folketingets ärenden, voteringar och konsoliderad lagstiftning via Retsinformation.

Sju verktyg med prefixet `dk_`:

| Verktyg | Beskrivning |
|---|---|
| `dk_sok` | Aggregerad sökning över alla danska källor |
| `dk_sok_folketing` | Søk i Folketingets ärenden (lovforslag, betænkninger, afstemninger) |
| `dk_sok_lovgivning` | Søk i dansk lagstiftning via Retsinformation |
| `dk_hamta_dokument` | Hämta fulltext och metadata för ett ärende eller dokument |
| `dk_lista_perioder` | Lista tillgängliga valperioder |
| `dk_hamta_afstemning` | Voteringsresultat för ett ärende, inklusive per-ledamot-röstning |
| `dk_sok_semantisk` | Semantisk sökning med pgvector (intfloat/multilingual-e5-base) |

## Datakällor

- **Folketing ODA** (oda.ft.dk) — ärenden, dokument, voteringar, ledamöter. Ca 98 000 ärenden från ca 1985 och framåt.
- **Retsinformation** (api.retsinformation.dk) — konsoliderad dansk lagstiftning. Ca 62 000 gällande lagar via harvest-API och historisk sitemap.

## Krav

- Python 3.10+
- PostgreSQL med pgvector (för semantisk sökning och relationsspårning), eller SQLite (enklare installation utan vektorsökning)
- Se `requirements.txt` för Python-beroenden

## Installation

```bash
cp config.example.env .env
# Redigera .env — ange DATABASE_URL och övriga inställningar
pip install -r requirements.txt
```

Initiera och fyll databasen:

```bash
python3 02_synka_oda.py --fas 1   # Metadata för alla ärenden (~98 000)
python3 02_synka_oda.py --fas 2   # PDF-fulltext för lovforslag och beslutningsforslag
python3 05_synka_retsinformation_sitemap.py --bara-lta --trad 2   # Historisk harvest (~9 h)
python3 04_chunka_och_embedda.py  # Chunkning och embedding
```

Daglig synk installeras med:

```bash
python3 02_synka_oda.py --installera-schema
```

## Konfiguration i Claude Desktop

Lägg till i `claude_desktop_config.json`:

```json
"danmark": {
  "command": "/stig/till/.venv/bin/python3",
  "args": ["/stig/till/mcp_server.py"],
  "cwd": "/stig/till/stream-14-danmark"
}
```

## Licens

GNU Affero General Public License v3.0 — se LICENSE.
