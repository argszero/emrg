import { describe, expect, it } from "vitest";
import {
  dialogReducer,
  initialDialogState,
  resolveConfirmButton,
  type ConfirmRequest,
  type RenameRequest,
} from "./dialog";

/**
 * dialog.test.ts — 对话框状态纯逻辑测试（Batch 4 slice 1）。
 * 镜像 vanilla showConfirm/showRename 的模块级状态语义。
 */

describe("dialogReducer", () => {
  it("初始状态：confirm/rename 均为 null", () => {
    expect(initialDialogState).toEqual({ confirm: null, rename: null });
  });

  it("open-confirm 设置 confirm 请求", () => {
    const req: ConfirmRequest = { title: "删除模型", message: "确定？" };
    expect(dialogReducer(initialDialogState, { type: "open-confirm", payload: req })).toEqual({
      confirm: req,
      rename: null,
    });
  });

  it("close-confirm 清空 confirm", () => {
    const s = dialogReducer(initialDialogState, {
      type: "open-confirm",
      payload: { title: "T", message: "M" },
    });
    expect(dialogReducer(s, { type: "close-confirm" }).confirm).toBeNull();
  });

  it("open-rename 设置 rename（confirm 保留，两槽独立）", () => {
    const s = dialogReducer(initialDialogState, {
      type: "open-confirm",
      payload: { title: "T", message: "M" },
    });
    const req: RenameRequest = { sessionId: "s1" };
    expect(dialogReducer(s, { type: "open-rename", payload: req })).toEqual({
      confirm: { title: "T", message: "M" },
      rename: req,
    });
  });

  it("close-rename 清空 rename", () => {
    const s = dialogReducer(initialDialogState, { type: "open-rename", payload: { sessionId: "s1" } });
    expect(dialogReducer(s, { type: "close-rename" }).rename).toBeNull();
  });

  it("open-confirm 覆盖已有 confirm（新请求替换旧请求）", () => {
    const s = dialogReducer(initialDialogState, {
      type: "open-confirm",
      payload: { title: "A", message: "1" },
    });
    const s2 = dialogReducer(s, { type: "open-confirm", payload: { title: "B", message: "2" } });
    expect(s2.confirm).toEqual({ title: "B", message: "2" });
  });

  it("未知 action 返回原状态（不改写）", () => {
    expect(dialogReducer(initialDialogState, { type: "bogus" } as never)).toBe(initialDialogState);
  });
});

describe("resolveConfirmButton", () => {
  it("默认：confirm.delete 文案 + danger 样式（vanilla 默认删除确认）", () => {
    expect(resolveConfirmButton(undefined, undefined)).toEqual({ okText: "confirm.delete", danger: true });
  });

  it("danger:false → settings.save + primary（vanilla opts.danger===false 分支）", () => {
    expect(resolveConfirmButton(undefined, false)).toEqual({ okText: "settings.save", danger: false });
  });

  it("显式 okText 透传（i18n key 或已译字符串均可）", () => {
    expect(resolveConfirmButton("确认", true)).toEqual({ okText: "确认", danger: true });
  });
});
