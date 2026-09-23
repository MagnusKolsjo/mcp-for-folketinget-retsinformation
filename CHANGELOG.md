# Ändringslogg — mcp-for-folketinget-retsinformation

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet tillämpar [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Ändrat

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

### Tillagt

- Titel och annotationer (`readOnlyHint`, `openWorldHint` m.fl.) på alla
  verktyg, cachningshintar för verktygslistan och serverinstruktioner som
  beskriver verktygskedjorna.
- I http-läget laddas embeddingmodellen vid uppstart.

### Rättat

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

### Borttaget

- SSE-transporten (`/sse`, `/messages/`).

---

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
