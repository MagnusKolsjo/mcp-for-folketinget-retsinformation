# Ändringslogg — mcp-for-folketinget-retsinformation

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet tillämpar [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

## [2.0.0] — 2026-09-26

### Rättat

- OCR-språket för sidor utan textlager var engelska (pymupdf4llms
  standardvärde), eftersom ingen kod angav `ocr_language`. Danska tecken i
  skannade sidor blev därför fel. `DK_OCR_SPRAK` (standard `dan+eng`) styr
  nu språket explicit.
- **Serverns uppstart gör inga tunga dataskrivningar.** Engångsmarkeringarna
  av `fulltext_kalla` och `chunk_hash` gick i `initialisera_schema()` och tog
  över en minut på en stor databas; MCP-klienten slutade vänta efter 60 s, avbröt
  processen och samma sak upprepades vid varje start. De ligger nu i
  `db.migrera_data()`, som körs med `python3 db.py --migrera` och automatiskt i
  början av `02_synka_oda.py` och `04_chunka_och_embedda.py`. Uppstarten gör
  bara `ADD COLUMN IF NOT EXISTS` och den begränsade halfvec-konverteringen
  (0,4 s mätt med 146 000 dokument och 700 000 chunks). Innan migreringen
  körts antas dokument med chunks men utan `chunk_hash` vara aktuella, så att
  inget embeddas om i onödan.
- **`04_chunka_och_embedda.py` chunkar om dokument vars text ändrats.**
  Tidigare valdes bara dokument utan chunks, så när fas 2 i ODA-synken ersatte
  ett ärendes resume med PDF-texten behöll den semantiska sökningen vektorer
  byggda ur resume. Den nya kolumnen `chunk_hash` (md5 av texten vid
  chunkningen, migration för Postgres och SQLite) jämförs med nuvarande text;
  avvikande dokument chunkas om, och gamla chunks och embeddings ersätts i
  samma transaktion som hashen sparas. Vid första initieringen efter
  uppgraderingen antas befintliga chunks höra till nuvarande text (en gång per
  databas, `migrering_chunk_hash` i `sync_status`), så att inte hela korpusen
  embeddas om. Jämförelsen över alla dokument tar cirka 3 s i driften.
- **Fas 2 i ODA-synken hämtar PDF även för ärenden vars fulltext bara är
  resume.** Fas 1 sparade resume som fulltext, och fas 2 valde bara ärenden
  helt utan text, så lovforslag och beslutningsforslag med resume fick aldrig
  sin PDF. Den nya kolumnen `fulltext_kalla` (`resume`, `pdf`, `ingen_pdf`)
  säger varifrån texten kommer; fas 2 väljer ärenden utan text eller med
  `resume`. En ändrad resume slår igenom så länge den är ärendets enda text.
  Ett ärende där ingen PDF hittas behåller sin resume i stället för att den
  ersätts med platshållaren "(ingen PDF hittad)".
- **Schemaändring:** `fulltext_kalla` läggs till via migrationsblocket i
  `db.py` (Postgres och SQLite). Vid första initieringen efter uppgraderingen
  markeras befintliga ODA-rader vars fulltext är identisk med resume som
  `resume`, en gång per databas (noteras i `sync_status` som
  `migrering_fulltext_kalla`). Nästa fas 2 hämtar då PDF:er för dem, vilket
  kan bli många anrop mot ft.dk första gången.
- `03_synka_retsinformation.py` flyttar checkpointen
  (`retsinformation_senaste_synk`) bara när körningen gick igenom. Tidigare
  räknades ett misslyckat harvest-anrop som ett dygn utan ändringar, och
  dokument vars XML inte kunde hämtas gick förlorade. Vid fel avslutas
  skriptet med exitkod 1 och nästa körning tar om samma dygn. Utanför
  Retsinformations öppettid (03:00–23:45 dansk tid) görs inga anrop, och
  omförsök efter nätverksfel väntar tio sekunder. `synk_daglig.sh`
  fortsätter med övriga steg när steget misslyckas.
- En uppdatering av ett ODA-ärende skriver inte längre över en redan
  extraherad PDF-text med ärendets resume (`db.upsert_dokument` har fått
  `behall_fulltext`).
- Buffrad utdata på stdout och stderr hamnar inte längre i loggfilerna för
  pymupdf och embeddingmodellen.
- `dk_hamta_afstemning` och `dk_hamta_dokument` med `sagid` redovisar delar
  som inte gick att hämta från ODA (`ofullstandig: true` och `anmarkningar`)
  i stället för att tyst hoppa över dem. En votering vars röster per ledamot
  inte kunde hämtas saknar `stemmer` i stället för att visa en tom lista, och
  ett dokument vars fil-uppgift inte gick att hämta finns kvar i listan.
  Ärenden med minst 50 kopplade dokument får en anmärkning om att listan är
  kapad.
- `dk_hamta_dokument` visar när ändringslagarna inte gick att kontrollera
  (`andringar_kontrollerade: false` och en varning i `advarsel_andringar`).
  Tidigare svaldes felet, och en inaktuell lagtext såg gällande ut.
- När en ft.dk-PDF inte ger någon text säger `anmarkning` varför: botskydd,
  nätverksfel, saknat paket (curl-cffi, pymupdf4llm), trasig PDF eller en
  inskannad PDF utan text. Tidigare blev `fulltext_md` tyst null.
- `dk_hamta_dokument` skickar bara Folketingets PDF:er på ft.dk till
  PDF-hämtningen. Retsinformation-dokument utan lokal fulltext (drygt 6 000,
  främst äldre och historiska) gick tidigare samma väg: servern hämtade
  HTML-sidan bakom ELI-länken, utanför källans anropsgräns, och
  `fulltext_md` blev null utan förklaring. Nu görs inget anrop, och det nya
  fältet `anmarkning` säger varför texten saknas och länkar till
  retsinformation.dk.
- När embeddingmodellen inte kan laddas ger de semantiska verktygen ett
  verktygsfel som namnger modellen och orsaken, i stället för ett allmänt fel.
- Bara fel i `DATABASE_URL` och schemafilerna kallas konfigurationsfel
  (`db.Konfigurationsfel`, en underklass till `RuntimeError`); andra körtidsfel
  rapporteras inte längre som felaktig konfiguration.
- `QUERY_EXPANSION_ENABLED` respekteras. Tidigare försökte `dk_sok` nå
  LLM-endpointen vid varje anrop även när flaggan var avstängd, vilket kostade
  runt 1,5 sekunder när ingen endpoint svarade. Utan expansion är fältet
  `expansion` null.
- `dk_sok_semantisk` sorterar träffarna efter avstånd. Tidigare gav den
  de dokument som hade lägst id, oavsett hur väl de matchade frågan.
- Lat inläsning av embeddingmodellen och av chunkmodulen är trådsäker.
- Omdirigeringen av fd 1 och 2 under pymupdf- och embeddinganrop är
  serialiserad, så att samtidiga anrop inte lämnar stdout och stderr pekande
  på en loggfil. Det serialiserar också pymupdf, som inte tål flera trådar.
- Typfiltret i `dk_sok`, `dk_sok_folketing` och `dk_sok_lovgivning` kraschar
  inte längre på dokument utan typ.
- Ingen FutureWarning från sentence-transformers 6 vid modellinläsning.

### Tillagt

- Minnes- och tidsvakt kring PDF-extraktionen (`pdftext_skydd.py`):
  extraktionen körs i en egen process per sidblock, och ett block som
  passerar minnes- eller tidsgränsen läses om med ren textutvinning i
  stället för att fälla processen.
- OCR-kö (`ocr_ko/ko.jsonl` + `ocr_ko/filer/`) för dokument där minst en
  sida saknade textlager eller där ett block föll tillbaka på ren
  textutvinning, så att de kan köras genom en bättre OCR senare.
- `08_konvertera_vektorer.py` byter en befintlig databas till halfvec och
  bygger HNSW-indexet. `--torrkorning` visar uppskattad tid, diskbehov, minne
  och slutstorlek utan att ändra något; `--bara-index` bygger om indexet.
- **Embeddings som `halfvec(768)` med HNSW-index.** `danmark.embeddings` saknade
  vektorindex, så varje semantisk sökning läste alla vektorer ur TOAST
  (drygt 20 s i driften). halfvec tar 1 540 byte per vektor i stället för
  3 076 och ryms i själva tabellen. HNSW (m=16, ef_construction=64) ger
  sökningar på några hundradels sekunder. `hnsw.ef_search` sätts per fråga,
  standard 400 (`DK_HNSW_EF_SEARCH`), och höjs till minst antalet kandidater.
  Mätt på 60 000 embeddings ur driften: halfvec ger samma topp-10 som vector
  (recall@10 0,993); med HNSW är recall@10 0,94 för chunks och 0,92 för
  dokument, mot 0,82 med ef_search 100.
- Servern och embeddingskriptet läser kolumntypen och fungerar både före och
  efter konverteringen. Tabeller med högst 50 000 vektorer konverteras
  automatiskt vid uppstart; större med `08_konvertera_vektorer.py`.
- `dk_sok_semantisk` hämtar de närmaste chunkarna med en indexvänlig fråga och
  grupperar dem per dokument; `dk_sok_i_dokument` sorterar exakt inom
  dokumentet utan att gå via indexet.
- Retsinformation synkas fortsatt via harvest-API:et. ELI Atom-feeden som
  Civilstyrelsen annonserat har ingen dokumenterad eller hittbar adress
  (kontrollerat 2026-09-24); skälen står i `03_synka_retsinformation.py`.
- **Inkrementell ODA-synk på `opdateringsdato`.** `02_synka_oda.py --fas 1`
  hämtar ärenden som ändrats sedan förra lyckade körningen, inte bara ärenden
  med högre id än förut. Ändrade ärenden (ny status, resume, afgørelse)
  uppdateras därmed i databasen. Pagineringen sker på nyckel
  (`opdateringsdato`, `id`) i stället för `$skip`, så att ett ärende som
  uppdateras under körningen inte förskjuter sidorna. Checkpointen
  (`oda_senaste_opdateringsdato`) flyttas bara fram när körningen lyckats;
  vid fel avslutas skriptet med exitkod 1. `--full` hämtar alla ärenden,
  `--sedan YYYY-MM-DD` ärenden ändrade sedan ett datum. Den första körningen
  utan checkpoint är en full synk (cirka 100 000 ärenden, ungefär 1 000
  anrop). Den tidigare nyckeln `oda_senaste_sagid` används inte längre.
- `synk_daglig.sh` fortsätter med fulltext och embedding när ODA-steget
  misslyckas, och loggar att checkpointen inte flyttades.
- Titel och annotationer (`readOnlyHint`, `openWorldHint` m.fl.) på alla
  verktyg, cachningshintar för verktygslistan och serverinstruktioner som
  beskriver verktygskedjorna.
- I http-läget laddas embeddingmodellen vid uppstart.

### Borttaget

- Den odokumenterade `ocrmypdf`-reserven fanns aldrig i det här repot —
  ingen ändring krävdes av den anledningen.
- SSE-transporten (`/sse`, `/messages/`).

---

### Ändrat

- Texterna är produktneutrala: README, konfigurationsexempel, kommentarer och äldre CHANGELOG-poster nämner MCP-klienten i stället för en viss klient.
- User-Agent-strängen följer huvudversionen: `mcp-for-folketinget-retsinformation/2.0`.
- **Brytande: MCP Python SDK 2.x krävs** (`mcp>=2.0,<3`). Servern är
  omskriven från lågnivå-`Server` med handskrivna scheman till `MCPServer`
  med `@mcp.tool()`. Verktygsnamn, parametrar och beskrivningar är
  oförändrade.
- **Brytande: http-läget kör Streamable HTTP på `/mcp` och kräver
  `MCP_API_KEY`.** Utan nyckel avbryts uppstarten med exitkod 2 i stället för
  att servern startar oskyddad. Fel nyckel ger 403, saknad header 401.
- **Brytande: förväntade fel är verktygsfel.** Okänt `dok_id`, `sagid` eller
  `aktorid`, ODA som inte svarar, databasfel och semantisk sökning mot SQLite
  ger nu `isError` med ett svenskt meddelande i stället för ett vanligt
  textsvar. Klienter som tolkade feltexten som resultat behöver se över det.
- **Brytande: `max_tecken` i `dk_hamta_dokument` har ett tak på 400 000
  tecken**, och `0` betyder "så mycket som ryms" i stället för hela texten.
  Svaret skickas nu både som JSON-text och som strukturerat innehåll, och
  en hel lagtext på nära en miljon tecken skulle då spränga protokollets
  gräns. Längre texter läses i flera anrop med `fran_tecken`.
- Alla verktyg returnerar typade, strukturerade svar med utdataschema.
  Textsvaret är samma JSON som förut. `dk_lista_perioder` ger en lista, som
  skickas som ett textblock per period och strukturerat som `{"result": [...]}`;
  `dk_hamta_dokument` och `dk_hamta_aktor`, som har två svarsformer, har
  sitt strukturerade svar under `result`.
- Verktygen körs på arbetstrådar i stället för att blockera servern, så att
  flera anrop kan pågå samtidigt.
- `requirements.txt` har versionsgränser.

## [1.2.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `dk_hamta_dokument`**, med standardtaket
  `DK_MAX_TECKEN` (60 000 tecken, konfigurerbart i `.env`). Det största danska
  dokumentet i cachen är **958 297 tecken**, vilket gav ett svar på 970 017 tecken —
  strax under MCP-protokollets gräns på 1 048 576, alltså inom felmarginalen för att
  slå i taket vid nästa något större lagtext. Med standardtaket blir samma anrop
  62 756 tecken. Kapade svar bär `trunkerad`, `tecken_totalt`, `tecken_visade` och
  `fortsatt_fran_tecken`; kapningen sker på ordgräns.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar och fält; inga brytande
ändringar och inga schemaändringar. Cachen och databasen lagrar fortfarande hela
texten — trunkeringen gäller bara svaret till anroparen, så sökning och indexering
påverkas inte.

---

## [1.1.0] — 2026-05-22

Två nya MCP-verktyg.

- `dk_sok_i_dokument(dok_id, fraga, max_treff)` — semantisk pgvector-sökning scoped till ett enskilt cachat dokument. Returnerar topp-N chunk-träffar med `chunk_nr`, `text` och cosinus-avstånd. Implementerat som ny funktion `semantisk_sok_i_dokument()` i `04_chunka_och_embedda.py`.
- `dk_hamta_aktor(aktorid | aktorider)` — slår upp ledamot, parti, ministerium eller utskott via ODA Aktør-entiteten. Stöder både enskilt uppslag och batch (lista av aktørid:n, batchas internt mot ODA `$filter` i grupper om 50). Returnerar `typeid` (1=Ministerium, 2=Folketinget, 3=Udvalg, 4=Folketingsgruppe, 5=Person — fler typer förekommer transparent, se ODA-entiteten `/Aktørtype`).
- Lazy-import i `mcp_server.py` refaktorerad till modul-nivå (`_hamta_chunka_modul()`) så båda funktionerna kan exponeras från samma module-cache.

Totalt 9 MCP-verktyg.

---

## [1.0.0] — 2026-05-21

Första publicering.
