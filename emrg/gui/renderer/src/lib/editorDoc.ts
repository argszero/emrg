/**
 * Editor behaviour that a test can hold still (rant 2026-11-20T18:01:52).
 *
 * The panel around it is layout and wiring; what is decided here is the set of rules that
 * each have a wrong-but-plausible neighbour:
 *
 * - which language a path is coloured as — an unknown extension must be *plain text*, not
 *   a failed import and not a guess that throws;
 * - when a file is opened **read only** — and the honest reasons are only three: the
 *   reader did not give us the whole file, it is not text at all, or it is past the size
 *   this editor will hold;
 * - what counts as "changed on disk" — the pair the panel recorded when it read or saved,
 *   compared with the pair a probe returns now;
 * - how the minimap reduces a document to a fixed number of bars, so a 40,000-line file
 *   and a 40-line one both draw in the same column;
 * - and the two per-session display choices, which live in memory keyed by session so one
 *   conversation's wrap setting does not arrive in another's.
 */

import { languages } from "@codemirror/language-data";
import type { Extension } from "@codemirror/state";

/** One file's colouring. `name` is the catalogue's name; `null` means plain text. */
export interface LanguageChoice {
  name: string | null;
}

/**
 * Extension to language name.
 *
 * Every value here is a name the installed catalogue actually carries (checked against
 * `languages` by this file's test, so a catalogue that renames one goes red rather than
 * silently colouring nothing). Only the last path segment's suffix counts, and a file with
 * no suffix is plain text.
 */
const BY_EXTENSION: Record<string, string> = {
  py: "Python",
  js: "JavaScript",
  mjs: "JavaScript",
  cjs: "JavaScript",
  jsx: "JSX",
  ts: "TypeScript",
  tsx: "TSX",
  json: "JSON",
  md: "Markdown",
  markdown: "Markdown",
  html: "HTML",
  htm: "HTML",
  css: "CSS",
  sh: "Shell",
  bash: "Shell",
  zsh: "Shell",
  yml: "YAML",
  yaml: "YAML",
  toml: "TOML",
  rs: "Rust",
  go: "Go",
  java: "Java",
  sql: "SQL",
  xml: "XML",
};

/**
 * Plain text: no colouring, and no language name to print.
 *
 * The word the status corner prints for this case comes from the dictionary, not from
 * here — a hard-coded "Plain text" would be the one English string in a Chinese panel, and
 * it would sit in a module that has no business knowing the interface's language.
 */
export const PLAIN_TEXT: LanguageChoice = { name: null };

/**
 * The colouring for one path.
 *
 * An extension this table does not know is not an error: the rant's own words are "认不出
 * 的按纯文本显示且不报错", and the failure guarded against is a lookup that throws — or a
 * fallback that silently colours C code as JavaScript.
 */
export function languageFor(path: string): LanguageChoice {
  const name = path.split(/[\\/]/).pop() ?? path;
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return PLAIN_TEXT;
  const ext = name.slice(dot + 1).toLowerCase();
  const found = BY_EXTENSION[ext];
  if (!found) return PLAIN_TEXT;
  return { name: found };
}

/** The catalogue's names, so a test can compare that table against what is installed. */
export function catalogueNames(): string[] {
  return languages.map((language) => language.name);
}

/**
 * The support extension for one language, loaded on first use.
 *
 * `null` for plain text and for a name the catalogue does not carry — the editor then
 * shows text with no colouring, which is what "认不出的按纯文本显示" asks for. The import is
 * dynamic because the catalogue is ~30 grammars and a session that never opens a Rust file
 * should not pay for one.
 */
export async function languageSupportFor(choice: LanguageChoice): Promise<Extension | null> {
  if (!choice.name) return null;
  const description = languages.find((language) => language.name === choice.name);
  if (!description) return null;
  const support = description.support ?? (await description.load()).support;
  return support ?? null;
}

/** Past this many bytes the editor opens the file read only. */
export const LARGE_FILE_BYTES = 4 * 1024 * 1024;

/** What the reader answered about a file, reduced to the facts the read-only gate needs. */
export interface ReadShape {
  binary: boolean;
  truncated: boolean;
  /** The file's size on disk, when the panel managed to probe it. */
  size: number | null;
}

/** Why a file is read only. The panel turns each into its own sentence. */
export type ReadOnlyReason = "binary" | "truncated" | "large";

/**
 * Whether this file has to be opened read only, and why.
 *
 * Three reasons and no fourth. `binary` is not text at all. `truncated` means the reader
 * showed a window, not the file: letting that buffer save would delete every line past the
 * window, so read-only is not caution, it is the only correct answer. The size bound is a
 * policy about what this editor will hold rather than a measurement of anything.
 */
export function readOnlyReason(shape: ReadShape): ReadOnlyReason | null {
  if (shape.binary) return "binary";
  if (shape.truncated) return "truncated";
  if (shape.size !== null && shape.size > LARGE_FILE_BYTES) return "large";
  return null;
}

/** What the panel recorded about a file it read or wrote. */
export interface DiskStamp {
  mtime: number | null;
  size: number | null;
  /** False when the probe says the file is gone. */
  exists?: boolean;
}

/** What a probe found, in the three words the panel has sentences for. */
export type DiskChange = "deleted" | "changed" | null;

/**
 * Whether the file under the panel moved since the stamp was taken.
 *
 * Three cases, and each is a different sentence to the host: the file is **gone**, its
 * **stamp** differs, or nothing moved. A `null` on either side means "not measured", and
 * an unmeasured probe is never reported as a change — the panel would then prompt about a
 * change it cannot describe. Comparing only when *both* sides carried the number is what
 * keeps a first probe (no stamp yet) from reading as an edit.
 */
export function diskChange(recorded: DiskStamp | null, probed: DiskStamp | null): DiskChange {
  if (!probed) return null;
  if (probed.exists === false) return "deleted";
  if (!recorded) return null;
  const sizeDiffers =
    probed.size !== null && recorded.size !== null && probed.size !== recorded.size;
  const mtimeDiffers =
    probed.mtime !== null && recorded.mtime !== null && probed.mtime !== recorded.mtime;
  return sizeDiffers || mtimeDiffers ? "changed" : null;
}

/** The two display choices, remembered per session (in memory — nothing is persisted). */
export interface EditorPrefs {
  wrap: boolean;
  minimap: boolean;
}

/** Wrap is on by default: a log with a 400-column line is the common case in this panel. */
export const DEFAULT_PREFS: EditorPrefs = { wrap: true, minimap: false };

/**
 * One preference store per panel, keyed by session id.
 *
 * A store object rather than a module-level `Map`: two panels in one process (a test, or a
 * future second window) must not share a session's toggles, and a module-level map would
 * also carry one test's setting into the next.
 */
export class EditorPrefsStore {
  private bySession = new Map<string, EditorPrefs>();

  private key(sid: string | null | undefined): string {
    return sid ? `session:${sid}` : "no-session";
  }

  get(sid: string | null | undefined): EditorPrefs {
    return this.bySession.get(this.key(sid)) ?? DEFAULT_PREFS;
  }

  set(sid: string | null | undefined, patch: Partial<EditorPrefs>): EditorPrefs {
    const next = { ...this.get(sid), ...patch };
    this.bySession.set(this.key(sid), next);
    return next;
  }
}

/** A caret position, 1-based for both numbers, which is how an editor reports it. */
export interface CursorPosition {
  line: number;
  column: number;
}

/** Where the caret is, from a document and an offset. */
export function cursorPositionAt(
  doc: { lineAt(offset: number): { number: number; from: number } },
  offset: number,
): CursorPosition {
  const line = doc.lineAt(offset);
  return { line: line.number, column: offset - line.from + 1 };
}

/** One row of the minimap: how far across to draw, and whether there is anything there. */
export interface MinimapBar {
  /** Longest line in this slice, as a fraction of the document's longest line (0..1). */
  width: number;
  /** False for a slice that is entirely blank, so a gap in the file reads as a gap. */
  filled: boolean;
}

/** How many rows the minimap draws, whatever the document's length. */
export const MINIMAP_BARS = 120;

/**
 * Reduce a document to at most `maxBars` rows.
 *
 * The minimap's whole job is to be the same size for every file, so a long document is
 * **aggregated** rather than truncated: each row takes the longest line of the lines it
 * stands for, which keeps a single long line visible in a 40,000-line file. Capping by
 * slicing would make every large file look like its own first page.
 *
 * `width` is measured against the document's own longest line, so the map uses its full
 * width whether the file's longest line is 20 characters or 2,000. A document with no
 * non-blank line answers empty rather than 120 zero-width rows.
 */
export function minimapBars(text: string, maxBars = MINIMAP_BARS): MinimapBar[] {
  const lines = text.split("\n");
  const longest = lines.reduce((max, line) => Math.max(max, line.trimEnd().length), 0);
  if (longest === 0) return [];
  const perBar = Math.max(1, Math.ceil(lines.length / maxBars));
  const bars: MinimapBar[] = [];
  for (let start = 0; start < lines.length; start += perBar) {
    const slice = lines.slice(start, start + perBar);
    const widest = slice.reduce((max, line) => Math.max(max, line.trimEnd().length), 0);
    bars.push({ width: widest / longest, filled: widest > 0 });
  }
  return bars;
}

/**
 * The line a click at `ratio` down the minimap should jump to.
 *
 * The map is drawn from the top of the document, so the fraction is of the document's line
 * count, 1-based — and clamped, because a click on the last bar's bottom pixel reports a
 * ratio of 1 and asking for line `count + 1` would land past the end.
 */
export function lineForRatio(ratio: number, lineCount: number): number {
  if (lineCount <= 0) return 1;
  const clamped = Math.min(1, Math.max(0, ratio));
  return Math.min(lineCount, Math.floor(clamped * lineCount) + 1);
}
