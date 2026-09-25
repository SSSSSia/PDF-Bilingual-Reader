import { memo, useMemo, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { usePdfStore } from "../stores/pdfStore";
import { useUiStore } from "../stores/uiStore";
import { useSessionsStore } from "../stores/sessionsStore";
import { useZoomWheel } from "../hooks/useZoomWheel";
import type { TextBlock } from "../types";
import MarkdownText from "./common/MarkdownText";
import TranslatableImage from "./common/TranslatableImage";
import BlockTranslateButton from "./common/BlockTranslateButton";
import FormulaButton from "./common/FormulaButton";
import ReaderToolbar from "./ReaderToolbar";
import ReaderTabs from "./ReaderTabs";
import OriginalReader from "./OriginalReader";
import DualPdfPage from "./DualPdfPage";
import { useBabelDocStore } from "../stores/babeldocStore";

/** 纯图片块（markdown 图片引用），不与译文配对，整块原样展示 */
const isPureImage = (t: string) => /^\s*!\[[^\]]*\]\([^)]+\)\s*$/.test(t);

/** 单块：memo 化——applyBlockPatches 批量补丁下未触及块的
 * 对象引用保持稳定，浅比较直接跳过重渲染（与 BilingualPage 同款机制）。 */
const InlineBlock = memo(function InlineBlock({ block }: { block: TextBlock }) {
  return (
    <div
      className="mb-5"
      style={{ contentVisibility: "auto", containIntrinsicSize: "auto 120px" }}
    >
      {isPureImage(block.original) ? (
        /* 图表块：只出现一次，默认原图；表格图带「译」按钮可按需
           * 生成译制图（原排版+表内文字译文），下方图注走正文对照 */
        <figure className="my-6 flex flex-col items-center">
          <TranslatableImage md={block.translated || block.original} />
        </figure>
      ) : (
        <>
          <div className="text-slate-900 dark:text-slate-100 paper-font text-justify group relative">
            {/* 悬停浮现的单块按钮：公式块只出「式」
                ，其余「译/重译」 */}
            <span className="absolute right-0 top-0 z-10 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
              {block.formula_hint ? (
                <FormulaButton block={block} />
              ) : (
                <BlockTranslateButton block={block} />
              )}
            </span>
            <MarkdownText text={block.original} />
          </div>
          {block.translated && (
            <div className="mt-1.5 border-b border-dashed border-slate-300 pb-2 text-blue-900 dark:border-slate-600 dark:text-blue-100 paper-font text-justify">
              <MarkdownText text={block.translated} />
            </div>
          )}
        </>
      )}
    </div>
  );
});

/**
 * 紧跟模式：整篇连续文档流——
 * 原文段落在上，译文紧贴其下，段落间虚线分隔，图片/表格按原文档顺序穿插。
 * content-visibility:auto 保证长文档滚动性能。
 * 根容器挂 Ctrl+滚轮全局缩放（hook），内容区经 --reader-zoom
 * 缩放 prose 根字号（工具栏/页签为 chrome 不缩放；原版分支由 pdfjs scale 走）。
 */
export default function InlinePage() {
  const { pages, isLoading, progress, error, file, sessionKey } = usePdfStore();
  const readerMode = useUiStore((s) => s.readerMode);
  // 用户未手动设置过缩放（null）时，重排版缺省 100%（排版基准）
  const zoom = useUiStore((s) => s.zoom) ?? 1;
  const zoomRef = useZoomWheel<HTMLDivElement>();
  const blocks = useMemo(() => pages.flatMap((p) => p.blocks), [pages]);
  // F5 后会话清空，但 BabelDOC 任务已被静默重接管：直接呈现
  // 排版对照视图，否则卡在无会话门禁页
  const babeldocActive = useBabelDocStore(
    (s) => s.phase === "running" || s.phase === "done",
  );
  // 上传即对照文档：无重排版内容，恒呈现对照视图（含任务丢失后的
  // 确认卡重生成兜底），并豁免无会话门禁
  const isBabeldocDoc = useSessionsStore((s) =>
    sessionKey ? s.getByKey(sessionKey)?.reader === "babeldoc" : false,
  );

  if (blocks.length === 0 && !babeldocActive && !isBabeldocDoc) {
    return (
      <div className="py-20 text-center">
        <div
          className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-slate-100 text-2xl dark:bg-slate-800"
          aria-hidden="true"
        >
          📄
        </div>
        <p className="mb-4 text-slate-600 dark:text-slate-300">
          请先上传 PDF 并完成识别
        </p>
        <Link to="/" className="btn-primary">
          返回首页
        </Link>
      </div>
    );
  }

  /* 阅读页=纵向 flex 壳层（工具栏/页签/进度 shrink-0，内容通顶滚动） */
  return (
    <div ref={zoomRef} className="flex h-full min-h-0 flex-col">
      <ReaderToolbar />
      <ReaderTabs />

      {error && (
        <div
          role="alert"
          className="mx-4 mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800/60 dark:bg-red-900/20 dark:text-red-300"
        >
          {error}
        </div>
      )}

      {/* 原版PDF 组；无会话但 BabelDOC 重接管运行中
          → 直接呈现对照视图（F5 恢复，同 BilingualPage）；
          babeldoc 上传文档恒对照（无重排版内容） */}
      {readerMode === "original_bilingual" ||
      isBabeldocDoc ||
      (blocks.length === 0 && babeldocActive) ? (
        <DualPdfPage />
      ) : readerMode === "original_click" ? (
        <OriginalReader />
      ) : (
      /* 整篇连续文档流：所有页的 block 按文档顺序排布 */
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div
          className="mx-auto max-w-4xl px-4 md:px-6 pb-16"
          style={{ "--reader-zoom": zoom } as CSSProperties}
        >
          {blocks.map((b) => (
            <InlineBlock key={`${b.page}-${b.block_id}`} block={b} />
          ))}
        </div>
      </div>
      )}

      {/* 翻译进行中：底部状态条 */}

      {isLoading && (
        <div
          role="status"
          aria-label="翻译进度"
          className="flex h-7 shrink-0 items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 dark:border-slate-700 dark:bg-slate-900"
        >
          <span className="truncate text-xs text-slate-500 dark:text-slate-400">
            正在翻译「{(file?.name ?? "").replace(/\.pdf$/i, "")}」，已完成的段落实时显示…
          </span>
          <div className="flex shrink-0 items-center gap-2">
            <div className="h-1 w-28 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
              <div
                className="h-full rounded-full bg-blue-600 transition-all duration-300"
                style={{ width: `${Math.max(2, Math.round(progress))}%` }}
              />
            </div>
            <span className="w-9 text-right text-xs tabular-nums text-slate-500 dark:text-slate-400">
              {Math.round(progress)}%
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
