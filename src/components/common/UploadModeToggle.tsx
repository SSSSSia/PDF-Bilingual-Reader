import { useUiStore, type UploadMode } from "../../stores/uiStore";

const OPTIONS: {
  value: UploadMode;
  title: string;
  desc: string;
  badge?: string;
  features: string[];
}[] = [
  {
    value: "typeset",
    title: "智能重排版",
    desc: "解析论文结构后重新排版，中文与原文逐段对照",
    badge: "默认",
    features: ["重排版·左右对照 / 紧跟", "原版PDF·点击翻译", "Markdown 译文导出"],
  },
  {
    value: "babeldoc",
    title: "原版对照",
    desc: "保留论文原始排版，生成原版+译文的对照 PDF",
    badge: "推荐",
    features: ["版式还原更稳（公式/复杂版式）", "重排版可稍后在阅读页补跑", "仅支持英→中"],
  },
];

/**
 * 翻译模式选择：默认=两张模式选择卡（添加文章页 / 主页空状态，上传流程
 * 第一步）；compact=页头紧凑分段切换（有文献时的主页页头，无说明文字）。
 * 记住上次选择；主页与添加页上传均按当前选中模式分流。
 */
export default function UploadModeToggle({ compact = false }: { compact?: boolean }) {
  const uploadMode = useUiStore((s) => s.uploadMode);
  const setUploadMode = useUiStore((s) => s.setUploadMode);
  if (compact) {
    return (
      <div
        role="radiogroup"
        aria-label="翻译模式"
        className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 dark:border-slate-700 dark:bg-slate-800"
      >
        {OPTIONS.map((o) => {
          const active = uploadMode === o.value;
          return (
            <button
              key={o.value}
              role="radio"
              aria-checked={active}
              title={o.desc}
              onClick={() => setUploadMode(o.value)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150 ${
                active
                  ? "bg-white text-slate-900 shadow-sm dark:bg-slate-600 dark:text-white"
                  : "text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
              }`}
            >
              {o.title}
            </button>
          );
        })}
      </div>
    );
  }
  return (
    <div
      role="radiogroup"
      aria-label="翻译模式"
      className="grid grid-cols-1 gap-3 sm:grid-cols-2"
    >
      {OPTIONS.map((o) => {
        const active = uploadMode === o.value;
        return (
          <button
            key={o.value}
            role="radio"
            aria-checked={active}
            onClick={() => setUploadMode(o.value)}
            className={`rounded-xl border-2 p-4 text-left transition-colors duration-150 ${
              active
                ? "border-blue-500 bg-blue-50/60 dark:border-blue-400 dark:bg-blue-900/20"
                : "border-slate-200 bg-white hover:border-blue-300 dark:border-slate-700 dark:bg-slate-800 dark:hover:border-blue-500"
            }`}
          >
            <span className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className={`h-4 w-4 shrink-0 rounded-full border-2 transition-colors duration-150 ${
                  active
                    ? "border-blue-600 bg-blue-600 ring-2 ring-white ring-inset dark:border-blue-400 dark:bg-blue-400 dark:ring-slate-800"
                    : "border-slate-300 bg-white dark:border-slate-500 dark:bg-slate-700"
                }`}
              />
              <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                {o.title}
              </span>
              {o.badge && (
                <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-900/50 dark:text-blue-300">
                  {o.badge}
                </span>
              )}
            </span>
            <span className="mt-1.5 block text-xs leading-snug text-slate-600 dark:text-slate-300">
              {o.desc}
            </span>
            <span className="mt-2 block text-xs leading-relaxed text-slate-400 dark:text-slate-500">
              {o.features.join(" · ")}
            </span>
          </button>
        );
      })}
    </div>
  );
}
