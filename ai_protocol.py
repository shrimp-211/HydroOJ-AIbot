"""Provider-independent request policy and token accounting for chat completions."""
from copy import deepcopy
from urllib.parse import urlparse


def provider_for(config: dict) -> str:
    explicit = config.get("provider", "auto")
    if explicit != "auto":
        return explicit
    host = urlparse(config["base_url"]).hostname or ""
    return {"api.openai.com": "openai", "api.deepseek.com": "deepseek",
            "open.bigmodel.cn": "zhipu"}.get(host, "compatible")


def build_request(model: str, messages: list, config: dict, *,
                  max_tokens: int = 0, effort: str = "") -> dict:
    """Send provider-specific options only when configured or recognized."""
    provider = provider_for(config)
    token_param = config.get("token_parameter") or (
        "max_completion_tokens" if provider == "openai" else "max_tokens")
    if token_param not in ("max_tokens", "max_completion_tokens"):
        raise ValueError("token_parameter must be max_tokens or max_completion_tokens")
    limit = int(config["max_tokens"])
    if max_tokens:
        limit = min(limit, int(max_tokens))
    if limit <= 0:
        raise ValueError("max_tokens must be positive")
    args = {"model": model, "messages": deepcopy(messages), token_param: limit}
    reasoning = str(effort or config.get("reasoning_effort") or "").split(",")[0].strip()
    if config.get("supports_reasoning_effort", provider in ("openai", "deepseek")) and reasoning:
        args["reasoning_effort"] = reasoning
    extra = deepcopy(config.get("extra_body") or {})
    thinking = config.get("thinking")
    if thinking is None and provider in ("deepseek", "zhipu"):
        thinking = "enabled"
    if thinking in ("enabled", "disabled"):
        extra["thinking"] = {"type": thinking}
    if extra:
        if set(extra) & {"model", "messages", "stream", "max_tokens", "max_completion_tokens"}:
            raise ValueError("extra_body cannot override routing or token limits")
        args["extra_body"] = extra
    return args


def extract_usage(obj) -> dict:
    """Input includes cached tokens; cache creation is not a cache hit."""
    if obj is None:
        return {}
    def number(*paths):
        for path in paths:
            value = obj
            for key in path.split("."):
                value = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
        return 0
    inp = number("prompt_tokens", "input_tokens")
    out = number("completion_tokens", "output_tokens")
    cached = number("prompt_cache_hit_tokens", "prompt_tokens_details.cached_tokens",
                    "input_tokens_details.cached_tokens", "cache_read_input_tokens")
    return {"input": inp, "output": out, "total": number("total_tokens") or inp + out,
            "cache_hit": min(cached, inp)}
