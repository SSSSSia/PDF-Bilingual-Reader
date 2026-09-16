import { useEffect, useState } from "react";
import type { DocMeta } from "../types";
import { renderFirstPage } from "../lib/pdfThumb";

/**
 * 文档库卡片首页缩略图。
 * 对 file_exists 的文档逐个渲染第一页（并发受限），失败/缺失留空由卡片显示占位。
 * docs 列表变化（返回主页重新拉取）时自动重算。
 */
export function useDocThumbnails(docs: DocMeta[] | null) {
  const [thumbs, setThumbs] = useState<Record<string, string>>({});

  useEffect(() => {
    let active = true;
    const targets = (docs ?? []).filter((d) => d.file_exists && d.file_path);
    if (targets.length === 0) {
      setThumbs({});
      return;
    }
    const queue = [...targets];
    let cursor = 0;
    const worker = async () => {
      while (cursor < queue.length) {
        const doc = queue[cursor++];
        const url = await renderFirstPage(doc.file_path, 0.5);
        if (!active) return;
        if (url) {
          setThumbs((prev) => ({ ...prev, [doc.doc_id]: url }));
        }
      }
    };
    void Promise.all(
      Array.from({ length: Math.min(3, queue.length) }, () => worker()),
    );
    return () => {
      active = false;
    };
  }, [docs]);

  return thumbs;
}
