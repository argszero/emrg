"use strict";
/**
 * browser-policy.test.js — the embedded browser's decisions, tested where they are made.
 *
 * These are the requirements a Linux CI runner can check without Electron: which schemes a
 * page may reach, what a typed address means, that every permission is refused, that the
 * certificate verdict leaves Chromium's own check in place, that a download is never given a
 * path of its own, and where the view's box comes from.
 */

const { test } = require("node:test");
const assert = require("node:assert");

const {
  BROWSER_PARTITION,
  addressTarget,
  blocksMainFrameNavigation,
  browserNavPolicy,
  browserRect,
  certificateResult,
  newWindowTarget,
  permissionGranted,
  saveDialogOptions,
} = require("../browser-policy");

test("the browser has a partition of its own, and it is persisted", () => {
  assert.strictEqual(BROWSER_PARTITION, "persist:emrg-browser");
  // The prefix is the whole mechanism: without it the partition is in-memory and a sign-in
  // does not survive the restart that requirement 5 is about.
  assert.match(BROWSER_PARTITION, /^persist:/, "a non-persisted partition forgets its cookies on quit");
});

test("a page may navigate to http, https, file and about, and nothing else", () => {
  for (const url of [
    "https://example.com/",
    "http://localhost:3000/index.html",
    "file:///Users/someone/page.html",
    "about:blank",
  ]) {
    assert.strictEqual(browserNavPolicy(url), "allow", `${url} is a page this browser draws`);
  }
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
    "blob:https://example.com/1234",
    "chrome://settings",
    "mailto:someone@example.com",
    "view-source:https://example.com/",
    "",
  ]) {
    assert.strictEqual(browserNavPolicy(url), "deny", `${url} is not a navigation the view may make`);
  }
  // A subject that is not a string is a refusal, not a crash: this runs inside an event
  // handler, where a throw leaves the navigation to proceed.
  assert.strictEqual(browserNavPolicy(null), "deny");
  assert.strictEqual(browserNavPolicy(undefined), "deny");
});

test("a new window becomes a navigation in the same view, or is refused", () => {
  assert.strictEqual(newWindowTarget("https://example.com/two"), "https://example.com/two");
  assert.strictEqual(newWindowTarget("file:///tmp/a.html"), "file:///tmp/a.html");
  // Refusing outright is the safe half: a window this app opened would carry no policy.
  assert.strictEqual(newWindowTarget("mailto:someone@example.com"), null);
  assert.strictEqual(newWindowTarget("javascript:alert(1)"), null);
  assert.strictEqual(newWindowTarget(null), null);
});

test("a typed address is read as an address, not as a string to be parsed", () => {
  const url = (raw) => addressTarget(raw);

  assert.deepStrictEqual(url("https://example.com/a?b=1"), { ok: true, url: "https://example.com/a?b=1" });
  // The three shapes people actually type. `example.com` throws in `new URL`, and
  // `localhost:3000` parses as a `localhost:` protocol, so both need reading.
  assert.deepStrictEqual(url("example.com"), { ok: true, url: "https://example.com" });
  assert.deepStrictEqual(url("localhost:3000"), { ok: true, url: "http://localhost:3000" });
  assert.deepStrictEqual(url("127.0.0.1:8080/a"), { ok: true, url: "http://127.0.0.1:8080/a" });
  // A host that is not this machine gets https even with a port on it.
  assert.deepStrictEqual(url("example.com:8443/x"), { ok: true, url: "https://example.com:8443/x" });
  // Surrounding space is the paste's, not the address's.
  assert.deepStrictEqual(url("  example.com  "), { ok: true, url: "https://example.com" });

  assert.deepStrictEqual(url(""), { ok: false, error: "empty" });
  assert.deepStrictEqual(url("   "), { ok: false, error: "empty" });
  assert.deepStrictEqual(url(null), { ok: false, error: "empty" });
  // A scheme the browser cannot render is refused rather than repaired into a URL.
  assert.deepStrictEqual(url("javascript:alert(1)"), { ok: false, error: "scheme" });
  assert.deepStrictEqual(url("data:text/html,x"), { ok: false, error: "scheme" });
  // A scheme with no host is an error page nobody asked for.
  assert.deepStrictEqual(url("http://"), { ok: false, error: "invalid" });
});

test("every permission is refused, the five named ones included", () => {
  // The names are Electron's own permission strings; the last two stand for a permission
  // this build has never heard of, which must get the same answer as the ones it has.
  for (const permission of [
    "media",
    "notifications",
    "geolocation",
    "clipboard-read",
    "clipboard-sanitized-write",
    "display-capture",
    "midi",
    "fullscreen",
    "some-future-permission",
    "unknown",
  ]) {
    assert.strictEqual(permissionGranted(permission), false, `${permission} must not be granted`);
  }
});

test("a certificate error is left to Chromium, not accepted and not blanket-rejected", () => {
  // 0 accepts every certificate (a self-signed one included) and -2 rejects every certificate
  // (a valid one included). -3 asks Chromium for its own verdict, which is the only answer
  // that lets a valid site load and still fails an invalid certificate.
  assert.strictEqual(certificateResult(), -3);
  assert.notStrictEqual(certificateResult(), 0);
  assert.notStrictEqual(certificateResult(), -2);
});

test("a download's dialog is pre-filled with the file's name and with no directory", () => {
  assert.deepStrictEqual(saveDialogOptions("report.zip"), { defaultPath: "report.zip" });
  // Nothing to name → nothing pre-filled; the dialog is still the dialog's to ask.
  assert.deepStrictEqual(saveDialogOptions(""), {});
  assert.deepStrictEqual(saveDialogOptions(undefined), {});
  // The one thing this must never carry: a directory that would make the write silent.
  assert.deepStrictEqual(Object.keys(saveDialogOptions("a.zip")), ["defaultPath"]);
  assert.doesNotMatch(JSON.stringify(saveDialogOptions("a.zip")), /\//, "a path would choose for the user");
});

test("a download's name comes from the server, so a directory in it never survives", () => {
  // `main.js` passes the download item's own name, which Electron derives from the
  // response's `Content-Disposition` — the page supplies it, not the person clicking Save.
  // So the property is asserted over the class and not over one sample: `a.zip` carries no
  // separator, and an assertion fed only that passes for a function that keeps everything.
  const named = [
    ["../../evil.sh", "evil.sh"],
    ["dir/name.txt", "name.txt"],
    ["dir\\name.txt", "name.txt"],
    ["..\\..\\evil.exe", "evil.exe"],
    ["/etc/passwd", "passwd"],
    ["resume\u0000.pdf", "resume.pdf"],
    ["report.zip", "report.zip"],
  ];
  for (const [wire, name] of named) {
    const answer = saveDialogOptions(wire);
    assert.deepStrictEqual(answer, { defaultPath: name }, JSON.stringify(wire));
    // The contract, for every one of them: a separator and a control character are the two
    // things a file's own name cannot hold, so neither may be carried.
    assert.doesNotMatch(answer.defaultPath, /[/\\]/, `${JSON.stringify(wire)} carried a separator`);
    assert.doesNotMatch(
      answer.defaultPath,
      /[\u0000-\u001f\u007f]/,
      `${JSON.stringify(wire)} carried a control character`,
    );
  }
  // Nothing left to name: a trailing separator names no file, `.`/`..` are directories, and
  // a name of spaces is a name of nothing.
  for (const wire of ["trailing/", "dir\\", "..", ".", "   "]) {
    assert.deepStrictEqual(saveDialogOptions(wire), {}, JSON.stringify(wire));
  }
});

test("the view's box comes from the renderer's measurement, rounded", () => {
  assert.deepStrictEqual(
    browserRect({ x: 10.4, y: 20.6, width: 300.5, height: 400.4 }),
    { x: 10, y: 21, width: 301, height: 400 },
  );
  // A collapsed column measures zero, and that is an instruction to hide rather than a box
  // to set: a view of size 0 still takes part in layout.
  assert.strictEqual(browserRect({ x: 0, y: 0, width: 0, height: 400 }), null);
  assert.strictEqual(browserRect({ x: 0, y: 0, width: 300, height: 0 }), null);
  // An unmeasured or partly measured box is not a box.
  assert.strictEqual(browserRect(null), null);
  assert.strictEqual(browserRect(undefined), null);
  assert.strictEqual(browserRect({}), null);
  assert.strictEqual(browserRect({ x: 0, y: 0, width: 300 }), null);
  assert.strictEqual(browserRect({ x: 0, y: 0, width: Number.NaN, height: 400 }), null);
  assert.strictEqual(browserRect({ x: 0, y: 0, width: "300", height: 400 }), null);
  // One pixel is a box: the smallest visible view is still a view.
  assert.deepStrictEqual(browserRect({ x: 0, y: 0, width: 1, height: 1 }), { x: 0, y: 0, width: 1, height: 1 });
});

test("a main-frame navigation is judged from the event object, not from positional arguments", () => {
  // What Electron 31 actually passes: one object carrying `url` and `isMainFrame`.
  assert.strictEqual(
    blocksMainFrameNavigation({ url: "https://example.com/", isMainFrame: true }),
    false,
    "a page this browser draws is not blocked",
  );
  assert.strictEqual(
    blocksMainFrameNavigation({ url: "javascript:alert(1)", isMainFrame: true }),
    true,
    "a scheme outside NAV_SCHEMES must be cancelled in the main frame",
  );
  assert.strictEqual(blocksMainFrameNavigation({ url: "data:text/html,<p>x", isMainFrame: true }), true);
  // A subframe is not this policy's subject: a page may frame what it likes.
  assert.strictEqual(blocksMainFrameNavigation({ url: "javascript:alert(1)", isMainFrame: false }), false);
  // The shape the wiring once assumed — four positional arguments — reaches here as no details
  // at all. Answering `false` for it is the whole defect: the guard silently allowed
  // everything, so this arm is the regression, not a nicety.
  assert.strictEqual(blocksMainFrameNavigation(undefined, "javascript:alert(1)", false, true), false);
  assert.strictEqual(blocksMainFrameNavigation(null), false);
  assert.strictEqual(blocksMainFrameNavigation({}), false);
  // `isMainFrame` must be the boolean true, not a truthy stand-in.
  assert.strictEqual(blocksMainFrameNavigation({ url: "javascript:alert(1)", isMainFrame: "yes" }), false);
});
