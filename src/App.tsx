import { useEffect, useRef, useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import MainPage from "./components/MainPage";
import AddArticlePage from "./components/AddArticlePage";
import ConfigPage from "./components/ConfigPage";
import BilingualPage from "./components/BilingualPage";
import InlinePage from "./components/InlinePage";
import { useConfigStore } from "./stores/configStore";
import { useUiStore } from "./stores/uiStore";
import Layout from "./components/common/Layout";
import ErrorBoundary from "./components/common/ErrorBoundary";
import LoadingSpinner from "./components/common/LoadingSpinner";
import ConfirmDialog from "./components/common/ConfirmDialog";
import { logFrontend, listRunningTranslations, listRunningExports, waitForBackend, isTauri } from "./lib/bridge";
import { attach, currentTranslationKey } from "./lib/translationManager";
import { useBabelDocStore } from "./stores/babeldocStore";

function App() {
  const { isConfigured, configLoaded, config, loadConfig } = useConfigStore();
  const { theme, setTheme } = useUiStore();

  // 启动过渡收尾：React 已挂载——移除 index.html 静态
  // splash 并显示窗口（tauri.conf visible:false 起始隐藏，替代 ~3s 白屏）。
  // 放在最前的 effect 以尽早 show；浏览器 dev 无窗口可显，仅移除 splash。
  useEffect(() => {
    document.getElementById("splash")?.remove();
    if ("__TAURI_INTERNALS__" in window) {
      import("@tauri-apps/api/window")
        .then(({ getCurrentWindow }) => getCurrentWindow().show())
        .catch(() => {
          /* show 失败不阻断启动（index.html 10s 兜底会再试） */
        });
    }
  }, []);

  // 全局错误上报：渲染外未捕获的异常/Promise 拒绝落到后端 frontend.log，
  // 打包 exe 无控制台时这是排查"页面崩溃"的主要线索
  useEffect(() => {
    const onError = (e: ErrorEvent) => {
      logFrontend(
        "error",
        `window.onerror: ${e.message} @ ${e.filename}:${e.lineno}:${e.colno}`,
      );
    };
    const onRejection = (e: PromiseRejectionEvent) => {
      const reason = e.reason instanceof Error ? `${e.reason.message}\n${e.reason.stack}` : String(e.reason);
      logFrontend("error", `unhandledrejection: ${reason}`);
    };
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  // 启动时从 config.json（后端单一来源）灌入一次配置。
  // Key 已配置则 isConfigured 立即为真，主流程不再出现任何 Key 提示；
  // 仅当「已加载且确无 Key」时路由守卫才导向 /config。
  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  // 启动时用配置文件中的主题/模式初始化（来自 Rust 端落盘的 config.ui）
  useEffect(() => {
    if (config?.ui?.theme) setTheme(config.ui.theme as "light" | "dark");
  }, [config, setTheme]);

  // 主题变化即同步到 <html> 的 dark 类（Tailwind dark: 与 index.css 的 .dark body 都依赖它）
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  // 阅读页守卫三态：配置未加载完不跳转（避免启动闪跳 /config），加载完且确无 Key 才导向设置
  const readerGuard = !configLoaded ? (
    <LoadingSpinner text="加载配置…" />
  ) : isConfigured ? null : (
    <Navigate to="/config" replace />
  );

  // 后端就绪门：打包版 sidecar 是 66MB
  // onefile，首次启动需解压引导 + Defender 扫描新装 exe，可能耗时数十秒——
  // 此前文献库等首查失败无重试，永挂"加载文献库…"。就绪前全屏等待，
  // 不渲染任何会话发请求的页面。浏览器 dev 模式后端由脚本预先拉起，跳过。
  // 注意：本组件的所有 hook 必须先于此早退声明（hook 数量跨渲染必须一致，
  // 否则就绪翻转时 React 直接崩树——打包版白屏根因）。
  const [backendReady, setBackendReady] = useState(!isTauri());
  useEffect(() => {
    if (!isTauri()) return;
    let alive = true;
    void waitForBackend().then((ok) => {
      if (!alive) return;
      if (!ok) {
        logFrontend("error", "backend not ready after 120s — sidecar 可能启动失败");
      }
      setBackendReady(true); // 超限也放行：各页面错误态可见，好过永久等待
    });
    return () => {
      alive = false;
    };
  }, []);

  // 翻译任务自动重接管：F5 整页重载后前端 job_id 丢失，后端任务
  // 孤儿化继续跑。配置加载完成后查一次 running 列表，发现未完成任务弹自绘
  // 确认，确认后 attach 恢复进度与流式渲染；忽略/无任务均不打扰。
  const [reattach, setReattach] = useState<{
    jobId: string;
    filePath: string;
    fileName: string;
    progress: number;
  } | null>(null);
  const reattachChecked = useRef(false);
  useEffect(() => {
    if (!configLoaded || reattachChecked.current) return;
    reattachChecked.current = true;
    const discover = async (attempt: number): Promise<void> => {
      if (currentTranslationKey()) return;
      try {
        const [pipeline, exports] = await Promise.all([
          listRunningTranslations(),
          listRunningExports(),
        ]);
        // BabelDOC 运行中：静默重接管（无需既定决策，进度卡在其模式内呈现；
        // 用户点忽略也不丢——任务照跑，产物完成自动落缓存）
        const ex = exports[0];
        if (ex) {
          useBabelDocStore
            .getState()
            .reattachRunning(ex.job_id, ex.file_path, Math.round(ex.progress));
          // F5 时若停在阅读页，恢复到对照模式：会话已清空，重接管视图
          // （进度/产物）在排版对照里呈现，工具栏状态与内容保持一致
          if (window.location.pathname.startsWith("/reader")) {
            useUiStore.getState().setReaderMode("original_bilingual");
          }
        }
        // 翻译运行中：弹确认（接管会改变当前阅读视图，需用户点头）
        if (pipeline.length > 0 && !currentTranslationKey()) {
          const j = pipeline[0];
          const fileName = (j.file_path.split(/[\\/]/).pop() ?? "文档").replace(
            /\.pdf$/i,
            "",
          );
          setReattach({
            jobId: j.job_id,
            filePath: j.file_path,
            fileName,
            progress: Math.round(j.progress),
          });
        }
      } catch {
        // 后端未就绪（浏览器模式手动启动等）：最多再等两轮
        if (attempt < 2) {
          setTimeout(() => void discover(attempt + 1), 5000);
        }
      }
    };
    void discover(0);
  }, [configLoaded]);

  // 就绪门：所有 hook 已声明完毕，此处早退安全（hook 数量跨渲染一致）
  if (!backendReady) {
    return (
      <LoadingSpinner text="正在启动本地服务…（首次启动需初始化运行环境，约需半分钟）" />
    );
  }

  return (
    <ErrorBoundary>
      <Layout>
        <Routes>
          <Route path="/" element={<MainPage />} />
          <Route path="/add" element={<AddArticlePage />} />
          <Route path="/folder/:folderId" element={<MainPage />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route
            path="/reader/bilingual"
            element={readerGuard ?? <BilingualPage />}
          />
          <Route
            path="/reader/inline"
            element={readerGuard ?? <InlinePage />}
          />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </Layout>
      <ConfirmDialog
        open={reattach !== null}
        title="检测到未完成的翻译"
        description={
          reattach
            ? `「${reattach.fileName}」仍在后台翻译中（当前 ${reattach.progress}%）。继续后将恢复进度与实时译文，已翻译内容不会重复计费。`
            : ""
        }
        confirmText="继续翻译"
        cancelText="忽略"
        onConfirm={() => {
          if (!reattach) return;
          const r = attach(reattach.jobId, reattach.filePath);
          if (r.ok) setReattach(null);
          // 接管失败（理论上仅并发接管冲突）保留弹窗可再次确认
        }}
        onCancel={() => setReattach(null)}
      />
    </ErrorBoundary>
  );
}

export default App;
