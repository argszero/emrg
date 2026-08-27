import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import {
  createDaemonBridge,
  type DaemonBridge,
  type DaemonEventFrame,
  type InitResult,
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
 * - 🔧 Batch 5 修复（rant 2026-08-27T10:53:38）：挂载时调用 window.emrg.init() 并把
 *   返回值（connected/sessions/openSessions/model/serverId/evolutionCount 等）融合进
 *   bridge store。vanilla boot（app.js run 调 window.emrg.init()，line 73）用返回值
 *   初始化 config/sessions/open_sessions/model/connected；React 版此前只订阅
 *   onEvent/sendMessage、从未调 init → main.js ensureConnected() 从不执行 → GUI 永不
 *   连 daemon（常显断连）。此改动恢复 renderer 的 init 职责，不再依赖 main 兜底拉起。
 */

export interface DaemonBridgeContextValue {
  bridge: DaemonBridge;
  transcript: TranscriptStore;
}

const DaemonBridgeContext = createContext<DaemonBridgeContextValue | null>(null);

/** preload.js 暴露的 window.emrg 最小形状（init/onEvent/sendMessage 三个通道） */
interface EmrgWindow {
  emrg?: {
    init?: () => Promise<InitResult>;
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
        init: async () => {
          const emrg = (window as unknown as EmrgWindow).emrg;
          if (!emrg?.init) return {};
          return emrg.init();
        },
        sendMessage: async (p) => {
          const emrg = (window as unknown as EmrgWindow).emrg;
          if (!emrg?.sendMessage) return { requestId: p.requestId };
          return emrg.sendMessage(p);
        },
      },
      transcript,
      t: tt,
    });
    // 🔧 Batch 5 修复：挂载即 init，把 init 返回值（连接状态/会话列表/model 等）
    // 融合进 store。vanilla boot 语义：init 成功（config_exists && api_key_configured）
    // 才置 connected=true；config/key 缺失则由 main 返回 config_exists=false /
    // api_key_configured=false → connected 保持 false（UI 显示"未配置"降级，不崩）。
    void (async () => {
      try {
        const result = await (window as unknown as EmrgWindow).emrg?.init?.();
        if (result) bridge.applyInit(result);
      } catch (e) {
        // init 失败（daemon 未就绪/通道缺失）：静默降级，连接状态由后续事件驱动
        console.error("[emrg] gui init failed", e);
      }
    })();
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
