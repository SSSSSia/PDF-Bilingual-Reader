import { useEffect, useState } from "react";
import { useUiStore, ZOOM_STEP } from "../stores/uiStore";

/**
 * 全局缩放：阅读页根容器挂 Ctrl+滚轮缩放（0.7–2.0，步进 0.1），
 * 对照/紧跟/原版三种内容形态统一生效（zoom 写 uiStore 并持久化）。
 * 另附键盘快捷键 Ctrl + =/-/0（放大/缩小/重置）作为滚轮的兜底入口。
 *
 * 实现要点：
 * - `preventDefault` 阻止 WebView2 的浏览器级 Ctrl+滚轮页面缩放
 * （Chromium 语义：非被动监听可拦截）；
 * - `stopPropagation`：未来若出现多层容器各自挂 hook，防止重复步进；
 * - 逐格即时提交：缩放的两相化（CSS 实时跟随 / canvas 停顿后重绘）
 * 在 uiStore.renderZoom 层完成（ZOOM_SETTLE_MS），本 hook 无需再防抖——
 * 旧的「150ms 累积后一次提交」滚动中零反馈、停下后突跳，正是卡顿感来源；
 * - callback-ref 模式（useState + effect [node]）：条件渲染/重挂载/
 * 时序差异下都能可靠绑定（useRef + effect [] 在 ref 晚挂载时会静默失效）。
 */
export function useZoomWheel<T extends HTMLElement>() {
  const [node, setNode] = useState<T | null>(null);

  useEffect(() => {
    if (!node) return;

    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      e.stopPropagation();
      useUiStore.getState().stepZoom(e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP);
    };
    // 键盘兜底：Ctrl = / + 放大、Ctrl - 缩小、Ctrl 0 重置
    // （WebView2/浏览器对这三个组合键的页面级 preventDefault 可拦截，
    //   与 Ctrl+W/T 等保留快捷键不同）
    const onKeyDown = (e: KeyboardEvent) => {
      if (!e.ctrlKey) return;
      if (e.key === "=" || e.key === "+") {
        e.preventDefault();
        useUiStore.getState().stepZoom(ZOOM_STEP);
      } else if (e.key === "-") {
        e.preventDefault();
        useUiStore.getState().stepZoom(-ZOOM_STEP);
      } else if (e.key === "0") {
        e.preventDefault();
        useUiStore.getState().resetZoom();
      }
    };

    node.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("keydown", onKeyDown);
    return () => {
      node.removeEventListener("wheel", onWheel);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [node]);

  return setNode;
}
