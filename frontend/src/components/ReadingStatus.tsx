export function ReadingPercent({ percent }: { percent: number }) {
  return <span className="reading-percent" title="Reading progress" aria-label={`Reading progress: ${percent}%`}>{percent}% read</span>;
}

export function ReadingStatus({ finished }: { finished: boolean }) {
  return <span className="reading-status">{finished ? "Finished reading" : "Still reading"}</span>;
}

export function ProcessedCheck() {
  return <span className="processed-check" role="img" aria-label="Processing complete" title="Processing complete">✓</span>;
}
