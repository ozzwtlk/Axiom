"""Anki export (deterministic v1): cards from verified units, no model calls.

Card sources (only units at Fast-Track level 1 or titled definitions/results, to avoid card spam):
  * definition / theorem / result units with a title → "Define/State: <title>" → text (+ its equation)
  * a prose lead-in immediately followed by a numbered equation → lead-in → verbatim equation
Math is converted to Anki's MathJax delimiters \\( \\) and \\[ \\].
Each note has a stable GUID derived from its source block ids, so re-importing updates existing notes
(Anki matches on GUID) instead of creating duplicates. Anki's importer asks before updating; nothing is
overwritten silently.
"""

from __future__ import annotations

import hashlib
import html
import json
import re

from axiom.storage import db


def to_anki_math(md: str) -> str:
    s = re.sub(r"\$\$(.+?)\$\$", lambda m: r"\[" + m.group(1).strip() + r"\]", md or "", flags=re.S)
    s = re.sub(r"(?<![\\$])\$(?!\$)(.+?)(?<![\\$])\$", lambda m: r"\(" + m.group(1).strip() + r"\)", s, flags=re.S)
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", s)
    return s.replace("\n", "<br>").replace("\t", " ")


def _guid(kind: str, block_ids: list[str]) -> str:
    return "axiom-" + hashlib.sha1((kind + "|" + ",".join(block_ids)).encode()).hexdigest()[:16]


def build_cards(doc_id: str, chapters: list[int] | None = None) -> list[dict]:
    doc = db.q1("SELECT title FROM documents WHERE id=?", (doc_id,))
    q = ("SELECT s.id, s.number, s.title, c.number AS ch FROM sections s JOIN chapters c ON c.id=s.chapter_id"
         " WHERE s.doc_id=? AND s.stage='verified' ORDER BY c.number, s.ord")
    cards: list[dict] = []
    for sec in db.q(q, (doc_id,)):
        if chapters and sec["ch"] not in chapters:
            continue
        units = [json.loads(r["data"]) for r in db.q("SELECT data FROM units WHERE section_id=? ORDER BY ord", (sec["id"],))]
        tags = [f"axiom::ch{sec['ch']}", f"axiom::sec{sec['number'].replace('.', '_')}"]
        cite = f"{doc['title']} §{sec['number']} {sec['title']}"
        for i, u in enumerate(units):
            if (u.get("verification") or {}).get("status") == "flagged":
                continue  # an independent check could not confirm it: not flashcard material
            nxt = units[i + 1] if i + 1 < len(units) else None
            eq_after = nxt if (nxt and nxt["kind"] == "equation" and nxt.get("latex")) else None
            pages = u.get("pages") or []
            src = f"{cite}, p.{pages[0]}" if pages else cite
            if u["kind"] in ("definition", "theorem", "result") and u.get("title"):
                verb = "Define" if u["kind"] == "definition" else "State"
                back = u.get("fast_md") or u["text_md"]
                if eq_after:
                    back += f"\n$${eq_after['latex']}$$" + (f" ({eq_after['eq_number']})" if eq_after.get("eq_number") else "")
                cards.append({"guid": _guid("def", u["block_ids"]), "front": f"{verb}: {u['title']}", "back": back,
                              "source": src, "tags": tags + [f"axiom::{u['kind']}"], "level": u["min_level"]})
            elif (eq_after and eq_after.get("eq_number") and u["kind"] != "heading" and u["min_level"] <= 2
                  and (u["text_md"] or "").rstrip().endswith(":")):
                cards.append({"guid": _guid("eq", eq_after["block_ids"]),
                              "front": (u.get("fast_md") or u["text_md"]).strip() + " (complete the equation)",
                              "back": f"$${eq_after['latex']}$$ ({eq_after['eq_number']})"
                                      + (f"\n{eq_after['annotation_md']}" if eq_after.get("annotation_md") else ""),
                              "source": src, "tags": tags + ["axiom::equation"], "level": min(u["min_level"], eq_after["min_level"])})
    seen, out = set(), []
    for c in cards:
        if c["guid"] not in seen:
            seen.add(c["guid"])
            out.append(c)
    return out


def to_tsv(cards: list[dict], deck: str) -> str:
    lines = ["#separator:tab", "#html:true", "#notetype:Basic", f"#deck:{deck}", "#guid column:1", "#tags column:4"]
    for c in cards:
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
    return [to_anki_math(c["front"]), to_anki_math(c["back"]), html.escape(c["source"]), c["guid"]]


def _deck_id(deck: str) -> int:
    return int(hashlib.sha1(("axiom-deck|" + deck).encode()).hexdigest()[:8], 16)


def to_apkg(cards: list[dict], deck: str, path) -> int:
    import genanki
    model = genanki.Model(MODEL_ID, MODEL_NAME, fields=[{"name": f} for f in FIELDS],
                          templates=[{"name": "Card 1", "qfmt": QFMT, "afmt": AFMT}], css=CSS)
    d = genanki.Deck(_deck_id(deck), deck)
    for c in cards:
        d.add_note(genanki.Note(model=model, fields=_fields(c), guid=c["guid"],
                                tags=[t.replace(" ", "_") for t in c["tags"]]))
    genanki.Package(d).write_to_file(str(path))
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
