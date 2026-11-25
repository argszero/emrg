"use strict";
/**
 * config_fields.js — the GUI's half of `~/.emrg/config.toml`: which fields exist, how each is
 * validated, and how each is written.
 *
 * **Why this module exists.** `main.js` kept a whitelist of five string keys plus `models`, and
 * the writer set only the keys that survived it. A field the renderer sent but the whitelist did
 * not name was dropped without an error: the save reported success and the file did not move. So
 * twelve of the seventeen fields in `emrg/config.py` — nine under `[llm]`, both of `[update]`,
 * and `[sandbox] pwsh_path` — could only be changed by hand-editing TOML, and a host who typed
 * them into a form would have seen nothing happen (issue #3221).
 *
 * The cure is one list, `config-fields.json`, read by three places instead of three lists kept in
 * step by hand: this module (validation and writing), `main.js` (which calls it), and
 * `tests/test_config_field_coverage.py` (which compares it against the dataclasses in
 * `emrg/config.py` and reddens on a field with no entry). The renderer draws a control for every
 * `control: "auto"` entry out of the same list, which is why adding a field is one JSON entry
 * rather than a JSON entry, a form field, a whitelist line and a test.
 *
 * A `control: "covered"` entry is a `config.py` field that a *different* entry's control already
 * reaches: `[llm] vision` is one file key feeding both `vision` (the effective value) and
 * `vision_default` (the fallback), so there is one checkbox for it and the second field names the
 * first in `coveredBy` rather than growing a second control for the same key.
 */

const FIELDS = require("./config-fields.json").fields;

/** The entries a form draws a control for. */
const AUTO_FIELDS = FIELDS.filter((f) => f.control === "auto");

/**
 * The entry an IPC field name names, or `null`.
 *
 * Only entries the renderer can send are found: a `covered` entry has no `ipc` of its own.
 */
function fieldByIpc(name) {
  return AUTO_FIELDS.find((f) => f.ipc === name) || null;
}

/** The entries a given settings page carries, in file order. */
function fieldsOnTab(tab) {
  return AUTO_FIELDS.filter((f) => f.tab === tab);
}

/**
 * Read `entry`'s value out of a parsed `config.toml`, or its declared default.
 *
 * A value the file does not carry answers the default rather than `undefined`, because the form
 * shows what the daemon is actually running on: an absent key and a key set to its default are
 * the same setting, and showing an empty box for the first would read as "not set" when the
 * daemon is using 8192.
 */
function readField(toml, entry) {
  const section = (toml || {})[entry.section] || {};
  if (!(entry.key in section)) return clone(entry.default);
  const raw = section[entry.key];
  if (entry.kind === "usage") return Boolean(raw && raw.include_usage);
  return raw;
}

/**
 * Validate and coerce one value, without touching the file.
 *
 * Returns `{ ok: true, value }` or `{ ok: false, message }` — never throws, so a caller can
 * report the refusal beside the control the value was typed into. The message names the field as
 * `[section] key`, because that is the spelling the host will find in `config.toml`.
 */
function coerce(entry, raw) {
  const where = `[${entry.section}] ${entry.key}`;
  switch (entry.kind) {
    case "int": {
      const n = asNumber(raw);
      if (n === null || !Number.isInteger(n)) return { ok: false, message: `${where} wants a whole number, got ${show(raw)}` };
      return inRange(entry, n, where);
    }
    case "float": {
      const n = asNumber(raw);
      if (n === null) return { ok: false, message: `${where} wants a number, got ${show(raw)}` };
      return inRange(entry, n, where);
    }
    case "bool":
      return { ok: true, value: asBool(raw) };
    case "usage": {
      // One checkbox decides `include_usage`; the object shape stays the file's business.
      return { ok: true, value: { include_usage: asBool(raw) } };
    }
    case "text":
      return { ok: true, value: raw === undefined || raw === null ? "" : String(raw) };
    default:
      return { ok: false, message: `${where} has no declared type, so it cannot be validated` };
  }
}

/** A number from a value a form sends, or `null` when it is not one. */
function asNumber(raw) {
  if (typeof raw === "number") return Number.isFinite(raw) ? raw : null;
  if (typeof raw !== "string") return null;
  const text = raw.trim();
  if (text === "") return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

/** A flag from a value a form sends. Absent reads as false, never as "keep the old one". */
function asBool(raw) {
  if (typeof raw === "boolean") return raw;
  if (typeof raw === "string") return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
  return Boolean(raw);
}

function inRange(entry, n, where) {
  if (entry.min !== undefined && n < entry.min) return { ok: false, message: `${where} is at least ${entry.min}, got ${n}` };
  if (entry.max !== undefined && n > entry.max) return { ok: false, message: `${where} is at most ${entry.max}, got ${n}` };
  return { ok: true, value: n };
}

/** How a value reads inside a refusal, with a string quoted so `"abc"` and `abc` differ. */
function show(raw) {
  if (typeof raw === "string") return JSON.stringify(raw);
  return String(raw);
}

function clone(value) {
  return value === undefined ? value : JSON.parse(JSON.stringify(value));
}

/**
 * Write one validated value into a parsed `config.toml`, in place.
 *
 * Returns whether anything moved, so the caller can report "saved, nothing changed" honestly.
 * The section is created when absent and left alone otherwise: this is a read-modify-write of the
 * whole file, so every key this call does not name survives.
 */
function writeField(toml, entry, value) {
  toml[entry.section] = toml[entry.section] || {};
  const before = toml[entry.section][entry.key];
  if (entry.kind === "usage") {
    const next = value || { include_usage: false };
    const same = Boolean(before && before.include_usage) === Boolean(next.include_usage);
    if (same) return false;
    toml[entry.section][entry.key] = { include_usage: Boolean(next.include_usage) };
    return true;
  }
  if (sameValue(before, value)) return false;
  toml[entry.section][entry.key] = value;
  return true;
}

function sameValue(before, value) {
  if (before === value) return true;
  if (typeof before === "object" && typeof value === "object") {
    return JSON.stringify(before) === JSON.stringify(value);
  }
  return false;
}

/**
 * Validate every `control: "auto"` value the caller sent and write it, in place.
 *
 * Returns `{ ok: true, changed }` or `{ ok: false, field, message }` — the first refusal wins and
 * **nothing is written**, because `validate` runs over every value before `write` touches the
 * file. A caller therefore cannot half-save: either the whole form is acceptable or the file is
 * byte-identical.
 */
function applyAll(toml, payload) {
  const parsed = [];
  for (const entry of AUTO_FIELDS) {
    if (!payload || !(entry.ipc in payload)) continue;
    const result = coerce(entry, payload[entry.ipc]);
    if (!result.ok) return { ok: false, field: entry.ipc, message: result.message };
    parsed.push([entry, result.value]);
  }
  let changed = 0;
  for (const [entry, value] of parsed) {
    if (writeField(toml, entry, value)) changed += 1;
  }
  return { ok: true, changed };
}

module.exports = { FIELDS, AUTO_FIELDS, fieldByIpc, fieldsOnTab, readField, coerce, writeField, applyAll };
