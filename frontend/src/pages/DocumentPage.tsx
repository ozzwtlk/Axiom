import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ankiUrl, blockImageUrl, coverUrl, getDocument, getMastery, getProgress, getStudyCards, MOCK, pageImageUrl, processDocument, pushToAnki, queueAnki, queueCover, restructureDocument } from "../api/client";
import { Md } from "../components/Md";
import { ModelPicker } from "../components/ModelPicker";
import type { DocumentDetail, Profile, ProgressReport } from "../api/types";
import { jobsRunning, ProgressPanel } from "../components/ProgressPanel";
import { ProcessedCheck, ReadingPercent, ReadingStatus } from "../components/ReadingStatus";
import { loadReading, summarizeReading } from "../lib/reading";
import { QualityPanel } from "../components/QualityPanel";
import { Icon } from "../components/Icon";
import { SearchBox } from "../components/SearchBox";
import { isOpenable, StatusBadge } from "../components/StatusBadge";
import { getModelChoice, useModelsReport } from "../lib/models";
import { load, loadPosition, save } from "../lib/storage";

export function DocumentPage() {
  const { docId = "" } = useParams();
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [progress, setProgress] = useState<ProgressReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [profile, setProfile] = useState<Profile>("balanced");
  const [procModel, setProcModel] = useState<string>(() => load<string>("axiom.processModel", "local"));
  const report = useModelsReport();
  const known = (report?.models ?? []).filter((m) => m.vision && m.kind !== "api");
  // no machine connected (server mode): still let the user queue a run for Qwen or Claude
  const visionModels = known.length ? known : [
    { id: "local", label: "Local model", kind: "local", available: true },
    { id: "claude-code:haiku", label: "Claude Code · Haiku", kind: "subscription", available: true },
  ];
  const [parallel, setParallel] = useState<boolean>(() => load<boolean>("axiom.processParallel", false));
  const premiumRun = procModel !== "auto" && procModel !== "local";
  const [submitting, setSubmitting] = useState(false);
  const [coverSubmitting, setCoverSubmitting] = useState(false);
  const [kick, setKick] = useState(0); // forces polling right after submitting
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [mastery, setMastery] = useState<Record<string, number>>({});
  useEffect(() => {
    if (docId) getMastery(docId).then(setMastery).catch(() => undefined);
  }, [docId]);
  const pos = loadPosition(docId);

  const refresh = useCallback(async () => {
    try {
      const [d, p] = await Promise.all([getDocument(docId), getProgress(docId).catch(() => null)]);
      setDoc(d);
      setProgress(p);
      setErr(null);
      return { d, p };
    } catch (e) {
      setErr(`Could not load document: ${(e as Error).message ?? e}`);
      return null;
    }
  }, [docId]);

  const sectionsRunning = !!doc?.chapters.some(
    (c) => c.status === "running" || c.sections.some((s) => s.status === "running"),
  );
  const polling = jobsRunning(progress) || sectionsRunning || kick > 0;

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!polling) return;
    const t = setInterval(async () => {
      const r = await refresh();
      if (kick > 0 && r && !jobsRunning(r.p)) setKick((k) => Math.max(0, k - 1));
    }, 3000);
    return () => clearInterval(t);
  }, [polling, refresh, kick]);

  const toggle = (n: number) =>
    setSelected((s) => {
      const c = new Set(s);
      c.has(n) ? c.delete(n) : c.add(n);
      return c;
    });

  const onProcess = async () => {
    if (!selected.size) return;
    setSubmitting(true);
    try {
      await processDocument(docId, {
        chapters: [...selected].sort((a, b) => a - b),
        profile,
        ...(procModel !== "auto" ? { model: procModel } : {}),
        ...(premiumRun && parallel ? { parallel: true } : {}),
      });
      setSelected(new Set());
      setKick(3); // keep polling for a few ticks even if the job list is not populated yet
      await refresh();
    } catch (e) {
      setErr(`Could not start processing: ${(e as Error).message ?? e}`);
    } finally {
      setSubmitting(false);
    }
  };

  if (err && !doc) return <main className="page"><p className="error">{err}</p><Link to="/">← Library</Link></main>;
  if (!doc) return <main className="page"><p className="muted">Loading…</p></main>;

  const reading = loadReading(doc.id);
  if (pos) reading[pos.sectionId] ??= 0;
  const readingIds = new Set(doc.reading_section_ids ?? []);
  const bookReading = summarizeReading([...readingIds], reading);
  const firstOpen = doc.chapters.flatMap((c) => c.sections).find((s) => isOpenable(s.status));
  const pct = doc.chapters_total ? Math.round((100 * doc.chapters_done) / doc.chapters_total) : 0;
  const unprocessed = doc.chapters.filter((c) => !c.completed).map((c) => c.number);
  const coverJob = progress?.jobs.filter((j) => j.kind === "cover").at(-1);
  const coverPending = coverSubmitting || coverJob?.status === "pending" || coverJob?.status === "running";
  const onStarted = () => {
    setKick(3);
    return refresh();
  };

  return (
    <main className="page page-wide">
      <nav className="crumbs">
        <Link to="/">Library</Link> <span aria-hidden="true">/</span> <span>{doc.title.split(":")[0]}</span>
      </nav>
      <section className="doc-hero">
        <Cover docId={doc.id} title={doc.title} revision={doc.cover_revision} />
        <div>
          <span className="eyebrow">Textbook</span>
          <h1>{doc.title}</h1>
          <div className="doc-hero-meta">
            {doc.page_count} pages · {doc.chapters.length} chapters ·{" "}
            <button
              type="button"
              className="link-btn"
              title="Read the chapters and sections from the PDF outline again (only before processing starts)"
              onClick={async () => {
                setErr(null);
                try {
                  const r = await restructureDocument(doc.id);
                  await refresh();
                  if (!r.changed) setErr("The PDF has no usable outline; the document stays one section.");
                } catch (e) {
                  setErr(e instanceof Error ? e.message : String(e));
                }
              }}
            >
              re-read outline
            </button>
          </div>
          <div className="doc-hero-progress">
            <div className="bar" aria-label={`${doc.chapters_done} of ${doc.chapters_total} chapters ready`}>
              <div style={{ width: `${pct}%` }} />
            </div>
            <span>
              {doc.chapters_done} of {doc.chapters_total} chapters ready
              {doc.completed && <> · <ProcessedCheck /></>}
            </span>
          </div>
          {!!readingIds.size && <div className="reading-line">
            <ReadingPercent percent={bookReading.percent} />
            {bookReading.touched && <ReadingStatus finished={bookReading.finished} />}
          </div>}
          <div className="doc-actions">
            {pos ? (
              <Link className="btn" to={`/read/${pos.sectionId}`} state={{ unitId: pos.unitId }}>
                Continue reading <Icon name="arrowRight" />
              </Link>
            ) : (
              firstOpen && (
                <Link className="btn" to={`/read/${firstOpen.id}`}>
                  Start reading <Icon name="arrowRight" />
                </Link>
              )
            )}
            <SearchBox docId={doc.id} />
            {!MOCK && <button type="button" className="btn btn-quiet" disabled={coverPending} onClick={async () => {
              setCoverSubmitting(true);
              setErr(null);
              try {
                await queueCover(doc.id);
                await onStarted();
              } catch (e) {
                setErr((e as Error).message);
              } finally {
                setCoverSubmitting(false);
              }
            }}>{coverPending ? "Cover queued or running…" : `${doc.cover_revision ? "Regenerate" : "Generate"} cover with Codex`}</button>}
          </div>
          {coverJob?.status === "failed" && <p className="small error">Cover generation failed. <Link to="/jobs">See Jobs</Link>.</p>}
        </div>
      </section>
      {err && <p className="error">{err}</p>}

      <div className="doc-grid">
        <section className="toc" aria-label="Contents">
          <div className="toc-head">
            <h2>Contents</h2>
            {unprocessed.length > 0 && (
              <button
                type="button"
                className="link-btn small"
                onClick={() =>
                  setSelected(selected.size === unprocessed.length ? new Set() : new Set(unprocessed))
                }
              >
                {selected.size === unprocessed.length ? "Clear selection" : "Select all unprocessed"}
              </button>
            )}
          </div>

          <ol className="chapters">
            {doc.chapters.map((c) => {
              const isCollapsed = collapsed.has(c.number);
              const ready = isOpenable(c.status);
              const chapterReadingIds = c.sections.filter((s) => readingIds.has(s.id)).map((s) => s.id);
              return (
                <li key={c.id} className={`chapter ${ready ? "is-done" : "is-pending"}`}>
                  <div className="chapter-row">
                    <span className="chapter-num">{c.number}</span>
                    <button
                      type="button"
                      className="chapter-toggle"
                      aria-expanded={!isCollapsed}
                      onClick={() =>
                        setCollapsed((s) => {
                          const n = new Set(s);
                          n.has(c.number) ? n.delete(c.number) : n.add(c.number);
                          return n;
                        })
                      }
                    >
                      <span className="chapter-title">{c.title}</span>
                      <span className="chapter-sub">
                        pp. {c.page_start}–{c.page_end} · {c.sections.length} sections
                      </span>
                    </button>
                    <div className="chapter-actions">
                      {c.status !== "done" ? <StatusBadge status={c.status} /> : c.completed && <ProcessedCheck />}
                      {!!chapterReadingIds.length && <ReadingPercent percent={summarizeReading(chapterReadingIds, reading).percent} />}
                      {ready && (
                        <Link className="btn btn-quiet btn-sm" to={`/quiz/chapter/${c.id}`} title="Exam on this chapter">
                          <Icon name="target" /> Chapter exam
                        </Link>
                      )}
                      {!c.completed && (
                        <label className="chapter-check" title="Select for processing">
                          <input
                            type="checkbox"
                            aria-label={`Select chapter ${c.number} for processing`}
                            checked={selected.has(c.number)}
                            onChange={() => toggle(c.number)}
                          />
                          process
                        </label>
                      )}
                      <span className="chevron" aria-hidden="true">
                        <Icon name="chevron" />
                      </span>
                    </div>
                  </div>
                  {!isCollapsed && (
                    <ul className="sections">
                      {c.sections.map((s) => (
                        <li key={s.id} className="section-row">
                          {isOpenable(s.status) ? (
                            <Link className="sec-link" to={`/read/${s.id}`}>
                              <span className="sec-num">{s.number}</span>
                              <span>{s.title}</span>
                              <span className="leader" />
                            </Link>
                          ) : (
                            <span className="sec-link disabled-link" title="Not processed yet">
                              <span className="sec-num">{s.number}</span>
                              <span>{s.title}</span>
                              <span className="leader" />
                            </span>
                          )}
                          {s.is_exercises && <span className="tag">exercises</span>}
                          {s.status !== "done" ? <StatusBadge status={s.status} /> : s.completed && <ProcessedCheck />}
                          {readingIds.has(s.id) && <ReadingPercent percent={reading[s.id] ?? 0} />}
                          {mastery[s.id] !== undefined && (
                            <span className="mastery-bar" title={`Mastery: best ${Math.round(mastery[s.id] * 100)}% in checks`}>
                              <span style={{ width: `${Math.round(mastery[s.id] * 100)}%` }} />
                            </span>
                          )}
                          <span className="page-no">{s.page_start}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ol>
        </section>

        <div className="doc-side">
          <StudyTools key={doc.id} doc={doc} progress={progress} onStarted={onStarted} />
          {(unprocessed.length > 0 || selected.size > 0) && (
            <section className="card side-card" aria-label="Process chapters">
              <h3>Process chapters</h3>
              <p className="small muted">
                {selected.size
                  ? `${selected.size} chapter${selected.size === 1 ? "" : "s"} selected.`
                  : "Tick “process” on the chapters you want to read next."}
              </p>
              <div className="process-bar">
                <label className="field">
                  Model
                  <select
                    value={procModel}
                    onChange={(e) => {
                      setProcModel(e.target.value);
                      save("axiom.processModel", e.target.value);
                    }}
                  >
                    {visionModels.map((m) => (
                      <option key={m.id} value={m.id} disabled={!m.available}>
                        {m.kind === "local" ? "Qwen (local, free)" : `${m.label.replace("Claude Code · ", "Claude ")} (subscription)`}
                        {m.available ? "" : " — not available"}
                      </option>
                    ))}
                    <option value="auto">As in AI model settings</option>
                  </select>
                </label>
                <label className="field">
                  Quality profile
                  <select value={profile} onChange={(e) => setProfile(e.target.value as Profile)}>
                    <option value="economy">Economy (local only)</option>
                    <option value="balanced">Balanced</option>
                    <option value="max">Max quality</option>
                  </select>
                </label>
                <label className="field checkbox">
                  <input
                    type="checkbox"
                    checked={premiumRun && parallel}
                    disabled={!premiumRun}
                    onChange={(e) => {
                      setParallel(e.target.checked);
                      save("axiom.processParallel", e.target.checked);
                    }}
                  />
                  Process in parallel
                  <span className="small muted">
                    {premiumRun
                      ? " — chapters and sections run concurrently; afterwards a notation check re-writes sections that contradict earlier chapters"
                      : " — needs a premium model (Qwen has one GPU)"}
                  </span>
                </label>
                <p className="small muted process-note">
                  {procModel.startsWith("claude-code")
                    ? `Fast (≈ 30–60 min per chapter), but uses ≈ 700–800 calls per chapter from your Claude plan. If the plan's limit is hit, the run continues on Qwen.`
                    : procModel === "local"
                      ? "Free and private; ≈ 3–5 hours of GPU time per chapter."
                      : "Uses the per-task models from the AI models page."}
                </p>
                <button type="button" className="btn" disabled={!selected.size || submitting} onClick={onProcess}>
                  {submitting ? "Starting…" : `Process ${selected.size || ""} chapter${selected.size === 1 ? "" : "s"}`}
                </button>
              </div>
            </section>
          )}
          <QualityPanel
            docId={doc.id}
            model={procModel}
            profile={profile}
            tick={(progress?.sections_done ?? 0) * 1000 + (progress?.jobs.filter((j) => j.status === "done").length ?? 0)}
            onStarted={() => {
              setKick(3);
              refresh();
            }}
          />
          {progress && <ProgressPanel p={progress} polling={polling} />}
        </div>
      </div>
    </main>
  );
}

/** Book cover from the scan's first page (falls back to the title). */
export function Cover({ docId, title, revision }: { docId: string; title: string; revision?: string | null }) {
  const [failed, setFailed] = useState<string[]>([]);
  const src = [revision ? coverUrl(docId, revision) : null, pageImageUrl(docId, 1)]
    .find((url): url is string => url !== null && !failed.includes(url));
  return (
    <div className="book-cover">
      {src && !MOCK ? (
        <img key={src} src={src} alt="" loading="lazy" onError={() => setFailed((urls) => [...urls, src])} />
      ) : (
        <div className="book-cover-fallback">{title}</div>
      )}
    </div>
  );
}

function StudyTools({ doc, progress, onStarted }: { doc: DocumentDetail; progress: ProgressReport | null; onStarted: () => Promise<unknown> }) {
  const [deck, setDeck] = useState(() => `${doc.title.split(":")[0].slice(0, 40) || "AXIOM"} · Study`);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cards, setCards] = useState<Awaited<ReturnType<typeof getStudyCards>>>([]);
  const jobs = progress?.jobs.filter((j) => j.kind === "anki") ?? [];
  const active = jobs.filter((j) => j.status === "pending" || j.status === "running").length;
  const finished = jobs.filter((j) => j.status === "done").map((j) => `${j.id}:${j.updated_at}`).join();
  useEffect(() => {
    let current = true;
    getStudyCards(doc.id).then((c) => { if (current) setCards(c); }).catch((e) => { if (current) setMsg(e.message); });
    return () => { current = false; };
  }, [doc.id, finished]);
  const generate = async (regenerate = false) => {
    setBusy(true);
    setMsg(null);
    try {
      const { job_ids } = await queueAnki(doc.id, regenerate, getModelChoice("anki"));
      if (!job_ids.length) setMsg("No new verified sections to generate from.");
      await onStarted();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const push = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await pushToAnki(doc.id, deck);
      setMsg(`Anki: ${r.added} added, ${r.updated} updated in “${r.deck}”.`);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card side-card study-tools" aria-label="Study tools">
      <h3>Study</h3>
      <Link className="side-link" to={`/practice/${doc.id}`}>
        <span className="icon-box"><Icon name="pencil" /></span>
        <span>
          <strong>Exercise-first practice</strong>
          <span className="muted">Attempt problems, read when stuck</span>
        </span>
      </Link>
      <hr className="divider" />
      <div className="side-link" style={{ cursor: "default" }}>
        <span className="icon-box"><Icon name="cards" /></span>
        <span>
          <strong>Anki flashcards</strong>
          <span className="muted">Standalone reasoning, ansatz, short derivations and source figures.</span>
        </span>
      </div>
      {!MOCK && <>
        <ModelPicker group="anki" label="Model for Anki generation" />
        <div className="export-row">
          <button type="button" className="btn btn-sm" disabled={busy || active > 0} onClick={() => generate()}>
            {active ? `${active} sections queued or running` : cards.length ? "Add newly verified sections" : "Generate study cards"}
          </button>
          {!!finished && !active && <button type="button" className="link-btn small" disabled={busy} onClick={() => generate(true)}>Rebuild</button>}
        </div>
      </>}
      {jobs.some((j) => j.status === "failed") && <p className="small error">Some card jobs failed. <Link to="/jobs">See Jobs</Link>.</p>}
      <p className="small muted">AI-generated and model-reviewed. Up to about one card per two pages, based on chapter length and available verified material.</p>
      {!!cards.length && <details className="study-preview">
        <summary>Preview ({cards.length} cards)</summary>
        {cards.slice(0, 5).map((card) => <details key={card.guid}>
          <summary>{card.front.split("\n")[0].replace(/\$+$/, "").slice(0, 110)}{card.front.length > 110 ? "…" : ""}</summary>
          <Md src={card.front} />
          {card.figure && <img src={blockImageUrl(card.figure)} alt="Source figure" loading="lazy" />}
          <Md src={card.back} />
          <small className="muted">{card.source}</small>
        </details>)}
      </details>}
      {!!cards.length && <><label className="field">
        Deck name
        <input type="text" value={deck} onChange={(e) => setDeck(e.target.value)} />
      </label>
      <div className="export-row">
        <a className="btn btn-sm" href={ankiUrl(doc.id, "apkg", deck)} download>
          Download .apkg
        </a>
        {cards.some((c) => !c.figure) && <a className="btn btn-quiet btn-sm" href={ankiUrl(doc.id, "tsv", deck)} download>
          .txt (no figures)
        </a>}
        <button type="button" className="link-btn small" disabled={busy} onClick={push} title="Needs Anki running with the AnkiConnect add-on">
          {busy ? "Sending…" : "Send to Anki"}
        </button>
      </div></>}
      {msg && <p className="small" role="status">{msg}</p>}
    </section>
  );
}
