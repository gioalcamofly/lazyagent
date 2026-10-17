// Theme: "dark" (default), "light" or "system", kept in localStorage. The
// colours themselves are CSS tokens (app.css); `resolvedTheme` is what is
// shown, for the terminals' palettes.

import { signal, effect } from "./html.js";

const KEY = "lazyagent-theme";
export const THEMES = ["dark", "light", "system"];

function stored() {
  try {
    const value = localStorage.getItem(KEY);
    return THEMES.includes(value) ? value : "dark";
  } catch {
    return "dark";
  }
}

const media = matchMedia("(prefers-color-scheme: light)");
const systemLight = signal(media.matches);
media.addEventListener("change", () => (systemLight.value = media.matches));

export const theme = signal(stored());
export const resolvedTheme = signal("dark");

effect(() => {
  const chosen = theme.value;
  const shown = chosen === "system" ? (systemLight.value ? "light" : "dark") : chosen;
  // The tokens first: resolvedTheme's readers take colours from them.
  document.documentElement.dataset.theme = shown;
  document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", shown);
  resolvedTheme.value = shown;
  try {
    localStorage.setItem(KEY, chosen);
  } catch {
    // Not persisted; the choice holds for this load.
  }
});

export function cycleTheme() {
  theme.value = THEMES[(THEMES.indexOf(theme.value) + 1) % THEMES.length];
}

/** An xterm.js theme from the current CSS tokens. */
export function terminalTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    background: v("--term-bg"),
    foreground: v("--term-fg"),
    cursor: v("--term-fg"),
    selectionBackground: v("--term-selection"),
  };
}
