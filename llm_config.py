"""AI 润色的通用配置层（OpenAI 兼容接口）。

支持三种使用方式，优先级从高到低：
1. 调用方显式传入的 override（UI 上填写的值）；
2. 通用变量 LLM_PROVIDER / LLM_API_KEY / LLM_BASE_URL / LLM_MODEL；
3. 旧变量 MINIMAX_APIKEY / MINIMAX_API_BASE / MINIMAX_MODEL（保持向后兼容）。

本模块只依赖标准库与 openai SDK，不引用 whisper，方便单元测试。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

PROVIDER_PRESETS = {
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.7-flash",
    },
    "minimax": {
        "label": "MiniMax",
        "base_url": "https://api.minimax.cn/v1",
        "model": "MiniMax-M3",
    },
    "custom": {
        "label": "自定义兼容接口",
        "base_url": "",
        "model": "",
    },
}

# 这些模型默认开启思考模式，会吃光输出预算，需要显式关闭
THINKING_MODELS = ("glm-4.7", "glm-4.6", "glm-4.5", "glm-z1", "glm-5")


def mask_key(api_key: str) -> str:
    """日志/界面里展示密钥时使用，永不明文输出。"""
    if not api_key:
        return "(未配置)"
    key = api_key.strip()
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}***{key[-4:]}"


def _guess_provider(base_url: str, model: str) -> str:
    text = f"{base_url} {model}".lower()
    if "bigmodel.cn" in text or "glm" in text:
        return "zhipu"
    if "minimax" in text:
        return "minimax"
    return "custom"


@dataclass
class LLMSettings:
    provider: str = "custom"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    source: str = "none"

    @property
    def provider_label(self) -> str:
        return PROVIDER_PRESETS.get(self.provider, {}).get("label", self.provider)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def masked_key(self) -> str:
        return mask_key(self.api_key)

    @property
    def supports_thinking_switch(self) -> bool:
        name = (self.model or "").lower()
        return any(prefix in name for prefix in THINKING_MODELS)

    def summary(self) -> str:
        if not self.configured:
            return "AI 润色未配置（仍可正常生成原始转写）"
        return f"{self.provider_label} / {self.model} / 密钥 {self.masked_key}"


def load_settings(override: Optional[dict] = None) -> LLMSettings:
    """读取当前生效的 AI 配置，兼容旧的 MINIMAX_* 变量名。"""
    override = override or {}

    def pick(key: str, env_names: tuple[str, ...], default: str = "") -> str:
        if override.get(key):
            return str(override[key]).strip()
        for name in env_names:
            value = os.getenv(name)
            if value and value.strip():
                return value.strip()
        return default

    api_key = pick("api_key", ("LLM_API_KEY", "MINIMAX_APIKEY", "MINIMAX_API_KEY"))
    base_url = pick("base_url", ("LLM_BASE_URL", "MINIMAX_API_BASE"))
    model = pick("model", ("LLM_MODEL", "MINIMAX_MODEL"))

    provider = (override.get("provider") or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not provider or provider not in PROVIDER_PRESETS:
        provider = _guess_provider(base_url, model)

    preset = PROVIDER_PRESETS.get(provider, {})
    if not base_url and preset.get("base_url"):
        base_url = preset["base_url"]
    if not model and preset.get("model"):
        model = preset["model"]

    if override.get("api_key"):
        source = "ui"
    elif os.getenv("LLM_API_KEY"):
        source = "llm_env"
    elif os.getenv("MINIMAX_APIKEY") or os.getenv("MINIMAX_API_KEY"):
        source = "legacy_env"
    else:
        source = "none"

    return LLMSettings(
        provider=provider,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        source=source,
    )


def build_client(settings: LLMSettings):
    """构造 OpenAI 兼容客户端；未配置密钥时返回 None。"""
    if not settings.api_key:
        return None
    from openai import OpenAI

    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


def request_kwargs(settings: LLMSettings) -> dict:
    kwargs = {}
    if settings.supports_thinking_switch:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return kwargs


def classify_error(error: Exception) -> tuple[str, str]:
    """把接口异常归类成可读的中文提示，便于 UI 直接展示。"""
    status = getattr(error, "status_code", None)
    text = str(error)
    lowered = text.lower()

    if status == 401 or "invalid api key" in lowered or "身份验证失败" in text:
        return "auth", "密钥无效或已过期，请重新填写 API Key。"
    if status == 403 or "无权访问" in text or "permission" in lowered:
        return "permission", "该密钥没有访问此模型的权限。"
    if status == 404 or "模型不存在" in text or "model not found" in lowered or "1211" in text:
        return "model", "模型 ID 不存在，请检查模型名称是否正确。"
    if status == 429:
        if "1113" in text or "欠费" in text or "余额" in text:
            return "quota", "账户余额不足或额度已用完。"
        if "1305" in text:
            return "overload", "模型当前访问量过大（平台过载），请稍后重试。"
        return "rate_limit", "触发频率限制，请降低请求频率后重试。"
    if status and 500 <= int(status) < 600:
        return "server", "服务端错误，请稍后重试。"
    if isinstance(error, (TimeoutError,)) or "timed out" in lowered or "timeout" in lowered:
        return "network", "网络超时，请检查网络或代理设置。"
    if "connection" in lowered or "ssl" in lowered or "proxy" in lowered:
        return "network", "无法连接接口地址，请检查网络或 Base URL 是否正确。"
    return "unknown", f"调用失败：{text[:200]}"


def test_connection(settings: Optional[LLMSettings] = None, *, timeout: float = 30.0) -> dict:
    """发起一次极短的请求验证连通性、密钥与模型权限。

    返回 dict：ok / category / message / model / latency / provider。
    """
    settings = settings or load_settings()
    result = {
        "ok": False,
        "category": "unconfigured",
        "message": "",
        "model": settings.model,
        "provider": settings.provider_label,
        "latency": None,
    }
    if not settings.api_key:
        result["message"] = "未配置 API Key。"
        return result
    if not settings.base_url:
        result["message"] = "未配置 Base URL。"
        result["category"] = "config"
        return result
    if not settings.model:
        result["message"] = "未配置模型 ID。"
        result["category"] = "config"
        return result

    client = build_client(settings)
    started = time.time()
    try:
        response = client.chat.completions.create(
            model=settings.model,
            messages=[{"role": "user", "content": "只回复两个字：正常"}],
            max_tokens=16,
            temperature=0.0,
            timeout=timeout,
            **request_kwargs(settings),
        )
        latency = time.time() - started
        content = ""
        if response.choices:
            content = (response.choices[0].message.content or "").strip()
        served = getattr(response, "model", None) or settings.model
        result.update(
            ok=True,
            category="ok",
            model=served,
            latency=round(latency, 2),
            message=f"连接正常（{latency:.1f}s，模型 {served}）",
        )
        if not content:
            result["message"] += "；返回内容为空，可能开启了思考模式"
        return result
    except Exception as error:  # noqa: BLE001 - 统一归类后交给 UI
        category, message = classify_error(error)
        result.update(ok=False, category=category, message=message)
        return result
