import { useEffect } from "react";
import { createPortal } from "react-dom";

interface Props {
  open: boolean;
  title: string;
  description?: string;
  confirmText?: string;
  cancelText?: string;
  /** 危险操作（删除等）确认按钮用红色 */
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * 应用内二次确认对话框。
 * 不用 window.confirm：Tauri WebView2 对原生 confirm/alert 支持不可靠
 * （可能静默返回 false），必须自绘模态框。
 * 反馈①：改经 portal 挂到 body——部分调用点（如 Sidebar）位于
 * position:sticky 容器内，sticky 会创建独立 stacking context，fixed z-50
 * 蒙版被困其中，正文里自带堆叠上下文的元素反而画在蒙版之上（表现为"部分
 * 元素高亮"且可点击穿透）；portal 到 body 后层级归位根上下文，全局生效。
 */
export default function ConfirmDialog({
  open,
  title,
  description,
  confirmText = "确认",
  cancelText = "取消",
  danger = false,
  onConfirm,
  onCancel,
}: Props) {
  // Esc 取消
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-800"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">
          {title}
        </h2>
        {description && (
          <p className="mt-2 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
            {description}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onCancel} className="btn-secondary">
            {cancelText}
          </button>
          <button
            autoFocus
            onClick={onConfirm}
            className={`rounded-lg px-4 py-2 text-sm font-medium text-white transition-colors duration-150 focus:outline-none focus:ring-2 focus:ring-offset-1 ${
              danger
                ? "bg-red-600 hover:bg-red-700 focus:ring-red-500/40"
                : "bg-blue-600 hover:bg-blue-700 focus:ring-blue-500/40"
            }`}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
