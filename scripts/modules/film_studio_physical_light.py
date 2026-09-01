# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Restricted rolling-body / hinged-occluder physical-light contract.

The spec supplies metric initial conditions and cinematic intent only. Blender
Bullet owns the sphere motion, contact time, shutter angle and all final poses.
The reveal lamp is static; illumination changes only when simulated geometry
opens the aperture.
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
import film_studio_physical_performance as direction


SPEC_VERSION = "bfs.physicalLightTransferSpec.v0.1"
CONTRACT_VERSION = "bfs.filmStudioPhysicalLight.v0.1"
GENERATED_TAG = "bfs.physicalLight.v0.1"


class PhysicalLightError(RuntimeError):
    """A fail-closed physical-light rejection with a stable reason."""

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
    raise PhysicalLightError("NONFINITE_NUMBER", f"Non-finite JSON token {token} is forbidden")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except PhysicalLightError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PhysicalLightError("INVALID_JSON", str(error)) from error


def _below_existing(root, uri):
    normalized = str(uri).replace("\\", "/")
    if not normalized or normalized.startswith("/") or "://" in normalized or ".." in normalized.split("/"):
        raise PhysicalLightError("PATH_ESCAPE", "PhysicalLightSpec escapes the repository root")
    candidate = root / normalized
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise PhysicalLightError("MISSING_INPUT", normalized) from error
    if root not in resolved.parents or Path(os.path.abspath(candidate)) != resolved:
        raise PhysicalLightError("PATH_ESCAPE", "PhysicalLightSpec traverses a link or escapes the root")
    return resolved


def _exact_keys(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        reason = "UNKNOWN_TOP_LEVEL_FIELD" if path == "/" else "SPEC_SCHEMA"
        raise PhysicalLightError(reason, path)


def _number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PhysicalLightError("NONFINITE_NUMBER", label)
    if not low <= value <= high:
        raise PhysicalLightError("SPEC_SCHEMA", label)


def _vector(value, count, low, high, label):
    if not isinstance(value, list) or len(value) != count:
        raise PhysicalLightError("SPEC_SCHEMA", label)
    for item in value:
        _number(item, low, high, label)


def _validate(document):
    _exact_keys(document, {"$schema", "schemaVersion", "projectId", "timeline", "world", "rollingActor", "ramp", "lightGate", "illumination", "cinematography", "forbidden", "physicalLightSpecHash"}, "/")
    if document["$schema"] != SPEC_VERSION or document["schemaVersion"] != SPEC_VERSION:
        raise PhysicalLightError("UNSUPPORTED_SCHEMA", str(document.get("schemaVersion")))
    if not isinstance(document["projectId"], str) or not document["projectId"]:
        raise PhysicalLightError("SPEC_SCHEMA", "projectId")
    if document["physicalLightSpecHash"] != _self_hash(document, "physicalLightSpecHash"):
        raise PhysicalLightError("SELF_HASH_MISMATCH", "physicalLightSpecHash")

    timeline = document["timeline"]
    _exact_keys(timeline, {"frameStart", "frameEnd", "fps"}, "/timeline")
    for key in ("frameStart", "frameEnd", "fps"):
        _number(timeline[key], 1, 100000, key)
    if timeline["frameEnd"] <= timeline["frameStart"] + 48:
        raise PhysicalLightError("SPEC_SCHEMA", "timeline range")

    world = document["world"]
    _exact_keys(world, {"gravityMetersPerSecondSquared", "unitScaleMeters"}, "/world")
    _vector(world["gravityMetersPerSecondSquared"], 3, -100.0, 100.0, "gravity")
    if world["gravityMetersPerSecondSquared"] != [0.0, 0.0, -9.81] or world["unitScaleMeters"] != 1.0:
        raise PhysicalLightError("NONMETRIC_SCENE", "world")

    actor = document["rollingActor"]
    _exact_keys(actor, {"archetype", "radiusMeters", "massKg", "friction", "restitution", "startAlongRampMeters", "pathOffsetFromHingeMeters", "poseSourceAfterRelease"}, "/rollingActor")
    if actor["archetype"] != "GROOVED_CERAMIC_SPHERE" or actor["poseSourceAfterRelease"] != "BLENDER_BULLET_RIGID_BODY":
        raise PhysicalLightError("UNSUPPORTED_FACTORY", "rollingActor")
    for key, low, high in (("radiusMeters", 0.04, 0.5), ("massKg", 0.05, 20.0), ("friction", 0.05, 1.0), ("restitution", 0.0, 0.8), ("startAlongRampMeters", 0.1, 1.0), ("pathOffsetFromHingeMeters", 0.1, 2.0)):
        _number(actor[key], low, high, key)

    ramp = document["ramp"]
    _exact_keys(ramp, {"factory", "lengthMeters", "widthMeters", "riseMeters", "deckThicknessMeters", "friction"}, "/ramp")
    if ramp["factory"] != "METRIC_WEDGE_WITH_SIDE_RAILS":
        raise PhysicalLightError("UNSUPPORTED_FACTORY", "ramp")
    for key, low, high in (("lengthMeters", 0.5, 8.0), ("widthMeters", 0.3, 4.0), ("riseMeters", 0.05, 3.0), ("deckThicknessMeters", 0.01, 0.3), ("friction", 0.05, 1.0)):
        _number(ramp[key], low, high, key)

    gate = document["lightGate"]
    _exact_keys(gate, {"factory", "apertureWidthMeters", "apertureHeightMeters", "shutterThicknessMeters", "shutterMassKg", "hingeAxis", "hingeLowerDegrees", "hingeUpperDegrees", "linearDamping", "angularDamping", "poseSourceAfterContact"}, "/lightGate")
    if gate["factory"] != "HINGED_OCCLUDER_IN_ARCHITECTURAL_APERTURE" or gate["hingeAxis"] != "WORLD_Z" or gate["poseSourceAfterContact"] != "BLENDER_BULLET_RIGID_BODY_AND_HINGE_CONSTRAINT":
        raise PhysicalLightError("UNSUPPORTED_FACTORY", "lightGate")
    for key, low, high in (("apertureWidthMeters", 0.3, 3.0), ("apertureHeightMeters", 0.3, 3.0), ("shutterThicknessMeters", 0.01, 0.3), ("shutterMassKg", 0.05, 40.0), ("hingeLowerDegrees", -10.0, 10.0), ("hingeUpperDegrees", 20.0, 160.0), ("linearDamping", 0.0, 1.0), ("angularDamping", 0.0, 1.0)):
        _number(gate[key], low, high, key)
    if gate["hingeUpperDegrees"] <= gate["hingeLowerDegrees"]:
        raise PhysicalLightError("SPEC_SCHEMA", "hinge limits")

    light = document["illumination"]
    _exact_keys(light, {"source", "powerWatts", "colorLinearRgb", "animatedPowerChannels", "receiver", "changeSource"}, "/illumination")
    if light["source"] != "STATIC_AREA_LIGHT_BEHIND_PHYSICAL_OCCLUDER" or light["receiver"] != "MATTE_RELIEF_SIGNAL_WALL" or light["changeSource"] != "SIMULATED_OCCLUDER_GEOMETRY_ONLY" or light["animatedPowerChannels"] != 0:
        raise PhysicalLightError("LIGHT_AUTHORITY", "illumination")
    _number(light["powerWatts"], 10.0, 10000.0, "powerWatts")
    _vector(light["colorLinearRgb"], 3, 0.0, 1.0, "colorLinearRgb")

    cinema = document["cinematography"]
    _exact_keys(cinema, {"shotStrategy", "motionBlurStrategy", "motionBlurPosition", "reviewResolution", "contactClipFrameCount"}, "/cinematography")
    if cinema["shotStrategy"] != "SEMANTIC_CAUSE_CONTACT_REVEAL" or cinema["motionBlurStrategy"] != "MEASURED_PROJECTED_MEDIAN_MOTION" or cinema["motionBlurPosition"] != "CENTER" or cinema["reviewResolution"] != [960, 540] or cinema["contactClipFrameCount"] != 48:
        raise PhysicalLightError("SPEC_SCHEMA", "cinematography")

    forbidden = document["forbidden"]
    keys = {"authoredActorTransformAfterRelease", "authoredShutterTransformAfterContact", "manualContactFrame", "animatedLightPowerOrColor", "fakeEmissionReveal", "postprocessLightBeam", "postprocessMotionBlur", "projectIdOrFixtureHashBranchInProductCode", "arbitraryPythonShellNetworkOrFilesystemAuthority"}
    _exact_keys(forbidden, keys, "/forbidden")
    if any(forbidden[key] is not True for key in keys):
        raise PhysicalLightError("AUTHORITY_EXPANSION", "all forbidden controls must remain true")
    return document


def matches_physical_light(repository_root, spec_uri):
    root = Path(repository_root).resolve(strict=True)
    return _read_json(_below_existing(root, spec_uri)).get("schemaVersion") == SPEC_VERSION


def _inspection(repository_root, spec_uri):
    root = Path(repository_root).resolve(strict=True)
    path = _below_existing(root, spec_uri)
    document = _validate(_read_json(path))
    file_hash = _sha256(path.read_bytes())
    body = {"contractVersion": CONTRACT_VERSION, "fileSha256": file_hash, "physicalLightSpecHash": document["physicalLightSpecHash"]}
    return document, file_hash, _sha256(_canonical(body).encode("utf-8"))


def inspect_physical_light(repository_root, spec_uri):
    document, file_hash, token = _inspection(repository_root, spec_uri)
    return {
        "status": "APPROVED_READY",
        "sceneId": document["projectId"],
        "actorFactory": document["rollingActor"]["archetype"],
        "targetFactory": document["lightGate"]["factory"],
        "targetCount": 1,
        "collisionShapes": ["SPHERE", "BOX"],
        "finalPoseSource": "BLENDER_BULLET_RIGID_BODY_AND_HINGE_CONSTRAINT",
        "cameraFitSource": "EVALUATED_SEMANTIC_CAUSE_CONTACT_REVEAL",
        "sceneSpecHash": document["physicalLightSpecHash"],
        "fileSha256": file_hash,
        "inspectionToken": token,
    }


def _tag(obj, role):
    obj["film_studio_physical_light"] = GENERATED_TAG
    obj["film_studio_semantic_role"] = role
    return obj


def _material(name, color, roughness=0.45, metallic=0.0):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = (*color, 1.0)
    principled.inputs["Roughness"].default_value = roughness
    principled.inputs["Metallic"].default_value = metallic
    return material


def _box(name, location, scale, material, role, bevel=0.0, rotation=(0.0, 0.0, 0.0)):
    # Callers specify half-extents. A Blender size=1 cube has unit dimensions,
    # so convert half-extents to full dimensions explicitly.
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location, rotation=rotation, scale=tuple(value * 2.0 for value in scale))
    obj = _tag(bpy.context.object, role)
    obj.name = name
    direction._select(obj)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel > 0.0:
        modifier = obj.modifiers.new("Physical edge radius", "BEVEL")
        modifier.width = bevel
        modifier.segments = 3
    obj.data.materials.append(material)
    return obj


def _passive(obj, shape="BOX", friction=0.6):
    body = direction._rigid_body(obj, "PASSIVE", shape, 1.0, 0.001)
    body.friction = friction
    return body


def _active(obj, shape, mass, friction, restitution, linear=0.04, angular=0.08):
    body = direction._rigid_body(obj, "ACTIVE", shape, mass, 0.001)
    body.friction = friction
    body.restitution = restitution
    body.linear_damping = linear
    body.angular_damping = angular
    return body


def _clear_scene(scene):
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for material in list(bpy.data.materials):
        if material.users == 0:
            bpy.data.materials.remove(material)
    scene.timeline_markers.clear()


def _wedge(name, start_x, end_x, rise, width, thickness, material, friction):
    vertices = [
        (start_x, -width / 2, rise), (start_x, width / 2, rise),
        (end_x, -width / 2, 0.0), (end_x, width / 2, 0.0),
        (start_x, -width / 2, -thickness), (start_x, width / 2, -thickness),
        (end_x, -width / 2, -thickness), (end_x, width / 2, -thickness),
    ]
    faces = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 4, 6, 2), (1, 3, 7, 5), (0, 1, 5, 4), (2, 6, 7, 3)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = _tag(bpy.data.objects.new(name, mesh), "metric_ramp")
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(material)
    _passive(obj, "MESH", friction)
    return obj


def _parent_keep(child, parent):
    world = child.matrix_world.copy()
    child.parent = parent
    child.matrix_world = world


def _camera(name, frame, target, semantic_objects, direction_vector, occupancy):
    data = bpy.data.cameras.new(name)
    data.lens = 46.0
    data.sensor_width = 36.0
    data.dof.use_dof = True
    camera = _tag(bpy.data.objects.new(name, data), f"camera_{name.lower()}")
    bpy.context.scene.collection.objects.link(camera)
    camera.location = Vector(target) - Vector(direction_vector).normalized() * 5.0
    direction._point_at(camera, target)
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    fitted = direction._fit_camera(bpy.context.scene, camera, semantic_objects, occupancy, direction_vector)
    focus = sum(direction._world_points(semantic_objects), Vector()) / len(direction._world_points(semantic_objects))
    data.dof.focus_object = None
    data.dof.focus_distance = (focus - camera.location).length
    data.dof.aperture_fstop = 5.6
    return camera, fitted


def _pose_keyframes(obj):
    if not obj.animation_data or not obj.animation_data.action:
        return 0
    action = obj.animation_data.action
    curves = list(action.fcurves) if hasattr(action, "fcurves") else []
    return sum(len(curve.keyframe_points) for curve in curves if curve.data_path in {"location", "rotation_euler", "rotation_quaternion", "scale"})


def _closest_sphere_panel_gap(ball, shutter, radius, width, height, thickness):
    local = shutter.matrix_world.inverted() @ ball.matrix_world.translation
    closest = Vector((
        max(-thickness / 2, min(thickness / 2, local.x)),
        max(-width / 2, min(width / 2, local.y)),
        max(-height / 2, min(height / 2, local.z)),
    ))
    return max(0.0, (local - closest).length - radius)


def _shutter_angle(initial_quaternion, shutter):
    return math.degrees(initial_quaternion.rotation_difference(shutter.matrix_world.to_quaternion()).angle)


def _build_scene(scene, document):
    _clear_scene(scene)
    timeline, actor, ramp, gate, light_spec = (document[key] for key in ("timeline", "rollingActor", "ramp", "lightGate", "illumination"))
    scene.frame_start, scene.frame_end, scene.render.fps = timeline["frameStart"], timeline["frameEnd"], timeline["fps"]
    scene.gravity = Vector(document["world"]["gravityMetersPerSecondSquared"])
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = document["cinematography"]["reviewResolution"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    scene.render.image_settings.color_depth = "8"
    scene.world.color = (0.003, 0.006, 0.012)
    scene.view_settings.look = "AgX - Medium High Contrast"

    dark = _material("MAT_SignalGate_Dark", (0.018, 0.028, 0.045), 0.36, 0.7)
    edge = _material("MAT_SignalGate_Edge", (0.04, 0.15, 0.2), 0.28, 0.75)
    ceramic = _material("MAT_SignalGate_Ceramic", (0.42, 0.055, 0.018), 0.24, 0.08)
    groove = _material("MAT_SignalGate_Groove", (0.015, 0.34, 0.42), 0.24, 0.38)
    receiver_material = _material("MAT_SignalGate_Receiver", (0.15, 0.18, 0.2), 0.62, 0.05)
    signal_material = _material("MAT_SignalGate_Signal", (0.36, 0.19, 0.035), 0.3, 0.6)

    floor = _box("PHYSICAL_LIGHT_FLOOR", (1.0, 0.0, -0.08), (5.2, 2.4, 0.08), dark, "floor", 0.025)
    _passive(floor, "BOX", 0.72)
    ramp_start, ramp_end = -2.75, -0.15
    ramp_obj = _wedge("PHYSICAL_LIGHT_RAMP", ramp_start, ramp_end, ramp["riseMeters"], ramp["widthMeters"], ramp["deckThicknessMeters"], dark, ramp["friction"])
    angle = math.atan2(ramp["riseMeters"], ramp["lengthMeters"])
    slope_length = math.hypot(ramp["lengthMeters"], ramp["riseMeters"])
    for side in (-1, 1):
        rail = _box(f"PHYSICAL_LIGHT_RAIL_{side:+d}", ((ramp_start + ramp_end) / 2, side * (ramp["widthMeters"] / 2 + 0.025), ramp["riseMeters"] / 2 + 0.055), (slope_length / 2, 0.025, 0.055), edge, "ramp_rail", 0.012, (0.0, angle, 0.0))
        _passive(rail, "BOX", ramp["friction"])

    gate_x, gate_z = 0.76, 0.55
    door_x = gate_x + 0.15
    width, height, thick = gate["apertureWidthMeters"], gate["apertureHeightMeters"], gate["shutterThicknessMeters"]
    shutter_width = width - 0.018
    shutter_height = height - 0.018
    hinge_y = -width / 2 + 0.009
    for name, location, scale in (
        ("LEFT", (gate_x, -width / 2 - 0.1, gate_z), (0.12, 0.1, height / 2 + 0.16)),
        ("RIGHT", (gate_x, width / 2 + 0.1, gate_z), (0.12, 0.1, height / 2 + 0.16)),
        ("TOP", (gate_x, 0.0, gate_z + height / 2 + 0.1), (0.12, width / 2 + 0.2, 0.1)),
    ):
        frame = _box(f"PHYSICAL_LIGHT_APERTURE_{name}", location, scale, edge, "aperture_frame", 0.025)
        frame["film_studio_collision_role"] = "VISUAL_ARCHITECTURE_OUTSIDE_ACTOR_PATH"
    for name, location, scale in (
        ("LEFT", (gate_x + 0.01, -1.18, 1.0), (0.1, 0.62, 1.0)),
        ("RIGHT", (gate_x + 0.01, 1.18, 1.0), (0.1, 0.62, 1.0)),
        ("TOP", (gate_x + 0.01, 0.0, 1.58), (0.1, 1.8, 0.48)),
    ):
        baffle = _box(f"PHYSICAL_LIGHT_BAFFLE_{name}", location, scale, dark, "light_baffle", 0.018)
        baffle["film_studio_light_authority"] = "STATIC_GEOMETRY_OCCLUSION"
    hinge_post = _box("PHYSICAL_LIGHT_HINGE_POST", (door_x, -width / 2 - 0.045, gate_z), (0.035, 0.035, height / 2 + 0.12), edge, "hinge_hardware", 0.012)

    shutter = _box("PHYSICAL_LIGHT_SHUTTER", (door_x, 0.0, gate_z), (thick / 2, shutter_width / 2, shutter_height / 2), dark, "hinged_occluder", 0.014)
    shutter["film_studio_pose_source"] = gate["poseSourceAfterContact"]
    shutter["film_studio_closed_matrix"] = [value for row in shutter.matrix_world for value in row]
    body = _active(shutter, "BOX", gate["shutterMassKg"], 0.58, 0.08, gate["linearDamping"], gate["angularDamping"])
    body.use_deactivation = False
    anchor = _box("PHYSICAL_LIGHT_HINGE_ANCHOR", (door_x, hinge_y, gate_z), (0.006, 0.006, 0.006), dark, "passive_hinge_anchor")
    anchor.hide_render = True
    anchor.display_type = "WIRE"
    _passive(anchor)
    for index in range(5):
        y = hinge_y + shutter_width * (index + 0.5) / 5
        rib = _box(f"PHYSICAL_LIGHT_SHUTTER_RIB_{index}", (door_x - thick * 0.58, y, gate_z), (0.014, shutter_width / 12, height * 0.42), edge, "shutter_rib", 0.008)
        _parent_keep(rib, shutter)
    for index, z in enumerate((gate_z - height * 0.32, gate_z, gate_z + height * 0.32)):
        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.065, depth=0.11, location=(door_x, hinge_y - 0.025, z), rotation=(math.pi / 2, 0.0, 0.0))
        knuckle = _tag(bpy.context.object, "hinge_hardware")
        knuckle.name = f"PHYSICAL_LIGHT_HINGE_KNUCKLE_{index}"
        knuckle.data.materials.append(edge)
        _parent_keep(knuckle, shutter if index != 1 else hinge_post)

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(door_x, hinge_y, gate_z))
    hinge = _tag(bpy.context.object, "hinge_constraint")
    hinge.name = "PHYSICAL_LIGHT_HINGE_CONSTRAINT"
    direction._select(hinge)
    bpy.ops.rigidbody.constraint_add()
    constraint = hinge.rigid_body_constraint
    constraint.type = "HINGE"
    constraint.object1 = anchor
    constraint.object2 = shutter
    constraint.disable_collisions = True
    constraint.use_limit_ang_z = True
    constraint.limit_ang_z_lower = math.radians(gate["hingeLowerDegrees"])
    constraint.limit_ang_z_upper = math.radians(gate["hingeUpperDegrees"])
    constraint.use_override_solver_iterations = True
    constraint.solver_iterations = 120

    # A real gate needs a collision-owned end stop rather than relying on the
    # mathematical hinge limit as its visible piece of hardware.  Keep a
    # five-degree safety margin inside the authored upper limit so Bullet,
    # rather than a pose key or post-simulation clamp, owns the peak angle.
    stop_angle_degrees = gate["hingeUpperDegrees"] - 5.0
    stop_angle = math.radians(stop_angle_degrees)
    stop_radius = max(0.035, thick * 0.7)
    stop_location = (
        door_x + shutter_width * math.sin(stop_angle),
        hinge_y + shutter_width * math.cos(stop_angle),
        gate_z,
    )
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=32,
        radius=stop_radius,
        depth=max(0.22, shutter_height * 0.28),
        location=stop_location,
    )
    angular_stop = _tag(bpy.context.object, "hinge_angular_stop")
    angular_stop.name = "PHYSICAL_LIGHT_HINGE_ANGULAR_STOP"
    angular_stop.data.materials.append(edge)
    angular_stop["film_studio_pose_source"] = "STATIC_PASSIVE_COLLISION"
    angular_stop["film_studio_stop_angle_degrees"] = stop_angle_degrees
    _passive(angular_stop, "CYLINDER", 0.62)

    radius = actor["radiusMeters"]
    start_x = ramp_start + actor["startAlongRampMeters"]
    surface_z = ramp["riseMeters"] * (ramp_end - start_x) / (ramp_end - ramp_start)
    ball_y = -width / 2 + actor["pathOffsetFromHingeMeters"]
    normal = Vector((math.sin(angle), 0.0, math.cos(angle)))
    ball_location = Vector((start_x, ball_y, surface_z)) + normal * (radius + 0.002)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=radius, location=ball_location)
    ball = _tag(bpy.context.object, "rolling_actor")
    ball.name = "PHYSICAL_LIGHT_ROLLING_ACTOR"
    ball.data.materials.append(ceramic)
    ball["film_studio_pose_source"] = actor["poseSourceAfterRelease"]
    ball["film_studio_release_frame"] = timeline["frameStart"]
    _active(ball, "SPHERE", actor["massKg"], actor["friction"], actor["restitution"], 0.02, 0.035)
    for index, rotation in enumerate(((0.0, 0.0, 0.0), (math.pi / 2, 0.0, 0.0), (0.0, math.pi / 2, 0.0))):
        bpy.ops.mesh.primitive_torus_add(major_radius=radius * 0.985, minor_radius=radius * 0.04, major_segments=64, minor_segments=10, location=ball_location, rotation=rotation)
        ring = _tag(bpy.context.object, "rotation_witness_groove")
        ring.name = f"PHYSICAL_LIGHT_ACTOR_GROOVE_{index}"
        ring.data.materials.append(groove)
        _parent_keep(ring, ball)

    receiver = _box("PHYSICAL_LIGHT_RECEIVER", (-0.34, -0.22, 0.018), (0.68, 0.6, 0.018), receiver_material, "illumination_receiver", 0.018)
    _passive(receiver)
    for index, y in enumerate((-0.32, -0.18, -0.04)):
        bar = _box(f"PHYSICAL_LIGHT_SIGNAL_BAR_{index}", (-0.36 + index * 0.13, y, 0.047), (0.28 - index * 0.035, 0.035, 0.018), signal_material, "relief_signal", 0.012, (0.0, 0.0, -0.3))
        _passive(bar)

    light_data = bpy.data.lights.new("PHYSICAL_LIGHT_STATIC_REVEAL", "AREA")
    light_data.energy = light_spec["powerWatts"]
    light_data.color = light_spec["colorLinearRgb"]
    light_data.shape = "DISK"
    light_data.size = 0.52
    light = _tag(bpy.data.objects.new("PHYSICAL_LIGHT_STATIC_REVEAL", light_data), "static_reveal_light")
    scene.collection.objects.link(light)
    light.location = (2.3, 0.08, 1.8)
    direction._point_at(light, (-0.25, -0.2, 0.0))
    light["film_studio_change_source"] = light_spec["changeSource"]
    fill_data = bpy.data.lights.new("PHYSICAL_LIGHT_STATIC_FILL", "AREA")
    fill_data.energy = 115.0
    fill_data.color = (0.06, 0.18, 0.32)
    fill_data.shape = "RECTANGLE"
    fill_data.size = 3.0
    fill = _tag(bpy.data.objects.new("PHYSICAL_LIGHT_STATIC_FILL", fill_data), "static_fill_light")
    scene.collection.objects.link(fill)
    fill.location = (-0.8, -0.6, 3.2)
    direction._point_at(fill, (-0.5, 0.0, 0.0))
    catcher = _box("PHYSICAL_LIGHT_ACTOR_CATCHER", (2.0, ball_y, 0.24), (0.055, 1.2, 0.24), dark, "actor_catcher", 0.025)
    _passive(catcher, "BOX", 0.7)

    world = scene.rigidbody_world
    if world is None:
        raise PhysicalLightError("PHYSICS_WORLD", "rigid body world was not created")
    world.substeps_per_frame = max(world.substeps_per_frame, 40)
    world.solver_iterations = max(world.solver_iterations, 120)
    world.point_cache.frame_start = timeline["frameStart"]
    world.point_cache.frame_end = timeline["frameEnd"]
    return {"ball": ball, "shutter": shutter, "hinge": hinge, "hingeAnchor": anchor, "hingePost": hinge_post, "hingeAngularStop": angular_stop, "hingeAngularStopDegrees": stop_angle_degrees, "ramp": ramp_obj, "receiver": receiver, "light": light, "fill": fill, "floor": floor, "apertureWidth": width, "shutterWidth": shutter_width, "apertureHeight": height, "shutterThickness": thick}


def _simulate(scene, created, document):
    timeline, actor = document["timeline"], document["rollingActor"]
    start, end, fps = timeline["frameStart"], timeline["frameEnd"], timeline["fps"]
    ball, shutter = created["ball"], created["shutter"]
    initial_ball = ball.matrix_world.translation.copy()
    initial_shutter = shutter.matrix_world.to_quaternion().copy()
    rows = []
    previous_ball = previous_quaternion = None
    for frame in range(start, end + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        position = ball.matrix_world.translation.copy()
        quaternion = ball.matrix_world.to_quaternion().copy()
        distance = 0.0 if previous_ball is None else (position - previous_ball).length
        if previous_quaternion is None:
            angle_step = 0.0
        else:
            raw_angle = previous_quaternion.rotation_difference(quaternion).angle
            angle_step = min(raw_angle, math.tau - raw_angle)
        rolling_arc = actor["radiusMeters"] * angle_step
        slip = 0.0 if max(distance, rolling_arc) < 1e-6 else abs(distance - rolling_arc) / max(distance, rolling_arc)
        gap = _closest_sphere_panel_gap(ball, shutter, actor["radiusMeters"], created["shutterWidth"], created["apertureHeight"], created["shutterThickness"])
        rows.append({"frame": frame, "actorLocation": list(position), "actorQuaternion": list(quaternion), "actorStepMeters": distance, "rollingArcStepMeters": rolling_arc, "rollingSlipRatio": slip, "spherePanelGapMeters": gap, "shutterAngleDegrees": _shutter_angle(initial_shutter, shutter)})
        previous_ball, previous_quaternion = position, quaternion
    contact = next((row["frame"] for row in rows if row["frame"] > start and row["spherePanelGapMeters"] <= 0.002), None)
    response = next((row["frame"] for row in rows if contact is not None and row["frame"] >= contact and row["shutterAngleDegrees"] >= 0.1), None)
    if contact is None or response is None:
        minimum_gap = min(rows, key=lambda row: row["spherePanelGapMeters"])
        maximum_angle = max(rows, key=lambda row: row["shutterAngleDegrees"])
        raise PhysicalLightError(
            "PHYSICS_RESPONSE",
            f"no derived sphere-to-shutter response; minimum gap {minimum_gap['spherePanelGapMeters']:.8f} m at frame {minimum_gap['frame']}; maximum angle {maximum_angle['shutterAngleDegrees']:.8f} deg at frame {maximum_angle['frame']}",
        )
    peak = max((row for row in rows if row["frame"] >= response), key=lambda row: (row["shutterAngleDegrees"], -row["frame"]))
    reversal = next((row["frame"] for row in rows if row["frame"] > peak["frame"] and row["shutterAngleDegrees"] <= peak["shutterAngleDegrees"] - 0.2), None)
    settled = None
    for index, row in enumerate(rows):
        window = rows[index:index + 8]
        if row["frame"] >= response and len(window) == 8 and max(abs(window[i + 1]["shutterAngleDegrees"] - window[i]["shutterAngleDegrees"]) for i in range(7)) <= 0.05:
            settled = row["frame"]
            break
    rolling_rows = [row for row in rows if start + 2 <= row["frame"] <= contact and row["actorStepMeters"] >= 0.001]
    median_slip = statistics.median(row["rollingSlipRatio"] for row in rolling_rows) if rolling_rows else 1.0
    travel = max((Vector(row["actorLocation"]) - initial_ball).length for row in rows)
    scene.frame_set(end)
    bpy.context.view_layer.update()
    return {
        "source": "BLENDER_BULLET_EVALUATED_WORLD_TRANSFORMS",
        "contactFrame": contact,
        "firstShutterResponseFrame": response,
        "firstResponseDelayFrames": response - contact,
        "peakOpenFrame": peak["frame"],
        "peakShutterOpenDegrees": round(peak["shutterAngleDegrees"], 8),
        "directionReversalFrame": reversal,
        "settledWindowStartFrame": settled,
        "actorTravelMeters": round(travel, 8),
        "medianRollingSlipRatio": round(median_slip, 8),
        "samples": [{"frame": row["frame"], "actorLocation": [round(value, 8) for value in row["actorLocation"]], "actorQuaternion": [round(value, 8) for value in row["actorQuaternion"]], "actorStepMeters": round(row["actorStepMeters"], 8), "rollingArcStepMeters": round(row["rollingArcStepMeters"], 8), "rollingSlipRatio": round(row["rollingSlipRatio"], 8), "spherePanelGapMeters": round(row["spherePanelGapMeters"], 8), "shutterAngleDegrees": round(row["shutterAngleDegrees"], 8)} for row in rows],
    }


def _configure_cinematography(scene, created, physics, document):
    start, end = document["timeline"]["frameStart"], document["timeline"]["frameEnd"]
    cause_frame = max(start, physics["contactFrame"] - 18)
    contact_frame = min(end, max(physics["firstShutterResponseFrame"] + 3, physics["contactFrame"]))
    reveal_frame = min(end, max(physics["peakOpenFrame"], contact_frame + 18))
    scene.frame_set(cause_frame); bpy.context.view_layer.update()
    cause_objects = [created["ramp"], created["ball"], created["shutter"]]
    cause, cause_fit = _camera("PHYSICAL_LIGHT_CAM_CAUSE", cause_frame, (-0.9, 0.0, 0.35), cause_objects, (1.0, 0.78, -0.48), 0.68)
    scene.frame_set(contact_frame); bpy.context.view_layer.update()
    contact_objects = [created["ball"], created["shutter"], created["hingePost"]]
    contact, contact_fit = _camera("PHYSICAL_LIGHT_CAM_CONTACT", contact_frame, (0.56, 0.0, 0.46), contact_objects, (0.9, 1.0, -0.25), 0.63)
    scene.frame_set(reveal_frame); bpy.context.view_layer.update()
    reveal_objects = [created["receiver"], created["shutter"], created["ball"]]
    reveal, reveal_fit = _camera("PHYSICAL_LIGHT_CAM_REVEAL", reveal_frame, (-0.05, -0.08, 0.28), reveal_objects, (1.0, 0.12, -0.55), 0.68)
    for name, frame, camera in (("CAUSE", cause_frame, cause), ("CONTACT", contact_frame, contact), ("REVEAL", reveal_frame, reveal)):
        marker = scene.timeline_markers.new(f"PHYSICAL_LIGHT_{name}", frame=frame)
        marker.camera = camera

    scene.frame_set(max(start, contact_frame - 1)); bpy.context.view_layer.update()
    before = {obj.name: world_to_camera_view(scene, contact, obj.matrix_world.translation) for obj in (created["ball"], created["shutter"])}
    scene.frame_set(contact_frame); bpy.context.view_layer.update()
    after = {obj.name: world_to_camera_view(scene, contact, obj.matrix_world.translation) for obj in (created["ball"], created["shutter"])}
    width, height = document["cinematography"]["reviewResolution"]
    speeds = {name: math.hypot((after[name].x - before[name].x) * width, (after[name].y - before[name].y) * height) for name in before}
    median_speed = statistics.median(speeds.values())
    if median_speed <= 1e-8:
        raise PhysicalLightError("MOTION_BLUR_MEASUREMENT", "projected contact motion is zero")
    shutter = round(max(0.2, min(0.5, 6.0 / median_speed)), 8)
    scene.render.use_motion_blur = True
    scene.render.motion_blur_shutter = shutter
    scene.render.motion_blur_position = document["cinematography"]["motionBlurPosition"]
    scene.camera = cause
    scene.frame_set(start)
    return {
        "cause": {"frame": cause_frame, "camera": cause.name, **cause_fit},
        "contact": {"frame": contact_frame, "camera": contact.name, **contact_fit},
        "reveal": {"frame": reveal_frame, "camera": reveal.name, **reveal_fit},
        "motionBlur": {"source": "BULLET_CONTACT_PROJECTED_MOTION", "measurementFrame": contact_frame, "measurementResolution": [width, height], "objectPixelsPerFrame": {key: round(value, 8) for key, value in speeds.items()}, "medianPixelsPerFrame": round(median_speed, 8), "targetBlurPixels": 6.0, "computedShutterFrames": shutter, "achievedMedianBlurPixels": round(median_speed * shutter, 8), "position": "CENTER", "nativeTransformMotionBlur": True, "compositorOrPostprocessBlur": False},
    }


def execute_physical_light(repository_root, spec_uri, inspection_token, scene=None):
    scene = scene or bpy.context.scene
    document, file_hash, expected_token = _inspection(repository_root, spec_uri)
    if not isinstance(inspection_token, str) or inspection_token != expected_token:
        raise PhysicalLightError("INSPECTION_TOKEN_MISMATCH", "inspect again before execution")
    created = _build_scene(scene, document)
    physics = _simulate(scene, created, document)
    cinematography = _configure_cinematography(scene, created, physics, document)
    start = document["timeline"]["frameStart"]
    scene.frame_set(start)
    with bpy.context.temp_override(point_cache=scene.rigidbody_world.point_cache):
        bpy.ops.ptcache.bake(bake=True)
    scene.frame_set(start)
    bpy.context.view_layer.update()
    light = created["light"]
    light_curves = 0 if not light.data.animation_data or not light.data.animation_data.action else len(light.data.animation_data.action.fcurves)
    result = {
        "schemaVersion": "bfs.physicalLightResult.v0.1",
        "status": "PASS_EXECUTED",
        "physicalLightSpecHash": document["physicalLightSpecHash"],
        "physicalLightSpecFileSha256": file_hash,
        "objects": {"actor": created["ball"].name, "shutter": created["shutter"].name, "hingeConstraint": created["hinge"].name, "receiver": created["receiver"].name, "staticRevealLight": light.name},
        "authority": {"actorPoseKeyframesAfterRelease": _pose_keyframes(created["ball"]), "shutterPoseKeyframesAfterContact": _pose_keyframes(created["shutter"]), "lightAnimationChannels": light_curves, "lightPowerWatts": light.data.energy, "lightColorLinearRgb": list(light.data.color), "illuminationChangeSource": document["illumination"]["changeSource"], "finalPoseSource": document["lightGate"]["poseSourceAfterContact"]},
        "mechanism": {"activeRigidBodyCount": 2, "hingeConstraintCount": 1, "actorCollisionShape": "SPHERE", "shutterCollisionShape": "BOX", "hingeAxis": "WORLD_Z", "hingeLimitsDegrees": [document["lightGate"]["hingeLowerDegrees"], document["lightGate"]["hingeUpperDegrees"]], "angularStop": {"object": created["hingeAngularStop"].name, "poseSource": "STATIC_PASSIVE_COLLISION", "derivedStopAngleDegrees": created["hingeAngularStopDegrees"], "derivation": "HINGE_UPPER_LIMIT_MINUS_FIVE_DEGREES"}},
        "physics": physics,
        "cinematography": cinematography,
        "review": {"stillFrames": [cinematography[role]["frame"] for role in ("cause", "contact", "reveal")], "contactClipFrameRangeInclusive": [max(start, physics["contactFrame"] - 12), min(document["timeline"]["frameEnd"], physics["contactFrame"] + 35)]},
    }
    result["resultHash"] = _sha256(_canonical(result).encode("utf-8"))
    scene["film_studio_physical_light_result"] = json.dumps(result, sort_keys=True, separators=(",", ":"))
    scene["film_studio_physical_light_result_hash"] = result["resultHash"]
    scene["film_studio_physical_light_spec_hash"] = document["physicalLightSpecHash"]
    return result
