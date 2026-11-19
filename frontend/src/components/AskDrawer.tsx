import type { AskAction, AskResponse } from "../api/types";
import { Md } from "./Md";
import { ModelPicker } from "./ModelPicker";

export interface AskEntry {
  id: number;
  action: AskAction;
  question?: string;
  selection: string;
  blockIds: string[];
  status: "loading" | "done" | "error";
  response?: AskResponse;
  error?: string;
}

export function AskDrawer({ entries, onClose, onClear }: { entries: AskEntry[]; onClose: () => void; onClear: () => void }) {
  return (
    <aside className="drawer drawer-right" aria-label="AI answers">
      <div className="drawer-head">
        <h2>AI answers</h2>
        <div>
          <ModelPicker group="ask" compact />
          {entries.length > 0 && (
            <button type="button" className="link-btn" onClick={onClear}>
              clear
            </button>
          )}
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close answers">
            ×
          </button>
        </div>
      </div>
      <p className="ai-disclaimer">
        Answers are <strong>AI-generated</strong> and may be wrong. The book text and original scan are the reference.
      </p>
      {entries.length === 0 && <p className="muted">Select text in the reader and choose an action.</p>}
      {entries.map((e) => (
        <article className="ask-entry" key={e.id}>
          <div className="ask-entry-q">
            <span className="ask-action">{e.action === "custom" ? "Question" : e.action}</span>
            {e.question && <div className="ask-question">{e.question}</div>}
            <blockquote>{e.selection.length > 280 ? e.selection.slice(0, 280) + "…" : e.selection}</blockquote>
          </div>
          {e.status === "loading" && <p className="muted">Thinking…</p>}
          {e.status === "error" && <p className="error">{e.error}</p>}
          {e.response && (
            <>
              <div className="ask-answer">
                <span className="ai-tag">AI-generated</span>
                <Md src={e.response.answer_md} />
              </div>
              <div className="ask-meta">
                <span>model: {e.response.model}</span>
                {e.response.cached && <span className="cached-tag">cached</span>}
              </div>
              {e.response.context_used.length > 0 && (
                <details className="ask-context">
                  <summary>Context used ({e.response.context_used.length})</summary>
                  <ul>
                    {e.response.context_used.map((c, i) => (
                      <li key={i}>
                        <span className="muted">{c.kind}</span> {c.title} <code className="small">{c.ref}</code>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          )}
        </article>
      ))}
    </aside>
  );
}
