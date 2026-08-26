import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SettingsPanel } from "./SettingsPanel";
import { I18nProvider } from "../lib/i18n";
import * as i18n from "../lib/i18n";

/**
 * SettingsPanel.test.tsx — Batch 5 slice 6：设置面板接线测试。
 * 模拟 window.emrg 的 getSettings/saveSettings/githubStatus/githubConnect/
 * githubDisconnect IPC，验证：
 * - 五个 tab 渲染与切换（model/github/appearance/language/about）；
 * - 模型服务 tab：getSettings 回填 + 增删改设默认 + saveSettings 载荷；
 * - GitHub tab：状态展示 + connect/disconnect 调用；
 * - 外观 tab：主题点击落 data-theme 属性；语言 tab：setLocale 写 localStorage；
 * - 关于 tab：version/evolutionCount 注入展示；
 * - window.emrg 缺失时降级不崩溃（保存 no-op 提示）。
 */

function mockEmrg(over: Record<string, unknown> = {}) {
  const calls: Record<string, unknown[][]> = {};
  const fn = (name: string, result: unknown) => {
    const mock = vi.fn().mockResolvedValue(result);
    calls[name] = [];
    mock.mockImplementation(async (...args: unknown[]) => {
      calls[name].push(args);
      return result;
    });
    return mock;
  };
  const bridge = {
    getSettings: fn("getSettings", {
      apiKey: "sk-test",
      baseUrl: "https://api.deepseek.com",
      model: "deepseek-v3",
      theme: "dark",
      models: ["deepseek-v3", "gpt-4o"],
      modelDetails: [
        { name: "deepseek-v3", model: "deepseek-v3", vision: false },
        { name: "gpt-4o", model: "gpt-4o", vision: true },
      ],
    }),
    saveSettings: fn("saveSettings", { ok: true }),
    githubStatus: fn("githubStatus", { authenticated: true, user: "argszero" }),
    githubConnect: fn("githubConnect", { ok: true, user: "argszero" }),
    githubDisconnect: fn("githubDisconnect", { ok: true }),
    ...over,
  };
  (window as unknown as { emrg?: unknown }).emrg = bridge;
  return { bridge, calls };
}

function setup(
  over: Record<string, unknown> = {},
  props: { version?: string; evolutionCount?: number | null } = {},
) {
  const { bridge, calls } = mockEmrg(over);
  render(
    <I18nProvider lang="en">
      <SettingsPanel version={props.version} evolutionCount={props.evolutionCount} />
    </I18nProvider>,
  );
  return { bridge, calls };
}

afterEach(() => {
  cleanup(); // 显式卸载（本环境 RTL 自动清理不可靠）
  vi.restoreAllMocks();
  delete (window as unknown as { emrg?: unknown }).emrg;
  document.documentElement.removeAttribute("data-theme");
  try {
    localStorage.clear();
  } catch {
    /* jsdom 无 localStorage 时忽略 */
  }
});

describe("SettingsPanel", () => {
  it("renders panel with five tabs, model tab active by default", async () => {
    setup();
    expect(screen.getByTestId("panel-settings")).toBeTruthy();
    for (const id of ["model", "github", "appearance", "language", "about"]) {
      expect(screen.getByTestId(`settings-tab-${id}`)).toBeTruthy();
    }
    // model body visible, others hidden
    expect(screen.getByTestId("settings-body-model")).toBeTruthy();
    expect(screen.queryByTestId("settings-body-github")).toBeNull();
    expect(screen.queryByTestId("settings-body-about")).toBeNull();
  });

  it("switches tabs", async () => {
    setup();
    await screen.findByTestId("settings-body-model");
    fireEvent.click(screen.getByTestId("settings-tab-github"));
    expect(screen.getByTestId("settings-body-github")).toBeTruthy();
    fireEvent.click(screen.getByTestId("settings-tab-language"));
    expect(screen.getByTestId("settings-body-language")).toBeTruthy();
    fireEvent.click(screen.getByTestId("settings-tab-appearance"));
    expect(screen.getByTestId("settings-body-appearance")).toBeTruthy();
    fireEvent.click(screen.getByTestId("settings-tab-about"));
    expect(screen.getByTestId("settings-body-about")).toBeTruthy();
    expect(screen.queryByTestId("settings-body-model")).toBeNull();
  });

  it("loads settings from bridge into model tab", async () => {
    setup();
    await waitFor(() =>
      expect((screen.getByTestId("set-api-key") as HTMLInputElement).value).toBe("sk-test"),
    );
    expect((screen.getByTestId("set-base-url") as HTMLInputElement).value).toBe(
      "https://api.deepseek.com",
    );
    // default row (deepseek-v3) + extra row (gpt-4o, deduped)
    expect(screen.getAllByTestId("model-item").length).toBe(2);
    expect(screen.getByText("deepseek-v3")).toBeTruthy();
    expect(screen.getByText("gpt-4o")).toBeTruthy();
    // theme applied from saved settings
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("adds a model via the form", async () => {
    setup();
    await screen.findByTestId("add-model-btn");
    fireEvent.click(screen.getByTestId("add-model-btn"));
    expect(screen.getByTestId("model-form")).toBeTruthy();
    fireEvent.change(screen.getByTestId("model-form-name"), {
      target: { value: "claude-3-7" },
    });
    fireEvent.change(screen.getByTestId("model-form-id"), {
      target: { value: "claude-sonnet" },
    });
    fireEvent.click(screen.getByTestId("model-form-vision"));
    fireEvent.click(screen.getByTestId("model-form-save"));
    await waitFor(() => expect(screen.getAllByTestId("model-item").length).toBe(3));
    expect(screen.getByText("claude-3-7")).toBeTruthy();
    expect(screen.getByText("claude-sonnet")).toBeTruthy();
  });

  it("rejects duplicate model name", async () => {
    setup();
    await screen.findByTestId("add-model-btn");
    fireEvent.click(screen.getByTestId("add-model-btn"));
    fireEvent.change(screen.getByTestId("model-form-name"), {
      target: { value: "gpt-4o" },
    });
    fireEvent.click(screen.getByTestId("model-form-save"));
    const msg = await screen.findByTestId("settings-msg");
    expect(msg.className).toContain("err");
    // form stays open (not saved)
    expect(screen.getByTestId("model-form")).toBeTruthy();
  });

  it("deletes and sets default on model rows", async () => {
    setup();
    await waitFor(() => expect(screen.getAllByTestId("model-item").length).toBe(2));
    // set default on gpt-4o row (second item has radio button)
    fireEvent.click(screen.getAllByTestId("model-radio")[0]);
    // gpt-4o becomes default: its row now renders model-radio-default
    await waitFor(() => expect(screen.queryByTestId("model-radio-default")).toBeTruthy());
    // delete the non-default (deepseek-v3) row
    const before = screen.getAllByTestId("model-item").length;
    const delButtons = screen
      .getAllByTestId("model-item")
      .map((row) => row.querySelector('[title="Delete"]'));
    const deletable = delButtons.find((b) => b !== null) as HTMLElement | undefined;
    if (deletable) {
      fireEvent.click(deletable);
      await waitFor(() => expect(screen.getAllByTestId("model-item").length).toBe(before - 1));
    }
  });

  it("saves settings through the bridge with merged model list", async () => {
    const { calls } = setup();
    await waitFor(() =>
      expect((screen.getByTestId("set-api-key") as HTMLInputElement).value).toBe("sk-test"),
    );
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(calls.saveSettings.length).toBe(1));
    const payload = calls.saveSettings[0][0] as {
      apiKey: string;
      baseUrl: string;
      model: string;
      theme: string;
      models: unknown[];
    };
    expect(payload.apiKey).toBe("sk-test");
    expect(payload.baseUrl).toBe("https://api.deepseek.com");
    expect(payload.model).toBe("deepseek-v3");
    expect(payload.theme).toBe("dark");
    expect(payload.models.length).toBe(2);
    await waitFor(() => expect(screen.getByTestId("settings-msg").className).toContain("ok"));
  });

  it("blocks save when api key is empty", async () => {
    const { calls } = setup({
      getSettings: vi.fn().mockResolvedValue({
        apiKey: "",
        baseUrl: "",
        model: "",
        theme: "system",
        modelDetails: [],
      }),
    });
    await screen.findByTestId("set-api-key");
    fireEvent.click(screen.getByTestId("settings-save"));
    const msg = await screen.findByTestId("settings-msg");
    expect(msg.className).toContain("err");
    // no saveSettings call
    expect(calls.saveSettings.length).toBe(0);
  });

  it("shows github status and connects/disconnects", async () => {
    const { calls } = setup();
    fireEvent.click(screen.getByTestId("settings-tab-github"));
    await waitFor(() =>
      expect(screen.getByTestId("github-status").textContent).toContain("argszero"),
    );
    fireEvent.change(screen.getByTestId("set-github-token"), { target: { value: "ghp_x" } });
    fireEvent.click(screen.getByTestId("github-connect-btn"));
    await waitFor(() => expect(calls.githubConnect.length).toBe(1));
    expect(calls.githubConnect[0][0]).toEqual({ token: "ghp_x" });
    fireEvent.click(screen.getByTestId("github-disconnect-btn"));
    await waitFor(() => expect(calls.githubDisconnect.length).toBe(1));
    await waitFor(() =>
      expect(screen.getByTestId("github-status").textContent).not.toContain("argszero"),
    );
  });

  it("applies theme option to documentElement", async () => {
    setup();
    await screen.findByTestId("settings-tab-appearance");
    fireEvent.click(screen.getByTestId("settings-tab-appearance"));
    fireEvent.click(screen.getByTestId("theme-light"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    fireEvent.click(screen.getByTestId("theme-dark"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    fireEvent.click(screen.getByTestId("theme-system"));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("sets locale from language tab", async () => {
    const spy = vi.spyOn(i18n, "setLocale").mockImplementation((l): i18n.Locale => (l === "" ? "zh" : l));
    setup();
    await screen.findByTestId("settings-tab-language");
    fireEvent.click(screen.getByTestId("settings-tab-language"));
    fireEvent.click(screen.getByTestId("lang-zh"));
    expect(spy).toHaveBeenCalledWith("zh");
    fireEvent.click(screen.getByTestId("lang-system"));
    expect(spy).toHaveBeenCalledWith("");
  });

  it("renders version and evolution count in about tab", async () => {
    setup({}, { version: "0.2.81", evolutionCount: 115 });
    await screen.findByTestId("settings-tab-about");
    fireEvent.click(screen.getByTestId("settings-tab-about"));
    const line = screen.getByTestId("about-line").textContent || "";
    expect(line).toContain("0.2.81");
    expect(line).toContain("115");
  });

  it("degrades gracefully without window.emrg", async () => {
    render(
      <I18nProvider lang="en">
        <SettingsPanel />
      </I18nProvider>,
    );
    expect(screen.getByTestId("panel-settings")).toBeTruthy();
    // default model row shows not-configured placeholder
    expect(screen.getByText("Not configured")).toBeTruthy();
    // save without bridge → no-op with saved message (api key filled first)
    fireEvent.change(screen.getByTestId("set-api-key"), { target: { value: "sk-x" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(screen.getByTestId("settings-msg").className).toContain("ok"));
  });
});
