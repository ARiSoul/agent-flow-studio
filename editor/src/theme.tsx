// Theme choice: "system" follows the OS (light/dark); the others are fixed palettes in styles.css.
// To add a theme: tokens in styles.css ([data-theme="<id>"]), an entry here, the name in
// public/theme-init.js and a "theme.<id>" string in every i18n file.
import { useCallback, useEffect, useState } from "react";

export const THEMES = {
  system: { base: null },
  light: { base: "light" },
  dark: { base: "dark" },
  sepia: { base: "light" },
  ocean: { base: "dark" },
  contrast: { base: "dark" },
} as const satisfies Record<string, { base: "light" | "dark" | null }>;
export type Theme = keyof typeof THEMES;
export type Base = "light" | "dark";

const STORAGE_KEY = "afs.theme";
const isTheme = (v: unknown): v is Theme => typeof v === "string" && v in THEMES;
const media = () => window.matchMedia("(prefers-color-scheme: dark)");

function savedTheme(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (isTheme(v)) return v;
  } catch { /* storage not available */ }
  return "system";
}

/** The chosen theme, the palette actually shown and its light/dark base (for React Flow). */
export function useTheme(): { theme: Theme; setTheme: (t: Theme) => void; shown: Exclude<Theme, "system">; base: Base } {
  const [theme, setThemeState] = useState<Theme>(savedTheme);
  const [osDark, setOsDark] = useState(() => media().matches);
  useEffect(() => {
    const m = media();
    const on = (e: MediaQueryListEvent) => setOsDark(e.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  const shown = theme === "system" ? (osDark ? "dark" : "light") : theme;
  useEffect(() => { document.documentElement.dataset.theme = shown; }, [shown]);
  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    try { localStorage.setItem(STORAGE_KEY, t); } catch { /* storage not available */ }
  }, []);
  return { theme, setTheme, shown, base: THEMES[shown].base };
}
