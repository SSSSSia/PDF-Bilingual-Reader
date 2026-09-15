import { useState } from "react";
import { usePdfStore } from "../stores/pdfStore";
import { useConfigStore } from "../stores/configStore";
import { useUiStore } from "../stores/uiStore";
import { useBabelDocStore } from "../stores/babeldocStore";
import { exportBilingual, probeDualPdf, saveDualPdf, ExportFormat } from "../utils/export";
import ConfirmDialog from "./common/ConfirmDialog";

/**
 * 导出（2026-09-15 用户决策收敛为两种文件）：
 * - Markdown 译文：重排版双语内容（原文/译文成对）；
 * - PDF·原版对照：BabelDOC 双语 PDF——已有成品直接另存；未生成则引导去
 *   「原版双语对照」模式生成（BabelDOC 独立翻译整篇需数分钟，进度在该
 *   模式内呈现），完成后从该模式内的保存按钮或此处导出。
 */
export default function ExportBar() {
  const { pages, file, filePath, setError } = usePdfStore();
  const { config } = useConfigStore();
  const bdoc = useBabelDocStore();
  const setReaderMode = useUiStore((s) => s.setReaderMode);
  const [format, setFormat] = useState<ExportFormat>("markdown");
  const [busy, setBusy] = useState(false);
  // 原版对照未生成的引导（确认后切换到对照模式接力生成流程）
  const [needGen, setNeedGen] = useState(false);

  // 对照模式下 F5 后会话可能为空：文件路径回落到对照任务自带路径
  const sourcePath = filePath || bdoc.filePath || "";

  const handlePdf = async () => {
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

  const handle = async () => {
    if (format === "pdf") {
      await handlePdf();
      return;
    }
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

  return (
    <div className="flex shrink-0 items-center gap-2 whitespace-nowrap">
      <select
        value={format}
        onChange={(e) => setFormat(e.target.value as ExportFormat)}
        aria-label="导出格式"
        className="input-field w-auto"
      >
        <option value="markdown">Markdown 译文</option>
        <option value="pdf">PDF·原版对照</option>
      </select>
      <button
        onClick={handle}
        disabled={busy || (format === "markdown" && !pages.length)}
        className="btn-secondary"
      >
        {busy ? "导出中…" : "导出"}
      </button>

      <ConfirmDialog
        open={needGen}
        title="原版对照 PDF 尚未生成"
        description="将切换到「原版双语对照」模式启动生成（BabelDOC 独立翻译整篇，首跑约数分钟、消耗模型额度；生成过一次后秒开）。生成完成后，再从这里选择「PDF·原版对照」导出即可。"
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
