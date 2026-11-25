"use strict";
/**
 * browser-wiring.test.js — the static half of the embedded browser's contract.
 *
 * `main.js` is never executed by a test: the suites that exist either mock `window.emrg` or
 * talk to `daemon_client` directly, and this sandbox cannot launch Electron at all. So the
 * decisions are tested where they live (`browser-policy.test.js`) and what is left here is
 * the wiring, pinned the way `boot-contract.test.js` pins the boot chain: by source shape.
 *
 * What it holds, and why each line is not decoration:
 *
 *  - the four session policies are installed on the **browser's** session, obtained from
 *    `session.fromPartition(BROWSER_PARTITION)` — not on the default session. Electron hangs
 *    permission, download and certificate policy off a `Session`, so one handler set on the
 *    default session would govern the app's own renderer too;
 *  - the download handler never calls `setSavePath`, because calling it is what turns the
 *    save dialog into a silent write;
 *  - a new window is loaded in the same view rather than denied and dropped;
 *  - leaving the tab hides the view instead of destroying it, so a return finds the page
 *    where it was.
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

// Normalised once, here: a CRLF checkout makes every indexOf/slice below answer -1 and
// collapse to "", which reads as "the wiring is missing" on one platform only.
const MAIN = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8").replace(/\r\n/g, "\n");

/** The body of one function in main.js, from its declaration to the closing brace that
 *  starts a new top-level sibling. */
function functionRegion(name) {
  const start = MAIN.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} must exist in main.js`);
  const next = MAIN.indexOf("\n  function ", start + 10);
  const fallback = MAIN.indexOf("\n  // ──", start + 10);
  const ends = [next, fallback].filter((i) => i > 0);
  return MAIN.slice(start, ends.length ? Math.min(...ends) : undefined);
}

test("the browser's policies hang off its own partition, never the default session", () => {
  const region = functionRegion("applyBrowserPolicy");
  // The subject: a Session built from the persisted partition this feature owns.
  assert.match(
    region,
    /setPermissionRequestHandler/,
    "a permission handler must be installed, or Electron's default decides",
  );
  assert.match(
    region,
    /\.on\(\s*"will-download"/,
    "a download handler must be installed, or a download lands wherever Electron puts it",
  );
  assert.match(
    region,
    /setCertificateVerifyProc/,
    "the certificate verdict must be stated, not inherited",
  );
  // And the other half: nothing in main.js may name the default session at all. One handler
  // there also governs the app's renderer, and one clearStorageData there takes the column
  // widths and the language with it.
  assert.doesNotMatch(
    MAIN,
    /defaultSession/,
    "the browser's policies must not reach the app's own session",
  );
  const created = functionRegion("ensureBrowserView");
  assert.match(
    created,
    /session\.fromPartition\(\s*BROWSER_PARTITION\s*\)/,
    "the policies' subject is the browser's partition, named once as a constant",
  );
  assert.match(
    created,
    /applyBrowserPolicy\(/,
    "the view that is built must be the one the policies are installed for",
  );
});

test("the download dialog is never skipped by writing a path", () => {
  assert.doesNotMatch(
    MAIN,
    /setSavePath\s*\(/,
    "calling setSavePath is what makes a download silent; the dialog is the requirement",
  );
  assert.match(
    functionRegion("applyBrowserPolicy"),
    /setSaveDialogOptions\(/,
    "the dialog is pre-filled with the file's own name",
  );
});

test("a new window loads in the same view instead of being dropped", () => {
  const region = functionRegion("applyBrowserPolicy");
  assert.match(region, /setWindowOpenHandler\(/, "a window-open handler must exist");
  assert.match(region, /newWindowTarget\(/, "the decision comes from the policy module");
  assert.match(
    region,
    /loadURL\(/,
    "the target is loaded in this view: denying and dropping is what loses the click",
  );
});

test("leaving the tab hides the view and does not destroy it", () => {
  const region = functionRegion("updateBrowserBounds");
  assert.match(region, /setVisible\(false\)/, "an unfilled box means hidden");
  assert.doesNotMatch(
    MAIN,
    /browserView\.destroy\(|browserView\.webContents\.close\(/,
    "a hidden page must keep its contents: destroying it is what a reload on return would hide",
  );
  assert.match(
    MAIN,
    /ipcMain\.handle\(\s*"emrg:hideBrowser"/,
    "the renderer needs a channel to hide it when the tab is switched away",
  );
});

test("both views read frame navigation off the event object Electron 31 passes", () => {
  // The defect this pins was live and silent: `will-frame-navigate`'s listener receives ONE
  // argument (`WebContentsWillFrameNavigateEventParams` — url and isMainFrame are properties),
  // so a handler written `(event, url, isInPlace, isMainFrame)` reads `undefined` twice, the
  // condition is falsy, and every navigation it was written to refuse is allowed. A source
  // shape is all a test can reach here, because main.js is never executed by a suite.
  // A comment may quote the broken shape — the one in main.js explains it, in prose — so the
  // shape is looked for in code with its comment lines removed, and only where an arrow's
  // parameter list ends in `=>`. Matching the comment is how this guard first failed itself.
  const codeOnly = (text) => text.replace(/^\s*\/\/.*$/gm, "");
  const POSITIONAL = /\(\s*(?:event|details|_?e)\s*,\s*_?url\s*,\s*_?isInPlace\s*,\s*_?isMainFrame\s*\)\s*=>/;

  const browser = functionRegion("applyBrowserPolicy");
  assert.match(browser, /will-frame-navigate/, "the browser view installs the handler");
  assert.match(
    browser,
    /blocksMainFrameNavigation\(/,
    "the browser's verdict comes from the policy module, which a test can call",
  );
  assert.doesNotMatch(
    codeOnly(browser),
    POSITIONAL,
    "positional arguments are undefined in Electron 31 — the policy then never fires",
  );

  const preview = functionRegion("ensurePreviewView");
  assert.match(preview, /will-frame-navigate/, "the preview view installs the handler");
  assert.match(preview, /details\.isMainFrame/, "isMainFrame is read off the event object");
  assert.match(preview, /details\.url/, "the URL is read off the event object");
  assert.doesNotMatch(
    codeOnly(preview),
    POSITIONAL,
    "the preview's file-only rule was dead while it read positional arguments",
  );
});
