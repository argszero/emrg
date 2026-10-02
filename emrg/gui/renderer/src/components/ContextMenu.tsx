import { useEffect, useRef, useState } from "react";

/**
 * ContextMenu — 会话列表的右键菜单（vanilla `js/app.js showConvMenu/showOpenSessionsMenu`）。
 *
 * 为什么它存在：`Sidebar` 的 `onContextMenu` 从 Batch 3 起就有文档与实现
 * （`e.preventDefault()` + 交给调用方弹菜单），但 **`Shell` 从来没有传过这个 prop**
 * ——右键点一个会话，原生菜单被吃掉、替代菜单又不出现，于是「什么都不会发生」。
 * 同时 `css/components.css` 里 `.ctx-menu` / `.ctx-item` / `.ctx-item.active`
 * （注释写着「键盘导航高亮（↑↓）」）一直都在，词典里 `app.closeSession` /
 * `app.closeFailed` 也一直在——**样式与词条留下了，元素与读者随 #1024 一起没了**。
 *
 * 行为逐条对齐 vanilla：
 * - 选项点击 → 先关菜单再执行（`mk()` 里的 `hideCtxMenu(); action();`）
 * - ↑/↓ 循环移动 `.active`，Enter 触发当前项，Escape 关闭
 * - 点菜单以外的地方关闭
 * - 定位用**被点元素的右下角**，并夹进视口（vanilla `Math.min(rect.right, innerWidth - 160)`
 *   / `Math.min(rect.bottom, innerHeight - 80)`；160/80 是 CSS `min-width:140px` 加内边距
 *   与三行选项的高度，照抄以免菜单画出屏幕）
 */

export interface ContextMenuItem {
  label: string;
  /** vanilla `mk(label, danger, action)` 的 danger 参数 → `.ctx-item.danger` */
  danger?: boolean;
  onSelect: () => void;
}

export interface ContextMenuProps {
  /** 被点元素的右下角（视口坐标） */
  x: number;
  y: number;
  items: ContextMenuItem[];
  onDismiss: () => void;
}

/** vanilla 的夹取余量：菜单最小宽 140px / 三行选项的高度 */
const CLAMP_X = 160;
const CLAMP_Y = 80;

export function ContextMenu({ x, y, items, onDismiss }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  // vanilla `setActive(0)`：菜单一出现第一项就是高亮的
  const [active, setActive] = useState(0);

  const left = Math.min(x, (typeof window !== "undefined" ? window.innerWidth : x) - CLAMP_X);
  const top = Math.min(y, (typeof window !== "undefined" ? window.innerHeight : y) - CLAMP_Y);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        setActive((i) => (items.length ? (i + 1) % items.length : 0));
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        setActive((i) => (items.length ? (i - 1 + items.length) % items.length : 0));
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        const item = items[active];
        if (item) {
          onDismiss();
          item.onSelect();
        }
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        onDismiss();
      }
    };
    const onClick = (ev: MouseEvent) => {
      const el = ref.current;
      if (el && ev.target instanceof Node && !el.contains(ev.target)) onDismiss();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("click", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("click", onClick);
    };
  }, [items, active, onDismiss]);

  return (
    <div
      ref={ref}
      className="ctx-menu"
      data-testid="ctx-menu"
      role="menu"
      style={{ left, top }}
    >
      {items.map((item, i) => (
        <button
          key={item.label}
          type="button"
          role="menuitem"
          className={`ctx-item${item.danger ? " danger" : ""}${i === active ? " active" : ""}`}
          data-testid="ctx-item"
          onMouseEnter={() => setActive(i)}
          onClick={() => {
            onDismiss();
            item.onSelect();
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
