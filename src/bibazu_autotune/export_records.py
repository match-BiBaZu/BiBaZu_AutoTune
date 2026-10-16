"""Structural readers for finished roadmap records; no geometry or path search."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def group_evidence_details(record):
    """Format supplied evidence for display without establishing feasibility."""
    if not record or not record.get("source_group_key"):
        return ""
    witnesses = record.get("witnesses", [])
    models = sorted({w.get("landing_model", "unspecified").replace("_", " ") for w in witnesses})
    outcomes = sorted(
        {
            f"{o['member_id']} (group {o['group_id']})"
            if o.get("retained")
            else f"{o['member_id']} (excluded destination)"
            for w in witnesses
            for o in w.get("possible_outcomes", [])
        }
    )
    errors = [w["endpoint_error_deg"] for w in witnesses if w.get("endpoint_error_deg") is not None]
    lines = [
        "Geometry only; contact feasibility unverified.",
        "Execution requires an assigned profile calibrated for the source group.",
    ]
    if witnesses:
        lines += [
            f"Member witnesses: {len(witnesses)}",
            "Landing evidence: " + ", ".join(models),
        ]
    if errors:
        lines.append(f"Endpoint error: {min(errors):.6g} to {max(errors):.6g} degrees")
    if outcomes:
        lines.append("Possible landing destinations: " + ", ".join(outcomes))
    if record.get("witness_legs"):
        lines.append(
            "Each leg uses its exported member witnesses; intermediate members may differ."
        )
    return "\n".join(lines)


def relocated_path(path, catalogue):
    """Resolve only an exact, hash-preserving entry in the external migration file."""
    source = Path(path).expanduser().resolve()
    root = Path(catalogue).resolve()
    manifest = root / "legacy_catalogue/path_migrations.json"
    if source.is_file() or not manifest.is_file():
        return source
    records = json.loads(manifest.read_text(encoding="utf-8"))["records"]
    for record in records:
        if str(source).casefold() != str(Path(record["old_path"]).resolve()).casefold():
            continue
        target = (root / record["relative_path"]).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("Relocated roadmap asset is missing")
        if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Relocated roadmap asset hash differs; reimport explicitly")
        return target
    return source


def with_consumer_sidecar(payload, path):
    """Read an offline-produced contract for a byte-preserved historical export."""
    if "consumer_export" in payload:
        return payload
    source = Path(path)
    sidecar = source.with_name(source.name + ".consumer.json")
    if not sidecar.is_file():
        return payload
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    if record.get("roadmap_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
        raise ValueError("Consumer sidecar hash differs; upgrade the roadmap offline")
    return dict(payload, consumer_export=record["consumer_export"])


def validate_commands(raw):
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Commands must be a list")
    for command in raw:
        if not isinstance(command, dict) or command.get("axis") not in {"x", "y", "z"}:
            raise ValueError("Invalid command axis")
        if not math.isfinite(float(command["signed_angle_deg"])):
            raise ValueError("Invalid command angle")
        for field in ("start_quaternion_xyzw", "end_quaternion_xyzw"):
            q = command[field]
            if (
                not isinstance(q, (list, tuple))
                or len(q) != 4
                or not all(math.isfinite(float(v)) for v in q)
            ):
                raise ValueError("Invalid command quaternion")
    return tuple(raw)


def validate_transfer(raw, poses):
    if raw.get("source") not in poses or raw.get("target") not in poses:
        raise ValueError("Transfer references unknown pose")
    if raw.get("directed") is not True or raw.get("evidence_status") != "geometry_only":
        raise ValueError("Candidate must be directed and geometry_only")
    return validate_commands(raw["commands"])


def source_coverage(raw, allowed):
    if not isinstance(raw, (list, tuple)) or any(not isinstance(v, str) for v in raw):
        raise ValueError("Source-member coverage must be a list of member IDs")
    if len(set(raw)) != len(raw) or not set(raw).issubset(allowed):
        raise ValueError("Source-member coverage contains duplicate or unknown members")
    return tuple(sorted(raw))


def read_export(payload):
    """Check record shape and references, leaving geometric proofs to the producer."""
    section = payload.get("consumer_export", {})
    if section and section.get("version") not in (1, 2):
        raise ValueError("Unsupported consumer export version; upgrade the roadmap offline")
    states = payload.get("member_states", [])
    if not isinstance(states, list):
        raise ValueError("Member states must be a list")
    groups = {
        int(n.get("node_id", n.get("id"))) for n in payload.get("nodes", payload.get("poses", []))
    }
    members = {s["member_id"]: s for s in states}
    if len(members) != len(states):
        raise ValueError("Duplicate member ID")
    choices = section.get("calibration_choices", [])
    routes = section.get("execution_routes", [])
    if not isinstance(choices, list) or not isinstance(routes, list):
        raise ValueError("Exported choices and routes must be lists")
    # Old files expose their explicit member transfers, without constructing routes.
    if not section:
        choices = [
            dict(
                record,
                source_group_member_ids=sorted(
                    s["member_id"]
                    for s in states
                    if s["retained"] and s["group_id"] == record["source"]
                ),
            )
            for record in payload.get("member_transfers", [])
        ]
    ids = set()
    group_keys = section.get("group_keys", {})
    for record in choices:
        key = record["edge_id"]
        if (
            not key
            or key in ids
            or record["source"] not in groups
            or record["target"] not in groups
        ):
            raise ValueError("Duplicate choice or unknown pose")
        ids.add(key)
        if record.get("directed", True) is not True:
            raise ValueError("Calibration choices must be directed")
        validate_commands(record.get("commands", []))
        if record.get("source_group_key") and (
            section.get("version") != 2
            or record["source_group_key"] != group_keys.get(str(record["source"]))
            or record.get("target_group_key") != group_keys.get(str(record["target"]))
        ):
            raise ValueError("Choice has an unknown source/target group identity")
        for field, group in (
            ("source_member_ids", "source"),
            ("possible_target_member_ids", "target"),
        ):
            for member in record.get(field, []):
                if (
                    member not in members
                    or not members[member]["retained"]
                    or members[member]["group_id"] != record[group]
                ):
                    raise ValueError("Choice references unknown/incompatible member")
    by_id = {record["edge_id"]: record for record in choices}
    route_ids = set()
    for route in routes:
        if section.get("version") == 2 and (
            not route.get("group_semantics")
            or not isinstance(route.get("tie_breaker"), str)
            or not route["tie_breaker"]
        ):
            raise ValueError("Version-two execution requires explicit group semantics and ordering")
        key = route["route_id"]
        if not key or key in route_ids:
            raise ValueError("Duplicate execution route ID")
        route_ids.add(key)
        if route["source"] not in groups or route["target"] not in groups:
            raise ValueError("Route references unknown pose")
        if not route.get("profile_edge_ids") or any(
            key not in ids for key in route["profile_edge_ids"]
        ):
            raise ValueError("Route references unknown calibration choice")
        if [s["edge_id"] for s in route["steps"]] != route["profile_edge_ids"]:
            raise ValueError("Route profile requirements disagree")
        priority = route.get("priority")
        if (
            not isinstance(priority, list)
            or (section.get("version") == 1 and len(priority) != 2)
            or not priority
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in priority)
        ):
            raise ValueError("Invalid exported route priority")
        current = route["source"]
        for step in route["steps"]:
            choice = by_id[step["edge_id"]]
            if (
                step.get("source") != current
                or choice["source"] != current
                or step.get("target") != choice["target"]
            ):
                raise ValueError("Execution route endpoint references disagree")
            if route.get("group_semantics"):
                if (
                    section.get("version") != 2
                    or not choice.get("source_group_key")
                    or (step.get("required_source_group_key") != choice["source_group_key"])
                ):
                    raise ValueError("Execution route group coverage requirements disagree")
                current = choice["target"]
                continue
            for field, group in (
                ("required_source_member_ids", choice["source"]),
                ("possible_target_member_ids", choice["target"]),
            ):
                allowed = {
                    m for m, s in members.items() if s["retained"] and s["group_id"] == group
                }
                values = source_coverage(step.get(field, []), allowed)
                if states and not values:
                    raise ValueError("Member execution route has no explicit coverage requirements")
            current = choice["target"]
        if current != route["target"]:
            raise ValueError("Execution route target references disagree")
    return tuple(states), tuple(choices), tuple(routes)
