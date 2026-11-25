/**
 * userImages.ts — 把一条用户消息的正文切回「文字 + 图」交替的几段。
 *
 * 宿主 2026-11-25T18:17:18：GUI 里发出去的图，聊天区只剩 `[📷 label]` 一行字，看不到图
 * 也无处点开。字节一直在 `<cwd>/.emrg/sessions/<sid>/images/` 里，缺的是「把这段占位符
 * 换成图」这一步 —— 占位符本身是发送时由 `composer.imagePlaceholder()` 主动写进正文的
 * （TUI 那一侧是同一个动作），不是渲染失败留下的痕迹。
 *
 * 为什么切分而不是替换字符串：图要出现在**它自己那个位置**，所以正文切成若干段，段与图
 * 交替渲染；文字段仍走 markdown，加粗、代码、链接一个字都不变。
 *
 * 位置只认记录里给的那个 `position`。同一张图连发两次时两个标记字面完全相同，位置是唯一
 * 能把它们分开的东西；对不上就按顺序找下一个同名标记。**找不到标记的图不画** —— 把缩略图
 * 放在一个它不在的位置，比缺一张图更难看出是错的。
 */
import type { ImageAttach } from "./composer";

export type UserTextPart = { kind: "text"; text: string } | { kind: "image"; image: ImageAttach };

/**
 * 一条正文 + 它的图 → 交替的段。
 *
 * 没有图、或图一个都没落进正文时，答案就是原来那一段文字（渲染层据此完全走旧的
 * `MarkdownText` 路径，压缩前的老消息因此与今天看到的一模一样）。
 */
export function splitUserText(text: string, images?: ImageAttach[] | null): UserTextPart[] {
  const body = typeof text === "string" ? text : "";
  const list = (images ?? []).filter(
    (i): i is ImageAttach => !!i && typeof i.label === "string" && i.label.length > 0,
  );
  if (!list.length) return body ? [{ kind: "text", text: body }] : [];

  const parts: UserTextPart[] = [];
  let cursor = 0;
  for (const image of list) {
    const at = locate(body, image.label, image.position, cursor);
    if (at < 0) continue;
    if (at > cursor) parts.push({ kind: "text", text: body.slice(cursor, at) });
    parts.push({ kind: "image", image });
    cursor = at + image.label.length;
  }
  if (cursor < body.length) parts.push({ kind: "text", text: body.slice(cursor) });
  return parts;
}

/** 标记在正文里的位置：先信 `position`（它记的是发送那一刻的偏移），再从游标往后找。 */
function locate(body: string, label: string, position: number | undefined, from: number): number {
  if (
    typeof position === "number" &&
    Number.isFinite(position) &&
    position >= from &&
    body.startsWith(label, position)
  ) {
    return position;
  }
  return body.indexOf(label, from);
}
