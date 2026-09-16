import { create } from "zustand";

/** readerMode：
 * parallel = 重排版（bilingual/inline 由 mode 决定）；
 * original_click = 原版PDF·点击翻译（pdfjs 渲染 + 块坐标译文浮层）；
 * original_bilingual = 原版PDF·左右对照。
 * 注：曾加 original_replace（bbox 原地盖译文层），用户验收观感不佳
 * 已移除，决策专注优化左右对照。 */
export type ReaderMode = "parallel" | "original_click" | "original_bilingual";

/** 全局缩放：0.7–2.0、步进 0.1。阅读偏好（非 API 配置），
 * 按任务约定走 localStorage（`pdf-reader.zoom`），不进 config.json。 */
export const ZOOM_MIN = 0.7;
export const ZOOM_MAX = 2.0;
export const ZOOM_STEP = 0.1;
/** 各形态「未设置」时的缺省缩放。原版 PDF 固定版式 100% 偏大
 * （学术双栏尤其如此），初始 70% 可视范围更接近 PDF 阅读器惯例；
 * 重排版是自排文字，100% 为排版基准。用户一旦手动缩放即持久化，
 * 之后全形态以用户值为准（zoom != null）。 */
export const ZOOM_ORIGINAL_DEFAULT = 0.7;
export const ZOOM_REWRITE_DEFAULT = 1;
const ZOOM_STORAGE_KEY = "pdf-reader.zoom";

function clampZoom(z: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(z * 10) / 10));
}

/** null = 从未设置过（localStorage 无值）→ 各形态回落缺省值 */
function loadZoom(): number | null {
  try {
    const raw = localStorage.getItem(ZOOM_STORAGE_KEY);
    const v = raw == null ? NaN : parseFloat(raw);
    if (Number.isFinite(v)) return clampZoom(v);
  } catch {
    /* localStorage 不可用的环境 → 视为未设置 */
  }
  return null;
}

function persistZoom(z: number): void {
  try {
    localStorage.setItem(ZOOM_STORAGE_KEY, String(z));
  } catch {
    /* 持久化失败不影响本会话缩放 */
  }
}

/** 有效缩放值：用户设置过用用户值，未设置按形态回落缺省。
 * 非 parallel 即原版PDF 组（点击翻译/左右对照，缺省同为 70%）。 */
export function effectiveZoom(
  zoom: number | null,
  readerMode: ReaderMode
): number {
  if (zoom != null) return zoom;
  return readerMode === "parallel"
    ? ZOOM_REWRITE_DEFAULT
    : ZOOM_ORIGINAL_DEFAULT;
}

interface UiState {
  mode: "bilingual" | "inline";
  theme: "light" | "dark";
  readerMode: ReaderMode;
  /** null = 用户未手动设置过（渲染方按 effectiveZoom 回落形态缺省） */
  zoom: number | null;
  setMode: (mode: "bilingual" | "inline") => void;
  setTheme: (theme: "light" | "dark") => void;
  setReaderMode: (m: ReaderMode) => void;
  setZoom: (z: number) => void;
  stepZoom: (delta: number) => void;
  resetZoom: () => void;
  toggleTheme: () => void;
}

export const useUiStore = create<UiState>((set) => ({
  mode: "bilingual",
  theme: "light",
  readerMode: "parallel",
  zoom: loadZoom(),
  setMode: (mode) => set({ mode }),
  setTheme: (theme) => set({ theme }),
  setReaderMode: (readerMode) => set({ readerMode }),
  setZoom: (z) =>
    set(() => {
      const v = clampZoom(z);
      persistZoom(v);
      return { zoom: v };
    }),
  // 步进起点：未设置过时从「当前形态缺省」起步（原版 0.7、重排版 1.0），
  // 保证首次 + 得到 0.8/1.1 而不是从硬编码 1 起跳
  stepZoom: (delta) =>
    set((s) => {
      const base = s.zoom ?? effectiveZoom(null, s.readerMode);
      const v = clampZoom(base + delta);
      persistZoom(v);
      return { zoom: v };
    }),
  // 重置 = 回到「未设置」态：原版回落 70%、重排版回落 100%，并清除持久化
  resetZoom: () =>
    set(() => {
      try {
        localStorage.removeItem(ZOOM_STORAGE_KEY);
      } catch {
        /* 清理失败不影响本会话状态 */
      }
      return { zoom: null };
    }),
  toggleTheme: () =>
    set((state) => ({
      theme: state.theme === "light" ? "dark" : "light",
    })),
}));
