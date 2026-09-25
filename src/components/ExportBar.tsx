import { useState } from "react";
import { usePdfStore } from "../stores/pdfStore";
import { useConfigStore } from "../stores/configStore";
import { useUiStore } from "../stores/uiStore";
import { useBabelDocStore } from "../stores/babeldocStore";
import { exportBilingual, probeDualPdf, saveDualPdf } from "../utils/export";
import ConfirmDialog from "./common/ConfirmDialog";

/**
 * 导出：
 * 主色「导出」按钮点开二次选格式——
 * - Markdown 译文：重排版双语内容（原文/译文成对，可二次编辑）；
 * - PDF·原版对照：BabelDOC 双语 PDF——已有成品直接另存；未生成则引导去
 * 「原版双语对照」模式生成（独立翻译整篇需数分钟），完成后回来导出。
 * babeldoc 上传文档无重排版内容：仅保留 PDF 项（= 另存对照 PDF 副本）。
 */
export default function ExportBar({
  babeldocDoc = false,
}: {
  babeldocDoc?: boolean;
}) {
  const { pages, file, filePath, setError } = usePdfStore();
  const { config } = useConfigStore();
  const bdoc = useBabelDocStore();
  const setReaderMode = useUiStore((s) => s.setReaderMode);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // 原版对照未生成的引导（确认后切换到对照模式接力生成流程）
  const [needGen, setNeedGen] = useState(false);

  // 对照模式下 F5 后会话可能为空：文件路径回落到对照任务自带路径
  const sourcePath = filePath || bdoc.filePath || "";

  const runPdf = async () => {
    if (!sourcePath) {
      setError("源文件路径未知，无法导出原版对照 PDF");
      return;
    }
    setBusy(true);
    try {
      const probe = await probeDualPdf(sourcePath);
      if (probe.cached && probe.dual_path) {
        await saveDualPdf(probe.dual_path, file?.name ?? sourcePath);
      } else {
        setNeedGen(true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runMarkdown = async () => {
    if (!pages.length) return;
    setBusy(true);
    try {
      await exportBilingual(pages, file?.name, {
        source: config?.translate.source_language,
        target: config?.translate.target_language,
      });
    } finally {
      setBusy(false);
    }
  };

  const choose = (fmt: "markdown" | "pdf") => {
    setOpen(false);
    if (fmt === "pdf") void runPdf();
    else void runMarkdown();
  };

  const mdDisabled = !pages.length;

  return (
    <div className="relative flex shrink-0 items-center">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        title="选择导出格式"
        aria-haspopup="menu"
        aria-expanded={open}
        className="btn-primary h-7 gap-1 px-2.5 text-xs"
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-3.5 w-3.5"
          aria-hidden="true"
        >
          <path d="M12 3v12" />
          <path d="m7 10 5 5 5-5" />
          <path d="M5 21h14" />
        </svg>
        {busy ? "导出中…" : "导出"}
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`h-3 w-3 transition-transform duration-150 ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>

      {open && (
        <>
          {/* 点击菜单外区域关闭 */}

          <div
            className="fixed inset-0 z-20"
            onClick={() => {
              setOpen(false);
            }}
          />
          <div
            role="menu"
            aria-label="导出格式"
            className="absolute right-0 top-full z-30 mt-1.5 w-64 rounded-lg border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-800"
          >
            {!babeldocDoc && (
            <button
              role="menuitem"
              onClick={() => choose("markdown")}
              disabled={mdDisabled}
              title={mdDisabled ? "尚未打开文献（无重排版内容）" : undefined}
              className="flex w-full items-start gap-2.5 px-3 py-2 text-left transition-colors duration-150 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-45 dark:hover:bg-slate-700"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="mt-0.5 h-4 w-4 shrink-0 text-slate-400 dark:text-slate-500"
                aria-hidden="true"
              >
                <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z" />
                <path d="M14.5 3v4h4" />
                <path d="M9 13h6M9 17h6" />
              </svg>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-slate-800 dark:text-slate-200">
                  Markdown 译文
                </span>
                <span className="block text-xs leading-snug text-slate-400 dark:text-slate-500">
                  {mdDisabled ? "先打开文献后可用" : "重排版双语内容，可二次编辑"}
                </span>
              </span>
            </button>
            )}
            <button
              role="menuitem"
              onClick={() => choose("pdf")}
              className="flex w-full items-start gap-2.5 px-3 py-2 text-left transition-colors duration-150 hover:bg-slate-100 dark:hover:bg-slate-700"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="mt-0.5 h-4 w-4 shrink-0 text-red-400 dark:text-red-500"
                aria-hidden="true"
              >
                <path d="M14.5 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7L14.5 3z" />
                <path d="M14.5 3v4h4" />
                <path d="M9.5 13c.6-.8 2.4-.9 2.4.3 0 1.1-2.4 1-2.4 2.4 0 1.2 1.8 1.1 2.4.3" />
              </svg>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-slate-800 dark:text-slate-200">
                  PDF · 原版对照
                </span>
                <span className="block text-xs leading-snug text-slate-400 dark:text-slate-500">
                  BabelDOC 原版排版双语 PDF
                </span>
              </span>
            </button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={needGen}
        title="原版对照 PDF 尚未生成"
        description="将切换到「原版双语对照」模式启动生成（BabelDOC 独立翻译整篇，首跑约数分钟、消耗模型额度；生成过一次后秒开）。生成完成后，再从工具栏导出按钮选择「PDF·原版对照」即可。"
        confirmText="去生成"
        cancelText="取消"
        onConfirm={() => {
          setNeedGen(false);
          setReaderMode("original_bilingual");
        }}
        onCancel={() => setNeedGen(false)}
      />
    </div>
  );
}
