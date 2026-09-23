"""
Functional Acceptance Module — 56 pass/fail flags.

Runs a single combined functional acceptance suite — 10 "dimension" checks
and 46 extended checks — as one module. Every check is surfaced as its own
metric (1.0 = PASS, 0.0 = FAIL; SKIP is omitted → shown as "—" in the UI),
alongside summary counts and a `pass_rate`.

Request-parameter conversion: the checks were written against an LLM gateway
whose official top-level `thinking={"type":"disabled"}` switch is NOT honoured
by a bare vLLM / SGLang backend. `backend_style="auto"` (default) detects bare
engine vs gateway; when it resolves to "direct" all thinking switches are
converted to the chat_template_kwargs form the bare backend understands (Kimi:
`thinking`, GLM: `enable_thinking`). Set `backend_style="gateway"` to force the
official top-level format (and strict gateway expectations), or `"direct"` to
force the bare-engine conversion.

Score and pass/fail are NOT computed here — the evaluator uses the benchmark's
MetricConfig rules.

Default evaluation (default_metric_configs):
  Score:   pass_rate (passthrough, [0, 1] over executed checks)
  Redline: tests_failed ≤ 0  (acceptance: any failed check fails the module)
           + every individual check is its own redline (PASS=1.0 required;
             SKIP omitted → ignored), so each of the 10+46 checks is a hard
             pass/fail gate by default.
"""
from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel, Field

from bench.modules.base import (
    EndpointConfig, MetricConfig, MetricDescriptor,
    ModuleResult, ProgressCallback, TestModule, _noop_progress,
)
from bench.tests.functional.functional_acceptance import TEST_CATALOG
from utils.logger import logger

# Summary metrics emitted in addition to the per-test flags.
_SUMMARY_DESCRIPTORS = [
    MetricDescriptor("pass_rate",      "Pass Rate",     "%", "Passed / executed checks (skips excluded)", higher_is_better=True),
    MetricDescriptor("tests_total",    "Total Checks",  "",  "Number of checks recorded",                 higher_is_better=True),
    MetricDescriptor("tests_executed", "Executed",      "",  "Checks that ran (total − skipped)",         higher_is_better=True),
    MetricDescriptor("tests_passed",   "Passed",        "",  "Checks that passed",                        higher_is_better=True),
    MetricDescriptor("tests_failed",   "Failed",        "",  "Checks that failed",                        higher_is_better=False),
    MetricDescriptor("tests_skipped",  "Skipped",       "",  "Checks that were skipped",                  higher_is_better=False),
]


class FunctionalAcceptanceParams(BaseModel):
    backend_style: Literal["auto", "direct", "gateway"] = Field(
        default="auto",
        description="'auto' (default) = best-effort detect bare engine vs gateway "
                    "(engine-native endpoints / response fields); 'direct' = bare "
                    "vLLM/SGLang (thinking switches converted to chat_template_kwargs); "
                    "'gateway' = the gateway (official top-level thinking={type:...} "
                    "format). The resolved backend also drives a few backend-specific "
                    "checks: under 'gateway' top_p=0.0 and auth are asserted strictly "
                    "(catches a misconfigured gateway); under 'direct' they are "
                    "relaxed/skipped (bare-engine behaviour is spec-correct).",
    )
    request_timeout: float = Field(default=120.0, ge=1.0, le=86400.0, description="Per-request HTTP timeout in seconds")
    max_context_tokens: int = Field(
        default=131072, ge=1024, le=10_000_000,
        description="Model context limit. The max_tokens boundary checks (T3) use this: "
                    "max_tokens=limit is expected OK, limit+1 is expected to 4xx.",
    )
    cache_min_prompt_tokens: int = Field(
        default=2048, ge=64, le=10_000_000,
        description="Prompt cache check (D6): lower bound in tokens for the cache-priming "
                    "prompt. Prefix/radix caches only store full blocks of page_size × "
                    "dcp_world_size tokens (e.g. Kimi K3: page 64 × DCP 8 → 512-token "
                    "blocks), so a shorter prompt can never hit the cache and would fail "
                    "a healthy endpoint. Set this to at least the largest cache block the "
                    "tested deployments use; the check overshoots the bound by ~30-50% "
                    "and clamps the prompt to fit max_context_tokens.",
    )
    thinking_off_fields: str = Field(
        default="",
        description="Request fields (JSON object) that disable thinking for this "
                    "model, e.g. {\"chat_template_kwargs\":{\"thinking\":false}}. "
                    "The thinking-ON form is derived automatically (false→true, "
                    "'disabled'→'enabled'). Blank (default) = automatic: known "
                    "families (Kimi/GLM/Qwen) use their verified switch, and any "
                    "other family is probed at run start — each candidate spelling "
                    "is tried alone with a tiny greedy request and the one that "
                    "empirically stops reasoning wins. Set this only for a new "
                    "model family whose switch the probe doesn't know; malformed "
                    "JSON is ignored with a warning (auto behavior).",
    )
    multimodal: Literal["auto", "on", "off"] = Field(
        default="auto",
        description="Vision checks (D5/D8/T13): 'auto' = probe the endpoint with a tiny "
                    "image and enable the checks only if it accepts image input "
                    "(2xx + content); else skip. 'on'/'off' force.",
    )
    tool_choice_mode: Literal["auto", "named"] = Field(
        default="auto",
        description="Tool-call checks (D3/T5): 'auto' (default) sends tool_choice='auto' "
                    "and lets the model decide — the common real-world usage, and robust "
                    "against backends whose forced-choice path is broken (e.g. vLLM 0.21 "
                    "+ reasoning parser returns empty tool_calls for named/required). "
                    "'named' forces the function via OpenAI tool_choice={type:function,...} "
                    "— stricter: additionally verifies the server honours forced tool "
                    "choice, which agent SDKs with strict tool pipelines rely on.",
    )
    run_dimensions: bool = Field(default=True, description="Run the 10 dimension checks (d01..d10)")
    run_extended: bool = Field(default=True, description="Run the 46 extended checks (t1a..t16c)")
    # `skip_tests` stays typed `list[str]` (the runner warns+ignores unknown keys, so a
    # saved benchmark survives a catalog rename) but advertises the full catalog as an
    # `items.enum` + `x-enum-labels` map, so the admin benchmark form renders it as a
    # labelled checkbox list of every check rather than a free-text box.
    skip_tests: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "items": {
                "type": "string",
                "enum": [k for k, _ in TEST_CATALOG],
                "x-enum-labels": {k: d for k, d in TEST_CATALOG},
            },
        },
        description="Checks to skip when the endpoint does not support the feature they "
                    "exercise (e.g. n>1, json_schema, tool calling). Each ticked check is "
                    "recorded as SKIP (— in the UI) with a warning in the logs instead of "
                    "running and failing: it is excluded from pass_rate and its per-check "
                    "redline is ignored, so it cannot fail the module. Unknown keys are "
                    "warned about and ignored.",
    )


def _build_descriptors():
    descriptors = list(_SUMMARY_DESCRIPTORS)
    for key, display in TEST_CATALOG:
        descriptors.append(MetricDescriptor(key, display, "", display, higher_is_better=True))
    return descriptors


def _build_metric_configs():
    configs = [
        MetricConfig("pass_rate",     role="score",   weight=1.0, formula="passthrough"),
        MetricConfig("tests_failed",  role="redline", max_val=0.0),
        MetricConfig("pass_rate",     role="display"),
        MetricConfig("tests_total",   role="display"),
        MetricConfig("tests_executed", role="display"),
        MetricConfig("tests_passed",  role="display"),
        MetricConfig("tests_failed",  role="display"),
        MetricConfig("tests_skipped", role="display"),
    ]
    # Every individual check is its own redline: PASS (1.0) is required to pass,
    # FAIL (0.0) trips the redline, and SKIP (metric omitted → None) is ignored
    # by the evaluator. This makes each of the 10+46 checks a hard pass/fail gate
    # by default, in addition to the tests_failed summary redline above.
    for key, _display in TEST_CATALOG:
        configs.append(MetricConfig(key, role="redline", min_val=1.0))
    return configs


class FunctionalAcceptanceModule(TestModule):
    """Combined 56-flag functional acceptance suite."""

    name = "functional_acceptance"
    display_name = "Functional Acceptance (56 checks)"
    description = (
        "Runs 56 functional pass/fail checks against an OpenAI-compatible endpoint: "
        "10 dimension checks (basic/stream/tool-call/reasoning/multimodal/cache/"
        "thinking-toggle) + 46 extended checks (thinking switch, sampling-param "
        "boundaries, max_tokens limits, system prompt, function calling, multi-turn, "
        "SSE, JSON output, stop word, auth, multilingual, multimodal, idempotency, "
        "request validation). Each check is a metric (1=PASS, 0=FAIL, —=SKIP). "
        "Thinking params are converted for bare vLLM/SGLang by default. "
        "Default score = pass_rate; every individual check is its own redline "
        "(PASS required, SKIP ignored)."
    )
    ParamsSchema = FunctionalAcceptanceParams

    metrics_descriptors = _build_descriptors()
    default_metric_configs = _build_metric_configs()

    def run(
        self,
        endpoint: EndpointConfig,
        params: BaseModel,
        output_dir: str,
        progress_cb: ProgressCallback = _noop_progress,
        cancel_event=None,
    ) -> ModuleResult:
        p: FunctionalAcceptanceParams = params  # type: ignore[assignment]
        os.makedirs(output_dir, exist_ok=True)

        progress_cb(0.02, f"Starting functional acceptance (backend_style={p.backend_style})")
        try:
            from bench.tests.functional.functional_acceptance import FunctionalAcceptanceTest

            test = FunctionalAcceptanceTest(
                api_url=endpoint.api_url,
                model=endpoint.model,
                api_key=endpoint.api_key,
                backend_style=p.backend_style,
                request_timeout=p.request_timeout,
                max_context_tokens=p.max_context_tokens,
                cache_min_prompt_tokens=p.cache_min_prompt_tokens,
                thinking_off_fields=p.thinking_off_fields,
                multimodal=p.multimodal,
                tool_choice_mode=p.tool_choice_mode,
                run_dimensions=p.run_dimensions,
                run_extended=p.run_extended,
                skip_tests=p.skip_tests,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            outcome = test.run()
        except Exception:
            from traceback import format_exc
            err = format_exc()
            logger.error("[functional_acceptance] suite crashed: %s", err)
            return ModuleResult(error=err)

        metrics = outcome["metrics"]
        results = outcome["results"]

        # Persist full per-check detail (status + human-readable detail strings).
        artifacts = []
        try:
            path = os.path.join(output_dir, "functional_results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            artifacts.append(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[functional_acceptance] could not write results artifact: %s", exc)

        failed = [r["key"] for r in results if r["status"] == "FAIL"]
        progress_cb(0.99, f"Done — {metrics['tests_passed']} PASS / {metrics['tests_failed']} FAIL / "
                          f"{metrics['tests_skipped']} SKIP")
        logger.info("[functional_acceptance] pass_rate=%.3f passed=%d failed=%d skipped=%d failed_keys=%s",
                    metrics["pass_rate"], metrics["tests_passed"], metrics["tests_failed"],
                    metrics["tests_skipped"], failed)
        return ModuleResult(metrics=metrics, artifacts=artifacts)
