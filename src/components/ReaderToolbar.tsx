import { Link, useNavigate } from "react-router-dom";
import {
  useUiStore,
  effectiveZoom,
  ZOOM_MAX,
  ZOOM_MIN,
  ZOOM_ORIGINAL_DEFAULT,
} from "../stores/uiStore";
import { useConfigStore } from "../stores/configStore";
import { usePdfStore } from "../stores/pdfStore";
import ExportBar from "./ExportBar";

/**
 * 阅读页共享工具栏：
 * 通栏底色条（与标题栏同族） + 三段式——左「← 文档库」、中模式分段控件居中、
 * 右缩放/主题/导出组。
 * - 由 BilingualPage 与 InlinePage 共用，保证两个视图工具栏完全一致。
 * - 源 PDF 缺失（重开已删/移动文档）时「原版PDF」组禁用并提示。
 * - 全局缩放控件（−/百分比/＋，与 Ctrl+滚轮共用 uiStore.zoom 并持久化）。
 * - 模式选择器分「重排版」「原版PDF」两组（组名嵌控件内非交互标签）；
 * 验收决策：原版PDF·左右对照 = BabelDOC 排版对照（DualPdfPage）。
 * - 原页内页签条在 Tauri 下已升格进标题栏；浏览器 dev 由 ReaderTabs 渲染。
 */
export default function ReaderToolbar() {
  const { mode, setMode, readerMode, setReaderMode, zoom: zoomRaw, stepZoom, resetZoom, theme, setTheme, toggleTheme } =
    useUiStore();
  const { setTheme: saveTheme } = useConfigStore();
  const { filePath, file } = usePdfStore();
  const navigate = useNavigate();
  const sourceMissing = !filePath;
  // 显示与边界判断用有效缩放值（未设置过时按形态回落缺省：
  // 原版 70% / 重排版 100%），与阅读区实际渲染一致
  const zoom = effectiveZoom(zoomRaw, readerMode);
  // 文章标题：重开文档时为 doc.title，新翻译时为文件名（去 .pdf 后缀）
  const docTitle = (file?.name ?? "").replace(/\.pdf$/i, "");

  // 切换模式同时跳转对应路由（两个视图各自是独立页面组件）
  const switchMode = (m: "bilingual" | "inline") => {
    if (readerMode === "parallel" && m === mode) return;
    setReaderMode("parallel");
    setMode(m);
    navigate(m === "inline" ? "/reader/inline" : "/reader/bilingual");
  };

  // 原版PDF 组（两种形态任一激活）：缩放缺省/重置提示均按原版语义
  const originalActive = readerMode !== "parallel";

  // 暗色切换，
  // 持久化与主页一致走 config.json ui.theme
  const handleToggleTheme = async () => {
    const next = theme === "dark" ? "light" : "dark";
    toggleTheme();
    setTheme(next);
    await saveTheme(next);
  };

  // 选中态统一样式——
  // 与「原版PDF·左右对照」同款：白底胶囊 + 蓝字 + 细描边
  const segBtn = (active: boolean) =>
    `rounded-md px-3 py-1 text-sm font-medium transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40 ${
      active
        ? "bg-white text-blue-700 shadow-sm ring-1 ring-slate-200 dark:bg-slate-700 dark:text-blue-300 dark:ring-slate-600"
        : "text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
    }`;

  return (
    <div className="flex min-h-12 shrink-0 flex-wrap content-center items-center gap-x-3 gap-y-1 border-b border-slate-200 bg-slate-50 px-4 py-1 dark:border-slate-700 dark:bg-slate-900">
      {/* 左：返回文档库（Tauri 下标题栏 logo 承担返回，此处仅浏览器 dev 显示） */}

      <div className="flex min-w-0 flex-1 items-center">
        {"__TAURI_INTERNALS__" in window ? null : (
          <Link
            to="/"
            className="shrink-0 rounded-md px-2 py-1.5 text-sm font-medium text-slate-500 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            title="返回文档库（翻译结果保留，可直接再进入）"
          >
            ← 文档库
          </Link>
        )}
      </div>

      {/* 中：模式分段控件（mockup 方案 A 居中为主角） */}

      <div
        className="inline-flex shrink-0 items-center rounded-lg border border-slate-200 bg-white p-1 dark:border-slate-700 dark:bg-slate-800"
        role="tablist"
        aria-label="阅读模式"
      >
        <span
          aria-hidden="true"
          className="select-none px-2 text-xs text-slate-400 dark:text-slate-500"
        >
          重排版
        </span>
        <button
          role="tab"
          aria-selected={!originalActive && mode === "bilingual"}
          onClick={() => switchMode("bilingual")}
          className={segBtn(!originalActive && mode === "bilingual")}
        >
          左右对照
        </button>
        <button
          role="tab"
          aria-selected={!originalActive && mode === "inline"}
          onClick={() => switchMode("inline")}
          className={segBtn(!originalActive && mode === "inline")}
        >
          紧跟模式
        </button>
        <span
          aria-hidden="true"
          className="mx-1 h-5 w-px shrink-0 bg-slate-200 dark:bg-slate-600"
        />
        <span
          aria-hidden="true"
          className="select-none px-2 text-xs text-slate-400 dark:text-slate-500"
        >
          原版PDF
        </span>
        <button
          role="tab"
          aria-selected={readerMode === "original_bilingual"}
          disabled={sourceMissing}
          title={
            sourceMissing
              ? "源 PDF 已移动/删除，原版模式不可用（对照/紧跟不受影响）"
              : "BabelDOC 排版对照：整篇双语 PDF 应用内渲染（首次生成需翻译，之后秒开）"
          }
          onClick={() =>
            setReaderMode(
              readerMode === "original_bilingual"
                ? "parallel"
                : "original_bilingual"
            )
          }
          className={segBtn(readerMode === "original_bilingual")}
        >
          左右对照
        </button>
        <button
          role="tab"
          aria-selected={readerMode === "original_click"}
          disabled={sourceMissing}
          title={
            sourceMissing
              ? "源 PDF 已移动/删除，原版模式不可用（对照/紧跟不受影响）"
              : "按原版排版，点击高亮块查看译文（已选中时点击切回重排版）"
          }
          onClick={() =>
            setReaderMode(
              readerMode === "original_click" ? "parallel" : "original_click"
            )
          }
          className={segBtn(readerMode === "original_click")}
        >
          点击翻译
        </button>
      </div>

      {/* 右：缩放 / 主题 / 导出 */}

      <div className="flex flex-1 shrink-0 items-center justify-end gap-2">
        <div
          className="inline-flex shrink-0 items-center rounded-lg border border-slate-200 bg-white px-1 py-0.5 dark:border-slate-700 dark:bg-slate-800"
          role="group"
          aria-label="缩放"
        >
          <button
            onClick={() => stepZoom(-0.1)}
            disabled={zoom <= ZOOM_MIN}
            aria-label="缩小"
            title="缩小（也可用 Ctrl+滚轮）"
            className="h-6 w-6 rounded text-slate-500 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-200"
          >
            −
          </button>
          <button
            onClick={resetZoom}
            title={
              originalActive
                ? `点击重置为缺省 ${Math.round(ZOOM_ORIGINAL_DEFAULT * 100)}%`
                : "点击重置为 100%"
            }
            className="w-11 text-center text-xs tabular-nums text-slate-600 transition-colors duration-150 hover:text-slate-900 dark:text-slate-300 dark:hover:text-slate-100"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            onClick={() => stepZoom(0.1)}
            disabled={zoom >= ZOOM_MAX}
            aria-label="放大"
            title="放大（也可用 Ctrl+滚轮）"
            className="h-6 w-6 rounded text-slate-500 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-200"
          >
            ＋
          </button>
        </div>
        {/* 回归修复：阅读页主题切换入口（原在侧边栏） */}

        <button
          onClick={handleToggleTheme}
          title={theme === "dark" ? "切换到亮色" : "切换到暗色"}
          aria-label={theme === "dark" ? "切换到亮色" : "切换到暗色"}
          className="h-8 w-8 shrink-0 rounded-lg border border-slate-200 bg-white text-sm leading-none text-slate-500 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-200"
        >
          {theme === "dark" ? "🌞" : "🌙"}
        </button>
        <ExportBar />
      </div>
    </div>
  );
}
