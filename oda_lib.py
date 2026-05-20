# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
oda_lib.py — Hjälpfunktioner för data från Folketing ODA (oda.ft.dk).

Innehåller mapping- och normaliseringslogik som delas mellan synkskripten
(02_synka_oda.py initial laddning, 07_backfill_datum.py retroaktiv städning)
och andra konsumenter. Innehåller inga API-anrop — bara rena
datatransformationer av Sag-objekt.
"""

from typing import Optional


def basta_datum(sag: dict) -> Optional[str]:
    """
    Returnerar ärendets bästa tillgängliga datum (YYYY-MM-DD) för visning i
    sökresultat, prioriterat enligt vad som är mest informativt för en
    utredare:

      1. afgørelsesdato — slutligt beslut/svar
      2. lovnummerdato  — när lagen registrerades i Lovtidende (vedtagelse)
      3. rådsmødedato   — rådsmötesdatum
      4. opdateringsdato — senast uppdaterad i ODA (fallback)

    För lagstiftning ger lovnummerdato det relevanta vedtagelsesdatumet.
    För foresporgsel/spørgsmål utan beslutsdatum är opdateringsdato det enda
    tillgängliga datumet och används som fallback.

    Returnerar None om inget av fälten är ifyllt.
    """
    for falt in ("afgørelsesdato", "lovnummerdato", "rådsmødedato", "opdateringsdato"):
        v = sag.get(falt)
        if v:
            return v[:10]
    return None
