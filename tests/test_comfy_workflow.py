"""Tests for the ComfyUI workflow parse + parameter mapping (V1 image-gen)."""

from __future__ import annotations

import copy

from fabulist.comfy.workflow import (
    ADJUSTABLE_KEYS,
    OverrideResult,
    ParamMap,
    apply_overrides,
    parse_workflow,
)

#: A minimal API-format workflow: sampler -> two CLIPTextEncode conditioners and
#: an EmptyLatentImage resolution node.
WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 111,
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old positive", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old negative", "clip": ["4", 1]},
    },
}

#: A REAL user workflow (RES4LYF / kroma): a custom ``ClownsharKSampler_Beta`` fed
#: by a ``UNETLoader``, two ``CLIPTextEncode`` conditioners and an
#: ``EmptyLatentImage``. The custom sampler's extra inputs (``eta`` /
#: ``steps_to_run`` / ``sampler_mode`` / ``bongmath``) must be ignored by the
#: adjustable mapping, and its seed input is ``seed`` (not ``noise_seed``).
CLOWNSHARK_WORKFLOW = {
    "8": {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": "kroma-v0.3.1-turbo-opd.safetensors",
            "weight_dtype": "default",
        },
    },
    "11": {
        "class_type": "ClownsharKSampler_Beta",
        "inputs": {
            "seed": 798893994874649,
            "steps": 8,
            "cfg": 1,
            "sampler_name": "linear/euler",
            "scheduler": "simple",
            "model": ["8", 0],
            "positive": ["13", 0],
            "negative": ["14", 0],
            "latent_image": ["5", 0],
            "eta": 0.6,
            "steps_to_run": -1,
            "denoise": 1,
            "sampler_mode": "standard",
            "bongmath": True,
        },
    },
    "13": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old positive", "clip": ["7", 0]},
    },
    "14": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old negative", "clip": ["7", 0]},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 768, "height": 1024, "batch_size": 1},
    },
}


def test_parse_maps_seed_conditioners_and_resolution():
    parsed = parse_workflow(WORKFLOW)

    assert isinstance(parsed, ParamMap)
    assert parsed.seed == [("3", "seed")]
    assert parsed.positive == ("6", "text")
    assert parsed.negative == ("7", "text")
    assert parsed.width == ("5", "width")
    assert parsed.height == ("5", "height")
    assert parsed.mappable() == set(ADJUSTABLE_KEYS)


def test_parse_maps_ksampler_advanced_noise_seed():
    workflow = {
        "10": {
            "class_type": "KSamplerAdvanced",
            "inputs": {"noise_seed": 5, "positive": ["6", 0], "negative": ["7", 0]},
        },
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "p"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "n"}},
    }

    parsed = parse_workflow(workflow)

    assert parsed.seed == [("10", "noise_seed")]


def test_parse_maps_every_sampler_seed():
    workflow = {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["6", 0]}},
        "8": {"class_type": "KSampler", "inputs": {"seed": 2}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "p"}},
    }

    parsed = parse_workflow(workflow)

    assert parsed.seed == [("3", "seed"), ("8", "seed")]


def test_parse_reports_unmappable_when_no_targets():
    parsed = parse_workflow({"3": {"class_type": "KSampler", "inputs": {"seed": 1}}})

    # Unknown class -> no conditioner/resolution targets.
    assert parsed.positive is None
    assert parsed.negative is None
    assert parsed.width is None
    assert parsed.height is None
    assert parsed.mappable() == {"seed"}


def test_parse_is_total_on_malformed_input():
    for bad in (None, [], "nope", 7, {"x": "not a node"}):
        parsed = parse_workflow(bad)
        assert parsed.seed == []
        assert parsed.mappable() == set()


def test_apply_overrides_returns_new_dict_and_does_not_mutate_input():
    original = copy.deepcopy(WORKFLOW)

    result = apply_overrides(
        WORKFLOW, {"seed": 4242, "positive": "a fox", "negative": "blurry"}
    )

    assert isinstance(result, OverrideResult)
    assert result.workflow is not WORKFLOW
    assert result.workflow["3"]["inputs"]["seed"] == 4242
    assert result.workflow["6"]["inputs"]["text"] == "a fox"
    assert result.workflow["7"]["inputs"]["text"] == "blurry"
    assert result.unmapped == []
    # The caller's dict is untouched.
    assert WORKFLOW == original


def test_apply_overrides_sets_resolution():
    result = apply_overrides(WORKFLOW, {"width": 768, "height": 1024})

    assert result.workflow["5"]["inputs"]["width"] == 768
    assert result.workflow["5"]["inputs"]["height"] == 1024
    assert result.unmapped == []


def test_apply_overrides_reports_unmapped_keys_in_request_order():
    workflow = {"3": {"class_type": "KSampler", "inputs": {"seed": 1}}}

    result = apply_overrides(workflow, {"width": 10, "bogus": 1, "seed": 9})

    assert result.unmapped == ["width", "bogus"]
    assert result.workflow["3"]["inputs"]["seed"] == 9


def test_apply_overrides_on_malformed_workflow_reports_all_requested():
    result = apply_overrides("nope", {"seed": 1, "positive": "x"})

    assert result.workflow == {}
    assert result.unmapped == ["seed", "positive"]


def test_apply_overrides_with_empty_overrides_is_a_noop_copy():
    result = apply_overrides(WORKFLOW, {})

    assert result.workflow == WORKFLOW
    assert result.unmapped == []


def test_parse_maps_clownshark_custom_sampler():
    parsed = parse_workflow(CLOWNSHARK_WORKFLOW)

    assert parsed.seed == [("11", "seed")]
    assert parsed.positive == ("13", "text")
    assert parsed.negative == ("14", "text")
    assert parsed.width == ("5", "width")
    assert parsed.height == ("5", "height")
    assert parsed.mappable() == set(ADJUSTABLE_KEYS)


def test_apply_overrides_clownshark_workflow_replaces_all_five():
    original = copy.deepcopy(CLOWNSHARK_WORKFLOW)

    result = apply_overrides(
        CLOWNSHARK_WORKFLOW,
        {
            "seed": 7,
            "positive": "a fox",
            "negative": "blurry",
            "width": 640,
            "height": 640,
        },
    )

    assert result.unmapped == []
    assert result.workflow["11"]["inputs"]["seed"] == 7
    assert result.workflow["13"]["inputs"]["text"] == "a fox"
    assert result.workflow["14"]["inputs"]["text"] == "blurry"
    assert result.workflow["5"]["inputs"]["width"] == 640
    assert result.workflow["5"]["inputs"]["height"] == 640
    # Non-adjustable custom-sampler inputs are left exactly as authored.
    assert result.workflow["11"]["inputs"]["eta"] == 0.6
    assert result.workflow["11"]["inputs"]["sampler_mode"] == "standard"
    assert result.workflow["11"]["inputs"]["bongmath"] is True
    # The caller's dict is untouched.
    assert CLOWNSHARK_WORKFLOW == original
