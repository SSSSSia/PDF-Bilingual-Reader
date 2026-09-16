import json
import os
import platform


class Settings:
    """
    配置加载与热更新（R7）。

    - init 在应用启动时计算配置文件路径、创建缓存目录并首次加载。
    - refresh 每次处理任务前调用：若配置文件的修改时间(mtime)变化，自动重载，
      无需重启后端即可生效（改完 API Key/模型后保存即可）。
    - 路径与 Rust 端保持一致：Windows -> %APPDATA%/pdf-reader/config.json，
      其他平台 -> ~/.pdf-reader/config.json。

    （持久化路径统一）：config.json / cache/ / docs_index.json /
    cache/images/ 全部收敛在 data_dir（config.json 所在目录）之下；
    ~/.pdf-reader/ 仅作无 APPDATA 环境时的兜底轨，启动日志会打印实际生效目录。
    """

    def __init__(self):
        self.config_path = ""
        self.cache_dir = ""
        self._mtime = -1
        self.ocr_config: dict = {}
        self.translate_config: dict = {}

    @property
    def data_dir(self) -> str:
        """用户数据目录 = config.json 所在目录（所有持久化数据的统一根）。"""
        return os.path.dirname(self.config_path)

    def using_fallback_dir(self) -> bool:
        """是否处于 ~/.pdf-reader 兜底轨（Windows 缺 APPDATA 时；打包版不应出现）。"""
        if os.environ.get("PDF_READER_CONFIG"):
            return False  # 显式指定配置文件不算兜底
        return not (platform.system() == "Windows" and os.environ.get("APPDATA"))

    async def init(self):
        self.config_path = self._resolve_config_path()
        self.cache_dir = os.path.join(os.path.dirname(self.config_path), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._load()

    def _resolve_config_path(self) -> str:
        # 允许通过环境变量覆盖配置文件路径（便于测试与自定义部署）
        env_path = os.environ.get("PDF_READER_CONFIG")
        if env_path:
            return os.path.abspath(env_path)
        if platform.system() == "Windows":
            app_data = os.environ.get("APPDATA")
            if app_data:
                return os.path.join(app_data, "pdf-reader", "config.json")
        return os.path.join(os.path.expanduser("~"), ".pdf-reader", "config.json")

    def refresh(self) -> bool:
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            return False
        if mtime != self._mtime:
            self._load()
            return True
        return False

    def _default_ocr(self) -> dict:
        return {
            "provider": "siliconflow",
            "api_key": "",
            "api_url": "https://api.siliconflow.cn/v1",
            "model": "PaddlePaddle/PaddleOCR-VL-1.5",
            "optional_payload": {
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            },
        }

    def _default_translate(self) -> dict:
        # 默认英→中
        # 默认档 = 免费模型（产品卖点：Qwen3-8B 在 SiliconFlow 免费，
        # 确认维持）。付费质量档（如 DeepSeek-V4-Flash，
        # ¥1/M token≈每篇 ¥0.1）是用户可选升级，见设置页「质量档位」说明，
        # 不设为默认。
        return {
            "provider": "siliconflow",
            "api_key": "",
            "api_url": "https://api.siliconflow.cn/v1",
            "model": "Qwen/Qwen3-8B",
            "target_language": "zh",
            "source_language": "en",
        }

    def _load(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            self.ocr_config = config.get("ocr", self._default_ocr())
            self.translate_config = config.get("translate", self._default_translate())
        else:
            self.ocr_config = self._default_ocr()
            self.translate_config = self._default_translate()
        try:
            self._mtime = os.path.getmtime(self.config_path)
        except OSError:
            self._mtime = -1


settings = Settings()
