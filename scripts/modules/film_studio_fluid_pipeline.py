# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Typed liquid-iteration policy with fail-closed cache reuse.

The product observes Blender state and passes a grouped snapshot here.  This
module owns quality resolution, derives signatures, decides cache invalidation,
and requires an exact REVIEW receipt before admitting FINAL.  It deliberately
has no bpy dependency and never starts a bake or render.
"""

from __future__ import annotations

import copy
import hashlib
import math

import film_studio_contract


REQUEST_VERSION = "bfs.fluidIterationRequest.v0.1"
SNAPSHOT_VERSION = "bfs.fluidStateSnapshot.v0.1"
STATE_VERSION = "bfs.fluidIterationState.v0.1"
PLAN_VERSION = "bfs.fluidIterationPlan.v0.1"
REVIEW_RECEIPT_VERSION = "bfs.fluidReviewReceipt.v0.1"

QUALITY_TIERS = {
    "DRAFT": {"resolutionMax": 64, "maximumFrameCount": 12},
    "PREVIEW": {"resolutionMax": 96, "maximumFrameCount": 24},
    "REVIEW": {"resolutionMax": 128, "maximumFrameCount": 48},
    "FINAL": {"resolutionMax": 192, "maximumFrameCount": 240},
}

_BANNED_SNAPSHOT_KEYS = {
    "qualityTier",
    "requestedTier",
    "resolutionMax",
    "resolution_max",
    "cacheDecision",
    "dataSignature",
    "physicsSignature",
    "surfaceSignature",
    "visualSignature",
    "planHash",
    "stateHash",
    "receiptHash",
}


class FluidPipelineError(RuntimeError):
    """Fail-closed policy rejection with a stable machine reason."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _canonical(value):
    try:
        return film_studio_contract.javascript_canonical_json(value)
    except film_studio_contract.ContractError as error:
        raise FluidPipelineError(error.reason, str(error)) from error


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _self_hash(value, field):
    body = copy.deepcopy(value)
    body.pop(field, None)
    return _hash(body)


def _exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise FluidPipelineError("SPEC_SCHEMA", label)


def _hex(value, label, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise FluidPipelineError("SPEC_SCHEMA", label)


def _finite_tree(value, path="/"):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FluidPipelineError("NONFINITE_NUMBER", path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _finite_tree(child, f"{path}{index}/")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise FluidPipelineError("SPEC_SCHEMA", f"{path}non-string-key")
            _finite_tree(child, f"{path}{key}/")
        return
    raise FluidPipelineError("SPEC_SCHEMA", path)


def _scan_snapshot_authority(value, path="/"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _BANNED_SNAPSHOT_KEYS:
                raise FluidPipelineError("CALLER_AUTHORITY", f"{path}{key}")
            _scan_snapshot_authority(child, f"{path}{key}/")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_snapshot_authority(child, f"{path}{index}/")


def _validate_snapshot(snapshot, tier):
    _exact(snapshot, {"schemaVersion", "physics", "surface", "visual"}, "snapshot")
    if snapshot["schemaVersion"] != SNAPSHOT_VERSION:
        raise FluidPipelineError("SPEC_VERSION", "snapshot")
    _exact(snapshot["physics"], {"frameStart", "frameEnd", "fps", "parameters"}, "physics")
    _exact(snapshot["surface"], {"parameters"}, "surface")
    _exact(snapshot["visual"], {"parameters"}, "visual")
    physics = snapshot["physics"]
    if isinstance(physics["frameStart"], bool) or not isinstance(physics["frameStart"], int):
        raise FluidPipelineError("SPEC_SCHEMA", "frameStart")
    if isinstance(physics["frameEnd"], bool) or not isinstance(physics["frameEnd"], int):
        raise FluidPipelineError("SPEC_SCHEMA", "frameEnd")
    if physics["frameStart"] < 1 or physics["frameEnd"] < physics["frameStart"]:
        raise FluidPipelineError("FRAME_WINDOW", "invalid frame interval")
    frame_count = physics["frameEnd"] - physics["frameStart"] + 1
    if frame_count > QUALITY_TIERS[tier]["maximumFrameCount"]:
        raise FluidPipelineError("TIER_FRAME_CEILING", f"{tier} permits at most {QUALITY_TIERS[tier]['maximumFrameCount']} frames")
    if isinstance(physics["fps"], bool) or not isinstance(physics["fps"], (int, float)) or not 1 <= physics["fps"] <= 240:
        raise FluidPipelineError("SPEC_SCHEMA", "fps")
    for group in (physics["parameters"], snapshot["surface"]["parameters"], snapshot["visual"]["parameters"]):
        if not isinstance(group, dict):
            raise FluidPipelineError("SPEC_SCHEMA", "snapshot parameters")
    _finite_tree(snapshot)
    _scan_snapshot_authority(snapshot)


def _signatures(snapshot, tier):
    resolution = QUALITY_TIERS[tier]["resolutionMax"]
    physics_identity = snapshot["physics"]
    return {
        "physicsIdentityHash": _hash(physics_identity),
        "physicsSignature": _hash({"resolutionMax": resolution, "physics": physics_identity}),
        "surfaceSignature": _hash(snapshot["surface"]),
        "visualSignature": _hash(snapshot["visual"]),
    }


def _validate_state(state):
    if state is None:
        return
    _exact(state, {
        "schemaVersion", "tier", "physicsIdentityHash", "physicsSignature",
        "surfaceSignature", "visualSignature", "dataCacheHash",
        "meshCacheHash", "stateHash",
    }, "previousState")
    if state["schemaVersion"] != STATE_VERSION or state["tier"] not in QUALITY_TIERS:
        raise FluidPipelineError("STATE_SCHEMA", "previousState")
    for key in ("physicsIdentityHash", "physicsSignature", "surfaceSignature", "visualSignature", "dataCacheHash", "meshCacheHash"):
        _hex(state[key], key)
    if state["stateHash"] != _self_hash(state, "stateHash"):
        raise FluidPipelineError("STALE_STATE", "previous state self hash")


def _validate_review_receipt(receipt, signatures, snapshot):
    if receipt is None:
        raise FluidPipelineError("FINAL_WITHOUT_REVIEW", "FINAL requires a REVIEW receipt")
    _exact(receipt, {
        "schemaVersion", "status", "tier", "physicsIdentityHash",
        "surfaceSignature", "frameWindow", "machineAuditHash",
        "visualReviewHash", "reviewPlanHash", "receiptHash",
    }, "reviewReceipt")
    if receipt["schemaVersion"] != REVIEW_RECEIPT_VERSION or receipt["status"] != "PASS_REVIEW" or receipt["tier"] != "REVIEW":
        raise FluidPipelineError("STALE_REVIEW_RECEIPT", "review identity")
    for key in ("physicsIdentityHash", "surfaceSignature", "machineAuditHash", "visualReviewHash", "reviewPlanHash"):
        _hex(receipt[key], key)
    _exact(receipt["frameWindow"], {"frameStart", "frameEnd"}, "review frameWindow")
    if receipt["receiptHash"] != _self_hash(receipt, "receiptHash"):
        raise FluidPipelineError("STALE_REVIEW_RECEIPT", "review self hash")
    expected_window = {
        "frameStart": snapshot["physics"]["frameStart"],
        "frameEnd": snapshot["physics"]["frameEnd"],
    }
    if receipt["frameWindow"] != expected_window:
        raise FluidPipelineError("FRAME_WINDOW_CHANGED_AFTER_REVIEW", "frame window")
    if receipt["physicsIdentityHash"] != signatures["physicsIdentityHash"]:
        raise FluidPipelineError("PHYSICS_CHANGED_AFTER_REVIEW", "physics identity")
    if receipt["surfaceSignature"] != signatures["surfaceSignature"]:
        raise FluidPipelineError("SURFACE_CHANGED_AFTER_REVIEW", "surface identity")


def compile_iteration_plan(request):
    """Return a self-hashed plan; never execute the listed stages."""

    _exact(request, {"schemaVersion", "requestedTier", "currentSnapshot", "previousState", "reviewReceipt"}, "request")
    if request["schemaVersion"] != REQUEST_VERSION:
        raise FluidPipelineError("SPEC_VERSION", "request")
    tier = request["requestedTier"]
    if tier not in QUALITY_TIERS:
        raise FluidPipelineError("UNKNOWN_TIER", str(tier))
    snapshot = request["currentSnapshot"]
    _validate_snapshot(snapshot, tier)
    previous = request["previousState"]
    _validate_state(previous)
    signatures = _signatures(snapshot, tier)
    if tier == "FINAL":
        _validate_review_receipt(request["reviewReceipt"], signatures, snapshot)
    elif request["reviewReceipt"] is not None:
        raise FluidPipelineError("UNEXPECTED_REVIEW_RECEIPT", tier)

    if previous is None or previous["physicsSignature"] != signatures["physicsSignature"]:
        decision = "BAKE_DATA_THEN_MESH"
        stages = ["DATA", "MESH"]
        reuse = []
        invalidated = ["DATA", "MESH"]
        bound_data_cache = None
        bound_mesh_cache = None
    elif previous["surfaceSignature"] != signatures["surfaceSignature"]:
        decision = "REUSE_DATA_BAKE_MESH"
        stages = ["MESH"]
        reuse = ["DATA"]
        invalidated = ["MESH"]
        bound_data_cache = previous["dataCacheHash"]
        bound_mesh_cache = None
    elif previous["visualSignature"] != signatures["visualSignature"]:
        decision = "REUSE_DATA_AND_MESH_VISUAL_ONLY"
        stages = ["VISUAL"]
        reuse = ["DATA", "MESH"]
        invalidated = []
        bound_data_cache = previous["dataCacheHash"]
        bound_mesh_cache = previous["meshCacheHash"]
    else:
        decision = "REUSE_ALL"
        stages = []
        reuse = ["DATA", "MESH"]
        invalidated = []
        bound_data_cache = previous["dataCacheHash"]
        bound_mesh_cache = previous["meshCacheHash"]

    plan = {
        "schemaVersion": PLAN_VERSION,
        "requestedTier": tier,
        "resolutionMax": QUALITY_TIERS[tier]["resolutionMax"],
        "frameWindow": {
            "frameStart": snapshot["physics"]["frameStart"],
            "frameEnd": snapshot["physics"]["frameEnd"],
            "frameCount": snapshot["physics"]["frameEnd"] - snapshot["physics"]["frameStart"] + 1,
        },
        "decision": decision,
        "stages": stages,
        "reuse": reuse,
        "invalidated": invalidated,
        "signatures": signatures,
        "boundCaches": {"dataCacheHash": bound_data_cache, "meshCacheHash": bound_mesh_cache},
        "reviewReceiptHash": request["reviewReceipt"]["receiptHash"] if request["reviewReceipt"] else None,
    }
    plan["planHash"] = _self_hash(plan, "planHash")
    return plan


def seal_iteration_state(plan, data_cache_hash, mesh_cache_hash):
    """Bind observed cache hashes after the caller completes and verifies a plan."""

    _exact(plan, {
        "schemaVersion", "requestedTier", "resolutionMax", "frameWindow",
        "decision", "stages", "reuse", "invalidated", "signatures",
        "boundCaches", "reviewReceiptHash", "planHash",
    }, "plan")
    if plan["schemaVersion"] != PLAN_VERSION or plan["planHash"] != _self_hash(plan, "planHash"):
        raise FluidPipelineError("PLAN_HASH", "plan")
    _hex(data_cache_hash, "dataCacheHash")
    _hex(mesh_cache_hash, "meshCacheHash")
    state = {
        "schemaVersion": STATE_VERSION,
        "tier": plan["requestedTier"],
        **plan["signatures"],
        "dataCacheHash": data_cache_hash,
        "meshCacheHash": mesh_cache_hash,
    }
    state["stateHash"] = _self_hash(state, "stateHash")
    return state


def seal_review_receipt(review_plan, machine_audit_hash, visual_review_hash):
    """Bind external machine and visual PASS evidence for later FINAL admission."""

    if review_plan.get("schemaVersion") != PLAN_VERSION or review_plan.get("planHash") != _self_hash(review_plan, "planHash"):
        raise FluidPipelineError("PLAN_HASH", "review plan")
    if review_plan.get("requestedTier") != "REVIEW" or review_plan.get("resolutionMax") != 128:
        raise FluidPipelineError("REVIEW_TIER", "receipt can seal only a REVIEW plan")
    _hex(machine_audit_hash, "machineAuditHash")
    _hex(visual_review_hash, "visualReviewHash")
    receipt = {
        "schemaVersion": REVIEW_RECEIPT_VERSION,
        "status": "PASS_REVIEW",
        "tier": "REVIEW",
        "physicsIdentityHash": review_plan["signatures"]["physicsIdentityHash"],
        "surfaceSignature": review_plan["signatures"]["surfaceSignature"],
        "frameWindow": {
            "frameStart": review_plan["frameWindow"]["frameStart"],
            "frameEnd": review_plan["frameWindow"]["frameEnd"],
        },
        "machineAuditHash": machine_audit_hash,
        "visualReviewHash": visual_review_hash,
        "reviewPlanHash": review_plan["planHash"],
    }
    receipt["receiptHash"] = _self_hash(receipt, "receiptHash")
    return receipt
