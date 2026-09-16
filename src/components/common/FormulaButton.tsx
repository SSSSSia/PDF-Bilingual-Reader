import { useState } from "react";
import { usePdfStore } from "../../stores/pdfStore";
import { useConfigStore } from "../../stores/configStore";
import { recognizeBlockFormula, translateBlock } from "../../lib/bridge";
import type { TextBlock } from "../../types";

/**
 * 公式块按需识别按钮：
 * 公式密集块（formula_hint）悬停显示「式」，点击把块区域（多段块逐段）
 * 裁剪送视觉模型（PaddleOCR-VL）转 LaTeX/markdown。
 *
 * 两类结果：
 * - 纯公式块（is_formula_block）：识别结果（LaTeX）直接替换译文位，KaTeX 渲染；
 * - 数学密集混合块（math_mixed）：识别结果是「英文正文 + $..$ 行内公式」的
 * 干净 markdown——替换原文（原文栏同时变干净）后自动调单块翻译
 * （后端 protect_formulas 会保护 $..$ 段），译文 = 中文正文 + 渲染公式。
 *
 * 后端按 (pdf_hash,page,bbox,model) 缓存识别结果，重复点按/重开文档幂等零成本。
 */
export default function FormulaButton({ block }: { block: TextBlock }) {
  const [state, setState] = useState<"idle" | "busy" | "translating" | "err">(
    "idle",
  );
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const filePath = usePdfStore((s) => s.filePath);
  const isLoading = usePdfStore((s) => s.isLoading);
  const updateBlockTranslated = usePdfStore((s) => s.updateBlockTranslated);
  const updateBlockOriginal = usePdfStore((s) => s.updateBlockOriginal);
  const sourceLang = useConfigStore(
    (s) => s.config?.translate.source_language || "zh",
  );
  const targetLang = useConfigStore(
    (s) => s.config?.translate.target_language || "en",
  );

  /** 识别段坐标：多段块用 bboxes 逐段识别再拼接（混合块常跨段） */
  const segs =
    block.bboxes && block.bboxes.length > 0
      ? block.bboxes
      : block.bbox
        ? [{ page: block.page, bbox: block.bbox }]
        : [];

  const onClick = async () => {
    if (!filePath || segs.length === 0) return;
    setState("busy");
    setErrMsg(null);
    try {
      const parts: string[] = [];
      for (const seg of segs) {
        const { latex } = await recognizeBlockFormula(
          filePath,
          seg.page,
          seg.bbox,
        );
        if (latex.trim()) parts.push(latex.trim());
      }
      const markdown = parts.join("\n\n");
      if (!markdown) {
        setState("err");
        return;
      }
      // 混合块判定：识别结果含足量可读单词 = 散文+公式混合（与后端
      // is_formula_block 的纯公式判定同阈值），需继续走翻译
      const words = markdown.match(/[A-Za-z]{2,}/g)?.length ?? 0;
      if (words < 10) {
        updateBlockTranslated(block.page, block.block_id, markdown);
        setState("idle");
        return;
      }
      setState("translating");
      updateBlockOriginal(block.page, block.block_id, markdown);
      const translated = await translateBlock(markdown, sourceLang, targetLang);
      if (translated.trim()) {
        updateBlockTranslated(block.page, block.block_id, translated);
        setState("idle");
      } else {
        setState("err"); // 空译文：原文已替换，可重试翻译
      }
    } catch (e) {
      console.error("公式识别失败:", e);
      setErrMsg(e instanceof Error ? e.message : String(e));
      setState("err");
    }
  };

  const label =
    state === "busy"
      ? "识别中…"
      : state === "translating"
        ? "翻译中…"
        : state === "err"
          ? "失败·重试"
          : "式";
  const disabled = isLoading || state === "busy" || state === "translating" || !filePath || segs.length === 0;
  const title = segs.length === 0
    ? "该块未匹配到坐标，无法裁剪识别"
    : state === "err" && errMsg
      ? `上次失败：${errMsg}（点按重试；若提示 404 请重启后端进程）`
      : "识别此块公式为 LaTeX（含正文时自动重译）";

  return (
    <span className="inline-flex max-w-full items-center gap-1.5 align-middle">
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        title={title}
        className={`shrink-0 rounded border px-1.5 py-0.5 text-xs shadow-sm transition-colors ${
          state === "err"
            ? "border-red-300 bg-red-50 text-red-600 hover:bg-red-100 dark:border-red-800/60 dark:bg-red-900/30 dark:text-red-300"
            : "border-slate-200 bg-white text-slate-500 hover:border-violet-400 hover:text-violet-600 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-violet-500 dark:hover:text-violet-300"
        } disabled:cursor-not-allowed disabled:opacity-60`}
      >
        {label}
      </button>
      {/* 失败原因内联展示 */}

      {state === "err" && errMsg && (
        <span
          role="alert"
          className="min-w-0 flex-1 truncate text-xs text-red-600 dark:text-red-400"
          title={errMsg}
        >
          {errMsg}
        </span>
      )}
    </span>
  );
}
