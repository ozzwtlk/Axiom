import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listDocuments, listImports, uploadDocument } from "../api/client";
import type { DocumentSummary, PendingImport } from "../api/types";
import { Cover } from "./DocumentPage";

export function Library() {
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const [imports, setImports] = useState<PendingImport[]>([]);
  const refresh = () =>
    Promise.all([listDocuments(), listImports().catch(() => [])])
      .then(([d, im]) => (setDocs(d), setImports(im), setErr(null)))
      .catch((e) => setErr(`Could not load documents: ${e.message ?? e}`));

  useEffect(() => {
    refresh();
  }, []);
  useEffect(() => {  // a computing machine imports queued uploads: watch for them to appear
    if (!imports.length) return;
    const t = window.setInterval(refresh, 5000);
    return () => window.clearInterval(t);
  }, [imports.length]);

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
            <Link key={d.id} to={`/doc/${d.id}`} className="book-card">
              <Cover docId={d.id} title={d.title} />
              <div className="book-title">{d.title}</div>
              <div className="book-meta">
                {d.chapters_done} of {d.chapters_total} chapters ready · {d.page_count} pages
              </div>
              <div className="bar" aria-label={`${d.chapters_done} of ${d.chapters_total} chapters processed`}>
                <div style={{ width: `${d.chapters_total ? (100 * d.chapters_done) / d.chapters_total : 0}%` }} />
              </div>
            </Link>
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
