"""配置加载测试。

核心是优先级链：CLI 参数 > 环境变量 > config.toml > 内置默认值。
以及两条铁律：坏 TOML 不拦运行；api_key 的来源 env 优先于文件。
"""

from __future__ import annotations

import pytest

from niulai.config import Settings, read_config_file
from niulai.errors import ConfigError
from niulai.paths import data_dir


def _clear_env(monkeypatch) -> None:
    for name in (
        "NIULAI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "NIULAI_MODEL",
        "OPENAI_MODEL",
        "NIULAI_BASE_URL",
        "OPENAI_BASE_URL",
        "NIULAI_PROTOCOL",
    ):
        monkeypatch.delenv(name, raising=False)


def _write_config(tmp_path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_数据目录默认在用户主目录的_niulai下(monkeypatch):
    monkeypatch.delenv("NIULAI_HOME", raising=False)
    assert data_dir().name == ".niulai"


def test_配置文件不存在时走默认值(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")

    settings = Settings.load(config_file=tmp_path / "none.toml")
    assert settings.model == "deepseek-flash"
    assert settings.protocol == "openai"
    assert settings.base_url == "https://api.deepseek.com/v1"
    assert settings.max_turns == 12


def test_文件值在env缺省时生效(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    path = _write_config(
        tmp_path,
        '[llm]\nmodel = "kimi-moonshot"\nprotocol = "openai"\nmax_turns = 5\n',
    )

    settings = Settings.load(config_file=path)
    assert settings.model == "kimi-moonshot"
    assert settings.max_turns == 5


def test_env优先级高于文件(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    monkeypatch.setenv("NIULAI_MODEL", "env-model")
    path = _write_config(tmp_path, '[llm]\nmodel = "file-model"\n')

    assert Settings.load(config_file=path).model == "env-model"


def test_CLI参数优先级最高(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    monkeypatch.setenv("NIULAI_PROTOCOL", "anthropic")
    path = _write_config(tmp_path, '[llm]\nprotocol = "openai"\n')

    settings = Settings.load(protocol="openai", config_file=path)
    assert settings.protocol == "openai"
    assert settings.base_url == "https://api.deepseek.com/v1"


def test_文件里的协议能带动默认接入点和模型(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    path = _write_config(tmp_path, '[llm]\nprotocol = "anthropic"\n')

    settings = Settings.load(config_file=path)
    assert settings.protocol == "anthropic"
    assert settings.base_url == "https://api.deepseek.com/anthropic"
    assert settings.model == "deepseek-flash"


def test_坏TOML不拦运行(monkeypatch, tmp_path, capsys):
    """用户手编配置打错一个引号：按默认值跑 + stderr 警告，绝不抛异常。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    path = _write_config(tmp_path, '[llm]\nmodel = "没闭合的引号\n')

    assert read_config_file(path) == {}
    settings = Settings.load(config_file=path)
    assert settings.model == "deepseek-flash"
    assert "解析失败" in capsys.readouterr().err


def test_数字字段类型打错也能兜住(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    path = _write_config(
        tmp_path,
        '[llm]\nmax_turns = "五"\ntool_timeout_s = "abc"\nprompt_cache = "false"\n',
    )

    settings = Settings.load(config_file=path)
    assert settings.max_turns == 12  # 非法值 → 默认
    assert settings.tool_timeout_s == 15.0
    assert settings.prompt_cache is False  # 字符串 "false" 能识别


def test_api_key_env优先于文件(monkeypatch, tmp_path):
    """env 和文件同时有 key 时，env 赢。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-from-env")
    path = _write_config(
        tmp_path, '[llm]\nprotocol = "openai"\napi_key = "sk-from-file"\n'
    )

    settings = Settings.load(config_file=path)
    assert settings.api_key == "sk-from-env"


def test_api_key_env缺省时从文件读(monkeypatch, tmp_path):
    """本地单用户场景：文件里的 api_key 也认，省去每个终端设 env。"""
    _clear_env(monkeypatch)
    path = _write_config(
        tmp_path, '[llm]\nprotocol = "openai"\napi_key = "sk-from-file"\n'
    )

    settings = Settings.load(config_file=path)
    assert settings.api_key == "sk-from-file"


def test_api_key两处都没有时报错(monkeypatch, tmp_path):
    """env 和文件都没有 key → ConfigError。"""
    _clear_env(monkeypatch)
    path = _write_config(tmp_path, '[llm]\nprotocol = "openai"\n')

    with pytest.raises(ConfigError):
        Settings.load(config_file=path)


def test_read_config_file缺文件返回空(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    assert read_config_file() == {}
