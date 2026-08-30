# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Typed film workspace and bounded SceneSpec contract bridge."""

import bpy
import film_studio_contract

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
    contract_repository_root: StringProperty(name="Repository Root", subtype='DIR_PATH')
    contract_proposal_uri: StringProperty(name="Proposal URI")
    contract_approval_uri: StringProperty(name="Approval URI")
    contract_status: StringProperty(name="Contract Status", default="NOT_INSPECTED")
    contract_proposal_id: StringProperty(name="Proposal ID")
    contract_diff_summary: StringProperty(name="Proposal Diff")
    contract_approval_scope: StringProperty(name="Approval Scope")
    contract_output_uri: StringProperty(name="Approved Output")
    contract_plan_hash: StringProperty(name="Plan Hash")
    contract_inspection_token: StringProperty(name="Inspection Token", options={'HIDDEN'})


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


class FILMSTUDIO_OT_inspect_contract(Operator):
    bl_idname = "film_studio.inspect_contract"
    bl_label = "Inspect Proposal and Approval"
    bl_description = "Validate and display the typed proposal diff and exact approval scope without writing or changing the scene"
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.film_studio
        state.contract_inspection_token = ""
        try:
            result = film_studio_contract.inspect_proposal(
                bpy.path.abspath(state.contract_repository_root),
                state.contract_proposal_uri,
                state.contract_approval_uri,
            )
        except film_studio_contract.ContractError as error:
            state.contract_status = f"REJECTED: {error.reason}"
            state.contract_proposal_id = ""
            state.contract_diff_summary = ""
            state.contract_approval_scope = ""
            state.contract_output_uri = ""
            state.contract_plan_hash = ""
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.contract_status = result["status"]
        state.contract_proposal_id = result["proposalId"]
        state.contract_diff_summary = "none -> one immutable BuildPlan"
        state.contract_approval_scope = f"{result['approvedOperation']} / {', '.join(result['approvedMutationScope'])} only"
        state.contract_output_uri = result["outputUri"]
        state.contract_plan_hash = result["planHash"]
        state.contract_inspection_token = result["inspectionToken"]
        self.report({'INFO'}, "Proposal diff and approval scope inspected; no scene mutation")
        return {'FINISHED'}


class FILMSTUDIO_OT_execute_contract(Operator):
    bl_idname = "film_studio.execute_contract"
    bl_label = "Execute Approved Compile"
    bl_description = "Write only the approved immutable BuildPlan after a successful inspection"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        state = getattr(context.scene, "film_studio", None)
        return bool(state and state.contract_status == "APPROVED_READY" and state.contract_inspection_token)

    def execute(self, context):
        state = context.scene.film_studio
        try:
            result = film_studio_contract.execute_approved_compile(
                bpy.path.abspath(state.contract_repository_root),
                state.contract_proposal_uri,
                state.contract_approval_uri,
                state.contract_inspection_token,
            )
        except film_studio_contract.ContractError as error:
            state.contract_status = f"REJECTED: {error.reason}"
            state.contract_inspection_token = ""
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.contract_status = result["status"]
        state.contract_inspection_token = ""
        self.report({'INFO'}, f"BuildPlan written: {result['fileSha256'][:12]}")
        return {'FINISHED'}


classes = (
    FilmStudioProjectState,
    FilmStudioSceneState,
    FilmStudioCharacterState,
    FilmStudioShotState,
    FilmStudioWorkspaceState,
    FILMSTUDIO_OT_create_shot,
    FILMSTUDIO_OT_set_mode,
    FILMSTUDIO_OT_inspect_contract,
    FILMSTUDIO_OT_execute_contract,
)


def register():
    bpy.types.Scene.film_studio = PointerProperty(
        type=FilmStudioWorkspaceState,
        name="Film Studio",
        description="Versioned typed Project, Scene, Shot and Character state",
    )


def unregister():
    del bpy.types.Scene.film_studio
