# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Minimal typed film workspace state for the F0.3 feasibility gate."""

import bpy

from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Object, Operator, PropertyGroup


SCHEMA_VERSION = "bfs.filmWorkspace.v0.1"


class FilmStudioProjectState(PropertyGroup):
    __slots__ = ()

    identifier: StringProperty(name="Project ID", default="PRJ_REMAINDER")
    name: StringProperty(name="Project", default="Remainder")


class FilmStudioSceneState(PropertyGroup):
    __slots__ = ()

    identifier: StringProperty(name="Scene ID", default="SC01")
    name: StringProperty(name="Scene", default="The Room")


class FilmStudioCharacterState(PropertyGroup):
    __slots__ = ()

    identifier: StringProperty(name="Character ID", default="CHR_GUARDIAN")
    name: StringProperty(name="Character", default="Guardian")


class FilmStudioShotState(PropertyGroup):
    __slots__ = ()

    identifier: StringProperty(name="Shot ID", default="SH010")
    name: StringProperty(name="Shot", default="WIDE")
    camera: PointerProperty(name="Shot Camera", type=Object)


class FilmStudioWorkspaceState(PropertyGroup):
    __slots__ = ()

    schema_version: StringProperty(name="Schema", default=SCHEMA_VERSION)
    project: PointerProperty(type=FilmStudioProjectState)
    story_scene: PointerProperty(type=FilmStudioSceneState)
    character: PointerProperty(type=FilmStudioCharacterState)
    shots: CollectionProperty(type=FilmStudioShotState)
    active_shot_index: IntProperty(name="Active Shot", default=-1, min=-1)
    expert_mode: BoolProperty(name="Expert Mode", default=False)


def active_shot(state):
    index = state.active_shot_index
    if 0 <= index < len(state.shots):
        return state.shots[index]
    return None


def select_shot_camera(context, shot):
    camera = shot.camera
    if camera is None or camera.name not in context.scene.objects:
        return False
    for obj in context.selected_objects:
        obj.select_set(False)
    camera.hide_set(False)
    camera.hide_viewport = False
    camera.select_set(True)
    context.view_layer.objects.active = camera
    context.scene.camera = camera
    return True


class FILMSTUDIO_OT_create_shot(Operator):
    bl_idname = "film_studio.create_shot"
    bl_label = "Create Shot"
    bl_description = "Create and select the typed SH010 WIDE shot and its camera"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        state = context.scene.film_studio
        shot = active_shot(state)
        if shot is None:
            shot = state.shots.add()
            shot.identifier = "SH010"
            shot.name = "WIDE"
            state.active_shot_index = len(state.shots) - 1

        camera = shot.camera
        if camera is None or camera.name not in context.scene.objects:
            camera = context.scene.objects.get("SHOT_SH010_WIDE")
            if camera is None or camera.type != 'CAMERA':
                camera_data = bpy.data.cameras.new("SHOT_SH010_WIDE")
                camera = bpy.data.objects.new("SHOT_SH010_WIDE", camera_data)
                context.scene.collection.objects.link(camera)
            shot.camera = camera

        camera["film_studio_schema"] = SCHEMA_VERSION
        camera["film_studio_kind"] = "Shot"
        camera["film_studio_identifier"] = shot.identifier
        select_shot_camera(context, shot)
        self.report({'INFO'}, "Created and selected SH010 WIDE")
        return {'FINISHED'}


class FILMSTUDIO_OT_set_mode(Operator):
    bl_idname = "film_studio.set_mode"
    bl_label = "Set Film Studio Mode"
    bl_description = "Switch the surface without converting or copying film state"
    bl_options = {'REGISTER'}

    mode: EnumProperty(
        name="Mode",
        items=(
            ('FILM', "Film Mode", "Show the film-native start surface"),
            ('EXPERT', "Expert Mode", "Show the complete Blender workspace"),
        ),
        default='FILM',
        options={'HIDDEN'},
    )

    def execute(self, context):
        context.scene.film_studio.expert_mode = self.mode == 'EXPERT'
        for area in context.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}


classes = (
    FilmStudioProjectState,
    FilmStudioSceneState,
    FilmStudioCharacterState,
    FilmStudioShotState,
    FilmStudioWorkspaceState,
    FILMSTUDIO_OT_create_shot,
    FILMSTUDIO_OT_set_mode,
)


def register():
    bpy.types.Scene.film_studio = PointerProperty(
        type=FilmStudioWorkspaceState,
        name="Film Studio",
        description="Versioned typed Project, Scene, Shot and Character state",
    )


def unregister():
    del bpy.types.Scene.film_studio
