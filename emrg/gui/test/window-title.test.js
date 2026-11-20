"use strict";
/**
 * window-title.test.js — the window title is the bare product name (issue #3089).
 *
 * Issue #3040 put the open session into the title (`EMRG — <name> (<id>)`) and this file
 * guarded the seven sites that rewrote it. The host's 2026-11-20T18:09:49 rant reversed
 * that decision: the title bar is drawn by the operating system, so nothing inside it can
 * be dragged or copied — and the session id is the very thing a reader copies, into an
 * issue, into a log, to say to another agent which session is meant. The name and the id
 * moved into the status band, where they are ordinary selectable text
 * (`renderer/src/components/StatusBand.tsx`); the title stayed behind as one constant.
 *
 * `main.js` cannot be loaded by this suite (it requires Electron), so the wiring half is
 * read from its source — the shape the repo already uses where the subject is a file. What
 * it looks for is the *absence of a writer*: a title nothing recomposes is a title that
 * cannot come to name a session again.
 *
 * The three mutations this file has to redden (rant 2026-11-20T18:09:49): put the session
 * back into the title, and either the composer or its refresher reappears here.
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const titleModule = require("../window-title");

const GUI_ROOT = path.join(__dirname, "..");
const MAIN_SRC = fs.readFileSync(path.join(GUI_ROOT, "main.js"), "utf-8");
const INDEX_HTML = fs.readFileSync(path.join(GUI_ROOT, "renderer", "src", "index.html"), "utf-8");

/** How many times `needle` occurs — 0 asserted below reads as a count, not as a vibe. */
function count(haystack, needle) {
  return haystack.split(needle).length - 1;
}

test("the title is the bare product name", () => {
  assert.strictEqual(titleModule.APP_NAME, "EMRG");
});

test("both carriers of the title say the same thing", () => {
  // Electron adopts the page's <title> once it is parsed; the BrowserWindow option only
  // covers the moment before that. Two carriers, one value — or a window that renames
  // itself a second after it opens.
  const matches = INDEX_HTML.match(/<title>([\s\S]*?)<\/title>/);
  assert.ok(matches, "renderer/src/index.html carries no <title>");
  assert.strictEqual(matches[1].trim(), titleModule.APP_NAME);
});

test("the module carries no composition rule any more", () => {
  // A surviving export is how the old shape comes back: #3040's rule was a pure function
  // of (sessionId, name), so a caller that still found one would keep naming sessions.
  assert.deepStrictEqual(Object.keys(titleModule), ["APP_NAME"]);
});

test("main.js takes the name from this module", () => {
  assert.strictEqual(
    count(MAIN_SRC, 'const { APP_NAME } = require("./window-title")'),
    1,
    "main.js must take the product name from window-title.js, once",
  );
});

test("main.js never writes a window title", () => {
  // The three shapes a title writer takes: the Electron call, #3040's refresher, and a
  // hand-composed `EMRG — …` string. Any one of them would let the window name a session
  // again, which is the defect the status band exists to end.
  assert.strictEqual(count(MAIN_SRC, "setTitle"), 0, "main.js calls setTitle");
  assert.strictEqual(count(MAIN_SRC, "refreshWindowTitle"), 0, "main.js holds a title refresher");
  assert.strictEqual(count(MAIN_SRC, "EMRG \u2014 "), 0, "main.js composes an `EMRG — …` title");
});
