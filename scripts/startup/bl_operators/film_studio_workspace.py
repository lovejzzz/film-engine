# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Typed film workspace and bounded SceneSpec contract bridge."""

import bpy
import film_studio_causal
import film_studio_contract
import film_studio_physical_light
import film_studio_physical_performance
import film_studio_render

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
    causal_repository_root: StringProperty(name="Repository Root", subtype='DIR_PATH')
    causal_scene_spec_uri: StringProperty(name="Causal SceneSpec URI")
    causal_status: StringProperty(name="Physics Status", default="NOT_INSPECTED")
    causal_scene_id: StringProperty(name="Causal Scene ID")
    causal_scene_spec_hash: StringProperty(name="Causal SceneSpec Hash")
    causal_actor_factory: StringProperty(name="Actor Factory")
    causal_target_factory: StringProperty(name="Target Factory")
    causal_target_count: IntProperty(name="Target Count", default=0, min=0, max=16)
    causal_collision_shapes: StringProperty(name="Collision Shapes")
    causal_final_pose_source: StringProperty(name="Final Pose Source")
    causal_camera_fit_source: StringProperty(name="Camera Fit Source")
    causal_result_summary: StringProperty(name="Physics Result")
    causal_inspection_token: StringProperty(name="Physics Inspection Token", options={'HIDDEN'})
    render_repository_root: StringProperty(name="Repository Root", subtype='DIR_PATH')
    render_manifest_uri: StringProperty(name="Render Manifest URI")
    render_evidence_root: StringProperty(name="Evidence Root", subtype='DIR_PATH')
    render_status: StringProperty(name="Render Status", default="NOT_INSPECTED")
    render_job_id: StringProperty(name="Render Job ID")
    render_approval_id: StringProperty(name="Render Approval ID")
    render_manifest_hash: StringProperty(name="Manifest Hash")
    render_preview_status: StringProperty(name="Preview Status", default="NOT_INSPECTED")
    render_final_status: StringProperty(name="Final Status", default="NOT_INSPECTED")
    render_last_receipt_hash: StringProperty(name="Last Receipt Hash")
    render_resume_status: StringProperty(name="Resume Status", default="NOT_INSPECTED")
    render_next_stage: StringProperty(name="Next Stage", default="UNKNOWN")
    render_completed_stages: StringProperty(name="Completed Stages", default="NONE")
    render_last_decision_hash: StringProperty(name="Last Decision Hash")
    render_inspection_token: StringProperty(name="Render Inspection Token", options={'HIDDEN'})
    slice_repository_root: StringProperty(name="Repository Root", subtype='DIR_PATH')
    slice_manifest_uri: StringProperty(name="Vertical Slice Manifest URI")
    slice_evidence_root: StringProperty(name="Evidence Root", subtype='DIR_PATH')
    slice_status: StringProperty(name="Slice Status", default="NOT_INSPECTED")
    slice_id: StringProperty(name="Slice ID")
    slice_manifest_hash: StringProperty(name="Manifest Hash")
    slice_shared_identity: StringProperty(name="Shared Identity")
    slice_historical_boundary: StringProperty(name="Frame 288 Boundary")
    slice_current_shot: StringProperty(name="Current Shot", default="UNKNOWN")
    slice_completed_frames: IntProperty(name="Completed Frames", default=0, min=0, max=288)
    slice_last_receipt_hash: StringProperty(name="Last Receipt Hash")
    slice_inspection_token: StringProperty(name="Slice Inspection Token", options={'HIDDEN'})


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


class FILMSTUDIO_OT_inspect_causal_scene(Operator):
    bl_idname = "film_studio.inspect_causal_scene"
    bl_label = "Inspect Physical Scene"
    bl_description = "Validate allowlisted factories, initial conditions and Bullet-only final-pose authority without changing the scene"
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.film_studio
        state.causal_inspection_token = ""
        state.causal_result_summary = ""
        try:
            root = bpy.path.abspath(state.causal_repository_root)
            if film_studio_physical_light.matches_physical_light(root, state.causal_scene_spec_uri):
                result = film_studio_physical_light.inspect_physical_light(
                    root,
                    state.causal_scene_spec_uri,
                )
            elif film_studio_physical_performance.matches_physical_performance(root, state.causal_scene_spec_uri):
                result = film_studio_physical_performance.inspect_physical_performance(
                    root,
                    state.causal_scene_spec_uri,
                    context.scene,
                )
            else:
                result = film_studio_causal.inspect_causal_scene(root, state.causal_scene_spec_uri)
        except (film_studio_causal.CausalContractError, film_studio_physical_light.PhysicalLightError, film_studio_physical_performance.PhysicalPerformanceError) as error:
            state.causal_status = f"REJECTED: {error.reason}"
            state.causal_scene_id = ""
            state.causal_scene_spec_hash = ""
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.causal_status = result["status"]
        state.causal_scene_id = result["sceneId"]
        state.causal_scene_spec_hash = result["sceneSpecHash"]
        state.causal_actor_factory = result["actorFactory"]
        state.causal_target_factory = result["targetFactory"]
        state.causal_target_count = result["targetCount"]
        state.causal_collision_shapes = " / ".join(result["collisionShapes"])
        state.causal_final_pose_source = result["finalPoseSource"]
        state.causal_camera_fit_source = result["cameraFitSource"]
        state.causal_inspection_token = result["inspectionToken"]
        self.report({'INFO'}, "Physical scene inspected; scene mutations: 0")
        return {'FINISHED'}


class FILMSTUDIO_OT_execute_causal_scene(Operator):
    bl_idname = "film_studio.execute_causal_scene"
    bl_label = "Build with Real Physics"
    bl_description = "Build the inspected causal scene, release it to Bullet and frame the evaluated result"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        state = getattr(context.scene, "film_studio", None)
        return bool(state and state.causal_status == "APPROVED_READY" and state.causal_inspection_token)

    def execute(self, context):
        state = context.scene.film_studio
        try:
            root = bpy.path.abspath(state.causal_repository_root)
            if film_studio_physical_light.matches_physical_light(root, state.causal_scene_spec_uri):
                result = film_studio_physical_light.execute_physical_light(
                    root,
                    state.causal_scene_spec_uri,
                    state.causal_inspection_token,
                    context.scene,
                )
            elif film_studio_physical_performance.matches_physical_performance(root, state.causal_scene_spec_uri):
                result = film_studio_physical_performance.execute_physical_performance(
                    root,
                    state.causal_scene_spec_uri,
                    state.causal_inspection_token,
                    context.scene,
                )
            else:
                result = film_studio_causal.execute_causal_scene(
                    root,
                    state.causal_scene_spec_uri,
                    state.causal_inspection_token,
                    context.scene,
                )
        except (film_studio_causal.CausalContractError, film_studio_physical_light.PhysicalLightError, film_studio_physical_performance.PhysicalPerformanceError) as error:
            state.causal_status = f"REJECTED: {error.reason}"
            state.causal_inspection_token = ""
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.causal_status = result["status"]
        state.causal_inspection_token = ""
        if result.get("schemaVersion") == "bfs.physicalLightResult.v0.1":
            physics = result["physics"]
            state.causal_result_summary = f"Contact {physics['contactFrame']}; gate {physics['peakShutterOpenDegrees']:.1f} deg; reveal geometry-owned"
        elif "mechanism" in result:
            physics = result["physics"]
            state.causal_result_summary = f"Contact {physics['contactFrame']}; peak {physics['peakDisplacementMeters']:.3f} m; settled {physics['settledWindowStartFrame']}"
        else:
            response = result["physics"]["targetResponseFrames"]
            tilts = result["physics"]["finalTiltDegrees"]
            state.causal_result_summary = f"Responses {list(response.values())}; final tilts {[round(value, 1) for value in tilts.values()]}"
        self.report({'INFO'}, "Physical scene built; final poses are Bullet evaluated")
        return {'FINISHED'}


class FILMSTUDIO_OT_inspect_render_job(Operator):
    bl_idname = "film_studio.inspect_render_job"
    bl_label = "Inspect Approved Render Job"
    bl_description = "Validate the approved render manifest, source and bounded evidence root without rendering"
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.film_studio
        state.render_inspection_token = ""
        state.render_preview_status = "NOT_INSPECTED"
        state.render_final_status = "NOT_INSPECTED"
        state.render_last_receipt_hash = ""
        state.render_resume_status = "NOT_INSPECTED"
        state.render_next_stage = "UNKNOWN"
        state.render_completed_stages = "NONE"
        state.render_last_decision_hash = ""
        try:
            result = film_studio_render.inspect_job(
                bpy.path.abspath(state.render_repository_root),
                state.render_manifest_uri,
                bpy.path.abspath(state.render_evidence_root),
            )
        except film_studio_render.RenderContractError as error:
            state.render_status = f"REJECTED: {error.reason}"
            state.render_job_id = ""
            state.render_approval_id = ""
            state.render_manifest_hash = ""
            state.render_resume_status = f"REJECTED: {error.reason}"
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.render_status = result["status"]
        state.render_job_id = result["jobId"]
        state.render_approval_id = result["approvalId"]
        state.render_manifest_hash = result["manifestHash"]
        state.render_preview_status = result["previewStatus"]
        state.render_final_status = result["finalStatus"]
        state.render_last_receipt_hash = result["lastReceiptHash"]
        state.render_inspection_token = result["inspectionToken"]
        if result["restartable"]:
            try:
                resume = film_studio_render.plan_resume(
                    bpy.path.abspath(state.render_repository_root),
                    state.render_manifest_uri,
                    bpy.path.abspath(state.render_evidence_root),
                )
            except film_studio_render.RenderContractError as error:
                state.render_resume_status = f"REJECTED: {error.reason}"
                self.report({'ERROR'}, f"{error.reason}: {error}")
                return {'CANCELLED'}
            state.render_resume_status = resume["status"]
            state.render_next_stage = resume["nextStage"]
            state.render_completed_stages = ", ".join(resume["completedStages"]) or "NONE"
        else:
            state.render_resume_status = "NOT_AVAILABLE"
        self.report({'INFO'}, "Approved render job inspected; render calls: 0")
        return {'FINISHED'}


class FILMSTUDIO_OT_resume_render_job(Operator):
    bl_idname = "film_studio.resume_render_job"
    bl_label = "Resume Next Approved Stage"
    bl_description = "Verify immutable completed stages and execute only the first unfinished approved render stage"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        state = getattr(context.scene, "film_studio", None)
        return bool(
            state
            and state.render_repository_root
            and state.render_manifest_uri
            and state.render_evidence_root
            and not state.render_resume_status.startswith("REJECTED")
        )

    def execute(self, context):
        state = context.scene.film_studio
        try:
            result = film_studio_render.execute_next_stage(
                bpy.path.abspath(state.render_repository_root),
                state.render_manifest_uri,
                bpy.path.abspath(state.render_evidence_root),
            )
        except film_studio_render.RenderContractError as error:
            state.render_resume_status = f"REJECTED: {error.reason}"
            state.render_next_stage = "BLOCKED"
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.render_resume_status = result["status"]
        state.render_next_stage = result["nextStage"]
        state.render_completed_stages = ", ".join(result["completedStages"]) or "NONE"
        state.render_last_decision_hash = result["decisionHash"]
        self.report({'INFO'}, f"Executed {result['executedStage']}; next: {result['nextStage']}")
        return {'FINISHED'}


class FILMSTUDIO_OT_inspect_vertical_slice(Operator):
    bl_idname = "film_studio.inspect_vertical_slice"
    bl_label = "Inspect B62 Three-Shot Slice"
    bl_description = "Validate the exact B62 scene, shared non-camera identity and retained frame-288 rejection without rendering"
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.film_studio
        state.slice_inspection_token = ""
        try:
            result = film_studio_render.inspect_vertical_slice(
                bpy.path.abspath(state.slice_repository_root),
                state.slice_manifest_uri,
                bpy.path.abspath(state.slice_evidence_root),
            )
        except film_studio_render.RenderContractError as error:
            state.slice_status = f"REJECTED: {error.reason}"
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.slice_status = result["status"]
        state.slice_id = result["sliceId"]
        state.slice_manifest_hash = result["manifestHash"]
        state.slice_shared_identity = result["sharedStateHash"]
        state.slice_historical_boundary = result["historicalBoundary"]
        state.slice_current_shot = result["currentShot"]
        state.slice_completed_frames = result["completedFrames"]
        state.slice_last_receipt_hash = result["lastReceiptHash"]
        state.slice_inspection_token = result["inspectionToken"]
        self.report({'INFO'}, "B62 three-shot slice inspected; render calls: 0")
        return {'FINISHED'}


class FILMSTUDIO_OT_build_vertical_slice_review(Operator):
    bl_idname = "film_studio.build_vertical_slice_review"
    bl_label = "Build B62 Review Animatic"
    bl_description = "Render the approved 96/96/96-frame B62 review slice while preserving the historical frame-288 rejection"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        state = getattr(context.scene, "film_studio", None)
        return bool(state and state.slice_status == "APPROVED_READY" and state.slice_inspection_token)

    def execute(self, context):
        state = context.scene.film_studio
        try:
            receipt = film_studio_render.execute_vertical_slice(
                bpy.path.abspath(state.slice_repository_root),
                state.slice_manifest_uri,
                bpy.path.abspath(state.slice_evidence_root),
                state.slice_inspection_token,
            )
        except film_studio_render.RenderContractError as error:
            state.slice_status = f"REJECTED: {error.reason}"
            self.report({'ERROR'}, f"{error.reason}: {error}")
            return {'CANCELLED'}
        state.slice_status = "PASS_REVIEW_READY"
        state.slice_current_shot = "COMPLETE"
        state.slice_completed_frames = 288
        state.slice_last_receipt_hash = receipt["receiptHash"]
        state.slice_inspection_token = ""
        self.report({'INFO'}, "B62 three-shot review frames complete; human review remains pending")
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
    FILMSTUDIO_OT_inspect_causal_scene,
    FILMSTUDIO_OT_execute_causal_scene,
    FILMSTUDIO_OT_inspect_render_job,
    FILMSTUDIO_OT_resume_render_job,
    FILMSTUDIO_OT_inspect_vertical_slice,
    FILMSTUDIO_OT_build_vertical_slice_review,
)


def register():
    bpy.types.Scene.film_studio = PointerProperty(
        type=FilmStudioWorkspaceState,
        name="Film Studio",
        description="Versioned typed Project, Scene, Shot and Character state",
    )


def unregister():
    del bpy.types.Scene.film_studio
