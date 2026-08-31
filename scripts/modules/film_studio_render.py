# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded approved render jobs for the Film Studio workspace."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import bpy


SCHEMA_VERSION = "bfs.filmStudioRenderJob.v0.1"
RESTART_SCHEMA_VERSION = "bfs.filmStudioRenderJob.v0.2"
STAGE_RECEIPT_SCHEMA = "bfs.filmStudioRenderStageReceipt.v0.1"
FAILURE_RECEIPT_SCHEMA = "bfs.filmStudioRenderFailureReceipt.v0.1"
RESUME_DECISION_SCHEMA = "bfs.filmStudioResumeDecisionReceipt.v0.1"

STAGE_ARTIFACT_BUDGETS = {
    "PREVIEW": 16 * 1024 * 1024,
    "FINAL": 64 * 1024 * 1024,
}

FROZEN_PROFILES = {
    "PREVIEW": {
        "engine": "BLENDER_EEVEE",
        "frame": 1,
        "resolution": [640, 360, 100],
        "samples": 16,
        "format": "PNG",
        "colorMode": "RGBA",
        "colorDepth": "8",
        "output": "preview/preview.png",
    },
    "FINAL": {
        "engine": "CYCLES",
        "device": "CPU",
        "frame": 1,
        "resolution": [640, 360, 100],
        "samples": 32,
        "seed": 24082601,
        "animatedSeed": False,
        "denoising": False,
        "threadsMode": "FIXED",
        "threads": 8,
        "format": "OPEN_EXR_MULTILAYER",
        "colorDepth": "16",
        "codec": "ZIP",
        "requiredPasses": ["Combined", "Depth", "Normal"],
        "output": "final/final.exr",
    },
}


class RenderContractError(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require(value, reason, message):
    if not value:
        raise RenderContractError(reason, message)


def read_json(path, reason):
    require(path.is_file() and not path.is_symlink(), reason, f"Missing exact JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RenderContractError(reason, f"Invalid JSON file: {path}") from error
    require(isinstance(value, dict), reason, f"JSON root must be an object: {path}")
    return value


def valid_self_hash(value, field):
    observed = value.get(field)
    if not isinstance(observed, str) or len(observed) != 64:
        return False
    body = dict(value)
    del body[field]
    return sha256_bytes(canonical(body)) == observed


def resolved_inside(root, relative, reason):
    require(isinstance(relative, str) and relative, reason, "Relative path is absent")
    candidate = Path(relative)
    require(not candidate.is_absolute(), reason, "Absolute output path is forbidden")
    require(".." not in candidate.parts, reason, "Parent traversal is forbidden")
    resolved_root = root.resolve(strict=True)
    resolved = (resolved_root / candidate).resolve(strict=False)
    require(resolved != resolved_root and resolved_root in resolved.parents, reason, "Path escapes evidence root")
    return resolved


def _manifest_path(repository_root, manifest_uri):
    root = Path(repository_root).resolve(strict=True)
    path = resolved_inside(root, manifest_uri, "MANIFEST_PATH_OUT_OF_SCOPE")
    require(path.is_file(), "MANIFEST_MISSING", "Render manifest is absent")
    return root, path


def _utc_timestamp(value, reason):
    require(isinstance(value, str) and value.endswith("Z"), reason, "UTC timestamp must end in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RenderContractError(reason, "UTC timestamp is invalid") from error
    require(parsed.tzinfo is not None, reason, "UTC timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _validate_job_control(manifest, observed_at=None):
    control = manifest.get("jobControl")
    require(isinstance(control, dict), "JOB_CONTROL_MISSING", "Restart-safe job control is absent")
    require(control.get("stageOrder") == ["PREVIEW", "FINAL"], "STAGE_ORDER_MISMATCH", "Immutable stage order differs")
    require(control.get("completedStagesImmutable") is True, "RESUME_POLICY_MISMATCH", "Completed stages are not immutable")
    require(control.get("completeResumeIsNoOp") is True, "RESUME_POLICY_MISMATCH", "Complete resume is not a no-op")
    maximum_calls = control.get("maximumRenderCalls")
    maximum_bytes = control.get("maximumArtifactBytes")
    require(isinstance(maximum_calls, int) and not isinstance(maximum_calls, bool) and 0 <= maximum_calls <= 2, "RENDER_BUDGET_INVALID", "Render-call budget is invalid")
    require(isinstance(maximum_bytes, int) and not isinstance(maximum_bytes, bool) and 0 <= maximum_bytes <= 80 * 1024 * 1024, "ARTIFACT_BUDGET_INVALID", "Artifact-byte budget is invalid")
    not_before = _utc_timestamp(control.get("notBefore"), "VALIDITY_WINDOW_INVALID")
    valid_until = _utc_timestamp(control.get("validUntil"), "VALIDITY_WINDOW_INVALID")
    require(not_before < valid_until, "VALIDITY_WINDOW_INVALID", "Job validity window is empty")
    now = observed_at or datetime.now(timezone.utc)
    require(now >= not_before, "JOB_NOT_YET_VALID", "Render job is not yet valid")
    require(now <= valid_until, "JOB_EXPIRED", "Render job authorization expired")
    return control


def inspect_job(repository_root, manifest_uri, evidence_root, source_blend=None):
    root, manifest_path = _manifest_path(repository_root, manifest_uri)
    manifest = read_json(manifest_path, "MANIFEST_INVALID")
    schema_version = manifest.get("schemaVersion")
    require(schema_version in {SCHEMA_VERSION, RESTART_SCHEMA_VERSION}, "SCHEMA_MISMATCH", "Render job schema differs")
    require(manifest.get("status") == "APPROVED", "JOB_NOT_APPROVED", "Render job is not approved")
    require(valid_self_hash(manifest, "manifestHash"), "MANIFEST_HASH_INVALID", "Render manifest self hash differs")
    authority = manifest.get("authority", {})
    require(authority == {
        "approvedStages": ["PREVIEW", "FINAL"],
        "modelMayGeneratePython": False,
        "networkAllowed": False,
        "sourceBlendMayBeSaved": False,
        "outputScope": "AUTHORIZED_EVIDENCE_ROOT_ONLY",
    }, "AUTHORITY_MISMATCH", "Render authority differs")
    require(manifest.get("profiles") == FROZEN_PROFILES, "PROFILE_MISMATCH", "Frozen render profiles differ")
    if schema_version == RESTART_SCHEMA_VERSION:
        _validate_job_control(manifest)

    evidence = Path(evidence_root).resolve(strict=True)
    require(str(evidence) == manifest.get("authorizedEvidenceRoot"), "EVIDENCE_ROOT_MISMATCH", "Evidence root differs")
    source = Path(source_blend or bpy.data.filepath).resolve(strict=True)
    source_record = manifest.get("source", {})
    require(str(source) == source_record.get("absolutePath"), "SOURCE_PATH_MISMATCH", "Source .blend path differs")
    require(sha256_file(source) == source_record.get("sha256"), "SOURCE_HASH_MISMATCH", "Source .blend SHA-256 differs")
    require(source_record.get("planHash") and source_record.get("semanticStructureSha256"), "SOURCE_IDENTITY_MISSING", "Source semantic identity is absent")
    require(manifest.get("jobId") and manifest.get("approvalId"), "APPROVAL_MISSING", "Job or approval identity is absent")
    for profile in manifest["profiles"].values():
        resolved_inside(evidence, profile["output"], "OUTPUT_PATH_OUT_OF_SCOPE")

    token_body = {
        "manifestHash": manifest["manifestHash"],
        "sourceSha256": source_record["sha256"],
        "evidenceRoot": str(evidence),
        "approvedStages": authority["approvedStages"],
    }
    preview_receipt_path = evidence / "preview" / "receipt.json"
    final_receipt_path = evidence / "final" / "receipt.json"
    if preview_receipt_path.exists():
        preview_receipt = _verify_stage_receipt(evidence, manifest, "PREVIEW")
        preview_status = "PASS"
        final_status = "READY"
        last_receipt_hash = preview_receipt["receiptHash"]
    else:
        preview_status = "READY"
        final_status = "BLOCKED: PREVIEW_REQUIRED"
        last_receipt_hash = ""
    if final_receipt_path.exists():
        require(preview_receipt_path.exists(), "FINAL_RECEIPT_INVALID", "Final receipt exists without Preview")
        final_receipt = _verify_stage_receipt(evidence, manifest, "FINAL")
        final_status = "PASS"
        last_receipt_hash = final_receipt["receiptHash"]
    return {
        "status": "APPROVED_READY",
        "jobId": manifest["jobId"],
        "approvalId": manifest["approvalId"],
        "manifestHash": manifest["manifestHash"],
        "sourceSha256": source_record["sha256"],
        "previewStatus": preview_status,
        "finalStatus": final_status,
        "lastReceiptHash": last_receipt_hash,
        "restartable": schema_version == RESTART_SCHEMA_VERSION,
        "inspectionToken": sha256_bytes(canonical(token_body)),
        "manifest": manifest,
        "manifestPath": str(manifest_path.relative_to(root)),
    }


def _verify_stage_receipt(evidence, manifest, stage):
    reason = f"{stage}_RECEIPT_INVALID"
    receipt_path = evidence / stage.lower() / "receipt.json"
    receipt = read_json(receipt_path, f"{stage}_RECEIPT_MISSING")
    require(valid_self_hash(receipt, "receiptHash"), reason, f"{stage.title()} receipt self hash differs")
    require(receipt.get("schemaVersion") == STAGE_RECEIPT_SCHEMA, reason, f"{stage.title()} receipt schema differs")
    require(receipt.get("jobId") == manifest["jobId"] and receipt.get("approvalId") == manifest["approvalId"] and receipt.get("manifestHash") == manifest["manifestHash"], reason, f"{stage.title()} receipt job identity differs")
    require(receipt.get("stage") == stage and receipt.get("status") == "PASS", reason, f"{stage.title()} receipt stage differs")
    require(receipt.get("profile") == FROZEN_PROFILES[stage], reason, f"{stage.title()} receipt profile differs")
    require(receipt.get("process", {}).get("renderCalls") == 1, reason, f"{stage.title()} render count differs")
    output_record = receipt.get("output", {})
    require(output_record.get("uri") == FROZEN_PROFILES[stage]["output"], reason, f"{stage.title()} output URI differs")
    output = resolved_inside(evidence, output_record["uri"], f"{stage}_ARTIFACT_INVALID")
    require(output.is_file() and not output.is_symlink(), f"{stage}_ARTIFACT_INVALID", f"{stage.title()} artifact is absent")
    require(output.stat().st_size == output_record.get("bytes") and sha256_file(output) == output_record.get("sha256"), f"{stage}_ARTIFACT_INVALID", f"{stage.title()} artifact binding differs")
    return receipt


def _verify_preview_receipt(evidence, manifest):
    return _verify_stage_receipt(evidence, manifest, "PREVIEW")


def plan_resume(repository_root, manifest_uri, evidence_root):
    inspected = inspect_job(repository_root, manifest_uri, evidence_root)
    require(inspected["restartable"], "JOB_NOT_RESTARTABLE", "Render job has no restart-safe contract")
    manifest = inspected["manifest"]
    control = _validate_job_control(manifest)
    evidence = Path(evidence_root).resolve(strict=True)
    completed = []
    receipts = []
    for stage in control["stageOrder"]:
        receipt_path = evidence / stage.lower() / "receipt.json"
        if not receipt_path.exists():
            break
        if stage == "FINAL":
            require(completed == ["PREVIEW"], "FINAL_RECEIPT_INVALID", "Final cannot precede Preview")
        receipt = _verify_stage_receipt(evidence, manifest, stage)
        completed.append(stage)
        receipts.append(receipt)
    require(not (evidence / "final" / "receipt.json").exists() or completed == ["PREVIEW", "FINAL"], "FINAL_RECEIPT_INVALID", "Final stage chain differs")
    spent_calls = sum(receipt["process"]["renderCalls"] for receipt in receipts)
    spent_bytes = sum(receipt["output"]["bytes"] for receipt in receipts)
    require(spent_calls <= control["maximumRenderCalls"], "RENDER_BUDGET_EXCEEDED", "Completed stages exceed render-call budget")
    require(spent_bytes <= control["maximumArtifactBytes"], "ARTIFACT_BUDGET_EXCEEDED", "Completed artifacts exceed byte budget")
    next_stage = control["stageOrder"][len(completed)] if len(completed) < len(control["stageOrder"]) else "COMPLETE"
    if next_stage != "COMPLETE":
        require(spent_calls + 1 <= control["maximumRenderCalls"], "RENDER_BUDGET_EXHAUSTED", "No render-call budget remains")
        require(spent_bytes + STAGE_ARTIFACT_BUDGETS[next_stage] <= control["maximumArtifactBytes"], "ARTIFACT_BUDGET_EXHAUSTED", "No artifact-byte budget remains")
    token_body = {
        "manifestHash": manifest["manifestHash"],
        "completedStages": completed,
        "completedReceiptHashes": [receipt["receiptHash"] for receipt in receipts],
        "nextStage": next_stage,
        "spentRenderCalls": spent_calls,
        "spentArtifactBytes": spent_bytes,
    }
    return {
        "status": "COMPLETE" if next_stage == "COMPLETE" else "RESUME_READY",
        "jobId": manifest["jobId"],
        "manifestHash": manifest["manifestHash"],
        "completedStages": completed,
        "completedReceiptHashes": token_body["completedReceiptHashes"],
        "nextStage": next_stage,
        "spentRenderCalls": spent_calls,
        "spentArtifactBytes": spent_bytes,
        "remainingRenderCalls": control["maximumRenderCalls"] - spent_calls,
        "remainingArtifactBytes": control["maximumArtifactBytes"] - spent_bytes,
        "resumeToken": sha256_bytes(canonical(token_body)),
        "manifest": manifest,
    }


def _apply_resume_state(plan, decision_hash=""):
    state = getattr(bpy.context.scene, "film_studio", None)
    if state is None:
        return
    state.render_resume_status = plan["status"]
    state.render_next_stage = plan["nextStage"]
    state.render_completed_stages = ", ".join(plan["completedStages"]) or "NONE"
    state.render_last_decision_hash = decision_hash


def _write_resume_decision(evidence, plan_before, plan_after, executed_stage, stage_receipt):
    sequence = len(plan_before["completedStages"]) + 1
    label = executed_stage.lower() if executed_stage != "COMPLETE" else "complete"
    body = {
        "schemaVersion": RESUME_DECISION_SCHEMA,
        "status": "PASS",
        "jobId": plan_before["jobId"],
        "manifestHash": plan_before["manifestHash"],
        "sequence": sequence,
        "completedStagesBefore": plan_before["completedStages"],
        "immutableSkippedStages": plan_before["completedStages"],
        "executedStage": executed_stage,
        "completedStagesAfter": plan_after["completedStages"],
        "nextStageAfter": plan_after["nextStage"],
        "renderCallsThisDecision": 0 if executed_stage == "COMPLETE" else 1,
        "stageReceiptHash": stage_receipt.get("receiptHash") if stage_receipt else None,
        "resumeTokenBefore": plan_before["resumeToken"],
        "resumeTokenAfter": plan_after["resumeToken"],
        "process": {"pid": os.getpid(), "mouseInteractions": 0, "networkCalls": 0},
    }
    return exclusive_receipt(
        evidence / "job-control" / f"{sequence:02d}-{label}.json",
        body,
        "decisionHash",
    )


def execute_next_stage(repository_root, manifest_uri, evidence_root):
    evidence = Path(evidence_root).resolve(strict=True)
    plan_before = plan_resume(repository_root, manifest_uri, evidence)
    executed_stage = plan_before["nextStage"]
    if executed_stage == "COMPLETE":
        stage_receipt = None
        plan_after = plan_before
    else:
        stage_receipt = execute_stage(repository_root, manifest_uri, evidence, executed_stage)
        plan_after = plan_resume(repository_root, manifest_uri, evidence)
    decision = _write_resume_decision(evidence, plan_before, plan_after, executed_stage, stage_receipt)
    _apply_resume_state(plan_after, decision["decisionHash"])
    return {
        "status": plan_after["status"],
        "executedStage": executed_stage,
        "nextStage": plan_after["nextStage"],
        "completedStages": plan_after["completedStages"],
        "renderCalls": 0 if executed_stage == "COMPLETE" else 1,
        "stageReceiptHash": stage_receipt.get("receiptHash") if stage_receipt else None,
        "decisionHash": decision["decisionHash"],
    }


def plan_resume_with_failure_receipt(repository_root, manifest_uri, evidence_root, failure_name):
    evidence = Path(evidence_root).resolve(strict=True)
    source = Path(bpy.data.filepath).resolve(strict=True)
    source_before = sha256_file(source)
    before_entries = sorted(path.relative_to(evidence).as_posix() for path in evidence.rglob("*") if path.is_file())
    try:
        return plan_resume(repository_root, manifest_uri, evidence)
    except RenderContractError as error:
        body = {
            "schemaVersion": FAILURE_RECEIPT_SCHEMA,
            "status": "REJECTED",
            "reason": error.reason,
            "message": str(error),
            "stage": "RESUME",
            "manifestUri": manifest_uri,
            "process": {"pid": os.getpid(), "renderCalls": 0, "mouseInteractions": 0, "networkCalls": 0},
            "source": {"sha256BeforeAndAfter": source_before, "unchanged": sha256_file(source) == source_before},
            "preexistingEvidenceFiles": before_entries,
            "newRenderArtifactsWritten": 0,
        }
        exclusive_receipt(evidence / "failures" / f"{failure_name}.json", body, "failureHash")
        raise


def _configure_scene(stage, output):
    profile = FROZEN_PROFILES[stage]
    scene = bpy.context.scene
    scene.frame_set(profile["frame"])
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = profile["resolution"]
    scene.render.use_file_extension = True
    scene.render.film_transparent = False
    scene.render.filepath = str(output)
    scene.render.image_settings.color_mode = "RGBA"
    if stage == "PREVIEW":
        scene.render.engine = "BLENDER_EEVEE"
        scene.render.image_settings.media_type = "IMAGE"
        scene.eevee.taa_render_samples = profile["samples"]
        scene.render.image_settings.file_format = profile["format"]
        scene.render.image_settings.color_depth = profile["colorDepth"]
    else:
        scene.render.engine = "CYCLES"
        scene.render.image_settings.media_type = "MULTI_LAYER_IMAGE"
        scene.cycles.device = profile["device"]
        scene.cycles.samples = profile["samples"]
        scene.cycles.seed = profile["seed"]
        scene.cycles.use_animated_seed = profile["animatedSeed"]
        scene.cycles.use_denoising = profile["denoising"]
        scene.render.threads_mode = profile["threadsMode"]
        scene.render.threads = profile["threads"]
        scene.render.image_settings.file_format = profile["format"]
        scene.render.image_settings.color_depth = profile["colorDepth"]
        scene.render.image_settings.exr_codec = profile["codec"]
        bpy.context.view_layer.use_pass_z = True
        bpy.context.view_layer.use_pass_normal = True
    return profile


def exclusive_receipt(path, body, hash_field):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.exists() and not path.is_symlink(), "RECEIPT_PATH_EXISTS", f"Receipt path is not fresh: {path}")
    payload = dict(body)
    payload[hash_field] = sha256_bytes(canonical(body))
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def execute_stage(repository_root, manifest_uri, evidence_root, stage):
    stage = stage.upper()
    require(stage in FROZEN_PROFILES, "STAGE_NOT_APPROVED", "Only PREVIEW or FINAL is approved")
    inspected = inspect_job(repository_root, manifest_uri, evidence_root)
    manifest = inspected["manifest"]
    evidence = Path(evidence_root).resolve(strict=True)
    source = Path(bpy.data.filepath).resolve(strict=True)
    source_before = sha256_file(source)
    if stage == "FINAL":
        _verify_preview_receipt(evidence, manifest)
    output = resolved_inside(evidence, FROZEN_PROFILES[stage]["output"], "OUTPUT_PATH_OUT_OF_SCOPE")
    require(not output.exists() and not output.is_symlink(), "OUTPUT_PATH_EXISTS", "Render output is not fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    profile = _configure_scene(stage, output)
    started = time.perf_counter()
    result = bpy.ops.render.render(write_still=True)
    render_seconds = time.perf_counter() - started
    require("FINISHED" in result, "RENDER_FAILED", f"Render operator failed: {sorted(result)}")
    require(output.is_file() and output.stat().st_size > 0, "OUTPUT_MISSING", "Render output is absent")
    require(sha256_file(source) == source_before, "SOURCE_CHANGED", "Source .blend changed during render")
    require(math.isfinite(render_seconds) and render_seconds > 0, "TIMING_INVALID", "Render duration is invalid")
    body = {
        "schemaVersion": STAGE_RECEIPT_SCHEMA,
        "status": "PASS",
        "jobId": manifest["jobId"],
        "approvalId": manifest["approvalId"],
        "stage": stage,
        "manifestHash": manifest["manifestHash"],
        "inspectionToken": inspected["inspectionToken"],
        "process": {"pid": os.getpid(), "renderCalls": 1, "mouseInteractions": 0, "networkCalls": 0},
        "timing": {"renderSeconds": render_seconds},
        "source": {"sha256BeforeAndAfter": source_before, "planHash": manifest["source"]["planHash"], "semanticStructureSha256": manifest["source"]["semanticStructureSha256"]},
        "profile": profile,
        "output": {"uri": output.relative_to(evidence).as_posix(), "bytes": output.stat().st_size, "sha256": sha256_file(output)},
        "product": {"version": bpy.app.version_string, "buildHash": bpy.app.build_hash.decode() if isinstance(bpy.app.build_hash, bytes) else str(bpy.app.build_hash)},
    }
    receipt = exclusive_receipt(evidence / stage.lower() / "receipt.json", body, "receiptHash")
    state = getattr(bpy.context.scene, "film_studio", None)
    if state is not None:
        state.render_status = f"{stage}_PASS"
        state.render_preview_status = "PASS"
        state.render_final_status = "PASS" if stage == "FINAL" else "READY"
        state.render_last_receipt_hash = receipt["receiptHash"]
    return receipt


def execute_stage_with_failure_receipt(repository_root, manifest_uri, evidence_root, stage, failure_name="failure"):
    evidence = Path(evidence_root).resolve(strict=True)
    source = Path(bpy.data.filepath).resolve(strict=True)
    source_before = sha256_file(source)
    try:
        return execute_stage(repository_root, manifest_uri, evidence, stage)
    except RenderContractError as error:
        body = {
            "schemaVersion": FAILURE_RECEIPT_SCHEMA,
            "status": "REJECTED",
            "reason": error.reason,
            "message": str(error),
            "stage": stage.upper(),
            "manifestUri": manifest_uri,
            "process": {"pid": os.getpid(), "renderCalls": 0, "mouseInteractions": 0, "networkCalls": 0},
            "source": {"sha256BeforeAndAfter": source_before, "unchanged": sha256_file(source) == source_before},
        }
        exclusive_receipt(evidence / "failures" / f"{failure_name}.json", body, "failureHash")
        raise
