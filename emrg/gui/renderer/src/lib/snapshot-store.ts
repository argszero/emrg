/**
 * snapshot-store.ts — 自研轻量 store（设计文档 §4.2，决策 D2 定案；grok-bot
 * SnapshotStore 模式，零依赖）。
 *
 * 与 React 无关：current + listeners Set + Object.is 变更检测 + set/update。
 * 组件订阅用 useSyncExternalStore（见 ../hooks/useSnapshotStore.ts）。
 * 后续按 sid 建多个 store 实例（"每会话独立状态表"），事件按 sessionsBySid 键控，
 * 严格防会话串线回归（#977）。
 */
export interface SnapshotStore<Value> {
  get(): Value;
  subscribe(listener: () => void): () => void;
  set(value: Value): void;
  update(updater: (current: Value) => Value): void;
}

export function createSnapshotStore<Value>(initial: Value): SnapshotStore<Value> {
  let current = initial;
  const listeners = new Set<() => void>();

  function emit(): void {
    for (const listener of listeners) listener();
  }

  return {
    get: () => current,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    set: (value) => {
      if (Object.is(current, value)) return;
      current = value;
      emit();
    },
    update: (updater) => {
      const next = updater(current);
      if (Object.is(current, next)) return;
      current = next;
      emit();
    },
  };
}
