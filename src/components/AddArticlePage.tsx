import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { startTranslation, currentTranslationKey } from "../lib/translationManager";
import { startBabeldocUpload } from "../lib/babeldocUpload";
import { useConfigStore } from "../stores/configStore";
import { useUiStore } from "../stores/uiStore";
import UploadModeToggle from "./common/UploadModeToggle";
import { openFileDialog, uploadFile, isTauri } from "../lib/bridge";

/**
 * 添加文章页：
 * 大标题 + 卡片内虚线拖拽区。选择文件后交给 translationManager 后台翻译
 * （轮询不再依赖本页组件生命周期），成功立即返回文档库——进度内联显示，
 * 不占用阅读会话，翻译途中可自由打开其他文献。
 */
export default function AddArticlePage() {
  const navigate = useNavigate();
  const [isDragging, setIsDragging] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const uploadMode = useUiStore((s) => s.uploadMode);

  // 按当前选中的翻译模式分流：BabelDOC 模式入库后直接对照生成；
  // 重排版模式交给 translationManager 后台翻译（成功回文档库看进度）。
  // 无 Key 前置拦截：未配置直接提示并引导去设置，不发无效任务
  const handleFile = async (path: string) => {
    const { configLoaded, isConfigured } = useConfigStore.getState();
    if (configLoaded && !isConfigured) {
      setLocalError("请先在设置中配置 API Key，再添加文章");
      return;
    }
    const fileName = path.split(/[\\/]/).pop() || path;
    if (useUiStore.getState().uploadMode === "babeldoc") {
      // 生成器与主翻译并发是内存耗尽风险（DualPdfPage 门禁同口径）
      if (currentTranslationKey()) {
        setLocalError("已有翻译任务进行中，BabelDOC 对照需等待其完成后再添加");
        return;
      }
      const err = await startBabeldocUpload(path, fileName);
      if (err) {
        setLocalError(err);
        return;
      }
      navigate("/reader/bilingual");
      return;
    }
    const r = await startTranslation(path, fileName);
    if (r.ok) {
      navigate("/");
    } else {
      setLocalError(r.reason);
    }
  };

  // Tauri 环境下监听 OS 文件拖拽（本页挂载期间整页生效；MainPage 的监听
  // 随其卸载而移除，/add 页必须自持一份，否则拖放无响应）
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    if ("__TAURI_INTERNALS__" in window) {
      import("@tauri-apps/api/webview")
        .then(({ getCurrentWebview }) => {
          getCurrentWebview()
            .onDragDropEvent((event) => {
              const payload = event.payload;
              if (payload.type === "enter" || payload.type === "over") {
                setIsDragging(true);
              } else if (payload.type === "leave") {
                setIsDragging(false);
              } else if (payload.type === "drop") {
                setIsDragging(false);
                const paths = payload.paths;
                if (paths && paths.length > 0 && paths[0].toLowerCase().endsWith(".pdf")) {
                  void handleFile(paths[0]);
                }
              }
            })
            .then((fn) => {
              unlisten = fn;
            });
        })
        .catch(() => {
          /* 非 Tauri 环境忽略 */
        });
    }
    return () => unlisten?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleBrowse = async () => {
    const selected = await openFileDialog();
    if (!selected) return;
    if (!selected.toLowerCase().endsWith(".pdf")) {
      setLocalError("仅支持 PDF 文件");
      return;
    }
    void handleFile(selected);
  };

  const handleDrop = async (e: React.DragEvent) => {
    if (isTauri()) return;
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setLocalError("仅支持 PDF 文件");
      return;
    }
    const path = await uploadFile(file);
    if (path) void handleFile(path);
  };

  return (
    <div className="mx-auto max-w-3xl pt-8">
      <Link
        to="/"
        className="inline-flex items-center gap-1 rounded-md px-2 py-1.5 text-sm font-medium text-slate-500 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
      >
        ← 返回文档库
      </Link>

      <h1 className="mt-8 text-center text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-100 sm:text-3xl">
        添加文献至你的阅读列表
      </h1>

      <div className="card mt-8 p-4 sm:p-5">
        {/* 第一步：选翻译模式（选完再传；记住上次选择） */}
        <p className="text-sm font-medium text-slate-700 dark:text-slate-300">
          选择翻译模式
        </p>
        <div className="mt-3">
          <UploadModeToggle />
        </div>

        <p className="mt-5 flex items-center gap-1.5 border-t border-slate-100 pt-4 text-sm font-medium text-slate-700 dark:border-slate-700 dark:text-slate-300">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-4 w-4"
            aria-hidden="true"
          >
            <path d="M12 16V4M7 9l5-5 5 5" />
            <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
          </svg>
          上传本地文件
        </p>

        <div
          role="button"
          tabIndex={0}
          aria-label="拖入或选择 PDF 文件"
          onClick={handleBrowse}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              handleBrowse();
            }
          }}
          onDragOver={(e) => {
            if (isTauri()) return;
            e.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={handleDrop}
          className={`mt-3 flex h-64 cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed transition-colors duration-150 sm:h-72 ${
            isDragging
              ? "border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-900/20"
              : "border-slate-300 hover:border-blue-400 dark:border-slate-600 dark:hover:border-blue-500"
          }`}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-10 w-10 text-slate-300 dark:text-slate-600"
            aria-hidden="true"
          >
            <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z" />
            <path d="M14.5 3v4h4" />
            <circle cx="12" cy="13" r="2.5" />
            <path d="M12 11.5v3M10.75 13h2.5" />
          </svg>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            将 PDF 拖放到这里，或{" "}
            <span className="font-medium text-blue-600 hover:underline dark:text-blue-400">
              点击上传
            </span>
          </p>
          <p className="text-xs text-slate-400 dark:text-slate-500">
            {uploadMode === "babeldoc"
              ? "仅支持 PDF 文件 · 生成完成后自动打开对照视图"
              : "仅支持 PDF 文件 · 翻译完成后自动加入文档库"}
          </p>
        </div>

        {localError && (
          <div
            role="alert"
            className="mt-3 text-sm text-red-600 dark:text-red-400"
          >
            <p>{localError}</p>
            {/API Key/.test(localError) && (
              <Link
                to="/config"
                className="mt-1 inline-block font-medium text-blue-600 underline underline-offset-2 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
              >
                前往设置 →
              </Link>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
