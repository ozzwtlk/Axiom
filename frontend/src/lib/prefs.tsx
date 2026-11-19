import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { load, save } from "./storage";
import type { Level } from "../api/types";

export type ThemePref = "system" | "light" | "dark";
export type Width = "narrow" | "medium" | "wide";

interface Prefs {
  theme: ThemePref;
  effectiveTheme: "light" | "dark";
  toggleTheme: () => void;
  width: Width;
  setWidth: (w: Width) => void;
  level: Level;
  setLevel: (l: Level) => void;
  /** exercise-first mode: show the chapter's related exercises before a section's text */
  exerciseFirst: boolean;
  setExerciseFirst: (v: boolean) => void;
}

const Ctx = createContext<Prefs | null>(null);

function systemDark(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<ThemePref>(() => load<ThemePref>("axiom.theme", "system"));
  const [width, setWidthState] = useState<Width>(() => load<Width>("axiom.width", "medium"));
  const [level, setLevelState] = useState<Level>(() => load<Level>("axiom.level", 2));
  const [sysDark, setSysDark] = useState(systemDark);
  const [exerciseFirst, setExFirstState] = useState<boolean>(() => load<boolean>("axiom.exerciseFirst", false));

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const fn = () => setSysDark(mq.matches);
    mq.addEventListener("change", fn);
    return () => mq.removeEventListener("change", fn);
  }, []);

  const effectiveTheme: "light" | "dark" = theme === "system" ? (sysDark ? "dark" : "light") : theme;

  useEffect(() => {
    const el = document.documentElement;
    if (theme === "system") delete el.dataset.theme;
    else el.dataset.theme = theme;
    save("axiom.theme", theme);
  }, [theme]);

  useEffect(() => {
    document.documentElement.dataset.width = width;
    save("axiom.width", width);
  }, [width]);

  const toggleTheme = useCallback(() => {
    setTheme(() => {
      const next = effectiveTheme === "dark" ? "light" : "dark";
      // Going back to the system value: forget the override so OS changes apply again.
      return next === (sysDark ? "dark" : "light") ? "system" : next;
    });
  }, [effectiveTheme, sysDark]);

  const setWidth = useCallback((w: Width) => setWidthState(w), []);
  const setLevel = useCallback((l: Level) => {
    setLevelState(l);
    save("axiom.level", l);
  }, []);

  const setExerciseFirst = useCallback((v: boolean) => {
    setExFirstState(v);
    save("axiom.exerciseFirst", v);
  }, []);

  const value = useMemo(
    () => ({ theme, effectiveTheme, toggleTheme, width, setWidth, level, setLevel, exerciseFirst, setExerciseFirst }),
    [theme, effectiveTheme, toggleTheme, width, setWidth, level, setLevel, exerciseFirst, setExerciseFirst],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePrefs(): Prefs {
  const v = useContext(Ctx);
  if (!v) throw new Error("usePrefs outside PrefsProvider");
  return v;
}

/** True when a keyboard event originates from a text field (shortcuts must not fire). */
export function isTypingTarget(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  if (!t) return false;
  const tag = t.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable;
}
