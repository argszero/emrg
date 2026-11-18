import { useEffect, useState } from "react";

/**
 * theme.ts — one store for the window's theme (issue #2986).
 *
 * The theme used to be local state inside `SettingsPanel`, which was fine while the
 * settings page held the only control over it. The rail now carries a second control
 * (the moon/sun button the prototype draws), and two `useState` copies of one window
 * fact is the drift the cockpit exists to remove: toggling from the rail would leave
 * the settings page reading the old value until something reloaded it.
 *
 * So the value and its single writer live here, and every reader subscribes.
 *
 * The persisted key and the `data-theme` attribute are the settings page's own — it
 * already wrote them, and a second spelling would mean two windows disagreeing about
 * which theme they are in.
 */

export type ThemeMode = "light" | "dark" | "system";

export const THEME_STORAGE_KEY = "emrg.theme";

/** `system` follows the OS; the host picks light or dark explicitly. */
export function resolveTheme(mode: ThemeMode): "light" | "dark" {
  if (mode !== "system") return mode;
  if (typeof window === "undefined" || !window.matchMedia) return "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

const listeners = new Set<(mode: ThemeMode) => void>();

function readStored(): ThemeMode {
  try {
    const v = window.localStorage?.getItem(THEME_STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    // A window with storage denied is a window that runs on the default, not one that
    // fails to render.
  }
  return "system";
}

let current: ThemeMode = typeof window === "undefined" ? "system" : readStored();

/** Write the attribute every stylesheet reads. */
function apply(mode: ThemeMode): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", resolveTheme(mode));
}

if (typeof document !== "undefined") apply(current);

export function getTheme(): ThemeMode {
  return current;
}

/**
 * Set the mode, persist it, and put the resolved theme on the document.
 *
 * `system` is stored as itself rather than as the light/dark it currently resolves to,
 * because the host's choice was "follow the OS" — storing the resolution would freeze
 * the window to today's OS answer.
 */
export function setTheme(mode: ThemeMode): void {
  current = mode;
  try {
    window.localStorage?.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // Persistence is a convenience; the theme still changes for this window.
  }
  apply(mode);
  for (const fn of listeners) fn(mode);
}

/** The one hook: the mode, its resolved value, and the writer. */
export function useTheme(): { mode: ThemeMode; resolved: "light" | "dark"; setMode: (m: ThemeMode) => void } {
  const [mode, setMode] = useState<ThemeMode>(current);

  useEffect(() => {
    const fn = (m: ThemeMode) => setMode(m);
    listeners.add(fn);
    // A write that landed between render and subscribe would otherwise be missed.
    if (current !== mode) setMode(current);
    return () => {
      listeners.delete(fn);
    };
    // `mode` is deliberately absent: this subscribes once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { mode, resolved: resolveTheme(mode), setMode: setTheme };
}

/** The rail's toggle: light asks for dark, dark and system ask for light. */
export function nextMode(resolved: "light" | "dark"): ThemeMode {
  return resolved === "light" ? "dark" : "light";
}
