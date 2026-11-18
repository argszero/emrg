import type { ToolRow, TranscriptEntry } from "./transcript";

/**
 * inspector.ts — what the inspector column reads off the transcript (issue #2969).
 *
 * The inspector's first tab answers one question — what has this turn done so far —
 * and every part of that answer is already on screen in the transcript: the tool rows,
 * in the order they ran, each with the duration the daemon measured. Nothing here
 * counts, times or guesses on its own: the client inventing a duration would be the
 * second answer to a question the daemon already answered.
 *
 * "This turn" is what follows the last message the host sent, and it is read from the
 * transcript rather than held in a counter, so reopening a session mid-turn shows the
 * same list the host would have watched arrive.
 */

/** One tool call of the current turn, as the inspector lists it. */
export interface TurnAction {
  callId: string;
  toolName: string;
  status: ToolRow["status"];
  /** The daemon's own duration in seconds; `null` = it reported none (not zero). */
  elapsed: number | null;
}

/** One path this turn's tool calls named, with the call that named it. */
export interface TurnFile {
  path: string;
  toolName: string;
}

function toAction(row: ToolRow): TurnAction {
  const e = row.elapsed;
  return {
    callId: row.callId,
    toolName: row.toolName,
    status: row.status,
    elapsed: typeof e === "number" && Number.isFinite(e) && e >= 0 ? e : null,
  };
}

/** Where the current turn starts: just after the last message the host sent. */
function turnStart(entries: TranscriptEntry[]): number {
  for (let i = entries.length - 1; i >= 0; i--) {
    if (entries[i].kind === "user") return i + 1;
  }
  // No message from the host in this transcript: everything on screen is the turn
  // being watched, which is what an opened-in-flight session looks like.
  return 0;
}

/** This turn's tool calls, in the order they ran. */
export function turnActions(entries: TranscriptEntry[]): TurnAction[] {
  const out: TurnAction[] = [];
  for (let i = turnStart(entries); i < entries.length; i++) {
    const entry = entries[i];
    if (entry.kind === "tool-row") out.push(toAction(entry.row));
    else if (entry.kind === "tool-group") {
      for (const row of entry.group.rows) out.push(toAction(row));
    }
  }
  return out;
}

/** The argument names a tool uses for "the file I am working on". */
const PATH_KEYS = ["file_path", "path", "notebook_path"] as const;

function pathOf(args: unknown): string | null {
  if (!args || typeof args !== "object") return null;
  const rec = args as Record<string, unknown>;
  for (const key of PATH_KEYS) {
    const v = rec[key];
    if (typeof v === "string" && v.trim()) return v;
  }
  return null;
}

/**
 * The files this turn named, one row per path, in the order the turn reached them.
 *
 * A call that names no path (a search, a command, a message) contributes nothing, and
 * a path reached twice appears once — the tab answers "what is this turn touching",
 * not "how many calls happened", which the first tab answers.
 */
export function turnFiles(entries: TranscriptEntry[]): TurnFile[] {
  const seen = new Set<string>();
  const out: TurnFile[] = [];
  for (const action of turnCalls(entries)) {
    const p = pathOf(action.args);
    if (p === null || seen.has(p)) continue;
    seen.add(p);
    out.push({ path: p, toolName: action.toolName });
  }
  return out;
}

interface CallWithArgs {
  toolName: string;
  args: unknown;
}

function turnCalls(entries: TranscriptEntry[]): CallWithArgs[] {
  const out: CallWithArgs[] = [];
  const push = (row: ToolRow) => out.push({ toolName: row.toolName, args: row.arguments });
  for (let i = turnStart(entries); i < entries.length; i++) {
    const entry = entries[i];
    if (entry.kind === "tool-row") push(entry.row);
    else if (entry.kind === "tool-group") {
      for (const row of entry.group.rows) push(row);
    }
  }
  return out;
}

/**
 * A duration for a 262px column: `—` when the daemon reported none, tenths under ten
 * seconds, whole seconds above it (a 3-minute call does not need its tenths).
 */
export function formatActionElapsed(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return "—";
  }
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  return `${Math.round(seconds)}s`;
}
