from __future__ import annotations

import tomllib
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import secrets_store
from app.api import settings as settings_api
from app.config import settings
from app.services import ai_provider
from app.services.ai_provider import (
    _format_openai_error,
    _is_temperature_rejected,
    normalize_openai_base_url,
)


@pytest.fixture(autouse=True)
def _clear_rejected_params_cache():
    """参数降级缓存是进程级状态 → 每个用例前后清空，避免跨用例互相污染。"""
    ai_provider.reset_rejected_params_cache()
    yield
    ai_provider.reset_rejected_params_cache()


def test_normalize_openai_base_url_adds_v1_for_root_gateway():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_preserves_v1_base():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080/v1") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_strips_chat_completions_path():
    assert normalize_openai_base_url("http://ai.zedbox.cn:8080/v1/chat/completions") == "http://ai.zedbox.cn:8080/v1"


def test_normalize_openai_base_url_preserves_glm_v4():
    """智谱 GLM 用 /api/paas/v4, 不能强制补成 /v4/v1 (会 404)。"""
    assert normalize_openai_base_url("https://open.bigmodel.cn/api/paas/v4") == "https://open.bigmodel.cn/api/paas/v4"


def test_normalize_openai_base_url_strips_chat_completions_from_glm_v4():
    """用户填完整 /v4/chat/completions 时, 去掉后缀归一化为 /v4。"""
    assert normalize_openai_base_url("https://open.bigmodel.cn/api/paas/v4/chat/completions") == "https://open.bigmodel.cn/api/paas/v4"


def test_normalize_openai_base_url_preserves_other_version_segments():
    """其它非 v1 版本号 (/v2 等) 也应保持原样。"""
    assert normalize_openai_base_url("https://example.com/api/v2") == "https://example.com/api/v2"


def test_normalize_openai_base_url_strips_trailing_slash():
    assert normalize_openai_base_url("https://open.bigmodel.cn/api/paas/v4/") == "https://open.bigmodel.cn/api/paas/v4"


def test_openai_kwargs_prefers_final_answer_for_official_deepseek_v4():
    kwargs = ai_provider._openai_kwargs(
        temperature=0.5,
        max_tokens=8192,
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/v1",
        prefer_final_answer=True,
    )

    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_openai_kwargs_does_not_send_deepseek_option_to_other_providers():
    kwargs = ai_provider._openai_kwargs(
        temperature=0.5,
        max_tokens=8192,
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        prefer_final_answer=True,
    )

    assert "extra_body" not in kwargs


def test_openai_kwargs_keeps_deepseek_default_without_final_answer_preference():
    kwargs = ai_provider._openai_kwargs(
        temperature=0.5,
        max_tokens=8192,
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/v1",
    )

    assert "extra_body" not in kwargs


def test_format_openai_error_hides_html_gateway_body():
    response = httpx.Response(
        504,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<!DOCTYPE html><html><body><h1>Gateway Timeout</h1></body></html>",
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.InternalServerError("gateway timeout", response=response, body=response.text)

    message = _format_openai_error(exc)

    assert message == "AI 服务请求失败(504): AI 上游服务超时, 请稍后重试或检查 AI Base URL / 网络"
    assert "html" not in message.lower()
    assert "Gateway Timeout" not in message


def test_format_openai_error_prefers_upstream_detail_when_available():
    """有可读的上游 detail 时优先透出, 而不是用 400 通用文案吞掉。"""
    response = httpx.Response(
        400,
        json={"error": {"message": "model context length exceeded"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request",
        response=response,
        body={"error": {"message": "model context length exceeded"}},
    )

    message = _format_openai_error(exc)

    assert message == "AI 服务请求失败(400): model context length exceeded"


def test_format_openai_error_falls_back_to_status_message_without_detail():
    """上游无可读 detail (如 HTML 网关页) 时, 才回落到 400 通用文案。"""
    response = httpx.Response(
        400,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<!DOCTYPE html><html></html>",
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError("bad request", response=response, body=None)

    message = _format_openai_error(exc)

    assert message == "AI 服务请求失败(400): 请求参数无效, 请检查模型名称和上下文长度"


def test_is_temperature_rejected_matches_moonshot_message():
    """Moonshot 对 reasoning 模型报 'only 1 is allowed for this model'。"""
    response = httpx.Response(
        400,
        json={"error": {"message": "invalid temperature: only 1 is allowed for this model"}},
        request=httpx.Request("POST", "https://api.moonshot.cn/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request",
        response=response,
        body={"error": {"message": "invalid temperature: only 1 is allowed for this model"}},
    )
    assert _is_temperature_rejected(exc) is True


def test_optional_openai_params_use_targeted_400_fallbacks():
    response = httpx.Response(
        400,
        json={"error": {"message": "unsupported parameter: temperature"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request", response=response,
        body={"error": {"message": "unsupported parameter: temperature"}},
    )
    assert _is_temperature_rejected(exc) is True

    kwargs = {"max_tokens": 1000, "temperature": 0.3, "reasoning_effort": "high"}
    assert ai_provider._openai_retry_kwargs(exc, kwargs) == {
        "max_tokens": 1000,
        "reasoning_effort": "high",
    }

    response = httpx.Response(
        400,
        json={"error": {"message": "unrecognized request argument", "param": "reasoning_effort"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request", response=response,
        body={"error": {"message": "unrecognized request argument", "param": "reasoning_effort"}},
    )
    assert _is_temperature_rejected(exc) is False
    assert ai_provider._is_reasoning_effort_rejected(exc) is True
    assert ai_provider._openai_retry_kwargs(exc, kwargs) == {
        "max_tokens": 1000,
        "temperature": 0.3,
    }
    assert kwargs == {"max_tokens": 1000, "temperature": 0.3, "reasoning_effort": "high"}


def test_is_temperature_rejected_false_for_other_400():
    """非 temperature 相关的 400 (如 model not found) 不应触发去 temperature 重试。"""
    response = httpx.Response(
        400,
        json={"error": {"message": "model not found"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request", response=response,
        body={"error": {"message": "model not found"}},
    )
    assert _is_temperature_rejected(exc) is False


def test_thinking_body_rejected_falls_back_to_default_mode():
    """DeepSeek thinking 禁用参数被 400 拒绝时, 去参重试而非直接失败。"""
    response = httpx.Response(
        400,
        json={"error": {"message": "unknown parameter: thinking"}},
        request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad request", response=response,
        body={"error": {"message": "unknown parameter: thinking"}},
    )
    kwargs = {"max_tokens": None, "extra_body": {"thinking": {"type": "disabled"}}}
    retry = ai_provider._openai_retry_kwargs(exc, kwargs)
    assert retry == {"max_tokens": None}
    # 原 kwargs 不被修改
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    # 与 thinking 无关的 400 不触发该回退
    response_other = httpx.Response(
        400,
        json={"error": {"message": "model not found"}},
        request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions"),
    )
    exc_other = openai.BadRequestError(
        "bad request", response=response_other,
        body={"error": {"message": "model not found"}},
    )
    assert ai_provider._openai_retry_kwargs(exc_other, kwargs) is None


def test_is_temperature_rejected_false_for_non_400():
    response = httpx.Response(
        401,
        json={"error": {"message": "invalid api key"}},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    exc = openai.AuthenticationError("unauthorized", response=response, body=None)
    assert _is_temperature_rejected(exc) is False


def test_openai_kwargs_include_configured_reasoning_effort(monkeypatch):
    stored = {"ai_provider": "openai_compat"}
    monkeypatch.setattr(secrets_store, "load", lambda: stored)

    assert "reasoning_effort" not in ai_provider._openai_kwargs(temperature=None, max_tokens=1000)

    stored["ai_provider"] = "openai"
    assert ai_provider._openai_kwargs(temperature=None, max_tokens=1000)["reasoning_effort"] == "high"

    stored["ai_reasoning_effort"] = "custom-high"
    kwargs = ai_provider._openai_kwargs(temperature=0.3, max_tokens=1000)

    assert kwargs == {
        "max_tokens": 1000,
        "temperature": 0.3,
        "reasoning_effort": "custom-high",
    }

    stored["ai_reasoning_effort"] = ""
    assert "reasoning_effort" not in ai_provider._openai_kwargs(temperature=None, max_tokens=1000)

    stored["ai_reasoning_effort"] = "custom-high"
    stored["ai_provider"] = "openai_compat"
    assert "reasoning_effort" not in ai_provider._openai_kwargs(temperature=None, max_tokens=1000)


def test_openai_kwargs_none_max_tokens_omits_limit():
    """max_tokens=None → 不传上限(推理模型思考 token 计入预算, 分析类调用放开)。"""
    kwargs = ai_provider._openai_kwargs(temperature=0.5, max_tokens=None)
    assert "max_tokens" not in kwargs
    assert kwargs.get("temperature") == 0.5

    # 显式数值仍正常下发(策略标题生成等小任务依赖)
    assert ai_provider._openai_kwargs(temperature=None, max_tokens=8) == {"max_tokens": 8}


def test_codex_prompt_none_max_tokens_skips_length_hint():
    prompt = ai_provider._codex_prompt([{"role": "user", "content": "hi"}], max_tokens=None)
    assert "Keep the final answer" not in prompt

    bounded = ai_provider._codex_prompt([{"role": "user", "content": "hi"}], max_tokens=300)
    assert "Keep the final answer" in bounded


def test_ai_settings_keep_provider_models_separate(monkeypatch):
    stored = {
        "ai_provider": "openai_compat",
        "ai_model": "custom-api-model",
    }

    def save(updates: dict) -> dict:
        stored.update(updates)
        return stored

    def clear(*keys: str) -> dict:
        for key in keys:
            stored.pop(key, None)
        return stored

    monkeypatch.setattr(secrets_store, "load", lambda: stored)
    monkeypatch.setattr(secrets_store, "save", save)
    monkeypatch.setattr(secrets_store, "clear", clear)
    monkeypatch.setattr(ai_provider, "ai_configured", lambda provider=None: True)
    monkeypatch.setattr(settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(settings, "ai_base_url", "")
    monkeypatch.setattr(settings, "ai_model", "")
    monkeypatch.setattr(settings, "ai_codex_command", "codex")
    monkeypatch.setattr(settings, "ai_codex_reasoning_effort", "")
    monkeypatch.setattr(settings, "ai_user_agent", "")

    settings_api.save_ai_settings(
        settings_api.AiSettingsIn(
            provider="codex_cli",
            model="gpt-5.6-sol",
            codex_command="codex",
            codex_reasoning_effort="high",
        )
    )

    assert stored["ai_model"] == "custom-api-model"
    assert stored["ai_codex_model"] == "gpt-5.6-sol"

    settings_api.save_ai_settings(
        settings_api.AiSettingsIn(
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="openai-model",
            reasoning_effort="vendor-high",
        )
    )

    assert stored["ai_model"] == "openai-model"
    assert stored["ai_reasoning_effort"] == "vendor-high"
    assert stored["ai_codex_model"] == "gpt-5.6-sol"

    settings_api.save_ai_settings(
        settings_api.AiSettingsIn(
            provider="openai_compat",
            base_url="https://example.com/v1",
            model="new-custom-model",
        )
    )

    assert stored["ai_model"] == "new-custom-model"
    assert stored["ai_reasoning_effort"] == "vendor-high"
    assert stored["ai_codex_model"] == "gpt-5.6-sol"
# ── 输出上限 / 上下文窗口配置 ─────────────────────────────────


def test_resolve_max_tokens_none_stays_unlimited(monkeypatch):
    """None = 不传上限(推理模型放开) — 不能被映射成配置上限, 见 main 语义。"""
    monkeypatch.setattr(ai_provider, "current_ai_max_output_tokens", lambda: 8192)
    assert ai_provider._resolve_max_tokens(None) is None


def test_resolve_max_tokens_clamps_above_cap(monkeypatch):
    monkeypatch.setattr(ai_provider, "current_ai_max_output_tokens", lambda: 3000)
    assert ai_provider._resolve_max_tokens(9000) == 3000


def test_resolve_max_tokens_keeps_below_cap(monkeypatch):
    monkeypatch.setattr(ai_provider, "current_ai_max_output_tokens", lambda: 8192)
    assert ai_provider._resolve_max_tokens(2000) == 2000


def test_estimate_input_tokens_counts_cjk_and_ascii():
    # 中文按 1 字 1 token
    cjk = [{"role": "user", "content": "中文" * 100}]  # 200 字
    assert ai_provider._estimate_input_tokens(cjk) >= 200
    # 英文按 ~4 字符 1 token
    ascii_msg = [{"role": "user", "content": "a" * 400}]
    assert ai_provider._estimate_input_tokens(ascii_msg) <= 200


def test_check_input_budget_raises_when_over_window(monkeypatch):
    monkeypatch.setattr(ai_provider, "current_ai_context_window", lambda: 100)
    big = [{"role": "user", "content": "中" * 200}]  # 估算输入 ~200 tokens
    with pytest.raises(ValueError, match="上下文窗口"):
        ai_provider._check_input_budget(big, max_tokens=3000)


def test_check_input_budget_passes_within_window(monkeypatch):
    monkeypatch.setattr(ai_provider, "current_ai_context_window", lambda: 64000)
    small = [{"role": "user", "content": "中" * 100}]
    # 不抛异常
    ai_provider._check_input_budget(small, max_tokens=2000)


@pytest.mark.asyncio
async def test_generate_ai_text_clamps_max_tokens_to_config_cap(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(ai_provider, "current_ai_max_output_tokens", lambda: 3000)
    monkeypatch.setattr(ai_provider, "current_ai_context_window", lambda: 64000)

    async def fake_run(messages, *, temperature, max_tokens, timeout, model=None):
        captured["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(ai_provider, "_run_openai_once", fake_run)
    text = await ai_provider.generate_ai_text(
        [{"role": "user", "content": "hi"}], max_tokens=9000
    )
    assert text == "ok"
    assert captured["max_tokens"] == 3000


@pytest.mark.asyncio
async def test_generate_ai_text_default_cap_and_none_passthrough(monkeypatch):
    """默认 3000 且被钳制; 显式 None 贯穿为不限制。"""
    captured: dict = {}
    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(ai_provider, "current_ai_max_output_tokens", lambda: 4000)
    monkeypatch.setattr(ai_provider, "current_ai_context_window", lambda: 64000)

    async def fake_run(messages, *, temperature, max_tokens, timeout, model=None):
        captured["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(ai_provider, "_run_openai_once", fake_run)
    await ai_provider.generate_ai_text([{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == 3000  # 默认值, 未超 cap 原样下发
    await ai_provider.generate_ai_text(
        [{"role": "user", "content": "hi"}], max_tokens=None,
    )
    assert captured["max_tokens"] is None  # None = 推理模型放开, 不钳制


def test_save_ai_settings_persists_token_sizes(monkeypatch):
    from app.api import settings as settings_api
    from app.config import settings as app_settings

    saved: dict = {}
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates))
    monkeypatch.setattr(settings_api.secrets_store, "load", lambda: saved)
    original_output = app_settings.ai_max_output_tokens
    original_window = app_settings.ai_context_window
    try:
        req = settings_api.AiSettingsIn(
            provider="openai_compat",
            base_url="https://example.com/v1",
            api_key="sk-test",
            model="gpt-x",
            max_output_tokens=5000,
            context_window=128000,
        )
        result = settings_api.save_ai_settings(req)
        assert saved["ai_max_output_tokens"] == 5000
        assert saved["ai_context_window"] == 128000
        assert result["ai_max_output_tokens"] == 5000
        assert result["ai_context_window"] == 128000
    finally:
        app_settings.ai_max_output_tokens = original_output
        app_settings.ai_context_window = original_window


def test_save_ai_settings_rejects_non_positive(monkeypatch):
    from app.api import settings as settings_api
    from fastapi import HTTPException

    req = settings_api.AiSettingsIn(provider="openai_compat", max_output_tokens=-1)
    with pytest.raises(HTTPException):
        settings_api.save_ai_settings(req)
    req2 = settings_api.AiSettingsIn(provider="openai_compat", context_window=0)
    with pytest.raises(HTTPException):
        settings_api.save_ai_settings(req2)


def test_save_ai_settings_round_checkpoint(monkeypatch):
    """轮次检查点: 0=不检查 合法, 1-4 非法(防每轮弹卡), 保存后 GET/响应可见。"""
    from fastapi import HTTPException

    from app.api import settings as settings_api
    from app.config import settings as app_settings
    from app.services import ai_provider

    saved: dict = {}
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: saved.update(updates))
    monkeypatch.setattr(settings_api.secrets_store, "load", lambda: saved)
    original = app_settings.ai_round_checkpoint
    try:
        result = settings_api.save_ai_settings(
            settings_api.AiSettingsIn(provider="openai_compat", round_checkpoint=0),
        )
        assert saved["ai_round_checkpoint"] == 0
        assert result["ai_round_checkpoint"] == 0
        assert ai_provider.current_ai_round_checkpoint() == 0  # 0 有语义, 不落到默认

        with pytest.raises(HTTPException):
            settings_api.save_ai_settings(
                settings_api.AiSettingsIn(provider="openai_compat", round_checkpoint=3),
            )
    finally:
        app_settings.ai_round_checkpoint = original


async def _fake_openai_stream(*chunks):
    for chunk in chunks:
        yield chunk


def _stream_chunk(*, content=None, reasoning_content=None, finish_reason=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning_content)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


@pytest.mark.asyncio
async def test_iter_openai_text_rejects_reasoning_only_length_exhaustion():
    stream = _fake_openai_stream(
        _stream_chunk(reasoning_content="内部推理"),
        _stream_chunk(finish_reason="length"),
    )

    with pytest.raises(RuntimeError, match="推理达到输出长度上限"):
        async for _ in ai_provider._iter_openai_text(stream):
            pass


@pytest.mark.asyncio
async def test_iter_openai_text_rejects_truncated_partial_content():
    stream = _fake_openai_stream(
        _stream_chunk(content="未完成正文"),
        _stream_chunk(finish_reason="length"),
    )
    pieces = []

    with pytest.raises(RuntimeError, match="输出达到长度上限"):
        async for piece in ai_provider._iter_openai_text(stream):
            pieces.append(piece)

    assert pieces == ["未完成正文"]


@pytest.mark.asyncio
async def test_iter_openai_text_yields_complete_content_and_ignores_reasoning():
    stream = _fake_openai_stream(
        _stream_chunk(reasoning_content="内部推理"),
        _stream_chunk(content="完整"),
        _stream_chunk(content="正文"),
        _stream_chunk(finish_reason="stop"),
    )

    pieces = [piece async for piece in ai_provider._iter_openai_text(stream)]

    assert pieces == ["完整", "正文"]


@pytest.mark.asyncio
async def test_iter_openai_text_rejects_stream_without_content():
    stream = _fake_openai_stream(_stream_chunk(finish_reason="stop"))

    with pytest.raises(RuntimeError, match="未返回正文内容"):
        async for _ in ai_provider._iter_openai_text(stream):
            pass


def test_codex_process_env_excludes_application_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "test-path")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("TICKFLOW_API_KEY", "tickflow-secret")
    monkeypatch.setenv("AI_API_KEY", "ai-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("AUTH_PASSWORD", "password-secret")

    env = ai_provider._codex_process_env(tmp_path / "codex-home")

    assert env["PATH"] == "test-path"
    assert env["HTTPS_PROXY"] == "http://proxy.example"
    assert env["NO_COLOR"] == "1"
    assert env["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "TICKFLOW_API_KEY" not in env
    assert "AI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AUTH_PASSWORD" not in env


def test_codex_config_adapts_local_access_provider_for_docker(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_DOCKER_HOST", "host.docker.internal")
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "")
    monkeypatch.setattr(ai_provider, "current_codex_reasoning_effort", lambda: "")
    monkeypatch.setattr(
        ai_provider,
        "_read_codex_config",
        lambda: {
            "model_provider": "codex_local_access",
            "model": "gpt-5.6-sol",
            "model_providers": {
                "codex_local_access": {
                    "name": "Codex API Service",
                    "base_url": "http://localhost:62678/v1",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                    "supports_websockets": False,
                    "experimental_bearer_token": "local-secret",
                }
            },
        },
    )
    path = tmp_path / "config.toml"

    ai_provider._write_compatible_codex_config(path)

    with path.open("rb") as f:
        config = tomllib.load(f)
    assert config["model_provider"] == "codex_local_access"
    provider = config["model_providers"]["codex_local_access"]
    assert provider["base_url"] == "http://host.docker.internal:62678/v1"
    assert provider["experimental_bearer_token"] == "local-secret"
    assert provider["requires_openai_auth"] is True
    assert provider["supports_websockets"] is False


def test_codex_config_preserves_remote_provider_without_docker_rewrite(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_DOCKER_HOST", raising=False)
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "")
    monkeypatch.setattr(ai_provider, "current_codex_reasoning_effort", lambda: "")
    monkeypatch.setattr(
        ai_provider,
        "_read_codex_config",
        lambda: {
            "model_provider": "remote-api",
            "openai_base_url": "https://builtin.example/v1",
            "model_providers": {
                "remote-api": {
                    "base_url": "https://custom.example/v1",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                }
            },
        },
    )
    path = tmp_path / "config.toml"

    ai_provider._write_compatible_codex_config(path)

    with path.open("rb") as f:
        config = tomllib.load(f)
    assert config["model_provider"] == "remote-api"
    assert config["openai_base_url"] == "https://builtin.example/v1"
    provider = config["model_providers"]["remote-api"]
    assert provider["base_url"] == "https://custom.example/v1"
    assert provider["wire_api"] == "responses"
    assert provider["requires_openai_auth"] is True


# ---- Codex CLI 可用性检测 (实跑 --version, 不再仅 which) ----


class _FakeCompleted:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def test_codex_cli_available_runs_version_check(monkeypatch):
    """实跑 --version: 能发现 npm 壳存在但原生二进制跑不起来的情况。"""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _FakeCompleted(0)

    monkeypatch.setattr(ai_provider, "_codex_base_command", lambda: ["codex"])
    monkeypatch.setattr(ai_provider.subprocess, "run", fake_run)
    assert ai_provider.codex_cli_available() is True
    assert calls == [["codex", "--version"]]


def test_codex_cli_available_false_when_version_fails(monkeypatch):
    import subprocess

    monkeypatch.setattr(ai_provider, "_codex_base_command", lambda: ["codex"])
    monkeypatch.setattr(
        ai_provider.subprocess, "run", lambda a, **k: _FakeCompleted(1)
    )
    assert ai_provider.codex_cli_available() is False
    # 壳报 "Codex CLI not available" 这类非零退出同样判定不可用
    monkeypatch.setattr(
        ai_provider.subprocess,
        "run",
        lambda a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("codex", 1)),
    )
    assert ai_provider.codex_cli_available() is False


def test_codex_cli_available_false_when_command_missing(monkeypatch):
    def raise_not_found():
        raise RuntimeError("未找到 Codex CLI 命令: codex")

    monkeypatch.setattr(ai_provider, "_codex_base_command", raise_not_found)
    assert ai_provider.codex_cli_available() is False


@pytest.mark.asyncio
async def test_codex_exec_args_exclude_ephemeral(monkeypatch):
    """exec 参数不含 --ephemeral: 老版本 codex(如 0.58)无此参数, 传了直接报错。"""
    captured: dict = {}

    def fake_run_process(args, prompt, env, timeout):
        captured["args"] = list(args)
        return 0, b"ok", b""

    monkeypatch.setattr(ai_provider, "_codex_base_command", lambda: ["codex"])
    monkeypatch.setattr(ai_provider, "_prepare_codex_home", lambda p: None)
    monkeypatch.setattr(ai_provider, "_codex_process_env", lambda p: {})
    monkeypatch.setattr(ai_provider, "_run_codex_process", fake_run_process)
    monkeypatch.setattr(ai_provider, "_read_output_file", lambda p: "ok")
    monkeypatch.setattr(ai_provider, "_remove_tree_best_effort", lambda p: None)
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "gpt-5.6-sol")

    out = await ai_provider._run_codex_cli(
        [{"role": "user", "content": "hi"}], max_tokens=None, timeout=1.0,
    )
    assert out == "ok"
    args = captured["args"]
    assert "--ephemeral" not in args
    assert "exec" in args and "--skip-git-repo-check" in args
    assert args[args.index("--model") + 1] == "gpt-5.6-sol"


# ── 参数降级缓存 + 传输失败整体重试 ────────────────────────────────────
# 回归背景: 2026-10-09 模拟盘收盘复盘失败 —— kimi-k2.7-code 只接受 temperature=1,
# 每次复盘先吃一个 400 再重试; 另一次是长请求(65s)中途被上游断连, SDK 两次重试
# 用尽后整体失败, 面板显示「AI 服务连接失败」。
def _status_error(cls, status: int):
    resp = httpx.Response(
        status,
        json={"error": {"message": "x"}},
        request=httpx.Request("POST", "https://e.com/v1/chat/completions"),
    )
    return cls("e", response=resp, body={"error": {"message": "x"}})


def test_is_retryable_transport_excludes_param_400s():
    """400/401/404 不可整体重试; 429/5xx/连接类才可。

    关键: `_is_openai_transport_error` 对任何 openai.APIError 都是 True(含 400),
    直接拿它当重试判据会让参数类 400 白跑一遍, 所以必须用独立判据。
    """
    cases = [
        (400, openai.BadRequestError, False),
        (401, openai.AuthenticationError, False),
        (404, openai.NotFoundError, False),
        (429, openai.RateLimitError, True),
        (500, openai.InternalServerError, True),
        (503, openai.InternalServerError, True),
    ]
    for status, cls, expected in cases:
        exc = _status_error(cls, status)
        assert ai_provider._is_retryable_transport(exc) is expected, status
        # 对照: 旧判据对 400 也返回 True —— 正因如此不能复用
        assert ai_provider._is_openai_transport_error(exc) is True

    conn = openai.APIConnectionError(request=httpx.Request("POST", "https://e.com"))
    assert ai_provider._is_retryable_transport(conn) is True
    assert ai_provider._is_retryable_transport(ValueError("x")) is False


def test_rate_limit_429_retries_but_quota_429_does_not():
    """429 要分两种: 限流可重试; 余额/额度不足重试无意义(充值不是秒级动作)。

    回归: 2026-10-10 moonshot 余额耗尽返回 429 exceeded_current_quota_error,
    被当成"传输失败"整体重试 → 白跑一次请求, 且错误信息失真。
    """
    def _429(body: dict):
        resp = httpx.Response(
            429, json=body,
            request=httpx.Request("POST", "https://api.moonshot.cn/v1/chat/completions"),
        )
        return openai.RateLimitError("rate limited", response=resp, body=body)

    quota = _429({"error": {
        "message": "Your account org-x is suspended due to insufficient balance, "
                   "please recharge your account or check your plan and billing details",
        "type": "exceeded_current_quota_error",
    }})
    assert ai_provider._is_retryable_transport(quota) is False

    plain_limit = _429({"error": {"message": "rate limit exceeded, retry later",
                                  "type": "rate_limit_error"}})
    assert ai_provider._is_retryable_transport(plain_limit) is True


def test_rejected_param_cache_skips_repeat_400(monkeypatch):
    """识别出 temperature 被拒后, 后续同类调用不再发送该参数(省一次注定 400 的往返)。"""
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "kimi-k2.7-code")
    monkeypatch.setattr(
        secrets_store, "get_ai_config",
        lambda key, default=None: "https://api.moonshot.cn/v1" if key == "ai_base_url" else default,
    )

    resp = httpx.Response(
        400,
        json={"error": {"message": "invalid temperature: only 1 is allowed for this model"}},
        request=httpx.Request("POST", "https://api.moonshot.cn/v1/chat/completions"),
    )
    exc = openai.BadRequestError(
        "bad", response=resp,
        body={"error": {"message": "invalid temperature: only 1 is allowed for this model"}},
    )
    kwargs = {"max_tokens": 1000, "temperature": 0.2}
    assert ai_provider._openai_retry_kwargs(exc, kwargs) == {"max_tokens": 1000}
    assert kwargs == {"max_tokens": 1000, "temperature": 0.2}  # 入参不被改写

    # 同一模型 + 同一端点: 直接不再带 temperature
    same = ai_provider._openai_kwargs(
        temperature=0.2, max_tokens=1000,
        model="kimi-k2.7-code", base_url="https://api.moonshot.cn/v1",
    )
    assert "temperature" not in same and same["max_tokens"] == 1000
    assert ai_provider.rejected_params_snapshot()[
        "https://api.moonshot.cn/v1 kimi-k2.7-code"
    ] == ["temperature"]

    # 换模型/端点: 不被误伤, 仍带 temperature
    other = ai_provider._openai_kwargs(
        temperature=0.2, max_tokens=1000,
        model="deepseek-chat", base_url="https://api.deepseek.com/v1",
    )
    assert other["temperature"] == 0.2


def _fake_client(factory):
    """把 ai_provider._openai_client 换成返回伪客户端(chat.completions.create=factory)。"""
    class _Completions:
        async def create(self, **kwargs):
            return await factory(kwargs)

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_Completions())

    return _Client()


@pytest.mark.asyncio
async def test_run_openai_message_once_retries_transport_failure_once(monkeypatch):
    """长请求中途断连: 在 SDK 重试之外再整体重试一次(非流式无部分产出, 安全)。"""
    monkeypatch.setattr(ai_provider, "_TRANSPORT_RETRY_DELAY_S", 0)
    monkeypatch.setattr(secrets_store, "get_ai_key", lambda: "sk-test")
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "m1")
    attempts: list[dict] = []

    async def _create(kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://e.com"))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))]
        )

    monkeypatch.setattr(ai_provider, "_openai_client", lambda key, timeout: _fake_client(_create))

    msg = await ai_provider._run_openai_message_once(
        [{"role": "user", "content": "hi"}], temperature=None, max_tokens=None, timeout=30,
    )
    assert msg.content == "ok"
    assert len(attempts) == 2, "传输类失败应整体重试一次"


@pytest.mark.asyncio
async def test_run_openai_message_once_gives_up_after_bounded_retries(monkeypatch):
    """整体重试是有界的: 连续失败仍要抛出可读错误, 不能无限重试。

    client 的 max_retries=0（重试策略收归上层）, 所以这里须覆盖原先 SDK 的两次重试:
    共 1 次原始 + 2 次整体重试 = 3 次尝试。
    """
    monkeypatch.setattr(ai_provider, "_TRANSPORT_RETRY_DELAY_S", 0)
    monkeypatch.setattr(secrets_store, "get_ai_key", lambda: "sk-test")
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "m1")
    calls = []

    async def _create(kwargs):
        calls.append(1)
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://e.com"))

    monkeypatch.setattr(ai_provider, "_openai_client", lambda key, timeout: _fake_client(_create))

    with pytest.raises(RuntimeError, match="连接失败"):
        await ai_provider._run_openai_message_once(
            [{"role": "user", "content": "hi"}], temperature=None, max_tokens=None, timeout=30,
        )
    assert len(calls) == 1 + ai_provider._TRANSPORT_MAX_RETRIES, "原始 1 次 + 有界的整体重试"


@pytest.mark.asyncio
async def test_stream_openai_retries_when_nothing_yielded(monkeypatch):
    """流建立失败 → 未产出任何内容, 可以整体重试一次。"""
    monkeypatch.setattr(ai_provider, "_TRANSPORT_RETRY_DELAY_S", 0)
    monkeypatch.setattr(secrets_store, "get_ai_key", lambda: "sk-test")
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "m1")
    monkeypatch.setattr(
        secrets_store, "get_ai_config",
        lambda key, default=None: "https://e.com/v1" if key == "ai_base_url" else default,
    )
    calls = []

    async def _create(kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://e.com"))

        async def _gen():
            yield _stream_chunk(content="你好")
            yield _stream_chunk(finish_reason="stop")

        return _gen()

    monkeypatch.setattr(ai_provider, "_openai_client", lambda key, timeout: _fake_client(_create))

    pieces = [
        piece
        async for piece in ai_provider._stream_openai(
            [{"role": "user", "content": "hi"}],
            temperature=None, max_tokens=None, timeout=30, prefer_final_answer=False,
        )
    ]
    assert pieces == ["你好"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stream_openai_does_not_retry_after_partial_output(monkeypatch):
    """已产出内容后断流**不能**重试 —— 否则调用方会看到重复文本。"""
    monkeypatch.setattr(ai_provider, "_TRANSPORT_RETRY_DELAY_S", 0)
    monkeypatch.setattr(secrets_store, "get_ai_key", lambda: "sk-test")
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "m1")
    monkeypatch.setattr(
        secrets_store, "get_ai_config",
        lambda key, default=None: "https://e.com/v1" if key == "ai_base_url" else default,
    )
    calls = []

    async def _create(kwargs):
        calls.append(1)

        async def _gen():
            yield _stream_chunk(content="半截")
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://e.com"))

        return _gen()

    monkeypatch.setattr(ai_provider, "_openai_client", lambda key, timeout: _fake_client(_create))

    with pytest.raises(RuntimeError, match="连接失败"):
        async for _ in ai_provider._stream_openai(
            [{"role": "user", "content": "hi"}],
            temperature=None, max_tokens=None, timeout=30, prefer_final_answer=False,
        ):
            pass
    assert len(calls) == 1, "已产出内容时不得重试"



