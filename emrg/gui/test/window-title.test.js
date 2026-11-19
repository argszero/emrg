"use strict";
/**
 * window-title.test.js — issue #3040: the window title names a session by its id alone.
 *
 * Two layers are guarded here, because the defect had two halves:
 *
 *   1. **The composition** — `EMRG — <name> (<id>)` for a named session, `EMRG — <id>` for
 *      an unnamed one, the bare application name when nothing is open. Pure, so it is
 *      driven directly.
 *   2. **The wiring** — the title has to be *recomputed* at the moments a session changes:
 *      when a name arrives late, after a rename, when the open session closes. A title
 *      composed only at switch time keeps showing the name it had then, which is the half
 *      a reader notices. `main.js` cannot be loaded by this suite (it requires Electron),
 *      so this half is asserted against its source: the file may no longer compose a title
 *      itself, and each handler that can change the current session must call the
 *      refresher. That is the shape the repo already uses where the subject is a file.
 */
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { composeWindowTitle, cleanSessionName, makeTitleRefresher } = require("../window-title");

const GUI_ROOT = path.join(__dirname, "..");
const MAIN_SRC = fs.readFileSync(path.join(GUI_ROOT, "main.js"), "utf-8");
const SID = "s_260727_1103_866d";

/** Slice a named region out of main.js, refusing a slice that found nothing usable. */
function block(startMarker, endMarker) {
  const i = MAIN_SRC.indexOf(startMarker);
  assert.ok(i >= 0, `main.js no longer contains ${startMarker}`);
  const j = MAIN_SRC.indexOf(endMarker, i + startMarker.length);
  assert.ok(j > i, `main.js has no ${endMarker} after ${startMarker}`);
  const out = MAIN_SRC.slice(i, j);
  // A bound that collapsed to nothing would make every assertion below vacuous.
  assert.ok(out.length > 80, `the slice ${startMarker}…${endMarker} is only ${out.length} chars`);
  return out;
}

// ── 1. Composition ────────────────────────────────────────────────────────────

test("a named session writes the name and the id together", () => {
  assert.strictEqual(composeWindowTitle(SID, "emrg-main"), `EMRG — emrg-main (${SID})`);
});

test("an unnamed session writes the id alone: no empty parentheses, no undefined", () => {
  for (const name of [undefined, null, "", "   ", "\n", 42]) {
    const title = composeWindowTitle(SID, name);
    assert.strictEqual(title, `EMRG — ${SID}`, `name=${JSON.stringify(name)}`);
    assert.ok(!title.includes("()"), `name=${JSON.stringify(name)} produced empty parentheses`);
    assert.ok(!/undefined|null|NaN/.test(title), `name=${JSON.stringify(name)} leaked a placeholder`);
  }
});

test("with nothing open the title carries no session at all", () => {
  for (const sid of [null, undefined, "", "   "]) {
    assert.strictEqual(composeWindowTitle(sid, "emrg-main"), "EMRG", `sid=${JSON.stringify(sid)}`);
  }
});

test("a name with a newline or padding is flattened to one line", () => {
  assert.strictEqual(cleanSessionName("  emrg\n-main  "), "emrg -main");
  assert.strictEqual(composeWindowTitle(SID, "a\nb"), `EMRG — a b (${SID})`);
});

// ── 2. The refresher recomputes from live state ───────────────────────────────

function mountRefresher(initial = {}) {
  const state = { sid: SID, name: undefined, ...initial };
  const written = [];
  const refresh = makeTitleRefresher({
    setTitle: (title) => written.push(title),
    getSessionId: () => state.sid,
    getSessionName: () => state.name,
  });
  return { state, written, refresh };
}

test("a name arriving late updates the title on its own", () => {
  const { state, written, refresh } = mountRefresher();
  assert.strictEqual(refresh(), `EMRG — ${SID}`);
  state.name = "emrg-main"; // 第一条消息之后 daemon 才给它名字
  assert.strictEqual(refresh(), `EMRG — emrg-main (${SID})`);
  assert.deepStrictEqual(written, [`EMRG — ${SID}`, `EMRG — emrg-main (${SID})`]);
});

test("a rename is reflected immediately, not on the next switch", () => {
  const { state, written, refresh } = mountRefresher({ name: "before" });
  assert.strictEqual(refresh(), `EMRG — before (${SID})`);
  state.name = "after"; // 改名成功
  assert.strictEqual(refresh(), `EMRG — after (${SID})`);
  assert.strictEqual(written.at(-1), `EMRG — after (${SID})`);
});

test("closing the open session returns the title to the session-less form", () => {
  const { state, refresh } = mountRefresher({ name: "emrg-main" });
  assert.strictEqual(refresh(), `EMRG — emrg-main (${SID})`);
  state.sid = null;
  assert.strictEqual(refresh(), "EMRG");
});

// ── 3. The wiring in main.js ─────────────────────────────────────────────────

test("main.js composes no title of its own — the rule lives in one file", () => {
  assert.ok(
    !MAIN_SRC.includes("EMRG — "),
    "main.js must not compose a title: every site calls refreshWindowTitle()",
  );
  assert.ok(MAIN_SRC.includes('require("./window-title")'), "main.js must require window-title");
});

test("every handler that changes the current session recomputes the title", () => {
  for (const [name, label] of [
    ["emrg:switchSession", "switching"],
    ["emrg:newSession", "creating"],
    ["emrg:renameSession", "renaming"],
    ["emrg:removeAgent", "removing a project"],
  ]) {
    const body = block(`ipcMain.handle("${name}"`, "ipcMain.handle(");
    assert.ok(
      body.includes("refreshWindowTitle()"),
      `the ${label} handler (${name}) must call refreshWindowTitle()`,
    );
  }
});

test("a late name and a closed session both recompute the title", () => {
  const lateName = block("function touchOpenSession(", "function markSessionActive(");
  assert.ok(lateName.includes("refreshWindowTitle()"), "touchOpenSession must refresh once a name arrives");
  const closing = block("async function closeSession(sid)", "return { ok: true, closed: !!entry };");
  assert.ok(closing.includes("refreshWindowTitle()"), "closeSession must refresh the title");
});
