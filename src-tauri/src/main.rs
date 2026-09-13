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
            commands::test_api_connection,
        ])
        .setup(|_app| {
            // ── 内嵌 FastAPI sidecar（决策 D1）──────────────────────────
            // release：打包了 pdf-backend.exe，由 Tauri 拉起本地 8000 端口。
            // dev    ：不拉起，沿用 scripts/dev-start.ps1 单独启动的后端，
            //          避免两边同时占用 8000 端口。
            #[cfg(not(debug_assertions))]
            {
                use tauri_plugin_shell::ShellExt;
                use tauri_plugin_shell::process::CommandEvent;

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
        // 应用退出时杀掉内嵌后端，避免残留进程继续占用 8000 端口
        if let tauri::RunEvent::Exit = event {
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
