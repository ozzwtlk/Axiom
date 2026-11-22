"""Remove the owned copy of a book and its derived state, never the upload source."""
from __future__ import annotations

import re
import shutil
from uuid import uuid4

from axiom.config import settings
from axiom.storage import db


def remove_document(doc_id: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", doc_id):
        raise KeyError(doc_id)
    original = settings.doc_dir(doc_id)
    trash = original.with_name(f".removed-{uuid4().hex}")
    moved = False
    try:
        with db.connect() as c:
            # Acquire the write lock before checking the queue or touching files.
            if not c.execute("UPDATE documents SET id=id WHERE id=?", (doc_id,)).rowcount:
                raise KeyError(doc_id)
            if c.execute("SELECT 1 FROM jobs WHERE doc_id=? AND status IN ('pending','running')", (doc_id,)).fetchone():
                raise ValueError("Remove queued jobs and let running jobs finish before removing this book.")
            for table in ("solutions", "answer_checks"):
                c.execute(f"DELETE FROM {table} WHERE exercise_id IN (SELECT id FROM exercises WHERE doc_id=?)", (doc_id,))
            c.execute("DELETE FROM quiz_attempts WHERE quiz_id IN (SELECT id FROM quizzes WHERE doc_id=?)", (doc_id,))
            for table in ("units", "coverage", "study_cards"):
                c.execute(f"DELETE FROM {table} WHERE section_id IN (SELECT id FROM sections WHERE doc_id=?)", (doc_id,))
            for prefix in ("quality:", "faithfulness:", "audit:", "empty:"):
                c.execute("DELETE FROM meta WHERE key IN (SELECT ? || id FROM sections WHERE doc_id=?)", (prefix, doc_id))
            c.execute("DELETE FROM meta WHERE key=?", (f"notation:{doc_id}",))
            for table in ("blocks", "notation", "concepts", "exercises", "quizzes", "visualizations", "search_fts",
                          "jobs", "llm_calls", "covers", "sections", "chapters"):
                c.execute(f"DELETE FROM {table} WHERE doc_id=?", (doc_id,))
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            if original.exists():
                original.rename(trash)
                moved = True
    except Exception:
        if moved:
            trash.rename(original)
        raise
    if moved:
        shutil.rmtree(trash)
    exports = settings.data_dir / "exports"
    if exports.is_dir():
        for path in exports.iterdir():
            if path.stem.endswith(f"-{doc_id}") and path.suffix in (".apkg", ".txt"):
                path.unlink(missing_ok=True)
