# Functional Acceptance — the 10 + 46 checks

The `functional_acceptance` module runs **56 pass/fail checks** against an
OpenAI-compatible endpoint: **10 dimension checks** plus **46 extended
checks**. Each check yields **PASS / FAIL / SKIP** and is
surfaced as its own metric (`1.0` = PASS, `0.0` = FAIL; SKIP is omitted so the
UI renders "—").

- **Score** = `pass_rate` (passed / executed, skips excluded).
- **Redline** = every individual check is its own pass/fail redline
  (`min_val = 1.0`), plus a `tests_failed ≤ 0` summary redline. A SKIP omits
  the metric, so the evaluator ignores it — a skipped check never fails the
  module.

> **Ground truth is the code, not this table.** The exact prompts, thresholds,
> and pass logic live in
> [`bench/tests/functional/functional_acceptance.py`](../bench/tests/functional/functional_acceptance.py)
> (`TEST_CATALOG` + the `_run_dimensions` / `_run_extended` methods). This doc
> is a derived summary; re-verify against the source before relying on a
> specific criterion.

## Request-parameter conversion (thinking switches)

The checks were written against an **LLM gateway**, whose official top-level
`thinking={"type":"disabled"}` switch is **not** honoured by a bare
vLLM / SGLang backend. The `backend_style` param controls conversion. It
defaults to **`auto`**, which detects bare engine vs gateway on a best-effort
basis (engine-native endpoints like SGLang `/get_model_info` / vLLM `/version`,
or engine-specific response fields like `matched_stop` / `stop_reason`) and
resolves to `direct` or `gateway`. Detection requires affirmative engine
evidence to pick `direct`; absent any, it defaults to `gateway` (strict), so an
uncertain or misconfigured gateway is still held to the stricter expectations.
There is no fully reliable signal (a gateway can transparently proxy an engine),
so `direct` / `gateway` remain explicit overrides.

| resolved `backend_style` | Kimi thinking switch | GLM / Qwen thinking switch |
| --- | --- | --- |
| `direct` | `chat_template_kwargs={"thinking": <bool>}` | `chat_template_kwargs={"enable_thinking": <bool>}` |
| `gateway` | top-level `thinking={"type": "enabled"\|"disabled"}` | `chat_template_kwargs={"enable_thinking": <bool>}` |

GLM and Qwen are both toggled with `chat_template_kwargs={"enable_thinking": <bool>}`
in either mode. For a model family the harness doesn't recognise by name, the
`direct` path sends **both** spellings
(`chat_template_kwargs={"thinking": <bool>, "enable_thinking": <bool>}`) — an
unrecognised key is just an unused chat-template variable, so it's harmless and
avoids guessing from the model id.

The resolved backend also gates a few checks whose correct behaviour differs:

| Check | resolved `direct` (bare engine) | resolved `gateway` |
| --- | --- | --- |
| `t2_top_p_0_0` (`top_p=0.0`, out of range) | accept **2xx or 4xx** (engine may validly reject) | assert **2xx** (gateway must normalize; a 4xx is a gateway bug) |
| `t11a_no_auth` / `t11b_wrong_auth` | **SKIP** if auth not enforced (no `--api-key`); if enforced (401), verify strict 401 | assert **401** (a 2xx is a misconfigured gateway → FAIL) |

---

## 10 dimension checks (`d01`–`d10`)

| Key | Check | What it verifies | PASS criteria |
| --- | --- | --- | --- |
| `d01_basic_nostream` | Basic chat (non-stream) | Plain non-streaming completion returns text + usage | HTTP 2xx **and** non-empty `content` **and** `completion_tokens > 0` |
| `d02_stream_usage` | Basic chat (stream) + usage | SSE streaming works and emits a final usage chunk | 2xx **and** ≥5 `data:` chunks **and** exactly one `[DONE]` **and** ≥1 usage chunk |
| `d03_tool_call` | Tool calling | Model emits a valid `get_weather` tool call | Forced (`tool_choice="required"`, `temperature=0`, thinking off): 2xx **and** `tool_calls` present **and** arguments parse as JSON (string or dict). `finish_reason` is informational. |
| `d04_reasoning` | Reasoning parse | Step-by-step prompt returns a coherent answer | 2xx **and** (reasoning or content) **and** combined len > 20 **and** contains `"1"`/`"one"` |
| `d05_multimodal` | Multimodal image | Vision: describe an inline base64 red PNG | 2xx **and** no `error` **and** answer len > 15 · **SKIP** when vision off (`auto` probe found no image support / `multimodal=off`) |
| `d06_cache_hit` | Prompt cache hit | Same long prompt twice → second is served from cache | 2nd response `prompt_tokens_details.cached_tokens > 0` |
| `d07_reasoning_plus_content` | Reasoning + content split | Response splits `reasoning_content` from `content` (max_tokens 4096, thinking on) | 2xx **and** reasoning len > 20 **and** content len > 20 · **SKIP** if parser didn't split (reasoning 0, content > 100) · **SKIP** if `finish_reason="length"` truncated it before the answer (content ≤ 20) — budget artifact, raise max_tokens · FAIL only on HTTP error or a normal-stop response that still lacks one half |
| `d08_image_url_blocked` | External image URL blocked | Remote `image_url` is rejected (SSRF guard) | HTTP ≥ 400 (external URL rejected) · FAIL on 200 (fetched) or transport error · **SKIP** when vision off |
| `d09_thinking_disable_top` | Thinking off (official top-level fmt) | Thinking-off via official format (converted to CTK in `direct` mode) | 2xx **and** reasoning empty **and** content contains `"4"` **and** content < 100 chars |
| `d10_thinking_disable_ctk` | Thinking off (chat_template_kwargs) | Thinking-off via `chat_template_kwargs` (compat form, always) | same as `d09` |

---

## 46 extended checks (`t1a`–`t16c`)

### T1 — thinking switch (3)

| Key | Check | PASS criteria |
| --- | --- | --- |
| `t1a_thinking_true` | thinking = true | 2xx **and** reasoning len > 0 |
| `t1b_thinking_false` | thinking = false | 2xx **and** reasoning len == 0 |
| `t1c_thinking_default` | thinking = default (no switch) | 2xx **and** reasoning len > 0 (default-on) |

### T2 — sampling-parameter boundaries (17)

Each fires a trivial prompt with one sampling param. `ok` cases expect HTTP
2xx; the out-of-range case expects a 4xx rejection.

| Key | Param | Expect |
| --- | --- | --- |
| `t2_temperature_0_0` | `temperature=0.0` | 2xx |
| `t2_temperature_1_0` | `temperature=1.0` | 2xx |
| `t2_temperature_1_1` | `temperature=1.1` | 2xx |
| `t2_temperature_2_0` | `temperature=2.0` | 2xx |
| `t2_top_p_0_0` | `top_p=0.0` | gateway: 2xx · direct: 2xx **or** 4xx (out of range — engine may validly reject) |
| `t2_top_p_0_01` | `top_p=0.01` | 2xx |
| `t2_top_p_0_95` | `top_p=0.95` | 2xx |
| `t2_top_p_1_0` | `top_p=1.0` | 2xx |
| `t2_top_p_1_1` | `top_p=1.1` | **4xx** (out of range) |
| `t2_frequency_penalty_neg2` | `frequency_penalty=-2` | 2xx |
| `t2_frequency_penalty_0` | `frequency_penalty=0` | 2xx |
| `t2_frequency_penalty_2` | `frequency_penalty=2` | 2xx |
| `t2_presence_penalty_neg2` | `presence_penalty=-2` | 2xx |
| `t2_presence_penalty_0` | `presence_penalty=0` | 2xx |
| `t2_presence_penalty_2` | `presence_penalty=2` | 2xx |
| `t2_n_1` | `n=1` | 2xx |
| `t2_n_2` | `n=2` | 2xx |

### T3 — max_tokens boundaries (7)

Values derive from the `max_context_tokens` param (`mid = min(65536, ctx)`).
`ok` cases also assert the expected `finish_reason`.

| Key | `max_tokens` | Expect |
| --- | --- | --- |
| `t3_max_tokens_none` | omitted | 2xx, `finish_reason == "stop"` |
| `t3_max_tokens_1` | 1 | 2xx, `finish_reason == "length"` |
| `t3_max_tokens_64` | 64 | 2xx, `finish_reason == "length"` |
| `t3_max_tokens_mid` | `min(65536, ctx)` | 2xx, `finish_reason == "stop"` |
| `t3_max_tokens_max` | `ctx` (context limit) | 2xx, `finish_reason == "stop"` |
| `t3_max_tokens_neg1` | -1 | **4xx** |
| `t3_max_tokens_over` | `ctx + 1` | **4xx** |

### T4–T16 — feature & validation checks

| Key | Check | What it verifies | PASS criteria |
| --- | --- | --- | --- |
| `t4a_no_system` | No system prompt | Bare user instruction is followed | 2xx **and** content contains `_NO_SYSTEM` |
| `t4b_system_control` | System prompt override | System instruction steers the reply | 2xx **and** content contains `_SYSTEM_CONTROL` |
| `t5_function_calling` | Function calling | Valid `get_weather` tool call (same logic as `d03`) | Forced call (see `d03`): 2xx **and** tool call **and** JSON args (string or dict). `finish_reason` informational. |
| `t6_multi_turn` | Multi-turn memory | Recalls a fact from earlier turn | 2xx **and** content contains `BLUE_42` |
| `t7_streaming_sse` | Streaming SSE + usage | Streaming + final usage chunk | 2xx **and** ≥5 chunks **and** one `[DONE]` **and** ≥1 usage |
| `t8_json_object` | `response_format=json_object` | Returns valid JSON with requested values | 2xx **and** parsed `name=="Alice"` **and** `age==30` |
| `t9_json_schema` | `response_format=json_schema` | Conforms to a `person` schema | 2xx **and** parsed `name=="Alice"` **and** `age==30` |
| `t10_stop_word` | Stop word | Generation halts at the stop string | Prompt counts `1..30`, `stop=["15"]`; 2xx **and** `"1"` in content **and** `"16"` **not** in content (counting is deterministic so `15` is reliably emitted — unlike an arbitrary marker the model may reformat away) |
| `t11a_no_auth` | Missing auth → 401 | Unauthenticated request is rejected | gateway: HTTP 401 (2xx ⇒ FAIL, misconfig) · direct: **SKIP** if not enforced, else HTTP 401 |
| `t11b_wrong_auth` | Wrong auth → 401 | Bad key is rejected | gateway: HTTP 401 · direct: **SKIP** if auth not enforced, else HTTP 401 |
| `t12_chinese` | Multilingual (Chinese) | Echoes Chinese text exactly | 2xx **and** content contains `中文测试：你好世界` |
| `t12_japanese` | Multilingual (Japanese) | Echoes Japanese text exactly | 2xx **and** content contains `こんにちは` |
| `t12_emoji` | Emoji | Echoes emoji exactly | 2xx **and** content contains `🎉` |
| `t13_multimodal_base64` | Multimodal base64 PNG | Vision: name the colour of a red PNG | 2xx **and** content contains `red` · **SKIP** when vision off |
| `t14_empty_body` | Empty body → 4xx | Empty request body is rejected | **4xx** |
| `t15_idempotency_seed` | Idempotency (seed, temp=0) | `seed=42, temperature=0` → identical replies | both 2xx **and** reply1 == reply2 **and** non-empty |
| `t16a_missing_role` | Missing role → 4xx | Message without `role` is rejected | **4xx** |
| `t16b_missing_content` | Missing content → 4xx | Message without `content` is rejected | **4xx** |
| `t16c_empty_messages` | Empty messages → 4xx | `messages: []` is rejected | **4xx** |

---

## SKIP conditions at a glance

A SKIP omits the metric entirely, so it neither contributes to `pass_rate` nor
trips a redline.

| Checks | Skipped when |
| --- | --- |
| `d05_multimodal`, `d08_image_url_blocked`, `t13_multimodal_base64` | Vision disabled — `multimodal=off`, or `auto` probed the endpoint with a tiny image and it rejected image input (no 2xx + content) |
| `t11a_no_auth`, `t11b_wrong_auth` | Resolved backend is `direct` **and** the endpoint doesn't enforce auth (bare engine without `--api-key`). Under `gateway` they are never skipped — a 2xx FAILs. |
| `d07_reasoning_plus_content` | Backend returned content but the reasoning parser didn't split it out, **or** the response was truncated by `max_tokens` while still in `reasoning_content` (finish=length, no final answer to judge the split against) |
| `d09` / `d10` thinking-off | Ambiguous result that is neither a clean pass nor a clear violation |
