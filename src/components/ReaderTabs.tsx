import { useNavigate } from "react-router-dom";
import { usePdfStore } from "../stores/pdfStore";
import { useSessionsStore } from "../stores/sessionsStore";

/**
 * 阅读页签条：每篇打开的文献/翻译任务一个页签，
 * 切换 = 会话快照交换（路由不变）。翻译中页签显示进度徽标且不可关闭；
 * 关闭活跃页签自动切换到最近使用的会话，全部关闭回文献库。
 * 仅在 ≥2 个会话时渲染（单会话时工具栏标题已足够）。
 */
export default function ReaderTabs() {
  const sessions = useSessionsStore((s) => s.sessions);
  const activate = useSessionsStore((s) => s.activate);
  const close = useSessionsStore((s) => s.close);
  const sessionKey = usePdfStore((s) => s.sessionKey);
  const navigate = useNavigate();

  // 验收决策：Tauri 下页签升格进自绘标题栏（TitleBar 多开 tab），
  // 本组件仅浏览器 dev 渲染
  if ("__TAURI_INTERNALS__" in window) return null;

  // 按创建顺序稳定排列
  const ordered = [...sessions].sort((a, b) => a.createdAt - b.createdAt);
  if (ordered.length < 2) return null;

  const handleClose = (key: string, running: boolean) => {
    if (running) return;
    const next = close(key);
    if (next === null) navigate("/");
  };

  return (
    <div
      className="mb-3 flex items-center gap-1 overflow-x-auto border-b border-slate-200 pb-2 dark:border-slate-700"
      role="tablist"
      aria-label="打开的文献"
    >
      {ordered.map((s) => {
        const active = s.key === sessionKey;
        const running = s.job?.status === "running";
        const failed = s.job?.status === "failed";
        return (
          <div
            key={s.key}
            role="tab"
            aria-selected={active}
            tabIndex={0}
            onClick={() => activate(s.key)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate(s.key);
              }
            }}
            title={running ? `翻译中 ${Math.round(s.job?.progress ?? 0)}%` : s.title}
            className={`group flex shrink-0 cursor-pointer items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition-colors duration-150 ${
              active
                ? "border-slate-300 bg-white font-medium text-slate-900 shadow-sm dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
                : "border-transparent text-slate-500 hover:bg-slate-100 hover:text-slate-800 dark:text-slate-400 dark:hover:bg-slate-800/60 dark:hover:text-slate-200"
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
            <span className="max-w-[9rem] truncate">{s.title}</span>
            {running && (
              <span className="shrink-0 text-xs text-blue-600 dark:text-blue-400">
                {Math.round(s.job?.progress ?? 0)}%
              </span>
            )}
            {!running && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  handleClose(s.key, false);
                }}
                aria-label={`关闭 ${s.title}`}
                title="关闭页签"
                className="ml-0.5 shrink-0 rounded p-0.5 text-slate-400 opacity-0 transition-opacity duration-150 hover:bg-slate-200 hover:text-slate-700 focus:opacity-100 group-hover:opacity-100 dark:hover:bg-slate-700 dark:hover:text-slate-200"
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
  );
}
