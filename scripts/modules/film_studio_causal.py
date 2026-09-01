# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Restricted declarative causal-scene contract backed by Blender/Bullet.

The document may choose values and allowlisted factories.  It cannot carry
Python, shell commands, network requests, arbitrary paths, or final poses.
Only pre-release initial conditions are authored; Bullet owns every dynamic
pose after release.  Cameras fit evaluated semantic bounds after simulation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import statistics
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

import film_studio_contract


SPEC_VERSIONS = {"bfs.causalSceneSpec.v0.1", "bfs.causalSceneSpec.v0.2", "bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}
CONTRACT_VERSION = "bfs.filmStudioCausalContract.v0.4"
ALLOWED_FACTORIES = {
    "GROOVED_SPHERE",
    "BEVELED_DOMINO_BLOCK",
    "FILLED_LATHED_BOTTLE",
    "SIMPLE_WALL",
    "AREA_LIGHT",
    "CAMERA_FROM_DIRECTION_CLASS",
}
ALLOWED_COLLISION_SHAPES = {"SPHERE", "BOX", "CONVEX_HULL"}
DIRECTION_CAMERA = {
    "FRONT_LEFT_HIGH": ((-5.5, -6.0, 3.0), (0.0, 0.0, 0.55)),
    "FRONT_LEFT_LOW": ((-3.0, -5.4, 1.8), (0.2, 0.0, 0.55)),
    "FRONT_RIGHT_HIGH": ((5.4, -6.0, 3.0), (0.5, 0.0, 0.42)),
}


class CausalContractError(RuntimeError):
    """A fail-closed causal contract rejection with a stable reason."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return film_studio_contract.javascript_canonical_json(value)


def _self_hash(value, field):
    body = copy.deepcopy(value)
    body.pop(field, None)
    return _sha256(_canonical(body).encode("utf-8"))


def _reject_constant(token):
    raise CausalContractError("NONFINITE_NUMBER", f"Non-finite JSON token {token} is forbidden")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except CausalContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CausalContractError("INVALID_JSON", str(error)) from error


def _below_existing(root, uri):
    normalized = str(uri).replace("\\", "/")
    if not normalized or normalized.startswith("/") or "://" in normalized or ".." in normalized.split("/"):
        raise CausalContractError("PATH_ESCAPE", "CausalSceneSpec escapes the repository root")
    candidate = root / normalized
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise CausalContractError("MISSING_INPUT", normalized) from error
    if root not in resolved.parents or Path(os.path.abspath(candidate)) != resolved:
        raise CausalContractError("PATH_ESCAPE", "CausalSceneSpec traverses a link or escapes the root")
    return resolved


def _exact_keys(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise CausalContractError("UNKNOWN_TOP_LEVEL_FIELD" if path == "/" else "SPEC_SCHEMA", path)


def _finite_tree(value):
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CausalContractError("NONFINITE_NUMBER", "Every numeric input must be finite")
        return
    if isinstance(value, list):
        for child in value:
            _finite_tree(child)
        return
    if isinstance(value, dict):
        for child in value.values():
            _finite_tree(child)
        return
    raise CausalContractError("SPEC_SCHEMA", type(value).__name__)


def _number(value, minimum, maximum, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CausalContractError("NONFINITE_NUMBER", label)
    if not minimum <= value <= maximum:
        raise CausalContractError("SPEC_SCHEMA", label)


def _vector(value, length, minimum, maximum, label):
    if not isinstance(value, list) or len(value) != length:
        raise CausalContractError("SPEC_SCHEMA", label)
    for child in value:
        _number(child, minimum, maximum, label)


def _validate_rigid_body(value, expected_shape):
    _exact_keys(value, {"mass", "collisionShape", "friction", "restitution", "linearDamping", "angularDamping"}, "/rigidBody")
    if value["collisionShape"] not in ALLOWED_COLLISION_SHAPES or value["collisionShape"] != expected_shape:
        raise CausalContractError("UNSUPPORTED_COLLISION_SHAPE", value["collisionShape"])
    _number(value["mass"], 0.001, 1000.0, "mass")
    for key in ("friction", "restitution", "linearDamping", "angularDamping"):
        _number(value[key], 0.0, 1.0, key)


def _physical_identity(document):
    actor = document["dynamicActor"]
    targets = document["targetGroup"]
    return {
        "dynamicActor": {
            "radius": actor["radius"],
            "initialPosition": actor["initialPosition"],
            "launchWaypoints": actor["launchWaypoints"],
            "rigidBody": actor["rigidBody"],
        },
        "targetGroup": {
            "factory": targets["factory"],
            "count": targets["count"],
            "initialBasePositions": targets["initialBasePositions"],
            "physicalArchetype": targets["physicalArchetype"],
            "rigidBody": targets["rigidBody"],
        },
    }


def _validate_metric_rigid_body(value, expected_shape, actor=False):
    keys = {"collisionShape", "collisionMarginMeters", "friction", "restitution", "linearDamping", "angularDamping"}
    if actor:
        keys.add("mass")
    _exact_keys(value, keys, "/rigidBody")
    if value["collisionShape"] != expected_shape or value["collisionShape"] not in ALLOWED_COLLISION_SHAPES:
        raise CausalContractError("UNSUPPORTED_COLLISION_SHAPE", value["collisionShape"])
    if actor:
        _number(value["mass"], 0.001, 1000.0, "mass")
    _number(value["collisionMarginMeters"], 0.0001, 0.01, "collision margin")
    for key in ("friction", "restitution", "linearDamping", "angularDamping"):
        _number(value[key], 0.0, 1.0, key)


def _validate_metric_bottles(targets):
    _exact_keys(targets, {"semanticRole", "factory", "count", "initialBasePositions", "physicalArchetype", "rigidBody", "modeling", "deterministicVariation", "bodyPalette", "labelPalette", "liquidPalette"}, "/targetGroup")
    if targets["semanticRole"] != "target_group" or targets["factory"] != "FILLED_LATHED_BOTTLE" or targets["count"] != 3:
        raise CausalContractError("UNSUPPORTED_FACTORY", str(targets.get("factory")))
    for key in ("initialBasePositions", "bodyPalette", "labelPalette", "liquidPalette"):
        if not isinstance(targets[key], list) or len(targets[key]) != targets["count"]:
            raise CausalContractError("TARGET_COUNT_OUT_OF_RANGE", key)
    for position in targets["initialBasePositions"]:
        _vector(position, 3, -100.0, 100.0, "target base position")
    for palette in (targets["bodyPalette"], targets["labelPalette"], targets["liquidPalette"]):
        for color in palette:
            _vector(color, 3, 0.0, 1.0, "target color")
    archetype = targets["physicalArchetype"]
    _exact_keys(archetype, {"heightMeters", "bodyRadiusMeters", "neckRadiusMeters", "interiorBaseHeightMeters", "liquidBodyHeightMeters", "containerMassKg", "nominalCapacityLiters", "liquidDensityKgPerLiter", "containerCenterOfMassHeightRatio", "fillFractions", "massStrategy", "centerOfMassStrategy"}, "/targetGroup/physicalArchetype")
    for key in ("heightMeters", "bodyRadiusMeters", "neckRadiusMeters", "interiorBaseHeightMeters", "liquidBodyHeightMeters", "containerMassKg", "nominalCapacityLiters", "liquidDensityKgPerLiter"):
        _number(archetype[key], 0.0001, 10.0, key)
    _number(archetype["containerCenterOfMassHeightRatio"], 0.05, 0.95, "container COM ratio")
    if not archetype["bodyRadiusMeters"] > archetype["neckRadiusMeters"] or archetype["liquidBodyHeightMeters"] > archetype["heightMeters"]:
        raise CausalContractError("SPEC_SCHEMA", "bottle proportions")
    if archetype["massStrategy"] != "CONTAINER_PLUS_LIQUID_FROM_FILL" or archetype["centerOfMassStrategy"] != "WEIGHTED_CONTAINER_AND_LIQUID_COLUMN":
        raise CausalContractError("SPEC_SCHEMA", "derived physical strategy")
    if not isinstance(archetype["fillFractions"], list) or len(archetype["fillFractions"]) != targets["count"]:
        raise CausalContractError("TARGET_COUNT_OUT_OF_RANGE", "fill fractions")
    for fill in archetype["fillFractions"]:
        _number(fill, 0.01, 0.99, "fill fraction")
    if len({round(fill, 8) for fill in archetype["fillFractions"]}) != targets["count"]:
        raise CausalContractError("SPEC_SCHEMA", "fill levels must be distinct")
    _validate_metric_rigid_body(targets["rigidBody"], "CONVEX_HULL")
    modeling = targets["modeling"]
    _exact_keys(modeling, {"profileSegments", "profileMeters", "wallThicknessMeters", "capHeightMeters", "capRadiusMeters", "capRidgeCount", "labelHeightRatio", "requiredReadableStages"}, "/targetGroup/modeling")
    if not isinstance(modeling["profileSegments"], int) or not 24 <= modeling["profileSegments"] <= 128 or not isinstance(modeling["capRidgeCount"], int) or not 8 <= modeling["capRidgeCount"] <= 64:
        raise CausalContractError("SPEC_SCHEMA", "bottle segments")
    if not isinstance(modeling["profileMeters"], list) or not 6 <= len(modeling["profileMeters"]) <= 32:
        raise CausalContractError("SPEC_SCHEMA", "bottle profile")
    previous_height = -1.0
    for radius, height in modeling["profileMeters"]:
        _number(radius, 0.001, archetype["bodyRadiusMeters"] * 1.1, "profile radius")
        _number(height, 0.0, archetype["heightMeters"], "profile height")
        if height <= previous_height:
            raise CausalContractError("SPEC_SCHEMA", "profile height order")
        previous_height = height
    if abs(modeling["profileMeters"][-1][1] - archetype["heightMeters"]) > 1e-9:
        raise CausalContractError("SPEC_SCHEMA", "profile height exact")
    for key in ("wallThicknessMeters", "capHeightMeters", "capRadiusMeters", "labelHeightRatio"):
        _number(modeling[key], 0.0001, 1.0, key)
    if modeling["requiredReadableStages"] != ["base", "body", "shoulder", "neck", "cap", "label", "liquid_fill"]:
        raise CausalContractError("SPEC_SCHEMA", "readable bottle stages")


def _derive_bottle_physics(archetype, fill_fraction):
    liquid_mass = archetype["nominalCapacityLiters"] * archetype["liquidDensityKgPerLiter"] * fill_fraction
    total_mass = archetype["containerMassKg"] + liquid_mass
    container_com = archetype["heightMeters"] * archetype["containerCenterOfMassHeightRatio"]
    liquid_com = archetype["interiorBaseHeightMeters"] + archetype["liquidBodyHeightMeters"] * fill_fraction / 2.0
    center_of_mass = (archetype["containerMassKg"] * container_com + liquid_mass * liquid_com) / total_mass
    return round(total_mass, 8), round(center_of_mass, 8), round(liquid_com, 8)


def _validate(document):
    top_keys = {"$schema", "schemaVersion", "sceneId", "title", "timeline", "dynamicActor", "targetGroup", "studio", "shots", "acceptance", "forbidden", "sceneSpecHash"}
    if document.get("schemaVersion") in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        top_keys.add("cinematography")
    _exact_keys(document, top_keys, "/")
    _finite_tree(document)
    version = document["schemaVersion"]
    if version not in SPEC_VERSIONS or document["sceneSpecHash"] != _self_hash(document, "sceneSpecHash"):
        raise CausalContractError("SPEC_HASH", "CausalSceneSpec self hash or version is invalid")
    _exact_keys(document["timeline"], {"fps", "frameStart", "frameEnd", "releaseFrame"}, "/timeline")
    timeline = document["timeline"]
    if not (timeline["fps"] in range(1, 121) and timeline["frameStart"] == 1 and 3 <= timeline["releaseFrame"] <= timeline["frameEnd"] <= 1000):
        raise CausalContractError("SPEC_SCHEMA", "timeline")
    actor = document["dynamicActor"]
    _exact_keys(actor, {"semanticRole", "factory", "count", "radius", "initialPosition", "launchWaypoints", "rigidBody", "material"}, "/dynamicActor")
    if actor["semanticRole"] != "dynamic_actor" or actor["factory"] != "GROOVED_SPHERE" or actor["factory"] not in ALLOWED_FACTORIES or actor["count"] != 1:
        raise CausalContractError("UNSUPPORTED_FACTORY", str(actor.get("factory")))
    _number(actor["radius"], 0.03, 5.0, "actor radius")
    _vector(actor["initialPosition"], 3, -100.0, 100.0, "actor position")
    if not isinstance(actor["launchWaypoints"], list) or not 2 <= len(actor["launchWaypoints"]) <= 16:
        raise CausalContractError("SPEC_SCHEMA", "launchWaypoints")
    previous = 0
    for waypoint in actor["launchWaypoints"]:
        _exact_keys(waypoint, {"frame", "position", "rotationY"}, "/dynamicActor/launchWaypoints")
        if not isinstance(waypoint["frame"], int) or not previous < waypoint["frame"] < timeline["releaseFrame"]:
            raise CausalContractError("FINAL_POSE_AUTHORITY", "Waypoints must be ordered before release")
        previous = waypoint["frame"]
        _vector(waypoint["position"], 3, -100.0, 100.0, "waypoint position")
        _number(waypoint["rotationY"], -1000.0, 1000.0, "waypoint rotation")
    if version == "bfs.causalSceneSpec.v0.5":
        _validate_metric_rigid_body(actor["rigidBody"], "SPHERE", actor=True)
    else:
        _validate_rigid_body(actor["rigidBody"], "SPHERE")
    _exact_keys(actor["material"], {"baseColor", "roughness", "proceduralGrain", "channelCount"}, "/dynamicActor/material")
    _vector(actor["material"]["baseColor"], 3, 0.0, 1.0, "actor color")
    if actor["material"]["proceduralGrain"] is not True or actor["material"]["channelCount"] != 3:
        raise CausalContractError("SPEC_SCHEMA", "actor material")

    targets = document["targetGroup"]
    if version == "bfs.causalSceneSpec.v0.5":
        _validate_metric_bottles(targets)
    else:
        target_keys = {"semanticRole", "factory", "count", "dimensions", "initialPositions", "rigidBody", "modeling", "palette"}
        if version in {"bfs.causalSceneSpec.v0.2", "bfs.causalSceneSpec.v0.4"}:
            target_keys.add("deterministicVariation")
        _exact_keys(targets, target_keys, "/targetGroup")
        if targets["semanticRole"] != "target_group" or targets["factory"] != "BEVELED_DOMINO_BLOCK" or targets["factory"] not in ALLOWED_FACTORIES:
            raise CausalContractError("UNSUPPORTED_FACTORY", str(targets.get("factory")))
        if not isinstance(targets["count"], int) or not 1 <= targets["count"] <= 16:
            raise CausalContractError("TARGET_COUNT_OUT_OF_RANGE", str(targets.get("count")))
        if len(targets["initialPositions"]) != targets["count"] or len(targets["palette"]) != targets["count"]:
            raise CausalContractError("TARGET_COUNT_OUT_OF_RANGE", "positions/palette")
        _vector(targets["dimensions"], 3, 0.02, 10.0, "target dimensions")
        for position in targets["initialPositions"]:
            _vector(position, 3, -100.0, 100.0, "target position")
        for color in targets["palette"]:
            _vector(color, 3, 0.0, 1.0, "target color")
        _validate_rigid_body(targets["rigidBody"], "BOX")
        _exact_keys(targets["modeling"], {"bevelWidth", "bevelSegments", "insetFacePanel", "edgeBand"}, "/targetGroup/modeling")
        if targets["modeling"]["insetFacePanel"] is not True or targets["modeling"]["edgeBand"] is not True:
            raise CausalContractError("SPEC_SCHEMA", "target modeling")
    if version in {"bfs.causalSceneSpec.v0.2", "bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        variation = targets["deterministicVariation"]
        variation_keys = {"seed", "positionJitterMetersMaximum", "yawJitterDegreesMaximum", "frictionJitterMaximum", "restitutionJitterMaximum"}
        if version in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
            variation_keys.add("basisSceneSpecHash")
        _exact_keys(variation, variation_keys, "/targetGroup/deterministicVariation")
        if not isinstance(variation["seed"], int) or isinstance(variation["seed"], bool) or not 0 <= variation["seed"] <= 2147483647:
            raise CausalContractError("SPEC_SCHEMA", "variation seed")
        if version in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"} and (not isinstance(variation["basisSceneSpecHash"], str) or len(variation["basisSceneSpecHash"]) != 64 or any(character not in "0123456789abcdef" for character in variation["basisSceneSpecHash"])):
            raise CausalContractError("SPEC_SCHEMA", "variation basis hash")
        _number(variation["positionJitterMetersMaximum"], 0.0, 0.1, "position jitter")
        _number(variation["yawJitterDegreesMaximum"], 0.0, 10.0, "yaw jitter")
        _number(variation["frictionJitterMaximum"], 0.0, 0.2, "friction jitter")
        _number(variation["restitutionJitterMaximum"], 0.0, 0.2, "restitution jitter")
        if version == "bfs.causalSceneSpec.v0.5" and variation["basisSceneSpecHash"] != _sha256(_canonical(_physical_identity(document)).encode("utf-8")):
            raise CausalContractError("SPEC_HASH", "physical variation basis differs")

    studio = document["studio"]
    _exact_keys(studio, {"ground", "backdrop", "lights"}, "/studio")
    if studio["backdrop"].get("factory") != "SIMPLE_WALL" or studio["backdrop"].get("participatesInPhysics") is not False:
        raise CausalContractError("UNSUPPORTED_FACTORY", "backdrop")
    if len(studio["lights"]) != 3 or any(light.get("kind") != "AREA" for light in studio["lights"]):
        raise CausalContractError("UNSUPPORTED_FACTORY", "lights")
    if version in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        cinematography = document["cinematography"]
        _exact_keys(cinematography, {"motionBlur"}, "/cinematography")
        motion_blur = cinematography["motionBlur"]
        _exact_keys(motion_blur, {"strategy", "semanticRoles", "measurementResolution", "targetBlurPixels", "minimumShutterFrames", "maximumShutterFrames", "position"}, "/cinematography/motionBlur")
        if motion_blur["strategy"] != "MEASURED_PROJECTED_MEDIAN_MOTION" or motion_blur["semanticRoles"] != ["dynamic_actor", "target_group"]:
            raise CausalContractError("SPEC_SCHEMA", "motion blur measurement authority")
        if motion_blur["measurementResolution"] != [960, 540] or motion_blur["position"] != "CENTER":
            raise CausalContractError("SPEC_SCHEMA", "motion blur measurement contract")
        _number(motion_blur["targetBlurPixels"], 1.0, 24.0, "target blur pixels")
        _number(motion_blur["minimumShutterFrames"], 0.05, 1.0, "minimum shutter")
        _number(motion_blur["maximumShutterFrames"], 0.05, 1.0, "maximum shutter")
        if motion_blur["minimumShutterFrames"] > motion_blur["maximumShutterFrames"]:
            raise CausalContractError("SPEC_SCHEMA", "shutter bounds")
    shots = document["shots"]
    if not isinstance(shots, list) or [shot.get("shotId") for shot in shots] != ["SETUP", "IMPACT", "AFTERMATH"]:
        raise CausalContractError("SPEC_SCHEMA", "shots")
    for shot in shots:
        _exact_keys(shot, {"shotId", "selection", "lensMm", "directionClass", "targetOccupancy"}, "/shots")
        if shot["directionClass"] not in DIRECTION_CAMERA:
            raise CausalContractError("UNSUPPORTED_FACTORY", "camera direction")
        _number(shot["targetOccupancy"], 0.3, 0.85, "target occupancy")
    expected_selections = {
        "bfs.causalSceneSpec.v0.1": ["last frame before first target response", "first target response frame", "frameEnd"],
        "bfs.causalSceneSpec.v0.2": ["last frame before first target response", "peak propagated angular motion frame", "first settled frame after all targets respond"],
        "bfs.causalSceneSpec.v0.4": ["last frame before first target response", "peak propagated angular motion frame", "first settled frame after all targets respond"],
        "bfs.causalSceneSpec.v0.5": ["last frame before first target response", "peak propagated angular motion frame", "first settled frame after all targets respond"],
    }[version]
    if [shot["selection"] for shot in shots] != expected_selections:
        raise CausalContractError("SPEC_SCHEMA", "shot selection")
    acceptance = document["acceptance"]
    acceptance_keys = {"initialPenetrationMaximumMeters", "actorForwardTravelBeforeFirstResponseMinimumMeters", "firstTargetResponseFrameWindowInclusive", "targetTiltDegreesAtFinalMinimumEach", "finiteTransformRequired", "reopenResponseFramesExact", "reopenFinalTiltToleranceDegrees", "postReleaseActorPoseKeyframes", "targetPoseKeyframes", "reviewOccupancyRange", "reviewNegativeSpaceMarginMinimum"}
    if version in {"bfs.causalSceneSpec.v0.2", "bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        acceptance_keys.update({"impactActiveTargetCountMinimum", "impactFrameAfterFirstResponseMinimum", "settleAngularStepDegreesMaximum", "settleConsecutiveFrames", "deterministicVariationRequired", "impactClipFrameCount"})
    if version in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        acceptance_keys.update({"measuredMedianMotionPixelsPerFrameRange", "computedShutterFramesRange", "computedBlurTargetErrorPixelsMaximum", "blurredImpactMustDifferFromSharpControl"})
    if version == "bfs.causalSceneSpec.v0.5":
        acceptance_keys.update({"targetsTiltedAtLeast60DegreesMinimumCount", "metricScaleRequired", "derivedMassesKgExact", "derivedCenterOfMassHeightsMetersExact", "recognizableBottleDetailObjectsMinimumEach", "distinctVisibleFillLevelsMinimum", "collisionShapeMustMatchVisibleBottleHull"})
    _exact_keys(acceptance, acceptance_keys, "/acceptance")
    if acceptance["postReleaseActorPoseKeyframes"] != 0 or acceptance["targetPoseKeyframes"] != 0:
        raise CausalContractError("FINAL_POSE_AUTHORITY", "Final pose keyframes are forbidden")
    if version in {"bfs.causalSceneSpec.v0.2", "bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        if acceptance["deterministicVariationRequired"] is not True:
            raise CausalContractError("SPEC_SCHEMA", "deterministic variation")
        if not isinstance(acceptance["impactActiveTargetCountMinimum"], int) or not 1 <= acceptance["impactActiveTargetCountMinimum"] <= targets["count"]:
            raise CausalContractError("SPEC_SCHEMA", "impact active target count")
        if not isinstance(acceptance["impactFrameAfterFirstResponseMinimum"], int) or not 1 <= acceptance["impactFrameAfterFirstResponseMinimum"] <= 120:
            raise CausalContractError("SPEC_SCHEMA", "impact frame delay")
        _number(acceptance["settleAngularStepDegreesMaximum"], 0.0, 5.0, "settle angular step")
        if not isinstance(acceptance["settleConsecutiveFrames"], int) or not 2 <= acceptance["settleConsecutiveFrames"] <= 48:
            raise CausalContractError("SPEC_SCHEMA", "settle frames")
        if not isinstance(acceptance["impactClipFrameCount"], int) or not 8 <= acceptance["impactClipFrameCount"] <= 96:
            raise CausalContractError("SPEC_SCHEMA", "impact clip frames")
    if version in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        _vector(acceptance["measuredMedianMotionPixelsPerFrameRange"], 2, 0.0, 1000.0, "measured motion range")
        _vector(acceptance["computedShutterFramesRange"], 2, 0.0, 1.0, "computed shutter range")
        if acceptance["measuredMedianMotionPixelsPerFrameRange"][0] > acceptance["measuredMedianMotionPixelsPerFrameRange"][1] or acceptance["computedShutterFramesRange"][0] > acceptance["computedShutterFramesRange"][1]:
            raise CausalContractError("SPEC_SCHEMA", "acceptance ranges")
        _number(acceptance["computedBlurTargetErrorPixelsMaximum"], 0.0, 1.0, "blur target error")
        if acceptance["blurredImpactMustDifferFromSharpControl"] is not True:
            raise CausalContractError("SPEC_SCHEMA", "blurred impact control")
    if version == "bfs.causalSceneSpec.v0.5":
        if acceptance["metricScaleRequired"] is not True or acceptance["collisionShapeMustMatchVisibleBottleHull"] is not True:
            raise CausalContractError("SPEC_SCHEMA", "metric physical archetype")
        derived = [_derive_bottle_physics(targets["physicalArchetype"], fill) for fill in targets["physicalArchetype"]["fillFractions"]]
        if acceptance["derivedMassesKgExact"] != [row[0] for row in derived] or acceptance["derivedCenterOfMassHeightsMetersExact"] != [row[1] for row in derived]:
            raise CausalContractError("SPEC_SCHEMA", "frozen derived physical values")
        if acceptance["targetsTiltedAtLeast60DegreesMinimumCount"] not in range(1, targets["count"] + 1) or acceptance["recognizableBottleDetailObjectsMinimumEach"] < 4 or acceptance["distinctVisibleFillLevelsMinimum"] != targets["count"]:
            raise CausalContractError("SPEC_SCHEMA", "physical archetype acceptance")
    forbidden = document["forbidden"]
    forbidden_keys = {"acceptedBottleFactory", "acceptedBottleFinalCoordinates", "projectSpecificCameraCoordinates", "externalModelsOrTextures", "manualTargetOrFinalPoseAnimation"}
    if version == "bfs.causalSceneSpec.v0.4":
        forbidden_keys.update({"manualShutterValue", "compositorOrPostprocessBlur", "effectCoverForWeakerPrimaryPhysics"})
    if version == "bfs.causalSceneSpec.v0.5":
        forbidden_keys = {"acceptedBottleFinalCoordinates", "projectSpecificCameraCoordinates", "externalModelsOrTextures", "manualTargetOrFinalPoseAnimation", "manualShutterValue", "compositorOrPostprocessBlur", "effectCoverForWeakerPrimaryPhysics", "manualPerTargetMassOrCenterOfMass", "nonMetricScaleSubstitution", "decorativeCollisionProxyThatDiffersFromVisibleBottle", "liquidSimulationClaim"}
    _exact_keys(forbidden, forbidden_keys, "/forbidden")
    if not all(value is True for value in forbidden.values()):
        raise CausalContractError("SPEC_EXECUTABLE_AUTHORITY", "All authority denials must remain true")
    return document


def inspect_causal_scene(repository_root, scene_spec_uri):
    root = Path(repository_root).resolve(strict=True)
    path = _below_existing(root, scene_spec_uri)
    document = _validate(_read_json(path))
    file_hash = _sha256(path.read_bytes())
    token_body = {"fileSha256": file_hash, "sceneSpecHash": document["sceneSpecHash"], "contractVersion": CONTRACT_VERSION}
    return {
        "status": "APPROVED_READY",
        "sceneId": document["sceneId"],
        "actorFactory": document["dynamicActor"]["factory"],
        "targetFactory": document["targetGroup"]["factory"],
        "targetCount": document["targetGroup"]["count"],
        "collisionShapes": [document["dynamicActor"]["rigidBody"]["collisionShape"], document["targetGroup"]["rigidBody"]["collisionShape"]],
        "finalPoseSource": "BLENDER_BULLET_RIGID_BODY",
        "cameraFitSource": "EVALUATED_FRAME_SEMANTIC_WORLD_BOUNDS",
        "sceneSpecHash": document["sceneSpecHash"],
        "fileSha256": file_hash,
        "inspectionToken": _sha256(_canonical(token_body).encode("utf-8")),
    }


def _material(name, color, roughness, metallic=0.0):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = (*color, 1.0)
    principled.inputs["Roughness"].default_value = roughness
    principled.inputs["Metallic"].default_value = metallic
    return material


def _grain_material(name, color):
    material = _material(name, color, 0.34)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 8.5
    noise.inputs["Detail"].default_value = 3.0
    noise.inputs["Roughness"].default_value = 0.62
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.16
    bump.inputs["Distance"].default_value = 0.025
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], nodes.get("Principled BSDF").inputs["Normal"])
    return material


def _smooth(obj):
    for polygon in obj.data.polygons if obj.type == "MESH" else []:
        polygon.use_smooth = True


def _preserve_parent(child, parent):
    matrix = child.matrix_world.copy()
    child.parent = parent
    child.matrix_world = matrix


def _action_fcurves(obj):
    action = obj.animation_data.action if obj.animation_data and obj.animation_data.action else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    return [curve for layer in action.layers for strip in layer.strips for bag in strip.channelbags for curve in bag.fcurves]


def _add_rigid_body(obj, kind, spec):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.rigidbody.object_add()
    body = obj.rigid_body
    body.type = kind
    body.collision_shape = spec.get("collisionShape", "BOX")
    body.mass = spec.get("mass", 1.0)
    body.friction = spec.get("friction", 0.5)
    body.restitution = spec.get("restitution", 0.0)
    body.linear_damping = spec.get("linearDamping", 0.04)
    body.angular_damping = spec.get("angularDamping", 0.1)
    body.use_margin = True
    body.collision_margin = spec.get("collisionMarginMeters", 0.002)
    obj.select_set(False)
    return body


def _point_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def _clear_scene(scene):
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        if collection.users == 0:
            bpy.data.collections.remove(collection)


def _create_actor(document, dark):
    spec = document["dynamicActor"]
    radius = float(spec["radius"])
    location = tuple(spec["initialPosition"])
    actor_material = _material("MAT_CausalActor", tuple(spec["material"]["baseColor"]), float(spec["material"]["roughness"]))
    nodes, links = actor_material.node_tree.nodes, actor_material.node_tree.links
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 105.0
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.16
    bump.inputs["Distance"].default_value = 0.018
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], nodes.get("Principled BSDF").inputs["Normal"])
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=radius, location=location)
    actor = bpy.context.object
    actor.name = "CAUSAL_ACTOR_001"
    actor.data.materials.append(actor_material)
    actor["film_studio_semantic_role"] = "dynamic_actor"
    actor["film_studio_factory"] = spec["factory"]
    _smooth(actor)
    details = []
    metric_actor = document["schemaVersion"] == "bfs.causalSceneSpec.v0.5"
    seam_radius = radius * 0.022 if metric_actor else 0.009
    seam_major_radius = radius + seam_radius * 0.2 if metric_actor else radius + 0.002
    for index, rotation in enumerate(((0, 0, 0), (math.pi / 2, 0, 0), (0, math.pi / 2, 0)), 1):
        bpy.ops.mesh.primitive_torus_add(major_radius=seam_major_radius, minor_radius=seam_radius, major_segments=96, minor_segments=10, location=location, rotation=rotation)
        seam = bpy.context.object
        seam.name = f"CAUSAL_DETAIL_ActorChannel_{index:02d}"
        seam.data.materials.append(dark)
        seam["film_studio_semantic_role"] = "modeling_detail"
        _preserve_parent(seam, actor)
        details.append(seam)
    body = _add_rigid_body(actor, "ACTIVE", spec["rigidBody"])
    body.kinematic = True
    for waypoint in spec["launchWaypoints"]:
        actor.location = tuple(waypoint["position"])
        actor.rotation_euler = (0.0, waypoint["rotationY"], 0.0)
        actor.keyframe_insert(data_path="location", frame=waypoint["frame"])
        actor.keyframe_insert(data_path="rotation_euler", frame=waypoint["frame"])
    release = document["timeline"]["releaseFrame"]
    body.kinematic = True
    actor.keyframe_insert(data_path="rigid_body.kinematic", frame=release - 1)
    body.kinematic = False
    actor.keyframe_insert(data_path="rigid_body.kinematic", frame=release)
    for curve in _action_fcurves(actor):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"
    return actor, details


def _lathed_mesh(name, profile, segments, center_of_mass):
    vertices = []
    for radius, height in profile:
        for index in range(segments):
            angle = 2.0 * math.pi * index / segments
            vertices.append((radius * math.cos(angle), radius * math.sin(angle), height - center_of_mass))
    faces = []
    for ring in range(len(profile) - 1):
        for index in range(segments):
            nxt = (index + 1) % segments
            a = ring * segments + index
            b = ring * segments + nxt
            c = (ring + 1) * segments + nxt
            d = (ring + 1) * segments + index
            faces.append((a, b, c, d))
    bottom = len(vertices)
    top = bottom + 1
    vertices.extend(((0.0, 0.0, profile[0][1] - center_of_mass), (0.0, 0.0, profile[-1][1] - center_of_mass)))
    for index in range(segments):
        nxt = (index + 1) % segments
        faces.append((bottom, nxt, index))
        top_a = (len(profile) - 1) * segments + index
        top_b = (len(profile) - 1) * segments + nxt
        faces.append((top, top_a, top_b))
    mesh = bpy.data.meshes.new(f"{name}_MESH")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return mesh


def _bottle_shell_material(name, color):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    glass = nodes.new("ShaderNodeBsdfGlass")
    glass.inputs["Color"].default_value = (*color, 1.0)
    glass.inputs["Roughness"].default_value = 0.12
    glass.inputs["IOR"].default_value = 1.46
    material.node_tree.links.new(glass.outputs["BSDF"], output.inputs["Surface"])
    material.diffuse_color = (*color, 1.0)
    material.use_screen_refraction = True
    material.use_raytrace_refraction = True
    return material


def _create_metric_bottle_targets(document, dark):
    spec = document["targetGroup"]
    archetype = spec["physicalArchetype"]
    modeling = spec["modeling"]
    targets, details, initial_conditions = [], [], []
    for index, (base, body_color, label_color, liquid_color, fill) in enumerate(zip(spec["initialBasePositions"], spec["bodyPalette"], spec["labelPalette"], spec["liquidPalette"], archetype["fillFractions"]), 1):
        variation = spec["deterministicVariation"]
        def sample(channel):
            token = f"{variation['basisSceneSpecHash']}:{variation['seed']}:{index}:{channel}".encode("utf-8")
            integer = int.from_bytes(hashlib.sha256(token).digest()[:8], "big")
            return integer / (2 ** 64 - 1) * 2.0 - 1.0
        position_x = base[0] + sample("position-x") * variation["positionJitterMetersMaximum"]
        position_y = base[1] + sample("position-y") * variation["positionJitterMetersMaximum"]
        yaw_degrees = sample("yaw") * variation["yawJitterDegreesMaximum"]
        friction = max(0.0, min(1.0, spec["rigidBody"]["friction"] + sample("friction") * variation["frictionJitterMaximum"]))
        restitution = max(0.0, min(1.0, spec["rigidBody"]["restitution"] + sample("restitution") * variation["restitutionJitterMaximum"]))
        mass, center_of_mass, liquid_center = _derive_bottle_physics(archetype, fill)
        yaw = math.radians(yaw_degrees)
        location = (position_x, position_y, base[2] + center_of_mass)
        mesh = _lathed_mesh(f"CAUSAL_TARGET_{index:03d}", modeling["profileMeters"], modeling["profileSegments"], center_of_mass)
        target = bpy.data.objects.new(f"CAUSAL_TARGET_{index:03d}", mesh)
        bpy.context.collection.objects.link(target)
        target.location = location
        target.rotation_euler.z = yaw
        target.data.materials.append(_bottle_shell_material(f"MAT_CausalBottleShell_{index:02d}", tuple(body_color)))
        target["film_studio_semantic_role"] = "target_group"
        target["film_studio_factory"] = spec["factory"]
        target["film_studio_fill_fraction"] = fill
        target["film_studio_mass_kg"] = mass
        target["film_studio_center_of_mass_height_m"] = center_of_mass
        target["film_studio_collision_visible_body_match"] = True
        _smooth(target)
        bevel = target.modifiers.new("Bottle_Edge_Soften", "BEVEL")
        bevel.width, bevel.segments = modeling["wallThicknessMeters"], 2
        solidify = target.modifiers.new("Bottle_Wall_Thickness", "SOLIDIFY")
        solidify.thickness = modeling["wallThicknessMeters"]

        fill_height = archetype["liquidBodyHeightMeters"] * fill
        bpy.ops.mesh.primitive_cylinder_add(vertices=modeling["profileSegments"], radius=archetype["bodyRadiusMeters"] * 0.86, depth=fill_height, location=(position_x, position_y, base[2] + archetype["interiorBaseHeightMeters"] + fill_height / 2.0), rotation=(0.0, 0.0, yaw))
        liquid = bpy.context.object
        liquid.name = f"CAUSAL_DETAIL_BottleLiquid_{index:02d}"
        liquid.data.materials.append(_material(f"MAT_CausalBottleLiquid_{index:02d}", tuple(liquid_color), 0.2))
        liquid["film_studio_semantic_role"] = "physical_state_detail"
        liquid["film_studio_fill_fraction"] = fill
        liquid_bevel = liquid.modifiers.new("Liquid_Meniscus_Edge", "BEVEL")
        liquid_bevel.width, liquid_bevel.segments = modeling["wallThicknessMeters"] * 0.5, 2
        _preserve_parent(liquid, target)

        label_height = archetype["heightMeters"] * modeling["labelHeightRatio"]
        label_center = archetype["heightMeters"] * 0.45
        bpy.ops.mesh.primitive_cylinder_add(vertices=modeling["profileSegments"], radius=archetype["bodyRadiusMeters"] + modeling["wallThicknessMeters"] * 0.6, depth=label_height, location=(position_x, position_y, base[2] + label_center), rotation=(0.0, 0.0, yaw))
        label = bpy.context.object
        label.name = f"CAUSAL_DETAIL_BottleLabel_{index:02d}"
        label.data.materials.append(_material(f"MAT_CausalBottleLabel_{index:02d}", tuple(label_color), 0.34))
        label["film_studio_semantic_role"] = "modeling_detail"
        _preserve_parent(label, target)

        cap_center = base[2] + archetype["heightMeters"] - modeling["capHeightMeters"] / 2.0
        bpy.ops.mesh.primitive_cylinder_add(vertices=modeling["capRidgeCount"] * 2, radius=modeling["capRadiusMeters"], depth=modeling["capHeightMeters"], location=(position_x, position_y, cap_center), rotation=(0.0, 0.0, yaw))
        cap = bpy.context.object
        cap.name = f"CAUSAL_DETAIL_BottleCap_{index:02d}"
        cap.data.materials.append(dark)
        cap["film_studio_semantic_role"] = "modeling_detail"
        cap["film_studio_cap_ridge_count"] = modeling["capRidgeCount"]
        cap_bevel = cap.modifiers.new("Cap_Edge", "BEVEL")
        cap_bevel.width, cap_bevel.segments = modeling["wallThicknessMeters"] * 1.5, 2
        _preserve_parent(cap, target)

        bpy.ops.mesh.primitive_torus_add(major_radius=archetype["bodyRadiusMeters"] * 0.88, minor_radius=modeling["wallThicknessMeters"], major_segments=modeling["profileSegments"], minor_segments=8, location=(position_x, position_y, base[2] + modeling["wallThicknessMeters"] * 2.0), rotation=(0.0, 0.0, yaw))
        base_ring = bpy.context.object
        base_ring.name = f"CAUSAL_DETAIL_BottleBaseRing_{index:02d}"
        base_ring.data.materials.append(dark)
        base_ring["film_studio_semantic_role"] = "modeling_detail"
        _preserve_parent(base_ring, target)

        body_spec = {**spec["rigidBody"], "mass": mass, "friction": friction, "restitution": restitution}
        _add_rigid_body(target, "ACTIVE", body_spec)
        initial_conditions.append({
            "target": target.name,
            "basePosition": [round(position_x, 8), round(position_y, 8), round(base[2], 8)],
            "yawDegrees": round(yaw_degrees, 8),
            "fillFraction": round(fill, 8),
            "derivedMassKg": mass,
            "derivedCenterOfMassHeightMeters": center_of_mass,
            "liquidCenterOfMassHeightMeters": liquid_center,
            "visibleLiquidSurfaceHeightMeters": round(archetype["interiorBaseHeightMeters"] + fill_height, 8),
            "friction": round(friction, 8),
            "restitution": round(restitution, 8),
            "collisionShape": body_spec["collisionShape"],
            "collisionMarginMeters": body_spec["collisionMarginMeters"],
            "visibleBodyIsCollisionHullSource": True,
            "detailObjectCount": 4,
        })
        targets.append(target)
        details.extend((liquid, label, cap, base_ring))
    return targets, details, initial_conditions


def _create_targets(document, dark):
    spec = document["targetGroup"]
    if spec["factory"] == "FILLED_LATHED_BOTTLE":
        return _create_metric_bottle_targets(document, dark)
    dimensions = tuple(spec["dimensions"])
    targets, details, initial_conditions = [], [], []
    for index, (position, color) in enumerate(zip(spec["initialPositions"], spec["palette"]), 1):
        variation = spec.get("deterministicVariation")
        derived = {"positionX": float(position[0]), "positionY": float(position[1]), "yawDegrees": 0.0, "friction": float(spec["rigidBody"]["friction"]), "restitution": float(spec["rigidBody"]["restitution"])}
        if variation:
            variation_basis = variation.get("basisSceneSpecHash", document["sceneSpecHash"])
            def sample(channel):
                token = f"{variation_basis}:{variation['seed']}:{index}:{channel}".encode("utf-8")
                integer = int.from_bytes(hashlib.sha256(token).digest()[:8], "big")
                return integer / (2 ** 64 - 1) * 2.0 - 1.0
            derived["positionX"] += sample("position-x") * variation["positionJitterMetersMaximum"]
            derived["positionY"] += sample("position-y") * variation["positionJitterMetersMaximum"]
            derived["yawDegrees"] = sample("yaw") * variation["yawJitterDegreesMaximum"]
            derived["friction"] = max(0.0, min(1.0, derived["friction"] + sample("friction") * variation["frictionJitterMaximum"]))
            derived["restitution"] = max(0.0, min(1.0, derived["restitution"] + sample("restitution") * variation["restitutionJitterMaximum"]))
        derived = {key: round(value, 8) for key, value in derived.items()}
        target_position = (derived["positionX"], derived["positionY"], float(position[2]))
        yaw = math.radians(derived["yawDegrees"])
        bpy.ops.mesh.primitive_cube_add(location=target_position, rotation=(0.0, 0.0, yaw))
        target = bpy.context.object
        target.name = f"CAUSAL_TARGET_{index:03d}"
        target.dimensions = dimensions
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        target.data.materials.append(_grain_material(f"MAT_CausalTarget_{index:02d}", tuple(color)))
        target["film_studio_semantic_role"] = "target_group"
        target["film_studio_factory"] = spec["factory"]
        bevel = target.modifiers.new("Manufactured_Bevel", "BEVEL")
        bevel.width = spec["modeling"]["bevelWidth"]
        bevel.segments = spec["modeling"]["bevelSegments"]
        panel_material = _material(f"MAT_CausalPanel_{index:02d}", tuple(min(1.0, value * 1.28) for value in color), 0.28)
        panel_position = target.matrix_world @ Vector((0.0, -dimensions[1] / 2 - 0.012, 0.0))
        bpy.ops.mesh.primitive_cube_add(location=panel_position, rotation=(0.0, 0.0, yaw))
        panel = bpy.context.object
        panel.name = f"CAUSAL_DETAIL_TargetPanel_{index:02d}"
        panel.dimensions = (dimensions[0] * 0.72, 0.018, dimensions[2] * 0.44)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        panel.data.materials.append(panel_material)
        panel["film_studio_semantic_role"] = "modeling_detail"
        panel_bevel = panel.modifiers.new("Panel_Bevel", "BEVEL")
        panel_bevel.width, panel_bevel.segments = 0.018, 3
        _preserve_parent(panel, target)
        band_position = target.matrix_world @ Vector((0.0, 0.0, -dimensions[2] * 0.29))
        bpy.ops.mesh.primitive_cube_add(location=band_position, rotation=(0.0, 0.0, yaw))
        band = bpy.context.object
        band.name = f"CAUSAL_DETAIL_TargetBand_{index:02d}"
        band.dimensions = (dimensions[0] + 0.012, dimensions[1] + 0.012, 0.055)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        band.data.materials.append(dark)
        band["film_studio_semantic_role"] = "modeling_detail"
        _preserve_parent(band, target)
        body_spec = {**spec["rigidBody"], "friction": derived["friction"], "restitution": derived["restitution"]}
        _add_rigid_body(target, "ACTIVE", body_spec)
        initial_conditions.append({"target": target.name, **derived})
        targets.append(target)
        details.extend((panel, band))
    return targets, details, initial_conditions


def _create_studio(document):
    studio = document["studio"]
    floor_mat = _material("MAT_CausalFloor", (0.105, 0.125, 0.155), 0.29)
    wall_mat = _material("MAT_CausalWall", (0.055, 0.070, 0.105), 0.42)
    dark = _material("MAT_CausalDarkDetail", (0.008, 0.009, 0.012), 0.38)
    bpy.ops.mesh.primitive_plane_add(size=studio["ground"]["size"], location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "CAUSAL_GROUND"
    floor.data.materials.append(floor_mat)
    floor["film_studio_semantic_role"] = "ground"
    _add_rigid_body(floor, "PASSIVE", {"collisionShape": "BOX", "mass": 1.0, "friction": studio["ground"]["friction"], "restitution": 0.0, "linearDamping": 0.0, "angularDamping": 0.0})
    bpy.ops.mesh.primitive_cube_add(location=(0.0, 5.1, 3.25), scale=(10.0, 0.10, 3.25))
    wall = bpy.context.object
    wall.name = "CAUSAL_ENVIRONMENT"
    wall.data.materials.append(wall_mat)
    wall["film_studio_semantic_role"] = "studio_environment"
    lights = []
    positions = ((-2.7, -3.4, 5.8), (4.2, -1.0, 3.2), (1.7, 3.5, 4.7))
    sizes = (3.2, 4.2, 2.5)
    for index, (spec, position, size) in enumerate(zip(studio["lights"], positions, sizes), 1):
        data = bpy.data.lights.new(f"CAUSAL_LIGHT_{index:02d}_DATA", "AREA")
        data.energy, data.color, data.shape, data.size = spec["energy"], tuple(spec["color"]), "DISK", size
        light = bpy.data.objects.new(f"CAUSAL_LIGHT_{index:02d}", data)
        bpy.context.collection.objects.link(light)
        light.location = position
        _point_at(light, (0.3, 0.0, 0.55))
        light["film_studio_semantic_role"] = spec["semanticRole"]
        lights.append(light)
    return floor, wall, lights, dark


def _create_cameras(document):
    cameras = {}
    for shot in document["shots"]:
        location, target = DIRECTION_CAMERA[shot["directionClass"]]
        data = bpy.data.cameras.new(f"CAUSAL_CAM_{shot['shotId']}_DATA")
        data.lens, data.sensor_width = shot["lensMm"], 36.0
        camera = bpy.data.objects.new(f"CAUSAL_CAM_{shot['shotId']}", data)
        bpy.context.collection.objects.link(camera)
        camera.location = location
        _point_at(camera, target)
        camera["film_studio_semantic_role"] = "camera"
        camera["film_studio_shot_id"] = shot["shotId"]
        cameras[shot["shotId"]] = camera
    return cameras


def _tilt(obj):
    axis = obj.matrix_world.to_3x3() @ Vector((0, 0, 1))
    return math.degrees(math.acos(max(-1.0, min(1.0, axis.normalized().dot(Vector((0, 0, 1)))))))


def _simulate(scene, actor, targets, document):
    initial_actor = Vector(document["dynamicActor"]["initialPosition"])
    scene.frame_set(document["timeline"]["frameStart"])
    bpy.context.view_layer.update()
    initial = {obj.name: obj.matrix_world.translation.copy() for obj in targets}
    response = {obj.name: None for obj in targets}
    previous_tilts = {obj.name: _tilt(obj) for obj in targets}
    motion = []
    for frame in range(document["timeline"]["frameStart"], document["timeline"]["frameEnd"] + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        angular_steps = {}
        for target in targets:
            location = target.matrix_world.translation
            tilt = _tilt(target)
            angular_steps[target.name] = abs(tilt - previous_tilts[target.name])
            previous_tilts[target.name] = tilt
            displacement = (Vector((location.x, location.y)) - Vector((initial[target.name].x, initial[target.name].y))).length
            if response[target.name] is None and (tilt >= 1.0 or displacement >= 0.012):
                response[target.name] = frame
        motion.append({
            "frame": frame,
            "activeTargetCount": sum(step >= 0.25 for step in angular_steps.values()),
            "aggregateAngularStepDegrees": round(sum(angular_steps.values()), 8),
            "targetAngularStepDegrees": {name: round(value, 8) for name, value in angular_steps.items()},
        })
    final_tilts = {obj.name: round(_tilt(obj), 8) for obj in targets}
    first = min((frame for frame in response.values() if frame is not None), default=None)
    if first is not None:
        scene.frame_set(first)
        bpy.context.view_layer.update()
    travel = None if first is None else round(actor.matrix_world.translation.x - initial_actor.x, 8)
    motion_selection = None
    if document["schemaVersion"] in {"bfs.causalSceneSpec.v0.2", "bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"} and first is not None and all(frame is not None for frame in response.values()):
        acceptance = document["acceptance"]
        impact_minimum = first + acceptance["impactFrameAfterFirstResponseMinimum"]
        candidates = [row for row in motion if row["frame"] >= impact_minimum]
        impact = max(candidates, key=lambda row: (row["activeTargetCount"], row["aggregateAngularStepDegrees"], -row["frame"]))
        settle_start = max(response.values())
        settle_count = acceptance["settleConsecutiveFrames"]
        settle_limit = acceptance["settleAngularStepDegreesMaximum"]
        aftermath = document["timeline"]["frameEnd"]
        for offset, row in enumerate(motion):
            window = motion[offset:offset + settle_count]
            if row["frame"] >= settle_start and len(window) == settle_count and all(sample["aggregateAngularStepDegrees"] <= settle_limit for sample in window):
                aftermath = row["frame"]
                break
        motion_selection = {
            "source": "EVALUATED_TARGET_WORLD_TILT_DELTAS",
            "impactRule": "MAX_ACTIVE_TARGETS_THEN_AGGREGATE_ANGULAR_STEP_THEN_EARLIEST",
            "impactFrame": impact["frame"],
            "impactActiveTargetCount": impact["activeTargetCount"],
            "impactAggregateAngularStepDegrees": impact["aggregateAngularStepDegrees"],
            "impactTargetAngularStepDegrees": impact["targetAngularStepDegrees"],
            "aftermathRule": "FIRST_POST_RESPONSE_SETTLED_WINDOW_ELSE_FRAME_END",
            "aftermathFrame": aftermath,
            "settleConsecutiveFrames": settle_count,
            "settleAngularStepDegreesMaximum": settle_limit,
        }
    scene.frame_set(document["timeline"]["frameEnd"])
    bpy.context.view_layer.update()
    return {"firstTargetResponseFrame": first, "targetResponseFrames": response, "finalTiltDegrees": final_tilts, "actorForwardTravelBeforeFirstResponse": travel, "motionSamples": motion, "motionSelection": motion_selection}


def _projected_bounds(scene, camera, objects):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        if obj.type == "MESH":
            evaluated = obj.evaluated_get(depsgraph)
            points.extend(world_to_camera_view(scene, camera, evaluated.matrix_world @ Vector(corner)) for corner in evaluated.bound_box)
    visible = [point for point in points if point.z > 0]
    if not visible:
        raise CausalContractError("CAMERA_FIT", "No visible semantic bounds")
    return min(p.x for p in visible), max(p.x for p in visible), min(p.y for p in visible), max(p.y for p in visible)


def _fit_camera(scene, camera, objects, desired):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        if obj.type == "MESH":
            evaluated = obj.evaluated_get(depsgraph)
            points.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
    minimum = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    maximum = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    center = (minimum + maximum) / 2
    forward = camera.matrix_world.to_quaternion() @ Vector((0, 0, -1))
    forward.normalize()
    distance = max(2.0, (maximum - minimum).length * 1.5)
    for _ in range(7):
        camera.location = center - forward * distance
        _point_at(camera, center)
        bpy.context.view_layer.update()
        x0, x1, y0, y1 = _projected_bounds(scene, camera, objects)
        observed = max(x1 - x0, y1 - y0)
        distance *= max(0.55, min(1.8, observed / desired))
    camera.location = center - forward * distance
    _point_at(camera, center)
    bpy.context.view_layer.update()
    x0, x1, y0, y1 = _projected_bounds(scene, camera, objects)
    observed = max(x1 - x0, y1 - y0)
    margin = min(x0, y0, 1 - x1, 1 - y1)
    return {"source": "EVALUATED_FRAME_SEMANTIC_WORLD_BOUNDS", "occupancy": round(observed, 8), "negativeSpaceMargin": round(margin, 8), "cameraLocation": [round(value, 8) for value in camera.location]}


def _configure_measured_shutter(scene, camera, objects, impact_frame, document):
    spec = document["cinematography"]["motionBlur"]
    width, height = spec["measurementResolution"]
    projected = {}
    for frame in (impact_frame - 1, impact_frame):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        projected[frame] = {}
        for obj in objects:
            point = world_to_camera_view(scene, camera, obj.matrix_world.translation)
            projected[frame][obj.name] = (point.x * width, (1.0 - point.y) * height)
    speeds = {}
    for obj in objects:
        before = projected[impact_frame - 1][obj.name]
        after = projected[impact_frame][obj.name]
        speeds[obj.name] = math.hypot(after[0] - before[0], after[1] - before[1])
    median_speed = statistics.median(speeds.values())
    if median_speed <= 1e-9:
        raise CausalContractError("MOTION_BLUR_MEASUREMENT", "Impact projected motion is zero")
    unclamped = spec["targetBlurPixels"] / median_speed
    shutter = max(spec["minimumShutterFrames"], min(spec["maximumShutterFrames"], unclamped))
    shutter = round(shutter, 8)
    achieved = median_speed * shutter
    scene.render.use_motion_blur = True
    scene.render.motion_blur_shutter = shutter
    scene.render.motion_blur_position = spec["position"]
    scene.frame_set(impact_frame)
    bpy.context.view_layer.update()
    return {
        "source": "EVALUATED_SEMANTIC_PROJECTED_ORIGIN_MOTION",
        "strategy": spec["strategy"],
        "measurementFrame": impact_frame,
        "measurementInterval": [impact_frame - 1, impact_frame],
        "measurementResolution": [width, height],
        "semanticObjectCount": len(objects),
        "objectPixelsPerFrame": {name: round(value, 8) for name, value in speeds.items()},
        "medianPixelsPerFrame": round(median_speed, 8),
        "targetBlurPixels": spec["targetBlurPixels"],
        "unclampedShutterFrames": round(unclamped, 8),
        "computedShutterFrames": shutter,
        "achievedMedianBlurPixels": round(achieved, 8),
        "targetErrorPixels": round(abs(achieved - spec["targetBlurPixels"]), 8),
        "position": spec["position"],
        "nativeTransformMotionBlur": True,
        "compositorOrPostprocessBlur": False,
    }


def execute_causal_scene(repository_root, scene_spec_uri, inspection_token, scene=None):
    inspection = inspect_causal_scene(repository_root, scene_spec_uri)
    if inspection_token != inspection["inspectionToken"]:
        raise CausalContractError("INSPECTION_REQUIRED", "Inspect the exact CausalSceneSpec before mutation")
    root = Path(repository_root).resolve(strict=True)
    document = _validate(_read_json(_below_existing(root, scene_spec_uri)))
    scene = scene or bpy.context.scene
    _clear_scene(scene)
    scene.name = "FILM_STUDIO_CAUSAL_SCENE"
    scene.frame_start, scene.frame_end, scene.render.fps = document["timeline"]["frameStart"], document["timeline"]["frameEnd"], document["timeline"]["fps"]
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 960, 540, 100
    scene.render.image_settings.file_format, scene.render.image_settings.color_mode = "PNG", "RGBA"
    scene.render.use_motion_blur = False
    scene.gravity = (0, 0, -9.81)
    if scene.world is None:
        scene.world = bpy.data.worlds.new("FILM_STUDIO_CAUSAL_WORLD")
    scene.world.color = (0.012, 0.015, 0.025)
    if document["schemaVersion"] == "bfs.causalSceneSpec.v0.5" and hasattr(scene, "eevee") and hasattr(scene.eevee, "use_raytracing"):
        scene.eevee.use_raytracing = True
    floor, wall, lights, dark = _create_studio(document)
    actor, actor_details = _create_actor(document, dark)
    targets, target_details, initial_conditions = _create_targets(document, dark)
    cameras = _create_cameras(document)
    if scene.rigidbody_world:
        scene.rigidbody_world.substeps_per_frame = 10
        scene.rigidbody_world.solver_iterations = 40
        scene.rigidbody_world.point_cache.frame_start = scene.frame_start
        scene.rigidbody_world.point_cache.frame_end = scene.frame_end
    physics = _simulate(scene, actor, targets, document)
    first = physics["firstTargetResponseFrame"]
    if document["schemaVersion"] in {"bfs.causalSceneSpec.v0.2", "bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"} and physics["motionSelection"]:
        frames = {"SETUP": max(scene.frame_start, first - 1), "IMPACT": physics["motionSelection"]["impactFrame"], "AFTERMATH": physics["motionSelection"]["aftermathFrame"]}
    else:
        frames = {"SETUP": max(scene.frame_start, first - 2) if first else scene.frame_start, "IMPACT": first or (scene.frame_start + scene.frame_end) // 2, "AFTERMATH": scene.frame_end}
    narrative = [actor, *actor_details, *targets, *target_details]
    framing = {}
    for shot in document["shots"]:
        shot_id = shot["shotId"]
        scene.frame_set(frames[shot_id])
        bpy.context.view_layer.update()
        record = _fit_camera(scene, cameras[shot_id], narrative, shot["targetOccupancy"])
        record["frame"] = frames[shot_id]
        cameras[shot_id]["film_studio_framing"] = json.dumps(record, sort_keys=True)
        framing[shot_id] = record
    cinematography = None
    if document["schemaVersion"] in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        cinematography = {
            "motionBlur": _configure_measured_shutter(
                scene,
                cameras["IMPACT"],
                [actor, *targets],
                physics["motionSelection"]["impactFrame"],
                document,
            )
        }
    scene.camera = cameras["SETUP"]
    scene.frame_set(scene.frame_start)
    initial_conditions_record = {
        "source": "SHA256_SCENE_HASH_SEED_TARGET_INDEX_CHANNEL" if document["schemaVersion"] == "bfs.causalSceneSpec.v0.2" else "DECLARED_EXACT",
        "targets": initial_conditions,
    }
    if document["schemaVersion"] in {"bfs.causalSceneSpec.v0.4", "bfs.causalSceneSpec.v0.5"}:
        initial_conditions_record["source"] = "SHA256_VARIATION_BASIS_HASH_SEED_TARGET_INDEX_CHANNEL"
        initial_conditions_record["basisSceneSpecHash"] = document["targetGroup"]["deterministicVariation"]["basisSceneSpecHash"]
    result = {
        **inspection,
        "status": "PHYSICS_READY",
        "contractVersion": CONTRACT_VERSION,
        "physics": physics,
        "initialConditions": initial_conditions_record,
        "framing": framing,
        "provenance": {
            "finalPoseSource": "BLENDER_BULLET_RIGID_BODY",
            "postReleaseActorPoseKeyframes": 0,
            "targetPoseKeyframes": 0,
            "sceneSpecExecutableAuthority": 0,
            "networkCalls": 0,
        },
        "semanticRoster": {
            "dynamicActor": [actor.name],
            "targets": [obj.name for obj in targets],
            "cameras": [camera.name for camera in cameras.values()],
            "lights": [light.name for light in lights],
            "ground": floor.name,
            "environment": wall.name,
        },
    }
    if cinematography is not None:
        result["cinematography"] = cinematography
    if document["schemaVersion"] == "bfs.causalSceneSpec.v0.5":
        result["physicalArchetypes"] = {
            "units": {"length": "meter", "mass": "kilogram", "capacity": "liter"},
            "actor": {
                "kind": "basketball",
                "radiusMeters": document["dynamicActor"]["radius"],
                "massKg": document["dynamicActor"]["rigidBody"]["mass"],
                "collisionShape": actor.rigid_body.collision_shape,
                "collisionMarginMeters": round(actor.rigid_body.collision_margin, 8),
            },
            "targets": initial_conditions,
            "massAndCenterOfMassSource": "VISIBLE_FILL_FRACTION_DERIVATION",
            "visibleBodyIsCollisionHullSource": all(row["visibleBodyIsCollisionHullSource"] for row in initial_conditions),
            "liquidSimulationClaim": False,
        }
    scene["film_studio_causal_result"] = json.dumps(result, sort_keys=True, separators=(",", ":"))
    scene["film_studio_causal_scene_spec_hash"] = document["sceneSpecHash"]
    return result
