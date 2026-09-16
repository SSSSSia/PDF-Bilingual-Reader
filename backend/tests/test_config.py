import asyncio
import json

from config import settings


def test_hot_reload_via_mtime(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"ocr": {"api_key": "a"}, "translate": {"api_key": "b"}}),
        encoding="utf-8",
    )
    # 让 _resolve_config_path 走环境变量覆盖路径
    monkeypatch.setenv("PDF_READER_CONFIG", str(cfg_file))
    settings.config_path = ""
    asyncio.run(settings.init())
    assert settings.ocr_config["api_key"] == "a"

    # 修改文件内容，mtime 变化应触发重载
    cfg_file.write_text(
        json.dumps({"ocr": {"api_key": "c"}, "translate": {"api_key": "d"}}),
        encoding="utf-8",
    )
    changed = settings.refresh()
    assert changed is True
    assert settings.ocr_config["api_key"] == "c"


def test_data_dir_and_fallback_flag(tmp_path, monkeypatch):
    """data_dir = config.json 所在目录；显式 PDF_READER_CONFIG 不算兜底轨。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PDF_READER_CONFIG", str(cfg_file))
    settings.config_path = ""
    asyncio.run(settings.init())
    assert settings.data_dir == str(tmp_path)
    assert settings.using_fallback_dir() is False  # 显式指定不算兜底
    assert settings.cache_dir == str(tmp_path / "cache")
    assert (tmp_path / "cache").is_dir()  # init 已建缓存目录


def test_data_dir_uses_env_config_dir(tmp_path, monkeypatch):
    """data_dir = config.json 所在目录；显式 PDF_READER_CONFIG 不算兜底轨。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PDF_READER_CONFIG", str(cfg_file))
    settings.config_path = ""
    asyncio.run(settings.init())
    assert settings.data_dir == str(tmp_path)
    assert settings.using_fallback_dir() is False  # 显式指定不算兜底
    assert settings.cache_dir == str(tmp_path / "cache")
    assert (tmp_path / "cache").is_dir()  # init 已建缓存目录
