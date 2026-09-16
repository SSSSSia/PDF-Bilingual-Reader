import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { usePdfStore } from "../stores/pdfStore";
import { useUiStore } from "../stores/uiStore";
import { useLibraryStore } from "../stores/libraryStore";
import { useSessionsStore } from "../stores/sessionsStore";
import { useConfigStore } from "../stores/configStore";
import {
  startTranslation,
  attach,
  currentTranslationKey,
  EXTRACT_DONE,
} from "../lib/translationManager";
import ConfirmDialog from "./common/ConfirmDialog";
import {
  openFileDialog,
  uploadFile,
  isTauri,
  openDoc,
  listRunningTranslations,
  renameDoc,
} from "../lib/bridge";
import { useDocThumbnails } from "../hooks/useDocThumbnails";
import type { DocMeta } from "../types";
import LoadingSpinner from "./common/LoadingSpinner";

/**
 * 文献库：
 * - 左侧边栏（Layout/Sidebar）负责导航与文件夹分组，本页负责网格内容；
 * - /folder/:folderId 进入文件夹视图（标题为文件夹名，仅显示归档文献）；
 * - 卡片：小尺寸居中首页缩略图 + 两行标题 + 元信息，hover 出「⋯」移动菜单；
 * - 空状态保留整块拖拽上传区；有文献后仍支持整页拖入 PDF；
 * - 翻译进度内联显示，OCR 完成自动进入阅读页；点击卡片缓存秒开。
 */

export default function MainPage() {
  const { folderId } = useParams();
  const { pages, isLoading, error, setError } = usePdfStore();
  const { mode } = useUiStore();
  const { docs, folders, loaded, fetchAll, moveDoc } = useLibraryStore();
  const sessions = useSessionsStore((s) => s.sessions);
  // 进行中的翻译任务：与"当前活跃阅读会话"解耦
  const runningJob = sessions.find((s) => s.job?.status === "running");
  const navigate = useNavigate();
  const [isDragging, setIsDragging] = useState(false);
  // 跳转时机由「全部翻译完成」提前到「pages 就绪（OCR 完成）」。
  // 用 ref 保证同一文件只跳一次；处理新文件时重置。
  const navigatedRef = useRef(false);
  const [openingId, setOpeningId] = useState<string | null>(null);
  // 扩展：打开文献时发现后端仍在翻译 → 提示接管
  const [attachPrompt, setAttachPrompt] = useState<{
    jobId: string;
    filePath: string;
    title: string;
    progress: number;
    doc: DocMeta;
  } | null>(null);
  // 打开文献遇「提取缓存已不存在」（TEXT_LAYER_MODEL 版本熔断/缓存清空）
  // → 确认后重新提取并复用译文缓存
  const [reextract, setReextract] = useState<{ doc: DocMeta } | null>(null);
  const [query, setQuery] = useState("");
  // 「移动到文件夹」菜单当前展开的文档（null = 关闭）
  const [menuDoc, setMenuDoc] = useState<DocMeta | null>(null);
  // 行内重命名——卡片标题变输入框
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);
  const thumbs = useDocThumbnails(docs);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  useEffect(() => {
    if (renamingId) renameInputRef.current?.select();
  }, [renamingId]);

  const startRename = (doc: DocMeta) => {
    setMenuDoc(null);
    setRenameDraft(doc.title);
    setRenamingId(doc.doc_id);
  };

  const commitRename = async () => {
    const docId = renamingId;
    const title = renameDraft.trim();
    setRenamingId(null);
    if (!docId || !title) return;
    try {
      await renameDoc(docId, title);
      await fetchAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // 自动跳转时机（实测反馈：原「pages 首次非空」跳太早——后端
  // 逐页提取，多页文档首次 page 就绪仅 progress≈8-10，落进阅读页时原文
  // 还在一页页长、布局持续跳动）。改为等提取阶段完成（progress≥EXTRACT_DONE，
  // 原文排版定型）才进入；此后译文仍逐段流入，边译边读不变。
  // 仅「本页挂载时 pages 为空 → 变就绪」才触发：用户翻译途中主动回到
  // 文献库（挂载时已就绪）不会被弹回阅读页。
  const pagesWereEmptyRef = useRef(pages.length === 0);
  const extractReady =
    !!runningJob && (runningJob.job?.progress ?? 0) >= EXTRACT_DONE;
  useEffect(() => {
    if (
      isLoading &&
      extractReady &&
      pages.length > 0 &&
      pagesWereEmptyRef.current &&
      !navigatedRef.current
    ) {
      navigatedRef.current = true;
      navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
    }
  }, [isLoading, extractReady, pages, mode, navigate]);

  // Tauri 环境下监听 OS 文件拖拽（整页生效；HTML5 drop 在 Tauri 中会被拦截）
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
                if (paths && paths.length > 0) {
                  handlePath(paths[0]);
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
  }, []);

  const handlePath = async (selected: string) => {
    if (!selected.toLowerCase().endsWith(".pdf")) {
      return;
    }
    // 无 Key 前置拦截：configLoaded 三态防启动误报，
    // 后端 run_pipeline 入口也会 fail-fast，这里省一次无效上传。
    // 错误文案含"API Key"→ 下方错误卡自动出现"前往设置"引导按钮。
    const { configLoaded, isConfigured } = useConfigStore.getState();
    if (configLoaded && !isConfigured) {
      setError("请先在设置中配置 API Key，再添加文章");
      return;
    }
    // 同一时间仅 1 个翻译任务（后端支持并发，先按当前需求收口）；
    // 翻译不再占用阅读会话——进行中可自由打开其他文献
    if (currentTranslationKey()) {
      setError("已有翻译任务进行中，请等待完成后再添加新任务");
      return;
    }
    const fileName = selected.split(/[\\/]/).pop() || selected;
    // 不 await 轮询完成：跳转仍由上方 pages 就绪 effect 驱动；
    // 轮询在 translationManager 后台进行，MainPage 卸载不受影响。
    navigatedRef.current = false;
    pagesWereEmptyRef.current = true; // 本页发起的新翻译：pages 就绪后自动跳转
    const r = await startTranslation(selected, fileName);
    if (!r.ok) setError(r.reason);
  };

  const handleBrowse = async () => {
    const selected = await openFileDialog();
    if (!selected) return;
    await handlePath(selected);
  };

  // 浏览器模式下 HTML5 拖拽（整页容器接收；Tauri 模式走 webview 事件，这里跳过）
  const handleDrop = async (e: React.DragEvent) => {
    if (isTauri()) return;
    e.preventDefault();
    setIsDragging(false);
    const dropped = e.dataTransfer.files?.[0];
    if (!dropped) return;
    if (!dropped.name.toLowerCase().endsWith(".pdf")) return;
    const path = await uploadFile(dropped);
    if (path) await handlePath(path);
  };

  /** 点击进度卡进入翻译会话：若当前活跃会话不是这一篇，
   * 先快照停靠当前会话并换入翻译会话，再导航——不打断正在读的文献。 */
  const handleProgressClick = () => {
    if (!runningJob) return;
    // 提取未完成（原文未排版定型）时进入只会看到不完整空页，禁止
    if ((runningJob.job?.progress ?? 0) < EXTRACT_DONE) return;
    if (usePdfStore.getState().sessionKey !== runningJob.key) {
      useSessionsStore.getState().activate(runningJob.key);
    }
    navigatedRef.current = true;
    navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
  };

  /** 打开已翻译文章：已有页签直接激活；否则停靠当前会话
   * → 缓存重建秒开 → 注册新会话。翻译进行中不再互斥（轮询已后台化）。 */
  const handleOpenDoc = async (
    doc: DocMeta,
    opts?: { skipAttachCheck?: boolean },
  ) => {
    if (openingId) return;
    const sessions = useSessionsStore.getState();
    // 已是该活跃会话（或内存中装的就是这一篇但未登记）→ 直接回阅读页
    if (usePdfStore.getState().sessionKey === doc.doc_id) {
      navigatedRef.current = true;
      navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
      return;
    }
    // 已有页签 → 快照交换激活
    if (sessions.getByKey(doc.doc_id)) {
      sessions.activate(doc.doc_id);
      navigatedRef.current = true;
      navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
      return;
    }
    setOpeningId(doc.doc_id);
    setMenuDoc(null);
    setError(null);
    // 扩展：该文档的翻译仍在后端运行（上传后 F5/关开应用场景）
    // → 提示接管继续，而非按"已完成"打开（此刻缓存只有部分页）
    if (!opts?.skipAttachCheck) {
      const running = await listRunningTranslations().catch(() => []);
      const hit = running.find(
        (r) =>
          r.file_path.toLowerCase() === doc.file_path.toLowerCase() ||
          r.file_path.split(/[\\/]/).pop()?.toLowerCase() ===
            doc.file_path.split(/[\\/]/).pop()?.toLowerCase(),
      );
      if (hit) {
        setOpeningId(null);
        setAttachPrompt({
          jobId: hit.job_id,
          filePath: doc.file_path,
          title: doc.title,
          progress: Math.round(hit.progress),
          doc,
        });
        return;
      }
    }
    try {
      const r = await openDoc(doc.doc_id);
      // 停靠当前活跃会话（可能是另一篇文献，也可能是翻译中的任务）
      sessions.captureActive();
      const pdf = usePdfStore.getState();
      pdf.setFile({
        name: `${doc.title}.pdf`,
        size: 0,
        type: "application/pdf",
        path: r.file_exists ? doc.file_path : "",
      } as any);
      // 源文件缺失：filePath 置 null（原版模式/式按钮自动禁用），对照/紧跟纯缓存可用
      pdf.setFilePath(r.file_exists ? doc.file_path : null);
      pdf.setPages(r.pages);
      pdf.setResult(null);
      pdf.setCurrentPage(0);
      pdf.setError(null);
      pdf.setLoading(false);
      pdf.setProgress(0);
      pdf.setSessionKey(doc.doc_id);
      sessions.register({
        key: doc.doc_id,
        title: doc.title,
        kind: "doc",
        snapshot: {
          filePath: r.file_exists ? doc.file_path : null,
          fileName: `${doc.title}.pdf`,
          pages: r.pages,
          currentPage: 0,
          result: null,
          error: null,
        },
      });
      navigatedRef.current = true; // 直接导航，避免 pages effect 重复跳转
      navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // 提取缓存失效且源文件路径在：给一键重提取入口，点卡片不再死路
      if (msg.includes("提取缓存") && doc.file_path) {
        setReextract({ doc });
      } else {
        setError(msg);
      }
    } finally {
      setOpeningId(null);
    }
  };

  const currentFolder = folderId
    ? folders.find((f) => f.folder_id === folderId)
    : undefined;

  const folderDocs = useMemo(
    () =>
      folderId ? docs.filter((d) => d.folder_id === folderId) : docs,
    [docs, folderId],
  );
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return folderDocs;
    return folderDocs.filter((d) => d.title.toLowerCase().includes(q));
  }, [folderDocs, query]);

  const hasDocs = folderDocs.length > 0;

  return (
    <div
      className="relative mx-auto max-w-5xl"
      onDragOver={(e) => {
        if (isTauri()) return;
        e.preventDefault();
        setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={handleDrop}
    >
      {/* 整页拖入提示层（有文献时拖拽反馈；空状态由大拖拽区自行高亮） */}

      {isDragging && hasDocs && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-blue-500 bg-blue-50/80 dark:border-blue-400 dark:bg-blue-900/30">
          <p className="text-sm font-medium text-blue-600 dark:text-blue-300">
            松开即可开始翻译
          </p>
        </div>
      )}

      {/* 页头：标题 + 添加文章按钮 */}

      <header className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-100">
          {currentFolder ? currentFolder.name : "文献库"}
        </h1>
        <button
          onClick={() => navigate("/add")}
          className="btn-primary inline-flex items-center gap-1.5"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            className="h-4 w-4"
            aria-hidden="true"
          >
            <path d="M12 5v14M5 12h14" />
          </svg>
          添加文章
        </button>
      </header>

      {/* 空状态：整块拖拽上传区作主视觉（入口不丢弃）；文件夹不存在单独提示 */}

      {loaded && folderId && !currentFolder && (
        <p className="mt-10 text-center text-sm text-slate-500 dark:text-slate-400">
          该文件夹不存在或已被删除。
        </p>
      )}
      {loaded && !folderId && !hasDocs && (
        <div
          role="button"
          tabIndex={0}
          aria-label="拖入或选择 PDF 文件开始翻译"
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
          className={`mt-8 flex cursor-pointer flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-16 text-center transition-colors duration-150 sm:py-20 ${
            isDragging
              ? "border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-900/20"
              : "border-slate-300 hover:border-blue-400 hover:bg-white dark:border-slate-600 dark:hover:border-blue-500 dark:hover:bg-slate-800/60"
          }`}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-12 w-12 text-slate-300 dark:text-slate-600"
            aria-hidden="true"
          >
            <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z" />
            <path d="M14.5 3v4h4" />
            <path d="M12 11v6M9.5 14.5 12 17l2.5-2.5" />
          </svg>
          <p className="text-base font-medium text-slate-900 dark:text-slate-100">
            {isDragging ? "松开即可开始翻译" : "将 PDF 拖放到这里"}
          </p>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            或点击选择文件 · 翻译完成后自动加入文献库
          </p>
        </div>
      )}

      {!loaded && (
        <div className="mt-8">
          <LoadingSpinner text="加载文献库…" />
        </div>
      )}

      {/* 计数 + 搜索行：有文献，或文件夹视图（空文件夹也显示，避免整页空白） */}

      {(hasDocs || (loaded && currentFolder)) && (
        <div className="mt-5 flex items-center justify-between gap-4">
          <p className="shrink-0 text-sm text-slate-500 dark:text-slate-400">
            共 {folderDocs.length} 篇文献
          </p>
          <div className="relative w-56 sm:w-64">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400 dark:text-slate-500"
              aria-hidden="true"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索文献"
              aria-label="搜索文献"
              className="w-full rounded-lg border border-slate-300 bg-white py-1.5 pl-8 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:border-blue-400"
            />
          </div>
        </div>
      )}

      {/* 翻译进行中：内联进度卡（后台轮询驱动，与活跃阅读会话解耦；
          提取完成（progress≥30，原文排版定型）后才可点击进入阅读页） */}
      {runningJob && runningJob.job && (
        <div
          role="button"
          tabIndex={0}
          onClick={handleProgressClick}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              handleProgressClick();
            }
          }}
          title={
            runningJob.job.progress >= EXTRACT_DONE
              ? "点击进入阅读页（翻译继续中，可边译边读）"
              : "正在结构化解析原文（公式/版式识别），完成后自动进入"
          }
          className={`card mt-5 px-4 py-3.5 animate-fade-in transition-colors duration-150 ${
            runningJob.job.progress >= EXTRACT_DONE
              ? "cursor-pointer hover:border-blue-400 dark:hover:border-blue-500"
              : "cursor-default"
          }`}
        >
          <div className="flex items-center justify-between gap-3">
            <p className="min-w-0 truncate text-sm text-slate-700 dark:text-slate-300">
              {runningJob.job.progress >= EXTRACT_DONE
                ? "正在翻译："
                : "结构化解析原文："}
              <span className="font-medium text-slate-900 dark:text-slate-100">
                {runningJob.snapshot.fileName}
              </span>
              {runningJob.job.progress >= EXTRACT_DONE && (
                <span className="ml-2 text-xs text-blue-600 dark:text-blue-400">
                  点击进入 →
                </span>
              )}
            </p>
            <span className="shrink-0 text-xs text-slate-500 dark:text-slate-400">
              {Math.round(runningJob.job.progress)}%
            </span>
          </div>
          <div
            role="progressbar"
            aria-valuenow={Math.round(runningJob.job.progress)}
            aria-valuemin={0}
            aria-valuemax={100}
            className="mt-2.5 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
            style={{ height: 6 }}
          >
            <div
              className="h-full rounded-full bg-blue-600 transition-all duration-300"
              style={{
                width: `${Math.max(2, Math.round(runningJob.job.progress))}%`,
              }}
            />
          </div>
        </div>
      )}

      {/* 文献网格：小尺寸居中首页缩略图卡片（3 列） */}

      {hasDocs && filtered.length > 0 && (
        <ul className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((d) => (
            <li key={d.doc_id} className="group relative">
              {/* 卡片主体（div+role：内部还要放「⋯」按钮，避免 button 嵌套） */}

              <div
                role="button"
                tabIndex={0}
                onClick={() => void handleOpenDoc(d)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    void handleOpenDoc(d);
                  }
                }}
                title={
                  d.file_exists === false
                    ? "源 PDF 已移动/删除：对照与紧跟模式可用，原版模式不可用"
                    : "打开已翻译文件"
                }
                className={`card h-full cursor-pointer p-3 transition-colors duration-150 hover:border-slate-400 dark:hover:border-slate-500 ${
                  openingId ? "cursor-wait opacity-60" : ""
                }`}
              >
                {/* 缩略图：小尺寸居中（白边留白），不再整卡满铺 */}

                <div className="flex h-44 items-center justify-center overflow-hidden rounded-lg bg-slate-100 p-3 dark:bg-slate-800">
                  {thumbs[d.doc_id] ? (
                    <img
                      src={thumbs[d.doc_id]}
                      alt=""
                      className="max-h-full max-w-full rounded-sm object-contain shadow-sm ring-1 ring-slate-200 dark:ring-slate-700"
                      draggable={false}
                    />
                  ) : (
                    <div className="flex flex-col items-center gap-1.5 text-slate-400 dark:text-slate-500">
                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        className="h-9 w-9"
                        aria-hidden="true"
                      >
                        <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z" />
                        <path d="M14.5 3v4h4" />
                      </svg>
                      <span className="text-xs font-medium tracking-wide">
                        {d.file_exists === false ? "源文件缺失" : "PDF"}
                      </span>
                    </div>
                  )}
                </div>
                {/* 标题（两行截断） + 元信息；行内重命名态切换为输入框 */}

                <div className="px-1 pb-1 pt-2.5">
                  {renamingId === d.doc_id ? (
                    <input
                      ref={renameInputRef}
                      value={renameDraft}
                      onChange={(e) => setRenameDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void commitRename();
                        if (e.key === "Escape") setRenamingId(null);
                      }}
                      onBlur={() => void commitRename()}
                      onClick={(e) => e.stopPropagation()}
                      maxLength={100}
                      className="w-full rounded-md border border-blue-500 px-1.5 py-1 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-500/20 dark:bg-slate-800 dark:text-slate-100"
                      aria-label="重命名文献"
                    />
                  ) : (
                    <p
                      className="line-clamp-2 min-h-[2.5em] text-sm font-medium leading-snug text-slate-900 dark:text-slate-100"
                      title={d.title}
                    >
                      {d.title}
                    </p>
                  )}
                  <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
                    {d.translated_at} · {d.page_count} 页
                  </p>
                </div>
              </div>

              {/* 「⋯」移动到文件夹菜单（hover 显现；阻止冒泡不触发打开） */}

              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setMenuDoc(menuDoc?.doc_id === d.doc_id ? null : d);
                }}
                title="移动到文件夹"
                aria-label={`移动 ${d.title} 到文件夹`}
                className={`absolute right-4 top-4 z-10 rounded-md bg-white/90 p-1 text-slate-500 shadow-sm ring-1 ring-slate-200 transition-opacity duration-150 hover:text-slate-800 dark:bg-slate-900/90 dark:ring-slate-700 dark:hover:text-slate-100 ${
                  menuDoc?.doc_id === d.doc_id
                    ? "opacity-100"
                    : "opacity-0 group-hover:opacity-100"
                }`}
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  className="h-4 w-4"
                  aria-hidden="true"
                >
                  <circle cx="5" cy="12" r="1.6" />
                  <circle cx="12" cy="12" r="1.6" />
                  <circle cx="19" cy="12" r="1.6" />
                </svg>
              </button>
              {menuDoc?.doc_id === d.doc_id && (
                <>
                  {/* 点击菜单外区域关闭 */}

                  <div
                    className="fixed inset-0 z-20"
                    onClick={(e) => {
                      e.stopPropagation();
                      setMenuDoc(null);
                    }}
                  />
                  <div className="absolute right-4 top-11 z-30 w-44 rounded-lg border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-800">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        startRename(d);
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-slate-700 transition-colors duration-150 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-700"
                    >
                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        className="h-3.5 w-3.5 shrink-0"
                        aria-hidden="true"
                      >
                        <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" />
                      </svg>
                      重命名
                    </button>
                    <p className="px-3 py-1.5 text-xs font-semibold text-slate-400 dark:text-slate-500">
                      移动到文件夹
                    </p>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void moveDoc(d.doc_id, null).then(() => setMenuDoc(null));
                      }}
                      className="flex w-full items-center justify-between px-3 py-1.5 text-left text-sm text-slate-700 transition-colors duration-150 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-700"
                    >
                      未分类
                      {!d.folder_id && (
                        <span className="text-xs text-blue-600 dark:text-blue-400">✓</span>
                      )}
                    </button>
                    {folders.map((f) => (
                      <button
                        key={f.folder_id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void moveDoc(d.doc_id, f.folder_id).then(() =>
                            setMenuDoc(null),
                          );
                        }}
                        className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm text-slate-700 transition-colors duration-150 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-700"
                      >
                        <span className="min-w-0 truncate">{f.name}</span>
                        {d.folder_id === f.folder_id && (
                          <span className="shrink-0 text-xs text-blue-600 dark:text-blue-400">✓</span>
                        )}
                      </button>
                    ))}
                    {folders.length === 0 && (
                      <p className="px-3 py-1.5 text-xs text-slate-400 dark:text-slate-500">
                        尚无文件夹，可在左侧新建
                      </p>
                    )}
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* 空文件夹：虚线空态提示（计数/搜索行已在上方显示） */}

      {loaded && currentFolder && !hasDocs && (
        <div className="mt-6 flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-300 px-6 py-14 text-center dark:border-slate-600">
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
            <path d="M4 5h5l2 2.5h9V19a1.5 1.5 0 0 1-1.5 1.5h-14A1.5 1.5 0 0 1 3 19V6.5A1.5 1.5 0 0 1 4.5 5z" />
          </svg>
          <p className="text-sm font-medium text-slate-900 dark:text-slate-100">
            该文件夹暂无文献
          </p>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            在文献库中点击卡片右上角「⋯」即可将文献移入
          </p>
        </div>
      )}

      {/* 搜索/过滤无结果 */}

      {hasDocs && filtered.length === 0 && (
        <p className="mt-10 text-center text-sm text-slate-500 dark:text-slate-400">
          {query.trim()
            ? `没有匹配「${query}」的文献`
            : "该文件夹暂无文献 · 通过卡片右上角「⋯」归档"}
        </p>
      )}

      {openingId && (
        <div className="mt-4 animate-fade-in">
          <LoadingSpinner text="正在打开…" />
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800/60 dark:bg-red-900/20 animate-fade-in"
        >
          <p className="text-sm text-red-700 dark:text-red-300">
            {String(error)}
          </p>
          {/* Key 平时不再打扰；仅认证类失败时才引导进入设置 */}

          {/401|403|api[ _-]?key|认证|unauthorized|invalid[ _-]?key/i.test(
            String(error),
          ) && (
            <button
              onClick={() => navigate("/config")}
              className="mt-2 text-sm font-medium text-blue-600 underline underline-offset-2 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
            >
              前往设置检查 API Key →
            </button>
          )}
        </div>
      )}

      {/* 扩展：打开文献时发现翻译仍在跑 → 提示接管 */}

      <ConfirmDialog
        open={attachPrompt !== null}
        title="检测到未完成的翻译"
        description={
          attachPrompt
            ? `「${attachPrompt.title}」仍在后台翻译中（当前 ${attachPrompt.progress}%）。继续后将恢复进度与实时译文；选择「直接打开」可先查看已翻译部分，不打断后台任务。`
            : ""
        }
        confirmText="继续翻译"
        cancelText="直接打开"
        onConfirm={() => {
          if (!attachPrompt) return;
          // 提取未完成（排版未定型）时只接管不进入：主页进度卡的自动跳转
          // 会在 ≥EXTRACT_DONE 时兜底入场
          const ready = attachPrompt.progress >= EXTRACT_DONE;
          const r = attach(attachPrompt.jobId, attachPrompt.filePath);
          setAttachPrompt(null);
          if (r.ok && ready) {
            navigatedRef.current = true;
            navigate(mode === "inline" ? "/reader/inline" : "/reader/bilingual");
          }
        }}
        onCancel={() => {
          const p = attachPrompt;
          setAttachPrompt(null);
          if (p) void handleOpenDoc(p.doc, { skipAttachCheck: true });
        }}
      />

      {/* 提取缓存失效（应用升级熔断/缓存清空）→ 确认后重新提取 */}

      <ConfirmDialog
        open={reextract !== null}
        title={`「${reextract?.doc.title ?? ""}」的提取缓存已失效`}
        description="应用升级后需要重新提取原文排版。将复用已有译文，只有变化的段落（如标题）会重新翻译，几乎不产生新费用。需要源文件仍在原位置。"
        confirmText="重新提取并打开"
        onConfirm={() => {
          const doc = reextract?.doc;
          setReextract(null);
          if (!doc?.file_path) return;
          const { configLoaded, isConfigured } = useConfigStore.getState();
          if (configLoaded && !isConfigured) {
            setError("请先在设置中配置 API Key，再添加文章");
            return;
          }
          if (currentTranslationKey()) {
            setError("已有翻译任务进行中，请等待完成后再添加新任务");
            return;
          }
          navigatedRef.current = false;
          pagesWereEmptyRef.current = true;
          void startTranslation(
            doc.file_path,
            `${doc.title}.pdf`,
          ).then((r) => {
            if (!r.ok) setError(r.reason);
          });
        }}
        onCancel={() => setReextract(null)}
      />
    </div>
  );
}
