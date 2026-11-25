import { describe, expect, it, beforeEach } from "vitest";
import { DICTS, EN_DICT, ZH_DICT, detectLocale, getLocale, setLocale, t, LOCALE_KEY } from "./i18n";

/**
 * i18n.test.ts — 完整词典 + 原生逻辑移植测试（Batch 1 remainder）。
 * 断言镜像旧 emrg/gui/test/i18n.test.js（node:test → Vitest），
 * 并新增词典完整性守卫（394 keys zh/en 严格对齐 —— 防未来词典漂移）。
 */

describe("detectLocale", () => {
  it("zh 前缀 → zh，其他 → en", () => {
    expect(detectLocale("zh-CN")).toBe("zh");
    expect(detectLocale("zh-Hant")).toBe("zh");
    expect(detectLocale("en-US")).toBe("en");
    expect(detectLocale("ja-JP")).toBe("en");
  });
  it("无参（缺省 navigator.language）→ en 或 zh 之一，不抛错", () => {
    expect(["zh", "en"]).toContain(detectLocale());
  });
});

describe("t（词典取词）", () => {
  it("en 语言返回英文文案", () => {
    expect(t("sidebar.newChat", undefined, "en")).toBe("＋ New chat");
    expect(t("copy.disconnected", undefined, "en")).toBe("Connection lost — reconnecting…");
  });
  it("zh 语言返回中文文案", () => {
    expect(t("sidebar.newChat", undefined, "zh")).toBe("＋ 新对话");
    expect(t("copy.disconnected", undefined, "zh")).toBe("连接中断了，正在重新连接…");
  });
  it("{var} 插值", () => {
    expect(t("copy.aboutEvolution", { n: 42 }, "en")).toBe(
      "EMRG has self-evolved 42 times — thanks for every bit of feedback",
    );
    expect(t("copy.aboutEvolution", { n: 7 }, "zh")).toBe("EMRG 已自我成长 7 次，感谢你的每一次反馈");
  });
  it("未知 key → 原样返回", () => {
    expect(t("definitely.missing.key", undefined, "en")).toBe("definitely.missing.key");
  });
});

describe("getLocale / setLocale（localStorage 覆盖）", () => {
  beforeEach(() => {
    try {
      localStorage.removeItem(LOCALE_KEY);
    } catch {
      /* jsdom 不可用时忽略 */
    }
  });

  it("无覆盖 → 跟随系统检测", () => {
    // jsdom 默认 en-US；显式断言 getLocale 返回合法值即可
    expect(["zh", "en"]).toContain(getLocale());
  });

  it("setLocale 持久化到 localStorage 并立即生效", () => {
    const l = setLocale("en");
    expect(l).toBe("en");
    try {
      expect(localStorage.getItem(LOCALE_KEY)).toBe("en");
    } catch {
      /* 无 localStorage 环境跳过持久化断言 */
    }
    expect(t("sidebar.newChat", undefined, getLocale())).toBe("＋ New chat");
  });

  it("空值恢复跟随系统", () => {
    setLocale("en");
    setLocale("");
    const l = getLocale();
    expect(["zh", "en"]).toContain(l);
  });
});

describe("词典完整性守卫（防漂移）", () => {
  // 两个绝对数是有意钉住的：新增/删除 key 必须在这里显式改一次，
  // 否则一次手滑删掉几条词条不会有人发现（下一行的对齐断言只要求 zh/en 一致，
  // 两边同时少一条它照样通过）。2026-09-23 由 `tool.pwsh.*`（Windows 方言，P8）从 397 改为 399；
  // 2026-09-30 由 `tool.detailName/-Input/-Output`（工具详情三段，rant 2026-09-30T09:17:54 → #1787）从 403 改为 406。
  // 2026-09-30 由 `composer.imageReason.*`（图片拒绝必须可见，rant 2026-09-30T09:35:04）从 403 改为 408。
  // 两个改动各加各的 key，互不重叠：合并后是 403 + 3 + 5 = 411（既不是 406 也不是 408）。
  // 2026-10-08 由 `composer.chooseImage`（选图入口，rant 2026-09-30T09:35:04 缺口 1）从 411 改为 412。
  // 2026-10-08 由 `composer.imageReasonClipboard`（系统给的图不受白名单所限，同 rant 要求 4）
  // 从 412 改为 413。
  // 2026-10-09 由 `composer.roots*`（3 条）+ `cmd.sandbox.hint`（1 条）+
  // `sandboxRoots.*`（12 条）——会话额外可写根的 GUI 入口与对话框，rant
  // 2026-10-09T09:43:39 Part 2(a)——从 413 改为 429。
  // 2026-10-11 issue #2113：device flow 的两个提示串 → 431。
  // 2026-10-22 issue #2490：调度窗口 9 个键（含下限插值）→ 440。
  // 2026-11-09 issue #2774：aitokenpool 账号登录 21 个键 → 465。
  // 2026-11-17 issue #2920：输入框改纯文本后，格式栏与链接对话框的 15 个键
  // （chooseImage / formatBar / bold / italic / strike / code / link / bullet /
  // bulletList / ordered / orderedList / quote / heading / linkPrompt /
  // linkDialogTitle）随它们一起删除，附件入口新增 3 个 → 465 - 15 + 3 = 453。
  // 2026-11-17 issue #2924：打开会话对话框两步各加一个搜索框，新增
  // searchAgents / searchSessions / noMatches 3 个键 → 453 + 3 = 456。
  // 2026-11-18 issue #2959：驾驶舱外壳（状态带 / 图标栏 / 会话搜索 / 面板搜索与
  // 行内删除确认）新增 19 个键 → 456 + 19 = 475；rant 行删除的失败提示再加 1 个 → 476；
  // 检查栏三个 tab 与其空态 +7；旧「打开的会话」分组标签 -1；旧结果面板的 result.* 12 个随
  // 组件一起删 → 476 + 7 - 1 - 12 = 469（issue #2969）。
  // 2026-11-18 issue #2980：工具组摘要的四种缺省各一个键（有时间+最新一行 / 只有时间 /
  // 只有最新一行 / 都没有）→ 469 + 3 = 472。
  // 2026-11-18 issue #2986：图标轨的提示与连接点 3 个、会话计数的标签 1 个、设置页五个
  // 分区名 5 个、沙箱与权限两段 3 个、快捷键三行 3 个 → 472 + 14 = 486，issue #3026 的右栏两个文件树把两条一轮清单的 key 换成十条树的 key → 491；
  // 2026-11-19 issue #3037：代码块复制按钮接线，`chat.copyCode` 从来没有读者（按钮文案一直是
  // `md.copyCode`）→ 491 - 1 = 490。
  // 2026-11-19 issue #3039：状态带去掉轮数那一格，它的文案与悬停说明两条键一起删 → 490 - 2 = 488。
  // 2026-11-19 issue #3041：两棵目录树的右键菜单四项（复制完整路径 / 使用默认方式打开 /
  // 已复制 / 复制失败）→ 488 + 4 = 492。
  // 2026-11-19 issue #3050：快捷键页补一行换行键（Shift+Enter / Option+Enter）→ 492 + 1 = 493。
  // 2026-11-20 issue #3064：额外可写根对话框加「选择…」按钮 → 493 + 1 = 494。
  // 2026-11-20 issue #3068：右栏变编辑器——删掉左栏两张树原来的两个标签键（-2），补两段表头
  // 与编辑器五条文案（+7）→ 494 + 5 = 499。
  // 2026-11-20 issue #3078：六条带参数的指令（/delete /rename /resume /model /memory
  // /sandbox）各补一条参数用法，中英各 6 条 → 499 + 6 = 505。
  // 2026-11-20 issue #3089：状态带上那一格的悬停提示「当前会话」中英各 1 条 → 505 + 1 = 506。
  // 2026-11-20 issue #3091：目录那一段的段名、额外根目录、没有根时的「没有」各 1 条
  // → 506 + 3 = 509。
  // 2026-11-20 issue #3093：状态带那一条合起来的读数换成三段各一条（+4），并加上按段排名的
  // 面板文案 14 条，同时删掉原来那一条（-1）→ 509 + 4 + 14 - 1 = 526。
  // 2026-11-20 issue #3095：右栏从只读预览改成编辑器，保存 / 脏点 / 关闭三选一 / 磁盘
  // 改动 / 只读说明 / 两个开关 / 语言与光标角标 +18 → 526 + 18 = 544；
  // 分组工具条的「刷新」「全部收起」随按钮一起删掉 → 544 - 2 = 542。
  // 2026-11-21 issue #3123：排名面板改成挂在状态带读数下的浮层，遮挡与关闭按钮一起删掉，
  // 「关闭」中英各 1 条 → 542 - 1 = 541。
  // 2026-11-21 issue #3125：右栏去掉记忆标签与记忆门，标签名 / 提示语 / 打开按钮
  // 三条文案中英各删一条 → 541 - 3 = 538。
  // 2026-11-22 rant 2026-11-22T13:43:13：关于页加「重启守护进程」入口，标题 / 说明 /
  // 按钮 / 进行中 / 失败前缀 / 无通道 六条中英各加一条 → 538 + 6 = 544。
  // 2026-11-24 issue #3200：右栏编辑器补齐十项差距，重载确认、放弃改动、放弃确认、关闭
  // 其他标签、关闭右侧标签、复制路径、缩进宽度（空格 / Tab）八条中英各加一条 → 544 + 8 = 552。
  // 2026-11-24 issue #3204：排队成为消息行自己的状态，`app.queued` 改写为位置标记，
  // 另加删除按钮提示、队列摘要与「走了几条」三条 → 552 + 3 = 555。
  // 2026-11-25 两个分支同一天各加键，合并后是并集：编辑器的分页与失败原因八条、关于页的
  // 检查更新十条，中英各加一条 → 555 + 8 + 10 = 573。这个数是数合并后的字典量出来的。
  // 2026-11-25 rant 2026-11-25T11:10:45：HTML tab 的浏览器模式——开关、地址、地址提示、
  // 打开、空地址、地址打不开六条中英各加一条 → 573 + 6 = 579。
  // 2026-11-25 issue #3221：十二个配置字段有了界面入口——十一个控件的名称与含义、两个分组
  // 标题、一条间隔说明、一条拒绝原话，中英各加二十六条 → 579 + 26 = 605。
  it("zh/en 各 605 个 key 且完全对齐", () => {
    const zhKeys = Object.keys(ZH_DICT);
    const enKeys = Object.keys(EN_DICT);
    expect(zhKeys.length).toBe(605);
    expect(enKeys.length).toBe(605);
    expect(zhKeys.sort()).toEqual(enKeys.sort());
    // DICTS 聚合结构
    expect(Object.keys(DICTS)).toEqual(["zh", "en"]);
  });
  /**
   * issue #3078 第 8 条：打错的指令要说「没有这条指令」并指出 `/` 能看全部——旧措辞
   * 「暂未开放」读起来像「以后会有」，把一个打错的字说成一条没做完的功能。两个语言
   * 各测一遍，因为这条文案是两个 key。
   */
  it("app.cmdUnknown 说的是「没有这条」并指向 /，不是「暂未开放」", () => {
    expect(ZH_DICT["app.cmdUnknown"]).toContain("没有");
    expect(ZH_DICT["app.cmdUnknown"]).toContain("/");
    expect(ZH_DICT["app.cmdUnknown"]).not.toContain("暂未开放");
    expect(EN_DICT["app.cmdUnknown"]).toContain("no");
    expect(EN_DICT["app.cmdUnknown"]).toContain("/");
    expect(EN_DICT["app.cmdUnknown"]).not.toContain("not available yet");
  });

  it("关键分区 key 存在（侧边栏/输入区/设置/工具/聊天/错误边界）", () => {
    for (const key of [
      "sidebar.newChat",
      "nav.sessions",
      "composer.placeholder",
      "settings.title",
      "tool.bash.doing",
      "tool.pwsh.doing",
      "chat.copied",
      "chat.copyFailed",
      "md.copyCode",
      "errorBoundary.title",
    ]) {
      expect(ZH_DICT[key]).toBeTruthy();
      expect(EN_DICT[key]).toBeTruthy();
    }
  });
  it("两条占位提示都列出两个换行键（issue #3050）", () => {
    // 两种语言各一条：只改中文时英文那条会在这里变红，反之亦然。
    for (const placeholder of [
      ZH_DICT["composer.placeholder"],
      EN_DICT["composer.placeholder"],
    ]) {
      expect(placeholder).toContain("Shift+Enter");
      expect(placeholder).toContain("Option+Enter");
    }
  });
});
