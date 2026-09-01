# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Procedural physical-look treatments for solver-owned action graphs.

This module changes visible construction, material response and passive scale
cues.  It never creates animation, rigid-body authority or event frames.
"""

from __future__ import annotations

import math

import bpy
from mathutils import Vector


def _set_input(node, name, value):
    socket = node.inputs.get(name)
    if socket is not None:
        socket.default_value = value


def _principled_material(name, color, roughness, metallic=0.0, transmission=0.0, ior=1.45, coat=0.0):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    shader = nodes.new("ShaderNodeBsdfPrincipled")
    _set_input(shader, "Base Color", (*color, 1.0))
    _set_input(shader, "Roughness", roughness)
    _set_input(shader, "Metallic", metallic)
    _set_input(shader, "Transmission Weight", transmission)
    _set_input(shader, "IOR", ior)
    _set_input(shader, "Coat Weight", coat)
    _set_input(shader, "Coat Roughness", max(0.03, roughness * 0.55))
    material.node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    material.diffuse_color = (*color, 1.0)
    return material, shader


def _noise_bump(material, shader, scale, strength, distance, detail=4.0, roughness=0.65):
    nodes, links = material.node_tree.nodes, material.node_tree.links
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = detail
    noise.inputs["Roughness"].default_value = roughness
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = strength
    bump.inputs["Distance"].default_value = distance
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], shader.inputs["Normal"])
    return noise


def worn_basketball_material(name="MAT_PhysicalLook_WornBasketball"):
    material, shader = _principled_material(name, (0.58, 0.115, 0.018), 0.52, coat=0.08)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    noise = _noise_bump(material, shader, 185.0, 0.24, 0.0011, detail=2.2, roughness=0.72)
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.18
    ramp.color_ramp.elements[0].color = (0.16, 0.018, 0.004, 1.0)
    ramp.color_ramp.elements[1].position = 0.86
    ramp.color_ramp.elements[1].color = (0.76, 0.24, 0.035, 1.0)
    links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
    return material


def glass_material(name, tint):
    material, shader = _principled_material(name, tint, 0.065, transmission=0.0, ior=1.47, coat=0.48)
    _set_input(shader, "Alpha", 0.28)
    material.diffuse_color = (*tint, 0.28)
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    material.use_screen_refraction = False
    material.use_raytrace_refraction = False
    return material


def liquid_material(name, color):
    material, shader = _principled_material(name, color, 0.16, transmission=0.22, ior=1.333, coat=0.12)
    _set_input(shader, "Alpha", 0.94)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    absorption = nodes.new("ShaderNodeVolumeAbsorption")
    absorption.inputs["Color"].default_value = (*color, 1.0)
    absorption.inputs["Density"].default_value = 0.34
    links.new(absorption.outputs["Volume"], nodes.get("Material Output").inputs["Volume"])
    return material


def paper_material(name, color):
    material, shader = _principled_material(name, color, 0.58)
    _noise_bump(material, shader, 72.0, 0.09, 0.00022, detail=3.0, roughness=0.7)
    return material


def cap_material(name, color):
    material, shader = _principled_material(name, color, 0.28, metallic=0.72, coat=0.18)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    wave = nodes.new("ShaderNodeTexWave")
    wave.wave_type = "BANDS"
    wave.bands_direction = "Z"
    wave.inputs["Scale"].default_value = 68.0
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.14
    bump.inputs["Distance"].default_value = 0.00035
    links.new(wave.outputs["Color"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], shader.inputs["Normal"])
    return material


def wood_material(name="MAT_PhysicalLook_AgedMaple"):
    material, shader = _principled_material(name, (0.43, 0.19, 0.065), 0.31, coat=0.3)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 5.5
    noise.inputs["Detail"].default_value = 5.0
    noise.inputs["Roughness"].default_value = 0.72
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.2
    ramp.color_ramp.elements[0].color = (0.12, 0.035, 0.008, 1.0)
    ramp.color_ramp.elements[1].position = 0.84
    ramp.color_ramp.elements[1].color = (0.62, 0.31, 0.095, 1.0)
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.12
    bump.inputs["Distance"].default_value = 0.0012
    links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], shader.inputs["Normal"])
    return material


def _replace_material(obj, material):
    obj.data.materials.clear()
    obj.data.materials.append(material)


def _parent_preserve(child, parent):
    matrix = child.matrix_world.copy()
    child.parent = parent
    child.matrix_world = matrix


def _tag(obj, stage):
    obj["film_studio_semantic_role"] = "physical_look_detail"
    obj["film_studio_readable_stage"] = stage
    obj["film_studio_collision_authority"] = "NONE_VISUAL_CHILD"
    return obj


def _torus(name, location, major_radius, minor_radius, material, parent, stage):
    bpy.ops.mesh.primitive_torus_add(major_radius=major_radius, minor_radius=minor_radius, major_segments=72, minor_segments=8, location=location)
    obj = _tag(bpy.context.object, stage)
    obj.name = name
    obj.data.materials.append(material)
    _parent_preserve(obj, parent)
    return obj


def enhance_basketball(actor):
    _replace_material(actor, worn_basketball_material())
    actor["film_studio_surface_preset"] = "WORN_GAME_BASKETBALL"
    actor["film_studio_surface_detail_source"] = "PROCEDURAL_NOISE_BUMP"
    return {"preset": "WORN_GAME_BASKETBALL", "proceduralPebbleScale": 185.0, "externalAssets": 0}


def enhance_glass_bottles(targets, details, derived, height, body_radius, wall_thickness):
    glass_colors = ((0.16, 0.34, 0.22), (0.17, 0.29, 0.34), (0.28, 0.24, 0.16))
    liquid_colors = ((0.08, 0.42, 0.18), (0.58, 0.22, 0.04), (0.08, 0.42, 0.52))
    label_colors = ((0.64, 0.095, 0.045), (0.045, 0.22, 0.42), (0.55, 0.31, 0.055))
    cap_colors = ((0.24, 0.25, 0.27), (0.16, 0.19, 0.22), (0.31, 0.27, 0.20))
    detail_by_name = {obj.name: obj for obj in details}
    added = []
    records = []
    for index, (target, physical) in enumerate(zip(targets, derived), 1):
        suffix = f"{index:02d}"
        shell = glass_material(f"MAT_PhysicalLook_Glass_{suffix}", glass_colors[(index - 1) % len(glass_colors)])
        liquid = liquid_material(f"MAT_PhysicalLook_Liquid_{suffix}", liquid_colors[(index - 1) % len(liquid_colors)])
        label = paper_material(f"MAT_PhysicalLook_Label_{suffix}", label_colors[(index - 1) % len(label_colors)])
        cap = cap_material(f"MAT_PhysicalLook_Cap_{suffix}", cap_colors[(index - 1) % len(cap_colors)])
        _replace_material(target, shell)
        liquid_obj = detail_by_name[f"CAUSAL_DETAIL_BottleLiquid_{suffix}"]
        label_obj = detail_by_name[f"CAUSAL_DETAIL_BottleLabel_{suffix}"]
        cap_obj = detail_by_name[f"CAUSAL_DETAIL_BottleCap_{suffix}"]
        _replace_material(liquid_obj, liquid)
        _replace_material(label_obj, label)
        _replace_material(cap_obj, cap)
        com = physical["derivedCenterOfMassHeightMeters"]
        def point(height_meters):
            return target.matrix_world @ Vector((0.0, 0.0, height_meters - com))
        dark, _ = _principled_material(f"MAT_PhysicalLook_GlassEdge_{suffix}", (0.022, 0.035, 0.031), 0.23, coat=0.18)
        stages = (
            ("lip", height * 0.927, body_radius * 0.43, wall_thickness * 0.8, dark),
            ("shoulder_ring", height * 0.802, body_radius * 0.735, wall_thickness * 0.55, dark),
            ("label_top_edge", height * 0.575, body_radius * 1.014, wall_thickness * 0.48, label),
            ("label_bottom_edge", height * 0.325, body_radius * 1.014, wall_thickness * 0.48, label),
            ("liquid_meniscus", physical["visibleLiquidSurfaceHeightMeters"], body_radius * 0.855, wall_thickness * 0.7, liquid),
        )
        for stage, z_value, major, minor, material in stages:
            added.append(_torus(f"PHYSICAL_LOOK_{stage.upper()}_{suffix}", point(z_value), major, minor, material, target, stage))
        target["film_studio_surface_preset"] = "HOUSEHOLD_GLASS_WITH_VISIBLE_FILL_AND_VARIATION"
        target["film_studio_readable_stage_count"] = 10
        records.append({
            "target": target.name,
            "readableStageCount": 10,
            "addedVisualDetailCount": len(stages),
            "wallThicknessMeters": wall_thickness,
            "visibleLiquidSurfaceHeightMeters": physical["visibleLiquidSurfaceHeightMeters"],
            "collisionHullSource": target.name,
            "visualChildrenHaveRigidBody": False,
        })
    return {"preset": "HOUSEHOLD_GLASS_WITH_VISIBLE_FILL_AND_VARIATION", "records": records, "addedObjects": added, "externalAssets": 0}


def _box(name, location, dimensions, material, stage):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = _tag(bpy.context.object, stage)
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(material)
    bevel = obj.modifiers.new("PhysicalLookEdge", "BEVEL")
    bevel.width, bevel.segments = min(dimensions) * 0.08, 2
    return obj


def build_aged_court(scene, floor, floor_parameters, floor_location):
    width = floor_parameters["widthMeters"]
    depth = floor_parameters["depthMeters"]
    thickness = floor_parameters["thicknessMeters"]
    top = floor_location[2] + thickness / 2.0
    _replace_material(floor, wood_material())
    seam_mat, _ = _principled_material("MAT_PhysicalLook_BoardSeam", (0.032, 0.012, 0.004), 0.5)
    paint_mat, _ = _principled_material("MAT_PhysicalLook_CourtPaint", (0.73, 0.64, 0.43), 0.36, coat=0.12)
    wall_mat, wall_shader = _principled_material("MAT_PhysicalLook_AgedWall", (0.065, 0.078, 0.09), 0.67)
    _noise_bump(wall_mat, wall_shader, 4.2, 0.19, 0.006, detail=4.0, roughness=0.74)
    base_mat, _ = _principled_material("MAT_PhysicalLook_Baseboard", (0.025, 0.033, 0.04), 0.42, metallic=0.18)
    cues = []
    for index in range(1, 14):
        y = floor_location[1] - depth / 2.0 + depth * index / 14.0
        cues.append(_box(f"PHYSICAL_LOOK_BOARD_SEAM_{index:02d}", (floor_location[0], y, top + 0.00055), (width * 0.985, 0.0015, 0.001), seam_mat, "board_seam"))
    line_x = floor_location[0] + width * 0.12
    cues.append(_box("PHYSICAL_LOOK_COURT_LINE", (line_x, floor_location[1], top + 0.0008), (0.035, depth * 0.96, 0.0012), paint_mat, "painted_scale_line"))
    back_y = floor_location[1] + depth / 2.0 + 0.075
    wall = _box("PHYSICAL_LOOK_BACK_WALL", (floor_location[0], back_y, 1.22), (width, 0.14, 2.44), wall_mat, "aged_background_wall")
    baseboard = _box("PHYSICAL_LOOK_BASEBOARD", (floor_location[0], back_y - 0.08, 0.105), (width, 0.055, 0.21), base_mat, "baseboard")
    cues.extend((wall, baseboard))
    end_x = floor_location[0] + width / 2.0 + 0.075
    end_wall = _box("PHYSICAL_LOOK_END_WALL", (end_x, floor_location[1], 1.22), (0.14, depth, 2.44), wall_mat, "aged_background_wall")
    end_baseboard = _box("PHYSICAL_LOOK_END_BASEBOARD", (end_x - 0.08, floor_location[1], 0.105), (0.055, depth, 0.21), base_mat, "baseboard")
    cues.extend((end_wall, end_baseboard))
    for index, x in enumerate((-width * 0.34, 0.0, width * 0.34), 1):
        cues.append(_box(f"PHYSICAL_LOOK_WALL_RIB_{index:02d}", (floor_location[0] + x, back_y - 0.09, 1.18), (0.055, 0.045, 2.1), base_mat, "wall_rib"))
    bench_top = _box("PHYSICAL_LOOK_BENCH_TOP", (floor_location[0] + width * 0.31, back_y - 0.38, 0.34), (1.25, 0.28, 0.07), wood_material("MAT_PhysicalLook_BenchWood"), "background_bench")
    bench_leg_a = _box("PHYSICAL_LOOK_BENCH_LEG_A", (bench_top.location.x - 0.46, bench_top.location.y, 0.17), (0.065, 0.22, 0.3), base_mat, "background_bench")
    bench_leg_b = _box("PHYSICAL_LOOK_BENCH_LEG_B", (bench_top.location.x + 0.46, bench_top.location.y, 0.17), (0.065, 0.22, 0.3), base_mat, "background_bench")
    cues.extend((bench_top, bench_leg_a, bench_leg_b))
    scene.world.color = (0.008, 0.012, 0.019)
    if scene.world.use_nodes:
        background = scene.world.node_tree.nodes.get("Background")
        if background:
            background.inputs["Color"].default_value = (0.008, 0.012, 0.019, 1.0)
            background.inputs["Strength"].default_value = 0.12
    floor["film_studio_surface_preset"] = "AGED_MAPLE_COURT_WITH_SCALE_CUES"
    floor["film_studio_environment_scale_cue_count"] = len(cues)
    return {"preset": "AGED_MAPLE_COURT_WITH_SCALE_CUES", "scaleCueCount": len(cues), "objects": cues, "externalAssets": 0}
