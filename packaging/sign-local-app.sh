#!/usr/bin/env bash
# sign-local-app.sh — seal the LOCALLY built EMRG.app with a STABLE identity.
#
# Why this exists (rant 2026-10-16T12:03:58, issue #2322):
#
# An ad-hoc signature has no certificate, so macOS can only key the application by
# the hash of its binary — `codesign -d -r-` then answers
#
#     designated => cdhash H"3a4551473dac3fb487755037793394d17d7f6c4d"
#
# Every upgrade rebuilds the bundle (upgrade_prompt.j2 step 3) and re-seals it, so
# that hash changes with every upgrade, and macOS reads the result as a *different
# application*: every TCC grant the host made — Screen Recording, Accessibility,
# Files & Folders, … — stops matching, and the host is asked to authorise EMRG
# again after every single upgrade. The TCC log says it in one line:
#
#     Failed to match existing code requirement for subject com.emrg.gui
#     cdhash H"e93edd27…"   ← the grant recorded in System Settings
#     cdhash H"e8025621…"   ← the app that is now running
#
# Signing with a certificate moves the designated requirement onto that
# certificate, which a rebuild does not change:
#
#     designated => identifier "com.emrg.gui" and anchor apple generic and
#                   certificate leaf[subject.OU] = "V5N9YB9H35"
#
# Re-sealing is still required — an unsealed bundle carrying com.apple.provenance
# is moved to the Trash on macOS 26 (rant 2026-08-25T09:18:19). The defect this
# script fixes is *which identity* the re-seal uses, not whether it happens.
#
# The identity is chosen HERE rather than by the agent reading upgrade_prompt.j2:
# an identity chosen per upgrade is one chosen differently per upgrade, which is
# the same defect with extra steps.
#
# Usage:
#   packaging/sign-local-app.sh <path-to-EMRG.app>
#
# Environment:
#   EMRG_SIGN_IDENTITY   the full identity name to sign with (e.g.
#                        "Apple Development: Created via API (V5N9YB9H35)").
#                        Overrides the discovery order below. Use it when the
#                        machine holds several identities and you want to pin one.
#
# Identity discovery order (first match wins) — all three give a certificate-based
# designated requirement, so they are equally stable; the order prefers the
# longest-lived, most release-like identity when one is installed:
#   1. Developer ID Application   (the release identity; multi-year)
#   2. Apple Development          (present on any machine able to build the app)
#   3. Apple Distribution
#
# Exit codes (the house contract: 0 ok / 1 fault / 2 could not measure):
#   0  the bundle is sealed and `codesign --verify --deep --strict` passed, with a
#      stable identity or — loudly — with ad-hoc; the upgrade proceeds
#   1  the bundle exists but does not verify after signing: do NOT copy it, roll
#      back (an unsealed bundle is the artifact macOS 26 trashes)
#   2  `codesign` is not available, so nothing was signed and nothing verified:
#      the upgrade proceeds with the artifact exactly as the build left it
#
# Deliberately NOT done here:
#   * `--options runtime` is never passed. Hardening the local build would let
#     library validation block Electron's bundled native libraries, and the
#     current ad-hoc re-seal does not pass it either — so this change cannot
#     change whether the GUI launches (issue #2322 records this).
#   * No notarisation and no timestamp server: `--timestamp=none` keeps an
#     offline upgrade working, and a locally built app carries no quarantine
#     attribute, so Gatekeeper never asks it to be notarised.
#
# No output is produced on a healthy run beyond the two lines below; the
# `sign-local-app: identity=` line is REQUIRED to appear in the upgrade log
# (upgrade_prompt.j2 step 3e), because "which identity sealed this build" is the
# question the next upgrade's TCC behaviour depends on.

set -euo pipefail

PREFERRED_IDENTITIES=(
  "Developer ID Application"
  "Apple Development"
  "Apple Distribution"
)

usage() {
  echo "usage: $0 <path-to-EMRG.app>" >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 1
fi

APP="$1"

if [ ! -d "$APP" ]; then
  echo "sign-local-app: the bundle does not exist: $APP" >&2
  exit 1
fi

if ! command -v codesign >/dev/null 2>&1; then
  echo "sign-local-app: verdict=could-not-measure (no codesign on PATH — the bundle is untouched)"
  exit 2
fi

# The stable identities this machine holds. `security` prints one line per valid
# signing identity, the name in double quotes:
#   1) C643979F… "Apple Development: Created via API (V5N9YB9H35)"
identities() {
  security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(.*\)".*/\1/p' || true
}

discover_identity() {
  local available="$1" want match
  for want in "${PREFERRED_IDENTITIES[@]}"; do
    match="$(printf '%s\n' "$available" | grep -F -- "$want" | head -n 1 || true)"
    if [ -n "$match" ]; then
      printf '%s' "$match"
      return 0
    fi
  done
  return 0
}

IDENTITY="${EMRG_SIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
  IDENTITY="$(discover_identity "$(identities)")"
fi

if [ -n "$IDENTITY" ]; then
  if codesign --force --deep --sign "$IDENTITY" --timestamp=none "$APP" >/dev/null 2>&1; then
    if codesign --verify --deep --strict "$APP" >/dev/null 2>&1; then
      echo "sign-local-app: identity=${IDENTITY}"
      echo "sign-local-app: the designated requirement now names this certificate, so rebuilding the app does not void the host's macOS privacy grants."
      exit 0
    fi
    echo "sign-local-app: identity=${IDENTITY} signed the bundle but codesign --verify --deep --strict failed — do not copy it." >&2
    exit 1
  fi
  # The stable identity was found but signing with it failed. Fall through to the
  # ad-hoc re-seal rather than blocking the upgrade — but say so, loudly.
  echo "sign-local-app: signing with ${IDENTITY} failed — falling back to ad-hoc." >&2
fi

if ! codesign --force --deep --sign - --timestamp=none "$APP" >/dev/null 2>&1; then
  echo "sign-local-app: verdict=unavailable (neither a stable identity nor the ad-hoc re-seal sealed the bundle) — do not copy it." >&2
  exit 1
fi

if ! codesign --verify --deep --strict "$APP" >/dev/null 2>&1; then
  echo "sign-local-app: identity=adhoc sealed the bundle but codesign --verify --deep --strict failed — do not copy it." >&2
  exit 1
fi

echo "sign-local-app: identity=adhoc — NO STABLE IDENTITY. macOS keys this app by the hash of its binary, so this rebuild is a DIFFERENT app to macOS and the host's privacy grants (Screen Recording, Accessibility, …) WILL BE VOIDED. To stop that, install a codesigning certificate in the login keychain (Developer ID Application, Apple Development or Apple Distribution)."
exit 0
