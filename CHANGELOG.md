# Ändringslogg — mcp-for-folketinget-retsinformation

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet tillämpar [Semantic Versioning](https://semver.org/).

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
