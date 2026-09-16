import { useEffect, useMemo, useRef, useState } from "react";
import { usePdfStore } from "../stores/pdfStore";
import { useUiStore, effectiveZoom } from "../stores/uiStore";
import { useZoomWheel } from "../hooks/useZoomWheel";
import { usePdfDocument } from "../hooks/usePdfDocument";
import { useLazyPage, type PageLayout } from "../hooks/useLazyPage";
import MarkdownText from "./common/MarkdownText";
import BlockTranslateButton from "./common/BlockTranslateButton";
import FormulaButton from "./common/FormulaButton";
import type { TextBlock } from "../types";

type BBox = [number, number, number, number];
type BBoxSeg = { page: number; bbox: BBox };
type Overlay = { block: TextBlock; bb: BBox };

/**
 * 原版对照模式：pdfjs 原样渲染 PDF 页面（公式/图表/
 * 双栏版式零损失），已译块按多段 bbox 高亮（断栏/跨页续段各自所在页都
 * 有高亮），点击浮层查看译文。
 *
 * 设计要点：
 * - 连续滚动逐页懒渲染：IntersectionObserver(rootMargin 600px) 触发，
 * 渲染任务可取消（task.cancel）防止快速滚动时的渲染竞态
 * - fit-width × devicePixelRatio × zoom：canvas 物理像素按 dpr 放大保证
 * 高清，CSS 尺寸按逻辑 scale 定位——overlay 坐标 = bbox(pt) × scale；
 * - bbox 为 PyMuPDF top-left 原点坐标，与 pdfjs 旋转 0° viewport 直接
 * 乘法兼容；旋转页不渲染 overlay（坐标会错位，明确提示）；
 * - bbox 缺失（公式碎块/图内文字/扫描页）只是不高亮，页面渲染零依赖匹配。
 */
export default function OriginalReader() {
  const filePath = usePdfStore((s) => s.filePath);
  const pages = usePdfStore((s) => s.pages);
  // /zoom 收敛到 uiStore 全局缩放（三形态共用 + localStorage 持久化；
  // Ctrl+滚轮由本组件根容器 useZoomWheel 驱动，± 控件统一在工具栏，
  // 此处不再放重复控件——实测反馈两处缩放计数重复）。
  // 用户未手动设置过缩放（zoom=null）时，原版缺省 70%
  // （固定版式 100% 偏大，70% 更接近 PDF 阅读器惯例）；设置过则全形态用用户值。
  const zoom = effectiveZoom(
    useUiStore((s) => s.zoom),
    useUiStore((s) => s.readerMode)
  );
  const zoomRef = useZoomWheel<HTMLDivElement>();
  const { pdf, error } = usePdfDocument(filePath);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [wrapW, setWrapW] = useState(0);

  // 视口宽度跟踪（fit-width 基准）
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setWrapW(el.clientWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 每页 overlay 分段索引：一个逻辑块可有多段坐标（断栏/跨页合并），
  // 跨页合并块的分段按 seg.page 挂到各自所在页（含续文页）
  const overlaysByPage = useMemo(() => {
    const m = new Map<number, Overlay[]>();
    pages.forEach((p) =>
      p.blocks.forEach((b) => {
        const segs: BBoxSeg[] =
          b.bboxes && b.bboxes.length
            ? b.bboxes
            : b.bbox
              ? [{ page: p.page, bbox: b.bbox as BBox }]
              : [];
        segs.forEach((seg) => {
          if (!seg?.bbox) return;
          const arr = m.get(seg.page) ?? [];
          arr.push({ block: b, bb: seg.bbox });
          m.set(seg.page, arr);
        });
      })
    );
    return m;
  }, [pages]);

  return (
    /* 外层 wrapper 挂 Ctrl+滚轮缩放 hook（事件冒泡至此监听，
       * preventDefault 仍可拦 WebView2 页面缩放）；内层滚动容器
       * 的 wrapRef 专职 wrapW 测量（fit-width 基准，一个节点一个 ref） */
    <div ref={zoomRef} className="flex h-full min-h-0 flex-col">
      <div
        ref={wrapRef}
        className="relative min-h-0 flex-1 overflow-auto bg-slate-200 dark:bg-slate-950"
      >
      {/* 缩放走工具栏统一控件：此处不再放重复的 ± 控件。
          「点高亮块看译文」操作提示保留为纯文字小条。 */}
      <div className="sticky left-2 top-2 z-20 w-max rounded-lg border border-slate-300 bg-white/95 px-2 py-1 text-xs text-slate-400 shadow-sm backdrop-blur-sm dark:border-slate-700 dark:bg-slate-900/95 dark:text-slate-500">
        点高亮块看译文 · 缩放用上方控件或 Ctrl+滚轮
      </div>

      {error && (
        <div
          role="alert"
          className="mx-auto mt-6 max-w-2xl rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800/60 dark:bg-red-900/20 dark:text-red-300"
        >
          原版渲染失败：{error}
        </div>
      )}
      {!pdf && !error && (
        <div className="py-20 text-center text-sm text-slate-500 dark:text-slate-400">
          正在加载 PDF…
        </div>
      )}

      {pdf && (
        /* 页面列容器显式宽度 = fit-width × zoom（每页 CSS 宽度公式相同，
           * 可直接算出，不必等懒渲染）——滚动区域宽度由显式宽度决定，
           * 放大超宽时横向滚动必然可用（实测反馈：溢出传播
           * 在 w-full/max-w 容器链上不可靠，右侧被裁且无法滚动）。
           * zoom=1 时宽 = wrapW-16 < 容器宽，居中且完整显示。 */
        <div
          className="mx-auto px-4 pb-16 pt-3"
          style={{ width: wrapW > 0 ? (wrapW - 48) * zoom + 32 : undefined }}
        >
          {pages.map((p) => (
            <OriginalPage
              key={p.page}
              pdf={pdf}
              pageNo={p.page + 1}
              overlays={overlaysByPage.get(p.page) ?? []}
              wrapW={wrapW}
              zoom={zoom}
            />
          ))}
        </div>
      )}
      </div>
    </div>
  );
}

/** 单页：懒渲染 canvas + bbox overlay（多段） + 译文浮层 */
function OriginalPage({
  pdf,
  pageNo,
  overlays,
  wrapW,
  zoom,
}: {
  pdf: any;
  pageNo: number;
  overlays: Overlay[];
  wrapW: number;
  zoom: number;
}) {
  // 懒渲染逻辑抽至 useLazyPage
  const { holderRef, canvasRef, layout, rotated } = useLazyPage({
    pdf,
    pageNo,
    renderW: wrapW,
    zoom,
  });
  const [selected, setSelected] = useState<{ block: TextBlock; bb: BBox } | null>(
    null
  );

  return (
    <div
      ref={holderRef}
      className="relative mb-6"
      style={{ minHeight: layout ? layout.h + 28 : 920 }}
      onMouseDown={() => setSelected(null)}
    >
      <div
        className="relative mx-auto bg-white shadow-md dark:shadow-black/50"
        style={{ width: layout ? layout.w : undefined }}
      >
        <canvas ref={canvasRef} className="block" />

        {layout && !rotated && (
          <div className="absolute inset-0">
            {overlays.map((o, i) => {
              const bb = o.bb;
              const s = layout.scale;
              const has = !!o.block.translated;
              return (
                <div
                  key={`${o.block.block_id}-${i}`}
                  role="button"
                  tabIndex={0}
                  onMouseDown={(e) => e.stopPropagation()}
                  onClick={() => setSelected({ block: o.block, bb })}
                  onKeyDown={(e) =>
                    e.key === "Enter" && setSelected({ block: o.block, bb })
                  }
                  className={`absolute rounded-[3px] transition-colors duration-100 ${
                    has
                      ? "cursor-pointer bg-blue-500/10 hover:bg-blue-500/30"
                      : "cursor-pointer bg-slate-400/5 hover:bg-slate-400/30"
                  }`}
                  style={{
                    left: bb[0] * s,
                    top: bb[1] * s,
                    width: Math.max(2, (bb[2] - bb[0]) * s),
                    height: Math.max(2, (bb[3] - bb[1]) * s),
                  }}
                  title={has ? "点击查看译文" : "未翻译"}
                />
              );
            })}

            {selected && (
              <BlockCard
                block={selected.block}
                bb={selected.bb}
                layout={layout}
                onClose={() => setSelected(null)}
              />
            )}
          </div>
        )}

        {rotated && (
          <div className="absolute left-2 top-2 rounded bg-amber-100 px-2 py-1 text-xs text-amber-800">
            旋转页暂不支持坐标对照
          </div>
        )}
      </div>
      <div className="mt-1.5 text-center text-xs text-slate-400 dark:text-slate-500">
        第 {pageNo} 页
      </div>
    </div>
  );
}

/** 译文浮层：只显示译文，优先贴块下方，空间不足翻到上方 */
function BlockCard({
  block,
  bb,
  layout,
  onClose,
}: {
  block: TextBlock;
  bb: BBox;
  layout: PageLayout;
  onClose: () => void;
}) {
  const s = layout.scale;
  const cardW = Math.min(380, Math.max(260, layout.w * 0.45));
  const left = Math.min(Math.max(4, bb[0] * s), Math.max(4, layout.w - cardW - 4));
  const below = bb[3] * s + 6;
  const top = below + 300 > layout.h ? Math.max(4, bb[1] * s - 306) : below;

  return (
    <div
      className="absolute z-30 rounded-lg border border-slate-200 bg-white/95 p-3 shadow-lg backdrop-blur-sm dark:border-slate-700 dark:bg-slate-900/95"
      style={{ left, top, width: cardW }}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-blue-600 dark:text-blue-400">
          译文
        </span>
        <div className="flex items-center gap-1.5">
          {/* 公式块只出「式」，其余「译/重译」 */}

          {block.formula_hint ? (
            <FormulaButton block={block} />
          ) : (
            <BlockTranslateButton block={block} />
          )}
          <button
            onClick={onClose}
            aria-label="关闭"
            className="h-6 w-6 rounded text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-300"
          >
            ×
          </button>
        </div>
      </div>
      <div className="max-h-72 overflow-y-auto text-sm leading-relaxed text-slate-900 dark:text-slate-100">
        {block.translated ? (
          <MarkdownText text={block.translated} />
        ) : (
          <span className="italic text-slate-400 dark:text-slate-500">待翻译…</span>
        )}
      </div>
    </div>
  );
}
