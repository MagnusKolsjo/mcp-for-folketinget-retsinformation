"""
retsinformation_client.py — HTTP-klient för retsinformation.dk ELI-endpoint.

Hanterar nätverksanrop mot retsinformation.dk ELI JSON-API:et.
Separerat från db.py så att databasmodulen inte innehåller HTTP-logik.
"""

import json as _json
import urllib.request


def hamta_eli_json_relationer(eli_url: str) -> list[str]:
    """
    Hämtar eli:changes-URLs direkt från retsinformation.dk ELI JSON-endpoint.
    Används vid skörd för att extrahera relationer.
    Returnerar lista av ELI-URLs som detta dokument ändrar.
    """
    json_url = eli_url.rstrip("/") + ".json"
    try:
        req = urllib.request.Request(json_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    ELI_CHANGES = "http://data.europa.eu/eli/ontology#changes"
    changes = []
    for item in data:
        # Hitta huvud-resursen (inte dan/xml/html-varianter)
        item_id = item.get("@id", "")
        if "/dan" in item_id or item_id.endswith("/xml") or item_id.endswith("/html"):
            continue
        for rel in item.get(ELI_CHANGES, []):
            url = rel.get("@value") or rel.get("@id")
            if url:
                changes.append(url)
    return changes
