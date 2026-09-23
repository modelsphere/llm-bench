"""Build guidellm's entrypoint arguments from the platform's BenchConfig.

Targets the post-v0.7 guidellm API (`BenchmarkScenario` with a nested `spec`),
which replaced the flat `BenchmarkGenerativeTextArgs`. The old builder's
runtime field-presence probing is gone: the pinned guidellm is a single known
version, so the mapping is written out explicitly and a schema change should
fail loudly at validation rather than silently drop a setting.

Mapping from the old flat args:
    target/model/api_key/backend_kwargs -> spec.backend    (type -> kind)
    profile + rate                      -> spec.profile    (rate -> streams)
    max_seconds                         -> spec.constraints[max_duration]
    max_requests / per-level counts     -> spec.constraints[max_requests]
    prompt_tokens/output_tokens         -> spec.data[synthetic_text]
    processor                           -> spec.tokenizer
    sample_requests                     -> spec.metrics.sample_size
    output_path + output_formats        -> spec.outputs
    random_seed                         -> spec.seed
    warmup/cooldown/rampup              -> inside spec.profile (were top-level)
"""
import os
import re
from typing import Any, Dict, List

from guidellm.benchmark.schemas import BenchmarkScenario

from utils.api import to_base_url
from utils.bench_config import BenchConfig
from utils.logger import logger

# Profiles that take an explicit per-level load list, and the field it goes in.
# "concurrent" takes integer stream counts (closed-loop, N in flight); the
# arrival-rate profiles take floats (open-loop, N requests/sec).
_PROFILE_LOAD_FIELD = {
    "concurrent": "streams",
    "constant": "rate",
    "poisson": "rate",
}


def _build_data_spec(cfg: BenchConfig) -> List[Dict[str, Any]]:
    """Build the synthetic_text data spec from the token configuration.

    File-backed datasets (ShareGPT) are deliberately unsupported: upstream
    replaced the old "pass a path" form with a data-kind + column-mapper
    pipeline that this deployment does not use. Failing loudly beats silently
    substituting random tokens, which would change what a stored benchmark
    measures without changing its configuration.
    """
    if cfg.dataset_name and cfg.dataset_name != "random":
        raise ValueError(
            f"file-backed datasets are no longer supported (got "
            f"{cfg.dataset_name!r}). Clear the benchmark's dataset_path to use "
            f"synthetic random tokens."
        )

    data: Dict[str, Any] = {"kind": "synthetic_text"}

    if cfg.prompt_tokens_range:
        lo, hi = cfg.prompt_tokens_range
        data.update(prompt_tokens=(lo + hi) // 2,
                    prompt_tokens_min=lo, prompt_tokens_max=hi)
    else:
        data["prompt_tokens"] = cfg.prompt_tokens

    if cfg.output_tokens_range:
        lo, hi = cfg.output_tokens_range
        data.update(output_tokens=(lo + hi) // 2,
                    output_tokens_min=lo, output_tokens_max=hi)
    else:
        data["output_tokens"] = cfg.output_tokens

    return [data]


def _build_backend_spec(cfg: BenchConfig) -> Dict[str, Any]:
    base_url = to_base_url(cfg.api_url)

    # Normalise the key here too, so the CLI path gets the same protection as
    # the platform's schema-level trim. A whitespace-only key is truthy, so
    # guidellm emits `Authorization: Bearer  ` and h11 rejects it as an illegal
    # header value — every request then fails at construction, before any bytes
    # are sent, and the run reports zero successful requests. "" is falsy all
    # the way down (SecretStr("") included), so it takes the keyless path.
    api_key = (cfg.api_key or "").strip()

    backend: Dict[str, Any] = {
        "kind": "openai_http",
        "target": base_url,
        "model": cfg.model,
        "api_key": api_key,
        "timeout": cfg.timeout,
        "validate_backend": False,
        # http2=False forces HTTP/1.1 for the load generator. With HTTP/2, httpx
        # multiplexes all concurrent requests onto a single connection whose
        # server cap is SETTINGS_MAX_CONCURRENT_STREAMS (16 for api.moonshot.cn);
        # the excess fails client-side with `LocalProtocolError: Max outbound
        # streams is 16, 16 open` and silently tanks uptime. HTTP/1.1 gives each
        # concurrent request its own connection (bounded by the pool's
        # max_connections, unset/unlimited here).
        "http2": False,
    }

    # guidellm hardcodes the OpenAI "v1/<path>" route onto the target and only
    # strips a trailing "/v1" from it. For a provider on a different API version
    # (e.g. Zhipu's https://open.bigmodel.cn/api/paas/v4), to_base_url correctly
    # keeps the "/v4", but guidellm would still append "v1/chat/completions" ->
    # ".../v4/v1/chat/completions" (404). When the base ends in a non-v1 version
    # segment, override the api_routes so it appends the bare path and the
    # version we kept is honoured. v1 and version-less bases keep the defaults.
    version = re.search(r"/v(\d+)$", base_url)
    if version and version.group(1) != "1":
        backend["api_routes"] = {
            "/v1/chat/completions": "chat/completions",
            "/v1/completions": "completions",
            "/v1/models": "models",
        }

    return backend


def _build_profile_spec(cfg: BenchConfig) -> Dict[str, Any]:
    """Build the profile spec. Warmup/cooldown/rampup live here now (they were
    top-level args before); both still accept a float (fraction when < 1.0,
    absolute otherwise) or a TransientPhaseConfig dict."""
    profile: Dict[str, Any] = {"kind": cfg.profile}

    load_field = _PROFILE_LOAD_FIELD.get(cfg.profile)
    if load_field == "streams":
        # Closed-loop concurrency levels are integers upstream.
        profile["streams"] = [int(round(r)) for r in cfg.rate]
    elif load_field == "rate":
        profile["rate"] = [float(r) for r in cfg.rate]
    # synchronous/throughput/sweep take no load list.

    if cfg.warmup is not None:
        profile["warmup"] = cfg.warmup
    if cfg.cooldown is not None:
        profile["cooldown"] = cfg.cooldown
    if cfg.rampup:
        profile["rampup_duration"] = cfg.rampup

    return profile


def _build_constraints_spec(cfg: BenchConfig) -> List[Dict[str, Any]]:
    """Build the constraint list. Duration and request-count constraints
    compose: a level stops at whichever fires first.

    `max_requests_per_level` becomes a list-valued count, one entry per load
    level, consumed in order (guidellm advances the constraint's index once per
    strategy). This per-strategy indexing is broken in guidellm before the
    #786/#877 constraints refactor — see
    docs/plans/guidellm-upstream-migration.md.
    """
    constraints: List[Dict[str, Any]] = []

    if cfg.max_seconds:
        constraints.append({"kind": "max_duration", "seconds": cfg.max_seconds})

    per_level = getattr(cfg, "max_requests_per_level", None)
    if per_level:
        if len(per_level) != len(cfg.rate):
            raise ValueError(
                f"max_requests_per_level has {len(per_level)} entries for "
                f"{len(cfg.rate)} load levels — they must match 1:1"
            )
        constraints.append({"kind": "max_requests",
                            "count": [int(n) for n in per_level]})
    elif cfg.max_requests:
        constraints.append({"kind": "max_requests", "count": int(cfg.max_requests)})

    return constraints


_REPO_ID = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")


def _tokenizer_ref(processor_path: str, model: str) -> str:
    if processor_path and os.path.isdir(processor_path):
        return processor_path
    if processor_path and _REPO_ID.match(processor_path):
        return processor_path
    if processor_path:
        logger.warning(
            "Processor %r is neither a local directory nor a Hugging Face repo "
            "id — falling back to the served model name %r as the tokenizer",
            processor_path, model,
        )
    return model


def build_guidellm_args(cfg: BenchConfig, processor_path: str) -> BenchmarkScenario:
    """Construct the BenchmarkScenario guidellm's entrypoint consumes."""
    spec: Dict[str, Any] = {
        "backend": _build_backend_spec(cfg),
        "profile": _build_profile_spec(cfg),
        "data": _build_data_spec(cfg),
        "seed": {"kind": "static", "value": cfg.random_seed},
    }

    constraints = _build_constraints_spec(cfg)
    if constraints:
        spec["constraints"] = constraints

    # The tokenizer: an existing local directory, or a Hugging Face repo id
    # (`org/name`) that transformers downloads into HF_HOME. A path that does
    # not exist is neither, and falls back to the served model name — which
    # only works when that name happens to be a repo id too, hence the warning.
    spec["tokenizer"] = {"kind": "huggingface_auto", "model": _tokenizer_ref(processor_path, cfg.model)}

    if cfg.sample_requests is not None:
        spec["metrics"] = {"kind": "generative", "sample_size": cfg.sample_requests}

    if cfg.guidellm_output_dir:
        # JSON only. The HTML report is not generated: the platform never reads
        # it, and rendering it pulls a remote template mid-run (a known source
        # of whole-run failures when raw.githubusercontent.com is unreachable).
        spec["outputs"] = [{
            "kind": "json",
            "path": os.path.join(cfg.guidellm_output_dir, "benchmarks.json"),
        }]
    else:
        logger.warning(
            "No guidellm_output_dir set — guidellm will write its report to its "
            "default location (cwd)."
        )

    logger.info("Data spec: %s", spec["data"])
    return BenchmarkScenario.create(scenario=None, spec=spec)
