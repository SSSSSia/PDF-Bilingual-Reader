import { useState } from "react";
import { usePdfStore } from "../../stores/pdfStore";
import { useConfigStore } from "../../stores/configStore";
import { translateBlock } from "../../lib/bridge";
import type { TextBlock } from "../../types";

/**
 * 块级手动翻译按钮：
 * 悬停原文段落时显示——未翻译显示「译」，已翻译显示「重译」；
 * 点击调 /api/block/translate 单块翻译/重翻，结果就地替换显示，
 * 后端同时写回块级缓存（重开文档不丢）。
 * 全文翻译进行中禁用：后端流式回写会覆盖手动结果，等它跑完再点。
 */
export default function BlockTranslateButton({ block }: { block: TextBlock }) {
  const [state, setState] = useState<"idle" | "busy" | "err">("idle");
  const isLoading = usePdfStore((s) => s.isLoading);
  const updateBlockTranslated = usePdfStore((s) => s.updateBlockTranslated);
  const config = useConfigStore((s) => s.config);
  const sourceLang = config?.translate.source_language || "zh";
  const targetLang = config?.translate.target_language || "en";

  const onClick = async () => {
    setState("busy");
    try {
      const translated = await translateBlock(
        block.original,
        sourceLang,
        targetLang,
      );
      if (translated.trim()) {
        updateBlockTranslated(block.page, block.block_id, translated);
        setState("idle");
      } else {
        setState("err"); // 空译文：保持可重试
      }
    } catch (e) {
      console.error("单块翻译失败:", e);
      setState("err");
    }
  };

  const label =
    state === "busy"
      ? "翻译中…"
      : state === "err"
        ? "重试"
        : block.translated
          ? "重译"
          : "译";
  const disabled = isLoading || state === "busy";
  const title = isLoading
    ? "全文翻译进行中，请稍后再试"
    : state === "err"
      ? "翻译失败：请检查 API Key 配置或网络后重试"
      : block.translated
        ? "重新翻译此段"
        : "翻译此段";

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`rounded border px-1.5 py-0.5 text-xs shadow-sm transition-colors ${
        state === "err"
          ? "border-red-300 bg-red-50 text-red-600 hover:bg-red-100 dark:border-red-800/60 dark:bg-red-900/30 dark:text-red-300"
          : "border-slate-200 bg-white text-slate-500 hover:border-blue-300 hover:text-blue-600 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-blue-500 dark:hover:text-blue-300"
      } disabled:cursor-not-allowed disabled:opacity-60`}
    >
      {label}
    </button>
  );
}
