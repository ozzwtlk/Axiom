import { useEffect } from "react";
import { BrowserRouter, Link, Route, Routes, useLocation } from "react-router-dom";
import { MOCK } from "./api/client";
import { Icon } from "./components/Icon";
import { DocumentPage } from "./pages/DocumentPage";
import { ExercisePage } from "./pages/ExercisePage";
import { Library } from "./pages/Library";
import { PracticePage } from "./pages/PracticePage";
import { QuizPage } from "./pages/QuizPage";
import { Reader } from "./pages/Reader";
import { SettingsPage } from "./pages/SettingsPage";
import { isTypingTarget, PrefsProvider, usePrefs } from "./lib/prefs";

function GlobalKeys() {
  const { toggleTheme } = usePrefs();
  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTypingTarget(e)) return;
      if (e.key === "d" || e.key === "D") toggleTheme();
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, [toggleTheme]);
  return null;
}

function TopBar() {
  const loc = useLocation();
  const { toggleTheme, effectiveTheme } = usePrefs();
  if (loc.pathname.startsWith("/read/")) return null; // the reader has its own header
  return (
    <header className="topbar">
      <Link to="/" className="brand" aria-label="AXIOM library">
        <span className="brand-mark" aria-hidden="true">∂</span>
        <span className="brand-name">AXIOM</span>
      </Link>
      <nav className="topnav">
        <Link to="/" className={loc.pathname === "/" ? "active" : ""}>
          Library
        </Link>
        <Link to="/settings" className={loc.pathname === "/settings" ? "active" : ""}>
          AI models
        </Link>
      </nav>
      {MOCK && <span className="tag" title="Using built-in fixtures (?mock=0 to disable)">mock data</span>}
      <span className="spacer" />
      <button type="button" className="icon-btn" onClick={toggleTheme} title="Toggle theme (D)" aria-label="Toggle theme">
        <Icon name={effectiveTheme === "dark" ? "sun" : "moon"} />
      </button>
    </header>
  );
}

export function App() {
  return (
    <PrefsProvider>
      <BrowserRouter>
        <GlobalKeys />
        <TopBar />
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/doc/:docId" element={<DocumentPage />} />
          <Route path="/read/:sectionId" element={<Reader />} />
          <Route path="/exercise/:id" element={<ExercisePage />} />
          <Route path="/quiz/:scope/:id" element={<QuizPage />} />
          <Route path="/practice/:docId" element={<PracticePage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<main className="page"><p>Not found. <Link to="/">Library</Link></p></main>} />
        </Routes>
      </BrowserRouter>
    </PrefsProvider>
  );
}
