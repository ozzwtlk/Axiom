"""Export reviewed study decks with MathJax and original figure crops."""

from __future__ import annotations

import base64
import hashlib
import html
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from axiom.anki.study import build_cards as build_cards
from axiom.storage import db


def to_anki_math(md: str) -> str:
    s = re.sub(r"\$\$(.+?)\$\$", lambda m: r"\[" + m.group(1).strip() + r"\]", md or "", flags=re.S)
    s = re.sub(r"(?<![\\$])\$(?!\$)(.+?)(?<![\\$])\$", lambda m: r"\(" + m.group(1).strip() + r"\)", s, flags=re.S)
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", s)
    return s.replace("\n", "<br>").replace("\t", " ")


def _with_media(cards: list[dict]) -> tuple[list[dict], dict[str, bytes]]:
    from axiom.extraction.extract import crop_png, open_pdf
    from contextlib import ExitStack

    out, media, names, pdfs = [], {}, {}, {}
    with ExitStack() as stack:
        for card in cards:
            bid = card.get("figure")
            if not bid:
                out.append(card)
                continue
            if bid not in names:
                names[bid] = None
                row = db.q1("SELECT * FROM blocks WHERE id=? AND block_type='figure'", (bid,))
                if row:
                    doc_id = row["doc_id"]
                    if doc_id not in pdfs:
                        pdfs[doc_id] = stack.enter_context(open_pdf(doc_id))
                    try:
                        png = crop_png(doc_id, row["page"], [row[k] for k in ("x0", "y0", "x1", "y1")],
                                       dpi=130, pdf=pdfs[doc_id])
                    except ValueError:  # stale or degenerate crop; don't export an unanswerable card
                        continue
                    name = f"axiom-{hashlib.sha256(png).hexdigest()[:24]}.png"
                    media[name] = png
                    names[bid] = name
            if names[bid]:
                out.append(dict(card, media_name=names[bid]))
    return out, media


def to_tsv(cards: list[dict], deck: str) -> str:
    deck = " ".join(deck.split()) or "AXIOM"
    lines = ["#separator:tab", "#html:true", "#notetype:Basic", f"#deck:{deck}", "#guid column:1", "#tags column:4"]
    for c in cards:
        if c.get("figure"):
            continue  # this single-file format cannot carry figure media
        back = to_anki_math(c["back"]) + f"<br><small>{html.escape(c['source'])}</small>"
        lines.append("\t".join([c["guid"], to_anki_math(c["front"]), back, " ".join(c["tags"])]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- .apkg + AnkiConnect
# One note type for both paths, so a deck imported from .apkg and notes pushed via AnkiConnect are the same notes
# (AnkiConnect cannot set GUIDs; it finds notes by the AxiomID field instead).
MODEL_NAME = "AXIOM Basic"
MODEL_ID = 1607392319  # fixed: changing it would make Anki treat re-imports as a new note type
FIELDS = ["Front", "Back", "Source", "AxiomID"]
QFMT = "{{Front}}"
AFMT = "{{FrontSide}}<hr id=answer>{{Back}}<div class=src>{{Source}}</div>"
CSS = (".card{font-family:Georgia,serif;font-size:19px;text-align:left;color:#111;background:#fff}"
       ".src{margin-top:1em;font-size:12px;color:#777}.nightMode .src{color:#aaa}")


def _fields(c: dict) -> list[str]:
    front = to_anki_math(c["front"])
    if c.get("media_name"):
        front += f'<br><img src="{html.escape(c["media_name"], quote=True)}" alt="Source figure">'
    return [front, to_anki_math(c["back"]), html.escape(c["source"]), c["guid"]]


def _deck_id(deck: str) -> int:
    return int(hashlib.sha1(("axiom-deck|" + deck).encode()).hexdigest()[:8], 16)


def to_apkg(cards: list[dict], deck: str, path) -> int:
    import genanki
    model = genanki.Model(MODEL_ID, MODEL_NAME, fields=[{"name": f} for f in FIELDS],
                          templates=[{"name": "Card 1", "qfmt": QFMT, "afmt": AFMT}], css=CSS)
    d = genanki.Deck(_deck_id(deck), deck)
    cards, media = _with_media(cards)
    for c in cards:
        d.add_note(genanki.Note(model=model, fields=_fields(c), guid=c["guid"],
                                tags=[t.replace(" ", "_") for t in c["tags"]]))
    with TemporaryDirectory() as tmp:
        paths = []
        for name, png in media.items():
            p = Path(tmp) / name
            p.write_bytes(png)
            paths.append(str(p))
        genanki.Package(d, media_files=paths).write_to_file(str(path))
    return len(cards)


def push_ankiconnect(cards: list[dict], deck: str, url: str = "http://127.0.0.1:8765") -> dict:
    """Add or update notes in a running Anki desktop (AnkiConnect add-on). Local only; nothing leaves the machine."""
    import httpx

    def inv(action: str, **params):
        try:
            r = httpx.post(url, json={"action": action, "version": 6, "params": params}, timeout=30)
        except httpx.HTTPError as e:
            raise ConnectionError("AnkiConnect is not reachable: is Anki running with the AnkiConnect add-on?") from e
        d = r.json()
        if d.get("error"):
            raise RuntimeError(f"AnkiConnect {action}: {d['error']}")
        return d["result"]

    inv("version")
    if MODEL_NAME not in inv("modelNames"):
        inv("createModel", modelName=MODEL_NAME, inOrderFields=FIELDS, css=CSS,
            cardTemplates=[{"Name": "Card 1", "Front": QFMT, "Back": AFMT}])
    inv("createDeck", deck=deck)
    cards, media = _with_media(cards)
    for name, png in media.items():
        inv("storeMediaFile", filename=name, data=base64.b64encode(png).decode("ascii"))
    added = updated = 0
    for c in cards:
        fields = dict(zip(FIELDS, _fields(c)))
        ids = inv("findNotes", query=f'"note:{MODEL_NAME}" "AxiomID:{c["guid"]}"')
        if ids:
            inv("updateNoteFields", note={"id": ids[0], "fields": fields})
            updated += 1
        else:
            inv("addNote", note={"deckName": deck, "modelName": MODEL_NAME, "fields": fields,
                                 "tags": [t.replace(" ", "_") for t in c["tags"]],
                                 "options": {"allowDuplicate": False}})
            added += 1
    return {"added": added, "updated": updated, "deck": deck}


def export_path(doc_id: str, deck: str, fmt: str):
    from axiom.config import settings
    out = settings.data_dir / "exports"
    out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "_", deck).strip("_") or "AXIOM"
    return out / f"{safe}-{doc_id}.{'apkg' if fmt == 'apkg' else 'txt'}"
