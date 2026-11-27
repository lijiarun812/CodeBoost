"""
OpenAI 兼容客户端，默认走智谱 GLM-4-Flash（永久免费）。
环境变量:
  LLM_PROVIDER = zhipu | deepseek | openai | ollama
  以及对应的 API_KEY
"""
import os
from pathlib import Path
import requests
import time


class FreeQuotaExhausted(RuntimeError):
    """The provider has stopped calls at the free-tier boundary."""


def load_local_env():
    """Read the repo-local .env without replacing explicit shell settings."""
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and not name.startswith("#"):
            os.environ.setdefault(name, value.strip().strip('"').strip("'"))


load_local_env()

PROVIDERS = {
    "dashscope": {
        "base_url": os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "api_key_env": "DASHSCOPE_API_KEY",
        "model": os.environ.get("DASHSCOPE_MODEL", "qwen4-max"),
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "api_key_env": "ZHIPU_API_KEY",
        "model": "glm-4-flash",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "openai": {
        "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",  # ollama 不校验，随便填
        "model": "qwen2.5-coder:7b",
    },
}


def get_client():
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    cfg = PROVIDERS[provider]
    api_key = os.environ.get(cfg["api_key_env"], "")
    if not api_key:
        # Ollama 不校验 key；其他 provider 没 key 时直接报错
        if provider == "ollama":
            api_key = "ollama"
        else:
            raise RuntimeError(
                f"环境变量 {cfg['api_key_env']} 未设置。当前 provider={provider}。"
            )
    return cfg["base_url"], api_key, cfg["model"]


def chat(prompt: str, system: str = "You are an expert Python developer.",
         temperature: float = 0.2, max_tokens: int = 2048) -> str:
    return chat_with_meta(prompt, system=system, temperature=temperature,
                          max_tokens=max_tokens)["content"]


def chat_with_meta(prompt: str, system: str = "You are an expert Python developer.",
                   temperature: float = 0.2, max_tokens: int = 2048) -> dict:
    base_url, api_key, model = get_client()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7 if model == "qwen4-max" else temperature,
        "max_tokens": max_tokens,
    }
    if model == "qwen4-max":
        payload["enable_thinking"] = False
    started = time.perf_counter()
    response = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=90,
    )
    latency_s = time.perf_counter() - started
    if response.status_code == 403:
        try:
            error_code = response.json().get("code", "")
        except ValueError:
            error_code = ""
        if error_code == "AllocationQuota.FreeTierOnly" or "AllocationQuota.FreeTierOnly" in response.text:
            raise FreeQuotaExhausted("qwen4-max free quota exhausted; experiment stopped")
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"].get("content")
    if not content:
        raise RuntimeError("Model returned an empty message")
    usage = data.get("usage") or {}
    return {"content": content, "model": data.get("model", model),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "latency_s": round(latency_s, 4)}
