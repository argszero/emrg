// window-title.js — the window title's single composition rule (issue #3040)
//
// The Electron window title named the open session by its id alone, even though a
// session's name is already carried everywhere it is needed: the daemon's session list
// hands it out, the main process keeps a copy per open session in `openSessions`, and the
// renderer even computed one that nothing read.
//
// Three call sites used to compose the string themselves — switching sessions, creating
// one, and restoring the last open one — so the title could only be as fresh as the last
// site that remembered to write it: a session renamed afterwards kept showing the name it
// had at switch time. This module replaces the three with one rule plus one refresher,
// and the refresher deliberately holds **no** copy of the state: it reads the current
// session id and that session's name at the moment it runs, so a rename or a name arriving
// late needs nothing but another call.
"use strict";

const APP_NAME = "EMRG";
/** The separator between the application name and the session: an em dash, as it has always been. */
const SEP = "\u2014";

/**
 * Flatten a session name to one line and trim it.
 *
 * A name reaches the title from metadata a later writer controls, so it can carry a
 * newline or padding; a title is one line, and `"  a\nb  "` has to read `a b` rather than
 * paint half of itself off the edge of a window switcher.
 */
function cleanSessionName(name) {
  if (typeof name !== "string") return "";
  return name.replace(/\s+/g, " ").trim();
}

/**
 * `EMRG — <name> (<id>)` for a named session, `EMRG — <id>` for an unnamed one, and the
 * bare application name when nothing is open.
 *
 * An unnamed session never gets an empty pair of parentheses: the name half is omitted
 * whole, so no reader has to tell "no name" from "a name that came out blank".
 */
function composeWindowTitle(sessionId, name) {
  const id = typeof sessionId === "string" ? sessionId.trim() : "";
  if (!id) return APP_NAME;
  const clean = cleanSessionName(name);
  return clean ? `${APP_NAME} ${SEP} ${clean} (${id})` : `${APP_NAME} ${SEP} ${id}`;
}

/**
 * Build the refresher a host binds to its own state.
 *
 * `getSessionId()` and `getSessionName(sid)` are asked **every** call, which is the whole
 * point: a title recomposed from a snapshot is a second answer that drifts, and drifting
 * is exactly the defect this replaces. The refresher returns the title it wrote so a
 * caller can log it without composing a second time.
 *
 * @param {object} deps
 * @param {(title: string) => void} deps.setTitle   writes the title to the window
 * @param {() => string|null} deps.getSessionId     the session the window is showing
 * @param {(sid: string) => (string|undefined|null)} deps.getSessionName
 */
function makeTitleRefresher({ setTitle, getSessionId, getSessionName }) {
  return function refreshWindowTitle() {
    const sid = getSessionId();
    const title = composeWindowTitle(sid, sid && getSessionName ? getSessionName(sid) : "");
    setTitle(title);
    return title;
  };
}

module.exports = { APP_NAME, composeWindowTitle, cleanSessionName, makeTitleRefresher };
