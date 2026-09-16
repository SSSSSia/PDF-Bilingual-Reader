import { useEffect, useState } from "react";
import * as pdfjsLib from "pdfjs-dist";
// Vite 把 worker 作为本地资源打包（与 usePdfThumbnails 同一约定；重复赋值 workerSrc 幂等）
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { convertFileSrc } from "../lib/bridge";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

/**
 * 加载 PDF 文档。
 * 桥接层双模：Tauri convertFileSrc / 浏览器 /api/file/raw。
 *
 * pdfjs 6 破坏性变更：getDocument 只收 DocumentInitParameters 对象，
 * 裸字符串简写已删除（传 string 时 src.url 为 undefined 直接抛
 * "expected either data, range, or url parameter"，实测踩坑）。
 * 卸载/换文件时 destroy 句柄防泄漏。
 */
export function usePdfDocument(filePath: string | null) {
  const [pdf, setPdf] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!filePath) return;
    let cancelled = false;
    let doc: any = null;
    (async () => {
      try {
        setError(null);
        doc = await pdfjsLib
          .getDocument({ url: convertFileSrc(filePath) } as any)
          .promise;
        if (cancelled) {
          doc.destroy?.();
          return;
        }
        setPdf(doc);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
      doc?.destroy?.();
    };
  }, [filePath]);

  return { pdf, error };
}
