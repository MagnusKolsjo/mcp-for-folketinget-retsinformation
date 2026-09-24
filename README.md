# MCP-server för Folketing och Retsinformation

MCP-server för dansk parlamentarisk data och rättslig information — sök i Folketingets ärenden, voteringar och konsoliderad lagstiftning via Retsinformation.

Nio verktyg med prefixet `dk_`:

| Verktyg | Beskrivning |
|---|---|
| `dk_sok` | Aggregerad sökning över alla danska källor |
| `dk_sok_folketing` | Søk i Folketingets ärenden (lovforslag, betænkninger, afstemninger) |
| `dk_sok_lovgivning` | Søk i dansk lagstiftning via Retsinformation |
| `dk_hamta_dokument` | Hämta fulltext och metadata för ett ärende eller dokument |
| `dk_lista_perioder` | Lista tillgängliga valperioder |
| `dk_hamta_afstemning` | Voteringsresultat för ett ärende, inklusive per-ledamot-röstning |
| `dk_sok_semantisk` | Semantisk sökning med pgvector (intfloat/multilingual-e5-base) |
| `dk_sok_i_dokument` | Semantisk sökning inom ett enskilt cachat dokument |
| `dk_hamta_aktor` | Ledamöter, partier, ministerier och utskott ur ODA:s Aktør-entitet |

Alla verktyg returnerar strukturerade svar med utdataschema. Förväntade fel —
okänt id, ODA som inte svarar, databas som är nere — ges som verktygsfel med
ett meddelande som säger vad som gick fel.

## Datakällor

- **Folketing ODA** (oda.ft.dk) — ärenden, dokument, voteringar, ledamöter. Ca 98 000 ärenden från ca 1985 och framåt.
- **Retsinformation** (api.retsinformation.dk) — konsoliderad dansk lagstiftning. Ca 62 000 gällande lagar via harvest-API och historisk sitemap.

## Krav

- Python 3.10+
- MCP Python SDK 2.x (`mcp>=2.0,<3`)
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

Efter den första körningen är `02_synka_oda.py --fas 1` inkrementell: den
hämtar ärenden vars `opdateringsdato` i ODA ligger efter förra lyckade
körningen, alltså både nya och ändrade ärenden. Checkpointen ligger i
`sync_status` (`oda_senaste_opdateringsdato`) och flyttas bara fram när hela
körningen lyckats. `--full` hämtar alla ärenden på nytt, och
`--sedan YYYY-MM-DD` hämtar ärenden ändrade sedan ett visst datum.

Daglig synk installeras med:

```bash
python3 02_synka_oda.py --installera-schema
```

## Semantisk sökning och vektorindex

Embeddings lagras som `halfvec(768)` (16-bitars flyttal, 1 540 byte per vektor)
med ett HNSW-index (`m=16`, `ef_construction=64`). `dk_sok_semantisk` hämtar de
närmaste chunkarna via indexet och grupperar dem per dokument;
`dk_sok_i_dokument` sorterar exakt inom dokumentet. `hnsw.ef_search` sätts per
fråga, standard 400 (`DK_HNSW_EF_SEARCH` i `.env`). Mätt på 60 000 embeddings:
recall@10 0,94 mot exakt sökning, på några hundradels sekunder.

Nya databaser och databaser med högst 50 000 vektorer konverteras automatiskt
vid uppstart. En större databas med `vector(768)` fungerar som förut (exakt
sökning över alla vektorer, långsamt) tills den konverterats:

```bash
python3 08_konvertera_vektorer.py --torrkorning   # tid, disk, minne och slutstorlek
python3 08_konvertera_vektorer.py --minne 2GB     # omskrivning och HNSW-bygge
```

Tabellen `danmark.embeddings` är låst under omskrivningen; semantiska
sökningar väntar tills den är klar. HNSW-bygget går mycket snabbare när grafen
ryms i `maintenance_work_mem`: räkna med knappt 2 kB per vektor.

## Uppgradering av en befintlig installation (från 1.2.0)

Ordningen spelar roll; stegen 2–5 ändrar databasen och kan ta tid.

1. Installera den nya koden och `requirements.txt` (mcp 2.x) och starta servern
   en gång. Uppstarten gör bara snabba schemaändringar (kolumnerna
   `fulltext_kalla` och `chunk_hash`) och tar några sekunder. Lagrar databasen
   embeddings som `vector` loggas att `08_konvertera_vektorer.py` behövs; servern
   fungerar ändå.
2. Kör engångsuppdateringarna av befintliga rader:

   ```bash
   python3 db.py --migrera
   ```

   De markerar ODA-ärenden vars fulltext bara är resume och ger redan chunkade
   dokument sin `chunk_hash`, och tar ungefär en minut på en databas med
   150 000 dokument. De ligger utanför serverns uppstart eftersom
   MCP-klienter bara väntar en begränsad tid på att servern svarar. Steget körs också
   automatiskt i början av `02_synka_oda.py` och `04_chunka_och_embedda.py`,
   och varje del bara en gång. Innan det körts fungerar allt som före
   uppgraderingen: fas 2 hämtar inte PDF för ärenden med bara resume, och
   dokument med chunks antas vara aktuella.
3. Byt vektorlagringen: `python3 08_konvertera_vektorer.py --torrkorning`,
   därefter `python3 08_konvertera_vektorer.py --minne 2GB`.
4. Kör ODA-synken. Den första körningen saknar checkpoint för `opdateringsdato`
   och hämtar därför alla ärenden (cirka 100 000, ungefär 1 000 anrop mot ODA).
   Fas 2 hämtar sedan PDF för alla lovforslag och beslutningsforslag som bara
   har resume, vilket kan bli många anrop mot ft.dk första gången:

   ```bash
   python3 02_synka_oda.py --fas 1
   python3 02_synka_oda.py --fas 2
   ```
5. Chunka och embedda nya dokument och dokument vars text ändrats (till exempel
   när fas 2 hämtat PDF:en): `python3 04_chunka_och_embedda.py`.

Därefter sköter den dagliga synken resten; ODA-synken fortsätter från förra
lyckade körningen.

## Transport: stdio eller http

Transporten väljs med `MCP_TRANSPORT` i `.env`. Båda är likvärdiga val.

### stdio (lokal MCP-klient)

Lägg till i MCP-klientens konfigurationsfil:

```json
"danmark": {
  "command": "/stig/till/.venv/bin/python3",
  "args": ["/stig/till/mcp_server.py"],
  "cwd": "/stig/till/stream-14-danmark"
}
```

### http (Streamable HTTP)

För delad drift bakom en reverse proxy. Servern lyssnar på
`http://MCP_HOST:MCP_PORT/mcp` (standard `127.0.0.1:8714`).

```bash
MCP_TRANSPORT=http
MCP_API_KEY=<LÅNG_SLUMPAD_NYCKEL>
```

`MCP_API_KEY` är obligatorisk i http-läget: utan den avbryts uppstarten med
exitkod 2. Klienten skickar nyckeln som `Authorization: Bearer <nyckel>`;
saknad header ger 401 och fel nyckel 403. Embeddingmodellen laddas vid
uppstart i http-läget, så att första semantiska sökningen inte väntar på den.


## Svarsstorlek och trunkering

MCP-protokollet har en övre storleksgräns per svar. Det största danska dokumentet i cachen är **958 297 tecken** — strax under gränsen, alltså inom felmarginalen för nästa något större lagtext.
`dk_hamta_dokument` tar därför två parametrar:

| Parameter | Innebörd |
|---|---|
| `max_tecken` | Teckentak för texten. Standard 60 000 tecken, högst 400 000; `0` ger så mycket som ryms, alltså 400 000. |
| `fran_tecken` | Börja vid denna teckenposition — för att läsa vidare där ett kapat svar slutade. |

Ett kapat svar säger alltid ifrån med fälten `trunkerad`, `tecken_totalt`, `tecken_visade` och `fortsatt_fran_tecken`. Kapningen sker på ordgräns, aldrig mitt i
ett ord.

Svaret skickas både som JSON-text och som strukturerat innehåll, alltså två
gånger. Taket på 400 000 tecken per svar håller det under protokollets gräns;
längre texter läses i flera anrop.

**Vid ordagranna citat:** citera aldrig ur ett svar som är markerat som kapat.
Läs vidare med `fran_tecken` tills hela passagen är hämtad. Standardvärdet kan
sättas i `.env` med `DK_MAX_TECKEN`.

## Licens

GNU Affero General Public License v3.0 — se LICENSE.
