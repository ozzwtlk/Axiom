import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listDocuments, listImports, MOCK, removeDocument, uploadDocument } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import type { DocumentSummary, PendingImport } from "../api/types";
import { Cover } from "./DocumentPage";

export function Library() {
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const [imports, setImports] = useState<PendingImport[]>([]);
  const refresh = () =>
    Promise.all([listDocuments(), listImports().catch(() => [])])
      .then(([d, im]) => (setDocs(d), setImports(im), setErr(null)))
      .catch((e) => setErr(`Could not load documents: ${e.message ?? e}`));

  useEffect(() => {
    refresh();
  }, []);
  useEffect(() => {
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible" && !removing) refresh();
    }, 8000);
    return () => window.clearInterval(t);
  }, [removing]);

  const onRemove = async (doc: DocumentSummary) => {
    if (!window.confirm(`Remove “${doc.title}” and its processed data from AXIOM? Your original upload is not affected.`)) return;
    setRemoving(doc.id);
    setErr(null);
    try {
      await removeDocument(doc.id);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setRemoving(null);
    }
  };

  const onUpload = async (f: File | undefined) => {
    if (!f) return;
    setUploading(true);
    setErr(null);
    try {
      await uploadDocument(f);
      await refresh();
    } catch (e) {
      setErr(`Upload failed: ${(e as Error).message ?? e}`);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <main className="page page-wide">
      <div className="page-head">
        <div>
          <span className="eyebrow">Your books</span>
          <h1>Library</h1>
        </div>
        <span className="muted small">Original PDFs stay on this computer and are never modified.</span>
      </div>
      {err && <p className="error">{err}</p>}
      {docs === null && !err && <p className="muted">Loading…</p>}
      {docs && (
        <div className="lib-grid">
          {docs.map((d) => (
            <article key={d.id} className="book-entry">
              <Link to={`/doc/${d.id}`} className="book-card">
                <Cover docId={d.id} title={d.title} revision={d.cover_revision} />
                <div className="book-title">{d.title}</div>
                <div className="book-meta">
                  {d.chapters_done} of {d.chapters_total} chapters ready · {d.page_count} pages
                  {d.completed && <> · <StatusBadge status="done" /></>}
                </div>
                <div className="bar" aria-label={`${d.chapters_done} of ${d.chapters_total} chapters processed`}>
                  <div style={{ width: `${d.chapters_total ? (100 * d.chapters_done) / d.chapters_total : 0}%` }} />
                </div>
              </Link>
              {!MOCK && <button type="button" className="link-btn small book-remove" disabled={removing !== null}
                                aria-label={`Remove ${d.title}`} onClick={() => onRemove(d)}>
                {removing === d.id ? "Removing…" : "Remove"}
              </button>}
            </article>
          ))}
          {imports.map((im) => (
            <div key={im.id} className="book-card pending-import">
              <div className="book-cover">
                <div className="book-cover-fallback">
                  {im.status === "failed" ? "Import failed" : im.status === "running" ? "Importing…" : "Waiting for a computing machine…"}
                </div>
              </div>
              <div className="book-title">{im.filename}</div>
              <div className="book-meta">{im.error ?? "Stored on the server; imported as soon as a PC connects."}</div>
            </div>
          ))}
          <label className={`add-card ${uploading ? "disabled" : ""}`}>
            <span className="plus" aria-hidden="true">+</span>
            <strong>{uploading ? "Uploading…" : "Add a PDF"}</strong>
            <span className="small">A physics textbook or lecture notes</span>
            <input
              ref={fileRef}
              type="file"
              accept="application/pdf,.pdf"
              disabled={uploading}
              hidden
              onChange={(e) => onUpload(e.target.files?.[0])}
            />
          </label>
        </div>
      )}
    </main>
  );
}
