import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import {
  createDaemonBridge,
  type DaemonBridge,
  type DaemonEventFrame,
  type SendMessagePayload,
} from "../lib/daemonBridge";
import { createTranscriptStore, type TranscriptStore } from "../lib/transcript";
import { useI18n, type TranslateFn } from "../lib/i18n";

/**
 * DaemonBridgeProvider — Batch 5 slice 2：daemon-event context 层（设计文档 §4.1
 * AppProviders）。PR #1016 的 lib/daemonBridge.ts 是纯逻辑地基；本组件把它接进
 * React 树：
 *
 * - 创建 TranscriptStore（聊天区状态机，与 TranscriptView 共享同一实例）和
 *   DaemonBridge（订阅 window.emrg.onEvent，发送走 window.emrg.sendMessage）。
 * - context 暴露 { bridge, transcript }；`useDaemonBridge()` 消费（Shell 及后续
 *   slice 的组件用它取会话列表 / 连接状态 / 聊天状态机）。
 * - 桥在 effect 中创建：StrictMode dev 双挂载时 dispose 清理第一次订阅（无泄漏），
 *   生产构建 StrictMode 为 no-op，单次挂载；value 就绪前一帧渲染 null。
 * - 非 Electron 环境（window.emrg 缺失）优雅降级：onEvent 返回 no-op 取消函数、
 *   sendMessage 以传入 requestId resolve —— 浏览器预览 / 单测不崩溃，store 可用。
 */

export interface DaemonBridgeContextValue {
  bridge: DaemonBridge;
  transcript: TranscriptStore;
}

const DaemonBridgeContext = createContext<DaemonBridgeContextValue | null>(null);

/** preload.js 暴露的 window.emrg 最小形状（仅本组件用到的两个通道） */
interface EmrgWindow {
  emrg?: {
    onEvent?: (cb: (evt: DaemonEventFrame) => void) => () => void;
    sendMessage?: (p: SendMessagePayload) => Promise<{ requestId?: string }>;
  };
}

export function DaemonBridgeProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  // t 每次渲染可能是新函数对象（无 I18nProvider 时 useI18n 回退路径每帧新建），
  // 若把 t 放进 effect deps 会无限重建桥（事件处理时翻译需取最新 t）→ 用 ref 桥接：
  // 桥只在挂载时创建一次（deps []），翻译始终委托给 tRef.current（最新词典）。
  const tRef = useRef(t);
  tRef.current = t;
  const [value, setValue] = useState<DaemonBridgeContextValue | null>(null);

  useEffect(() => {
    const tt: TranslateFn = (key, params) => tRef.current(key, params);
    const transcript = createTranscriptStore({ t: tt });
    const bridge = createDaemonBridge({
      onEvent: (cb) => {
        const emrg = (window as unknown as EmrgWindow).emrg;
        return emrg?.onEvent ? emrg.onEvent(cb) : () => {};
      },
      emrg: {
        sendMessage: async (p) => {
          const emrg = (window as unknown as EmrgWindow).emrg;
          if (!emrg?.sendMessage) return { requestId: p.requestId };
          return emrg.sendMessage(p);
        },
      },
      transcript,
      t: tt,
    });
    setValue({ bridge, transcript });
    return () => bridge.dispose();
  }, []);

  if (!value) return null;
  return <DaemonBridgeContext.Provider value={value}>{children}</DaemonBridgeContext.Provider>;
}

/** 消费 hook：返回 { bridge, transcript }；Provider 之外调用抛错（接线错误快速暴露） */
export function useDaemonBridge(): DaemonBridgeContextValue {
  const ctx = useContext(DaemonBridgeContext);
  if (!ctx) throw new Error("useDaemonBridge must be used within DaemonBridgeProvider");
  return ctx;
}
