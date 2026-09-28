/**
 * Tauri 系统级文件拖拽桥：整个应用生命周期只注册一次 onDragDropEvent，
 * 以 window CustomEvent 转发给当前页面（MainPage / AddArticlePage 监听）。
 *
 * 为什么不沿用各页面自注册：Tauri v2 + WebView2 下每次导航注册/注销
 * 原生拖拽钩子，反复数次后拖拽会整体失灵（Windows 实测：主页↔添加页
 * 来回切换后拖放无任何反应，重启才恢复）。App 级单次注册根治。
 *
 * 事件约定：
 * - `tauri-file-drag`      拖入/悬停（页面置高亮态）
 * - `tauri-file-drag-end`  拖离/落下（清除高亮）
 * - `tauri-file-drop`      落下，detail = 首个文件绝对路径
 */

let installed = false;

export function installTauriDragBridge(): void {
  if (installed) return;
  if (!("__TAURI_INTERNALS__" in window)) return;
  installed = true;
  void import("@tauri-apps/api/webview")
    .then(({ getCurrentWebview }) =>
      getCurrentWebview()
        .onDragDropEvent((event) => {
          const payload = event.payload;
          if (payload.type === "enter" || payload.type === "over") {
            window.dispatchEvent(new CustomEvent("tauri-file-drag"));
          } else if (payload.type === "leave") {
            window.dispatchEvent(new CustomEvent("tauri-file-drag-end"));
          } else if (payload.type === "drop") {
            window.dispatchEvent(new CustomEvent("tauri-file-drag-end"));
            const paths = payload.paths;
            if (paths && paths.length > 0) {
              window.dispatchEvent(
                new CustomEvent<string>("tauri-file-drop", { detail: paths[0] }),
              );
            }
          }
        })
        .then(() => {
          // 留痕：拖拽再失灵时，logs/frontend.log 里有无这条即是分水岭
          // （无=原生监听没注册上；有=监听活着但 WebView2 钩子失效，走 HTML5 兜底）
          void import("./bridge").then(({ logFrontend }) =>
            logFrontend("info", "tauri drag bridge registered"),
          );
        }),
    )
    .catch((e) => {
      installed = false; // 允许下次挂载重试
      void import("./bridge").then(({ logFrontend }) =>
        logFrontend("error", `tauri drag bridge register failed: ${String(e)}`),
      );
    });
}
