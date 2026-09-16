import { invoke } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";
import { PageResult } from "../types";
import { checkBabeldocCached, isTauri, openLocalPdf } from "../lib/bridge";

/** 导出收敛为两种文件：
 * markdown = 重排版双语内容（Markdown 译文）；pdf = BabelDOC 原版对照 PDF */
export type ExportFormat = "markdown" | "pdf";

interface ExportMeta {
  source?: string;
  target?: string;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/** 文档名去扩展（导出默认文件名用） */
function docTitle(name: string | undefined): string {
  return (name || "").replace(/\.pdf$/i, "") || "bilingual";
}

// 双语 Markdown：每页一个二级标题，原文/译文成对呈现，便于二次编辑与分享。
export function buildMarkdown(pages: PageResult[], meta?: ExportMeta): string {
  const lines: string[] = ["# PDF 双语对照导出", ""];
  if (meta?.source || meta?.target) {
    lines.push(`> 翻译方向: ${meta.source ?? "?"} → ${meta.target ?? "?"}`);
  }
  lines.push(`> 共 ${pages.length} 页`, "");

  pages.forEach((page) => {
    lines.push(`## 第 ${page.page + 1} 页`, "");
    page.blocks.forEach((b) => {
      lines.push("**原文**", b.original || "", "", "**译文**", b.translated || "", "");
    });
  });
  return lines.join("\n");
}

/**
 * 导出双语 Markdown。
 * - Tauri 环境：用 save 对话框选路径，再经 Rust `export_content` 写文件（正确落盘）。
 * - 浏览器回退：直接触发 blob 下载（dev 无 Rust 后端时仍可用）。
 * @returns 是否成功导出
 */
export async function exportBilingual(
  pages: PageResult[],
  fileName: string | undefined,
  meta?: ExportMeta,
): Promise<boolean> {
  const content = buildMarkdown(pages, meta);
  const defaultName = `${docTitle(fileName)}-双语-${today()}.md`;

  if (isTauri()) {
    try {
      const selected = await save({
        defaultPath: defaultName,
        filters: [{ name: "Markdown", extensions: ["md"] }],
      });
      if (!selected) return false;
      await invoke("export_content", { path: selected, content });
      return true;
    } catch (e) {
      console.error("Tauri 导出失败，回退到下载:", e);
    }
  }

  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = defaultName;
  a.click();
  URL.revokeObjectURL(url);
  return true;
}

/**
 * 保存 BabelDOC 原版对照 PDF（从缓存复制到用户所选路径）。
 * - Tauri：save 对话框 + Rust `export_copy_file` 二进制复制；
 * - 浏览器：经 openLocalPdf 走后端原始文件接口新标签页打开（另存）。
 * @returns 是否成功保存（用户取消返回 false）
 */
export async function saveDualPdf(
  dualPath: string,
  fileName: string | undefined,
): Promise<boolean> {
  if (!isTauri()) {
    await openLocalPdf(dualPath);
    return true;
  }
  const selected = await save({
    defaultPath: `${docTitle(fileName)}-原版对照.pdf`,
    filters: [{ name: "PDF", extensions: ["pdf"] }],
  });
  if (!selected) return false;
  await invoke("export_copy_file", { src: dualPath, dst: selected });
  return true;
}

/** 探测文档是否已有原版对照成品（命中即可直接保存，未命中需先生成） */
export async function probeDualPdf(
  filePath: string,
): Promise<{ cached: boolean; dual_path: string }> {
  return checkBabeldocCached(filePath);
}
