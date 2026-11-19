import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listDocuments, uploadDocument } from "../api/client";
import type { DocumentSummary } from "../api/types";
import { Cover } from "./DocumentPage";

export function Library() {
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = () =>
    listDocuments()
      .then((d) => (setDocs(d), setErr(null)))
      .catch((e) => setErr(`Could not load documents: ${e.message ?? e}`));

  useEffect(() => {
    refresh();
  }, []);

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
