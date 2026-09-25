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
import BabeldocRewriteGate from "./common/BabeldocRewriteGate";
import { useBabelDocStore } from "../stores/babeldocStore";

// 纯图片块（图表快照）：后端已令译文=原文，前端整行居中渲染一次
const isPureImage = (t: string) => /^\s*!\[[^\]]*\]\([^)]+\)\s*$/.test(t);

/** 单块行卡片：memo 化——applyBlockPatches 批量补丁下未触及
 * 块的对象引用保持稳定，浅比较直接跳过重渲染；这是流式翻译不再整列表
 * 重渲的关键。content-visibility:auto 让视口外内容不参与渲染。 */
const BilingualBlockRow = memo(function BilingualBlockRow({
  block,
}: {
  block: TextBlock;
}) {
  if (isPureImage(block.original)) {
    return (
      <div
        className="grid grid-cols-1 gap-y-2 border-b border-dashed border-slate-200 dark:border-slate-700 md:grid-cols-2 md:gap-x-8"
        style={{ contentVisibility: "auto", containIntrinsicSize: "auto 260px" }}
      >
        <div className="py-3 pr-2">
          {/* 原文栏不显示「译」按钮：只有译文栏可生成译制图 */}

          <TranslatableImage md={block.original} interactive={false} />
        </div>
        <div className="py-3 md:border-l md:border-slate-200 md:pl-2 dark:md:border-slate-700">
          {/* 图表默认右栏也显示原图；表格可点按生成译制图 */}

          <TranslatableImage md={block.translated || block.original} />
        </div>
      </div>
    );
  }
  return (
    <div
      className="grid grid-cols-1 border-b border-dashed border-slate-200 dark:border-slate-700 md:grid-cols-2 md:gap-x-8"
      style={{ contentVisibility: "auto", containIntrinsicSize: "auto 160px" }}
    >
      <div className="paper-font group relative py-3 pr-2 text-justify text-slate-900 dark:text-slate-100">
        {/* 悬停浮现的单块翻译/重翻按钮；
            公式块（含纯公式/混合）只出「式」——识别成功后纯公式
            直出 LaTeX，混合块替换原文并自动重译（管线翻译完成后
            也会自动跑一遍，按钮作手动重试入口） */}
        <span className="absolute right-1 top-2 z-10 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          {block.formula_hint ? (
            <FormulaButton block={block} />
          ) : (
            <BlockTranslateButton block={block} />
          )}
        </span>
        <MarkdownText text={block.original} />
      </div>
      <div className="paper-font py-3 text-justify text-blue-900 dark:text-blue-100 md:border-l md:border-slate-200 md:pl-2 dark:md:border-slate-700">
        {block.translated ? (
          <>
            {/* 单列模式下给译文加个小标签，区分原文 */}

            <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-blue-500 md:hidden dark:text-blue-400">
              译文
            </span>
            <MarkdownText text={block.translated} />
          </>
        ) : (
          <span className="italic text-slate-400 dark:text-slate-500">
            待翻译…
          </span>
        )}
      </div>
    </div>
  );
});

/**
 * 左右对照模式：整篇连续滚动。
 * 对齐采用网格行配对——每一行 = 一个 block 的「原文 | 译文」，
 * DOM 结构保证左右严格同行，滚动天然同步，
 * 不再需要旧的百分比滚动同步（useScrollSync 已退役）。
 * content-visibility:auto 让长文档只渲染视口附近内容，滚动性能不随页数劣化。
 * 根容器挂 Ctrl+滚轮全局缩放（hook），内容区经 --reader-zoom
 * 缩放 prose 根字号（工具栏/页签为 chrome 不缩放；原版分支由 pdfjs scale 走）。
 */
export default function BilingualPage() {
  const { pages, isLoading, progress, error, file, sessionKey } = usePdfStore();
  const readerMode = useUiStore((s) => s.readerMode);
  // 用户未手动设置过缩放（null）时，重排版缺省 100%（排版基准）
  const zoom = useUiStore((s) => s.zoom) ?? 1;
  const zoomRef = useZoomWheel<HTMLDivElement>();
  const blocks = useMemo(() => pages.flatMap((p) => p.blocks), [pages]);
  // F5 后会话清空，但 BabelDOC 任务已被静默重接管：此时
  // 不显示无会话门禁，直接呈现排版对照视图（进度/产物），否则用户卡在
  // 「请先上传 PDF」门禁页
  const babeldocActive = useBabelDocStore(
    (s) => s.phase === "running" || s.phase === "done",
  );
  // 上传即对照文档（reader=babeldoc）：无会话门禁豁免；切到重排版
  // 形态且尚未跑重排版翻译时展示引导卡（点按钮补跑）
  const isBabeldocDoc = useSessionsStore((s) =>
    sessionKey ? s.getByKey(sessionKey)?.reader === "babeldoc" : false,
  );
  const showRewriteGate = isBabeldocDoc && blocks.length === 0;

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

  return (
    /* 阅读页=纵向 flex 壳层（工具栏/页签/进度 shrink-0，内容通顶滚动） */
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

      {/* 原版PDF 组：点击翻译=pdfjs 原样渲染 + 块坐标译文浮层；
          左右对照=左渲染右译文锚定对照；翻译进度见底部状态条。
          无会话但 BabelDOC 重接管运行中 → 直接呈现对照视图（F5 恢复）；
          babeldoc 上传文档切重排版形态且未跑翻译 → 重排版引导卡 */}
      {readerMode === "original_bilingual" ||
      (blocks.length === 0 && babeldocActive) ? (
        <DualPdfPage />
      ) : showRewriteGate ? (
        <BabeldocRewriteGate />
      ) : readerMode === "original_click" ? (
        <OriginalReader />
      ) : (
      /* 整篇单列滚动：所有页的 block 按文档顺序连续排布 */
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div
          className="mx-auto max-w-6xl px-4 md:px-6"
          style={{ "--reader-zoom": zoom } as CSSProperties}
        >
          {/* 窄窗口降级为单列：原文在上、译文在下 */}

          <div className="sticky top-0 z-10 hidden grid-cols-2 border-b border-slate-200 bg-slate-50/95 backdrop-blur-sm dark:border-slate-700 dark:bg-slate-900/95 md:grid">
            <div className="py-2 text-xs font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-500">
              原文
            </div>
            <div className="py-2 text-xs font-semibold uppercase tracking-wide text-blue-500 dark:text-blue-400">
              译文
            </div>
          </div>

          {blocks.map((b) => (
            <BilingualBlockRow key={`${b.page}-${b.block_id}`} block={b} />
          ))}

          <div className="h-16" aria-hidden="true" />
        </div>
      </div>
      )}

      {/* 翻译进行中：底部状态条（实测反馈：顶部通栏进度条不美观，
          移到窗口底边零遮挡；与工具栏同视觉语言，翻译完成即消失） */}
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
