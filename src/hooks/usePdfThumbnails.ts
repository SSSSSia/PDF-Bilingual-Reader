import { useEffect, useRef, useState } from "react";
import * as pdfjsLib from "pdfjs-dist";
import { convertFileSrc } from "../lib/bridge";
// Vite 会把 worker 作为本地资源打包，避免依赖 CDN（满足「仅本地使用」约束）
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { usePdfStore } from "../stores/pdfStore";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

// 缩略图渲染分辨率（viewport 宽度），越小越省内存
const THUMB_SCALE = 0.32;
// 并发渲染上限，避免大文档瞬间创建数百个 canvas 卡死主线程
const RENDER_CONCURRENCY = 4;

type ThumbState = {
  url: string | null; // dataURL，未渲染完时为 null
  width: number;
  height: number;
};

/**
 * 基于 pdfjs-dist 渲染 PDF 每页缩略图（用户已批准引入该依赖）。
 *
 * 设计要点：
 * - 复用单个 PDFDocumentProxy，避免重复解析。
 * - 逐页并发渲染（上限 RENDER_CONCURRENCY），每渲染完一页即通过 setState 增量更新，
 * 大文档也能渐进显示，不会长时间白屏。
 * - 仅在 filePath 存在时工作；浏览器环境（非 Tauri）convertFileSrc 不可用，静默降级。
 */
export function usePdfThumbnails() {
  const filePath = usePdfStore((s) => s.filePath);
  const [thumbs, setThumbs] = useState<Record<number, ThumbState>>({});
  const [numPages, setNumPages] = useState(0);
  const [loading, setLoading] = useState(false);
  const docRef = useRef<any>(null);
  const cancelledRef = useRef(false);

  useEffect(() => {
    let active = true;
    cancelledRef.current = false;

    async function run() {
      if (!filePath) {
        setThumbs({});
        setNumPages(0);
        return;
      }
      setLoading(true);
      setThumbs({});
      setNumPages(0);

      try {
        // 桥接层根据运行环境返回可用 URL：Tauri 用 convertFileSrc，浏览器用后端原始文件接口
        const src = convertFileSrc(filePath);
        // pdfjs 6 起只收 DocumentInitParameters 对象（裸字符串简写已删除，
        // 传 string 会抛 "expected either data, range, or url parameter"）
        const task = pdfjsLib.getDocument({ url: src } as any);
        const pdf: any = await task.promise;
        if (!active) {
          pdf.destroy?.();
          return;
        }
        docRef.current = pdf;
        setNumPages(pdf.numPages);

        // 并发受限的渲染队列
        const queue = Array.from({ length: pdf.numPages }, (_, i) => i);
        let cursor = 0;

        const worker = async () => {
          while (cursor < queue.length) {
            const pageIndex = queue[cursor++];
            const page = await pdf.getPage(pageIndex + 1);
            if (!active) return;
            const viewport = page.getViewport({ scale: THUMB_SCALE });
            const canvas = document.createElement("canvas");
            const ctx = canvas.getContext("2d");
            if (!ctx) return;
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            // pdfjs v4 的 RenderParameters 使用 canvas（取代旧版 canvasContext）
            await page.render({ canvas, viewport }).promise;
            if (!active) return;
            const url = canvas.toDataURL("image/png");
            setThumbs((prev) => ({
              ...prev,
              [pageIndex]: {
                url,
                width: viewport.width,
                height: viewport.height,
              },
            }));
            page.cleanup();
          }
        };

        await Promise.all(
          Array.from({ length: RENDER_CONCURRENCY }, () => worker())
        );
      } catch (e) {
        console.error("缩略图渲染失败:", e);
      } finally {
        if (active) setLoading(false);
      }
    }

    run();

    return () => {
      active = false;
      cancelledRef.current = true;
      if (docRef.current) {
        docRef.current.destroy().catch(() => {});
        docRef.current = null;
      }
    };
  }, [filePath]);

  return { thumbs, numPages, loading };
}
