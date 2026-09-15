import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { usePdfStore } from "../../stores/pdfStore";
import { useSessionsStore } from "../../stores/sessionsStore";
import { useUiStore } from "../../stores/uiStore";
import { useBabelDocStore } from "../../stores/babeldocStore";
import { EXTRACT_DONE, currentTranslationKey } from "../../lib/translationManager";

/**
 * 自绘标题栏（阶段10-T3；2026-09-10 验收反馈二次改造）：
 * 靠岸学术式——标题栏 = 「应用 logo + 文章多开 tab + 窗口三钮」。
 * - tab = 阶段8 会话注册表中的每篇打开文献/翻译任务：点击切换、× 关闭、
 *   「+」回主页新开；后台 tab 挂起保留翻译进度（进度徽标）。
 * - 原 ReaderTabs 页签条在 Tauri 下退役（由本栏承担）；浏览器 dev 保留。
 * - 仅 Tauri 环境渲染整栏（浏览器 dev 无 decorations:false，无需窗口钮）
 * - 整栏为拖拽区（data-tauri-drag-region，权限 core:window:allow-start-dragging
 *   ——2026-09-10 验收发现漏权限导致窗口拖不动，已补 capability）
 * - 双击最大化切换；关闭钮 hover 红色语义（桌面软件惯例）
 */
export default function TitleBar() {
  const isTauri = "__TAURI_INTERNALS__" in window;
  const location = useLocation();
  const navigate = useNavigate();
  const file = usePdfStore((s) => s.file);
  const sessionKey = usePdfStore((s) => s.sessionKey);
  const sessions = useSessionsStore((s) => s.sessions);
  const activate = useSessionsStore((s) => s.activate);
  const closeSession = useSessionsStore((s) => s.close);
  // BabelDOC 全局进度徽标（T10 反馈 6 改进 1）：任务运行中任何界面可见，
  // 点击回到该文档的「原版对照」视图看进度——解决「切走后像暂停了」
  const bdoc = useBabelDocStore();
  const [maximized, setMaximized] = useState(false);
  const [win, setWin] = useState<{
    minimize: () => void;
    toggleMaximize: () => void;
    close: () => void;
    onResized: (h: () => void) => Promise<() => void>;
  } | null>(null);

  useEffect(() => {
    if (!isTauri) return;
    import("@tauri-apps/api/window")
      .then(({ getCurrentWindow }) => {
        const w = getCurrentWindow();
        setWin({
          minimize: () => void w.minimize(),
          toggleMaximize: () => void w.toggleMaximize(),
          close: () => void w.close(),
          onResized: (h) => w.onResized(h),
        });
        void w.isMaximized().then(setMaximized);
        void w.onResized(() => void w.isMaximized().then(setMaximized));
      })
      .catch(() => setWin(null));
  }, [isTauri]);

  // 页面标题：无会话 tab 时的兜底显示（主页/设置/添加文献/单篇阅读）
  const isReader = location.pathname.startsWith("/reader");
  const docTitle = (file?.name ?? "").replace(/\.pdf$/i, "");
  const pageTitle = isReader
    ? docTitle || "阅读"
    : location.pathname === "/config"
      ? "设置"
      : location.pathname === "/add"
        ? "添加文献"
        : "主页";

  if (!isTauri) return null;

  // tab 按创建顺序稳定排列；与页内页签同源（会话注册表）
  const ordered = [...sessions].sort((a, b) => a.createdAt - b.createdAt);

  const handleActivate = (key: string) => {
    // 阶段11-T6 门禁：提取未完成（progress<EXTRACT_DONE，排版未定型）的
    // 翻译中 tab 不进阅读页。不在阅读页时仅激活（留在当前页看进度卡）；
    // 已在阅读页则整次忽略——激活会把提取中的半成品 pages 换进正看的文章
    const s = sessions.find((x) => x.key === key);
    if (s?.job?.status === "running" && (s.job?.progress ?? 0) < EXTRACT_DONE) {
      if (!isReader) activate(key);
      return;
    }
    if (activate(key) && !isReader) {
      // 阅读模式跟随用户上次的对照/紧跟选择（与重开入口统一口径，
      // 否则页面是左右对照而工具栏选中态还停在紧跟模式）
      const mode = useUiStore.getState().mode;
      navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
    }
  };

  const handleClose = (key: string, running: boolean) => {
    if (running) return; // 翻译中不可关闭（同旧页签约定）
    const next = closeSession(key);
    if (next === null) navigate("/");
  };

  const norm = (p?: string | null) =>
    (p || "").replace(/[\/]+/g, "/").toLowerCase();

  const jumpToBabeldoc = () => {
    const target = norm(bdoc.filePath);
    if (!target) return;
    const hit = sessions.find((x) => norm(x.snapshot?.filePath) === target);
    if (!hit) return; // 文档未开阅读会话：徽标仅展示（hover 文案说明）
    if (activate(hit.key) && !isReader) {
      const mode = useUiStore.getState().mode;
      navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
    }
    useUiStore.getState().setReaderMode("original_bilingual");
  };

  const btn =
    "flex h-full w-11 items-center justify-center text-sm text-slate-500 transition-colors duration-150 hover:bg-slate-200 hover:text-slate-800 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-100";

  return (
    <div
      data-tauri-drag-region
      onDoubleClick={() => win?.toggleMaximize()}
      className="flex h-10 shrink-0 select-none items-center gap-1 border-b border-slate-200 bg-slate-50 pl-2.5 dark:border-slate-700 dark:bg-slate-900"
    >
      {/* logo + 应用名：点击返回主页（同靠岸学术） */}
      <Link
        to="/"
        className="mr-1 flex shrink-0 items-center gap-1.5 rounded px-1 py-0.5"
        title="返回主页"
      >
        <span
          aria-hidden="true"
          className="flex h-[18px] w-[18px] items-center justify-center rounded bg-blue-600 text-[10px] font-bold text-white"
        >
          译
        </span>
        <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
          PDF双语阅读器
        </span>
      </Link>

      {/* 文章多开 tab（会话注册表）：点击切换 / × 关闭；无会话时回退页面标题 */}
      {ordered.length > 0 ? (
        <div className="flex h-full min-w-0 flex-1 items-center gap-1 overflow-x-auto px-1.5">
          {ordered.map((s) => {
            const active = s.key === sessionKey;
            // 运行中 = 状态 running 且是当前活跃翻译任务（同一时间仅 1 个）。
            // 状态 running 但不属于活跃任务的是轮询已死亡的冻结残留——
            // 按中断呈现（红点、不显示冻结时的假百分比），2026-09-12 用户反馈
            const running = s.job?.status === "running" && s.key === currentTranslationKey();
            const interrupted = s.job?.status === "running" && !running;
            const failed = s.job?.status === "failed" || interrupted;
            return (
              <div
                key={s.key}
                role="tab"
                aria-selected={active}
                tabIndex={0}
                onClick={() => handleActivate(s.key)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    handleActivate(s.key);
                  }
                }}
                title={
                  running
                    ? `翻译中 ${Math.round(s.job?.progress ?? 0)}%`
                    : interrupted
                      ? "翻译已中断，重新打开该文档可继续"
                      : s.title
                }
                // 等宽页签（2026-09-15 用户反馈"标签大小不一样"）：标题
                // 长度/运行态按钮切换不再影响外宽，标题区截断、悬停看全名
                className={`group flex h-7 w-36 shrink-0 cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 text-xs transition-colors duration-150 ${
                  active
                    ? "border-slate-200 bg-white font-medium text-slate-900 shadow-sm dark:border-slate-600 dark:bg-slate-700 dark:text-slate-100"
                    : "border-transparent text-slate-500 hover:bg-slate-200/70 hover:text-slate-800 dark:text-slate-400 dark:hover:bg-slate-800/70 dark:hover:text-slate-200"
                }`}
              >
                {running && (
                  <span
                    className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-blue-500"
                    aria-hidden="true"
                  />
                )}
                {failed && (
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full bg-red-500"
                    aria-hidden="true"
                  />
                )}
                <span className="min-w-0 flex-1 truncate">{s.title}</span>
                {running ? (
                  <span className="w-7 shrink-0 text-right text-[10px] tabular-nums text-blue-600 dark:text-blue-400">
                    {Math.round(s.job?.progress ?? 0)}%
                  </span>
                ) : (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleClose(s.key, false);
                    }}
                    aria-label={`关闭 ${s.title}`}
                    title="关闭"
                    className="shrink-0 rounded p-0.5 text-slate-400 opacity-0 transition-opacity duration-150 hover:bg-slate-200 hover:text-slate-700 focus:opacity-100 group-hover:opacity-100 dark:hover:bg-slate-700 dark:hover:text-slate-200"
                  >
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      className="h-3 w-3"
                      aria-hidden="true"
                    >
                      <path d="M18 6 6 18M6 6l12 12" />
                    </svg>
                  </button>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <span className="min-w-0 max-w-[45%] truncate px-1 text-xs text-slate-700 dark:text-slate-200" title={pageTitle}>
          {pageTitle}
        </span>
      )}

      {/* BabelDOC 生成中：全局进度徽标（跨文档可见，点击跳回该文档对照视图） */}
      {bdoc.phase === "running" && (
        <button
          onClick={jumpToBabeldoc}
          title={
            bdoc.filePath
              ? `正在生成排版对照：${bdoc.filePath.split(/[\/]/).pop() ?? ""}——点击回到该文档查看`
              : "排版对照生成中"
          }
          className="flex h-6 shrink-0 items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-2.5 text-[11px] font-medium text-blue-700 transition-colors duration-150 hover:bg-blue-100 dark:border-blue-500/40 dark:bg-blue-500/10 dark:text-blue-300 dark:hover:bg-blue-500/20"
        >
          <span
            className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500"
            aria-hidden="true"
          />
          对照生成中 {Math.round(bdoc.progress)}%
        </button>
      )}

      {/* 拖拽空白区 */}
      <div className="h-full flex-1" data-tauri-drag-region />
      {/* 窗口控制三钮（占满标题栏高度，点击区不触发拖拽） */}
      <div className="flex h-full items-center" onClick={(e) => e.stopPropagation()}>
        <button
          onClick={() => win?.minimize()}
          aria-label="最小化"
          className={`${btn} rounded-bl-none`}
        >
          &#x2500;
        </button>
        <button
          onClick={() => win?.toggleMaximize()}
          aria-label={maximized ? "还原" : "最大化"}
          className={btn}
        >
          {maximized ? "\u2750" : "\u25A1"}
        </button>
        <button
          onClick={() => win?.close()}
          aria-label="关闭"
          className="flex h-full w-11 items-center justify-center text-sm text-slate-500 transition-colors duration-150 hover:bg-red-500 hover:text-white dark:text-slate-400"
        >
          &#x2715;
        </button>
      </div>
    </div>
  );
}
