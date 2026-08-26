import { useSyncExternalStore } from "react";
import type { SnapshotStore } from "../lib/snapshot-store";

/** 组件订阅 hook：store 值变化时重渲染（useSyncExternalStore 保证并发安全） */
export function useSnapshotStore<Value>(store: SnapshotStore<Value>): Value {
  return useSyncExternalStore(store.subscribe, store.get, store.get);
}
