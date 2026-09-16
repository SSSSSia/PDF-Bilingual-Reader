#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;

use commands::AppState;
use std::sync::Mutex;
use tauri::Manager;

fn main() {
    let app_data = std::env::var("APPDATA")
        .unwrap_or_else(|_| String::from(""));
    let base_dir = if app_data.is_empty() {
        std::env::var("HOME")
            .unwrap_or_else(|_| String::from("."))
    } else {
        app_data
    };
    let app_path = format!("{}/pdf-reader", base_dir);
    let config_path = std::path::PathBuf::from(format!("{}/config.json", app_path));
    let cache_dir = std::path::PathBuf::from(format!("{}/cache", app_path));

    let app_state = AppState {
        config_path: config_path.clone(),
        cache_dir: cache_dir.clone(),
        fastapi_url: "http://127.0.0.1:8000".to_string(),
        backend_child: Mutex::new(None),
    };

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(app_state)
        .invoke_handler(tauri::generate_handler![
            commands::load_config,
            commands::save_config,
            commands::backend_health,
            commands::run_pipeline,
            commands::get_pipeline_status,
            commands::check_file_exists,
            commands::get_cache_dir,
            commands::export_content,
            commands::export_copy_file,
            commands::test_api_connection,
        ])
        .setup(|_app| {
            // ── 窗口尺寸自适应────
            // 1400x900 在小屏/缩放屏上超出可视区：钳制到工作区 90% 并居中
            {
                use tauri::Manager;
                if let Some(win) = _app.get_webview_window("main") {
                    if let Ok(Some(monitor)) = win.current_monitor() {
                        let size = monitor.size();
                        let scale = monitor.scale_factor();
                        // 逻辑坐标 = 物理像素 / 缩放
                        let max_w = (size.width as f64 / scale * 0.9).floor();
                        let max_h = (size.height as f64 / scale * 0.9).floor();
                        let cur = win.outer_size().unwrap_or_default();
                        let cur_w = cur.width as f64 / scale;
                        let cur_h = cur.height as f64 / scale;
                        let new_w = cur_w.min(max_w);
                        let new_h = cur_h.min(max_h);
                        if (new_w - cur_w).abs() > 1.0 || (new_h - cur_h).abs() > 1.0 {
                            let _ = win.set_size(tauri::LogicalSize::new(new_w, new_h));
                        }
                        let _ = win.center();
                    }
                }
            }

            // ── 内嵌 FastAPI sidecar（决策 D1）──────────────────────────
            // release：打包了 pdf-backend.exe，由 Tauri 拉起本地 8000 端口。
            // dev    ：不拉起，沿用 scripts/dev-start.ps1 单独启动的后端，
            //          避免两边同时占用 8000 端口。
            #[cfg(not(debug_assertions))]
            {
                use tauri_plugin_shell::ShellExt;
                use tauri_plugin_shell::process::CommandEvent;

                // 启动前清理残留 sidecar：
                // 退出路径不全时（直接关窗/强杀/崩溃）RunEvent::Exit 不触发，
                // 旧 pdf-backend.exe 成孤儿占住 8000，导致本次 sidecar 绑定
                // 失败 + 升级安装器写文件失败。只清自己名下的进程名，不碰
                // 用户无关服务；dev 终端里的 .venv python 不受影响。
                use std::os::windows::process::CommandExt;
                let _ = std::process::Command::new("taskkill")
                    .args(["/F", "/IM", "pdf-backend.exe"])
                    .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
                    .output();
                std::thread::sleep(std::time::Duration::from_millis(300));

                let handle = _app.handle().clone();
                let sidecar = handle
                    .shell()
                    .sidecar("pdf-backend")
                    .map_err(|e| format!("定位内嵌后端失败: {}", e))?;
                let (mut rx, child) = sidecar
                    .spawn()
                    .map_err(|e| format!("启动内嵌后端失败: {}", e))?;

                if let Some(state) = handle.try_state::<AppState>() {
                    *state
                        .backend_child
                        .lock()
                        .map_err(|e| format!("后端句柄加锁失败: {}", e))? = Some(child);
                }

                tauri::async_runtime::spawn(async move {
                    while let Some(event) = rx.recv().await {
                        match event {
                            CommandEvent::Stdout(line) => {
                                println!("[backend] {}", String::from_utf8_lossy(&line))
                            }
                            CommandEvent::Stderr(line) => {
                                eprintln!("[backend] {}", String::from_utf8_lossy(&line))
                            }
                            CommandEvent::Terminated(payload) => {
                                eprintln!("[backend] 已退出: {:?}", payload);
                                break;
                            }
                            _ => {}
                        }
                    }
                });
            }

            #[cfg(debug_assertions)]
            eprintln!("[dev] 跳过内嵌后端启动，请通过 scripts/dev-start.ps1 启动 FastAPI");

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        // 应用退出时清扫内嵌后端，避免残留进程继续占用 8000 端口。
        // child.kill() 只杀 Tauri 直接子进程，而 PyInstaller onefile 的
        // uvicorn 跑在引导器派生的同名孙进程里（Windows 不向子进程传播
        // kill），孤儿会继续占 8000——终端另起后端即报端口占用。按映像
        // 名 /T 全树清扫才能杀净（进程名应用独占，与启动前清理同口径）；
        // 仅 release 有内嵌后端，dev 终端自起的后端不受影响。
        if let tauri::RunEvent::Exit = event {
            #[cfg(not(debug_assertions))]
            {
                use std::os::windows::process::CommandExt;
                let _ = std::process::Command::new("taskkill")
                    .args(["/F", "/T", "/IM", "pdf-backend.exe"])
                    .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
                    .output();
            }
            if let Some(state) = app_handle.try_state::<AppState>() {
                if let Ok(mut guard) = state.backend_child.lock() {
                    if let Some(child) = guard.take() {
                        let _ = child.kill();
                    }
                }
            }
        }
    });
}
