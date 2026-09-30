import { useEffect, useRef, useState } from "react";

export type PageLayout = { scale: number; w: number; h: number; zoom: number };

/**
 * 单页懒渲染（自 OriginalReader 的 OriginalPage 抽出，
 * 原版点击翻译 / 原版左右对照两种形态共用）：
 * - 接近视口才启动渲染（IntersectionObserver rootMargin 600px），
 * 长文档连续滚动不卡；渲染任务可取消（task.cancel）防止
 * 快速滚动/改缩放时的渲染竞态；
 * - 可见性双向跟踪：滚远即置 near=false（effect 跳过，保留旧位图），
 * 重新接近才以最新 zoom 重绘——缩放手势只影响视口附近的页，
 * 已浏览过的远页不参与（单向闩锁时代每次缩放全量重绘已浏览页，长文卡顿）；
 * - fit-width × devicePixelRatio × zoom：canvas 物理像素按 dpr 放大
 * 保证高清；CSS 尺寸不在此设置——由调用方按 layout × 实时/定稿比例
 * 计算（缩放手势中位图实时拉伸，定稿后重绘归位，见 OriginalReader）；
 * - zoom 入参 = 定稿渲染值（uiStore.renderZoom 相），手势期间不变；
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

  // 接近视口才启动渲染（懒加载，长文档滚动不卡）；双向：滚远停绘、
  // 回近按当前 zoom 重绘
  useEffect(() => {
    const el = holderRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (es) => setNear(es.some((e) => e.isIntersecting)),
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
        task = page.render({ canvas, viewport });
        await task.promise;
        if (!cancelled)
          setLayout({
            scale,
            w: scale * base.width,
            h: scale * base.height,
            zoom,
          });
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
