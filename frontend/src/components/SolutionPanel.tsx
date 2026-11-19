import { useState } from "react";
import { ApiError, checkAnswer, generateSolution, getSolution } from "../api/client";
import type { AnswerCheck, Solution, SolutionPart } from "../api/types";
import { Md } from "./Md";
import { ModelPicker } from "./ModelPicker";
import { getModelChoice } from "../lib/models";

const EVIDENCE: Record<Solution["status"], string> = {
  consistent: "two independent AI solutions agree",
  inconsistent: "two independent AI solutions DISAGREE: at least one is wrong",
  not_comparable: "open-ended: the two AI solutions could not be compared mechanically",
};

const VERDICT: Record<AnswerCheck["verdict"], string> = {
  correct: "Looks correct",
  partially_correct: "Partly correct",
  incorrect: "Not correct yet",
  cannot_judge: "Can't judge reliably",
};

function Parts({ parts }: { parts: SolutionPart[] }) {
  return (
    <>
      {parts.map((p) => (
        <div key={p.label} className="sol-part">
          {parts.length > 1 && <strong>({p.label})</strong>}
          <Md src={p.solution_md} />
          <div className="final-answer">
            <span className="muted small">Final answer: </span>
            <Md src={p.final_answer_md} />
          </div>
        </div>
      ))}
    </>
  );
}

/** Answer checking (against the scratchpad) and the worked solution, which is hidden until asked for twice. */
export function SolutionPanel({ exerciseId, answer }: { exerciseId: string; answer: string }) {
  const [check, setCheck] = useState<AnswerCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [sol, setSol] = useState<Solution | null>(null);
  const [solState, setSolState] = useState<"hidden" | "confirm" | "loading" | "missing" | "generating" | "shown">("hidden");
  const [err, setErr] = useState<string | null>(null);

  const runCheck = async () => {
    setChecking(true);
    setErr(null);
    try {
      setCheck(await checkAnswer(exerciseId, answer, getModelChoice("check")));
    } catch (e) {
      setErr(`Check failed: ${(e as Error).message}`);
    } finally {
      setChecking(false);
    }
  };

  const reveal = async () => {
    setSolState("loading");
    try {
      setSol(await getSolution(exerciseId));
      setSolState("shown");
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setSolState("missing");
      else (setErr(String(e)), setSolState("hidden"));
    }
  };

  const generate = async () => {
    setSolState("generating");
    try {
      setSol(await generateSolution(exerciseId, getModelChoice("solve")));
      setSolState("shown");
    } catch (e) {
      setErr(`Solution failed: ${(e as Error).message}`);
      setSolState("missing");
    }
  };

  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h3>Check my answer</h3>
          <ModelPicker group="check" compact />
        </div>
        <p className="small muted">
          An AI grader compares your work with a reference solution and points to the first error without writing out
          the solution. The first check also writes the reference solution, which takes a while.
        </p>
        <button type="button" className="btn" disabled={checking || !answer.trim()} onClick={runCheck}>
          {checking ? "Checking…" : "Check my work"}
        </button>
        {check && (
          <div className="check-result">
            <p>
              <strong className={`verdict-${check.verdict}`}>{VERDICT[check.verdict]}</strong>{" "}
              <span className="ai-tag">AI judgement</span> <span className="small muted">· {check.model}</span>
              {check.reference_status === "inconsistent" && (
                <span className="small evidence evidence-inconsistent"> reference solutions disagree</span>
              )}
            </p>
            <Md src={check.feedback_md} />
            {check.first_error_md && (
              <>
                <p className="small muted">First error:</p>
                <Md src={check.first_error_md} />
              </>
            )}
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>Worked solution</h3>
          <ModelPicker group="solve" compact />
        </div>
        {err && <p className="error">{err}</p>}
        {solState === "hidden" && (
          <button type="button" className="link-btn" onClick={() => setSolState("confirm")}>
            show the solution…
          </button>
        )}
        {solState === "confirm" && (
          <p>
            Seeing the solution ends this exercise as practice. Sure?{" "}
            <button type="button" className="btn" onClick={reveal}>
              Yes, show it
            </button>{" "}
            <button type="button" className="link-btn" onClick={() => setSolState("hidden")}>
              not yet
            </button>
          </p>
        )}
        {solState === "loading" && <p className="muted">Loading…</p>}
        {solState === "missing" && (
          <p>
            No solution yet.{" "}
            <button type="button" className="btn" onClick={generate}>
              Generate (two independent solutions + comparison)
            </button>
          </p>
        )}
        {solState === "generating" && <p className="muted">Solving twice independently and comparing…</p>}
        {solState === "shown" && sol && (
          <>
            <p>
              <span className="ai-tag">AI-generated</span>{" "}
              <span className={`evidence evidence-${sol.status}`}>{EVIDENCE[sol.status]}</span>{" "}
              <span className="small muted">· {sol.model}</span>
            </p>
            <Parts parts={sol.solution.parts} />
            {sol.comparison.length > 0 && (
              <details>
                <summary className="small">How the two solutions compare</summary>
                <ul className="small">
                  {sol.comparison.map((c) => (
                    <li key={c.label}>
                      ({c.label}) {c.status}: <Md src={c.explanation} />
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {sol.alternative && (
              <details open>
                <summary>The other solution (disagrees)</summary>
                <Parts parts={sol.alternative.parts} />
              </details>
            )}
          </>
        )}
      </section>
    </>
  );
}
