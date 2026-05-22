# Ändringslogg — mcp-for-folketinget-retsinformation

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet tillämpar [Semantic Versioning](https://semver.org/).

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
