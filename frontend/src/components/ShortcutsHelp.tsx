const KEYS: [string, string][] = [
  ["1 / 2 / 3", "Fast Track / Standard / Deep Dive"],
  ["j / k", "Next / previous section"],
  ["e", "Jump to exercises"],
  ["d", "Toggle dark / light theme"],
  ["/", "Focus search"],
  ["Esc", "Close panels"],
  ["?", "Show this help"],
];

export function ShortcutsHelp({ onClose }: { onClose: () => void }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-label="Keyboard shortcuts" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Keyboard shortcuts</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <table className="keys">
          <tbody>
            {KEYS.map(([k, d]) => (
              <tr key={k}>
                <td>
                  <kbd>{k}</kbd>
                </td>
                <td>{d}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">Select any text to ask the AI about it.</p>
      </div>
    </div>
  );
}
