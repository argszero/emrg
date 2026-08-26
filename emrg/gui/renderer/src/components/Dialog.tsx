import { useEffect, type CSSProperties, type ReactNode } from "react";

/**
 * Dialog — 共享模态框外壳（Batch 4 slice 1，P4 slice 5）。
 * 从 vanilla `js/dialogs.js` 的 `<dialog showModal>` 语义迁为 React 受控组件：
 *
 * - jsdom 无 showModal（实测 undefined）→ 用 fixed 遮罩 div + .dialog-card 卡片，
 *   复用 vanilla components.css 的 .dialog-card/.dialog-actions 类（Batch 5 CSS 直接生效）。
 * - 遮罩样式内联（对应 vanilla dialog::backdrop：rgba(0,0,0,0.35) + blur(2px)）。
 * - ESC → onClose（对应 vanilla <dialog> 的 cancel 事件）；open=false 不渲染。
 * - 行为保真：不点遮罩关闭（vanilla 仅取消按钮 + ESC 可关）。
 */
export interface DialogProps {
  open: boolean;
  title?: string;
  onClose?: () => void;
  children?: ReactNode;
  /** 底部按钮区（.dialog-actions 内） */
  actions?: ReactNode;
  testId?: string;
}

const overlayStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: 1000,
  background: "rgba(0,0,0,0.35)",
  backdropFilter: "blur(2px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

export function Dialog({ open, title, onClose, children, actions, testId }: DialogProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="dialog-overlay"
      style={overlayStyle}
      data-testid={testId ?? "dialog"}
      role="dialog"
      aria-modal="true"
    >
      <div className="dialog-card">
        {title ? <h2>{title}</h2> : null}
        {children}
        {actions ? <div className="dialog-actions">{actions}</div> : null}
      </div>
    </div>
  );
}
