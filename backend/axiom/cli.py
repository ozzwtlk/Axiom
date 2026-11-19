"""AXIOM command line.

  axiom import <pdf>                      register a PDF (immutable copy)
  axiom process <doc_id> --chapters 1,2   queue + run the pipeline for chapters (foreground worker)
  axiom stage <doc_id> --chapters 1,2 --until transcribe   run stages directly (debug)
  axiom status <doc_id>                   progress report
  axiom serve                             API server + background worker
  axiom anki <doc_id> [--format apkg|tsv|push] [--deck D] [--out F]   flashcards (file or AnkiConnect)
  axiom refit-crops <doc_id>              grow cut-off equation boxes; reset their transcriptions
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from axiom.config import settings
from axiom.storage import db


def _chapters(doc_id: str, spec: str | None):
    rows = db.q("SELECT * FROM chapters WHERE doc_id=? ORDER BY number", (doc_id,))
    if spec:
        want = {int(x) for x in spec.split(",")}
        rows = [r for r in rows if r["number"] in want]
    return rows


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="axiom")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import"); p.add_argument("pdf"); p.add_argument("--title")
    p = sub.add_parser("process"); p.add_argument("doc_id"); p.add_argument("--chapters"); p.add_argument("--profile", default="economy")
    p = sub.add_parser("stage"); p.add_argument("doc_id"); p.add_argument("--chapters")
    p.add_argument("--until", default="verify", choices=["extract", "transcribe", "optimize", "verify"])
    p.add_argument("--force-extract", action="store_true"); p.add_argument("--profile", default="economy")
    p = sub.add_parser("status"); p.add_argument("doc_id")
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-worker", action="store_true")
    p = sub.add_parser("anki"); p.add_argument("doc_id"); p.add_argument("--format", default="apkg", choices=["apkg", "tsv", "push"])
    p.add_argument("--deck", default="AXIOM"); p.add_argument("--chapters"); p.add_argument("--out")
    p = sub.add_parser("refit-crops"); p.add_argument("doc_id")
    a = ap.parse_args(argv)
    db.init(settings.db_path)

    if a.cmd == "import":
        from axiom.ingestion.pdf import import_pdf
        doc_id = import_pdf(Path(a.pdf), a.title)
        n = db.q1("SELECT COUNT(*) n FROM chapters WHERE doc_id=?", (doc_id,))["n"]
        print(json.dumps({"doc_id": doc_id, "chapters": n}))
    elif a.cmd == "stage":
        from axiom.extraction.extract import extract_chapter
        from axiom.transformations.transcribe import transcribe_section
        for ch in _chapters(a.doc_id, a.chapters):
            n = extract_chapter(a.doc_id, ch["id"], force=a.force_extract)
            print(f"chapter {ch['number']}: extracted {n} blocks")
            if a.until == "extract":
                continue
            for s in db.q("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (ch["id"],)):
                print(f"  {s['number']} transcribe:", transcribe_section(s["id"], a.profile), flush=True)
                if a.until in ("optimize", "verify"):
                    from axiom.orchestration.pipeline import optimize_and_verify
                    print(f"  {s['number']} optimize:", optimize_and_verify(s["id"], a.profile), flush=True)
                if a.until == "verify":
                    from axiom.orchestration.pipeline import verify_and_finalize
                    print(f"  {s['number']} verify:", verify_and_finalize(s["id"], a.profile), flush=True)
    elif a.cmd == "process":
        from axiom.orchestration import jobs
        ids = jobs.enqueue_document(a.doc_id, [c["number"] for c in _chapters(a.doc_id, a.chapters)], a.profile)
        print(f"queued {len(ids)} jobs; running worker until idle")
        jobs.run_until_idle()
    elif a.cmd == "status":
        from axiom.api.app import progress_report
        print(progress_report(a.doc_id).model_dump_json(indent=2))
    elif a.cmd == "anki":
        from axiom.anki import export as ax
        cards = ax.build_cards(a.doc_id, [int(x) for x in a.chapters.split(",")] if a.chapters else None)
        if a.format == "push":
            print(json.dumps(ax.push_ankiconnect(cards, a.deck)))
        else:
            out = Path(a.out) if a.out else ax.export_path(a.doc_id, a.deck, a.format)
            if a.format == "apkg":
                ax.to_apkg(cards, a.deck, out)
            else:
                out.write_text(ax.to_tsv(cards, a.deck), encoding="utf-8")
            print(f"{len(cards)} cards -> {out}")
    elif a.cmd == "refit-crops":
        from axiom.extraction.extract import refit_equation_boxes
        changed = refit_equation_boxes(a.doc_id)
        print(f"{len(changed)} equation boxes grown (transcriptions reset): {changed}")
    elif a.cmd == "serve":
        import uvicorn
        from axiom.api.app import create_app
        uvicorn.run(create_app(start_worker=not a.no_worker), host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
