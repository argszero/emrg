import { describe, expect, it, vi } from "vitest";
import { createSnapshotStore } from "./snapshot-store";

describe("createSnapshotStore", () => {
  it("get returns the initial value", () => {
    const store = createSnapshotStore(0);
    expect(store.get()).toBe(0);
  });

  it("set replaces the value and notifies listeners", () => {
    const store = createSnapshotStore(0);
    const listener = vi.fn();
    store.subscribe(listener);
    store.set(1);
    expect(store.get()).toBe(1);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("does not notify when Object.is-equal (same reference)", () => {
    const store = createSnapshotStore({ a: 1 });
    const listener = vi.fn();
    store.subscribe(listener);
    store.set(store.get()); // 同引用 → 不通知
    expect(listener).not.toHaveBeenCalled();
    store.set({ a: 1 }); // 不同引用 → 通知
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("update applies the updater and notifies only on change", () => {
    const store = createSnapshotStore(1);
    const listener = vi.fn();
    store.subscribe(listener);
    store.update((n) => n + 1);
    expect(store.get()).toBe(2);
    expect(listener).toHaveBeenCalledTimes(1);
    store.update((n) => n); // 返回相同值 → 不通知
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("unsubscribe stops notifications", () => {
    const store = createSnapshotStore(0);
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    unsubscribe();
    store.set(1);
    expect(listener).not.toHaveBeenCalled();
  });
});
