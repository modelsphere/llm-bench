"""Unit tests for module-params secret redaction (app/core/param_secrets).

Pure/offline — no DB, no network. The redaction guards the API surfaces that
return params_json to non-admins (benchmark list/detail, submission run
snapshots), where e.g. the opencompass judge_api_key is the ADMIN's credential.
"""
from __future__ import annotations

from app.core.param_secrets import REDACTED, is_secret_param, redact_params


def test_secret_names_detected():
    for name in (
        "api_key", "judge_api_key", "apikey", "probe_api_token",
        "access_token", "webhook_secret", "db_password", "JUDGE_API_KEY",
    ):
        assert is_secret_param(name), name


def test_non_secret_names_pass_through():
    for name in ("judge_api_url", "judge_model", "max_tokens", "token_budget", "keyspace"):
        assert not is_secret_param(name), name


def test_redact_masks_only_non_empty_secret_strings():
    params = {
        "judge_api_url": "https://judge.example.com/v1",
        "judge_model": "gpt-judge",
        "judge_api_key": "sk-live-abc123",
        "api_key": "",             # blank = self-judge — stays visible as blank
        "max_workers": 16,
        "stream": True,
    }
    out = redact_params(params)
    assert out["judge_api_key"] == REDACTED
    assert "sk-live-abc123" not in str(out)
    assert out["api_key"] == ""
    assert out["judge_api_url"] == "https://judge.example.com/v1"
    assert out["max_workers"] == 16
    # the input dict is not mutated
    assert params["judge_api_key"] == "sk-live-abc123"


def test_redact_handles_empty_and_none():
    assert redact_params(None) is None
    assert redact_params({}) == {}


def test_redact_leaves_non_string_secret_values():
    # A non-string under a secret-looking name isn't a credential (and masking
    # it would corrupt typed params); leave it.
    assert redact_params({"api_key": None}) == {"api_key": None}
