import { useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import { Dialog } from "./Dialog";
import type { SandboxRootsNotice } from "../lib/daemonBridge";

/**
 * SandboxRootsDialog — the GUI entry to a session's host-named extra writable
 * roots (rant 2026-10-09T09:43:39, GUI half).
 *
 * The middle tier the mode table was missing: a `workspace-write` session could
 * reach a file outside its workspace only by lifting *every* boundary
 * (`danger-full-access`). A root is the narrower instrument — named per session,
 * visible, removable — and this is where the GUI host names one.
 *
 * **Data-injected, like every other dialog here**: `roots` is the daemon's list
 * as the bridge last heard it (`null` = nobody has asked), and `notice` is what
 * the daemon said about the last op. Nothing is written locally: a click calls
 * `onOp`, the daemon judges the path against the tier in force and answers with a
 * `sandbox_roots` frame, and that frame — not an optimistic edit — is what moves
 * the list. A client that moved its own copy first would be right about itself
 * and wrong every time the daemon refused, and it refuses on rules no client can
 * restate (a path it cannot read, a protected file under `~/.emrg`, `/`, `$HOME`).
 *
 * The three ops mirror the TUI's `/sandbox add|remove|list` one for one, because
 * they are one protocol with two front ends: `list` on open (the daemon answers
 * the asking connection and broadcasts nothing, so a read must be asked for),
 * `add` with the typed path, `remove` per row.
 */
export interface SandboxRootsDialogProps {
  open: boolean;
  /** The session the ops address; null disables every control (no session, no command). */
  sid: string | null;
  /** The daemon's list, or null while nobody has asked. */
  roots?: string[] | null;
  /** What the daemon said about the last op for this session, if anything. */
  notice?: SandboxRootsNotice | null;
  onOp?: (op: "add" | "remove" | "list", path: string) => void;
  onDismiss?: () => void;
}

export function SandboxRootsDialog({
  open,
  sid,
  roots = null,
  notice = null,
  onOp,
  onDismiss,
}: SandboxRootsDialogProps) {
  const { t } = useI18n();
  const [path, setPath] = useState("");
  const addRef = useRef<HTMLInputElement>(null);
  // The list is the daemon's and a read is a *message*, so asking is a side effect
  // of opening rather than a render-time read. Guarded on `sid`: a root is a
  // session property, so with no session there is nothing to ask about and the
  // command would only be refused for a missing `session_id`.
  //
  // Re-asking on every open is the point: the daemon is the only writer, and the
  // TUI (or another GUI) may have changed the list since this dialog last opened.
  useEffect(() => {
    if (!open || !sid) return;
    onOp?.("list", "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sid]);

  const canWrite = Boolean(sid);
  const submitAdd = (): void => {
    const value = path.trim();
    // An empty path is not sent: the daemon would refuse it, and a refusal the
    // host can see coming is noise in the one line that also carries real ones.
    if (!value) return;
    onOp?.("add", value);
    setPath("");
    addRef.current?.focus();
  };

  return (
    <Dialog
      open={open}
      title={t("sandboxRoots.title")}
      onClose={onDismiss}
      testId="sandbox-roots-dialog"
      actions={
        <button type="button" className="btn btn-ghost" data-testid="sandbox-roots-close" onClick={onDismiss}>
          {t("help.close")}
        </button>
      }
    >
      <p style={{ color: "var(--text-2)", fontSize: "var(--fs-secondary)", margin: "0 0 var(--sp-3)" }}>
        {t("sandboxRoots.desc")}
      </p>
      <label>
        <span>{t("sandboxRoots.pathLabel")}</span>
        <input
          ref={addRef}
          type="text"
          data-testid="sandbox-roots-path"
          placeholder={t("sandboxRoots.pathPlaceholder")}
          value={path}
          disabled={!canWrite}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submitAdd();
            }
          }}
        />
      </label>
      <div style={{ margin: "var(--sp-2) 0" }}>
        <button
          type="button"
          className="btn btn-primary"
          data-testid="sandbox-roots-add"
          disabled={!canWrite || path.trim() === ""}
          onClick={submitAdd}
        >
          {t("sandboxRoots.add")}
        </button>
      </div>
      <div className="help-list" data-testid="sandbox-roots-list">
        {!canWrite ? (
          <div className="help-row" data-testid="sandbox-roots-nosession">
            <span className="help-hint">{t("sandboxRoots.noSession")}</span>
          </div>
        ) : roots === null ? (
          <div className="help-row" data-testid="sandbox-roots-loading">
            <span className="help-hint">{t("dlg.loading")}</span>
          </div>
        ) : roots.length === 0 ? (
          <div className="help-row" data-testid="sandbox-roots-empty">
            <span className="help-hint">{t("sandboxRoots.empty")}</span>
          </div>
        ) : (
          roots.map((root) => (
            <div
              className="help-row"
              data-testid="sandbox-roots-row"
              data-root={root}
              key={root}
              style={{ display: "flex", alignItems: "center", gap: "var(--sp-2)" }}
            >
              <span className="help-hint" title={root} style={{ flex: 1, wordBreak: "break-all" }}>
                {root}
              </span>
              <button
                type="button"
                className="btn btn-ghost"
                data-testid="sandbox-roots-remove"
                data-root={root}
                title={t("sandboxRoots.removeTitle", { path: root })}
                aria-label={t("sandboxRoots.removeTitle", { path: root })}
                onClick={() => onOp?.("remove", root)}
              >
                {t("sandboxRoots.remove")}
              </button>
            </div>
          ))
        )}
      </div>
      {notice ? (
        <div
          className="help-row"
          data-testid="sandbox-roots-notice"
          data-kind={notice.kind}
          role={notice.kind === "error" ? "alert" : undefined}
        >
          <span className="help-hint">
            {notice.kind === "ok"
              ? t("sandboxRoots.done", { op: notice.op })
              : notice.text}
          </span>
        </div>
      ) : null}
    </Dialog>
  );
}
