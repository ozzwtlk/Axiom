import { useEffect, useState } from "react";
import { blockImageUrl, getBlock } from "../api/client";
import type { SourceBlock, UnitSource } from "../api/types";

/** "Original" panel: scan crops + raw OCR text for each source block of a unit. */
export function SourcePanel({ source }: { source: UnitSource }) {
  const [blocks, setBlocks] = useState<Record<string, SourceBlock | string>>({});

  useEffect(() => {
    let cancelled = false;
    for (const id of source.block_ids) {
      getBlock(id)
        .then((b) => !cancelled && setBlocks((m) => ({ ...m, [id]: b })))
        .catch((e) => !cancelled && setBlocks((m) => ({ ...m, [id]: `Could not load block: ${e.message ?? e}` })));
    }
    return () => {
      cancelled = true;
    };
  }, [source.block_ids]);

  return (
    <div className="source-panel">
      <div className="source-panel-head">
        Original scan · p. {source.pages.join(", ") || "?"} · coverage <strong>{source.coverage}</strong>
        {source.note && <span className="muted"> — {source.note}</span>}
      </div>
      {source.block_ids.length === 0 && <p className="muted small">No source blocks recorded for this unit.</p>}
      {source.block_ids.map((id) => {
        const b = blocks[id];
        return (
          <div className="source-block" key={id}>
            <img src={blockImageUrl(id)} alt={`Scan crop of source block ${id}`} loading="lazy" />
            {b === undefined && <p className="muted small">Loading OCR text…</p>}
            {typeof b === "string" && <p className="error small">{b}</p>}
            {b && typeof b !== "string" && (
              <details className="source-raw" open>
                <summary>
                  Raw text · {b.transcription_method.replace("_", " ")} · confidence {b.confidence.toFixed(2)}
                  {b.eq_number && <> · eq. ({b.eq_number})</>}
                </summary>
                <pre>{b.raw_text}</pre>
                {b.transcribed_md && (
                  <>
                    <div className="muted small">Transcription:</div>
                    <pre>{b.transcribed_md}</pre>
                  </>
                )}
              </details>
            )}
          </div>
        );
      })}
    </div>
  );
}
