import { useUiStore, type UploadMode } from "../../stores/uiStore";

const OPTIONS: { value: UploadMode; label: string; hint: string }[] = [
  {
    value: "typeset",
    label: "重排版",
    hint: "自研管线：重排版双语、原版点击翻译、Markdown 导出（默认）",
  },
  {
    value: "babeldoc",
    label: "BabelDOC 对照",
    hint: "原版排版对照 PDF 直出，版式还原更稳；仅支持英→中，生成约需数分钟",
  },
];

/**
 * 上传翻译模式切换（记住上次选择）。上传入口（主页页头 / 添加文章页）
 * 共用：拖拽与浏览上传均按当前选中模式分流。
 */
export default function UploadModeToggle() {
  const uploadMode = useUiStore((s) => s.uploadMode);
  const setUploadMode = useUiStore((s) => s.setUploadMode);
  return (
    <div className="flex flex-col items-end gap-1">
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
              title={o.hint}
              onClick={() => setUploadMode(o.value)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150 ${
                active
                  ? "bg-white text-slate-900 shadow-sm dark:bg-slate-600 dark:text-white"
                  : "text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
              }`}
            >
              {o.label}
            </button>
          );
        })}
      </div>
      <p className="text-xs text-slate-400 dark:text-slate-500">
        {OPTIONS.find((o) => o.value === uploadMode)?.hint}
      </p>
    </div>
  );
}
