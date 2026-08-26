/**
 * dialog.ts — 对话框状态纯逻辑（Batch 4 slice 1，P4 slice 5）。
 * 从 vanilla `js/dialogs.js` showConfirm/showRename 迁出为 React 状态管理：
 *
 * - DialogState：confirm/rename 两个请求槽（同 vanilla 的 confirmCb / renameSid
 *   模块级变量，一个时刻最多一个请求在途）。
 * - dialogReducer：open/close 转移，纯函数可测（对齐 transcript.ts 状态机模式）。
 * - resolveConfirmButton：确认按钮文案/样式解析，镜像 vanilla showConfirm：
 *   `ok.textContent = opts.okText || _t("dlg.delete")`、
 *   `ok.className = opts.danger === false ? "btn btn-primary" : "btn btn-danger"`。
 */

export interface ConfirmRequest {
  title: string;
  message: string;
  /** OK 按钮文案（i18n key 或已译字符串；缺省 → confirm.delete） */
  okText?: string;
  /** 默认 danger=true；danger===false → primary 样式（对应 vanilla opts.danger===false） */
  danger?: boolean;
  onOk?: () => void | Promise<void>;
}

export interface RenameRequest {
  sessionId: string;
  /** 当前标题；等于 sid 时视为未命名 → 输入框留空（vanilla 语义） */
  currentTitle?: string;
}

export interface DialogState {
  confirm: ConfirmRequest | null;
  rename: RenameRequest | null;
}

export type DialogAction =
  | { type: "open-confirm"; payload: ConfirmRequest }
  | { type: "close-confirm" }
  | { type: "open-rename"; payload: RenameRequest }
  | { type: "close-rename" };

export const initialDialogState: DialogState = { confirm: null, rename: null };

export function dialogReducer(state: DialogState, action: DialogAction): DialogState {
  switch (action.type) {
    case "open-confirm":
      return { ...state, confirm: action.payload };
    case "close-confirm":
      return { ...state, confirm: null };
    case "open-rename":
      return { ...state, rename: action.payload };
    case "close-rename":
      return { ...state, rename: null };
    default:
      return state;
  }
}

export interface ConfirmButtonSpec {
  okText: string;
  danger: boolean;
}

/** 确认按钮解析（对齐 vanilla showConfirm 的默认值与 danger 语义） */
export function resolveConfirmButton(
  okText: string | undefined,
  danger: boolean | undefined,
): ConfirmButtonSpec {
  const d = danger !== false;
  return { okText: okText || (d ? "confirm.delete" : "settings.save"), danger: d };
}
