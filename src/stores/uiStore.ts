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

/** 上传翻译模式：typeset = 自研重排版管线（默认，功能全）；
 * babeldoc = BabelDOC 对照直出（原版排版、质量更稳，仅英→中，
 * 无重排版/点击翻译/Markdown 导出）。按任务约定走 localStorage
 * （`pdf-reader.uploadMode`），不进 config.json。 */
export type UploadMode = "typeset" | "babeldoc";
const UPLOAD_MODE_STORAGE_KEY = "pdf-reader.uploadMode";

function loadUploadMode(): UploadMode {
  try {
    return localStorage.getItem(UPLOAD_MODE_STORAGE_KEY) === "babeldoc"
      ? "babeldoc"
      : "typeset";
  } catch {
    /* localStorage 不可用 → 默认重排版 */
    return "typeset";
  }
}

function persistUploadMode(m: UploadMode): void {
  try {
    localStorage.setItem(UPLOAD_MODE_STORAGE_KEY, m);
  } catch {
    /* 持久化失败不影响本会话 */
  }
}

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
  /** null = 用户未手动设置过（渲染方按 effectiveZoom 回落形态缺省）。
   * 实时值：缩放手势期间逐格更新，驱动 CSS 尺寸即时跟随（canvas 位图
   * 由浏览器拉伸，零栅格成本） */
  zoom: number | null;
  /** 定稿渲染值：null = 本会话尚未缩放过（渲染方回落 effectiveZoom）。
   * 手势开始时冻结为手势前的有效值（canvas 重绘 key 在此，期间不触发），
   * 停顿 SETTLE_MS 后收敛为最新 zoom 并持久化——「实时拉伸 + 停顿后
   * 高清重绘」两相化的定稿相 */
  renderZoom: number | null;
  /** 下一次上传使用的翻译模式（记住上次选择） */
  uploadMode: UploadMode;
  setMode: (mode: "bilingual" | "inline") => void;
  setTheme: (theme: "light" | "dark") => void;
  setReaderMode: (m: ReaderMode) => void;
  setZoom: (z: number) => void;
  stepZoom: (delta: number) => void;
  resetZoom: () => void;
  toggleTheme: () => void;
  setUploadMode: (m: UploadMode) => void;
}

/** 缩放定稿时延：最后一次缩放变化后等待此时长才触发 pdfjs 重栅格化
 * 与 localStorage 落盘（栅格化是重活，滚动中的连续步进只做 CSS 拉伸） */
const ZOOM_SETTLE_MS = 220;

let zoomSettleTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleZoomSettle() {
  if (zoomSettleTimer !== null) clearTimeout(zoomSettleTimer);
  zoomSettleTimer = setTimeout(() => {
    zoomSettleTimer = null;
    const s = useUiStore.getState();
    if (s.zoom == null) return;
    const settled = effectiveZoom(s.zoom, s.readerMode);
    persistZoom(settled);
    if (s.renderZoom !== settled) useUiStore.setState({ renderZoom: settled });
  }, ZOOM_SETTLE_MS);
}

export const useUiStore = create<UiState>((set) => ({
  mode: "bilingual",
  theme: "light",
  readerMode: "parallel",
  zoom: loadZoom(),
  renderZoom: null,
  uploadMode: loadUploadMode(),
  setMode: (mode) => set({ mode }),
  setTheme: (theme) => set({ theme }),
  setReaderMode: (readerMode) => set({ readerMode }),
  setUploadMode: (uploadMode) =>
    set(() => {
      persistUploadMode(uploadMode);
      return { uploadMode };
    }),
  // 实时相：立即更新 zoom（CSS 尺寸即时跟随）；renderZoom 冻结在手势前值
  // （首次步进时捕获），canvas 重绘与落盘都等 ZOOM_SETTLE_MS 停顿后一次完成
  setZoom: (z) =>
    set((s) => {
      const v = clampZoom(z);
      scheduleZoomSettle();
      return {
        zoom: v,
        renderZoom: s.renderZoom ?? effectiveZoom(s.zoom, s.readerMode),
      };
    }),
  // 步进起点：未设置过时从「当前形态缺省」起步（原版 0.7、重排版 1.0），
  // 保证首次 + 得到 0.8/1.1 而不是从硬编码 1 起跳
  stepZoom: (delta) =>
    set((s) => {
      const base = s.zoom ?? effectiveZoom(null, s.readerMode);
      const v = clampZoom(base + delta);
      scheduleZoomSettle();
      return {
        zoom: v,
        renderZoom: s.renderZoom ?? effectiveZoom(s.zoom, s.readerMode),
      };
    }),
  // 重置 = 回到「未设置」态：原版回落 70%、重排版回落 100%，并清除持久化。
  // 定稿值同步清空 → 渲染方按新回落值重绘；进行中的定稿 timer 作废
  resetZoom: () =>
    set(() => {
      if (zoomSettleTimer !== null) {
        clearTimeout(zoomSettleTimer);
        zoomSettleTimer = null;
      }
      try {
        localStorage.removeItem(ZOOM_STORAGE_KEY);
      } catch {
        /* 清理失败不影响本会话状态 */
      }
      return { zoom: null, renderZoom: null };
    }),
  toggleTheme: () =>
    set((state) => ({
      theme: state.theme === "light" ? "dark" : "light",
    })),
}));
