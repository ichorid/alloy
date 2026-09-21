"""Normalize the usage fields reported by the different harnesses."""


def normalize(raw: dict) -> dict:
    raw = raw or {}

    def first(*keys: str):
        return next((raw[key] for key in keys if raw.get(key) is not None), None)

    input_tokens = first("input_tokens", "inputTokens", "prompt_tokens")
    output_tokens = first("output_tokens", "outputTokens", "completion_tokens")
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    else:
        # A lone count (including the Pi harness's input_tokens) is an
        # ambiguous total; it does not report an input/output split.
        total_tokens = first("total_tokens", "totalTokens")
        if total_tokens is None:
            total_tokens = input_tokens if input_tokens is not None else output_tokens
        input_tokens = output_tokens = None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": raw.get("total_cost_usd"),
    }
