import requests

from utils.logger import logger


# OpenAI-compatible endpoint suffixes recognised when normalising user-supplied URLs.
_KNOWN_PATHS = ("chat/completions", "completions", "models", "messages")


def join_endpoint(api_url: str, path: str) -> str:
    """Build the full endpoint URL for `path` from a user-supplied api_url.

    Works whether the user pasted a bare base (``http://host:8000`` or
    ``.../v1``) or a full endpoint URL for *any* OpenAI-compatible route
    (``.../v1/chat/completions``, ``.../api/paas/v4/chat/completions``). Any
    recognised trailing endpoint suffix is stripped first (via ``to_base_url``)
    so the version prefix the user supplied is preserved and the requested
    `path` is swapped in — e.g. asking for ``models`` after the user pasted
    ``.../v1/chat/completions`` yields ``.../v1/models``, not the doubled-up
    ``.../v1/chat/completions/v1/models``.
    """
    url = api_url.rstrip("/")
    path = path.strip("/")
    base = to_base_url(url)
    if base != url:
        # url ended in a recognised endpoint suffix; `base` already carries the
        # correct version prefix (.../v1, .../api/paas/v4). Swap in the new path.
        return f"{base}/{path}"
    # No recognised suffix — treat url as a bare base and assume the standard
    # OpenAI /v1 layout, collapsing a trailing /v1 so we never double it.
    if url.endswith("/v1"):
        url = url[:-3]
    return f"{url}/v1/{path}"


def to_base_url(api_url: str) -> str:
    """Strip a known endpoint suffix so callers that need the bare base
    (e.g. guidellm's `target=`) work for both base and full-path inputs."""
    url = api_url.rstrip("/")
    for p in _KNOWN_PATHS:
        suffix = "/" + p
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def test_api_service(api_url: str, model: str, api_key: str, timeout: float = 30) -> bool:
    """Test whether the API service is reachable and returns a successful response."""
    test_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个有帮助的助手"},
            {"role": "user", "content": "你好！请用一句话介绍你自己背后的模型。"},
        ],
        "temperature": 0.7,
        "max_tokens": 128,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        url = join_endpoint(api_url, "chat/completions")
        logger.info("正在测试API服务:%s", url)
        response = requests.post(url=url, json=test_payload, headers=headers, timeout=timeout)
        if response.status_code == 200:
            logger.info("API服务测试成功,响应：%s", response.json())
            return True
        logger.error(
            "API服务测试失败,状态码：%s,响应：%s",
            response.status_code,
            response.text,
        )
        return False
    except Exception as e:
        logger.error("API服务测试异常: %s", str(e))
        return False
