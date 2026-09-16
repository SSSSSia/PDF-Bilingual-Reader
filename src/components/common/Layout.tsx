import { Link, useLocation } from "react-router-dom";
import { useConfigStore } from "../../stores/configStore";
import { useUiStore } from "../../stores/uiStore";
import Sidebar from "../Sidebar";
import TitleBar from "./TitleBar";

/**
 * 布局：
 * 壳层 = 自绘 TitleBar（标题栏） + 内容行（flex-1 min-h-0）。
 * - 阅读页（/reader/*）：无侧边栏，main 为纵向 flex 容器（工具栏+通顶内容），
 * 由页面组件自己管理滚动——阅读面积从 ~730px 提升到 ~960px（1080p）。
 * - 主页/设置/添加：保留侧边栏 + max-w 居中 + 页内滚动（原视觉不变）。
 * - <md 窄窗口降级顶栏仅在非阅读页保留（阅读页工具栏自带换行降级）。
 */
export default function Layout({ children }: { children: React.ReactNode }) {
  const { config, isConfigured, configLoaded, setTheme: saveTheme } =
    useConfigStore();
  const { theme, toggleTheme, setTheme } = useUiStore();
  const location = useLocation();
  const isReader = location.pathname.startsWith("/reader");

  const handleToggle = async () => {
    const next = theme === "dark" ? "light" : "dark";
    toggleTheme();
    setTheme(next);
    // 主题持久化走配置单一来源（落盘 config.json 的 ui.theme），
    // 下次启动由 App 启动灌入自动恢复。
    await saveTheme(next);
  };

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <TitleBar />
      <div className="flex min-h-0 flex-1">
        {/* 侧边栏：仅主页/设置等非阅读视图 */}

        {!isReader && (
          <div className="hidden md:block">
            <Sidebar />
          </div>
        )}

        {/* 内容区 */}

        <div className="flex min-w-0 flex-1 flex-col">
          {/* 窄窗口降级顶栏（<md，仅非阅读页；阅读页由工具栏自行换行降级） */}

          {!isReader && (
            <nav className="sticky top-0 z-30 border-b border-slate-200 bg-white md:hidden dark:border-slate-700 dark:bg-slate-900">
              <div className="flex h-12 items-center justify-between px-4">
                <Link to="/" className="flex items-center gap-2">
                  <span
                    className="flex h-6 w-6 items-center justify-center rounded bg-blue-600 text-xs font-bold text-white"
                    aria-hidden="true"
                  >
                    译
                  </span>
                  <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                    PDF双语阅读器
                  </span>
                </Link>
                <div className="flex items-center gap-1">
                  <button
                    onClick={handleToggle}
                    title={theme === "dark" ? "切换到亮色" : "切换到暗色"}
                    aria-label={theme === "dark" ? "切换到亮色" : "切换到暗色"}
                    className="rounded-md px-2 py-1.5 text-base leading-none text-slate-500 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
                  >
                    {theme === "dark" ? "🌞" : "🌙"}
                  </button>
                  <Link
                    to="/config"
                    aria-current={location.pathname === "/config" ? "page" : undefined}
                    className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150 ${
                      location.pathname === "/config"
                        ? "bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100"
                        : "text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
                    }`}
                  >
                    设置
                  </Link>
                </div>
              </div>
              {/* 只在「已加载且确无 Key」时提示，避免启动加载瞬间误报 */}

              {configLoaded && config && !isConfigured && (
                <div className="flex items-center gap-1.5 border-t border-slate-200 px-4 py-1.5 text-xs font-medium text-amber-700 dark:border-slate-700 dark:text-amber-400">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden="true" />
                  请先配置 API Key
                </div>
              )}
            </nav>
          )}

          <main
            className={
              isReader
                ? /* 阅读页：通顶 flex 容器，滚动交给页面组件 */
                  "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
                : /* 主页/设置：保持原 max-w 居中 + 页内滚动 */
                  "mx-auto w-full max-w-6xl flex-1 min-h-0 overflow-y-auto px-6 py-6"
            }
          >
            {children}
          </main>
        </div>
      </div>
    </div>
  );
}
