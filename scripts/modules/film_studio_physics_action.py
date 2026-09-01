# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Restricted declarative physics-action graph.

Specs contain approved asset factories, metric initial conditions and causal
relations.  Blender Bullet owns every active transform after release.  This
module derives contact, response, shot frames and native motion blur from
evaluated simulation state; it never accepts an authored outcome pose/frame.
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

import film_studio_causal as causal
import film_studio_contract
import film_studio_physical_light as light_build
import film_studio_physical_look as physical_look
import film_studio_physical_performance as direction


SPEC_VERSION = "bfs.physicsActionSpec.v0.1"
BREAKABLE_SPEC_VERSION = "bfs.physicsActionSpec.v0.2"
DERIVED_TARGET_SPEC_VERSION = "bfs.physicsActionSpec.v0.4"
CONTRACT_VERSION = "bfs.filmStudioPhysicsAction.v0.1"
BREAKABLE_CONTRACT_VERSION = "bfs.filmStudioPhysicsAction.v0.2"
DERIVED_TARGET_CONTRACT_VERSION = "bfs.filmStudioPhysicsAction.v0.3"
RESULT_VERSION = "bfs.physicsActionResult.v0.1"
BREAKABLE_RESULT_VERSION = "bfs.physicsActionResult.v0.2"
DERIVED_TARGET_RESULT_VERSION = "bfs.physicsActionResult.v0.3"
GENERATED_TAG = "bfs.physicsAction.v0.1"

FACTORIES = {
    "GROOVED_CERAMIC_SPHERE",
    "GROOVED_BASKETBALL",
    "METRIC_WEDGE_WITH_SIDE_RAILS",
    "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE",
    "MATTE_RELIEF_SIGNAL_WALL",
    "FILLED_LATHED_BOTTLE_ARRAY",
    "BREAKAWAY_BOTTLE_CAP",
    "BEVELED_GROUND_PLANE",
    "STATIC_AREA_LIGHT",
}
RELATIONS = {
    "ROLLS_ON",
    "RESTS_ON",
    "COLLIDES_WITH",
    "HINGED_TO_WORLD",
    "OCCLUDES_LIGHT_TO",
    "PROPAGATES_RESPONSE_WITHIN",
    "BREAKABLE_FIXED_TO",
}
PROHIBITED_OUTCOME_KEYS = {
    "finalPosition", "finalRotation", "finalPose", "targetPose", "targetFrame",
    "contactFrame", "responseFrame", "peakFrame", "breakFrame",
    "detachmentFrame", "detachedPose", "detachmentVelocity",
}


class PhysicsActionError(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _canonical(value):
    return film_studio_contract.javascript_canonical_json(value)


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _self_hash(value, field):
    body = copy.deepcopy(value)
    body.pop(field, None)
    return _sha256(_canonical(body).encode("utf-8"))


def _reject_constant(token):
    raise PhysicsActionError("NONFINITE_NUMBER", f"Non-finite JSON token {token} is forbidden")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except PhysicsActionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PhysicsActionError("INVALID_JSON", str(error)) from error


def _below_existing(root, uri):
    normalized = str(uri).replace("\\", "/")
    if not normalized or normalized.startswith("/") or "://" in normalized or ".." in normalized.split("/"):
        raise PhysicsActionError("PATH_ESCAPE", "PhysicsActionSpec escapes the repository root")
    candidate = root / normalized
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise PhysicsActionError("MISSING_INPUT", normalized) from error
    if root not in resolved.parents or Path(os.path.abspath(candidate)) != resolved:
        raise PhysicsActionError("PATH_ESCAPE", "PhysicsActionSpec traverses a link or escapes the root")
    return resolved


def _exact(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        reason = "UNKNOWN_TOP_LEVEL_FIELD" if path == "/" else "SPEC_SCHEMA"
        raise PhysicsActionError(reason, path)


def _number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise PhysicsActionError("SPEC_SCHEMA", label)


def _integer(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PhysicsActionError("SPEC_SCHEMA", label)


def _vector(value, count, low, high, label):
    if not isinstance(value, list) or len(value) != count:
        raise PhysicsActionError("SPEC_SCHEMA", label)
    for item in value:
        _number(item, low, high, label)


def _outcome_scan(value, path="/"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in PROHIBITED_OUTCOME_KEYS:
                raise PhysicsActionError("OUTCOME_AUTHORITY", f"{path}{key}")
            _outcome_scan(child, f"{path}{key}/")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _outcome_scan(child, f"{path}{index}/")


def _validate_initial(value, factory, timeline):
    if factory == "BREAKAWAY_BOTTLE_CAP":
        _exact(value, {"deriveFromTargetGeometry"}, "/nodes/initialCondition")
        if value["deriveFromTargetGeometry"] is not True:
            raise PhysicsActionError("SPEC_SCHEMA", "deriveFromTargetGeometry")
        return
    base = {"location", "rotationDegrees"}
    if factory in {"GROOVED_CERAMIC_SPHERE", "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE", "FILLED_LATHED_BOTTLE_ARRAY"}:
        keys = base | {"releaseFrame"}
    elif factory == "GROOVED_BASKETBALL":
        keys = base | {"releaseFrame", "preReleaseVelocityMetersPerSecond"}
    else:
        keys = base
    _exact(value, keys, "/nodes/initialCondition")
    _vector(value["location"], 3, -100.0, 100.0, "location")
    _vector(value["rotationDegrees"], 3, -360.0, 360.0, "rotationDegrees")
    if "releaseFrame" in value:
        _integer(value["releaseFrame"], timeline["frameStart"], timeline["frameEnd"] - 2, "releaseFrame")
    if "preReleaseVelocityMetersPerSecond" in value:
        _vector(value["preReleaseVelocityMetersPerSecond"], 3, -30.0, 30.0, "preReleaseVelocity")


def _validate_rigid(value, factory):
    if factory == "STATIC_AREA_LIGHT":
        if value is not None:
            raise PhysicsActionError("SPEC_SCHEMA", "light rigidBody")
        return
    if not isinstance(value, dict):
        raise PhysicsActionError("SPEC_SCHEMA", "rigidBody")
    body_type = value.get("type")
    if body_type == "ACTIVE":
        if factory == "FILLED_LATHED_BOTTLE_ARRAY":
            keys = {"type", "massDerivation", "collisionShape", "friction", "restitution", "linearDamping", "angularDamping"}
            _exact(value, keys, "/nodes/rigidBody")
            if value["massDerivation"] != "GLASS_PLUS_FILL_VOLUME_DENSITY":
                raise PhysicsActionError("SPEC_SCHEMA", "massDerivation")
        else:
            keys = {"type", "massKg", "collisionShape", "friction", "restitution", "linearDamping", "angularDamping"}
            _exact(value, keys, "/nodes/rigidBody")
            _number(value["massKg"], 0.001, 1000.0, "massKg")
        for key in ("friction", "restitution", "linearDamping", "angularDamping"):
            _number(value[key], 0.0, 1.0, key)
    elif body_type == "PASSIVE":
        _exact(value, {"type", "collisionShape", "friction", "restitution"}, "/nodes/rigidBody")
        _number(value["friction"], 0.0, 1.0, "friction")
        _number(value["restitution"], 0.0, 1.0, "restitution")
    else:
        raise PhysicsActionError("SPEC_SCHEMA", "rigid body type")
    allowed_shapes = {
        "GROOVED_CERAMIC_SPHERE": "SPHERE", "GROOVED_BASKETBALL": "SPHERE",
        "METRIC_WEDGE_WITH_SIDE_RAILS": "MESH", "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE": "BOX",
        "MATTE_RELIEF_SIGNAL_WALL": "BOX", "FILLED_LATHED_BOTTLE_ARRAY": "CONVEX_HULL",
        "BREAKAWAY_BOTTLE_CAP": "CYLINDER",
        "BEVELED_GROUND_PLANE": "BOX",
    }
    if value["collisionShape"] != allowed_shapes[factory]:
        raise PhysicsActionError("SPEC_SCHEMA", "collision shape")


def _validate_parameters(value, factory):
    keys = {
        "GROOVED_CERAMIC_SPHERE": {"radiusMeters", "materialPreset"},
        "GROOVED_BASKETBALL": {"radiusMeters", "materialPreset"},
        "METRIC_WEDGE_WITH_SIDE_RAILS": {"lengthMeters", "widthMeters", "riseMeters", "deckThicknessMeters", "materialPreset"},
        "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE": {"apertureWidthMeters", "apertureHeightMeters", "shutterThicknessMeters", "materialPreset"},
        "MATTE_RELIEF_SIGNAL_WALL": {"widthMeters", "heightMeters", "materialPreset"},
        "FILLED_LATHED_BOTTLE_ARRAY": {"count", "heightMeters", "bodyRadiusMeters", "fillFractions", "materialPreset", "initialBasePositions"},
        "BREAKAWAY_BOTTLE_CAP": set(value) if isinstance(value, dict) else set(),
        "BEVELED_GROUND_PLANE": {"widthMeters", "depthMeters", "thicknessMeters", "materialPreset"},
        "STATIC_AREA_LIGHT": {"powerWatts", "colorLinearRgb", "sizeMeters"},
    }[factory]
    _exact(value, keys, "/nodes/parameters")
    if factory in {"GROOVED_CERAMIC_SPHERE", "GROOVED_BASKETBALL"}:
        _number(value["radiusMeters"], 0.04, 0.5, "radiusMeters")
    elif factory == "METRIC_WEDGE_WITH_SIDE_RAILS":
        for key, low, high in (("lengthMeters", .5, 8), ("widthMeters", .3, 4), ("riseMeters", .05, 3), ("deckThicknessMeters", .01, .3)):
            _number(value[key], low, high, key)
    elif factory == "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE":
        for key in ("apertureWidthMeters", "apertureHeightMeters"):
            _number(value[key], .3, 3, key)
        _number(value["shutterThicknessMeters"], .01, .3, "shutterThicknessMeters")
    elif factory == "MATTE_RELIEF_SIGNAL_WALL":
        _number(value["widthMeters"], .3, 5, "widthMeters")
        _number(value["heightMeters"], .3, 5, "heightMeters")
    elif factory == "FILLED_LATHED_BOTTLE_ARRAY":
        _integer(value["count"], 2, 8, "count")
        if len(value["fillFractions"]) != value["count"] or len(value["initialBasePositions"]) != value["count"]:
            raise PhysicsActionError("SPEC_SCHEMA", "bottle array count")
        _number(value["heightMeters"], .12, .6, "heightMeters")
        _number(value["bodyRadiusMeters"], .02, .1, "bodyRadiusMeters")
        for fill in value["fillFractions"]:
            _number(fill, .05, .98, "fillFraction")
        for position in value["initialBasePositions"]:
            _vector(position, 3, -10, 10, "initialBasePosition")
    elif factory == "BREAKAWAY_BOTTLE_CAP":
        allowed = ({"targetMemberIndex", "materialPreset"}, {"targetMemberPolicy", "materialPreset"})
        if set(value) not in allowed:
            raise PhysicsActionError("SPEC_SCHEMA", "breakaway cap target binding")
        if "targetMemberIndex" in value:
            _integer(value["targetMemberIndex"], 0, 7, "targetMemberIndex")
        elif value["targetMemberPolicy"] != "MINIMUM_DISTANCE_TO_INITIATOR_RELEASE_RAY":
            raise PhysicsActionError("SPEC_SCHEMA", "targetMemberPolicy")
        if value["materialPreset"] != "CRIMPED_ALUMINUM_CAP":
            raise PhysicsActionError("SPEC_SCHEMA", "breakaway cap material")
    elif factory == "BEVELED_GROUND_PLANE":
        for key in ("widthMeters", "depthMeters", "thicknessMeters"):
            _number(value[key], .02, 20, key)
    elif factory == "STATIC_AREA_LIGHT":
        _number(value["powerWatts"], 10, 10000, "powerWatts")
        _vector(value["colorLinearRgb"], 3, 0, 1, "light color")
        _number(value["sizeMeters"], .02, 10, "light size")
    if "materialPreset" in value and (not isinstance(value["materialPreset"], str) or not value["materialPreset"]):
        raise PhysicsActionError("SPEC_SCHEMA", "materialPreset")


def _validate_relation(value, ids):
    kind = value.get("type") if isinstance(value, dict) else None
    if kind not in RELATIONS:
        raise PhysicsActionError("UNSUPPORTED_RELATION", str(kind))
    keys = {
        "ROLLS_ON": {"id", "type", "source", "target"},
        "RESTS_ON": {"id", "type", "source", "target"},
        "COLLIDES_WITH": {"id", "type", "source", "target", "contactToleranceMeters"},
        "HINGED_TO_WORLD": {"id", "type", "source", "axis", "limitsDegrees", "solverIterations", "passiveStopOffsetFromUpperLimitDegrees"},
        "OCCLUDES_LIGHT_TO": {"id", "type", "source", "target", "via"},
        "PROPAGATES_RESPONSE_WITHIN": {"id", "type", "source", "minimumResponders"},
        "BREAKABLE_FIXED_TO": {"id", "type", "source", "target", "breakingImpulseThreshold", "solverIterations", "disableCollisions"},
    }[kind]
    _exact(value, keys, "/relations")
    refs = [value[key] for key in ("source", "target", "via") if key in value]
    if any(ref not in ids for ref in refs):
        raise PhysicsActionError("MISSING_NODE_REFERENCE", value["id"])
    if kind == "COLLIDES_WITH":
        _number(value["contactToleranceMeters"], 0, .05, "contactToleranceMeters")
    elif kind == "HINGED_TO_WORLD":
        if value["axis"] != "WORLD_Z":
            raise PhysicsActionError("UNSUPPORTED_RELATION", "hinge axis")
        _vector(value["limitsDegrees"], 2, -180, 180, "hinge limits")
        if value["limitsDegrees"][1] <= value["limitsDegrees"][0] or value["limitsDegrees"][1] - value["limitsDegrees"][0] > 170:
            raise PhysicsActionError("SPEC_SCHEMA", "hinge limits")
        _integer(value["solverIterations"], 20, 200, "solverIterations")
        _number(value["passiveStopOffsetFromUpperLimitDegrees"], 1, 20, "stop offset")
    elif kind == "PROPAGATES_RESPONSE_WITHIN":
        _integer(value["minimumResponders"], 1, 8, "minimumResponders")
    elif kind == "BREAKABLE_FIXED_TO":
        _number(value["breakingImpulseThreshold"], 0.0001, 1000.0, "breakingImpulseThreshold")
        _integer(value["solverIterations"], 20, 200, "solverIterations")
        if value["disableCollisions"] is not True:
            raise PhysicsActionError("SPEC_SCHEMA", "breakable attached-body collisions")


def _validate(document):
    version = document.get("schemaVersion")
    top_keys = {"$schema", "schemaVersion", "projectId", "timeline", "world", "nodes", "relations", "beats", "cinematography", "forbidden", "physicsActionSpecHash"}
    if version == DERIVED_TARGET_SPEC_VERSION:
        top_keys.add("physicalVariationBasisSpecHash")
    _exact(document, top_keys, "/")
    if document["$schema"] != version or version not in {SPEC_VERSION, BREAKABLE_SPEC_VERSION, DERIVED_TARGET_SPEC_VERSION}:
        raise PhysicsActionError("UNSUPPORTED_SCHEMA", str(document.get("schemaVersion")))
    if not isinstance(document["projectId"], str) or not document["projectId"]:
        raise PhysicsActionError("SPEC_SCHEMA", "projectId")
    if document["physicsActionSpecHash"] != _self_hash(document, "physicsActionSpecHash"):
        raise PhysicsActionError("SELF_HASH_MISMATCH", "physicsActionSpecHash")
    _outcome_scan({key: document[key] for key in document if key != "forbidden"})
    timeline = document["timeline"]
    _exact(timeline, {"frameStart", "frameEnd", "fps"}, "/timeline")
    for key in timeline:
        _integer(timeline[key], 1, 100000, key)
    if timeline["frameEnd"] <= timeline["frameStart"] + 48:
        raise PhysicsActionError("SPEC_SCHEMA", "timeline range")
    world = document["world"]
    _exact(world, {"gravityMetersPerSecondSquared", "unitScaleMeters"}, "/world")
    if world != {"gravityMetersPerSecondSquared": [0.0, 0.0, -9.81], "unitScaleMeters": 1.0}:
        raise PhysicsActionError("NONMETRIC_SCENE", "world")
    if version == DERIVED_TARGET_SPEC_VERSION:
        basis = document["physicalVariationBasisSpecHash"]
        if not isinstance(basis, str) or len(basis) != 64 or any(character not in "0123456789abcdef" for character in basis):
            raise PhysicsActionError("SPEC_SCHEMA", "physicalVariationBasisSpecHash")
    if not isinstance(document["nodes"], list) or not 3 <= len(document["nodes"]) <= 16:
        raise PhysicsActionError("SPEC_SCHEMA", "nodes")
    ids = set()
    for node in document["nodes"]:
        factory = node.get("factory") if isinstance(node, dict) else None
        if factory not in FACTORIES:
            raise PhysicsActionError("UNSUPPORTED_FACTORY", str(factory))
        keys = {"id", "semanticRole", "factory", "parameters", "initialCondition"}
        if factory != "STATIC_AREA_LIGHT":
            keys.add("rigidBody")
        _exact(node, keys, "/nodes")
        if not isinstance(node["id"], str) or not node["id"] or node["id"] in ids:
            raise PhysicsActionError("DUPLICATE_NODE_ID", str(node.get("id")))
        ids.add(node["id"])
        if not isinstance(node["semanticRole"], str) or not node["semanticRole"]:
            raise PhysicsActionError("SPEC_SCHEMA", "semanticRole")
        _validate_parameters(node["parameters"], factory)
        _validate_initial(node["initialCondition"], factory, timeline)
        _validate_rigid(node.get("rigidBody"), factory)
    if version == SPEC_VERSION and any(node["factory"] == "BREAKAWAY_BOTTLE_CAP" for node in document["nodes"]):
        raise PhysicsActionError("UNSUPPORTED_FACTORY", "BREAKAWAY_BOTTLE_CAP requires v0.2")
    cap_nodes = [node for node in document["nodes"] if node["factory"] == "BREAKAWAY_BOTTLE_CAP"]
    if version == BREAKABLE_SPEC_VERSION and cap_nodes and "targetMemberIndex" not in cap_nodes[0]["parameters"]:
        raise PhysicsActionError("SPEC_SCHEMA", "v0.2 requires targetMemberIndex")
    if version == DERIVED_TARGET_SPEC_VERSION and cap_nodes and "targetMemberPolicy" not in cap_nodes[0]["parameters"]:
        raise PhysicsActionError("SPEC_SCHEMA", "v0.4 requires targetMemberPolicy")
    if not isinstance(document["relations"], list) or not document["relations"]:
        raise PhysicsActionError("SPEC_SCHEMA", "relations")
    relation_ids = set()
    for relation in document["relations"]:
        _validate_relation(relation, ids)
        if relation["id"] in relation_ids:
            raise PhysicsActionError("SPEC_SCHEMA", "duplicate relation")
        relation_ids.add(relation["id"])
    breakable_relations = [relation for relation in document["relations"] if relation["type"] == "BREAKABLE_FIXED_TO"]
    if version == SPEC_VERSION and breakable_relations:
        raise PhysicsActionError("UNSUPPORTED_RELATION", "BREAKABLE_FIXED_TO requires v0.2")
    if version in {BREAKABLE_SPEC_VERSION, DERIVED_TARGET_SPEC_VERSION}:
        caps = cap_nodes
        if len(caps) != 1 or len(breakable_relations) != 1:
            raise PhysicsActionError("SPEC_SCHEMA", "v0.2 requires one breakaway cap and one breakable relation")
        relation = breakable_relations[0]
        if relation["source"] != caps[0]["id"]:
            raise PhysicsActionError("SPEC_SCHEMA", "breakable relation source")
        target = next((node for node in document["nodes"] if node["id"] == relation["target"]), None)
        if target is None or target["factory"] != "FILLED_LATHED_BOTTLE_ARRAY":
            raise PhysicsActionError("SPEC_SCHEMA", "breakable relation target")
        if "targetMemberIndex" in caps[0]["parameters"] and caps[0]["parameters"]["targetMemberIndex"] >= target["parameters"]["count"]:
            raise PhysicsActionError("SPEC_SCHEMA", "targetMemberIndex")
    if sum(relation["type"] == "COLLIDES_WITH" for relation in document["relations"]) != 1:
        raise PhysicsActionError("SPEC_SCHEMA", "one primary collision required")
    beat_ids = set()
    for beat in document["beats"]:
        derive = beat.get("deriveFrom") if isinstance(beat, dict) else None
        keys = {
            "BEFORE_RELATION_EVENT": {"id", "deriveFrom", "relation", "offsetFrames"},
            "FIRST_CONTACT": {"id", "deriveFrom", "relation"},
            "PEAK_ANGULAR_RESPONSE": {"id", "deriveFrom", "node", "afterBeat"},
            "PEAK_GROUP_RESPONSE": {"id", "deriveFrom", "node", "afterBeat"},
            "SETTLED_GROUP_RESPONSE": {"id", "deriveFrom", "node", "afterBeat"},
        }.get(derive)
        if keys is None:
            raise PhysicsActionError("SPEC_SCHEMA", "beat derivation")
        _exact(beat, keys, "/beats")
        if beat["id"] in beat_ids:
            raise PhysicsActionError("SPEC_SCHEMA", "duplicate beat")
        beat_ids.add(beat["id"])
        if "relation" in beat and beat["relation"] not in relation_ids:
            raise PhysicsActionError("SPEC_SCHEMA", "beat relation")
        if "node" in beat and beat["node"] not in ids:
            raise PhysicsActionError("SPEC_SCHEMA", "beat node")
        if "afterBeat" in beat and beat["afterBeat"] not in beat_ids:
            raise PhysicsActionError("BEAT_DEPENDENCY", beat["id"])
        if "offsetFrames" in beat:
            _integer(beat["offsetFrames"], -48, -1, "offsetFrames")
    if beat_ids != {"cause", "contact", "effect"}:
        raise PhysicsActionError("SPEC_SCHEMA", "cause/contact/effect beats")
    cinema = document["cinematography"]
    _exact(cinema, {"policy", "reviewResolution", "clipFrameCount", "motionBlur"}, "/cinematography")
    if cinema["policy"] != "SEMANTIC_CAUSE_CONTACT_EFFECT" or cinema["reviewResolution"] not in ([960, 540], [1280, 720]) or cinema["clipFrameCount"] != 48:
        raise PhysicsActionError("SPEC_SCHEMA", "cinematography")
    blur = cinema["motionBlur"]
    _exact(blur, {"source", "targetBlurPixels", "position"}, "/cinematography/motionBlur")
    if blur["source"] != "MEASURED_PROJECTED_MEDIAN_MOTION" or blur["position"] != "CENTER":
        raise PhysicsActionError("SPEC_SCHEMA", "motion blur")
    _number(blur["targetBlurPixels"], 1, 12, "targetBlurPixels")
    forbidden_keys = {"authoredTransformAfterRelease", "authoredContactFrame", "authoredResponseFrame", "authoredFinalPose", "animatedLightPowerOrColor", "postprocessMotionBlur", "projectOrFixtureBranchInProductCode", "arbitraryPythonShellNetworkOrFilesystemAuthority"}
    if version in {BREAKABLE_SPEC_VERSION, DERIVED_TARGET_SPEC_VERSION}:
        forbidden_keys |= {"authoredBreakFrame", "authoredDetachedPose", "authoredDetachmentVelocity"}
    _exact(document["forbidden"], forbidden_keys, "/forbidden")
    if any(document["forbidden"][key] is not True for key in forbidden_keys):
        raise PhysicsActionError("AUTHORITY_EXPANSION", "forbidden controls")
    topology = {relation["type"] for relation in document["relations"]}
    supported = (
        {"ROLLS_ON", "COLLIDES_WITH", "HINGED_TO_WORLD", "OCCLUDES_LIGHT_TO"}.issubset(topology)
        or {"RESTS_ON", "COLLIDES_WITH", "PROPAGATES_RESPONSE_WITHIN"}.issubset(topology)
    )
    if not supported:
        raise PhysicsActionError("UNSUPPORTED_TOPOLOGY", ",".join(sorted(topology)))
    if any(beat["deriveFrom"] == "SETTLED_GROUP_RESPONSE" for beat in document["beats"]) and "PROPAGATES_RESPONSE_WITHIN" not in topology:
        raise PhysicsActionError("BEAT_DEPENDENCY", "settled response requires a response group")
    return document


def matches_physics_action(repository_root, spec_uri):
    root = Path(repository_root).resolve(strict=True)
    return _read_json(_below_existing(root, spec_uri)).get("schemaVersion") in {SPEC_VERSION, BREAKABLE_SPEC_VERSION, DERIVED_TARGET_SPEC_VERSION}


def _inspection(repository_root, spec_uri):
    root = Path(repository_root).resolve(strict=True)
    path = _below_existing(root, spec_uri)
    document = _validate(_read_json(path))
    file_hash = _sha256(path.read_bytes())
    graph = {"nodes": [{"id": node["id"], "role": node["semanticRole"], "factory": node["factory"]} for node in document["nodes"]], "relations": document["relations"], "beats": document["beats"]}
    graph_hash = _sha256(_canonical(graph).encode("utf-8"))
    contract_version = {
        SPEC_VERSION: CONTRACT_VERSION,
        BREAKABLE_SPEC_VERSION: BREAKABLE_CONTRACT_VERSION,
        DERIVED_TARGET_SPEC_VERSION: DERIVED_TARGET_CONTRACT_VERSION,
    }[document["schemaVersion"]]
    token = _sha256(_canonical({"contractVersion": contract_version, "fileSha256": file_hash, "specHash": document["physicsActionSpecHash"], "compiledGraphHash": graph_hash}).encode("utf-8"))
    return document, file_hash, graph, graph_hash, token


def inspect_physics_action(repository_root, spec_uri):
    document, file_hash, graph, graph_hash, token = _inspection(repository_root, spec_uri)
    contract_version = {
        SPEC_VERSION: CONTRACT_VERSION, BREAKABLE_SPEC_VERSION: BREAKABLE_CONTRACT_VERSION,
        DERIVED_TARGET_SPEC_VERSION: DERIVED_TARGET_CONTRACT_VERSION,
    }[document["schemaVersion"]]
    active = [node for node in document["nodes"] if node.get("rigidBody", {}).get("type") == "ACTIVE"]
    collision = next(relation for relation in document["relations"] if relation["type"] == "COLLIDES_WITH")
    nodes = {node["id"]: node for node in document["nodes"]}
    result = {
        "status": "APPROVED_READY",
        "sceneId": document["projectId"],
        "actorFactory": nodes[collision["source"]]["factory"],
        "targetFactory": nodes[collision["target"]]["factory"],
        "targetCount": nodes[collision["target"]]["parameters"].get("count", 1),
        "collisionShapes": [node["rigidBody"]["collisionShape"] for node in active],
        "finalPoseSource": "BLENDER_BULLET_EVALUATED_ACTION_GRAPH",
        "cameraFitSource": "MEASURED_CAUSE_CONTACT_EFFECT_SEMANTIC_BOUNDS",
        "sceneSpecHash": document["physicsActionSpecHash"],
        "fileSha256": file_hash,
        "compiledGraphHash": graph_hash,
        "compiledGraph": graph,
        "inspectionToken": token,
    }
    if document["schemaVersion"] in {BREAKABLE_SPEC_VERSION, DERIVED_TARGET_SPEC_VERSION}:
        result["contractVersion"] = contract_version
        result["breakableAttachmentCount"] = 1
    return result


def _node_map(document):
    return {node["id"]: node for node in document["nodes"]}


def _relation(document, kind):
    rows = [relation for relation in document["relations"] if relation["type"] == kind]
    if len(rows) != 1:
        raise PhysicsActionError("SPEC_SCHEMA", f"one {kind} relation required")
    return rows[0]


def _tag(obj, node_id, role):
    obj["film_studio_physics_action"] = GENERATED_TAG
    obj["film_studio_node_id"] = node_id
    obj["film_studio_semantic_role"] = role
    return obj


def _setup_scene(scene, document):
    light_build._clear_scene(scene)
    timeline = document["timeline"]
    scene.name = "FILM_STUDIO_PHYSICS_ACTION"
    scene.frame_start, scene.frame_end, scene.render.fps = timeline["frameStart"], timeline["frameEnd"], timeline["fps"]
    scene.gravity = Vector(document["world"]["gravityMetersPerSecondSquared"])
    scene.unit_settings.system, scene.unit_settings.scale_length = "METRIC", 1.0
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = document["cinematography"]["reviewResolution"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format, scene.render.image_settings.color_mode = "PNG", "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.world.color = (0.004, 0.007, 0.014)
    scene.view_settings.look = "AgX - Medium High Contrast"


def _active_from(node, obj):
    spec = node["rigidBody"]
    body = light_build._active(obj, spec["collisionShape"], spec["massKg"], spec["friction"], spec["restitution"], spec["linearDamping"], spec["angularDamping"])
    body.use_deactivation = False
    return body


def _passive_from(node, obj):
    body = light_build._passive(obj, node["rigidBody"]["collisionShape"], node["rigidBody"]["friction"])
    body.restitution = node["rigidBody"]["restitution"]
    return body


def _build_gate_graph(scene, document):
    nodes = _node_map(document)
    actor_node = next(node for node in document["nodes"] if node["factory"] == "GROOVED_CERAMIC_SPHERE")
    ramp_node = next(node for node in document["nodes"] if node["factory"] == "METRIC_WEDGE_WITH_SIDE_RAILS")
    gate_node = next(node for node in document["nodes"] if node["factory"] == "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE")
    receiver_node = next(node for node in document["nodes"] if node["factory"] == "MATTE_RELIEF_SIGNAL_WALL")
    light_node = next(node for node in document["nodes"] if node["factory"] == "STATIC_AREA_LIGHT")
    hinge_relation = _relation(document, "HINGED_TO_WORLD")

    dark = light_build._material("MAT_Action_Dark", (0.018, 0.028, 0.045), .36, .7)
    edge = light_build._material("MAT_Action_Edge", (0.04, .15, .2), .28, .75)
    ceramic = light_build._material("MAT_Action_Ceramic", (.42, .055, .018), .24, .08)
    groove = light_build._material("MAT_Action_Groove", (.015, .34, .42), .24, .38)
    receiver_mat = light_build._material("MAT_Action_Receiver", (.15, .18, .2), .62, .05)
    signal_mat = light_build._material("MAT_Action_Signal", (.36, .19, .035), .3, .6)
    floor = light_build._box("ACTION_FLOOR", (1, 0, -.08), (5.2, 2.4, .08), dark, "support_surface", .025)
    light_build._passive(floor, "BOX", .72)

    ramp_p, ramp_i = ramp_node["parameters"], ramp_node["initialCondition"]
    center_x, center_y, ground_z = ramp_i["location"]
    start_x, end_x = center_x - ramp_p["lengthMeters"] / 2, center_x + ramp_p["lengthMeters"] / 2
    ramp = light_build._wedge("ACTION_RAMP", start_x, end_x, ramp_p["riseMeters"], ramp_p["widthMeters"], ramp_p["deckThicknessMeters"], dark, ramp_node["rigidBody"]["friction"])
    for vertex in ramp.data.vertices:
        vertex.co.y += center_y
        vertex.co.z += ground_z
    _tag(ramp, ramp_node["id"], ramp_node["semanticRole"])
    angle = math.atan2(ramp_p["riseMeters"], ramp_p["lengthMeters"])
    slope_length = math.hypot(ramp_p["lengthMeters"], ramp_p["riseMeters"])
    for side in (-1, 1):
        rail = light_build._box(f"ACTION_RAIL_{side:+d}", (center_x, center_y + side * (ramp_p["widthMeters"] / 2 + .025), ground_z + ramp_p["riseMeters"] / 2 + .055), (slope_length / 2, .025, .055), edge, "motion_path_detail", .012, (0, angle, 0))
        light_build._passive(rail, "BOX", ramp_node["rigidBody"]["friction"])

    gate_p = gate_node["parameters"]
    door_x, gate_y, gate_z = gate_node["initialCondition"]["location"]
    width, height, thick = gate_p["apertureWidthMeters"], gate_p["apertureHeightMeters"], gate_p["shutterThicknessMeters"]
    shutter_width, shutter_height = width - .018, height - .018
    hinge_y, architecture_x = gate_y - width / 2 + .009, door_x - .15
    for name, location, scale in (
        ("LEFT", (architecture_x, gate_y - width / 2 - .1, gate_z), (.12, .1, height / 2 + .16)),
        ("RIGHT", (architecture_x, gate_y + width / 2 + .1, gate_z), (.12, .1, height / 2 + .16)),
        ("TOP", (architecture_x, gate_y, gate_z + height / 2 + .1), (.12, width / 2 + .2, .1)),
    ):
        light_build._box(f"ACTION_APERTURE_{name}", location, scale, edge, "architectural_frame", .025)
    hinge_post = light_build._box("ACTION_HINGE_POST", (door_x, hinge_y - .045, gate_z), (.035, .035, height / 2 + .12), edge, "hinge_hardware", .012)
    shutter = light_build._box("ACTION_HINGED_RESPONDER", (door_x, gate_y, gate_z), (thick / 2, shutter_width / 2, shutter_height / 2), dark, gate_node["semanticRole"], .014)
    _tag(shutter, gate_node["id"], gate_node["semanticRole"])
    shutter["film_studio_pose_source"] = "BLENDER_BULLET_RIGID_BODY_AND_HINGE_CONSTRAINT"
    _active_from(gate_node, shutter)
    anchor = light_build._box("ACTION_HINGE_ANCHOR", (door_x, hinge_y, gate_z), (.006, .006, .006), dark, "passive_hinge_anchor")
    anchor.hide_render, anchor.display_type = True, "WIRE"
    light_build._passive(anchor)
    for index in range(5):
        y = hinge_y + shutter_width * (index + .5) / 5
        rib = light_build._box(f"ACTION_SHUTTER_RIB_{index}", (door_x - thick * .58, y, gate_z), (.014, shutter_width / 12, height * .42), edge, "modeling_detail", .008)
        light_build._parent_keep(rib, shutter)
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(door_x, hinge_y, gate_z))
    hinge = _tag(bpy.context.object, "gate_hinge", "hinge_constraint")
    hinge.name = "ACTION_HINGE_CONSTRAINT"
    direction._select(hinge)
    bpy.ops.rigidbody.constraint_add()
    constraint = hinge.rigid_body_constraint
    constraint.type, constraint.object1, constraint.object2 = "HINGE", anchor, shutter
    constraint.disable_collisions, constraint.use_limit_ang_z = True, True
    constraint.limit_ang_z_lower, constraint.limit_ang_z_upper = map(math.radians, hinge_relation["limitsDegrees"])
    constraint.use_override_solver_iterations, constraint.solver_iterations = True, hinge_relation["solverIterations"]
    stop_degrees = hinge_relation["limitsDegrees"][1] - hinge_relation["passiveStopOffsetFromUpperLimitDegrees"]
    stop_angle = math.radians(stop_degrees)
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=max(.035, thick * .7), depth=max(.22, shutter_height * .28), location=(door_x + shutter_width * math.sin(stop_angle), hinge_y + shutter_width * math.cos(stop_angle), gate_z))
    stop = _tag(bpy.context.object, "gate_stop", "hinge_angular_stop")
    stop.name = "ACTION_HINGE_STOP"
    stop.data.materials.append(edge)
    light_build._passive(stop, "CYLINDER", .62)

    radius = actor_node["parameters"]["radiusMeters"]
    actor_location = Vector(actor_node["initialCondition"]["location"])
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=radius, location=actor_location)
    actor = _tag(bpy.context.object, actor_node["id"], actor_node["semanticRole"])
    actor.name = "ACTION_INITIATOR"
    actor.data.materials.append(ceramic)
    actor["film_studio_pose_source"] = "BLENDER_BULLET_RIGID_BODY"
    actor["film_studio_release_frame"] = actor_node["initialCondition"]["releaseFrame"]
    _active_from(actor_node, actor)
    for index, rotation in enumerate(((0, 0, 0), (math.pi / 2, 0, 0), (0, math.pi / 2, 0))):
        bpy.ops.mesh.primitive_torus_add(major_radius=radius * .985, minor_radius=radius * .04, major_segments=64, minor_segments=10, location=actor_location, rotation=rotation)
        ring = _tag(bpy.context.object, actor_node["id"], "rotation_witness")
        ring.name = f"ACTION_ACTOR_GROOVE_{index}"
        ring.data.materials.append(groove)
        light_build._parent_keep(ring, actor)

    receiver_p = receiver_node["parameters"]
    receiver = light_build._box("ACTION_RECEIVER", receiver_node["initialCondition"]["location"], (receiver_p["widthMeters"] / 2, receiver_p["heightMeters"] / 2, .018), receiver_mat, receiver_node["semanticRole"], .018)
    _tag(receiver, receiver_node["id"], receiver_node["semanticRole"])
    _passive_from(receiver_node, receiver)
    rx, ry, rz = receiver_node["initialCondition"]["location"]
    for index, offset in enumerate((-.1, .04, .18)):
        bar = light_build._box(f"ACTION_SIGNAL_BAR_{index}", (rx - .02 + index * .13, ry + offset, rz + .029), (.28 - index * .035, .035, .018), signal_mat, "revealed_detail", .012, (0, 0, -.3))
        light_build._passive(bar)

    light_p = light_node["parameters"]
    data = bpy.data.lights.new("ACTION_STATIC_KEY", "AREA")
    data.energy, data.color, data.shape, data.size = light_p["powerWatts"], light_p["colorLinearRgb"], "DISK", light_p["sizeMeters"]
    lamp = _tag(bpy.data.objects.new("ACTION_STATIC_KEY", data), light_node["id"], light_node["semanticRole"])
    scene.collection.objects.link(lamp)
    lamp.location = light_node["initialCondition"]["location"]
    direction._point_at(lamp, receiver.matrix_world.translation)
    fill_data = bpy.data.lights.new("ACTION_STATIC_FILL", "AREA")
    fill_data.energy, fill_data.color, fill_data.size = 115, (.06, .18, .32), 3
    fill = _tag(bpy.data.objects.new("ACTION_STATIC_FILL", fill_data), "fill", "static_fill")
    scene.collection.objects.link(fill)
    fill.location = (-.8, -.6, 3.2)
    direction._point_at(fill, (-.5, 0, 0))
    catcher = light_build._box("ACTION_CATCHER", (2, actor_location.y, .24), (.055, 1.2, .24), dark, "safety_catcher", .025)
    light_build._passive(catcher, "BOX", .7)
    world = scene.rigidbody_world
    if world is None:
        raise PhysicsActionError("PHYSICS_WORLD", "rigid body world missing")
    world.substeps_per_frame, world.solver_iterations = max(world.substeps_per_frame, 40), max(world.solver_iterations, hinge_relation["solverIterations"])
    world.point_cache.frame_start, world.point_cache.frame_end = scene.frame_start, scene.frame_end
    return {"actor": actor, "targets": [shutter], "shutter": shutter, "hinge": hinge, "hingePost": hinge_post, "stop": stop, "ramp": ramp, "receiver": receiver, "lights": [lamp, fill], "primaryLight": lamp, "shutterWidth": shutter_width, "apertureHeight": height, "shutterThickness": thick, "stopDegrees": stop_degrees}


def _bottle_document(document, actor_node, bottles_node):
    timeline = document["timeline"]
    release = actor_node["initialCondition"]["releaseFrame"]
    fps = timeline["fps"]
    start = Vector(actor_node["initialCondition"]["location"])
    velocity = Vector(actor_node["initialCondition"]["preReleaseVelocityMetersPerSecond"])
    pre_frame = release - 1
    pre = start + velocity * ((pre_frame - timeline["frameStart"]) / fps)
    radius = actor_node["parameters"]["radiusMeters"]
    travel = (pre - start).length
    bp = bottles_node["parameters"]
    height, body_radius = bp["heightMeters"], bp["bodyRadiusMeters"]
    group = Vector(bottles_node["initialCondition"]["location"])
    bases = [[group[i] + position[i] for i in range(3)] for position in bp["initialBasePositions"]]
    profile = [[body_radius * ratio, height * z] for ratio, z in ((.88, 0), (1, .03), (1, .63), (.97, .73), (.74, .81), (.43, .88), (.4, .97), (.43, 1))]
    rigid = bottles_node["rigidBody"]
    realism = bp["materialPreset"] in {"HOUSEHOLD_GLASS_WITH_VISIBLE_FILL_AND_VARIATION", "MOLDED_HOUSEHOLD_GLASS_WITH_CONTACT_OVALITY"}
    variation = {
        "basisSceneSpecHash": document.get("physicalVariationBasisSpecHash", document["physicsActionSpecHash"]),
        "seed": 84017 if realism else 31337,
        "positionJitterMetersMaximum": .006 if realism else 0,
        "yawJitterDegreesMaximum": 4 if realism else 0,
        "frictionJitterMaximum": .025 if realism else 0,
        "restitutionJitterMaximum": .015 if realism else 0,
    }
    body_palette = [[.16, .23, .19], [.18, .21, .17], [.13, .2, .2]] if realism else [[.76, .9, .94], [.9, .82, .62], [.78, .9, .72]]
    label_palette = [[.64, .095, .045], [.045, .22, .42], [.55, .31, .055]] if realism else [[.92, .38, .06], [.03, .42, .68], [.68, .12, .28]]
    liquid_palette = [[.055, .23, .12], [.44, .16, .028], [.09, .25, .31]] if realism else [[.15, .52, .78], [.92, .56, .08], [.22, .62, .31]]
    return {
        "schemaVersion": "bfs.causalSceneSpec.v0.5",
        "sceneSpecHash": document["physicsActionSpecHash"],
        "timeline": {**timeline, "releaseFrame": release},
        "dynamicActor": {
            "semanticRole": "dynamic_actor", "factory": "GROOVED_SPHERE", "count": 1, "radius": radius,
            "initialPosition": list(start),
            "launchWaypoints": [
                {"frame": timeline["frameStart"], "position": list(start), "rotationY": 0},
                {"frame": pre_frame, "position": list(pre), "rotationY": -travel / radius},
            ],
            "rigidBody": {"mass": actor_node["rigidBody"]["massKg"], "collisionShape": "SPHERE", "collisionMarginMeters": .0007, "friction": actor_node["rigidBody"]["friction"], "restitution": actor_node["rigidBody"]["restitution"], "linearDamping": actor_node["rigidBody"]["linearDamping"], "angularDamping": actor_node["rigidBody"]["angularDamping"]},
            "material": {"baseColor": [.72, .16, .035], "roughness": .48, "proceduralGrain": True, "channelCount": 3},
        },
        "targetGroup": {
            "semanticRole": "target_group", "factory": "FILLED_LATHED_BOTTLE", "count": bp["count"], "initialBasePositions": bases,
            "physicalArchetype": {"heightMeters": height, "bodyRadiusMeters": body_radius, "neckRadiusMeters": body_radius * .43, "interiorBaseHeightMeters": height * .0214, "liquidBodyHeightMeters": height * .6964, "containerMassKg": .038, "nominalCapacityLiters": .65, "liquidDensityKgPerLiter": .998, "containerCenterOfMassHeightRatio": .48, "fillFractions": bp["fillFractions"], "massStrategy": "CONTAINER_PLUS_LIQUID_FROM_FILL", "centerOfMassStrategy": "WEIGHTED_CONTAINER_AND_LIQUID_COLUMN"},
            "rigidBody": {"collisionShape": "CONVEX_HULL", "collisionMarginMeters": .0007, "friction": rigid["friction"], "restitution": rigid["restitution"], "linearDamping": rigid["linearDamping"], "angularDamping": rigid["angularDamping"]},
            "modeling": {"profileSegments": 64, "profileMeters": profile, "wallThicknessMeters": .0014, "capHeightMeters": height * .064, "capRadiusMeters": body_radius * .47, "capRidgeCount": 24, "labelHeightRatio": .25, "requiredReadableStages": ["base", "body", "shoulder", "neck", "cap", "label", "liquid_fill"]},
            "deterministicVariation": variation,
            "bodyPalette": body_palette[:bp["count"]],
            "labelPalette": label_palette[:bp["count"]],
            "liquidPalette": liquid_palette[:bp["count"]],
        },
    }


def _derive_release_ray_target_binding(document, synthetic):
    actor_node = next(node for node in document["nodes"] if node["factory"] == "GROOVED_BASKETBALL")
    start = Vector((*actor_node["initialCondition"]["location"][:2], 0.0))
    velocity = Vector((*actor_node["initialCondition"]["preReleaseVelocityMetersPerSecond"][:2], 0.0))
    if velocity.length <= 1e-8:
        raise PhysicsActionError("ATTACHMENT_TARGET", "initiator release ray has zero speed")
    direction_vector = velocity.normalized()
    target_group = synthetic["targetGroup"]
    variation = target_group["deterministicVariation"]

    def sample(member_number, channel):
        token = f"{variation['basisSceneSpecHash']}:{variation['seed']}:{member_number}:{channel}".encode("utf-8")
        integer = int.from_bytes(hashlib.sha256(token).digest()[:8], "big")
        return integer / (2 ** 64 - 1) * 2.0 - 1.0

    rows = []
    for index, base in enumerate(target_group["initialBasePositions"]):
        member_number = index + 1
        center = Vector((
            base[0] + sample(member_number, "position-x") * variation["positionJitterMetersMaximum"],
            base[1] + sample(member_number, "position-y") * variation["positionJitterMetersMaximum"],
            0.0,
        ))
        offset = center - start
        forward = offset.dot(direction_vector)
        perpendicular = abs(offset.x * direction_vector.y - offset.y * direction_vector.x)
        rows.append({"memberIndex": index, "centerMeters": [round(center.x, 8), round(center.y, 8)], "forwardProjectionMeters": round(forward, 8), "perpendicularDistanceMeters": round(perpendicular, 8)})
    eligible = sorted((row for row in rows if row["forwardProjectionMeters"] > 0), key=lambda row: (row["perpendicularDistanceMeters"], row["forwardProjectionMeters"], row["memberIndex"]))
    if len(eligible) < 2 or eligible[1]["perpendicularDistanceMeters"] - eligible[0]["perpendicularDistanceMeters"] < .002:
        raise PhysicsActionError("ATTACHMENT_TARGET", "release ray does not identify one unique target member")
    return {
        "policy": "MINIMUM_DISTANCE_TO_INITIATOR_RELEASE_RAY",
        "source": "METRIC_INITIAL_CONDITIONS_BEFORE_SCENE_MUTATION",
        "physicalVariationBasisSpecHash": variation["basisSceneSpecHash"],
        "selectedMemberIndex": eligible[0]["memberIndex"],
        "uniquenessMarginMeters": round(eligible[1]["perpendicularDistanceMeters"] - eligible[0]["perpendicularDistanceMeters"], 8),
        "candidates": rows,
    }


def _apply_molded_glass_contact_ovality(targets):
    amplitude, lobes = .00045, 2
    phases = []
    for index, target in enumerate(targets, 1):
        phase = math.radians((index * 57) % 180)
        phases.append([index, round(math.degrees(phase), 8)])
        for vertex in target.data.vertices:
            radius = math.hypot(vertex.co.x, vertex.co.y)
            if radius > 1e-8:
                offset = min(amplitude, radius * .01) * math.cos(lobes * (math.atan2(vertex.co.y, vertex.co.x) - phase))
                scale = (radius + offset) / radius
                vertex.co.x, vertex.co.y = vertex.co.x * scale, vertex.co.y * scale
        target.data.update()
        target["film_studio_contact_ovality_amplitude_m"] = amplitude
        target["film_studio_contact_ovality_lobes"] = lobes
    return {"source": "MATERIAL_PRESET_FACTORY_DEFAULT", "preset": "MOLDED_HOUSEHOLD_GLASS_WITH_CONTACT_OVALITY", "radialAmplitudeMeters": amplitude, "harmonicLobes": lobes, "memberPhaseDegrees": phases, "visibleMeshIsCollisionHullSource": True, "solverSleep": False}


def _build_bottle_graph(scene, document):
    actor_node = next(node for node in document["nodes"] if node["factory"] == "GROOVED_BASKETBALL")
    bottles_node = next(node for node in document["nodes"] if node["factory"] == "FILLED_LATHED_BOTTLE_ARRAY")
    floor_node = next(node for node in document["nodes"] if node["factory"] == "BEVELED_GROUND_PLANE")
    light_node = next(node for node in document["nodes"] if node["factory"] == "STATIC_AREA_LIGHT")
    cap_nodes = [node for node in document["nodes"] if node["factory"] == "BREAKAWAY_BOTTLE_CAP"]
    dark = causal._material("MAT_Action_BottleDark", (.008, .009, .012), .38)
    synthetic = _bottle_document(document, actor_node, bottles_node)
    attachment_derivation = None
    if cap_nodes and "targetMemberPolicy" in cap_nodes[0]["parameters"]:
        attachment_derivation = _derive_release_ray_target_binding(document, synthetic)
    actor, actor_details = causal._create_actor(synthetic, dark)
    _tag(actor, actor_node["id"], actor_node["semanticRole"])
    ball_look = None
    if actor_node["parameters"]["materialPreset"] == "WORN_GAME_BASKETBALL":
        ball_look = physical_look.enhance_basketball(actor)
    targets, target_details, derived = causal._create_metric_bottle_targets(synthetic, dark)
    for target in targets:
        _tag(target, bottles_node["id"], bottles_node["semanticRole"])
    contact_geometry = None
    if bottles_node["parameters"]["materialPreset"] == "MOLDED_HOUSEHOLD_GLASS_WITH_CONTACT_OVALITY":
        contact_geometry = _apply_molded_glass_contact_ovality(targets)
    bottle_look = None
    if bottles_node["parameters"]["materialPreset"] in {"HOUSEHOLD_GLASS_WITH_VISIBLE_FILL_AND_VARIATION", "MOLDED_HOUSEHOLD_GLASS_WITH_CONTACT_OVALITY"}:
        bottle_look = physical_look.enhance_glass_bottles(
            targets,
            target_details,
            derived,
            bottles_node["parameters"]["heightMeters"],
            bottles_node["parameters"]["bodyRadiusMeters"],
            synthetic["targetGroup"]["modeling"]["wallThicknessMeters"],
        )
        target_details.extend(bottle_look["addedObjects"])
    secondary, breakable_constraint, attachment_target = [], None, None
    if cap_nodes:
        cap_node = cap_nodes[0]
        attachment_relation = _relation(document, "BREAKABLE_FIXED_TO")
        target_index = cap_node["parameters"].get("targetMemberIndex")
        if target_index is None:
            target_index = attachment_derivation["selectedMemberIndex"]
        attachment_target = targets[target_index]
        cap = bpy.data.objects.get(f"CAUSAL_DETAIL_BottleCap_{target_index + 1:02d}")
        if cap is None:
            raise PhysicsActionError("FACTORY_BUILD", "derived bottle cap detail is missing")
        cap_world = cap.matrix_world.copy()
        cap.parent = None
        cap.matrix_world = cap_world
        _tag(cap, cap_node["id"], cap_node["semanticRole"])
        cap["film_studio_pose_source"] = "BLENDER_BULLET_BREAKABLE_FIXED_ATTACHMENT"
        cap["film_studio_attachment_target"] = attachment_target.name
        _active_from(cap_node, cap)
        bpy.ops.object.empty_add(type="PLAIN_AXES", location=cap.matrix_world.translation)
        constraint_object = _tag(bpy.context.object, attachment_relation["id"], "breakable_fixed_constraint")
        constraint_object.name = "ACTION_BREAKABLE_CAP_CONSTRAINT"
        direction._select(constraint_object)
        bpy.ops.rigidbody.constraint_add()
        breakable_constraint = constraint_object.rigid_body_constraint
        breakable_constraint.type = "FIXED"
        breakable_constraint.object1 = attachment_target
        breakable_constraint.object2 = cap
        breakable_constraint.disable_collisions = attachment_relation["disableCollisions"]
        breakable_constraint.use_breaking = True
        breakable_constraint.breaking_threshold = attachment_relation["breakingImpulseThreshold"]
        breakable_constraint.use_override_solver_iterations = True
        breakable_constraint.solver_iterations = attachment_relation["solverIterations"]
        secondary.append(cap)
    fp = floor_node["parameters"]
    floor = light_build._box("ACTION_GROUND", floor_node["initialCondition"]["location"], (fp["widthMeters"] / 2, fp["depthMeters"] / 2, fp["thicknessMeters"] / 2), causal._material("MAT_Action_Ground", (.105, .08, .055), .34), floor_node["semanticRole"], .025)
    _tag(floor, floor_node["id"], floor_node["semanticRole"])
    _passive_from(floor_node, floor)
    environment_look = None
    if floor_node["parameters"]["materialPreset"] == "AGED_MAPLE_COURT_WITH_SCALE_CUES":
        environment_look = physical_look.build_aged_court(scene, floor, fp, floor_node["initialCondition"]["location"])
    lp = light_node["parameters"]
    data = bpy.data.lights.new("ACTION_STATIC_KEY", "AREA")
    data.energy, data.color, data.size = lp["powerWatts"], lp["colorLinearRgb"], lp["sizeMeters"]
    lamp = _tag(bpy.data.objects.new("ACTION_STATIC_KEY", data), light_node["id"], light_node["semanticRole"])
    scene.collection.objects.link(lamp)
    lamp.location = light_node["initialCondition"]["location"]
    direction._point_at(lamp, Vector(bottles_node["initialCondition"]["location"]) + Vector((0, 0, .12)))
    fill_data = bpy.data.lights.new("ACTION_STATIC_FILL", "AREA")
    fill_data.energy, fill_data.color, fill_data.size = 320, (.28, .46, 1), 2.2
    fill = _tag(bpy.data.objects.new("ACTION_STATIC_FILL", fill_data), "fill", "static_fill")
    scene.collection.objects.link(fill)
    fill.location = (1.4, 1.5, 1.9)
    direction._point_at(fill, (0, 0, .1))
    world = scene.rigidbody_world
    if world is None:
        raise PhysicsActionError("PHYSICS_WORLD", "rigid body world missing")
    requested_iterations = 60
    if breakable_constraint is not None:
        requested_iterations = _relation(document, "BREAKABLE_FIXED_TO")["solverIterations"]
    world.substeps_per_frame, world.solver_iterations = max(world.substeps_per_frame, 20), max(world.solver_iterations, requested_iterations)
    world.point_cache.frame_start, world.point_cache.frame_end = scene.frame_start, scene.frame_end
    return {
        "actor": actor,
        "actorDetails": actor_details,
        "targets": targets,
        "secondary": secondary,
        "breakableConstraint": breakable_constraint,
        "attachmentTarget": attachment_target,
        "attachmentTargetDerivation": attachment_derivation,
        "contactGeometry": contact_geometry,
        "targetDetails": target_details,
        "floor": floor,
        "lights": [lamp, fill],
        "primaryLight": lamp,
        "derivedPhysicalArchetypes": derived,
        "actorRadius": actor_node["parameters"]["radiusMeters"],
        "targetRadius": bottles_node["parameters"]["bodyRadiusMeters"],
        "releaseFrame": actor_node["initialCondition"]["releaseFrame"],
        "physicalLook": {"basketball": ball_look, "bottles": None if bottle_look is None else {key: value for key, value in bottle_look.items() if key != "addedObjects"}, "environment": None if environment_look is None else {key: value for key, value in environment_look.items() if key != "objects"}},
        "environmentObjects": [] if environment_look is None else environment_look["objects"],
    }


def _tilt(obj):
    up = obj.matrix_world.to_3x3() @ Vector((0, 0, 1))
    return math.degrees(math.acos(max(-1, min(1, up.normalized().dot(Vector((0, 0, 1)))))))


def _simulate_gate(scene, created, document):
    actor_node = next(node for node in document["nodes"] if node["factory"] == "GROOVED_CERAMIC_SPHERE")
    gate_node = next(node for node in document["nodes"] if node["factory"] == "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE")
    old = {
        "timeline": document["timeline"],
        "rollingActor": {"radiusMeters": actor_node["parameters"]["radiusMeters"]},
    }
    adapter = {"ball": created["actor"], "shutter": created["shutter"], "shutterWidth": gate_node["parameters"]["apertureWidthMeters"] - .018, "apertureHeight": gate_node["parameters"]["apertureHeightMeters"], "shutterThickness": gate_node["parameters"]["shutterThicknessMeters"]}
    return light_build._simulate(scene, adapter, old)


def _simulate_bottles(scene, created, document):
    start, end = document["timeline"]["frameStart"], document["timeline"]["frameEnd"]
    actor, targets = created["actor"], created["targets"]
    secondary = created.get("secondary", [])
    initial_actor = actor.matrix_world.translation.copy()
    initial = {obj.name: obj.matrix_world.translation.copy() for obj in targets}
    initial_tilts = {obj.name: _tilt(obj) for obj in targets}
    previous_actor = previous_tilts = previous_targets = None
    previous_secondary_position = previous_secondary_rotation = None
    attachment_relative = secondary_initial_rotation = None
    floor_top = None
    if secondary:
        cap = secondary[0]
        attachment_target = created["attachmentTarget"]
        attachment_relative = attachment_target.matrix_world.inverted() @ cap.matrix_world
        secondary_initial_rotation = cap.matrix_world.to_quaternion().copy()
        floor_top = max((created["floor"].matrix_world @ Vector(corner)).z for corner in created["floor"].bound_box)
    responses = {obj.name: None for obj in targets}
    rows = []
    for frame in range(start, end + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        actor_position = actor.matrix_world.translation.copy()
        actor_step = 0 if previous_actor is None else (actor_position - previous_actor).length
        gaps = {obj.name: max(0.0, (actor_position - obj.matrix_world.translation).length - created["actorRadius"] - created["targetRadius"]) for obj in targets}
        tilts = {obj.name: _tilt(obj) for obj in targets}
        angular = {name: 0 if previous_tilts is None else abs(tilts[name] - previous_tilts[name]) for name in tilts}
        target_positions = {obj.name: obj.matrix_world.translation.copy() for obj in targets}
        target_steps = {name: 0 if previous_targets is None else (target_positions[name] - previous_targets[name]).length for name in target_positions}
        displacements = {obj.name: (Vector((obj.matrix_world.translation.x, obj.matrix_world.translation.y)) - Vector((initial[obj.name].x, initial[obj.name].y))).length for obj in targets}
        for obj in targets:
            if responses[obj.name] is None and (tilts[obj.name] - initial_tilts[obj.name] >= 1 or displacements[obj.name] >= .012):
                responses[obj.name] = frame
        row = {"frame": frame, "actorLocation": list(actor_position), "actorStepMeters": actor_step, "minimumContactGapMeters": min(gaps.values()), "targetGapMeters": gaps, "targetTiltDegrees": tilts, "targetAngularStepDegrees": angular, "maximumTargetTranslationStepMeters": max(target_steps.values()), "activeTargetCount": sum(value >= .25 for value in angular.values()), "aggregateAngularStepDegrees": sum(angular.values())}
        if secondary:
            cap = secondary[0]
            cap_position = cap.matrix_world.translation.copy()
            cap_rotation = cap.matrix_world.to_quaternion().copy()
            expected_cap = attachment_target.matrix_world @ attachment_relative
            separation = (cap_position - expected_cap.translation).length
            cap_step = 0 if previous_secondary_position is None else (cap_position - previous_secondary_position).length
            cap_angular_step = 0 if previous_secondary_rotation is None else math.degrees(previous_secondary_rotation.rotation_difference(cap_rotation).angle)
            cap_angular_response = math.degrees(secondary_initial_rotation.rotation_difference(cap_rotation).angle)
            cap_minimum_z = min((cap.matrix_world @ Vector(corner)).z for corner in cap.bound_box)
            row.update({
                "capLocation": list(cap_position),
                "capAttachmentSeparationMeters": separation,
                "capTranslationStepMeters": cap_step,
                "capAngularStepDegrees": cap_angular_step,
                "capAngularResponseDegrees": cap_angular_response,
                "capFloorPenetrationMeters": max(0.0, floor_top - cap_minimum_z),
            })
            previous_secondary_position, previous_secondary_rotation = cap_position, cap_rotation
        rows.append(row)
        previous_actor, previous_tilts, previous_targets = actor_position, tilts, target_positions
    tolerance = _relation(document, "COLLIDES_WITH")["contactToleranceMeters"]
    contact = next((row["frame"] for row in rows if row["frame"] >= created["releaseFrame"] and row["minimumContactGapMeters"] <= tolerance), None)
    response_values = [value for value in responses.values() if value is not None]
    first_response = min(response_values) if response_values else None
    if contact is None or first_response is None:
        nearest = min(rows, key=lambda row: row["minimumContactGapMeters"])
        raise PhysicsActionError("PHYSICS_RESPONSE", f"no derived contact/response; minimum gap {nearest['minimumContactGapMeters']:.8f} at frame {nearest['frame']}")
    candidates = [row for row in rows if row["frame"] >= first_response]
    peak = max(candidates, key=lambda row: (row["activeTargetCount"], row["aggregateAngularStepDegrees"], -row["frame"]))
    final = rows[-1]["targetTiltDegrees"]
    contact_rows = {row["frame"]: row for row in rows}
    continuous = all(contact_rows.get(frame, {}).get("actorStepMeters", 0) > .001 for frame in (contact - 1, contact, contact + 1) if frame in contact_rows)
    result = {
        "source": "BLENDER_BULLET_EVALUATED_WORLD_TRANSFORMS",
        "contactFrame": contact,
        "firstResponseFrame": first_response,
        "firstResponseDelayFrames": first_response - contact,
        "targetResponseFrames": responses,
        "respondingTargetCount": len(response_values),
        "peakGroupResponseFrame": peak["frame"],
        "peakActiveTargetCount": peak["activeTargetCount"],
        "peakAggregateAngularStepDegrees": round(peak["aggregateAngularStepDegrees"], 8),
        "finalTiltDegrees": {name: round(value, 8) for name, value in final.items()},
        "actorTravelMeters": round(max((Vector(row["actorLocation"]) - initial_actor).length for row in rows), 8),
        "continuousActorMotionThroughContact": continuous,
        "samples": [{"frame": row["frame"], "actorLocation": [round(value, 8) for value in row["actorLocation"]], "actorStepMeters": round(row["actorStepMeters"], 8), "minimumContactGapMeters": round(row["minimumContactGapMeters"], 8), "targetTiltDegrees": {name: round(value, 8) for name, value in row["targetTiltDegrees"].items()}, "targetAngularStepDegrees": {name: round(value, 8) for name, value in row["targetAngularStepDegrees"].items()}, "maximumTargetTranslationStepMeters": round(row["maximumTargetTranslationStepMeters"], 8)} for row in rows],
    }
    if secondary:
        attachment_relation = _relation(document, "BREAKABLE_FIXED_TO")
        precontact = [row for row in rows if row["frame"] <= contact - 1]
        detached = next((row for row in rows if row["frame"] >= contact and row["capAttachmentSeparationMeters"] >= .035), None)
        response = next((row for row in rows if row["frame"] >= contact and row["capAttachmentSeparationMeters"] >= .005), None)
        continuous_rows = [] if detached is None else [row for row in rows if detached["frame"] <= row["frame"] <= detached["frame"] + 2]
        result["breakableAttachment"] = {
            "source": "BLENDER_BULLET_BREAKABLE_FIXED_CONSTRAINT",
            "constraintType": "FIXED",
            "breakingEnabled": True,
            "breakingImpulseThreshold": attachment_relation["breakingImpulseThreshold"],
            "solverIterations": attachment_relation["solverIterations"],
            "attachedBodyCollisionsDisabled": attachment_relation["disableCollisions"],
            "attachmentTarget": created["attachmentTarget"].name,
            "attachmentTargetDerivation": created.get("attachmentTargetDerivation"),
            "secondaryBody": secondary[0].name,
            "maximumPrecontactAttachmentSeparationMeters": round(max(row["capAttachmentSeparationMeters"] for row in precontact), 8),
            "responseFrame": None if response is None else response["frame"],
            "detachmentFrame": None if detached is None else detached["frame"],
            "detachmentDelayFrames": None if detached is None else detached["frame"] - contact,
            "maximumAttachmentSeparationMeters": round(max(row["capAttachmentSeparationMeters"] for row in rows), 8),
            "maximumAngularResponseDegrees": round(max(row["capAngularResponseDegrees"] for row in rows), 8),
            "maximumFloorPenetrationMeters": round(max(row["capFloorPenetrationMeters"] for row in rows), 8),
            "continuousMotionForThreeFramesFromDetachment": len(continuous_rows) == 3 and all(row["capTranslationStepMeters"] > .0001 for row in continuous_rows[1:]),
            "eventFrameSource": "DERIVED_FROM_PRIMARY_CONTACT_AND_EVALUATED_ATTACHMENT_SEPARATION",
            "authoredBreakFrame": False,
            "authoredDetachedPose": False,
            "authoredDetachmentVelocity": False,
        }
        result["samples"] = [
            {**sample, "capLocation": [round(value, 8) for value in row["capLocation"]], "capAttachmentSeparationMeters": round(row["capAttachmentSeparationMeters"], 8), "capTranslationStepMeters": round(row["capTranslationStepMeters"], 8), "capAngularStepDegrees": round(row["capAngularStepDegrees"], 8), "capAngularResponseDegrees": round(row["capAngularResponseDegrees"], 8), "capFloorPenetrationMeters": round(row["capFloorPenetrationMeters"], 8)}
            for sample, row in zip(result["samples"], rows)
        ]
    if any(beat["deriveFrom"] == "SETTLED_GROUP_RESPONSE" for beat in document["beats"]):
        window_count = 10
        settled_window = None
        for index, row in enumerate(rows):
            if row["frame"] < peak["frame"]:
                continue
            window = rows[index:index + window_count]
            if len(window) < window_count:
                break
            if all(item["aggregateAngularStepDegrees"] <= .25 and item["maximumTargetTranslationStepMeters"] <= .0015 for item in window):
                settled_window = window
                break
        if settled_window is None:
            complete_windows = [rows[index:index + window_count] for index in range(len(rows) - window_count + 1) if rows[index]["frame"] >= peak["frame"]]
            best = min(complete_windows, key=lambda window: max(max(row["aggregateAngularStepDegrees"] for row in window) / .25, max(row["maximumTargetTranslationStepMeters"] for row in window) / .0015))
            best_angular = max(row["aggregateAngularStepDegrees"] for row in best)
            best_translation = max(row["maximumTargetTranslationStepMeters"] for row in best)
            raise PhysicsActionError("PHYSICS_SETTLE", f"no derived ten-frame group settle window; best frames {best[0]['frame']}-{best[-1]['frame']} angular {best_angular:.8f} translation {best_translation:.8f}")
        result["settledWindowStartFrame"] = settled_window[0]["frame"]
        result["settledGroupFrame"] = settled_window[-1]["frame"]
        result["settledWindowFrameCount"] = len(settled_window)
        result["settledMaximumAggregateAngularStepDegrees"] = round(max(row["aggregateAngularStepDegrees"] for row in settled_window), 8)
        result["settledMaximumTargetTranslationStepMeters"] = round(max(row["maximumTargetTranslationStepMeters"] for row in settled_window), 8)
    return result


def _semantic_center(objects):
    points = direction._world_points(objects)
    return sum(points, Vector()) / len(points)


def _camera(name, frame, objects, direction_vector, occupancy):
    center = _semantic_center(objects)
    return light_build._camera(name, frame, center, objects, direction_vector, occupancy)


def _secondary_readable_camera(scene, name, record_frame, readability_frame, objects, secondary, occupancy):
    scene.frame_set(readability_frame)
    center = _semantic_center(objects)
    camera, _ = _camera(name, readability_frame, objects, (1, 0, -.32), occupancy)
    candidates = []
    for azimuth in range(0, 360, 45):
        angle = math.radians(azimuth)
        vector = (math.cos(angle), math.sin(angle), -.32)
        camera.location = center - Vector(vector).normalized() * 5.0
        direction._point_at(camera, center)
        fit = direction._fit_camera(scene, camera, objects, occupancy, vector)
        camera.data.dof.focus_distance = (center - camera.location).length
        projected = {obj.name: world_to_camera_view(scene, camera, obj.matrix_world.translation) for obj in objects}
        in_frame = all(point.z > 0 and 0 <= point.x <= 1 and 0 <= point.y <= 1 for point in projected.values())
        cap_point = projected[secondary.name]
        separation = min(math.hypot(cap_point.x - point.x, cap_point.y - point.y) for obj_name, point in projected.items() if obj_name != secondary.name)
        candidates.append({"azimuthDegrees": azimuth, "direction": [round(value, 8) for value in vector], "allSemanticCentersInFrame": in_frame, "minimumSecondaryCenterSeparationNormalized": round(separation, 8), "_matrix": camera.matrix_world.copy(), "_lens": camera.data.lens, "_focus": camera.data.dof.focus_distance, "_fit": fit})
    selected = max(candidates, key=lambda row: (row["allSemanticCentersInFrame"], row["minimumSecondaryCenterSeparationNormalized"], -row["azimuthDegrees"]))
    camera.matrix_world, camera.data.lens, camera.data.dof.focus_distance = selected["_matrix"], selected["_lens"], selected["_focus"]
    evidence = {key: value for key, value in selected.items() if not key.startswith("_")}
    evidence.update({"source": "BOUNDED_PROJECTED_SECONDARY_READABILITY", "readabilityFrame": readability_frame, "recordFrame": record_frame, "candidateCount": len(candidates), "candidateCameraObjectCount": 1, "candidateCameraObjectDeletions": 0, "losingCandidateCameraObjectsRetained": 0, "candidates": [{key: value for key, value in row.items() if not key.startswith("_")} for row in candidates]})
    return camera, {**selected["_fit"], "secondaryReadability": evidence}


def _configure_cameras(scene, created, physics, document, topology):
    start, end = scene.frame_start, scene.frame_end
    offset = next(beat["offsetFrames"] for beat in document["beats"] if beat["id"] == "cause")
    contact = physics["contactFrame"]
    cause_frame = max(start, contact + offset)
    contact_frame = min(end, contact)
    effect_beat = next(beat for beat in document["beats"] if beat["id"] == "effect")
    if effect_beat["deriveFrom"] == "SETTLED_GROUP_RESPONSE":
        effect_frame = min(end, physics["settledGroupFrame"])
    else:
        effect_frame = min(end, physics.get("peakGroupResponseFrame", physics.get("peakOpenFrame", contact_frame + 12)))
    actor, targets = created["actor"], created["targets"]
    secondary = created.get("secondary", [])
    physical_environment = created.get("physicalLook", {}).get("environment")
    close_physical_staging = bool(physical_environment and physical_environment.get("preset") == "AGED_MAPLE_COURT_WITH_SCALE_CUES")
    cause_objects = [actor, *targets] if close_physical_staging else [actor, created.get("ramp", created.get("floor")), *targets]
    contact_objects = [actor, *targets, *secondary]
    effect_objects = [*targets, *secondary] if effect_beat["deriveFrom"] == "SETTLED_GROUP_RESPONSE" else [*targets, *secondary, actor]
    if topology == "HINGE_LIGHT":
        effect_objects.insert(0, created["receiver"])
    directions = ((1, .78, -.48), (.9, 1, -.25), (1, .12, -.55))
    if close_physical_staging:
        directions = (directions[0], directions[1], (1, .24, -.28))
    occupancies = (.58, .7, .64) if close_physical_staging else (.66, .68, .66)
    records, cameras = {}, {}
    for role, frame, objects, vector, occupancy in zip(
        ("cause", "contact", "effect"),
        (cause_frame, contact_frame, effect_frame),
        (cause_objects, contact_objects, effect_objects),
        directions,
        occupancies,
    ):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        objects = [obj for obj in objects if obj]
        if secondary and role in {"contact", "effect"} and physics.get("breakableAttachment", {}).get("detachmentFrame") is not None:
            readability_frame = min(end, physics["breakableAttachment"]["detachmentFrame"] + 2) if role == "contact" else frame
            camera, fit = _secondary_readable_camera(scene, f"ACTION_CAM_{role.upper()}", frame, readability_frame, objects, secondary[0], occupancy)
        else:
            camera, fit = _camera(f"ACTION_CAM_{role.upper()}", frame, objects, vector, occupancy)
        marker = scene.timeline_markers.new(f"ACTION_{role.upper()}", frame=frame)
        marker.camera = camera
        records[role], cameras[role] = {"frame": frame, "camera": camera.name, **fit}, camera
    blur_spec = document["cinematography"]["motionBlur"]
    measurement_frame = max(start + 1, contact_frame)
    camera = cameras["contact"]
    projected = {}
    for frame in (measurement_frame - 1, measurement_frame):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        projected[frame] = {obj.name: world_to_camera_view(scene, camera, obj.matrix_world.translation) for obj in [actor, *targets, *secondary]}
    width, height = document["cinematography"]["reviewResolution"]
    speeds = {name: math.hypot((projected[measurement_frame][name].x - projected[measurement_frame - 1][name].x) * width, (projected[measurement_frame][name].y - projected[measurement_frame - 1][name].y) * height) for name in projected[measurement_frame]}
    moving = [value for value in speeds.values() if value > 1e-8]
    if not moving:
        raise PhysicsActionError("MOTION_BLUR_MEASUREMENT", "projected contact motion is zero")
    median_speed = statistics.median(moving)
    shutter = round(max(.08, min(.5, blur_spec["targetBlurPixels"] / median_speed)), 8)
    scene.render.use_motion_blur, scene.render.motion_blur_shutter, scene.render.motion_blur_position = True, shutter, blur_spec["position"]
    scene.camera = cameras["cause"]
    scene.frame_set(start)
    records["motionBlur"] = {"source": "BULLET_EVALUATED_PROJECTED_MOTION", "measurementFrame": measurement_frame, "objectPixelsPerFrame": {key: round(value, 8) for key, value in speeds.items()}, "medianMovingPixelsPerFrame": round(median_speed, 8), "targetBlurPixels": blur_spec["targetBlurPixels"], "computedShutterFrames": shutter, "achievedMedianBlurPixels": round(median_speed * shutter, 8), "position": "CENTER", "nativeTransformMotionBlur": True, "compositorOrPostprocessBlur": False}
    return records


def _transform_keys_at_or_after(obj, frame):
    if not obj.animation_data or not obj.animation_data.action:
        return 0
    curves = getattr(obj.animation_data.action, "fcurves", [])
    return sum(1 for curve in curves if curve.data_path in {"location", "rotation_euler", "rotation_quaternion", "scale"} for point in curve.keyframe_points if point.co.x >= frame)


def execute_physics_action(repository_root, spec_uri, inspection_token, scene=None):
    document, file_hash, graph, graph_hash, expected_token = _inspection(repository_root, spec_uri)
    if not isinstance(inspection_token, str) or inspection_token != expected_token:
        raise PhysicsActionError("INSPECTION_TOKEN_MISMATCH", "Inspect the exact physics graph before mutation")
    scene = scene or bpy.context.scene
    _setup_scene(scene, document)
    topology = {relation["type"] for relation in document["relations"]}
    if "HINGED_TO_WORLD" in topology:
        topology_name = "HINGE_LIGHT"
        created = _build_gate_graph(scene, document)
        physics = _simulate_gate(scene, created, document)
    else:
        topology_name = "GROUP_RESPONSE"
        created = _build_bottle_graph(scene, document)
        physics = _simulate_bottles(scene, created, document)
    cinematography = _configure_cameras(scene, created, physics, document, topology_name)
    scene.frame_set(scene.frame_start)
    with bpy.context.temp_override(point_cache=scene.rigidbody_world.point_cache):
        bpy.ops.ptcache.bake(bake=True)
    scene.frame_set(scene.frame_start)
    bpy.context.view_layer.update()
    release = min(node["initialCondition"].get("releaseFrame", scene.frame_start) for node in document["nodes"] if node.get("rigidBody", {}).get("type") == "ACTIVE")
    dynamic = [created["actor"], *created["targets"], *created.get("secondary", [])]
    light_channels = sum(0 if not lamp.data.animation_data or not lamp.data.animation_data.action else len(lamp.data.animation_data.action.fcurves) for lamp in created["lights"])
    result = {
        "schemaVersion": {
            SPEC_VERSION: RESULT_VERSION,
            BREAKABLE_SPEC_VERSION: BREAKABLE_RESULT_VERSION,
            DERIVED_TARGET_SPEC_VERSION: DERIVED_TARGET_RESULT_VERSION,
        }[document["schemaVersion"]],
        "status": "PASS_EXECUTED",
        "contractVersion": {
            SPEC_VERSION: CONTRACT_VERSION,
            BREAKABLE_SPEC_VERSION: BREAKABLE_CONTRACT_VERSION,
            DERIVED_TARGET_SPEC_VERSION: DERIVED_TARGET_CONTRACT_VERSION,
        }[document["schemaVersion"]],
        "sceneId": document["projectId"],
        "sceneSpecHash": document["physicsActionSpecHash"],
        "sceneSpecFileSha256": file_hash,
        "compiledGraph": graph,
        "compiledGraphHash": graph_hash,
        "topology": topology_name,
        "physics": physics,
        "cinematography": cinematography,
        "authority": {
            "finalPoseSource": "BLENDER_BULLET_EVALUATED_ACTION_GRAPH",
            "postReleaseTransformKeyframes": sum(_transform_keys_at_or_after(obj, release) for obj in dynamic),
            "authoredOutcomeFields": 0,
            "authoredContactResponsePeakOrFinalFrames": 0,
            "lightAnimationChannels": light_channels,
            "cameraFrameSource": "MEASURED_PHYSICS_BEATS",
            "networkCalls": 0,
            "arbitraryExecutableAuthority": 0,
        },
        "mechanism": {
            "activeRigidBodyCount": sum(obj.rigid_body is not None and obj.rigid_body.type == "ACTIVE" for obj in dynamic),
            "rigidBodyConstraintCount": sum(obj.rigid_body_constraint is not None for obj in scene.objects),
            "nodeCount": len(document["nodes"]),
            "relationCount": len(document["relations"]),
            "factoryIds": [node["factory"] for node in document["nodes"]],
        },
        "semanticRoster": {
            "actor": created["actor"].name,
            "targets": [obj.name for obj in created["targets"]],
            "lights": [obj.name for obj in created["lights"]],
        },
        "physicalLook": created.get("physicalLook"),
        "review": {
            "resolution": document["cinematography"]["reviewResolution"],
            "stillFrames": [cinematography[key]["frame"] for key in ("cause", "contact", "effect")],
            "contactClipFrameRangeInclusive": [max(scene.frame_start, physics["contactFrame"] - 12), min(scene.frame_end, physics["contactFrame"] + 35)],
        },
    }
    if document["schemaVersion"] in {BREAKABLE_SPEC_VERSION, DERIVED_TARGET_SPEC_VERSION}:
        result["authority"].update({
            "authoredBreakFrames": 0,
            "authoredDetachedPoses": 0,
            "authoredDetachmentVelocities": 0,
        })
        result["mechanism"].update({
            "breakableFixedConstraintCount": 1,
            "constraintBreakSource": "BLENDER_BULLET_IMPULSE_THRESHOLD",
        })
        if created.get("attachmentTargetDerivation") is not None:
            result["mechanism"]["attachmentTargetDerivation"] = created["attachmentTargetDerivation"]
        if created.get("contactGeometry") is not None:
            result["mechanism"]["contactGeometry"] = created["contactGeometry"]
        result["semanticRoster"]["secondary"] = [obj.name for obj in created.get("secondary", [])]
    if "derivedPhysicalArchetypes" in created:
        result["physicalArchetypes"] = created["derivedPhysicalArchetypes"]
    if topology_name == "HINGE_LIGHT":
        result["illumination"] = {"changeSource": "SIMULATED_OCCLUDER_GEOMETRY_ONLY", "primaryLightPowerWatts": created["primaryLight"].data.energy, "primaryLightColorLinearRgb": list(created["primaryLight"].data.color), "hingeStopDegrees": created["stopDegrees"]}
    result["resultHash"] = _sha256(_canonical(result).encode("utf-8"))
    scene["film_studio_physics_action_result"] = json.dumps(result, sort_keys=True, separators=(",", ":"))
    scene["film_studio_physics_action_result_hash"] = result["resultHash"]
    scene["film_studio_physics_action_spec_hash"] = document["physicsActionSpecHash"]
    return result
