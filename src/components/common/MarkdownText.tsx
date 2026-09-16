import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import type { PluggableList } from "unified";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { assetUrl, openExternal } from "../../lib/bridge";
import { preprocessMath } from "../../lib/mathFragment";

// 含中文判定（CJK 扩展 A + 基本区）
const _CJK = /[\u3400-\u4dbf\u4e00-\u9fff]/;

/** 摊平 React 子节点取纯文本（用于判断 em 内容的语言） */
function nodeText(node: unknown): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (typeof node === "object" && (node as any).props)
    return nodeText((node as any).props.children);
  return "";
}

// react-markdown 对 props 做引用比较，此前每次 render 新建
// 插件数组/components 对象会让其内部缓存失效、text 变化时全量重解析——
// 提为模块级常量（text 不变时整个组件被 memo 跳过，不再走到这里）
const REMARK_PLUGINS: PluggableList = [remarkGfm, remarkMath];
const REHYPE_PLUGINS: PluggableList = [
  [rehypeKatex, { throwOnError: false, strict: false }],
];
const allowAllUrls = (url: string) => url;
const COMPONENTS: Components = {
  // 中文斜体修正：源文粗斜体（论文标题常见）
  // 保留 _.._ 标记 → <em> 斜体 → 中文没有真斜体字形，Windows
  // Chromium 回退渲染成楷体。中文排版规范强调用粗体不用斜体：
  // 含中文的 em 转加粗正体；纯西文 em 保持斜体（术语/书名惯例）
  em: ({ children }) =>
    _CJK.test(nodeText(children)) ? (
      <em className="not-italic font-bold">{children}</em>
    ) : (
      <em>{children}</em>
    ),
  a: ({ href, children }) => {
    // 链接不在软件内打开（会顶掉整个阅读界面）：
    // Tauri 内经 shell.open 交给默认浏览器，浏览器内开新标签页
    const url = typeof href === "string" ? href : "";
    return (
      <a
        href={url}
        onClick={(e) => {
          e.preventDefault();
          if (url) openExternal(url).catch(() => {});
        }}
      >
        {children}
      </a>
    );
  },
  img: ({ src, alt }) => {
    const raw = typeof src === "string" ? src : "";
    return (
      <img
        src={assetUrl(raw)}
        alt={alt || ""}
        loading="lazy"
        className="mx-auto max-w-full rounded-lg"
      />
    );
  },
  // 学术宽表兜底：列多时横向滚动，不撑破双栏布局
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table>{children}</table>
    </div>
  ),
};

/**
 * Markdown 渲染组件：OCR/文本层返回的是 markdown（含表格/标题/公式/图片引用），
 * 旧实现当纯文本渲染导致排版错乱（实测问题），统一走 typography 排版。
 * 图片引用是本地绝对路径（文本层提取导出的论文插图），经 assetUrl 转为可访问 URL。
 *
 * urlTransform 必须覆写：
 * react-markdown v10 默认 defaultUrlTransform 只放行 http/https 等协议，
 * 本地盘符路径 `D:/...` 被当作未知协议 `d:` 整个剥成空串——img src 恒为空。
 * 内容全部来自用户本地 PDF 提取，信任来源，直接原样放行。
 */
const MarkdownTextImpl = ({ text }: { text: string }) => {
  return (
    <div className="prose prose-sm prose-zoomable max-w-none dark:prose-invert sm:prose-base prose-headings:my-2 prose-p:my-1.5 prose-table:my-2 prose-li:my-0.5 prose-pre:my-2 prose-img:my-2 prose-img:rounded-lg prose-h1:text-xl prose-h1:leading-snug prose-h2:text-lg prose-h2:leading-snug prose-h3:text-base prose-h4:text-base">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        urlTransform={allowAllUrls}
        components={COMPONENTS}
      >
        {preprocessMath(text || "")}
      </ReactMarkdown>
    </div>
  );
};

/** memo 化——数百个块卡片流式翻译时，未收到新译文的块直接跳过渲染 */
export default memo(MarkdownTextImpl);
