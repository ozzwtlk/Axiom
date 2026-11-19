import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getDocument, getExerciseMap } from "../api/client";
import type { DocumentDetail, ExerciseMapItem } from "../api/types";
import { MdInline } from "../components/Md";
import { excerpt } from "../lib/markdown";
import { usePrefs } from "../lib/prefs";
import { exKey, load, type ExerciseMark } from "../lib/storage";

/** Exercise-first study path: start from the problems, read the sections each one draws on when stuck. */
export function PracticePage() {
  const { docId = "" } = useParams();
  const [items, setItems] = useState<ExerciseMapItem[] | null>(null);
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { exerciseFirst, setExerciseFirst } = usePrefs();

  useEffect(() => {
    getExerciseMap(docId).then(setItems).catch((e) => setErr(String(e)));
    getDocument(docId).then(setDoc).catch(() => undefined);
  }, [docId]);

  const secName = useMemo(() => {
    const m = new Map<string, string>();
    doc?.chapters.forEach((c) => c.sections.forEach((s) => m.set(s.id, `§${s.number} ${s.title}`)));
    return m;
  }, [doc]);

  const byChapter = useMemo(() => {
    const m = new Map<number, ExerciseMapItem[]>();
    (items ?? []).forEach((e) => m.set(e.chapter_number, [...(m.get(e.chapter_number) ?? []), e]));
    return [...m.entries()];
  }, [items]);

  return (
    <main className="page">
      <nav className="crumbs">
        <Link to={`/doc/${docId}`}>Document</Link>
      </nav>
      <h1>Exercise-first practice</h1>
      <p>
        Attempt each problem first; open the sections listed under it only when you are stuck. The links come from the
        concepts and equation numbers each exercise uses.
      </p>
      <label className="chapter-check" style={{ fontSize: "0.88rem" }}>
        <input type="checkbox" checked={exerciseFirst} onChange={(e) => setExerciseFirst(e.target.checked)} /> Also show
        related exercises at the top of each section while reading
      </label>
      {err && <p className="error">{err}</p>}
      {!items && !err && <p className="muted">Loading…</p>}
      {byChapter.map(([ch, list]) => (
        <section key={ch}>
          <h2 className="practice-chapter">Chapter {ch}</h2>
          <ul className="practice-list">
            {list.map((e) => {
              const mark = load<ExerciseMark>(exKey(e.id, "mark"), { completed: false, difficult: false });
              return (
                <li key={e.id} className="quiz-q">
                  <Link to={`/exercise/${e.id}`}>
                    <strong>Exercise {e.number}</strong>
                  </Link>{" "}
                  {mark.completed && <span className="tag tag-done">✓ done</span>}{" "}
                  {mark.difficult && <span className="tag">difficult</span>}
                  <div className="small">
                    <MdInline src={excerpt(e.statement_md, 220)} />
                  </div>
                  {e.related_section_ids.length > 0 && (
                    <div className="small muted">
                      If stuck, read:{" "}
                      {e.related_section_ids.map((s) => (
                        <Link key={s} to={`/read/${s}`} className="tag">
                          {secName.get(s) ?? s}
                        </Link>
                      ))}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </main>
  );
}
