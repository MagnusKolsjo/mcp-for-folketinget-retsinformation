# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
tyst_fd.py — Tillfällig omdirigering av fildeskriptor 1 och 2 till en loggfil.

Bibliotek med C-bindningar (pymupdf, sentence-transformers, tokenizers)
skriver ibland direkt till fd 1 och 2, förbi Pythons sys.stdout. Sådan
utdata ska till en loggfil i stället för att hamna i MCP-klientens logg
eller i terminalen under en synk.

Fildeskriptorerna är gemensamma för hela processen. MCP-servern kör
synkrona verktyg på flera arbetstrådar samtidigt, och två överlappande
omdirigeringar skulle återställa i fel ordning: den andra tråden sparar
den första trådens loggfil som "original" och lämnar fd 1 och 2 pekande
på loggfilen när den är klar. Omdirigeringarna serialiseras därför med
ett processgemensamt lås. Låset är återinträdesbart, så att en
omdirigering inuti en annan i samma tråd inte låser sig själv.

Låset gör också att pymupdf aldrig anropas från två trådar samtidigt,
vilket MuPDF inte klarar.

Ingångspunkt:
    tysta_fd(log_vag) -> kontexthanterare
"""

import contextlib
import os
import threading
from pathlib import Path

_FD_LAS = threading.RLock()


@contextlib.contextmanager
def tysta_fd(log_vag: Path | str):
    """Pekar om fd 1 och 2 till `log_vag` under blocket och återställer dem sedan."""
    with _FD_LAS:
        # Varje deskriptor stängs bara om den faktiskt öppnades, så att ett
        # fel i os.open eller os.dup inte läcker de deskriptorer som redan
        # skapats. Att återställa fd 1 och 2 innan de pekats om är ofarligt.
        spara_ut = spara_fel = log_fd = None
        try:
            spara_ut = os.dup(1)
            spara_fel = os.dup(2)
            log_fd = os.open(str(log_vag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            yield
        finally:
            if spara_ut is not None:
                os.dup2(spara_ut, 1)
                os.close(spara_ut)
            if spara_fel is not None:
                os.dup2(spara_fel, 2)
                os.close(spara_fel)
            if log_fd is not None:
                os.close(log_fd)
