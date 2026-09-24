#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
08_konvertera_vektorer.py — Byter danmark.embeddings från vector(768) till halfvec(768)

halfvec lagrar varje komponent som 16-bitars flyttal: 1 540 byte per vektor i
stället för 3 076. En vector(768) hamnar i TOAST, medan en halfvec ryms i
själva tabellen, så tabellen krymper till ungefär en tredjedel.
Träffsäkerheten för cosinussökning påverkas inte mätbart. Därefter byggs ett
HNSW-index, så att en semantisk sökning inte längre jämför frågan med
samtliga vektorer.

Servern fungerar före, under (med väntan) och efter konverteringen: den läser
kolumntypen vid varje sökning. Nya och små databaser (högst 50 000 vektorer)
konverteras automatiskt vid uppstart; det här skriptet är till för befintliga
databaser, där omskrivningen tar tid och kräver diskutrymme.

Så går det till:
  1. Ett befintligt vektorindex tas bort (dess operatorklass gäller vector).
  2. ALTER TABLE … TYPE halfvec(768) skriver om hela danmark.embeddings.
     Tabellen är låst under tiden; semantiska sökningar väntar.
  3. HNSW-indexet byggs (m=16, ef_construction=64).

Kör:
  python3 08_konvertera_vektorer.py --torrkorning      # uppskattning, ändrar inget
  python3 08_konvertera_vektorer.py --minne 2GB        # konvertera och bygg index
  python3 08_konvertera_vektorer.py --bara-index       # bygg bara om indexet
"""

import argparse
import logging
import math
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_SCRIPT_DIR))

from dotenv import load_dotenv
load_dotenv(_SCRIPT_DIR / ".env")

import db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("konvertera_vektorer")

# Uppmätt 2026-09-24 på 60 000 embeddings kopierade ur driften (pgvector
# 0.8.2, maintenance_work_mem 2 GB, två parallella arbetare). Används bara
# för uppskattningarna.
HALFVEC_BYTE_PER_RAD = 1_650         # tabell efter konvertering, per vektor (1 641)
HNSW_BYTE_PER_RAD = 1_910            # HNSW-index per vektor (1 909)
OMSKRIVNING_SEK_PER_1000 = 0.10      # omskrivningens tid per 1 000 vektorer (0,03; tre gångers marginal)
HNSW_SEK_60K = 48.0                  # indexbygge för 60 000 vektorer


def _gb(byte: float) -> str:
    return f"{byte / 1024**3:.2f} GB"


def _tid(sek: float) -> str:
    if sek < 120:
        return f"{sek:.0f} s"
    return f"{sek / 60:.0f} min" if sek < 5400 else f"{sek / 3600:.1f} h"


def _hnsw_sek(n: int) -> float:
    """HNSW-bygget växer ungefär som n·log n."""
    if n <= 0:
        return 0.0
    return HNSW_SEK_60K * (n / 60_000) * (math.log(max(n, 2)) / math.log(60_000))


def _index_storlek(cur) -> int:
    cur.execute("SELECT to_regclass(%s)", (f"danmark.{db.VEKTORINDEX_NAMN}",))
    if cur.fetchone()[0] is None:
        return 0
    cur.execute("SELECT pg_relation_size(%s::regclass)", (f"danmark.{db.VEKTORINDEX_NAMN}",))
    return cur.fetchone()[0]


def lagesbild() -> dict:
    """Läser storlekar ur katalogen och statistiken; inga tunga frågor."""
    with db._cursor() as cur:
        cur.execute("""
            SELECT c.reltuples::bigint, pg_relation_size(c.oid),
                   coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0),
                   pg_total_relation_size(c.oid)
            FROM pg_class c WHERE c.oid = 'danmark.embeddings'::regclass""")
        rader, heap, toast, totalt = cur.fetchone()
        if rader < 0:  # aldrig analyserad
            cur.execute("SELECT count(*) FROM danmark.embeddings")
            rader = cur.fetchone()[0]
        return {
            "rader": rader, "heap": heap, "toast": toast, "totalt": totalt,
            "typ": db.vektortyp(cur), "index": _index_storlek(cur),
        }


def torrkorning() -> None:
    info = lagesbild()
    log.info("danmark.embeddings: %d vektorer som %s; totalt %s (tabell %s, TOAST %s, "
             "vektorindex %s)", info["rader"], info["typ"], _gb(info["totalt"]),
             _gb(info["heap"]), _gb(info["toast"]), _gb(info["index"]))

    n = info["rader"]
    tabell_efter = n * HALFVEC_BYTE_PER_RAD
    hnsw_efter = n * HNSW_BYTE_PER_RAD
    ovriga_index = info["totalt"] - info["heap"] - info["toast"] - info["index"]
    totalt_efter = tabell_efter + hnsw_efter + ovriga_index

    log.info("Uppskattning (osäkerhet ungefär ±50 procent):")
    if info["typ"] == "vector":
        log.info("  omskrivning av tabellen: %s, tabellen låst under tiden",
                 _tid(n / 1000 * OMSKRIVNING_SEK_PER_1000))
        log.info("  disk under omskrivningen: ytterligare %s (ny kopia av tabellen); "
                 "den gamla frigörs när omskrivningen är klar", _gb(tabell_efter + ovriga_index))
    else:
        log.info("  kolumnen är redan halfvec; bara indexet byggs")
    log.info("  HNSW-bygge: %s med tillräckligt minne; maintenance_work_mem minst %s (--minne)",
             _tid(_hnsw_sek(n)), _gb(hnsw_efter * 1.2))
    log.info("  slutstorlek: %s (tabell %s, HNSW %s, övriga index %s); frigör %s",
             _gb(totalt_efter), _gb(tabell_efter), _gb(hnsw_efter), _gb(ovriga_index),
             _gb(info["totalt"] - totalt_efter))


def konvertera(minne: str, parallella: int, bara_index: bool) -> None:
    if not bara_index:
        with db._cursor() as cur:
            typ = db.vektortyp(cur)
        if typ == "vector":
            start = time.time()
            log.info("Skriver om danmark.embeddings; tabellen är låst under tiden...")
            with db._cursor() as cur:
                db.konvertera_till_halfvec(cur)
            log.info("Omskrivning klar på %s", _tid(time.time() - start))
        else:
            log.info("Kolumnen är redan halfvec; bygger bara index.")

    start = time.time()
    log.info("Bygger HNSW-index (maintenance_work_mem=%s)...", minne)
    db.bygg_vektorindex(minne=minne, parallella=parallella)
    log.info("Index klart på %s", _tid(time.time() - start))

    with db._cursor() as cur:
        cur.execute("ANALYZE danmark.embeddings")
        cur.execute("SELECT pg_total_relation_size('danmark.embeddings')")
        log.info("danmark.embeddings efter konverteringen: %s", _gb(cur.fetchone()[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Byter embeddings till halfvec(768) med HNSW-index")
    parser.add_argument("--torrkorning", action="store_true",
                        help="Visa uppskattad tid, diskbehov och slutstorlek; ändrar inget")
    parser.add_argument("--minne", default="2GB",
                        help="maintenance_work_mem för HNSW-bygget (standard 2GB)")
    parser.add_argument("--parallella", type=int, default=2,
                        help="Parallella arbetare för indexbygget (standard 2)")
    parser.add_argument("--bara-index", action="store_true",
                        help="Bygg bara om HNSW-indexet, ingen typkonvertering")
    args = parser.parse_args()

    if not db._ar_postgres():
        log.error("Skriptet kräver PostgreSQL med pgvector.")
        sys.exit(1)
    if args.torrkorning:
        torrkorning()
        return
    konvertera(args.minne, args.parallella, args.bara_index)
    log.info("Klar.")


if __name__ == "__main__":
    main()
