import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useLibraryStore } from "../stores/libraryStore";
import { useConfigStore } from "../stores/configStore";
import { useUiStore } from "../stores/uiStore";
import ConfirmDialog from "./common/ConfirmDialog";

/**
 * 侧边栏：
 * logo / 文献库导航 / 文件夹分组（新建、重命名、删除、计数）/ 底部设置与主题。
 * 窄窗口（<md）由 Layout 的降级顶栏替代，本组件隐藏。
 */
export default function Sidebar() {
  const { docs, folders, fetchAll, createFolder, renameFolder, deleteFolder } =
    useLibraryStore();
  const { config, isConfigured, configLoaded, setTheme: saveTheme } =
    useConfigStore();
  const { theme, toggleTheme, setTheme } = useUiStore();
  const location = useLocation();
  const navigate = useNavigate();

  // null = 未在编辑；"new" = 新建中；否则为重命名的 folder_id
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // 删除二次确认（Tauri WebView2 下 window.confirm 不可靠，必须自绘对话框）
  const [pendingDelete, setPendingDelete] = useState<{
    id: string;
    name: string;
  } | null>(null);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const counts = docs.reduce<Record<string, number>>((acc, d) => {
    if (d.folder_id) acc[d.folder_id] = (acc[d.folder_id] ?? 0) + 1;
    return acc;
  }, {});

  const startCreate = () => {
    setDraft("");
    setEditing("new");
  };
  const startRename = (id: string, name: string) => {
    setDraft(name);
    setEditing(id);
  };
  const confirmEdit = async () => {
    const name = draft.trim();
    if (name) {
      try {
        if (editing === "new") await createFolder(name);
        else if (editing) await renameFolder(editing, name);
      } catch {
        /* 后端校验失败（空名等）静默取消编辑态 */
      }
    }
    setEditing(null);
  };

  const handleDelete = async () => {
    if (!pendingDelete) return;
    const { id } = pendingDelete;
    setPendingDelete(null);
    await deleteFolder(id);
    // 正处在该文件夹视图时回文献库
    if (location.pathname === `/folder/${id}`) navigate("/");
  };

  const handleToggleTheme = async () => {
    const next = theme === "dark" ? "light" : "dark";
    toggleTheme();
    setTheme(next);
    await saveTheme(next);
  };

  const navCls = (active: boolean) =>
    `flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors duration-150 ${
      active
        ? "bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100"
        : "text-slate-600 hover:bg-slate-50 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800/60 dark:hover:text-slate-100"
    }`;

  return (
    <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
      {/* logo */}

      <div className="px-4 pb-2 pt-5">
        <NavLink to="/" className="flex items-center gap-2.5">
          <span
            className="flex h-7 w-7 items-center justify-center rounded-md bg-blue-600 text-sm font-bold text-white"
            aria-hidden="true"
          >
            译
          </span>
          <span className="text-base font-semibold text-slate-900 dark:text-slate-100">
            PDF双语阅读器
          </span>
        </NavLink>
      </div>

      {/* 主导航 */}

      <nav className="mt-3 space-y-0.5 px-3">
        <NavLink to="/" end className={({ isActive }) => navCls(isActive)}>
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-4 w-4 shrink-0"
            aria-hidden="true"
          >
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
          </svg>
          文献库
        </NavLink>
      </nav>

      {/* 文件夹分组 */}

      <div className="mt-6 flex min-h-0 flex-1 flex-col px-3">
        <div className="flex items-center justify-between px-3">
          <span className="text-xs font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-500">
            文件夹
          </span>
          <button
            onClick={startCreate}
            title="新建文件夹"
            aria-label="新建文件夹"
            className="rounded-md p-1 text-slate-400 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className="h-3.5 w-3.5"
              aria-hidden="true"
            >
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
        </div>

        <div className="mt-1.5 space-y-0.5 overflow-y-auto pb-2">
          {/* 新建输入行 */}

          {editing === "new" && (
            <input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void confirmEdit();
                if (e.key === "Escape") setEditing(null);
              }}
              onBlur={() => void confirmEdit()}
              placeholder="文件夹名称"
              maxLength={50}
              className="w-full rounded-lg border border-blue-500 px-3 py-1.5 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500/20 dark:bg-slate-800 dark:text-slate-100"
            />
          )}

          {folders.map((f) =>
            editing === f.folder_id ? (
              <input
                key={f.folder_id}
                ref={inputRef}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void confirmEdit();
                  if (e.key === "Escape") setEditing(null);
                }}
                onBlur={() => void confirmEdit()}
                maxLength={50}
                className="w-full rounded-lg border border-blue-500 px-3 py-1.5 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500/20 dark:bg-slate-800 dark:text-slate-100"
              />
            ) : (
              <div key={f.folder_id} className="group relative">
                <NavLink
                  to={`/folder/${f.folder_id}`}
                  className={({ isActive }) => navCls(isActive)}
                  title={f.name}
                >
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-4 w-4 shrink-0"
                    aria-hidden="true"
                  >
                    <path d="M4 5h5l2 2.5h9V19a1.5 1.5 0 0 1-1.5 1.5h-14A1.5 1.5 0 0 1 3 19V6.5A1.5 1.5 0 0 1 4.5 5z" />
                  </svg>
                  <span className="min-w-0 flex-1 truncate">{f.name}</span>
                  <span className="shrink-0 text-xs text-slate-400 dark:text-slate-500 group-hover:opacity-0">
                    {counts[f.folder_id] ?? 0}
                  </span>
                </NavLink>
                {/* hover 操作：重命名 / 删除（覆盖计数位置） */}

                <div className="absolute right-2 top-1/2 hidden -translate-y-1/2 items-center gap-0.5 group-hover:flex">
                  <button
                    onClick={() => startRename(f.folder_id, f.name)}
                    title="重命名"
                    aria-label={`重命名文件夹 ${f.name}`}
                    className="rounded p-1 text-slate-400 transition-colors duration-150 hover:bg-slate-200 hover:text-slate-700 dark:hover:bg-slate-700 dark:hover:text-slate-200"
                  >
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="h-3.5 w-3.5"
                      aria-hidden="true"
                    >
                      <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" />
                    </svg>
                  </button>
                  <button
                    onClick={() =>
                      setPendingDelete({ id: f.folder_id, name: f.name })
                    }
                    title="删除文件夹"
                    aria-label={`删除文件夹 ${f.name}`}
                    className="rounded p-1 text-slate-400 transition-colors duration-150 hover:bg-red-100 hover:text-red-600 dark:hover:bg-red-900/40 dark:hover:text-red-400"
                  >
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="h-3.5 w-3.5"
                      aria-hidden="true"
                    >
                      <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14" />
                    </svg>
                  </button>
                </div>
              </div>
            ),
          )}
          {folders.length === 0 && editing !== "new" && (
            <p className="px-3 py-1.5 text-xs text-slate-400 dark:text-slate-500">
              点击 + 新建文件夹
            </p>
          )}
        </div>
      </div>

      {/* 底部：Key 警示 + 设置 + 主题切换 */}

      <div className="space-y-0.5 border-t border-slate-200 px-3 py-3 dark:border-slate-700">
        {configLoaded && config && !isConfigured && (
          <div className="mb-1 flex items-center gap-1.5 rounded-md bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" aria-hidden="true" />
            请先配置 API Key
          </div>
        )}
        <NavLink to="/config" className={({ isActive }) => navCls(isActive)}>
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-4 w-4 shrink-0"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h0a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55h0a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v0a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z" />
          </svg>
          设置
        </NavLink>
        <button
          onClick={handleToggleTheme}
          className={navCls(false)}
          title={theme === "dark" ? "切换到亮色" : "切换到暗色"}
        >
          {theme === "dark" ? (
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-4 w-4 shrink-0"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
            </svg>
          ) : (
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-4 w-4 shrink-0"
              aria-hidden="true"
            >
              <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
            </svg>
          )}
          {theme === "dark" ? "亮色模式" : "暗色模式"}
        </button>
      </div>

      {/* 删除文件夹二次确认 */}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`删除文件夹「${pendingDelete?.name ?? ""}」？`}
        description="其中的文献不会被删除，将回到未分类。"
        confirmText="删除"
        danger
        onConfirm={() => void handleDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </aside>
  );
}
