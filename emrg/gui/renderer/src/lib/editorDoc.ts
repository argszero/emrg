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
  // `load()` answers a `LanguageSupport` — the grammar together with its extras — and its
  // `support` property is only the extras half. Configuring a view with that half alone
  // leaves the document unparsed: no colouring, no indentation, no comment tokens, and
  // nothing in the props to show for it, because the props carry the name we asked for.
  const support = description.support ?? (await description.load());
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
  /** The editor's text size in pixels. */
  fontSize: number;
}

/**
 * Wrap is off by default: this panel is opened to read code, and a wrapped line of code is
 * harder to follow than a long one — the toggle turns wrapping on, per session.
 *
 * The text size starts where the panel's own small step sits, so the zoom keys start from
 * what a reader already had rather than from a number this module invented.
 */
export const DEFAULT_PREFS: EditorPrefs = { wrap: false, minimap: false, fontSize: 13 };

/** The text size the editor starts at, and the size `Mod+0` returns to. */
export const DEFAULT_FONT_SIZE = DEFAULT_PREFS.fontSize;

/** The range `Mod+=` and `Mod+-` move between — below it the text is unreadable, above it the
 * panel is one word wide, so the keys stop at each end rather than shrinking to nothing. */
export const MIN_FONT_SIZE = 8;
export const MAX_FONT_SIZE = 28;

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

/**
 * How many lines the minimap draws before it starts sampling.
 *
 * The map renders the document's own text, so its cost is one element per line and a
 * 40,000-line file would cost 40,000 of them. Past this many, the lines are **sampled
 * evenly** rather than truncated: the map's job is the file's shape, and the shape of the
 * last tenth of a long file is part of it, while the eighty thousandth line is not a
 * distinguishable row. Truncating instead would make every large file look like its own
 * first page — the defect this replaces.
 */
export const MINIMAP_LINES = 600;

/**
 * The document's lines, ready to be drawn as shrunk code.
 *
 * Trailing whitespace is dropped, because a row whose visible content ends mid-line and one
 * that carries twenty trailing spaces draw identically — and only one of them is the file.
 * Tabs are left alone: a tab is *wider* than a space, so the shape of a tab-indented file is
 * the file's, and expanding it here would state a width this module does not own.
 */
export function minimapLines(text: string, maxLines = MINIMAP_LINES): string[] {
  const lines = text.split("\n").map((line) => line.replace(/\s+$/, ""));
  if (lines.length <= maxLines) return lines;
  const step = lines.length / maxLines;
  const sampled: string[] = [];
  for (let i = 0; i < maxLines; i += 1) {
    sampled.push(lines[Math.min(lines.length - 1, Math.floor(i * step))]);
  }
  return sampled;
}

/** How a document indents. The status corner reports it and the guides align to it. */
export interface IndentUnit {
  kind: "spaces" | "tabs";
  /** Columns per level. For tabs this is the tab stop, not a count of characters. */
  width: number;
}

/** The width assumed when a document gives no evidence of its own. */
export const DEFAULT_INDENT_WIDTH = 4;

/** Widest level this will believe. A wider "block" is far more likely to be alignment. */
const INDENT_WIDTH_CAP = 8;

/**
 * The indent width a document's own lines agree on.
 *
 * The question is not "what does this line use" but "what unit do these lines share": a file
 * of 4/8/12-space indents has a width of 4, and reading the *smallest* indent instead would
 * report 2 for a file that happens to have one continuation line, and 1 for one aligned
 * argument. So each candidate width is scored by how many indented lines are a multiple of
 * it, and the widest candidate that most lines agree on wins.
 *
 * Its failure mode is stated rather than hidden: a file whose every indented line is a
 * visual continuation (5, 9, 13) is a multiple of nothing, answers the default, and the
 * guides are then wrong by a constant — which is why this is a reading on the status corner
 * and not a fact anything saves back to disk.
 */
export function indentWidthFor(counts: number[]): number {
  const indented = counts.filter((n) => n > 0);
  if (indented.length === 0) return DEFAULT_INDENT_WIDTH;
  for (let width = INDENT_WIDTH_CAP; width >= 2; width -= 1) {
    const fits = indented.filter((n) => n % width === 0).length;
    if (fits / indented.length >= 0.75) return width;
  }
  return DEFAULT_INDENT_WIDTH;
}

/**
 * What a document indents with, from its own text.
 *
 * A tab-indented line is decisive: a file that indents with tabs is not asked about its
 * width, because the width is the reader's tab stop rather than the file's. Only when no
 * line starts with a tab does the space count matter.
 */
export function indentUnitFor(text: string): IndentUnit {
  const lines = text.split("\n");
  let tabbed = 0;
  const spaceCounts: number[] = [];
  for (const line of lines) {
    if (line.startsWith("\t")) {
      tabbed += 1;
      continue;
    }
    const leading = /^( +)\S/.exec(line);
    if (leading) spaceCounts.push(leading[1].length);
  }
  if (tabbed > 0) return { kind: "tabs", width: DEFAULT_INDENT_WIDTH };
  return { kind: "spaces", width: indentWidthFor(spaceCounts) };
}

/** A document's line ending, in the three words a status corner prints. */
export type LineEnding = "lf" | "crlf" | "cr";

/**
 * Which line ending a document uses.
 *
 * `\r\n` is checked first and on purpose: a CRLF file contains `\r`, so testing for `\r`
 * first would report every Windows file as a bare-CR one. A file with both is a CRLF file —
 * that is the ending its lines actually have, and the stray bare `\r` inside it is text.
 */
export function lineEndingFor(text: string): LineEnding {
  if (text.includes("\r\n")) return "crlf";
  if (text.includes("\r")) return "cr";
  return "lf";
}

/** The separator a line ending stands for. */
const SEPARATORS: Record<LineEnding, string> = { lf: "\n", crlf: "\r\n", cr: "\r" };

/**
 * A document reduced to one line ending — `\n` unless asked otherwise.
 *
 * CodeMirror's own document is always `\n`: it splits a state's text on `\r\n?|\n` and joins
 * it back with the line separator facet, which defaults to `\n`. So every text that leaves an
 * editor is LF, and a file that arrived as CRLF would silently become LF on the first
 * keystroke — and on the next save, every line of it.
 */
export function normalizeNewlines(text: string): string {
  return text.split(/\r\n?|\n/).join("\n");
}

/**
 * A text written in one line ending, whatever it arrived in.
 *
 * This is the inverse of `lineEndingFor`, and the two are used together: a buffer keeps the
 * ending its file has, so the panel reports the ending a save will produce rather than the one
 * the editor happens to hold.
 */
export function applyLineEnding(text: string, ending: LineEnding): string {
  const separator = SEPARATORS[ending];
  if (separator === "\n") return normalizeNewlines(text);
  return normalizeNewlines(text).split("\n").join(separator);
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
