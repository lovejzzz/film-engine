# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Restricted hybrid animation/Bullet physical-performance contract.

The open scene supplies authored intention.  A declarative spec binds semantic
roles; it cannot carry code, commands, network access, arbitrary paths, contact
frames, shutter values, or final poses.  The executor derives a kinematic hand
collider and a spring mechanism from evaluated scene motion.  Bullet owns the
mechanism response and every post-contact mechanism pose.
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
from mathutils import Matrix, Vector

import film_studio_contract

SPEC_VERSION = "bfs.physicalPerformanceSpec.v0.2"
CONTRACT_VERSION = "bfs.filmStudioPhysicalPerformance.v0.1"
GENERATED_TAG = "bfs.physicalPerformance.v0.1"
GENERATED_NAMES = {
    "collection": "FILM_STUDIO_PHYSICAL_PERFORMANCE",
    "housing": "PHYSICAL_PERFORMANCE_HOUSING",
    "base": "PHYSICAL_PERFORMANCE_SPRING_BASE",
    "stop": "PHYSICAL_PERFORMANCE_TRAVEL_STOP",
    "plunger": "PHYSICAL_PERFORMANCE_PLUNGER",
    "constraint": "PHYSICAL_PERFORMANCE_SPRING_CONSTRAINT",
    "collider": "PHYSICAL_PERFORMANCE_HAND_COLLIDER",
}

class PhysicalPerformanceError(RuntimeError):
    """A fail-closed physical-performance rejection with a stable reason."""

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
    raise PhysicalPerformanceError("NONFINITE_NUMBER", f"Non-finite JSON token {token} is forbidden")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except PhysicalPerformanceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PhysicalPerformanceError("INVALID_JSON", str(error)) from error


def _below_existing(root, uri):
    normalized = str(uri).replace("\\", "/")
    if not normalized or normalized.startswith("/") or "://" in normalized or ".." in normalized.split("/"):
        raise PhysicalPerformanceError("PATH_ESCAPE", "PerformanceSpec escapes the repository root")
    candidate = root / normalized
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise PhysicalPerformanceError("MISSING_INPUT", normalized) from error
    if root not in resolved.parents or Path(os.path.abspath(candidate)) != resolved:
        raise PhysicalPerformanceError("PATH_ESCAPE", "PerformanceSpec traverses a link or escapes the root")
    return resolved


def _exact_keys(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        reason = "UNKNOWN_TOP_LEVEL_FIELD" if path == "/" else "SPEC_SCHEMA"
        raise PhysicalPerformanceError(reason, path)


def _number(value, minimum, maximum, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PhysicalPerformanceError("NONFINITE_NUMBER", label)
    if not minimum <= value <= maximum:
        raise PhysicalPerformanceError("SPEC_SCHEMA", label)


def _role_bindings(rows, minimum, label):
    if not isinstance(rows, list) or len(rows) < minimum:
        raise PhysicalPerformanceError("SPEC_SCHEMA", label)
    roles, objects = [], []
    for row in rows:
        _exact_keys(row, {"role", "object"}, f"/{label}")
        if not all(isinstance(row[key], str) and 0 < len(row[key]) <= 255 for key in ("role", "object")):
            raise PhysicalPerformanceError("SPEC_SCHEMA", label)
        roles.append(row["role"])
        objects.append(row["object"])
    if len(set(roles)) != len(roles) or len(set(objects)) != len(objects):
        raise PhysicalPerformanceError("SPEC_SCHEMA", f"duplicate {label}")
    return roles


def _validate(document):
    _exact_keys(document, {"$schema", "schemaVersion", "performanceId", "sourceScene", "semanticBindings", "intentionalMotion", "physicalMechanism", "cinematography", "forbidden", "performanceSpecHash"}, "/")
    if document["$schema"] != SPEC_VERSION or document["schemaVersion"] != SPEC_VERSION:
        raise PhysicalPerformanceError("UNSUPPORTED_SCHEMA", str(document.get("schemaVersion")))
    if not isinstance(document["performanceId"], str) or not document["performanceId"]:
        raise PhysicalPerformanceError("SPEC_SCHEMA", "performanceId")
    if document["performanceSpecHash"] != _self_hash(document, "performanceSpecHash"):
        raise PhysicalPerformanceError("SELF_HASH_MISMATCH", "performanceSpecHash")

    source = document["sourceScene"]
    _exact_keys(source, {"sha256", "frameStart", "frameEnd", "fps"}, "/sourceScene")
    if not isinstance(source["sha256"], str) or len(source["sha256"]) != 64:
        raise PhysicalPerformanceError("SPEC_SCHEMA", "source sha256")
    for key in ("frameStart", "frameEnd", "fps"):
        _number(source[key], 1, 100000, key)
    if source["frameEnd"] <= source["frameStart"]:
        raise PhysicalPerformanceError("SPEC_SCHEMA", "frame range")

    bindings = document["semanticBindings"]
    scalar_bindings = {"intentionalActorArmature", "intentionalContactEffector", "visibleHandBody", "supportSurface", "existingControlCandidate", "faceplate", "eyeLine", "wideCamera", "mediumCamera", "closeCamera"}
    _exact_keys(bindings, scalar_bindings | {"environmentLayers", "facialLandmarks"}, "/semanticBindings")
    if not all(isinstance(bindings[key], str) and bindings[key] for key in scalar_bindings):
        raise PhysicalPerformanceError("SPEC_SCHEMA", "semantic binding")
    _role_bindings(bindings["environmentLayers"], 3, "environmentLayers")
    landmark_roles = _role_bindings(bindings["facialLandmarks"], 4, "facialLandmarks")

    motion = document["intentionalMotion"]
    _exact_keys(motion, {"source", "action", "contactSearchWindowInclusive", "allowedAuthoredChannels", "authoredMechanismPoseChannels"}, "/intentionalMotion")
    if motion["source"] != "EXISTING_ARMATURE_ACTION" or motion["authoredMechanismPoseChannels"] != 0:
        raise PhysicalPerformanceError("POSE_AUTHORITY", "mechanism pose authority is forbidden")
    window = motion["contactSearchWindowInclusive"]
    if not isinstance(window, list) or len(window) != 2 or not all(isinstance(value, int) for value in window):
        raise PhysicalPerformanceError("SPEC_SCHEMA", "contact search window")
    if not source["frameStart"] <= window[0] < window[1] <= source["frameEnd"]:
        raise PhysicalPerformanceError("SPEC_SCHEMA", "contact search range")
    if not isinstance(motion["allowedAuthoredChannels"], list) or not motion["allowedAuthoredChannels"]:
        raise PhysicalPerformanceError("SPEC_SCHEMA", "allowed authored channels")

    mechanism = document["physicalMechanism"]
    _exact_keys(mechanism, {"factory", "anchorStrategy", "contactColliderStrategy", "plungerCollisionShape", "constraintType", "travelAxis", "travelMeters", "movingMassKg", "stiffnessNewtonPerMeter", "dampingNewtonSecondPerMeter", "collisionMarginMeters", "substepsPerFrameMinimum", "solverIterationsMinimum", "postContactPoseSource"}, "/physicalMechanism")
    exact = {
        "factory": "SPRING_PLUNGER_FROM_EVALUATED_CONTACT_TRAJECTORY",
        "anchorStrategy": "PROJECT_CLOSEST_EFFECTOR_SAMPLE_ONTO_SUPPORT_SURFACE",
        "contactColliderStrategy": "KINEMATIC_CAPSULE_FROM_EVALUATED_HAND_BODY",
        "plungerCollisionShape": "CONVEX_HULL",
        "constraintType": "GENERIC_SPRING",
        "travelAxis": "SUPPORT_SURFACE_LOCAL_NORMAL",
        "postContactPoseSource": "BLENDER_BULLET_RIGID_BODY_AND_CONSTRAINT",
    }
    if any(mechanism[key] != value for key, value in exact.items()):
        raise PhysicalPerformanceError("UNSUPPORTED_FACTORY", "physical mechanism")
    for key, low, high in (("travelMeters", 0.005, 0.2), ("movingMassKg", 0.01, 20.0), ("stiffnessNewtonPerMeter", 1.0, 10000.0), ("dampingNewtonSecondPerMeter", 0.01, 1000.0), ("collisionMarginMeters", 0.0001, 0.01), ("substepsPerFrameMinimum", 1, 100), ("solverIterationsMinimum", 1, 1000)):
        _number(mechanism[key], low, high, key)

    cinema = document["cinematography"]
    _exact_keys(cinema, {"framingSource", "motionBlurStrategy", "motionBlurPosition", "mediumOccupancyRange", "wideEnvironmentLayersMinimum", "closeFacialLandmarks", "closeFacialLandmarksVisibleMinimum"}, "/cinematography")
    if cinema["framingSource"] != "EVALUATED_FRAME_SEMANTIC_WORLD_BOUNDS_AND_LANDMARKS" or cinema["motionBlurStrategy"] != "MEASURED_PROJECTED_MEDIAN_MOTION" or cinema["motionBlurPosition"] != "CENTER":
        raise PhysicalPerformanceError("SPEC_SCHEMA", "cinematography strategy")
    occupancy = cinema["mediumOccupancyRange"]
    if not isinstance(occupancy, list) or len(occupancy) != 2:
        raise PhysicalPerformanceError("SPEC_SCHEMA", "occupancy")
    _number(occupancy[0], 0.1, 0.95, "occupancy lower")
    _number(occupancy[1], occupancy[0], 0.98, "occupancy upper")
    if cinema["wideEnvironmentLayersMinimum"] < 3 or cinema["closeFacialLandmarksVisibleMinimum"] < 4:
        raise PhysicalPerformanceError("SPEC_SCHEMA", "visual floors")
    if not isinstance(cinema["closeFacialLandmarks"], list) or not set(cinema["closeFacialLandmarks"]).issubset(set(landmark_roles)):
        raise PhysicalPerformanceError("SPEC_SCHEMA", "facial landmark roles")

    forbidden = document["forbidden"]
    forbidden_keys = {"projectSpecificBranchInProductCode", "fixtureHashBranchInProductCode", "authoredPlungerOrMechanismFinalPose", "postContactMechanismPoseKeyframes", "manualContactFrame", "manualShutter", "postprocessMotionBlur", "environmentLayerDeletionToFixOcclusion", "arbitraryPythonShellNetworkOrFilesystemAuthority"}
    _exact_keys(forbidden, forbidden_keys, "/forbidden")
    if any(forbidden[key] is not True for key in forbidden_keys):
        raise PhysicalPerformanceError("AUTHORITY_EXPANSION", "all forbidden controls must remain true")
    return document


def matches_physical_performance(repository_root, spec_uri):
    root = Path(repository_root).resolve(strict=True)
    document = _read_json(_below_existing(root, spec_uri))
    return document.get("schemaVersion") == SPEC_VERSION


def _object(scene, name, expected_type=None):
    obj = scene.objects.get(name)
    if obj is None or (expected_type and obj.type != expected_type):
        raise PhysicalPerformanceError("SEMANTIC_BINDING", f"{name}: {expected_type or 'OBJECT'}")
    return obj


def _resolve_scene(document, scene):
    bindings = document["semanticBindings"]
    resolved = {
        "armature": _object(scene, bindings["intentionalActorArmature"], "ARMATURE"),
        "effector": _object(scene, bindings["intentionalContactEffector"]),
        "hand": _object(scene, bindings["visibleHandBody"], "MESH"),
        "support": _object(scene, bindings["supportSurface"], "MESH"),
        "candidate": _object(scene, bindings["existingControlCandidate"], "MESH"),
        "faceplate": _object(scene, bindings["faceplate"], "MESH"),
        "eyeLine": _object(scene, bindings["eyeLine"], "MESH"),
        "wideCamera": _object(scene, bindings["wideCamera"], "CAMERA"),
        "mediumCamera": _object(scene, bindings["mediumCamera"], "CAMERA"),
        "closeCamera": _object(scene, bindings["closeCamera"], "CAMERA"),
        "environment": [_object(scene, row["object"], "MESH") for row in bindings["environmentLayers"]],
        "landmarks": [_object(scene, row["object"], "MESH") for row in bindings["facialLandmarks"]],
    }
    action = bpy.data.actions.get(document["intentionalMotion"]["action"])
    if action is None:
        raise PhysicalPerformanceError("SEMANTIC_BINDING", "intentional action")
    assigned = resolved["armature"].animation_data.action if resolved["armature"].animation_data else None
    if assigned != action:
        raise PhysicalPerformanceError("SEMANTIC_BINDING", "armature action assignment")
    return resolved


def _surface_projection(support, world_point):
    local = support.matrix_world.inverted() @ world_point
    minimum = Vector(tuple(min(corner[index] for corner in support.bound_box) for index in range(3)))
    maximum = Vector(tuple(max(corner[index] for corner in support.bound_box) for index in range(3)))
    projected = Vector(tuple(max(minimum[index], min(maximum[index], local[index])) for index in range(3)))
    outside = [local[index] < minimum[index] or local[index] > maximum[index] for index in range(3)]
    if any(outside):
        _, axis, sign = max((abs(local[index] - projected[index]), index, -1 if local[index] < minimum[index] else 1) for index in range(3) if outside[index])
    else:
        _, axis, sign = min([(abs(local[index] - minimum[index]), index, -1) for index in range(3)] + [(abs(maximum[index] - local[index]), index, 1) for index in range(3)])
        projected[axis] = minimum[axis] if sign < 0 else maximum[axis]
    normal_local = Vector(tuple(sign if index == axis else 0.0 for index in range(3)))
    world_surface = support.matrix_world @ projected
    world_normal = (support.matrix_world.to_3x3() @ normal_local).normalized()
    return world_surface, world_normal


def _derive_contact(scene, resolved, document):
    start, end = document["intentionalMotion"]["contactSearchWindowInclusive"]
    original = scene.frame_current
    samples = []
    for frame in range(start - 1, end + 2):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        effector = resolved["effector"].matrix_world.translation.copy()
        surface, normal = _surface_projection(resolved["support"], effector)
        samples.append({"frame": frame, "effector": effector, "surface": surface, "normal": normal, "distance": (effector - surface).length})
    candidates = []
    for index in range(1, len(samples) - 1):
        sample = samples[index]
        if not start <= sample["frame"] <= end:
            continue
        velocity = (samples[index + 1]["effector"] - samples[index - 1]["effector"]) / 2.0
        approach = velocity.dot(sample["normal"])
        if approach < -1e-7:
            candidates.append((sample["distance"], sample["frame"], approach, sample))
    scene.frame_set(original)
    bpy.context.view_layer.update()
    if not candidates:
        raise PhysicalPerformanceError("CONTACT_DERIVATION", "no evaluated approaching sample")
    _, _, approach, sample = min(candidates, key=lambda row: (row[0], row[1]))
    return {
        "source": "EVALUATED_CLOSEST_APPROACH_TO_SUPPORT_OBB_WITH_INWARD_VELOCITY",
        "anchorFrame": sample["frame"],
        "distanceMeters": round(sample["distance"], 8),
        "approachMetersPerFrame": round(-approach, 8),
        "anchor": [round(value, 8) for value in sample["surface"]],
        "normal": [round(value, 8) for value in sample["normal"]],
    }


def _source_hash(document):
    filepath = Path(bpy.data.filepath)
    if not filepath.is_file() or bpy.data.is_dirty:
        raise PhysicalPerformanceError("SOURCE_SCENE", "open scene must be a clean saved file")
    observed = _sha256(filepath.read_bytes())
    if observed != document["sourceScene"]["sha256"]:
        raise PhysicalPerformanceError("SOURCE_SCENE", observed)
    return observed


def _inspection(repository_root, spec_uri, scene):
    root = Path(repository_root).resolve(strict=True)
    path = _below_existing(root, spec_uri)
    document = _validate(_read_json(path))
    source_hash = _source_hash(document)
    resolved = _resolve_scene(document, scene)
    contact = _derive_contact(scene, resolved, document)
    file_hash = _sha256(path.read_bytes())
    token_body = {"contractVersion": CONTRACT_VERSION, "fileSha256": file_hash, "performanceSpecHash": document["performanceSpecHash"], "sourceSceneSha256": source_hash, "contact": contact}
    return document, resolved, contact, file_hash, _sha256(_canonical(token_body).encode("utf-8"))


def inspect_physical_performance(repository_root, spec_uri, scene=None):
    scene = scene or bpy.context.scene
    document, resolved, contact, file_hash, token = _inspection(repository_root, spec_uri, scene)
    return {
        "status": "APPROVED_READY",
        "sceneId": document["performanceId"],
        "actorFactory": document["intentionalMotion"]["source"],
        "targetFactory": document["physicalMechanism"]["factory"],
        "targetCount": 1,
        "collisionShapes": ["CAPSULE", document["physicalMechanism"]["plungerCollisionShape"]],
        "finalPoseSource": document["physicalMechanism"]["postContactPoseSource"],
        "cameraFitSource": document["cinematography"]["framingSource"],
        "sceneSpecHash": document["performanceSpecHash"],
        "fileSha256": file_hash,
        "contact": contact,
        "semanticObjectCount": 10 + len(resolved["environment"]) + len(resolved["landmarks"]),
        "inspectionToken": token,
    }


def _material(name, color, roughness, metallic=0.0, emission=None):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = (*color, 1.0)
    principled.inputs["Roughness"].default_value = roughness
    principled.inputs["Metallic"].default_value = metallic
    if emission:
        principled.inputs["Emission Color"].default_value = (*emission, 1.0)
        principled.inputs["Emission Strength"].default_value = 3.0
    return material


def _tag(obj, role):
    obj["film_studio_physical_performance"] = GENERATED_TAG
    obj["film_studio_semantic_role"] = role
    return obj


def _select(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _rigid_body(obj, kind, shape, mass, margin):
    _select(obj)
    bpy.ops.rigidbody.object_add()
    body = obj.rigid_body
    body.type = kind
    body.collision_shape = shape
    body.mass = mass
    body.friction = 0.58
    body.restitution = 0.02
    body.linear_damping = 0.08
    body.angular_damping = 0.35
    body.use_margin = True
    body.collision_margin = margin
    obj.select_set(False)
    return body


def _align_axis(vector, track="Z", up="Y"):
    return Vector(vector).normalized().to_track_quat(track, up).to_euler()


def _create_mechanism(scene, resolved, contact, document):
    if any(obj.get("film_studio_physical_performance") == GENERATED_TAG for obj in scene.objects):
        raise PhysicalPerformanceError("DUPLICATE_EXECUTION", "physical performance already exists")
    mechanism = document["physicalMechanism"]
    anchor, normal = Vector(contact["anchor"]), Vector(contact["normal"])
    travel = float(mechanism["travelMeters"])
    margin = float(mechanism["collisionMarginMeters"])
    collection = bpy.data.collections.new(GENERATED_NAMES["collection"])
    scene.collection.children.link(collection)

    dark = _material("MAT_PhysicalPerformanceHousing", (0.012, 0.018, 0.024), 0.22, 0.72)
    active = _material("MAT_PhysicalPerformanceControl", (0.025, 0.28, 0.32), 0.18, 0.34, (0.02, 0.55, 0.68))
    cylinder_rotation = _align_axis(normal)
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=0.075, depth=0.024, location=anchor + normal * 0.012, rotation=cylinder_rotation)
    housing = _tag(bpy.context.object, "spring_control_housing")
    housing.name = GENERATED_NAMES["housing"]
    housing.data.materials.append(dark)

    plunger_depth = min(0.034, travel * 0.76)
    rest = anchor + normal * (0.024 + plunger_depth * 0.5)
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=0.052, depth=plunger_depth, location=rest, rotation=cylinder_rotation)
    plunger = _tag(bpy.context.object, "solver_owned_spring_plunger")
    plunger.name = GENERATED_NAMES["plunger"]
    plunger.data.materials.append(active)
    for polygon in plunger.data.polygons:
        polygon.use_smooth = True
    _rigid_body(plunger, "ACTIVE", mechanism["plungerCollisionShape"], mechanism["movingMassKg"], margin)
    plunger["film_studio_pose_source"] = mechanism["postContactPoseSource"]
    plunger["film_studio_rest_location"] = list(rest)
    plunger["film_studio_travel_axis"] = list(normal)

    bpy.ops.mesh.primitive_cube_add(size=0.012, location=rest, rotation=_align_axis(normal, "X", "Z"))
    base = _tag(bpy.context.object, "passive_spring_anchor")
    base.name = GENERATED_NAMES["base"]
    base.hide_render = True
    base.display_type = "WIRE"
    _rigid_body(base, "PASSIVE", "BOX", 1.0, margin)

    stop_depth = 0.012
    stop_center = rest - normal * (travel + plunger_depth * 0.5 + stop_depth * 0.5)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=stop_center, rotation=cylinder_rotation, scale=(0.068, 0.068, stop_depth * 0.5))
    stop = _tag(bpy.context.object, "passive_travel_stop")
    stop.name = GENERATED_NAMES["stop"]
    _select(stop)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    stop.hide_render = True
    stop.display_type = "WIRE"
    _rigid_body(stop, "PASSIVE", "BOX", 1.0, margin)

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=rest, rotation=_align_axis(normal, "X", "Z"))
    constraint_object = _tag(bpy.context.object, "generic_spring_constraint")
    constraint_object.name = GENERATED_NAMES["constraint"]
    _select(constraint_object)
    bpy.ops.rigidbody.constraint_add()
    constraint = constraint_object.rigid_body_constraint
    constraint.type = "GENERIC_SPRING"
    constraint.object1 = base
    constraint.object2 = plunger
    constraint.disable_collisions = True
    constraint.use_limit_lin_x = True
    constraint.limit_lin_x_lower = -travel
    constraint.limit_lin_x_upper = 0.001
    constraint.use_limit_lin_y = True
    constraint.limit_lin_y_lower = constraint.limit_lin_y_upper = 0.0
    constraint.use_limit_lin_z = True
    constraint.limit_lin_z_lower = constraint.limit_lin_z_upper = 0.0
    for axis in "xyz":
        setattr(constraint, f"use_limit_ang_{axis}", True)
        setattr(constraint, f"limit_ang_{axis}_lower", 0.0)
        setattr(constraint, f"limit_ang_{axis}_upper", 0.0)
    constraint.use_spring_x = True
    constraint.spring_stiffness_x = mechanism["stiffnessNewtonPerMeter"]
    constraint.spring_damping_x = mechanism["dampingNewtonSecondPerMeter"]
    constraint.use_override_solver_iterations = True
    constraint.solver_iterations = max(120, mechanism["solverIterationsMinimum"])

    hand_dims = resolved["hand"].dimensions
    collider_radius = max(0.015, min(0.03, min(hand_dims) / 6.0))
    collider_depth = max(collider_radius * 2.2, min(0.14, max(hand_dims) * 0.7))
    scene.frame_set(contact["anchorFrame"])
    bpy.context.view_layer.update()
    tangent_frame = min(document["sourceScene"]["frameEnd"], contact["anchorFrame"] + 1)
    p0 = resolved["effector"].matrix_world.translation.copy()
    scene.frame_set(tangent_frame)
    bpy.context.view_layer.update()
    tangent = resolved["effector"].matrix_world.translation - p0
    if tangent.length < 1e-8:
        tangent = normal
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=1.0, location=p0, rotation=_align_axis(tangent))
    collider = _tag(bpy.context.object, "kinematic_contact_collider")
    collider.name = GENERATED_NAMES["collider"]
    collider.scale = (collider_radius, collider_radius, collider_depth * 0.5)
    _select(collider)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    collider.hide_render = True
    collider.display_type = "WIRE"
    body = _rigid_body(collider, "ACTIVE", "CAPSULE", 1.0, margin)
    body.kinematic = True
    collider["film_studio_motion_source"] = "EVALUATED_INTENTIONAL_CONTACT_EFFECTOR"
    collider["film_studio_capsule_radius"] = collider_radius
    collider["film_studio_capsule_depth"] = collider_depth

    for frame in range(document["sourceScene"]["frameStart"], document["sourceScene"]["frameEnd"] + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        collider.location = resolved["effector"].matrix_world.translation
        collider.keyframe_insert(data_path="location", frame=frame, group="EVALUATED_EFFECTOR_TRAJECTORY")
    if collider.animation_data and collider.animation_data.action:
        action = collider.animation_data.action
        curves = list(action.fcurves) if hasattr(action, "fcurves") else [curve for layer in action.layers for strip in layer.strips for bag in strip.channelbags for curve in bag.fcurves]
        for curve in curves:
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"

    world = scene.rigidbody_world
    if world is None:
        raise PhysicalPerformanceError("PHYSICS_WORLD", "rigid body world was not created")
    world.substeps_per_frame = max(world.substeps_per_frame, mechanism["substepsPerFrameMinimum"], 40)
    world.solver_iterations = max(world.solver_iterations, mechanism["solverIterationsMinimum"], 120)
    world.point_cache.frame_start = document["sourceScene"]["frameStart"]
    world.point_cache.frame_end = document["sourceScene"]["frameEnd"]
    return {"housing": housing, "base": base, "stop": stop, "plunger": plunger, "constraint": constraint_object, "collider": collider, "rest": rest, "normal": normal, "colliderRadius": collider_radius, "plungerRadius": 0.052}


def _mechanism_pose_keyframes(plunger):
    if not plunger.animation_data or not plunger.animation_data.action:
        return 0
    curves = list(plunger.animation_data.action.fcurves) if hasattr(plunger.animation_data.action, "fcurves") else []
    return sum(len(curve.keyframe_points) for curve in curves if curve.data_path in {"location", "rotation_euler", "rotation_quaternion", "scale"})


def _simulate(scene, created, contact, document):
    start, end = document["sourceScene"]["frameStart"], document["sourceScene"]["frameEnd"]
    rest, normal, plunger = created["rest"], created["normal"], created["plunger"]
    rows = []
    previous_position = None
    for frame in range(start, end + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        position = plunger.matrix_world.translation.copy()
        axis_coordinate = -(position - rest).dot(normal)
        speed = 0.0 if previous_position is None else (position - previous_position).length * document["sourceScene"]["fps"]
        rows.append({"frame": frame, "axisCoordinateMeters": axis_coordinate, "speedMetersPerSecond": speed})
        previous_position = position
    equilibrium_end = contact["anchorFrame"] - 16
    equilibrium_start = equilibrium_end - 7
    equilibrium_window = [row for row in rows if equilibrium_start <= row["frame"] <= equilibrium_end]
    if len(equilibrium_window) != 8:
        raise PhysicalPerformanceError("PHYSICS_MEASUREMENT", "precontact equilibrium window")
    equilibrium = statistics.median(row["axisCoordinateMeters"] for row in equilibrium_window)
    for row in rows:
        row["signedRelativeDisplacementMeters"] = row["axisCoordinateMeters"] - equilibrium
        row["displacementMeters"] = abs(row["signedRelativeDisplacementMeters"])
    response = next((row["frame"] for row in rows if row["frame"] > equilibrium_end and row["displacementMeters"] >= 0.0005), None)
    if response is None or response > contact["anchorFrame"]:
        raise PhysicalPerformanceError("PHYSICS_RESPONSE", "no finite-size contact onset before closest approach")
    compression_rows = [row for row in rows if row["frame"] >= response]
    peak = max(compression_rows, key=lambda row: (row["signedRelativeDisplacementMeters"], -row["frame"]))
    after_peak = [row for row in rows if row["frame"] > peak["frame"]]
    reversal = next((row["frame"] for row in after_peak if row["signedRelativeDisplacementMeters"] <= peak["signedRelativeDisplacementMeters"] - 0.0005), None)
    settle_count = 8
    settled = None
    for index, row in enumerate(rows):
        window = rows[index:index + settle_count]
        if reversal is not None and row["frame"] >= reversal and len(window) == settle_count and all(sample["displacementMeters"] <= 0.002 for sample in window):
            settled = row["frame"]
            break
    maximum_speed = max(row["speedMetersPerSecond"] for row in rows)
    mass = document["physicalMechanism"]["movingMassKg"]
    stiffness = document["physicalMechanism"]["stiffnessNewtonPerMeter"]
    impulse = mass * maximum_speed
    kinetic = 0.5 * mass * maximum_speed * maximum_speed
    spring = 0.5 * stiffness * peak["displacementMeters"] * peak["displacementMeters"]
    scene.frame_set(end)
    bpy.context.view_layer.update()
    return {
        "source": "BLENDER_BULLET_EVALUATED_WORLD_TRANSFORMS",
        "anchorFrame": contact["anchorFrame"],
        "contactFrame": response,
        "firstResponseFrame": response,
        "firstResponseDelayFrames": 0,
        "precontactEquilibriumFrameRangeInclusive": [equilibrium_start, equilibrium_end],
        "precontactEquilibriumAxisCoordinateMeters": round(equilibrium, 8),
        "peakFrame": peak["frame"],
        "peakDisplacementMeters": round(peak["signedRelativeDisplacementMeters"], 8),
        "directionReversalFrame": reversal,
        "settledWindowStartFrame": settled,
        "settledResidualMaximumMeters": None if settled is None else round(max(row["displacementMeters"] for row in rows[settled - start:settled - start + settle_count]), 8),
        "evaluatedMomentumTransferKgMPerSecond": round(impulse, 8),
        "peakKineticEnergyJoule": round(kinetic, 8),
        "peakSpringEnergyJoule": round(spring, 8),
        "constraint": {"type": "GENERIC_SPRING", "linearAxis": "X", "travelMeters": document["physicalMechanism"]["travelMeters"], "stiffnessNewtonPerMeter": stiffness, "dampingNewtonSecondPerMeter": document["physicalMechanism"]["dampingNewtonSecondPerMeter"]},
        "samples": [{"frame": row["frame"], "axisCoordinateMeters": round(row["axisCoordinateMeters"], 8), "signedRelativeDisplacementMeters": round(row["signedRelativeDisplacementMeters"], 8), "displacementMeters": round(row["displacementMeters"], 8), "speedMetersPerSecond": round(row["speedMetersPerSecond"], 8)} for row in rows],
    }


def _world_points(objects):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        if obj.type != "MESH":
            continue
        evaluated = obj.evaluated_get(depsgraph)
        points.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
    if not points:
        raise PhysicalPerformanceError("CAMERA_FIT", "no semantic mesh bounds")
    return points


def _projected_bounds(scene, camera, objects):
    points = [world_to_camera_view(scene, camera, point) for point in _world_points(objects)]
    visible = [point for point in points if point.z > 0]
    if not visible:
        raise PhysicalPerformanceError("CAMERA_FIT", "no visible semantic bounds")
    return min(p.x for p in visible), max(p.x for p in visible), min(p.y for p in visible), max(p.y for p in visible)


def _point_at(camera, target):
    camera.rotation_euler = (Vector(target) - camera.location).to_track_quat("-Z", "Y").to_euler()


def _fit_camera(scene, camera, objects, desired, forward_override=None):
    points = _world_points(objects)
    minimum = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    maximum = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    center = (minimum + maximum) / 2.0
    forward = Vector(forward_override) if forward_override is not None else camera.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
    forward.normalize()
    distance = max(0.35, (maximum - minimum).length * 1.4)
    for _ in range(8):
        camera.location = center - forward * distance
        _point_at(camera, center)
        bpy.context.view_layer.update()
        x0, x1, y0, y1 = _projected_bounds(scene, camera, objects)
        observed = max(x1 - x0, y1 - y0)
        distance *= max(0.58, min(1.75, observed / desired))
    camera.location = center - forward * distance
    _point_at(camera, center)
    bpy.context.view_layer.update()
    x0, x1, y0, y1 = _projected_bounds(scene, camera, objects)
    return {"source": "EVALUATED_FRAME_SEMANTIC_WORLD_BOUNDS_AND_LANDMARKS", "occupancy": round(max(x1 - x0, y1 - y0), 8), "negativeSpaceMargin": round(min(x0, y0, 1 - x1, 1 - y1), 8), "cameraLocation": [round(value, 8) for value in camera.location]}


def _overlaps_frame(scene, camera, obj):
    x0, x1, y0, y1 = _projected_bounds(scene, camera, [obj])
    return x1 >= 0.0 and x0 <= 1.0 and y1 >= 0.0 and y0 <= 1.0


def _clear_line_of_sight(scene, camera, objects, desired, orbit_axis, allowed_objects=None):
    points = _world_points(objects)
    target = sum(points, Vector()) / len(points)
    direction = (target - camera.location).normalized()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    allowed = set(allowed_objects or objects)
    distance = (target - camera.location).length

    def visible_from(location):
        blockers = []
        for aim in [target] + [obj.matrix_world.translation.copy() for obj in objects]:
            ray = aim - location
            hit, _point, _normal, _index, obj, _matrix = scene.ray_cast(depsgraph, location, ray.normalized(), distance=ray.length + 0.05)
            if hit and obj not in allowed:
                blockers.append(obj.name)
        return not blockers, blockers

    clear, blockers = visible_from(camera.location)
    initial_blockers = sorted(set(blockers))
    chosen_factor = 0.0
    moved = 0.0
    if not clear:
        original = camera.location.copy()
        base_offset = -direction
        candidates = (-0.45, 0.45, -0.7, 0.7, -1.0, 1.0, -1.5, 1.5, -2.0, 2.0)
        for factor in candidates:
            offset = (base_offset + Vector(orbit_axis).normalized() * factor).normalized()
            location = target + offset * distance * max(1.0, abs(factor))
            candidate_clear, candidate_blockers = visible_from(location)
            if candidate_clear:
                camera.location = location
                chosen_factor = factor
                moved = (location - original).length
                break
        else:
            raise PhysicalPerformanceError("CAMERA_OCCLUSION", ",".join(sorted(set(blockers))))
        _point_at(camera, target)
        bpy.context.view_layer.update()
    for _ in range(4):
        x0, x1, y0, y1 = _projected_bounds(scene, camera, objects)
        observed = max(x1 - x0, y1 - y0)
        camera.data.lens = max(18.0, min(300.0, camera.data.lens * desired / observed))
        bpy.context.view_layer.update()
    camera.data.dof.focus_object = None; camera.data.dof.focus_distance = (target - camera.location).length; camera.data.dof.aperture_fstop = max(5.6, camera.data.dof.aperture_fstop)
    return {"source": "BOUNDED_ORBIT_MULTI_RAYCAST_TO_SEMANTIC_TARGET", "initialBlockers": initial_blockers, "orbitFactor": chosen_factor, "cameraMovedMeters": round(moved, 8), "finalLensMm": round(camera.data.lens, 8), "semanticFocusDistanceMeters": round(camera.data.dof.focus_distance, 8), "apertureFStop": round(camera.data.dof.aperture_fstop, 8)}


def _review_camera(scene, source, frame, role):
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    data = source.data.copy()
    camera = _tag(bpy.data.objects.new(f"PHYSICAL_PERFORMANCE_CAM_{role}", data), f"review_camera_{role.lower()}")
    scene.collection.objects.link(camera)
    camera.matrix_world = source.matrix_world.copy()
    return camera


def _face_forward(faceplate, seed_camera):
    extents = [max(corner[index] for corner in faceplate.bound_box) - min(corner[index] for corner in faceplate.bound_box) for index in range(3)]
    axis_index = min(range(3), key=lambda index: (extents[index], index))
    local_axis = Vector(tuple(1.0 if index == axis_index else 0.0 for index in range(3)))
    outward = (faceplate.matrix_world.to_3x3() @ local_axis).normalized()
    center = sum(_world_points([faceplate]), Vector()) / 8.0
    if (seed_camera.location - center).dot(outward) < 0.0:
        outward.negate()
    return -outward


def _organize_landmarks(document, resolved, seed_camera):
    faceplate = resolved["faceplate"]
    minimum = Vector(tuple(min(corner[index] for corner in faceplate.bound_box) for index in range(3)))
    maximum = Vector(tuple(max(corner[index] for corner in faceplate.bound_box) for index in range(3)))
    extents = maximum - minimum
    normal_axis = min(range(3), key=lambda index: (extents[index], index))
    width_axis = max(range(3), key=lambda index: (extents[index], -index))
    height_axis = next(index for index in range(3) if index not in {normal_axis, width_axis})
    center_local = (minimum + maximum) / 2.0
    center_world = faceplate.matrix_world @ center_local
    axes = [(faceplate.matrix_world.to_3x3() @ Vector(tuple(1.0 if child == index else 0.0 for child in range(3)))).normalized() for index in range(3)]
    width_world, height_world = axes[width_axis], axes[height_axis]
    outward = axes[normal_axis]
    if (seed_camera.location - center_world).dot(outward) < 0.0:
        outward.negate()
    if width_world.cross(outward).dot(height_world) < 0.0:
        width_world.negate()
    head_parts = [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and not obj.hide_render and obj.get("bfs_pc4_system") == "head"]
    head_points = _world_points(head_parts or [faceplate])
    central_points = [point for point in head_points if abs((point - center_world).dot(width_world)) <= extents[width_axis] * 0.56 and abs((point - center_world).dot(height_world)) <= extents[height_axis] * 0.56]
    front_depth = max(point.dot(outward) for point in (central_points or _world_points([faceplate])))
    plane_center = center_world + outward * (front_depth - center_world.dot(outward) + 0.012)
    zones = {
        "brow": (0.0, 0.24, 0.62, 0.055),
        "eye_line": (0.0, 0.06, 0.50, 0.045),
        "cheek_left": (-0.26, -0.10, 0.10, 0.18),
        "cheek_right": (0.26, -0.10, 0.10, 0.18),
        "jaw": (0.0, -0.32, 0.36, 0.05),
    }
    cyan = _material("MAT_PhysicalPerformanceFaceCyan", (0.01, 0.22, 0.28), 0.2, 0.35, (0.01, 0.28, 0.35))
    edge = _material("MAT_PhysicalPerformanceFaceEdge", (0.08, 0.14, 0.18), 0.24, 0.7, (0.01, 0.16, 0.22))
    rows = []
    role_objects = {row["role"]: obj for row, obj in zip(document["semanticBindings"]["facialLandmarks"], resolved["landmarks"])}
    for role, (horizontal, vertical, width, height) in zones.items():
        obj = role_objects[role]
        obj.parent = None
        transform = Matrix((width_world, outward, height_world)).transposed().to_4x4()
        transform.translation = plane_center + width_world * horizontal * extents[width_axis] + height_world * vertical * extents[height_axis]
        obj.matrix_world = transform
        bpy.context.view_layer.update()
        obj.dimensions = (width * extents[width_axis], 0.012, height * extents[height_axis])
        obj.data.materials.clear()
        obj.data.materials.append(cyan if role in {"eye_line", "cheek_left", "cheek_right"} else edge)
        obj["film_studio_landmark_layout_source"] = "FACEPLATE_NORMALIZED_SEMANTIC_ZONES"
        obj["film_studio_landmark_role"] = role
        rows.append({"role": role, "object": obj.name, "normalizedZone": [horizontal, vertical], "normalizedSize": [width, height]})
    bpy.context.view_layer.update()
    return {"source": "FRONTMOST_CENTRAL_HEAD_SURFACE_AND_DECLARATIVE_LANDMARK_ROLES", "normalAxis": normal_axis, "widthAxis": width_axis, "heightAxis": height_axis, "frontDepthMeters": round(front_depth, 8), "landmarks": rows}


def _configure_cinematography(scene, resolved, created, physics, document):
    contact_frame = physics["contactFrame"]
    impact_frame = physics["peakFrame"]
    close_frame = min(document["sourceScene"]["frameEnd"], max(contact_frame + 24, 240))
    wide_frame = max(document["sourceScene"]["frameStart"], min(48, document["sourceScene"]["frameEnd"]))
    cameras = {
        "wide": _review_camera(scene, resolved["wideCamera"], wide_frame, "WIDE"),
        "medium": _review_camera(scene, resolved["mediumCamera"], impact_frame, "MEDIUM"),
        "close": _review_camera(scene, resolved["closeCamera"], close_frame, "CLOSE"),
    }
    source_cameras = {"wide": resolved["wideCamera"], "medium": resolved["mediumCamera"], "close": resolved["closeCamera"]}
    marker_rebindings = []
    for marker in scene.timeline_markers:
        for role, source in source_cameras.items():
            if marker.camera == source:
                marker.camera = cameras[role]
                marker_rebindings.append({"marker": marker.name, "frame": marker.frame, "role": role, "camera": cameras[role].name})
    scene.frame_set(impact_frame)
    bpy.context.view_layer.update()
    for obj in resolved["environment"] + resolved["landmarks"]:
        obj.hide_render = False
        obj.hide_viewport = False
        obj.hide_set(False)
    hand_radius = resolved["hand"].dimensions.length * 1.5
    hand_visuals = [obj for obj in scene.objects if obj.type == "MESH" and not obj.hide_render and obj.get("bfs_pc4_system") == "hand" and (obj.matrix_world.translation - resolved["hand"].matrix_world.translation).length <= hand_radius]
    medium_objects = hand_visuals + [created["housing"], created["plunger"]]
    low, high = document["cinematography"]["mediumOccupancyRange"]; desired_medium = low + (high - low) / 6.0
    medium = _fit_camera(scene, cameras["medium"], medium_objects, desired_medium, -created["normal"]); medium["lineOfSight"] = _clear_line_of_sight(scene, cameras["medium"], medium_objects, desired_medium, cameras["medium"].matrix_world.to_quaternion() @ Vector((1.0, 0.0, 0.0)), medium_objects)

    scene.frame_set(close_frame)
    bpy.context.view_layer.update()
    landmark_layout = _organize_landmarks(document, resolved, cameras["close"])
    close_forward = _face_forward(resolved["faceplate"], cameras["close"])
    close_objects = [resolved["faceplate"]] + resolved["landmarks"]
    close = _fit_camera(scene, cameras["close"], close_objects, 0.68, close_forward)
    visible_head = [obj for obj in scene.objects if obj.type == "MESH" and not obj.hide_render and obj.get("bfs_pc4_system") == "head"]
    face_extents = [max(corner[index] for corner in resolved["faceplate"].bound_box) - min(corner[index] for corner in resolved["faceplate"].bound_box) for index in range(3)]
    face_width_axis = max(range(3), key=lambda index: (face_extents[index], -index))
    orbit_axis = resolved["faceplate"].matrix_world.to_3x3() @ Vector(tuple(1.0 if index == face_width_axis else 0.0 for index in range(3)))
    close["lineOfSight"] = _clear_line_of_sight(scene, cameras["close"], close_objects, 0.68, orbit_axis, close_objects + visible_head)
    x0, x1, y0, y1 = _projected_bounds(scene, cameras["close"], close_objects)
    close["occupancy"] = round(max(x1 - x0, y1 - y0), 8)
    close["negativeSpaceMargin"] = round(min(x0, y0, 1 - x1, 1 - y1), 8)
    close["cameraLocation"] = [round(value, 8) for value in cameras["close"].location]
    visible_landmarks = [obj.name for obj in resolved["landmarks"] if _overlaps_frame(scene, cameras["close"], obj)]
    face_bounds = _projected_bounds(scene, cameras["close"], [resolved["faceplate"]])
    face_area = max(1e-12, (face_bounds[1] - face_bounds[0]) * (face_bounds[3] - face_bounds[2]))
    landmark_ratios = {}
    for obj in resolved["landmarks"]:
        bounds = _projected_bounds(scene, cameras["close"], [obj])
        landmark_ratios[obj.name] = max(0.0, (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])) / face_area

    scene.frame_set(wide_frame)
    bpy.context.view_layer.update()
    visible_environment = [obj.name for obj in resolved["environment"] if _overlaps_frame(scene, cameras["wide"], obj)]

    scene.frame_set(impact_frame - 1)
    bpy.context.view_layer.update()
    before = {obj.name: world_to_camera_view(scene, cameras["medium"], obj.matrix_world.translation) for obj in (created["collider"], created["plunger"])}
    scene.frame_set(impact_frame)
    bpy.context.view_layer.update()
    after = {obj.name: world_to_camera_view(scene, cameras["medium"], obj.matrix_world.translation) for obj in (created["collider"], created["plunger"])}
    width, height = 960, 540
    speeds = {name: math.hypot((after[name].x - before[name].x) * width, (after[name].y - before[name].y) * height) for name in before}
    median_speed = statistics.median(speeds.values())
    if median_speed <= 1e-8:
        raise PhysicalPerformanceError("MOTION_BLUR_MEASUREMENT", "projected contact motion is zero")
    target_pixels = 6.0
    shutter = round(max(0.2, min(0.5, target_pixels / median_speed)), 8)
    scene.render.use_motion_blur = True
    scene.render.motion_blur_shutter = shutter
    scene.render.motion_blur_position = document["cinematography"]["motionBlurPosition"]
    scene.frame_set(contact_frame)
    bpy.context.view_layer.update()
    scene.camera = cameras["wide"]
    return {
        "cameraMarkerRebindings": marker_rebindings,
        "wide": {"frame": wide_frame, "camera": cameras["wide"].name, "visibleEnvironmentLayers": visible_environment, "visibleEnvironmentLayerCount": len(visible_environment)},
        "medium": {"frame": impact_frame, "camera": cameras["medium"].name, **medium},
        "close": {"frame": close_frame, "camera": cameras["close"].name, **close, "viewDirectionSource": "FACEPLATE_THINNEST_WORLD_AXIS", "landmarkLayout": landmark_layout, "visibleFacialLandmarks": visible_landmarks, "visibleFacialLandmarkCount": len(visible_landmarks), "largestLandmarkFaceAreaRatio": round(max(landmark_ratios.values()), 8)},
        "motionBlur": {"source": "BULLET_PEAK_RESPONSE_PROJECTED_MOTION", "measurementFrame": impact_frame, "measurementResolution": [width, height], "objectPixelsPerFrame": {name: round(value, 8) for name, value in speeds.items()}, "medianPixelsPerFrame": round(median_speed, 8), "targetBlurPixels": target_pixels, "computedShutterFrames": shutter, "achievedMedianBlurPixels": round(median_speed * shutter, 8), "position": document["cinematography"]["motionBlurPosition"], "nativeTransformMotionBlur": True, "compositorOrPostprocessBlur": False},
    }


def execute_physical_performance(repository_root, spec_uri, inspection_token, scene=None):
    scene = scene or bpy.context.scene
    document, resolved, contact, file_hash, expected_token = _inspection(repository_root, spec_uri, scene)
    if not isinstance(inspection_token, str) or inspection_token != expected_token:
        raise PhysicalPerformanceError("INSPECTION_TOKEN_MISMATCH", "inspect again before execution")
    created = _create_mechanism(scene, resolved, contact, document)
    scene.frame_start = document["sourceScene"]["frameStart"]
    scene.frame_end = document["sourceScene"]["frameEnd"]
    scene.render.fps = document["sourceScene"]["fps"]
    physics = _simulate(scene, created, contact, document)
    cinematography = _configure_cinematography(scene, resolved, created, physics, document)
    start = document["sourceScene"]["frameStart"]
    scene.frame_set(start)
    with bpy.context.temp_override(point_cache=scene.rigidbody_world.point_cache):
        bpy.ops.ptcache.bake(bake=True)
    scene.frame_set(start)
    bpy.context.view_layer.update()
    separation = (created["collider"].matrix_world.translation - created["plunger"].matrix_world.translation).length
    initial_penetration = max(0.0, created["colliderRadius"] + created["plungerRadius"] - separation)
    scene.frame_set(physics["contactFrame"])
    bpy.context.view_layer.update()
    result = {
        "schemaVersion": "bfs.physicalPerformanceResult.v0.1",
        "status": "PASS_EXECUTED",
        "performanceSpecHash": document["performanceSpecHash"],
        "performanceSpecFileSha256": file_hash,
        "contact": contact,
        "mechanism": {"factory": document["physicalMechanism"]["factory"], "objects": {key: value.name for key, value in created.items() if isinstance(value, bpy.types.Object)}, "kinematicHandColliderCount": 1, "activeMechanismRigidBodyCount": 1, "springConstraintCount": 1, "initialPenetrationMeters": round(initial_penetration, 8), "postContactMechanismPoseKeyframes": _mechanism_pose_keyframes(created["plunger"]), "finalPoseSource": document["physicalMechanism"]["postContactPoseSource"]},
        "physics": physics,
        "cinematography": cinematography,
        "review": {"stillFrames": [cinematography["wide"]["frame"], cinematography["medium"]["frame"], cinematography["close"]["frame"]], "contactClipFrameRangeInclusive": [max(document["sourceScene"]["frameStart"], physics["contactFrame"] - 16), min(document["sourceScene"]["frameEnd"], physics["contactFrame"] + 31)]},
    }
    result["resultHash"] = _sha256(_canonical(result).encode("utf-8"))
    scene["film_studio_physical_performance_result"] = json.dumps(result, sort_keys=True, separators=(",", ":"))
    scene["film_studio_physical_performance_result_hash"] = result["resultHash"]
    scene["film_studio_physical_performance_spec_hash"] = document["performanceSpecHash"]
    return result
