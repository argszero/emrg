// window-title.js — the window title is the product name, and nothing else (issue #3089)
//
// Issue #3040 composed the title out of the open session — `EMRG — <name> (<id>)` — and
// then had to recompute it at seven sites, so the title could only be as fresh as the last
// site that remembered to rewrite it. The host's 2026-11-20T18:09:49 rant found the other
// half of the problem: the title bar is drawn by the operating system, so nothing in it can
// be dragged or copied, and the session id is exactly what a reader needs to copy — into an
// issue, into a log, to say to another agent which session is meant. The name and the id
// therefore moved into the status band, where they are ordinary selectable text, and the
// title went back to the bare product name.
//
// With no session in it, the title needs no writer: a value that never changes cannot go
// stale, so the refresher and its seven call sites are gone rather than left unused.
//
// The name has two carriers — this constant, which `main.js` hands to `new BrowserWindow`
// so the window never flashes a path or "Electron" while the page loads, and
// `renderer/src/index.html`'s <title>, which Electron adopts once the page is parsed.
// `test/window-title.test.js` compares the two, so changing one alone reddens there.
"use strict";

const APP_NAME = "EMRG";

module.exports = { APP_NAME };
