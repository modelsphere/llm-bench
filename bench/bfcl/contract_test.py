"""Offline acceptance of the pinned official runner against a local fixture."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Thread

from bench.modules.base import EndpointConfig
from bench.modules.bfcl_v4 import BFCLV4Module, BFCLV4Params
from bench.modules.evaluator import check_redlines


def main():
    rendezvous = Barrier(2, timeout=10)
    synchronize_cases = True
    fail_multi_turn = False
    seen_tool_ids = []  # Appends happen before the runner completes.

    class Fixture(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal fail_multi_turn
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions"
            assert body["model"] == "fixture-model"
            assert self.headers["Authorization"] == "Bearer fixture-key"
            messages = body["messages"]
            tool_names = {tool["function"]["name"] for tool in body.get("tools", [])}
            if "determine_body_mass_index" in tool_names:
                # The first irrelevance case must be scored as a request failure.
                self.send_error(503)
                return
            triangle = "calculate_triangle_area" in tool_names
            multi_turn = "pwd" in tool_names
            if fail_multi_turn and multi_turn:
                fail_multi_turn = False
                self.send_error(503)
                return
            if synchronize_cases and (triangle or (multi_turn and len(messages) == 1)):
                rendezvous.wait()
            if triangle:
                name, args = "calculate_triangle_area", '{"base":10,"height":5}'
            elif "math_factorial" in tool_names:
                name, args = "math_factorial", '{"number":5}'
            elif "triangle_properties_get" in tool_names:
                name, args = "triangle_properties_get", '{"side1":5,"side2":4,"side3":3}'
            elif "math_triangle_area_heron" in tool_names:
                name, args = "math_triangle_area_heron", '{"side1":3,"side2":4,"side3":5}'
            elif "spotify_play" in tool_names:
                name, args = "spotify_play", '{"artist":"Taylor Swift","duration":20}'
            elif multi_turn and not any(
                message["role"] == "tool" for message in messages
            ):
                name, args = "pwd", "{}"
            else:
                for message in messages:
                    if message["role"] == "tool":
                        assert message["tool_call_id"] == "fixture-call"
                        seen_tool_ids.append(message["tool_call_id"])
                name = None
            call = (
                {
                    "id": "fixture-call",
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }
                if name
                else None
            )
            calls = [call] if call else []
            if name == "spotify_play":
                calls.append(
                    {
                        "id": "fixture-call-2",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": '{"artist":"Maroon 5","duration":15}',
                        },
                    }
                )
            reason = "tool_calls" if name else "stop"
            meta = {"id": "fixture-response", "created": 0, "model": body["model"]}
            usage = {
                "prompt_tokens": 20,
                "completion_tokens": 3,
                "total_tokens": 23,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
            self.send_response(200)
            if body.get("stream"):
                assert body["stream_options"]["include_usage"] is True
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if call:
                    pieces = [
                        {"tool_calls": [
                            {"index": index, **item}
                            for index, item in enumerate(calls)
                        ]}
                    ]
                else:
                    pieces = [{"content": "Done."}]
                chunks = [
                    {"choices": [{"index": 0, "delta": piece, "finish_reason": None}]}
                    for piece in pieces
                ]
                chunks += [
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                    {"choices": [], "usage": usage},
                ]
                for chunk in chunks:
                    data = {"object": "chat.completion.chunk", **meta, **chunk}
                    self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                msg = {"role": "assistant", "content": None if call else "Done."}
                if call:
                    msg["tool_calls"] = calls
                self.wfile.write(
                    json.dumps(
                        {
                            "object": "chat.completion",
                            **meta,
                            "usage": usage,
                            "choices": [
                                {"index": 0, "message": msg, "finish_reason": reason}
                            ],
                        }
                    ).encode()
                )

    with (
        ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server,
        TemporaryDirectory() as directory,
    ):
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
            os.environ["BFCL_PYTHON"] = sys.executable
            result = BFCLV4Module().run(
                EndpointConfig(endpoint, "fixture-model", "fixture-key"),
                BFCLV4Params(
                    export_replay_trace=True, min_ttft_coverage=1, min_usage_coverage=1
                ),
                directory,
            )
            assert not result.error, result.error
            assert seen_tool_ids
            metrics = result.metrics
            assert metrics["accuracy"] == 0.5, metrics
            assert metrics["accuracy_simple_python"] == 1
            assert metrics["input_tpm"] > 0 and metrics["output_tpm"] > 0
            assert metrics["total_tpm"] == metrics["input_tpm"] + metrics["output_tpm"]
            assert (
                metrics["cache_usage_coverage"] == 1 and metrics["cache_hit_rate"] == 0
            )
            attempt = Path(result.artifacts[0]).parent
            events = [
                json.loads(line)
                for line in (attempt / "request_events.jsonl").read_text().splitlines()
            ]
            first = [event for event in events if event["request_index"] == 0]
            assert len(first) == 2
            assert max(event["started_monotonic"] for event in first) < min(
                event["finished_monotonic"] for event in first
            )
            multi = [
                event for event in events if event["case_id"] == "multi_turn_base_0"
            ]
            assert len(multi) > 1
            assert all(
                a["finished_monotonic"] <= b["started_monotonic"]
                for a, b in zip(multi, multi[1:])
            )
            plain = Path(directory) / "plain"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bench.bfcl.smoke",
                    "--endpoint",
                    endpoint,
                    "--model",
                    "fixture-model",
                    "--output-dir",
                    str(plain),
                ],
                env={**os.environ, "BFCL_ENDPOINT_API_KEY": "fixture-key"},
                check=True,
            )
            for category in ("simple_python", "multi_turn_base"):
                paths = list((plain / "score").rglob(f"BFCL_v4_{category}_score.json"))
                header = json.loads(paths[0].read_text().splitlines()[0])
                assert metrics[f"accuracy_{category}"] == header["accuracy"]
            synchronize_cases = False
            expanded = BFCLV4Module().run(
                EndpointConfig(endpoint, "fixture-model", "fixture-key"),
                BFCLV4Params(
                    task_set="bfcl-v4-tool-use-v1",
                    cases_per_category=2,
                    probe_endpoint=True,
                    allow_request_failures=True,
                ),
                directory,
            )
            assert not expanded.error, expanded.error
            assert expanded.metrics["case_count"] == 10
            # OpenAI tool names replace dots with underscores. The official
            # evaluator must apply the same conversion to its expected names.
            assert expanded.metrics["accuracy_simple_python"] == 1, expanded.metrics
            assert expanded.metrics["accuracy_multiple"] == 1, expanded.metrics
            assert expanded.metrics["accuracy_parallel"] == 0.5, expanded.metrics
            assert expanded.metrics["accuracy_irrelevance"] == 0.5, expanded.metrics
            for category in (
                "simple_python",
                "multiple",
                "parallel",
                "irrelevance",
                "multi_turn_base",
            ):
                assert expanded.metrics[f"case_count_{category}"] == 2
            expanded_attempt = Path(expanded.artifacts[0]).parent
            expanded_cases = [
                json.loads(line)
                for line in (expanded_attempt / "case_results.jsonl").read_text().splitlines()
            ]
            irrelevance = {
                row["case_id"]: row for row in expanded_cases if row["category"] == "irrelevance"
            }
            assert irrelevance["irrelevance_0"]["request_failed"] is True
            assert irrelevance["irrelevance_0"]["success"] is False
            assert irrelevance["irrelevance_1"]["success"] is True
            assert not irrelevance["irrelevance_1"].get("request_failed")
            passed = 1 + sum(
                row["success"] for row in expanded_cases if row["category"] != "irrelevance"
            )
            assert expanded.metrics["case_success_count"] == passed
            assert expanded.metrics["accuracy"] == passed / 10
            manifest = json.loads((expanded_attempt / "manifest.json").read_text())
            assert manifest["handler_version"] == "llmbench-stream-v2"
            assert all(
                ids == [f"{category}_0", f"{category}_1"]
                for category, ids in manifest["cases"].items()
            )
            probe = json.loads((expanded_attempt / "probe" / "probe.json").read_text())
            assert probe["case_id"] == "multi_turn_base_0"
            assert probe["tool_call_observed"] is True
            assert probe["tool_result_roundtrip_observed"] is True
            assert probe["usage_sample_count"] == probe["successful_requests"]
            assert probe["successful_requests"] > 1
            assert not any(
                file.is_file()
                for file in (expanded_attempt / "probe" / "score").rglob("*")
            )
            assert not (expanded_attempt / "probe" / "trace.jsonl").exists()
            official = [
                json.loads(line)
                for line in (expanded_attempt / "request_events.jsonl")
                .read_text()
                .splitlines()
            ]
            assert all(event["phase"] == "generate" for event in official)
            assert expanded.metrics["http_attempt_count"] == len(official)
            fail_multi_turn = True
            partial = BFCLV4Module().run(
                EndpointConfig(endpoint, "fixture-model", "fixture-key"),
                BFCLV4Params(
                    allow_request_failures=True, min_ttft_coverage=1, min_usage_coverage=1
                ),
                directory,
            )
            assert not partial.error, partial.error
            assert not fail_multi_turn
            assert partial.metrics["case_count"] == 2
            assert partial.metrics["accuracy"] == 0.5
            assert partial.metrics["total_tpm"] > 0
            assert partial.metrics["uptime"] == 0.5
            assert not check_redlines(
                partial.metrics, [{"key": "uptime", "role": "redline", "min_val": 0.99}]
            )
            partial_attempt = Path(partial.artifacts[0]).parent
            failed_cases = [
                row for row in map(
                    json.loads, (partial_attempt / "case_results.jsonl").read_text().splitlines()
                ) if row.get("request_failed")
            ]
            assert [row["case_id"] for row in failed_cases] == ["multi_turn_base_0"]
            assert failed_cases[0]["success"] is False
            print(
                "Official BFCL streaming and non-streaming scores agree, and request failures count as incorrect"
            )
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
