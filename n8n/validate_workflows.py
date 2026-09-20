#!/usr/bin/env python3
"""Validate n8n workflow JSON files structurally.

Checks: valid JSON, required node fields, known node types/versions, connections
reference existing node names, webhook paths unique, IF conditions well-formed,
HTTP nodes have method+url, retry flags are top-level node fields.
Exit 0 = valid, 1 = problems found.
"""
import json
import sys
import uuid

KNOWN_TYPES = {
    "n8n-nodes-base.webhook": {2},
    "n8n-nodes-base.code": {2},
    "n8n-nodes-base.if": {2, 2.1, 2.2},
    "n8n-nodes-base.httpRequest": {4.2},
    "n8n-nodes-base.errorTrigger": {1},
}

errors = []


def err(path, msg):
    errors.append(f"{path}: {msg}")


def check_workflow(path):
    with open(path) as f:
        try:
            wf = json.load(f)
        except json.JSONDecodeError as e:
            err(path, f"invalid JSON: {e}")
            return
    if not wf.get("name"):
        err(path, "missing workflow name")
    nodes = wf.get("nodes", [])
    if not nodes:
        err(path, "no nodes")
    names = set()
    ids = set()
    for i, n in enumerate(nodes):
        p = f"{path} node[{i}]"
        for field in ("id", "name", "type", "typeVersion", "position", "parameters"):
            if field not in n:
                err(p, f"missing field '{field}'")
        try:
            uuid.UUID(str(n.get("id", "")))
        except ValueError:
            err(p, f"id is not a UUID: {n.get('id')}")
        if n.get("name") in names:
            err(p, f"duplicate node name: {n.get('name')}")
        names.add(n.get("name"))
        ids.add(n.get("id"))
        t, tv = n.get("type"), n.get("typeVersion")
        if t in KNOWN_TYPES and tv not in KNOWN_TYPES[t]:
            err(p, f"unexpected typeVersion {tv} for {t}")
        elif t not in KNOWN_TYPES:
            err(p, f"unknown node type {t} (allowed: {sorted(KNOWN_TYPES)})")
        pos = n.get("position")
        if not (isinstance(pos, list) and len(pos) == 2):
            err(p, "position must be [x, y]")
        for flag in ("retryOnFail", "maxTries", "waitBetweenTries", "onError"):
            if flag in n.get("parameters", {}):
                err(p, f"'{flag}' must be a top-level node field, not inside parameters")
        if t == "n8n-nodes-base.webhook" and not n["parameters"].get("path"):
            err(p, "webhook node missing parameters.path")
        if t == "n8n-nodes-base.if":
            conds = n["parameters"].get("conditions", {}).get("conditions", [])
            if not conds:
                err(p, "IF node has no conditions")
            for c in conds:
                op = c.get("operator", {})
                if not all(k in c for k in ("leftValue", "rightValue", "operator")):
                    err(p, "IF condition missing leftValue/rightValue/operator")
                if not all(k in op for k in ("type", "operation", "name")):
                    err(p, "IF operator missing type/operation/name")
        if t == "n8n-nodes-base.httpRequest":
            prm = n["parameters"]
            if not prm.get("url"):
                err(p, "HTTP node missing url")
            if not prm.get("method"):
                err(p, "HTTP node missing method")
            if prm.get("sendBody") and not prm.get("specifyBody"):
                err(p, "HTTP node sendBody=true but no specifyBody")
    # connections
    conns = wf.get("connections", {})
    for src, groups in conns.items():
        if src not in names:
            err(path, f"connection source '{src}' is not a node")
        for out_idx, out_list in enumerate(groups.get("main", [])):
            for c in out_list or []:
                if c.get("node") not in names:
                    err(path, f"connection {src}[{out_idx}] -> unknown node '{c.get('node')}'")
    # webhook paths unique
    paths = [n["parameters"]["path"] for n in nodes
             if n.get("type") == "n8n-nodes-base.webhook"]
    if len(paths) != len(set(paths)):
        err(path, f"duplicate webhook paths: {paths}")
    # every non-trigger node should be reachable (warn-level: just check isolated)
    targets = {c.get("node") for groups in conns.values()
               for out in groups.get("main", []) for c in (out or [])}
    triggers = {"n8n-nodes-base.webhook", "n8n-nodes-base.errorTrigger"}
    for n in nodes:
        if n["type"] not in triggers and n["name"] not in targets:
            err(path, f"node '{n['name']}' is not reachable from any connection")


def main():
    for path in sys.argv[1:]:
        check_workflow(path)
    if errors:
        print("WORKFLOW VALIDATION FAILED:")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    print(f"OK: {len(sys.argv) - 1} workflow file(s) valid")


if __name__ == "__main__":
    main()
