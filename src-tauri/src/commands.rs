use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::State;
use tauri_plugin_shell::process::CommandChild;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AppConfig {
    pub ocr: OcrConfig,
    pub translate: TranslateConfig,
    pub ui: UiConfig,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct OcrConfig {
    pub provider: String,
    pub api_key: String,
    pub api_url: String,
    pub model: String,
    pub optional_payload: serde_json::Value,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct TranslateConfig {
    pub provider: String,
    pub api_key: String,
    pub api_url: String,
    pub model: String,
    pub target_language: String,
    pub source_language: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct UiConfig {
    pub default_mode: String,
    pub theme: String,
}

pub struct AppState {
    pub config_path: PathBuf,
    pub cache_dir: PathBuf,
    pub fastapi_url: String,
    /// 内嵌 FastAPI sidecar 的子进程句柄，应用退出时 kill，避免残留进程
    pub backend_child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
pub async fn load_config(state: State<'_, AppState>) -> Result<String, String> {
    let path = state.config_path.clone();
    if path.exists() {
        let content = fs::read_to_string(&path).map_err(|e| e.to_string())?;
        Ok(content)
    } else {
        let default = serde_json::to_string(&AppConfig {
            ocr: OcrConfig {
                provider: "siliconflow".to_string(),
                api_key: "".to_string(),
                api_url: "https://api.siliconflow.cn/v1".to_string(),
                model: "PaddlePaddle/PaddleOCR-VL-1.5".to_string(),
                optional_payload: serde_json::json!({
                    "useDocOrientationClassify": false,
                    "useDocUnwarping": false,
                    "useChartRecognition": false
                }),
            },
            translate: TranslateConfig {
                provider: "siliconflow".to_string(),
                api_key: "".to_string(),
                api_url: "https://api.siliconflow.cn/v1".to_string(),
                model: "deepseek-ai/DeepSeek-V4-Flash".to_string(),
                target_language: "en".to_string(),
                source_language: "zh".to_string(),
            },
            ui: UiConfig {
                default_mode: "bilingual".to_string(),
                theme: "light".to_string(),
            },
        })
        .map_err(|e| e.to_string())?;
        Ok(default)
    }
}

// rename_all = "snake_case"：Tauri v2 默认要求 JS 端传 camelCase 参数，
// 前端桥接层统一传 snake_case（config_str / file_path / job_id），故显式声明。
#[tauri::command(rename_all = "snake_case")]
pub async fn save_config(state: State<'_, AppState>, config_str: String) -> Result<(), String> {
    let path = state.config_path.clone();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::write(&path, config_str).map_err(|e| e.to_string())?;
    Ok(())
}

/// 等待 FastAPI sidecar 就绪（最多约 15 秒），避免应用刚启动时请求被拒
async fn ensure_backend_ready(state: &AppState) -> Result<(), String> {
    let client = reqwest::Client::new();
    for _ in 0..30 {
        if let Ok(resp) = client
            .get(format!("{}/api/health", state.fastapi_url))
            .send()
            .await
        {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    Err("FastAPI 后端未就绪（已等待 15 秒）".to_string())
}

/// 前端启动就绪门：sidecar onefile 首启解压+Defender 扫描可
/// 能数十秒，前端等它再渲染会发请求的页面。Rust reqwest 探测（不走系统
/// 代理的 webview 网络栈），timeout_ms 由前端给定（默认 120s）。
#[tauri::command(rename_all = "snake_case")]
pub async fn backend_health(
    state: State<'_, AppState>,
    timeout_ms: Option<u64>,
) -> Result<(), String> {
    let client = reqwest::Client::new();
    let timeout = timeout_ms.unwrap_or(120_000);
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_millis(timeout);
    while tokio::time::Instant::now() < deadline {
        if let Ok(resp) = client
            .get(format!("{}/api/health", state.fastapi_url))
            .send()
            .await
        {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(1000)).await;
    }
    Err("后端未就绪（等待超时）".to_string())
}

#[tauri::command(rename_all = "snake_case")]
pub async fn run_pipeline(state: State<'_, AppState>, file_path: String) -> Result<String, String> {
    ensure_backend_ready(&state).await?;
    let client = reqwest::Client::new();
    let url = format!("{}/api/pipeline/run", state.fastapi_url);
    let resp = client
        .post(&url)
        .json(&serde_json::json!({ "file_path": file_path }))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let body = resp.text().await.map_err(|e| e.to_string())?;
    Ok(body)
}

#[tauri::command(rename_all = "snake_case")]
pub async fn get_pipeline_status(
    state: State<'_, AppState>,
    job_id: String,
) -> Result<String, String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/pipeline/status/{}", state.fastapi_url, job_id);
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let body = resp.text().await.map_err(|e| e.to_string())?;
    Ok(body)
}

#[tauri::command(rename_all = "snake_case")]
pub async fn check_file_exists(_state: State<'_, AppState>, file_path: String) -> Result<bool, String> {
    Ok(std::path::Path::new(&file_path).exists())
}

#[tauri::command]
pub async fn get_cache_dir(state: State<'_, AppState>) -> Result<String, String> {
    Ok(state.cache_dir.to_string_lossy().to_string())
}

/// 代理「测试连接」请求到 FastAPI /api/config/test。
/// Tauri webview 直连第三方/本地 API 有 CORS 与私有网络访问限制，统一走后端。
#[tauri::command]
pub async fn test_api_connection(
    state: State<'_, AppState>,
    spec: serde_json::Value,
) -> Result<String, String> {
    ensure_backend_ready(&state).await?;
    let client = reqwest::Client::new();
    let url = format!("{}/api/config/test", state.fastapi_url);
    let resp = client
        .post(&url)
        .json(&spec)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    // 不因 4xx/5xx 直接失败：把状态码与 body 一起交由前端展示具体原因
    let status = resp.status().as_u16();
    let body = resp.text().await.map_err(|e| e.to_string())?;
    if status >= 400 {
        return Err(format!("HTTP {}: {}", status, body));
    }
    Ok(body)
}

/// 将导出的双语内容写入用户所选路径（Markdown / 纯文本）。
/// 由前端用 dialog save 选择路径后调用，避免前端直接写文件权限问题。
#[tauri::command]
pub async fn export_content(
    _state: State<'_, AppState>,
    path: String,
    content: String,
) -> Result<(), String> {
    fs::write(&path, content).map_err(|e| e.to_string())?;
    Ok(())
}

/// 把缓存里的成品文件（BabelDOC 双语 PDF 等二进制产物）复制到用户所选
/// 路径。
/// export_content 只收字符串写不了二进制；fs 插件未引入，经 Rust 落盘。
#[tauri::command]
pub async fn export_copy_file(
    _state: State<'_, AppState>,
    src: String,
    dst: String,
) -> Result<(), String> {
    fs::copy(&src, &dst).map(|_| ()).map_err(|e| e.to_string())
}
