# SPDX-FileCopyrightText: 2026 BlenderFilmStudio Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Restricted SceneSpec v0.1 to immutable BuildPlan contract.

This module deliberately has no bpy dependency.  A proposal can request one
typed operation, writing one BuildPlan, and cannot carry Python or scene edits.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from decimal import Decimal
from pathlib import Path


SCENE_SCHEMA_URI = "specs/scene-spec.v0.1.schema.json"
OUTPUT_SPEC_URI = "specs/output-spec.v0.1.json"
TARGET_VERSION = "5.2.0"
COMPILER_VERSION = "0.1.0"
PROPOSAL_VERSION = "bfs.f0.4.sceneSpecCompileProposal.v0.1"
APPROVAL_VERSION = "bfs.f0.4.sceneSpecCompileApproval.v0.1"
APPROVED_OPERATION = "COMPILE_BUILD_PLAN"
APPROVED_SCOPE = ["WRITE_BUILD_PLAN"]


class ContractError(RuntimeError):
    """A fail-closed typed-contract rejection with a stable reason."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def javascript_number(value):
    if not math.isfinite(value):
        raise ContractError("NONFINITE_NUMBER", "JSON contains a non-finite number")
    if value == 0:
        return "0"
    absolute = abs(value)
    source = repr(value).lower()
    if 1e-6 <= absolute < 1e21:
        if "e" in source:
            fixed = format(Decimal(source), "f")
            return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        return source[:-2] if source.endswith(".0") else source
    if "e" not in source:
        source = format(value, ".15e")
        mantissa, exponent = source.split("e")
        mantissa = mantissa.rstrip("0").rstrip(".")
    else:
        mantissa, exponent = source.split("e")
    exponent_value = int(exponent)
    sign = "+" if exponent_value >= 0 else "-"
    return f"{mantissa}e{sign}{abs(exponent_value)}"


def javascript_canonical_json(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return javascript_number(value)
    if isinstance(value, list):
        return "[" + ",".join(javascript_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{javascript_canonical_json(key)}:{javascript_canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise ContractError("UNSUPPORTED_JSON_TYPE", type(value).__name__)


def javascript_pretty_json(value, depth=0):
    if not isinstance(value, (list, dict)):
        return javascript_canonical_json(value)
    indent = "  " * depth
    child_indent = "  " * (depth + 1)
    if isinstance(value, list):
        if not value:
            return "[]"
        body = ",\n".join(child_indent + javascript_pretty_json(item, depth + 1) for item in value)
        return "[\n" + body + "\n" + indent + "]"
    if not value:
        return "{}"
    body = ",\n".join(
        child_indent + javascript_canonical_json(key) + ": " + javascript_pretty_json(value[key], depth + 1)
        for key in sorted(value)
    )
    return "{\n" + body + "\n" + indent + "}"


def canonicalize(value):
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {key: canonicalize(value[key]) for key in sorted(value)}
    return value


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    return sha256_bytes(path.read_bytes())


def _reject_constant(token):
    raise ContractError("NONFINITE_NUMBER", f"Non-finite JSON token {token} is forbidden")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError("INVALID_JSON", str(error)) from error


def _below_existing(root, uri, label):
    normalized = str(uri).replace("\\", "/")
    if not normalized or normalized.startswith("/") or "://" in normalized or ".." in normalized.split("/"):
        raise ContractError("PATH_ESCAPE", f"{label} escapes the repository root")
    candidate = root / normalized
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise ContractError("MISSING_INPUT", f"{label} is missing: {normalized}") from error
    if root not in resolved.parents or Path(os.path.abspath(candidate)) != resolved:
        raise ContractError("PATH_ESCAPE", f"{label} traverses a symbolic link or escapes the repository root")
    return resolved


def _below_fresh_output(root, uri):
    normalized = str(uri).replace("\\", "/")
    if not normalized or normalized.startswith("/") or "://" in normalized or ".." in normalized.split("/"):
        raise ContractError("PATH_ESCAPE", "BuildPlan output escapes the repository root")
    candidate = root / normalized
    try:
        parent = candidate.parent.resolve(strict=True)
    except FileNotFoundError as error:
        raise ContractError("MISSING_OUTPUT_PARENT", "BuildPlan output parent must already exist") from error
    if root not in parent.parents and parent != root:
        raise ContractError("PATH_ESCAPE", "BuildPlan output parent escapes the repository root")
    if Path(os.path.abspath(candidate.parent)) != parent:
        raise ContractError("PATH_ESCAPE", "BuildPlan output parent traverses a symbolic link")
    if candidate.exists() or candidate.is_symlink():
        raise ContractError("OUTPUT_NOT_FRESH", "BuildPlan output already exists")
    return candidate


def _repository_relative(root, path):
    return path.relative_to(root).as_posix()


def _schema_type_matches(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _schema_errors(value, schema, root_schema, path=""):
    errors = []
    if "$ref" in schema:
        target = root_schema
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return _schema_errors(value, target, root_schema, path)
    for item in schema.get("allOf", []):
        errors.extend(_schema_errors(value, item, root_schema, path))
    if "const" in schema and value != schema["const"]:
        errors.append(("SCHEMA_VALIDATION", path, "const mismatch"))
    if "enum" in schema and value not in schema["enum"]:
        errors.append(("SCHEMA_VALIDATION", path, "enum mismatch"))
    expected = schema.get("type")
    if expected and not _schema_type_matches(value, expected):
        return errors + [("SCHEMA_VALIDATION", path, f"expected {expected}")]
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(("SCHEMA_VALIDATION", path + "/" + key, "required property missing"))
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(("SCHEMA_ADDITIONAL_PROPERTY", path + "/" + key, "additional property"))
        for key, child in value.items():
            if key in properties:
                errors.extend(_schema_errors(child, properties[key], root_schema, path + "/" + key))
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            errors.append(("SCHEMA_VALIDATION", path, "array length"))
        if schema.get("uniqueItems"):
            encoded = [javascript_canonical_json(item) for item in value]
            if len(encoded) != len(set(encoded)):
                errors.append(("SCHEMA_VALIDATION", path, "array items must be unique"))
        if "items" in schema:
            for index, child in enumerate(value):
                errors.extend(_schema_errors(child, schema["items"], root_schema, f"{path}/{index}"))
        if "contains" in schema and not any(not _schema_errors(child, schema["contains"], root_schema, path) for child in value):
            errors.append(("SCHEMA_VALIDATION", path, "contains requirement"))
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            errors.append(("SCHEMA_VALIDATION", path, "string length"))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(("SCHEMA_VALIDATION", path, "pattern mismatch"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            errors.append(("NONFINITE_NUMBER", path, "number must be finite"))
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(("SCHEMA_VALIDATION", path, "below minimum"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(("SCHEMA_VALIDATION", path, "above maximum"))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(("SCHEMA_VALIDATION", path, "below exclusive minimum"))
    return errors


def _validate_scene_spec(document, schema):
    errors = _schema_errors(document, schema, schema)
    if errors:
        reason, path, message = errors[0]
        raise ContractError(reason, f"{path or '/'}: {message}")
    if document["shot"]["frameEnd"] < document["shot"]["frameStart"]:
        raise ContractError("SEMANTIC_VALIDATION", "shot frame range is reversed")
    ids = set()
    for collection in ("assets", "actors", "cameras", "lights", "events"):
        for item in document[collection]:
            if item["id"] in ids:
                raise ContractError("SEMANTIC_VALIDATION", f"duplicate id {item['id']}")
            ids.add(item["id"])
    cameras = {camera["id"] for camera in document["cameras"]}
    if document["shot"]["activeCamera"] not in cameras:
        raise ContractError("SEMANTIC_VALIDATION", "active camera is missing")
    for camera in document["cameras"]:
        previous = -1
        for key in camera.get("transformKeys", []):
            if not document["shot"]["frameStart"] <= key["frame"] <= document["shot"]["frameEnd"] or key["frame"] <= previous:
                raise ContractError("SEMANTIC_VALIDATION", "camera keys are out of range or order")
            previous = key["frame"]
    assets = {asset["id"]: asset for asset in document["assets"]}
    for actor in document["actors"]:
        if actor["assetRef"] not in assets or assets[actor["assetRef"]]["kind"] != "CHARACTER":
            raise ContractError("SEMANTIC_VALIDATION", "actor does not reference a CHARACTER asset")
    event_subjects = set(assets) | {actor["id"] for actor in document["actors"]}
    for event in document["events"]:
        if not document["shot"]["frameStart"] <= event["frame"] <= document["shot"]["frameEnd"]:
            raise ContractError("SEMANTIC_VALIDATION", "event frame is outside the shot")
        if any(subject not in event_subjects for subject in event["subjects"]):
            raise ContractError("SEMANTIC_VALIDATION", "event subject is missing")
    for asset in document["assets"]:
        normalized = asset["uri"].replace("\\", "/")
        if normalized.startswith("/") or "://" in normalized or ".." in normalized.split("/"):
            raise ContractError("PATH_ESCAPE", f"asset {asset['id']} escapes the workspace")
        if not any(normalized.startswith(root) for root in document["security"]["allowedAssetRoots"]):
            raise ContractError("PATH_ESCAPE", f"asset {asset['id']} is outside allowed roots")
    output_root = document["render"]["outputRoot"].replace("\\", "/")
    if not output_root.startswith("renders/") or ".." in output_root.split("/"):
        raise ContractError("PATH_ESCAPE", "render output escapes renders/")


def _normalize_document(document):
    normalized = copy.deepcopy(document)
    for key in ("assets", "actors", "lights", "events"):
        normalized[key].sort(key=lambda item: item["id"])
    normalized["cameras"].sort(key=lambda item: item["id"])
    for camera in normalized["cameras"]:
        if "transformKeys" in camera:
            camera["transformKeys"].sort(key=lambda item: item["frame"])
    normalized["render"]["passes"].sort()
    normalized["security"]["allowedAssetRoots"].sort()
    normalized["security"]["allowedOperations"].sort()
    normalized["provenance"]["sources"].sort(key=lambda item: item["uri"])
    return canonicalize(normalized)


def _required_operations(document):
    required = {"SET_RENDER"}
    if document["assets"]:
        required.add("IMPORT_ASSET")
    if document["cameras"]:
        required.add("CREATE_CAMERA")
    if document["lights"]:
        required.add("CREATE_LIGHT")
    if document["assets"] or document["cameras"] or document["lights"]:
        required.add("SET_TRANSFORM")
    missing = required - set(document["security"]["allowedOperations"])
    if missing:
        raise ContractError("OPERATION_NOT_AUTHORIZED", ", ".join(sorted(missing)))
    return sorted(required)


def _verify_assets(root, document):
    verified = []
    for asset in document["assets"]:
        path = _below_existing(root, asset["uri"], f"Asset {asset['id']}")
        digest = sha256_file(path)
        if digest != asset["sha256"]:
            raise ContractError("HASH_MISMATCH", f"Asset {asset['id']} hash mismatch")
        verified.append({**asset, "uri": _repository_relative(root, path), "verifiedSha256": digest})
    return verified


def _verify_sources(root, document):
    verified = []
    for source in document["provenance"]["sources"]:
        if "://" in source["uri"]:
            verified.append({**source, "verification": "DECLARED_EXTERNAL"})
            continue
        path = _below_existing(root, source["uri"], "Provenance source")
        digest = sha256_file(path)
        if digest != source["sha256"]:
            raise ContractError("HASH_MISMATCH", "Provenance source hash mismatch")
        verified.append({**source, "uri": _repository_relative(root, path), "verification": "HASH_VERIFIED"})
    return sorted(verified, key=lambda item: item["uri"])


def _validate_proposal_and_approval(root, proposal_path, approval_path):
    proposal = read_json(proposal_path)
    approval = read_json(approval_path)
    proposal_keys = {"schemaVersion", "proposalId", "decision", "sceneSpec", "requestedOperation", "requestedMutationScope", "requestedOutput", "security", "diff"}
    approval_keys = {"schemaVersion", "approvalId", "decision", "authorizationSource", "authorizedAtUtc", "proposal", "approvedOperation", "approvedMutationScope", "approvedOutput", "security"}
    if set(proposal) != proposal_keys or proposal.get("schemaVersion") != PROPOSAL_VERSION or proposal.get("decision") != "PROPOSE":
        raise ContractError("PROPOSAL_SCHEMA", "Proposal fields or version are not exact")
    if set(approval) != approval_keys or approval.get("schemaVersion") != APPROVAL_VERSION or approval.get("decision") != "APPROVED":
        raise ContractError("APPROVAL_SCHEMA", "Approval fields or version are not exact")
    if set(proposal.get("requestedOutput", {})) != {"uri"} or set(proposal.get("diff", {})) != {"before", "after", "summary"}:
        raise ContractError("PROPOSAL_SCHEMA", "Proposal output or diff fields are not exact")
    if set(approval.get("proposal", {})) != {"uri", "fileSha256"} or set(approval.get("approvedOutput", {})) != {"uri"}:
        raise ContractError("APPROVAL_SCHEMA", "Approval binding or output fields are not exact")
    if not all(isinstance(proposal.get(key), str) and proposal[key] for key in ("proposalId", "requestedOperation")):
        raise ContractError("PROPOSAL_SCHEMA", "Proposal identifiers must be non-empty strings")
    if not all(isinstance(approval.get(key), str) and approval[key] for key in ("approvalId", "authorizationSource", "authorizedAtUtc", "approvedOperation")):
        raise ContractError("APPROVAL_SCHEMA", "Approval identifiers must be non-empty strings")
    proposal_uri = _repository_relative(root, proposal_path)
    if approval.get("proposal") != {"uri": proposal_uri, "fileSha256": sha256_file(proposal_path)}:
        raise ContractError("APPROVAL_BINDING", "Approval does not bind the exact proposal")
    exact_security = {"networkAccess": False, "arbitraryPython": False, "sceneMutation": False}
    if proposal.get("requestedOperation") != APPROVED_OPERATION or proposal.get("requestedMutationScope") != APPROVED_SCOPE:
        raise ContractError("APPROVAL_SCOPE", "Proposal requests an operation outside the bounded scope")
    if approval.get("approvedOperation") != APPROVED_OPERATION or approval.get("approvedMutationScope") != APPROVED_SCOPE:
        raise ContractError("APPROVAL_SCOPE", "Approval scope is not exact")
    if proposal.get("security") != exact_security or approval.get("security") != exact_security:
        raise ContractError("APPROVAL_SCOPE", "Python, network or scene mutation is forbidden")
    if approval.get("approvedOutput") != proposal.get("requestedOutput"):
        raise ContractError("APPROVAL_SCOPE", "Approval output does not equal the proposed output")
    return proposal, approval


def _compile_in_memory(root, proposal):
    scene_record = proposal.get("sceneSpec")
    if not isinstance(scene_record, dict) or set(scene_record) != {"uri", "fileSha256", "canonicalSha256"}:
        raise ContractError("PROPOSAL_SCHEMA", "SceneSpec binding is not exact")
    scene_path = _below_existing(root, scene_record["uri"], "SceneSpec")
    if sha256_file(scene_path) != scene_record["fileSha256"]:
        raise ContractError("HASH_MISMATCH", "SceneSpec file hash mismatch")
    document = read_json(scene_path)
    schema_path = _below_existing(root, SCENE_SCHEMA_URI, "SceneSpec schema")
    schema = read_json(schema_path)
    _validate_scene_spec(document, schema)
    normalized = _normalize_document(document)
    scene_canonical = sha256_bytes(javascript_canonical_json(normalized).encode("utf-8"))
    if scene_canonical != scene_record["canonicalSha256"]:
        raise ContractError("HASH_MISMATCH", "SceneSpec canonical hash mismatch")
    output_spec_path = _below_existing(root, OUTPUT_SPEC_URI, "OutputSpec")
    output_spec = read_json(output_spec_path)
    if output_spec["id"] != normalized["render"]["outputProfile"]:
        raise ContractError("OUTPUT_PROFILE", "OutputSpec id mismatch")
    ocio_path = _below_existing(root, output_spec["color"]["ocioConfigUri"], "OCIO config")
    ocio_sha = sha256_file(ocio_path)
    if ocio_sha != output_spec["color"]["ocioConfigSha256"]:
        raise ContractError("HASH_MISMATCH", "OCIO config hash mismatch")
    assets = _verify_assets(root, normalized)
    sources = _verify_sources(root, normalized)
    render_root = root / normalized["render"]["outputRoot"]
    render_relative = _repository_relative(root, render_root)
    plan = canonicalize({
        "compiler": {"name": "BFS_SCENE_COMPILER", "version": COMPILER_VERSION, "targetApplication": "Blender", "targetVersion": TARGET_VERSION},
        "source": {"sceneSpecPath": _repository_relative(root, scene_path), "sceneSpecVersion": normalized["specVersion"], "canonicalSha256": scene_canonical},
        "shot": normalized["shot"],
        "assets": assets,
        "actors": normalized["actors"],
        "cameras": normalized["cameras"],
        "lights": normalized["lights"],
        "world": normalized["world"],
        "events": normalized["events"],
        "render": normalized["render"],
        "outputSpec": {
            "id": output_spec["id"], "specVersion": output_spec["specVersion"],
            "canonicalSha256": sha256_bytes(javascript_canonical_json(output_spec).encode("utf-8")),
            "picture": output_spec["picture"],
            "color": {**output_spec["color"], "verifiedOcioConfigSha256": ocio_sha},
            "master": output_spec["master"], "acceptance": output_spec["acceptance"],
        },
        "security": {
            "networkAccess": False, "arbitraryPython": False,
            "authorizedOperations": _required_operations(normalized),
            "compilerInternalOperations": ["RESET_SCENE", "SET_WORLD", "SET_ACTIVE_CAMERA", "WRITE_MANIFEST", "SAVE_BLEND"],
        },
        "provenance": {**normalized["provenance"], "sources": sources},
        "outputs": {"root": render_relative, "blend": render_relative + "scene.blend", "manifest": render_relative + "scene.manifest.json"},
    })
    return canonicalize({
        "documentType": "BFS_BUILD_PLAN",
        "planVersion": COMPILER_VERSION,
        "planHash": sha256_bytes(javascript_canonical_json(plan).encode("utf-8")),
        "plan": plan,
    })


def inspect_proposal(repository_root, proposal_uri, approval_uri):
    root = Path(repository_root).resolve(strict=True)
    proposal_path = _below_existing(root, proposal_uri, "Proposal")
    approval_path = _below_existing(root, approval_uri, "Approval")
    proposal, approval = _validate_proposal_and_approval(root, proposal_path, approval_path)
    plan = _compile_in_memory(root, proposal)
    output_uri = proposal["requestedOutput"]["uri"]
    _below_fresh_output(root, output_uri)
    token_body = {"proposalSha256": sha256_file(proposal_path), "approvalSha256": sha256_file(approval_path), "planHash": plan["planHash"]}
    return {
        "status": "APPROVED_READY",
        "proposalId": proposal["proposalId"],
        "diff": proposal["diff"],
        "approvedOperation": approval["approvedOperation"],
        "approvedMutationScope": approval["approvedMutationScope"],
        "outputUri": output_uri,
        "planHash": plan["planHash"],
        "inspectionToken": sha256_bytes(javascript_canonical_json(token_body).encode("utf-8")),
    }


def execute_approved_compile(repository_root, proposal_uri, approval_uri, inspection_token):
    inspection = inspect_proposal(repository_root, proposal_uri, approval_uri)
    if inspection_token != inspection["inspectionToken"]:
        raise ContractError("INSPECTION_REQUIRED", "Proposal and approval must be inspected before execution")
    root = Path(repository_root).resolve(strict=True)
    proposal_path = _below_existing(root, proposal_uri, "Proposal")
    approval_path = _below_existing(root, approval_uri, "Approval")
    proposal, _approval = _validate_proposal_and_approval(root, proposal_path, approval_path)
    plan = _compile_in_memory(root, proposal)
    output_path = _below_fresh_output(root, proposal["requestedOutput"]["uri"])
    payload = (javascript_pretty_json(plan) + "\n").encode("utf-8")
    try:
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as error:
        raise ContractError("OUTPUT_NOT_FRESH", "BuildPlan output appeared after inspection") from error
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {
        **inspection,
        "status": "COMPILED",
        "bytesWritten": len(payload),
        "fileSha256": sha256_bytes(payload),
        "sceneMutations": 0,
        "networkCalls": 0,
        "arbitraryPythonFromProposalExecuted": 0,
    }
