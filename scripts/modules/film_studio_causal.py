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
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

import film_studio_contract


SPEC_VERSIONS = {"bfs.causalSceneSpec.v0.1", "bfs.causalSceneSpec.v0.2"}
CONTRACT_VERSION = "bfs.filmStudioCausalContract.v0.2"
ALLOWED_FACTORIES = {
    "GROOVED_SPHERE",
    "BEVELED_DOMINO_BLOCK",
    "SIMPLE_WALL",
    "AREA_LIGHT",
    "CAMERA_FROM_DIRECTION_CLASS",
}
ALLOWED_COLLISION_SHAPES = {"SPHERE", "BOX"}
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


def _validate(document):
    _exact_keys(document, {"$schema", "schemaVersion", "sceneId", "title", "timeline", "dynamicActor", "targetGroup", "studio", "shots", "acceptance", "forbidden", "sceneSpecHash"}, "/")
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
    _validate_rigid_body(actor["rigidBody"], "SPHERE")
    _exact_keys(actor["material"], {"baseColor", "roughness", "proceduralGrain", "channelCount"}, "/dynamicActor/material")
    _vector(actor["material"]["baseColor"], 3, 0.0, 1.0, "actor color")
    if actor["material"]["proceduralGrain"] is not True or actor["material"]["channelCount"] != 3:
        raise CausalContractError("SPEC_SCHEMA", "actor material")

    targets = document["targetGroup"]
    target_keys = {"semanticRole", "factory", "count", "dimensions", "initialPositions", "rigidBody", "modeling", "palette"}
    if version == "bfs.causalSceneSpec.v0.2":
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
    if version == "bfs.causalSceneSpec.v0.2":
        variation = targets["deterministicVariation"]
        _exact_keys(variation, {"seed", "positionJitterMetersMaximum", "yawJitterDegreesMaximum", "frictionJitterMaximum", "restitutionJitterMaximum"}, "/targetGroup/deterministicVariation")
        if not isinstance(variation["seed"], int) or isinstance(variation["seed"], bool) or not 0 <= variation["seed"] <= 2147483647:
            raise CausalContractError("SPEC_SCHEMA", "variation seed")
        _number(variation["positionJitterMetersMaximum"], 0.0, 0.1, "position jitter")
        _number(variation["yawJitterDegreesMaximum"], 0.0, 10.0, "yaw jitter")
        _number(variation["frictionJitterMaximum"], 0.0, 0.2, "friction jitter")
        _number(variation["restitutionJitterMaximum"], 0.0, 0.2, "restitution jitter")
    _validate_rigid_body(targets["rigidBody"], "BOX")
    _exact_keys(targets["modeling"], {"bevelWidth", "bevelSegments", "insetFacePanel", "edgeBand"}, "/targetGroup/modeling")
    if targets["modeling"]["insetFacePanel"] is not True or targets["modeling"]["edgeBand"] is not True:
        raise CausalContractError("SPEC_SCHEMA", "target modeling")

    studio = document["studio"]
    _exact_keys(studio, {"ground", "backdrop", "lights"}, "/studio")
    if studio["backdrop"].get("factory") != "SIMPLE_WALL" or studio["backdrop"].get("participatesInPhysics") is not False:
        raise CausalContractError("UNSUPPORTED_FACTORY", "backdrop")
    if len(studio["lights"]) != 3 or any(light.get("kind") != "AREA" for light in studio["lights"]):
        raise CausalContractError("UNSUPPORTED_FACTORY", "lights")
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
    }[version]
    if [shot["selection"] for shot in shots] != expected_selections:
        raise CausalContractError("SPEC_SCHEMA", "shot selection")
    acceptance = document["acceptance"]
    acceptance_keys = {"initialPenetrationMaximumMeters", "actorForwardTravelBeforeFirstResponseMinimumMeters", "firstTargetResponseFrameWindowInclusive", "targetTiltDegreesAtFinalMinimumEach", "finiteTransformRequired", "reopenResponseFramesExact", "reopenFinalTiltToleranceDegrees", "postReleaseActorPoseKeyframes", "targetPoseKeyframes", "reviewOccupancyRange", "reviewNegativeSpaceMarginMinimum"}
    if version == "bfs.causalSceneSpec.v0.2":
        acceptance_keys.update({"impactActiveTargetCountMinimum", "impactFrameAfterFirstResponseMinimum", "settleAngularStepDegreesMaximum", "settleConsecutiveFrames", "deterministicVariationRequired", "impactClipFrameCount"})
    _exact_keys(acceptance, acceptance_keys, "/acceptance")
    if acceptance["postReleaseActorPoseKeyframes"] != 0 or acceptance["targetPoseKeyframes"] != 0:
        raise CausalContractError("FINAL_POSE_AUTHORITY", "Final pose keyframes are forbidden")
    if version == "bfs.causalSceneSpec.v0.2":
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
    forbidden = document["forbidden"]
    _exact_keys(forbidden, {"acceptedBottleFactory", "acceptedBottleFinalCoordinates", "projectSpecificCameraCoordinates", "externalModelsOrTextures", "manualTargetOrFinalPoseAnimation"}, "/forbidden")
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
    body.collision_margin = 0.002
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
    for index, rotation in enumerate(((0, 0, 0), (math.pi / 2, 0, 0), (0, math.pi / 2, 0)), 1):
        bpy.ops.mesh.primitive_torus_add(major_radius=radius + 0.002, minor_radius=0.009, major_segments=96, minor_segments=10, location=location, rotation=rotation)
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


def _create_targets(document, dark):
    spec = document["targetGroup"]
    dimensions = tuple(spec["dimensions"])
    targets, details, initial_conditions = [], [], []
    for index, (position, color) in enumerate(zip(spec["initialPositions"], spec["palette"]), 1):
        variation = spec.get("deterministicVariation")
        derived = {"positionX": float(position[0]), "positionY": float(position[1]), "yawDegrees": 0.0, "friction": float(spec["rigidBody"]["friction"]), "restitution": float(spec["rigidBody"]["restitution"])}
        if variation:
            def sample(channel):
                token = f"{document['sceneSpecHash']}:{variation['seed']}:{index}:{channel}".encode("utf-8")
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
    if document["schemaVersion"] == "bfs.causalSceneSpec.v0.2" and first is not None and all(frame is not None for frame in response.values()):
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
    scene.gravity = (0, 0, -9.81)
    if scene.world is None:
        scene.world = bpy.data.worlds.new("FILM_STUDIO_CAUSAL_WORLD")
    scene.world.color = (0.012, 0.015, 0.025)
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
    if document["schemaVersion"] == "bfs.causalSceneSpec.v0.2" and physics["motionSelection"]:
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
    scene.camera = cameras["SETUP"]
    scene.frame_set(scene.frame_start)
    result = {
        **inspection,
        "status": "PHYSICS_READY",
        "contractVersion": CONTRACT_VERSION,
        "physics": physics,
        "initialConditions": {
            "source": "SHA256_SCENE_HASH_SEED_TARGET_INDEX_CHANNEL" if document["schemaVersion"] == "bfs.causalSceneSpec.v0.2" else "DECLARED_EXACT",
            "targets": initial_conditions,
        },
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
    scene["film_studio_causal_result"] = json.dumps(result, sort_keys=True, separators=(",", ":"))
    scene["film_studio_causal_scene_spec_hash"] = document["sceneSpecHash"]
    return result
