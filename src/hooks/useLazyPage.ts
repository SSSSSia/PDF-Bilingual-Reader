import { useEffect, useRef, useState } from "react";

export type PageLayout = { scale: number; w: number; h: number };

/**
 * 单页懒渲染（自 OriginalReader 的 OriginalPage 抽出，
 * 原版点击翻译 / 原版左右对照两种形态共用）：
 * - 接近视口才启动渲染（IntersectionObserver rootMargin 600px），
 * 长文档连续滚动不卡；渲染任务可取消（task.cancel）防止
 * 快速滚动/改缩放时的渲染竞态；
 * - fit-width × devicePixelRatio × zoom：canvas 物理像素按 dpr 放大
 * 保证高清，CSS 尺寸按逻辑 scale 定位——overlay 坐标 = bbox(pt) × scale；
 * - renderW 为滚动容器测量宽（fit-width 基准），48 = 页面列容器
 * 横向留白（px-4 的 32 + 视觉边距 16），与 OriginalReader 宽度公式同源。
 *
 * 返回 layout/rotated 供调用方上报（如左右对照右栏镜像几何依赖）。
 */
export function useLazyPage(opts: {
  pdf: any;
  /** 1-based 页码（pdfjs getPage 从 1 开始） */
  pageNo: number;
  renderW: number;
  zoom: number;
}) {
  const { pdf, pageNo, renderW, zoom } = opts;
  const holderRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [near, setNear] = useState(false);
  const [layout, setLayout] = useState<PageLayout | null>(null);
  const [rotated, setRotated] = useState(false);

  // 接近视口才启动渲染（懒加载，长文档滚动不卡）
  useEffect(() => {
    const el = holderRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (es) => {
        if (es.some((e) => e.isIntersecting)) {
          setNear(true);
          io.disconnect();
        }
      },
      { rootMargin: "600px 0px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  // 渲染当前页（任务可取消：快速滚动/改缩放时不做无用功）
  useEffect(() => {
    if (!near || !pdf || !canvasRef.current || renderW <= 0) return;
    let cancelled = false;
    let task: any = null;
    (async () => {
      try {
        const page = await pdf.getPage(pageNo);
        if (cancelled) return;
        if (((page.rotate as number) ?? 0) % 360 !== 0) setRotated(true);
        const base = page.getViewport({ scale: 1 });
        const scale = ((renderW - 48) / base.width) * zoom;
        const dpr = window.devicePixelRatio || 1;
        const viewport = page.getViewport({ scale: scale * dpr });
        const canvas = canvasRef.current;
        if (!canvas) return;
        canvas.width = Math.max(1, Math.floor(viewport.width));
        canvas.height = Math.max(1, Math.floor(viewport.height));
        canvas.style.width = `${Math.floor(scale * base.width)}px`;
        canvas.style.height = `${Math.floor(scale * base.height)}px`;
        task = page.render({ canvas, viewport });
        await task.promise;
        if (!cancelled)
          setLayout({ scale, w: scale * base.width, h: scale * base.height });
      } catch (e: any) {
        if (!cancelled && e?.name !== "RenderingCancelledException")
          console.error(`原版渲染 p${pageNo} 失败:`, e);
      }
    })();
    return () => {
      cancelled = true;
      task?.cancel?.();
    };
  }, [near, pdf, pageNo, renderW, zoom]);

  return { holderRef, canvasRef, layout, rotated };
}
