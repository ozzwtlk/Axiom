import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { search } from "../api/client";
import type { SearchHit } from "../api/types";
import { MdInline } from "./Md";

export function SearchBox({ docId, compact }: { docId: string; compact?: boolean }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const query = q.trim();
    if (query.length < 2) {
      setHits(null);
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      search(docId, query)
        .then((h) => !cancelled && (setHits(h), setErr(null)))
        .catch((e) => !cancelled && setErr(String(e.message ?? e)));
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q, docId]);

  useEffect(() => {
    const fn = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", fn);
    return () => document.removeEventListener("mousedown", fn);
  }, []);

  const go = (h: SearchHit) => {
    setOpen(false);
    nav(`/read/${h.section_id}`, { state: { blockId: h.block_id } });
  };

  return (
    <div className={`search ${compact ? "search-compact" : ""}`} ref={boxRef}>
      <input
        type="search"
        placeholder="Search book…"
        aria-label="Search this document"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Escape") (e.target as HTMLInputElement).blur(), setOpen(false);
          if (e.key === "Enter" && hits && hits[0]) go(hits[0]);
        }}
      />
      {open && (hits || err) && (
        <ul className="search-results" role="listbox">
          {err && <li className="error">{err}</li>}
          {hits && hits.length === 0 && <li className="muted">No matches</li>}
          {hits?.slice(0, 30).map((h, i) => (
            <li key={`${h.block_id}-${i}`}>
              <button type="button" onClick={() => go(h)}>
                <span className="search-hit-title">
                  {h.section_title} <span className="muted">· p.{h.page}</span>
                </span>
                {/* snippet is plain text from the backend; rendered inline (math-aware, HTML escaped) */}
                <MdInline src={h.snippet} className="search-hit-snippet" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
