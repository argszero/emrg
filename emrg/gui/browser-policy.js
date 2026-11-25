"use strict";
/**
 * browser-policy.js — the embedded browser's decisions, as pure functions.
 *
 * The GUI has one hardened view — `nav-policy.js`, where an external URL is handed to the
 * system browser because a remote page inside the privileged window would inherit the whole
 * `window.emrg` bridge — and this browser, which exists to do the opposite: draw a remote
 * page inside the app. Two things keep the second from being a hole in the first:
 *
 *  - it is a **different** `WebContentsView`, built with `sandbox: true`,
 *    `contextIsolation: true` and no preload, so nothing it loads can reach the bridge;
 *  - every policy below is installed on the browser's **own partition**, never on the
 *    default session. `setPermissionRequestHandler`, `will-download`,
 *    `setCertificateVerifyProc` and `clearStorageData` hang off a `Session`, so a handler
 *    installed on the default session would also govern the app's own renderer: one granted
 *    camera permission would be granted to the UI, and one cleared browser store would take
 *    the column widths and the language with it.
 *
 * `main.js` is `node --check`ed and statically guarded, never executed by a test. So the
 * decisions live here, where a test can reach them, and `test/browser-wiring.test.js` pins
 * that `main.js` installs these and invents none of its own.
 */

/**
 * The browser's own partition.
 *
 * The `persist:` prefix is what makes it durable: Electron stores a persisted partition
 * under the app's userData as `Partitions/<name>/`, so this one is
 * `Partitions/emrg-browser/` — one directory, nameable, inspectable, deletable on its own,
 * and untouched by anything the app's own session writes.
 */
const BROWSER_PARTITION = "persist:emrg-browser";

/**
 * The schemes a page may navigate to, in the main frame or through a new window.
 *
 * `file:` is here because browser mode is entered from a local `.html` tab and starts on
 * that file. `about:` is here because a page opens `about:blank` before it writes into it;
 * refusing that breaks pages that build a document in a frame.
 */
const NAV_SCHEMES = ["http:", "https:", "file:", "about:"];

/** Hosts that mean "this machine", which is served over http rather than https. */
const LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]", "0.0.0.0"];

/** The scheme of a URL, or `null` when the text is not one. */
function schemeOf(url) {
  try {
    return new URL(url).protocol;
  } catch {
    return null;
  }
}

/** Whether a page may go to this URL at all. */
function navAllowed(url) {
  if (typeof url !== "string" || url.length === 0) return false;
  const scheme = schemeOf(url);
  return scheme !== null && NAV_SCHEMES.includes(scheme);
}

/**
 * What a page's navigation policy answers for a URL: `"allow"` or `"deny"`.
 *
 * A denial here is a main-frame navigation being cancelled, so the browser shows the page
 * it was on rather than replacing itself with something it cannot render.
 */
function browserNavPolicy(url) {
  return navAllowed(url) ? "allow" : "deny";
}

/**
 * Whether a `will-frame-navigate` is to be cancelled, from the event Electron actually passes.
 *
 * The listener receives **one** argument — the event object, carrying `url` and `isMainFrame`
 * as properties (`WebContentsWillFrameNavigateEventParams` in Electron 31's own declarations) —
 * and not the older `(event, url, isInPlace, isMainFrame)`. Reading the positional form leaves
 * both names `undefined`, so the test collapses to `undefined && …`: falsy, and every main-frame
 * navigation the policy exists to refuse is quietly allowed. Taking the object keeps the
 * decision pure, so a test can hand it the shape the runtime hands it.
 */
function blocksMainFrameNavigation(details) {
  if (!details || typeof details !== "object") return false;
  if (details.isMainFrame !== true) return false;
  return browserNavPolicy(details.url) !== "allow";
}

/**
 * What a page's request for a new window becomes: the URL to load **in the same view**, or
 * `null` when the request is refused outright.
 *
 * A second Electron window would be a window with no policy on it, so `window.open` and
 * `target="_blank"` are turned into an in-place navigation rather than honoured.
 */
function newWindowTarget(url) {
  return navAllowed(url) ? url : null;
}

/**
 * What the address bar's text means: `{ ok: true, url }`, or `{ ok: false, error }` with one
 * of `"empty"`, `"scheme"`, `"invalid"`.
 *
 * Typed text is not a URL. `example.com` and `localhost:3000` are what a person writes, and
 * `new URL` throws on the first and reads the second as a `localhost:` protocol. So a text
 * with no usable scheme is read as a host: `https` by default, `http` when it names this
 * machine (a page served from a local directory is not usually behind TLS), and a scheme the
 * browser cannot render — `javascript:`, `data:` — is refused rather than repaired.
 */
function addressTarget(raw) {
  if (typeof raw !== "string") return { ok: false, error: "empty" };
  const text = raw.trim();
  if (text.length === 0) return { ok: false, error: "empty" };

  const match = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(text);
  const scheme = match ? `${match[1].toLowerCase()}:` : null;
  if (scheme !== null && NAV_SCHEMES.includes(scheme)) {
    // A rendered scheme still has to name something. `http://` is a scheme and no host — and
    // `new URL` refuses it outright, which is why the scheme is read from the text here and
    // not from a successful parse. `about:blank` names no host and is a page all the same, so
    // only the two network schemes are held to it.
    if (scheme === "http:" || scheme === "https:") {
      try {
        if (!new URL(text).host) return { ok: false, error: "invalid" };
      } catch {
        return { ok: false, error: "invalid" };
      }
    }
    return { ok: true, url: text };
  }
  if (scheme !== null) {
    // `localhost:3000` arrives here as the protocol `localhost:`; anything whose colon is
    // followed by a port number is a host, and the rest is a scheme the browser cannot load.
    if (!/^\d+(\/|$|\?|#)/.test(text.slice(match[0].length))) return { ok: false, error: "scheme" };
  }

  const host = text.split(/[/?#]/)[0].replace(/:\d+$/, "").toLowerCase();
  const local = LOCAL_HOSTS.includes(host) || /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
  const url = `${local ? "http" : "https"}://${text}`;
  if (schemeOf(url) === null) return { ok: false, error: "invalid" };
  return { ok: true, url };
}

/**
 * Whether a page's permission request is granted. Never.
 *
 * Camera, microphone, notifications, geolocation and clipboard reads are the five this
 * answers for by name; every other permission gets the same answer, because a default that
 * has to be added to is a default that grants what nobody thought about. Answering `false`
 * here is also what suppresses the system prompt: no prompt, no grant.
 */
function permissionGranted() {
  return false;
}

/**
 * The certificate verdict handed back to Chromium: `-3`, which means "use your own
 * verification result".
 *
 * The other two values are wrong for this browser on purpose: `0` accepts every certificate
 * including a self-signed one, and `-2` rejects every certificate including a valid one. `-3`
 * leaves the decision where it belongs, so an invalid certificate fails and the page shows
 * the error — which is the whole of what this browser is willing to do about it.
 */
function certificateResult() {
  return -3;
}

/**
 * What a download's save dialog is pre-filled with.
 *
 * Only the file's own name: the directory stays the dialog's to ask. The name is not the
 * dialog's, and it is not this app's either — `main.js` hands it `item.getFilename()`, which
 * Electron derives from the response's `Content-Disposition`, and the remote page writes that
 * header while the person clicking Save does not. So the name is read as what it is: a claim
 * from elsewhere. Its last segment is what can be a file's own name, both separators count
 * (`/` and `\` — the name crosses no filesystem boundary this code can see), and C0/DEL go,
 * because no file name contains them and a newline is how one name is stored as two. A name
 * with nothing left after that pre-fills nothing, which is this function's answer for "no
 * name" and not a second policy.
 *
 * `main.js` must never call `setSavePath`, because calling it is what turns the dialog into a
 * silent write; the typing leaves the path unset, and Electron's own routine then shows the
 * dialog.
 */
function saveDialogOptions(filename) {
  if (typeof filename !== "string") return {};
  const lastSegment = filename.split(/[\\/]/).pop() ?? "";
  const name = lastSegment.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  // `..` and `.` are directory references, not names; a row of dots is nothing to pre-fill.
  if (name.length === 0 || /^\.+$/.test(name)) return {};
  return { defaultPath: name };
}

/**
 * Where the browser view goes, from the box the renderer measured, or `null` when there is
 * no box to fill.
 *
 * `null` is the caller's instruction to **hide** the view rather than to set a zero-sized
 * one: a zero-sized view still takes part in layout, and the column being collapsed is a
 * reason to keep the page alive off screen, not to shrink it to a point.
 */
function browserRect(box) {
  if (!box || typeof box !== "object") return null;
  const pick = (value) => (typeof value === "number" && Number.isFinite(value) ? Math.round(value) : null);
  const x = pick(box.x);
  const y = pick(box.y);
  const width = pick(box.width);
  const height = pick(box.height);
  if (x === null || y === null || width === null || height === null) return null;
  if (width < 1 || height < 1) return null;
  return { x, y, width, height };
}

module.exports = {
  BROWSER_PARTITION,
  NAV_SCHEMES,
  addressTarget,
  blocksMainFrameNavigation,
  browserNavPolicy,
  browserRect,
  certificateResult,
  navAllowed,
  newWindowTarget,
  permissionGranted,
  saveDialogOptions,
};
