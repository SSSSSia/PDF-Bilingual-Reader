import { create } from "zustand";
import { AppConfig } from "../types";
import { loadConfig, saveConfig } from "../lib/bridge";

/** 单个 API 区块的连通性测试结果（spec 记录测试时的表单值：
 * 用户测完又改动地址/Key/模型则视为未测试）。放全局 store：
 * 保存后的自动补测若被用户切页打断，结果不随组件卸载丢失，
 * 返回设置页仍可见。 */
export interface SectionTest {
  state: "idle" | "testing" | "ok" | "fail";
  msg: string;
  spec?: { api_url: string; api_key: string; model: string; mode: "ocr" | "text" };
}

/**
 * 配置单一来源：后端 config.json（%APPDATA%/pdf-reader/）是唯一事实来源，
 * 前端不持久化任何配置副本（无 localStorage），仅内存镜像：
 * - 启动时 App.tsx 调用 loadConfig 灌入一次 → isConfigured 立即为真，主流程不再提示补填；
 * - 仅用户在设置页显式保存 / 切主题时才回写（saveConfig → config.json）。
 * configLoaded 区分「还没加载完」与「已加载但未配置」，避免启动瞬间误报"请先配置"。
 */
export const useConfigStore = create<ConfigState>((set, get) => ({
  config: null,
  isConfigured: false,
  configLoaded: false,
  apiTests: {
    ocr: { state: "idle", msg: "" },
    text: { state: "idle", msg: "" },
  },

  loadConfig: async () => {
    try {
      const raw = (await loadConfig()) as string;
      const config: AppConfig = JSON.parse(raw);
      const configured = !!(config.ocr.api_key && config.translate.api_key);
      set({ config, isConfigured: configured, configLoaded: true });
    } catch (e) {
      // 浏览器 dev 模式下 sidecar 可能尚未就绪：延迟重试一次；仍失败则置已加载
      // 但 config 保持 null（Layout 只在「已加载且确无 Key」时才警示，不误报）。
      console.error("Failed to load config:", e);
      const cur = get();
      if (!cur.configLoaded) {
        setTimeout(() => {
          void get().loadConfig();
        }, 2000);
        return;
      }
      set({ configLoaded: true });
    }
  },

  saveConfig: async (config: AppConfig) => {
    try {
      await saveConfig(JSON.stringify(config));
      const configured = !!(config.ocr.api_key && config.translate.api_key);
      set({ config, isConfigured: configured, configLoaded: true });
    } catch (e) {
      console.error("Failed to save config:", e);
      throw e;
    }
  },

  setTheme: async (theme: "light" | "dark") => {
    const cur = get().config;
    if (!cur) return;
    const updated: AppConfig = { ...cur, ui: { ...cur.ui, theme } };
    try {
      await saveConfig(JSON.stringify(updated));
      set({ config: updated });
    } catch (e) {
      console.error("Failed to save theme:", e);
    }
  },

  setApiTest: (mode, result) =>
    set((s) => ({ apiTests: { ...s.apiTests, [mode]: result } })),
}));

interface ConfigState {
  config: AppConfig | null;
  isConfigured: boolean;
  /** 是否已完成至少一次启动加载（无论成败）——路由守卫与警示徽标依赖它区分三态 */
  configLoaded: boolean;
  apiTests: { ocr: SectionTest; text: SectionTest };
  loadConfig: () => Promise<void>;
  saveConfig: (config: AppConfig) => Promise<void>;
  setTheme: (theme: "light" | "dark") => Promise<void>;
  setApiTest: (mode: "ocr" | "text", result: SectionTest) => void;
}
