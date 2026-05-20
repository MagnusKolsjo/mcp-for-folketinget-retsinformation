#!/bin/bash
# synk_daglig.sh — Daglig synk av dansk riksdags- och rättsdata.
#
# Körordning:
#   1. Retsinformation harvest (delta, 5–30 min beroende på antal missade dagar)
#      Skriptfil: 03_synka_retsinformation.py  (prefix 03, inte 01 — 01 finns ej)
#   2. Folketing ODA (fas 1 + fas 2, inkrementell via checkpoint)
#      Skriptfil: 02_synka_oda.py
#   3. Chunkning + embedding för nya/uppdaterade dokument
#      Skriptfil: 04_chunka_och_embedda.py
#
# Prefixen 01-04 är filnummer från datakällsordningen i repot, inte körordning.
# Körordningen 03 → 02 → 04 är avsiktlig: Retsinformation skörd sker först
# eftersom harvest-API:et returnerar kompletta delta-paket och bör ha företräde
# framför ODA-metadata vid konflikter.
#
# Anropas av launchd dagligen kl. 04:30.
# Kör manuellt: bash ~/MCP-Servers/danmark/synk_daglig.sh

set -euo pipefail

MAPP="$HOME/MCP-Servers/danmark"
PYTHON="$HOME/MCP-Servers/.venv/bin/python3"
LOGG="$MAPP/logs/synk_daglig.log"

mkdir -p "$MAPP/logs"

echo "=============================" >> "$LOGG"
echo "Daglig synk startad: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOGG"
echo "=============================" >> "$LOGG"

# Ladda .env om den finns
if [ -f "$MAPP/.env" ]; then
    set -a
    source "$MAPP/.env"
    set +a
fi

cd "$MAPP"

# ---------------------------------------------------------------------------
# Steg 1: Retsinformation (harvest-API — tar hänsyn till missade dagar auto.)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 1: Retsinformation harvest" >> "$LOGG"
"$PYTHON" "$MAPP/03_synka_retsinformation.py" >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 1 klar" >> "$LOGG"

# ---------------------------------------------------------------------------
# Steg 2: Folketing ODA (inkrementell — hoppar vid checkpoint)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 2: ODA fas 1 (metadata)" >> "$LOGG"
"$PYTHON" "$MAPP/02_synka_oda.py" --fas 1 >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 2a klar" >> "$LOGG"

echo "[$(date '+%H:%M:%S')] Steg 2b: ODA fas 2 (fulltext PDF)" >> "$LOGG"
"$PYTHON" "$MAPP/02_synka_oda.py" --fas 2 >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 2b klar" >> "$LOGG"

# ---------------------------------------------------------------------------
# Steg 3: Chunkning + embedding för nya dokument
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 3: Chunkning och embedding" >> "$LOGG"
"$PYTHON" "$MAPP/04_chunka_och_embedda.py" >> "$LOGG" 2>&1
echo "[$(date '+%H:%M:%S')] Steg 3 klar" >> "$LOGG"

echo "Daglig synk avslutad: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOGG"
echo "" >> "$LOGG"
