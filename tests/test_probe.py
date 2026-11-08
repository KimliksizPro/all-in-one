from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aio.probe import UserQuota, parse_user_status


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def status_with_models(models: list) -> dict:
    return {
        "userStatus": {
            "email": "test@example.com",
            "name": "Test Kullanıcı",
            "planStatus": {
                "availablePromptCredits": 500,
                "availableFlowCredits": 100,
                "planInfo": {
                    "planName": "Pro",
                    "teamsTier": "TEAMS_TIER_PRO",
                    "monthlyPromptCredits": 50000,
                    "monthlyFlowCredits": 150000,
                },
            },
            "userTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
            "cascadeModelConfigData": {"clientModelConfigs": models},
        }
    }


def test_parse_user_status_preserves_thirteen_models_and_plan():
    # Equal quotas do not imply a shared bucket, even within one family.
    expected = [
        ("gemini-flash-high", "Gemini Flash (High)", "gemini", 0.99998707, 2),
        ("gemini-flash-low", "Gemini Flash (Low)", "gemini", 0.99998707, 2),
        ("gemini-pro-high", "Gemini Pro (High)", "gemini", 0.7, 3),
        ("gemini-pro-low", "Gemini Pro (Low)", "gemini", 0.6, 4),
        ("gemini-argon-high", "Gemini Argon (High)", "argon", 0.5, 144),
        ("gemini-argon-medium", "Gemini Argon (Medium)", "argon", 0.0, 145),
        ("gemini-argon-low", "Gemini Argon (Low)", "argon", 0.3, 146),
        ("claude-opus-high", "Claude Opus (High)", "claude", 0.4, 1),
        ("claude-opus-low", "Claude Opus (Low)", "claude", 0.8, 5),
        ("claude-sonnet", "Claude Sonnet", "claude", 0.9, 6),
        ("gpt-oss-medium", "GPT-OSS (Medium)", "gpt", 0.2, 7),
        ("gpt-oss-low", "GPT-OSS (Low)", "gpt", 0.1, 8),
        ("custom-model", "Custom model", "other", 1.0, 9),
    ]
    models = [
        {"modelId": model_id, "label": label, "quotaInfo": {
            "remainingFraction": fraction,
            "resetTime": (NOW + timedelta(hours=hours)).isoformat(),
        }}
        for model_id, label, _, fraction, hours in expected
    ]
    quota = parse_user_status(status_with_models(models), now=NOW)
    assert quota.ok is True
    assert quota.email == "test@example.com"
    assert quota.plan_name == "Pro"
    assert quota.plan_id == "g1-pro-tier"
    assert quota.credits_prompt == 500
    assert quota.credits_flow == 100
    assert quota.monthly_prompt == 50000
    assert quota.monthly_flow == 150000
    assert quota.worst_remaining == 0.0
    assert quota.raw_model_count == len(quota.groups) == 13
    by_id = {group.id: group for group in quota.groups}
    assert len(by_id) == 13
    for model_id, label, family, fraction, hours in expected:
        group = by_id[f"model:{model_id}"]
        assert group.model_id == model_id
        assert group.name == label
        assert group.models == [label]
        assert group.family == family
        assert group.window == "bilinmiyor"
        assert group.remaining == fraction
        assert group.remaining_percent == fraction * 100.0
        assert group.reset_at == (NOW + timedelta(hours=hours)).isoformat()
        assert group.reset_in_seconds == hours * 3600
        assert group.exhausted is (fraction == 0.0)
    assert quota.groups[0].model_id == "gemini-argon-medium"
    reordered = parse_user_status(status_with_models(list(reversed(models))), now=NOW)
    assert {g.model_id: g.id for g in reordered.groups} == {g.model_id: g.id for g in quota.groups}


@pytest.mark.parametrize(("fields", "expected"), [
    ({"modelId": "preferred", "modelOrAlias": {"model": "nested"}, "model": "old"}, "preferred"),
    ({"modelOrAlias": {"model": "nested-model", "alias": "alias"}, "model": "old"}, "nested-model"),
    ({"modelOrAlias": {"alias": "nested-alias"}, "model": "old"}, "nested-alias"),
    ({"modelId": {"invalid": True}, "modelOrAlias": {"model": {"invalid": True}, "alias": "alias"}}, "alias"),
    ({"modelOrAlias": "invalid", "model": "plain-model"}, "plain-model"),
    ({"model": {"invalid": True}}, "Source label"),
])
def test_model_identity_precedence(fields, expected):
    quota = parse_user_status(status_with_models([{"label": "Source label", **fields}]), now=NOW)
    assert len(quota.groups) == 1
    group = quota.groups[0]
    assert group.model_id == expected
    assert group.id == f"model:{expected}"
    assert group.name == "Source label"
    assert group.models == ["Source label"]


def test_missing_quota_is_unknown_but_explicit_zero_is_exhausted():
    reset = (NOW + timedelta(hours=2)).isoformat()
    models = [
        {"modelId": "missing-quota"},
        {"modelId": "invalid-quota", "quotaInfo": []},
        {"modelId": "missing-fraction", "quotaInfo": {"resetTime": reset}},
        {"modelId": "null-fraction", "quotaInfo": {"remainingFraction": None}},
        {"modelId": "zero", "quotaInfo": {"remainingFraction": 0, "resetTime": reset}},
        {"modelId": "tiny", "quotaInfo": {"remainingFraction": 0.000001}},
        None,
        "not a model config",
    ]
    quota = parse_user_status(status_with_models(models), now=NOW)
    assert quota.raw_model_count == len(quota.groups) == 6
    by_id = {group.model_id: group for group in quota.groups}
    for model_id in ("missing-quota", "invalid-quota", "missing-fraction", "null-fraction"):
        assert by_id[model_id].remaining is None
        assert by_id[model_id].remaining_percent is None
        assert by_id[model_id].exhausted is False
    assert by_id["missing-fraction"].reset_at == reset
    assert by_id["missing-fraction"].reset_in_seconds == 7200
    assert by_id["zero"].remaining == 0.0
    assert by_id["zero"].remaining_percent == 0.0
    assert by_id["zero"].exhausted is True
    assert by_id["tiny"].remaining == 0.000001
    assert by_id["tiny"].exhausted is False
    assert quota.worst_remaining == 0.0
    unknown = parse_user_status(status_with_models(models[:4]), now=NOW)
    assert unknown.worst_remaining is None
    assert len(unknown.groups) == 4


@pytest.mark.parametrize("fraction", [
    True, False, float("nan"), float("inf"), float("-inf"),
    "NaN", "Infinity", -0.01, 1.000001, "bad", {}, [], 10 ** 1000,
])
def test_invalid_fraction_is_unknown_without_losing_reset(fraction):
    reset = NOW + timedelta(minutes=20, microseconds=123456)
    quota = parse_user_status(status_with_models([{
        "modelOrAlias": {"model": "gemini-model"},
        "quotaInfo": {"remainingFraction": fraction, "resetTime": reset.isoformat()},
    }]), now=NOW)
    assert quota.raw_model_count == 1
    group = quota.groups[0]
    assert group.remaining is None
    assert group.remaining_percent is None
    assert group.exhausted is False
    assert group.reset_at == reset.isoformat()
    assert group.reset_in_seconds == 1200
    assert quota.worst_remaining is None


@pytest.mark.parametrize("reset", [None, "invalid", {}, 123])
def test_invalid_reset_does_not_hide_known_fraction(reset):
    quota = parse_user_status(status_with_models([{
        "modelId": "claude-model",
        "quotaInfo": {"remainingFraction": 0.75, "resetTime": reset},
    }]), now=NOW)
    group = quota.groups[0]
    assert group.remaining == 0.75
    assert group.reset_at is None
    assert group.reset_in_seconds is None
    assert group.window == "bilinmiyor"


def test_user_quota_json_roundtrip_preserves_precision_and_null():
    quota = parse_user_status(status_with_models([
        {"modelId": "exact", "label": "Exact variant", "quotaInfo": {"remainingFraction": 0.99998707}},
        {"modelId": "unknown", "label": "Unknown variant"},
        {"modelId": "zero", "quotaInfo": {"remainingFraction": 0}},
    ]), now=NOW)
    payload = json.loads(json.dumps(quota.to_dict(), allow_nan=False))
    serialized = {group["model_id"]: group for group in payload["groups"]}
    assert serialized["exact"]["remaining"] == 0.99998707
    assert serialized["exact"]["remaining_percent"] == 0.99998707 * 100.0
    assert serialized["exact"]["remaining_percent"] < 100.0
    assert serialized["unknown"]["remaining"] is None
    assert serialized["unknown"]["remaining_percent"] is None
    assert serialized["unknown"]["exhausted"] is False
    assert serialized["zero"]["exhausted"] is True
    restored = UserQuota.from_dict(payload)
    assert restored.to_dict() == quota.to_dict()


def test_repeated_and_unidentified_source_rows_keep_independent_values():
    quota = parse_user_status(status_with_models([
        {"modelId": "same", "quotaInfo": {"remainingFraction": 0.8}},
        {"modelId": "same", "quotaInfo": {"remainingFraction": 0.2}},
        {"quotaInfo": {"remainingFraction": 0.6}},
        {"quotaInfo": {"remainingFraction": 0.4}},
    ]), now=NOW)
    assert len({g.id for g in quota.groups}) == 4
    assert sorted(g.remaining for g in quota.groups) == [0.2, 0.4, 0.6, 0.8]
    assert [g.model_id for g in quota.groups if g.model_id is None] == [None, None]
    assert sorted(g.remaining for g in UserQuota.from_dict(quota.to_dict()).groups) == [0.2, 0.4, 0.6, 0.8]


def test_parse_missing_user_status():
    quota = parse_user_status({})
    assert quota.ok is False
    assert quota.error
    assert quota.worst_remaining is None
