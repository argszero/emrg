import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SkillsDialog } from "./SkillsDialog";
import { I18nProvider } from "../lib/i18n";
import type { SkillRow } from "../lib/dialogLists";

/**
 * SkillsDialog.test.tsx — /skills 技能列表对话框测试（Batch 4 slice 2）。
 * 镜像 vanilla showSkillsDialog：loading/空/列表/错误四态。
 */

const skills: SkillRow[] = [
  { name: "browser-harness", source: "agent", description: "Web automation" },
  { name: "skill-catalog", source: "user" },
];

function setup(props: Partial<Parameters<typeof SkillsDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <SkillsDialog open skills={skills} {...props} />
    </I18nProvider>,
  );
}

describe("SkillsDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <SkillsDialog open={false} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("skills-dialog")).toBeNull();
  });

  it("loading → dlg.loading 行", () => {
    setup({ skills: null });
    expect(screen.getByTestId("skills-loading")).toHaveTextContent("加载中");
  });

  it("空列表 → app.noSkills", () => {
    setup({ skills: [] });
    expect(screen.getByTestId("skills-empty")).toHaveTextContent("还没有加载技能。");
  });

  it("列表：label = name；hint = source · description", () => {
    setup();
    const rows = screen.getAllByTestId("skill-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("browser-harness");
    expect(rows[0]).toHaveTextContent("agent · Web automation");
    expect(rows[1]).toHaveTextContent("skill-catalog");
    expect(rows[1]).toHaveTextContent("user");
  });

  it("error → app.skillsFailed 行", () => {
    setup({ error: "fail" });
    expect(screen.getByTestId("skills-error")).toHaveTextContent("加载技能失败：fail");
  });

  it("同名冲突 → 被顶替的那份被点名，两侧各带 source 与 path", () => {
    // 判据 1 在这一面：撞名的两份声明都要出现在这里 —— 生效的那份与落败的那份，
    // 各带 source 和 path。只列赢家等于把「你的另一份为什么没生效」藏起来。
    setup({
      shadows: [
        {
          name: "foo",
          kept: { source: "agent", path: "/repo/.emrg/skills/foo.md" },
          dropped: { source: "user", path: "/home/u/.emrg/skills/foo.md" },
        },
      ],
    });
    expect(screen.getByTestId("skill-shadow-row")).toHaveTextContent("foo");
    expect(screen.getByTestId("skill-shadow-row")).toHaveTextContent("/repo/.emrg/skills/foo.md");
    expect(screen.getByTestId("skill-shadow-row")).toHaveTextContent("/home/u/.emrg/skills/foo.md");
  });

  it("无同名冲突 → 版面与今天一致，不多出一块空区", () => {
    // 判据 4 是「同样的树答同样的列表」，在这一面就是：没有冲突就不出现影子区。
    setup();
    expect(screen.queryByTestId("skills-shadows")).toBeNull();
  });

  it("关闭按钮 → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    await userEvent.click(screen.getByTestId("dlg-close"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
