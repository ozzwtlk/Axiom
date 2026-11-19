import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { gradeQuiz, makeQuiz } from "../api/client";
import { ModelPicker } from "../components/ModelPicker";
import { useModelChoice } from "../lib/models";
import type { Quiz, QuizResult } from "../api/types";
import { Md, MdInline } from "../components/Md";

const LETTERS = "ABCD";

/** Mastery check: /quiz/section/<sectionId> or /quiz/chapter/<chapterId>  (?v=N for another question set). */
export function QuizPage() {
  const { scope = "section", id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const variant = Number(params.get("v") ?? 0);
  const [quiz, setQuiz] = useState<Quiz | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, number | string>>({});
  const [result, setResult] = useState<QuizResult | null>(null);
  const [grading, setGrading] = useState(false);
  const [model] = useModelChoice("quiz");

  useEffect(() => {
    setQuiz(null);
    setResult(null);
    setAnswers({});
    setErr(null);
    makeQuiz(scope as "section" | "chapter", id, variant, scope === "chapter" ? 10 : 6, model)
      .then(setQuiz)
      .catch((e) => setErr(`Could not build the quiz: ${(e as Error).message}`));
  }, [scope, id, variant, model]);

  const submit = async () => {
    if (!quiz) return;
    setGrading(true);
    try {
      setResult(await gradeQuiz(quiz.id, answers, model));
    } catch (e) {
      setErr(`Grading failed: ${(e as Error).message}`);
    } finally {
      setGrading(false);
    }
  };

  const res = (qid: string) => result?.results.find((r) => r.id === qid);
  const docId = id.split("-")[0];

  return (
    <main className="page quiz-page">
      <nav className="crumbs">
        <Link to={`/doc/${docId}`}>Document</Link>
        {scope === "section" && (
          <>
            {" · "}
            <Link to={`/read/${id}`}>Back to the section</Link>
          </>
        )}
      </nav>
      <div className="page-head">
        <h1>{scope === "chapter" ? "Chapter exam" : "Mastery check"}</h1>
        <ModelPicker group="quiz" compact />
      </div>
      {err && <p className="error">{err}</p>}
      {!quiz && !err && (
        <p className="muted">
          Writing questions from the checked text and answering each one blind to weed out bad ones… (a few minutes the
          first time, instant afterwards)
        </p>
      )}
      {quiz && (
        <>
          <p>
            <strong>{quiz.title}</strong> · {quiz.questions.length} questions{" "}
            <span className="ai-tag" title="Questions written by AI from the verified text; each cites its source">
              AI-written
            </span>
            {quiz.dropped_by_blind_check > 0 && (
              <span className="muted small"> · {quiz.dropped_by_blind_check} question(s) discarded by the blind check</span>
            )}
          </p>
          {quiz.questions.map((q, i) => {
            const r = res(q.id);
            return (
              <div key={q.id} className={`quiz-q${r ? (r.score >= 1 ? " q-right" : " q-wrong") : ""}`}>
                <div>
                  <strong>{i + 1}.</strong> <MdInline src={q.prompt_md} />
                </div>
                {q.type === "mcq" ? (
                  q.options.map((o, k) => (
                    <label key={k} className={`quiz-opt${r && r.correct === k ? " opt-key" : ""}`}>
                      <input
                        type="radio"
                        name={q.id}
                        disabled={!!result}
                        checked={answers[q.id] === k}
                        onChange={() => setAnswers({ ...answers, [q.id]: k })}
                      />
                      <span>{LETTERS[k]}.</span> <MdInline src={o} />
                    </label>
                  ))
                ) : (
                  <textarea
                    className="quiz-short"
                    disabled={!!result}
                    value={(answers[q.id] as string) ?? ""}
                    onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                    placeholder="Your answer ($math$ is fine)"
                  />
                )}
                {r && (
                  <div className="small">
                    <p>
                      Score {r.score} · {q.type === "short" && <>model answer: </>}
                      {q.type === "short" && <MdInline src={r.answer_md} />}
                    </p>
                    {r.feedback_md && <Md src={r.feedback_md} />}
                    {r.explanation_md && <Md src={r.explanation_md} />}
                    <p className="muted">
                      Source:{" "}
                      {r.source_units.map((u) => (
                        <Link key={u} to={`/read/${u.replace(/-u\d+$/, "")}#unit-${u}`}>
                          {u.replace(/^.*-s/, "§").replace(/-u0*/, " #")}{" "}
                        </Link>
                      ))}
                    </p>
                  </div>
                )}
              </div>
            );
          })}
          {!result ? (
            <button type="button" className="btn" onClick={submit} disabled={grading}>
              {grading ? "Grading…" : "Submit answers"}
            </button>
          ) : (
            <p>
              <span className="score-big">{Math.round(result.score * 100)}%</span>{" "}
              <button type="button" className="link-btn" onClick={() => setParams({ v: String(variant + 1) })}>
                try a new set of questions
              </button>
            </p>
          )}
        </>
      )}
    </main>
  );
}
