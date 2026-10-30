"""DeepSeek 官方 API 调用（OpenAI 兼容格式），开启 strict 工具模式。"""
from __future__ import annotations

import copy
import time
from typing import Any

import requests

from . import config


class LLMError(RuntimeError):
    """模型调用失败（网络 / 配额 / Key 缺失）。"""


# strict 模式的真实生效情况：账号不支持时会被自动降级，预检脚本据此提示
STRICT_STATUS: dict[str, Any] = {"downgraded": False, "reason": ""}


def _strip_strict(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """账号不支持 strict 时自动降级为普通 tools 模式。"""
    cloned = copy.deepcopy(tools)
    for tool in cloned:
        tool.get("function", {}).pop("strict", None)
    return cloned


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 (assistant message, usage)。失败重试 2 次，最终抛 LLMError。"""
    if not config.DEEPSEEK_API_KEY:
        raise LLMError("DEEPSEEK_API_KEY 未配置：请在项目根目录 .env 中填写后重试")

    url = f"{config.DEEPSEEK_BASE_URL}/chat/completions"
    payload: dict[str, Any] = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,  # 规划任务要稳
        "max_tokens": max_tokens or config.MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_error: Exception | None = None
    for attempt in range(config.LLM_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=config.LLM_TIMEOUT,
            )
            if resp.status_code == 400 and attempt == 0 and "tools" in payload:
                detail = resp.text or ""
                if "strict" in detail and config.STRICT_TOOLS:
                    payload["tools"] = _strip_strict(tools or [])
                    STRICT_STATUS["downgraded"] = True
                    STRICT_STATUS["reason"] = detail[:200]
                    continue
                raise LLMError(f"DeepSeek 参数错误：{detail[:300]}")
            if resp.status_code in (401, 403):
                raise LLMError("DeepSeek 鉴权失败，请检查 DEEPSEEK_API_KEY")
            if resp.status_code == 402:
                raise LLMError("DeepSeek 账户余额不足，请充值后重试")
            if resp.status_code == 429 or resp.status_code >= 500:
                # 服务端抖动/限流：指数退避重试，别把外部故障算成规划失败
                last_error = LLMError(f"DeepSeek 服务端暂时不可用（HTTP {resp.status_code}）")
                if attempt < config.LLM_RETRIES:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise last_error
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("DeepSeek 返回内容为空")
            return choices[0].get("message") or {}, {
                **(data.get("usage") or {}),
                "finish_reason": choices[0].get("finish_reason") or "",
            }
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < config.LLM_RETRIES:
                time.sleep(2 ** (attempt + 1))
    raise LLMError(f"DeepSeek 调用失败：{last_error}")
