import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { ask, getDocument, getExerciseMap, getSection, getXrefs, MOCK } from "../api/client";
import { XrefLayer } from "../components/XrefLayer";
import { setXrefs } from "../lib/xrefs";
import type { AskAction, ChapterSummary, DocumentDetail, ExerciseMapItem, Level, SectionView } from "../api/types";
import { AskDrawer, type AskEntry } from "../components/AskDrawer";
import { AskToolbar, snapshotSelection, type SelectionSnapshot } from "../components/AskToolbar";
import { Md } from "../components/Md";
import { excerpt } from "../lib/markdown";
import { getModelChoice } from "../lib/models";
import { SearchBox } from "../components/SearchBox";
import { ShortcutsHelp } from "../components/ShortcutsHelp";
import { isOpenable, StatusBadge } from "../components/StatusBadge";
import { UnitView } from "../components/UnitView";
import { Icon } from "../components/Icon";
import { MachinePicker } from "../components/MachinePicker";
import { isTypingTarget, usePrefs, type Width } from "../lib/prefs";

const WIDTHS: { w: Width; label: string }[] = [
  { w: "narrow", label: "Narrow" },
  { w: "medium", label: "Medium" },
  { w: "wide", label: "Wide" },
];
import { exKey, load, loadPosition, savePosition, type ExerciseMark } from "../lib/storage";

const LEVELS: { level: Level; label: string; hint: string }[] = [
  { level: 1, label: "Fast Track", hint: "Essentials only, condensed (key 1)" },
  { level: 2, label: "Standard", hint: "Full text (key 2)" },
  { level: 3, label: "Deep Dive", hint: "Everything plus AI-added explanations (key 3)" },
];

interface NavState {
  unitId?: string | null;
  blockId?: string;
}

interface Target {
  sectionId: string;
  unitId?: string | null;
  blockId?: string;
  restore: boolean;
}

/** Height of the sticky header (kept in sync with --header-h in CSS). */
function headerH(): number {
  const v = getComputedStyle(document.documentElement).getPropertyValue("--header-h");
  return parseFloat(v) || 56;
}

/** Index of the first element (in document order) whose bottom is below the sticky header. */
function topmostIndex(els: HTMLElement[]): number {
  const h = headerH();
  let lo = 0;
  let hi = els.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (els[mid].getBoundingClientRect().bottom > h + 1) {
      ans = mid;
      hi = mid - 1;
    } else lo = mid + 1;
  }
  return ans;
}

type NextInfo =
  | { kind: "append"; id: string }
  | { kind: "notReady"; id: string; title: string }
  | { kind: "chapterEnd"; next: ChapterSummary | null }
  | { kind: "bookEnd" };

export function Reader() {
  const { sectionId = "" } = useParams();
  const location = useLocation();
  const nav = useNavigate();
  const { level, setLevel } = usePrefs();

  const [sections, setSections] = useState<SectionView[]>([]);
  const sectionsRef = useRef(sections);
  sectionsRef.current = sections;
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [appendError, setAppendError] = useState<string | null>(null);
  const [current, setCurrent] = useState<string>(sectionId);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [snap, setSnap] = useState<SelectionSnapshot | null>(null);
  const [entries, setEntries] = useState<AskEntry[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [crossChapter, setCrossChapter] = useState<string | null>(null);

  const mainRef = useRef<HTMLDivElement>(null);
  const target = useRef<Target | null>(null);
  const pendingAnchor = useRef<{ id: string; offset: number } | null>(null);
  const loadToken = useRef(0);
  const lastSaved = useRef<string>("");

  const flash = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 2500);
  }, []);

  // ---- open the requested section (route param or same-route navigation with new state) ----
  useEffect(() => {
    const st = (location.state ?? null) as NavState | null;
    target.current = { sectionId, unitId: st?.unitId, blockId: st?.blockId, restore: !st };
    setSidebarOpen(false);
    if (sectionsRef.current.some((s) => s.id === sectionId)) {
      resolveTarget();
      return;
    }
    const token = ++loadToken.current;
    setSections([]);
    setError(null);
    setAppendError(null);
    setCurrent(sectionId);
    getSection(sectionId)
      .then((s) => {
        if (token !== loadToken.current) return;
        setSections([s]);
        setCurrent(s.id);
      })
      .catch((e) => token === loadToken.current && setError(`Could not load section: ${e.message ?? e}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sectionId, location.key]);

  // ---- document detail (sidebar + chapter boundaries) ----
  const docId = sections[0]?.document_id;
  useEffect(() => {
    if (!docId || doc?.id === docId) return;
    getDocument(docId)
      .then(setDoc)
      .catch(() => {
        /* sidebar degrades gracefully */
      });
  }, [docId, doc?.id]);

  // ---- cross-reference index: "(2.13)", "Figure 1.5", "Section 2.4" become links ----
  useEffect(() => {
    if (!docId || MOCK) return;
    let alive = true;
    getXrefs(docId)
      .then((x) => alive && setXrefs(x))
      .catch(() => {
        /* references simply stay plain text */
      });
    return () => {
      alive = false;
      setXrefs(null);
    };
  }, [docId]);
  const goToRef = useCallback(
    (sid: string, blockId?: string) => nav(`/read/${sid}`, { state: { blockId } satisfies NavState }),
    [nav],
  );

  const chapterNumber = sections.find((s) => s.id === current)?.chapter_number ?? sections[0]?.chapter_number;
  const chapter = doc?.chapters.find((c) => c.number === chapterNumber) ?? null;

  // ---- resolve navigation target after render ----
  const resolveTarget = useCallback(() => {
    const t = target.current;
    if (!t) return;
    const sec = sectionsRef.current.find((s) => s.id === t.sectionId);
    if (!sec) return;
    let unitId = t.unitId ?? null;
    if (!unitId && t.restore) {
      const p = loadPosition(sec.document_id);
      if (p && p.sectionId === sec.id) unitId = p.unitId;
    }
    if (!unitId && t.blockId) {
      const u = sec.units.find((x) => x.source.block_ids.includes(t.blockId!));
      if (u) unitId = u.id;
    }
    if (unitId) {
      const u = sec.units.find((x) => x.id === unitId);
      if (u && u.min_level > level) {
        // Target is hidden at this level: raise the level, then retry after re-render.
        setLevel(u.min_level);
        flash(`Switched to ${LEVELS[u.min_level - 1].label} to show this passage`);
        return;
      }
    }
    target.current = null;
    const el =
      (unitId && document.getElementById(`unit-${unitId}`)) || document.getElementById(`sec-${sec.id}`);
    if (el) {
      el.scrollIntoView({ block: "start" });
      if (unitId && (t.blockId || t.unitId)) {
        el.classList.add("flash");
        window.setTimeout(() => el.classList.remove("flash"), 1600);
      }
    }
    setCurrent(sec.id);
  }, [level, setLevel, flash]);

  useLayoutEffect(() => {
    if (target.current) resolveTarget();
  }, [sections, level, resolveTarget]);

  // ---- preserve reading position across level switches ----
  const switchLevel = useCallback(
    (next: Level) => {
      if (next === level) return;
      const root = mainRef.current;
      if (root) {
        const els = Array.from(root.querySelectorAll<HTMLElement>("[data-unit-id]"));
        const top = topmostIndex(els);
        if (top >= 0) {
          let anchor: HTMLElement | null = null;
          for (let i = top; i >= 0; i--) {
            if (Number(els[i].dataset.minLevel) <= next) {
              anchor = els[i];
              break;
            }
          }
          const h = headerH();
          if (anchor) {
            const r = anchor.getBoundingClientRect().top;
            pendingAnchor.current = { id: anchor.id, offset: anchor === els[top] ? r : Math.max(r, h + 8) };
          } else {
            const sec = document.getElementById(`sec-${els[top].dataset.sectionId}`);
            if (sec) pendingAnchor.current = { id: sec.id, offset: h + 8 };
          }
        }
      }
      setLevel(next);
    },
    [level, setLevel],
  );

  useLayoutEffect(() => {
    const a = pendingAnchor.current;
    if (!a) return;
    pendingAnchor.current = null;
    const el = document.getElementById(a.id);
    if (el) window.scrollBy(0, el.getBoundingClientRect().top - a.offset);
  }, [level]);

  // ---- track current section/unit while scrolling; persist reading position ----
  useEffect(() => {
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const root = mainRef.current;
        if (!root) return;
        const els = Array.from(root.querySelectorAll<HTMLElement>("[data-unit-id]"));
        const i = topmostIndex(els);
        if (i < 0) return;
        const el = els[i];
        const sid = el.dataset.sectionId!;
        const uid = el.dataset.unitId!;
        setCurrent(sid);
        const sec = sectionsRef.current.find((s) => s.id === sid);
        if (sec && lastSaved.current !== uid) {
          lastSaved.current = uid;
          savePosition(sec.document_id, { sectionId: sid, unitId: uid, at: Date.now() });
          if (!window.location.pathname.endsWith(`/read/${sid}`)) {
            // Update the URL without a router navigation (would re-trigger section loading).
            window.history.replaceState(window.history.state, "", `/read/${sid}`);
          }
        }
      });
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  // ---- continuous scroll: lazily append following sections of the same chapter ----
  const last = sections[sections.length - 1];
  const nextInfo: NextInfo | null = useMemo(() => {
    if (!last) return null;
    const nextId = last.next_section_id;
    const ch = doc?.chapters.find((c) => c.number === last.chapter_number);
    if (!nextId) return { kind: "bookEnd" };
    if (nextId === crossChapter) return { kind: "chapterEnd", next: null };
    if (ch && !ch.sections.some((s) => s.id === nextId)) {
      const idx = doc!.chapters.indexOf(ch);
      return { kind: "chapterEnd", next: doc!.chapters[idx + 1] ?? null };
    }
    const sum = ch?.sections.find((s) => s.id === nextId);
    if (sum && !isOpenable(sum.status)) return { kind: "notReady", id: nextId, title: `${sum.number} ${sum.title}` };
    return { kind: "append", id: nextId };
  }, [last, doc, crossChapter]);

  const loadNext = useCallback(async () => {
    if (!nextInfo || nextInfo.kind !== "append" || loadingMore) return;
    const token = loadToken.current;
    setLoadingMore(true);
    setAppendError(null);
    try {
      const s = await getSection(nextInfo.id);
      if (token !== loadToken.current) return;
      if (s.chapter_number !== sectionsRef.current[sectionsRef.current.length - 1]?.chapter_number) {
        // Backend linked across a chapter boundary that the doc detail did not reveal; stop here.
        setCrossChapter(s.id);
        return;
      }
      if (!sectionsRef.current.some((x) => x.id === s.id)) setSections((prev) => [...prev, s]);
    } catch (e) {
      if (token === loadToken.current) setAppendError(`Could not load next section: ${(e as Error).message ?? e}`);
    } finally {
      setLoadingMore(false);
    }
  }, [nextInfo, loadingMore]);

  const sentinelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || appendError) return;
    // Re-created whenever the chain grows so a still-visible sentinel fires again.
    const io = new IntersectionObserver((ents) => ents.some((e) => e.isIntersecting) && loadNext(), {
      rootMargin: "1200px 0px",
    });
    io.observe(el);
    return () => io.disconnect();
  }, [loadNext, sections.length, appendError]);

  // ---- selection → Ask AI toolbar ----
  useEffect(() => {
    let t = 0;
    const update = () => {
      window.clearTimeout(t);
      t = window.setTimeout(() => {
        const root = mainRef.current;
        if (!root) return;
        const s = snapshotSelection(root);
        if (s) setSnap(s);
        else if (!document.activeElement?.closest(".ask-toolbar")) setSnap(null);
      }, 180);
    };
    document.addEventListener("selectionchange", update);
    return () => {
      document.removeEventListener("selectionchange", update);
      window.clearTimeout(t);
    };
  }, []);

  const doAsk = useCallback(
    (action: AskAction, question?: string) => {
      if (!snap) return;
      const id = Date.now();
      const entry: AskEntry = {
        id,
        action,
        question,
        selection: snap.text,
        blockIds: snap.blockIds,
        status: "loading",
      };
      setEntries((e) => [entry, ...e]);
      setDrawerOpen(true);
      setSnap(null);
      window.getSelection()?.removeAllRanges();
      const model = getModelChoice("ask");
      ask({ section_id: snap.sectionId, selection: snap.text, block_ids: snap.blockIds, action, question: question ?? null,
            ...(model !== "auto" ? { model } : {}) })
        .then((r) => setEntries((es) => es.map((x) => (x.id === id ? { ...x, status: "done", response: r } : x))))
        .catch((err) =>
          setEntries((es) =>
            es.map((x) => (x.id === id ? { ...x, status: "error", error: String(err.message ?? err) } : x)),
          ),
        );
    },
    [snap],
  );

  // ---- navigation helpers ----
  const goSection = useCallback(
    (id: string | null | undefined) => {
      if (!id) return false;
      const el = document.getElementById(`sec-${id}`);
      if (el) {
        el.scrollIntoView({ block: "start" });
        setCurrent(id);
      } else nav(`/read/${id}`);
      return true;
    },
    [nav],
  );

  const jumpExercises = useCallback(() => {
    const blocks = Array.from(document.querySelectorAll<HTMLElement>(".exercises-block"));
    const h = headerH() + 10;
    const nextBlock = blocks.find((b) => b.getBoundingClientRect().top > h) ?? blocks[0];
    if (nextBlock) {
      nextBlock.scrollIntoView({ block: "start" });
      return;
    }
    const exSec = chapter?.sections.find((s) => s.is_exercises);
    if (exSec && isOpenable(exSec.status)) nav(`/read/${exSec.id}`);
    else if (exSec) flash("This chapter's exercises are not processed yet");
    else flash("No exercises in the loaded sections");
  }, [chapter, nav, flash]);

  // ---- keyboard shortcuts ----
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTypingTarget(e)) return;
      const cur = sectionsRef.current.find((s) => s.id === current);
      switch (e.key) {
        case "1":
        case "2":
        case "3":
          switchLevel(Number(e.key) as Level);
          break;
        case "j":
          if (!goSection(cur?.next_section_id)) flash("Last section");
          break;
        case "k":
          if (!goSection(cur?.prev_section_id)) flash("First section");
          break;
        case "e":
        case "E":
          jumpExercises();
          break;
        case "?":
          setHelpOpen((h) => !h);
          break;
        case "/": {
          const inp = document.querySelector<HTMLInputElement>(".reader-header .search input");
          if (inp) {
            e.preventDefault();
            inp.focus();
          }
          break;
        }
        case "Escape":
          setHelpOpen(false);
          setDrawerOpen(false);
          setSidebarOpen(false);
          setSnap(null);
          break;
        default:
          return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, switchLevel, goSection, jumpExercises, flash]);

  const currentSection = sections.find((s) => s.id === current) ?? sections[0];

  return (
    <div className={`reader ${drawerOpen ? "with-drawer" : ""}`}>
      <header className="reader-header">
        <div className="header-left">
          <button
            type="button"
            className="icon-btn sidebar-toggle"
            aria-label="Contents"
            aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen((o) => !o)}
          >
            <Icon name="menu" />
          </button>
          <Link
            to={docId ? `/doc/${docId}` : "/"}
            className="icon-btn header-back"
            title={doc ? `Back to “${doc.title}”` : "Back to document"}
            aria-label="Back to document"
          >
            <Icon name="back" />
          </Link>
          <div className="header-crumb">
            <span className="header-book">{doc ? doc.title : " "}</span>
            <span className="header-title">
              {currentSection && (
                <>
                  {currentSection.number} {currentSection.title}
                </>
              )}
            </span>
          </div>
        </div>
        <div className="level-switch" role="radiogroup" aria-label="Reading level">
          {LEVELS.map((l) => (
            <button
              type="button"
              key={l.level}
              role="radio"
              aria-checked={level === l.level}
              className={level === l.level ? "active" : ""}
              title={l.hint}
              onClick={() => switchLevel(l.level)}
            >
              <span className="level-num">{l.level}</span>
              <span className="level-label">{l.label}</span>
            </button>
          ))}
        </div>
        <div className="header-right">
          <MachinePicker compact />
          {docId && <SearchBox docId={docId} compact />}
          <button type="button" className="tool-btn" onClick={jumpExercises} title="Jump to exercises (E)">
            <Icon name="pencil" />
            <span className="tool-label">Exercises</span>
          </button>
          <DisplayMenu />
          <button
            type="button"
            className="tool-btn"
            onClick={() => setDrawerOpen((o) => !o)}
            aria-expanded={drawerOpen}
            title="AI answers — select text in the book to ask"
          >
            <Icon name="sparkle" />
            <span className="tool-label">Ask</span>
            {entries.length > 0 && <span className="count">{entries.length}</span>}
          </button>
          <button
            type="button"
            className="icon-btn"
            onClick={() => setHelpOpen(true)}
            title="Keyboard shortcuts (?)"
            aria-label="Keyboard shortcuts"
          >
            <Icon name="help" />
          </button>
        </div>
      </header>

      <div className="reader-body">
        {sidebarOpen && <div className="backdrop" onClick={() => setSidebarOpen(false)} />}
        <nav className={`sidebar ${sidebarOpen ? "open" : ""}`} aria-label="Sections in this chapter">
          {chapter ? (
            <>
              <div className="sidebar-chapter">
                <span className="eyebrow">Chapter {chapter.number}</span>
                <div className="sidebar-chapter-title">{chapter.title}</div>
              </div>
              <ul>
                {chapter.sections.map((s) => (
                  <li key={s.id} className={s.id === current ? "current" : ""}>
                    {isOpenable(s.status) ? (
                      <Link
                        to={`/read/${s.id}`}
                        aria-current={s.id === current ? "true" : undefined}
                        onClick={(e) => {
                          if (document.getElementById(`sec-${s.id}`)) {
                            e.preventDefault();
                            goSection(s.id);
                            setSidebarOpen(false);
                          }
                        }}
                      >
                        <span className="sec-num">{s.number}</span> {s.title}
                      </Link>
                    ) : (
                      <span className="disabled-link" title={`Not readable yet (${s.status})`}>
                        <span className="sec-num">{s.number}</span> {s.title}
                      </span>
                    )}
                    {s.status !== "done" && <StatusBadge status={s.status} />}
                  </li>
                ))}
              </ul>
              <div className="sidebar-foot">
                {docId && (
                  <Link to={`/doc/${docId}`}>
                    <Icon name="book" /> Book overview
                  </Link>
                )}
                <Link to={`/quiz/chapter/${chapter.id}`}>
                  <Icon name="target" /> Chapter {chapter.number} exam
                </Link>
                {docId && (
                  <Link to={`/practice/${docId}`}>
                    <Icon name="pencil" /> Exercise-first practice
                  </Link>
                )}
              </div>
            </>
          ) : (
            <p className="muted small">Loading contents…</p>
          )}
        </nav>

        <main className="reader-main" ref={mainRef}>
          <XrefLayer root={mainRef} go={goToRef} />
          {error && (
            <div className="notice error">
              {error} <Link to={docId ? `/doc/${docId}` : "/"}>Back</Link>
            </div>
          )}
          {!error && sections.length === 0 && <p className="muted reader-loading">Loading section…</p>}

          {sections.map((s) => (
            <SectionBlock key={s.id} section={s} level={level} />
          ))}

          {nextInfo?.kind === "append" && (
            <div ref={sentinelRef} className="sentinel">
              {appendError ? (
                <p className="error">
                  {appendError}{" "}
                  <button type="button" className="link-btn" onClick={() => setAppendError(null)}>
                    retry
                  </button>
                </p>
              ) : (
                <p className="muted">{loadingMore ? "Loading next section…" : ""}</p>
              )}
            </div>
          )}
          {nextInfo?.kind === "notReady" && (
            <p className="chain-end muted">
              Next: {nextInfo.title} — still being processed. It will become readable when done.
            </p>
          )}
          {nextInfo?.kind === "chapterEnd" && (
            <div className="chain-end">
              <p className="muted">End of chapter {last?.chapter_number}.</p>
              {!nextInfo.next && crossChapter && (
                <Link className="btn" to={`/read/${crossChapter}`}>
                  Continue to next chapter →
                </Link>
              )}
              {nextInfo.next &&
                (() => {
                  const first = nextInfo.next.sections.find((x) => isOpenable(x.status));
                  return first ? (
                    <Link className="btn" to={`/read/${first.id}`}>
                      Continue to Chapter {nextInfo.next.number}: {nextInfo.next.title} →
                    </Link>
                  ) : (
                    <p className="muted small">Chapter {nextInfo.next.number} has not been processed yet.</p>
                  );
                })()}
            </div>
          )}
          {nextInfo?.kind === "bookEnd" && <p className="chain-end muted">End of the processed text.</p>}
        </main>

        {drawerOpen && (
          <AskDrawer entries={entries} onClose={() => setDrawerOpen(false)} onClear={() => setEntries([])} />
        )}
      </div>

      {snap && <AskToolbar snap={snap} onAsk={doAsk} onClose={() => setSnap(null)} />}
      {helpOpen && <ShortcutsHelp onClose={() => setHelpOpen(false)} />}
      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </div>
  );
}

const exerciseMaps = new Map<string, Promise<ExerciseMapItem[]>>();

/** Exercises of the book whose related sections include `sectionId` (exercise-first mode). */
function useRelatedExercises(docId: string, sectionId: string, enabled: boolean): ExerciseMapItem[] {
  const [items, setItems] = useState<ExerciseMapItem[]>([]);
  useEffect(() => {
    if (!enabled || MOCK) return;
    if (!exerciseMaps.has(docId)) exerciseMaps.set(docId, getExerciseMap(docId).catch(() => []));
    let alive = true;
    exerciseMaps.get(docId)!.then((all) => alive && setItems(all.filter((e) => e.related_section_ids.includes(sectionId))));
    return () => {
      alive = false;
    };
  }, [docId, sectionId, enabled]);
  return enabled ? items : [];
}

function SectionBlock({ section, level }: { section: SectionView; level: Level }) {
  const units = useMemo(() => section.units.filter((u) => u.min_level <= level), [section.units, level]);
  const hidden = section.units.length - units.length;
  const { exerciseFirst } = usePrefs();
  const tryFirst = useRelatedExercises(section.document_id, section.id, exerciseFirst && section.exercises.length === 0);
  return (
    <section className="reader-section" id={`sec-${section.id}`} data-section-id={section.id}>
      <span className="eyebrow section-eyebrow">
        Chapter {section.chapter_number} · Section {section.number}
      </span>
      <h2 className="section-title">
        {section.title}
        {section.status !== "done" && <StatusBadge status={section.status} />}
      </h2>
      {section.status === "partial" && (
        <p className="notice small">
          Partially processed: some passages are shown verbatim or are not checked yet. “Fix with the model” in the book's Quality check re-runs only those parts; “source” on any passage shows the scan.
        </p>
      )}
      {tryFirst.length > 0 && (
        <div className="try-first">
          <h3>Try first</h3>
          <p className="small muted">
            Exercise-first mode: attempt one of these before reading; come back to the text when you are stuck.
          </p>
          <ul>
            {tryFirst.slice(0, 4).map((e) => (
              <li key={e.id}>
                <Link to={`/exercise/${e.id}`}>Exercise {e.number}</Link>
                <span className="small">
                  {" "}
                  — <Md src={excerpt(e.statement_md, 160)} />
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {section.summary_md && <SectionSummary md={section.summary_md} />}
      {units.map((u) => (
        <UnitView key={u.id} unit={u} level={level} sectionId={section.id} />
      ))}
      {hidden > 0 && (
        <p className="hidden-note muted small">
          {hidden} passage{hidden === 1 ? "" : "s"} hidden at this level.
        </p>
      )}
      {section.exercises.length > 0 && <ExercisesBlock section={section} />}
      {section.exercises.length === 0 && section.verified && (
        <p className="test-yourself">
          <Link className="btn" to={`/quiz/section/${section.id}`}>
            <Icon name="target" /> Test yourself on §{section.number}
          </Link>
          <span className="muted">Six questions written from the checked text, graded instantly.</span>
        </p>
      )}
    </section>
  );
}

function ExercisesBlock({ section }: { section: SectionView }) {
  return (
    <div className="exercises-block" id={`exercises-${section.id}`} data-section-id={section.id}>
      <h3>Exercises</h3>
      <ol className="exercise-list">
        {section.exercises.map((ex) => {
          const mark = load<ExerciseMark>(exKey(ex.id, "mark"), { completed: false, difficult: false });
          return (
            <li key={ex.id} className="exercise-item">
              <div className="exercise-head">
                <strong>Exercise {ex.number}</strong>
                <span className="muted small">p. {ex.page}</span>
                {mark.completed && <span className="tag tag-done">✓ completed</span>}
                {mark.difficult && <span className="tag tag-hard">difficult</span>}
                <Link to={`/exercise/${ex.id}`} className="exercise-open">
                  Open workspace →
                </Link>
              </div>
              <Md src={ex.statement_md} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** "Aa" menu: text width, theme, exercise-first mode. */
function DisplayMenu() {
  const { width, setWidth, effectiveTheme, toggleTheme, exerciseFirst, setExerciseFirst } = usePrefs();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="popover-wrap" ref={ref}>
      <button
        type="button"
        className="icon-btn"
        aria-expanded={open}
        aria-label="Display settings"
        title="Display settings"
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="type" />
      </button>
      {open && (
        <div className="popover" role="dialog" aria-label="Display settings">
          <h4>Text width</h4>
          <div className="seg">
            {WIDTHS.map((x) => (
              <button key={x.w} type="button" className={width === x.w ? "active" : ""} onClick={() => setWidth(x.w)}>
                {x.label}
              </button>
            ))}
          </div>
          <h4>Theme</h4>
          <div className="seg">
            {(["light", "dark"] as const).map((t) => (
              <button
                key={t}
                type="button"
                className={effectiveTheme === t ? "active" : ""}
                onClick={() => effectiveTheme !== t && toggleTheme()}
              >
                {t === "light" ? "Light" : "Dark"}
              </button>
            ))}
          </div>
          <label className="switch-row">
            <span>
              Exercise-first mode
              <span className="muted">Suggest related exercises before each section</span>
            </span>
            <input type="checkbox" checked={exerciseFirst} onChange={(e) => setExerciseFirst(e.target.checked)} />
          </label>
          <div className="menu-machine">
            <h4>Computing machine</h4>
            <MachinePicker />
          </div>
          <hr className="divider" />
          <Link to="/settings" className="small">
            AI model settings →
          </Link>
        </div>
      )}
    </div>
  );
}

/** AI section digest, clamped to three lines until expanded. */
function SectionSummary({ md }: { md: string }) {
  const [open, setOpen] = useState(false);
  const long = md.length > 260;
  return (
    <div className="section-summary">
      <div className="summary-head">
        <span className="eyebrow">In this section</span>
        <span className="ai-tag" title="Written by AI from the section text">
          AI summary
        </span>
      </div>
      <div className={`summary-body${long && !open ? " clamped" : ""}`}>
        <Md src={md} />
      </div>
      {long && (
        <button type="button" className="link-btn summary-toggle" onClick={() => setOpen((o) => !o)}>
          {open ? "Show less" : "Read full summary"}
        </button>
      )}
    </div>
  );
}
