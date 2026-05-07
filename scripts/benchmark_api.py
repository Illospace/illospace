#!/usr/bin/env python3
"""Measure Illo Brain API latency for common UI flows.

The default scenario is intentionally close to the Cortex page:

1. Layout/auth/bootstrap calls.
2. Cortex workspace load calls.
3. Opening one or more idea threads.

Write calls such as mark-read are skipped unless --include-writes is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
SENSITIVE_GET_TEMPLATES = {
    "/api/vault/{key_name}",
    "/api/skills/{skill_id}/assets/{asset_path}",
}
API_METHODS = {"get", "post", "put", "patch", "delete"}


@dataclass(frozen=True)
class RequestSpec:
    name: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    writes: bool = False
    source: str = "manual"


@dataclass
class Sample:
    name: str
    method: str
    path: str
    status: int | None
    elapsed_ms: float
    bytes_read: int
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class SkippedRoute:
    method: str
    path: str
    reason: str


class DiscoveryContext:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.multi_values: dict[str, list[str]] = {}
        self.skipped: list[SkippedRoute] = []

    def set(self, key: str, value: Any) -> None:
        if value is None:
            return
        text = str(value)
        if text:
            self.values[key] = text

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set_many(self, key: str, values: list[Any]) -> None:
        normalized = []
        for value in values:
            if value is None:
                continue
            text = str(value)
            if text and text not in normalized:
                normalized.append(text)
        if normalized:
            self.multi_values[key] = normalized
            self.values.setdefault(key, normalized[0])

    def values_for(self, key: str) -> list[str]:
        if key in self.multi_values:
            return self.multi_values[key]
        value = self.get(key)
        return [value] if value is not None else []


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(samples: list[Sample]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample.name, []).append(sample)

    rows = []
    for name, group in grouped.items():
        timings = [s.elapsed_ms for s in group]
        statuses = sorted({s.status for s in group})
        bytes_read = [s.bytes_read for s in group]
        failures = [s for s in group if not s.ok]
        first = group[0]
        rows.append(
            {
                "name": name,
                "method": first.method,
                "path": first.path,
                "runs": len(group),
                "ok": len(group) - len(failures),
                "fail": len(failures),
                "status": ",".join(str(s) for s in statuses),
                "min_ms": min(timings),
                "p50_ms": statistics.median(timings),
                "avg_ms": statistics.fmean(timings),
                "p95_ms": percentile(timings, 0.95),
                "max_ms": max(timings),
                "avg_kb": statistics.fmean(bytes_read) / 1024 if bytes_read else 0,
                "first_error": failures[0].error if failures else None,
            }
        )

    return sorted(rows, key=lambda row: row["p95_ms"], reverse=True)


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = [
        "name",
        "runs",
        "ok",
        "fail",
        "status",
        "p50",
        "avg",
        "p95",
        "max",
        "kb",
    ]
    widths = {
        "name": max([len("name"), *(len(row["name"]) for row in rows)]),
        "runs": len("runs"),
        "ok": len("ok"),
        "fail": len("fail"),
        "status": max([len("status"), *(len(row["status"]) for row in rows)]),
        "p50": len("p50"),
        "avg": len("avg"),
        "p95": len("p95"),
        "max": len("max"),
        "kb": len("kb"),
    }
    rendered = []
    for row in rows:
        rendered.append(
            {
                "name": row["name"],
                "runs": str(row["runs"]),
                "ok": str(row["ok"]),
                "fail": str(row["fail"]),
                "status": row["status"],
                "p50": f"{row['p50_ms']:.1f}",
                "avg": f"{row['avg_ms']:.1f}",
                "p95": f"{row['p95_ms']:.1f}",
                "max": f"{row['max_ms']:.1f}",
                "kb": f"{row['avg_kb']:.1f}",
            }
        )
    for row in rendered:
        for key, value in row.items():
            widths[key] = max(widths[key], len(value))

    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rendered:
        print("  ".join(row[header].ljust(widths[header]) for header in headers))


def print_skipped(skipped: list[SkippedRoute], *, limit: int = 40) -> None:
    if not skipped:
        return
    print()
    print(f"Skipped {len(skipped)} route(s):")
    for route in skipped[:limit]:
        print(f"  {route.method.upper()} {route.path} - {route.reason}")
    if len(skipped) > limit:
        print(f"  ... {len(skipped) - limit} more")


def openapi_operation_inventory(spec: dict[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for path, path_item in sorted(spec.get("paths", {}).items()):
        for method, operation in sorted(path_item.items()):
            if method.lower() not in API_METHODS or not isinstance(operation, dict):
                continue
            normalized_method = method.upper()
            if normalized_method == "GET" and path not in SENSITIVE_GET_TEMPLATES:
                benchmark_mode = "live_read"
                reason = "covered by --all when path parameters can be sampled"
            elif normalized_method == "GET":
                benchmark_mode = "controlled_read"
                reason = "excluded from generic live reads because it can expose secrets or raw assets"
            elif normalized_method == "POST" and path == "/api/auth/ws-token":
                benchmark_mode = "scenario_write"
                reason = "included only in the manual UI-flow scenario with --include-writes"
            else:
                benchmark_mode = "controlled_mutation"
                reason = "requires disposable fixtures and mocked side effects before timing"
            operations.append(
                {
                    "method": normalized_method,
                    "path": path,
                    "operation_id": operation.get("operationId") or path,
                    "benchmark_mode": benchmark_mode,
                    "reason": reason,
                }
            )
    return operations


def print_inventory(operations: list[dict[str, Any]]) -> None:
    by_method = Counter(row["method"] for row in operations)
    by_mode = Counter(row["benchmark_mode"] for row in operations)
    print(f"OpenAPI operations: {len(operations)}")
    print("By method: " + ", ".join(f"{method}={count}" for method, count in sorted(by_method.items())))
    print("By benchmark mode: " + ", ".join(f"{mode}={count}" for mode, count in sorted(by_mode.items())))
    print()
    print("Use --all to time live_read GETs. controlled_mutation entries need a seeded disposable DB and mocks.")


async def timed_request(client: httpx.AsyncClient, spec: RequestSpec) -> Sample:
    started = time.perf_counter()
    try:
        response = await client.request(spec.method, spec.path, json=spec.body)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Sample(
            name=spec.name,
            method=spec.method,
            path=spec.path,
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            bytes_read=len(response.content),
            ok=response.status_code < 400,
            error=None if response.status_code < 400 else response.text[:240],
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Sample(
            name=spec.name,
            method=spec.method,
            path=spec.path,
            status=None,
            elapsed_ms=elapsed_ms,
            bytes_read=0,
            ok=False,
            error=str(exc),
        )


async def fetch_json(client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    response = await client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def first_item(payload: Any) -> Any:
    if isinstance(payload, list):
        return payload[0] if payload else None
    if isinstance(payload, dict):
        for key in ("items", "results", "data", "memories", "skills", "candidates", "records", "apps"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                return value[0]
    return None


def all_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "results", "data", "memories", "skills", "candidates", "records", "apps"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def first_value(payload: Any, *keys: str) -> Any:
    item = first_item(payload)
    if not isinstance(item, dict):
        return None
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    return None


def add_query(path: str, params: dict[str, Any]) -> str:
    query = httpx.QueryParams({key: value for key, value in params.items() if value is not None})
    if not query:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{query}"


def sample_query_value(name: str, schema: dict[str, Any] | None = None) -> str | int | bool | None:
    lowered = name.lower()
    schema = schema or {}
    if lowered in {"q", "query", "search"}:
        return "test"
    if lowered in {"limit", "per_page", "page_size"}:
        return 50
    if lowered in {"offset", "page"}:
        return 0 if lowered == "offset" else 1
    if lowered in {"status"}:
        return "all"
    if lowered in {"before_seq", "after_seq"}:
        return None
    if lowered.startswith("include_") or schema.get("type") == "boolean":
        return False
    if schema.get("type") in {"integer", "number"}:
        return 1
    return "test"


async def discover_openapi_context(client: httpx.AsyncClient, idea_ids: list[str]) -> DiscoveryContext:
    ctx = DiscoveryContext()
    if idea_ids:
        ctx.set_many("idea_id", idea_ids)

    async def try_get(path: str) -> Any:
        try:
            return await fetch_json(client, "GET", path)
        except Exception:
            return None

    ideas = await try_get("/api/cortex/ideas")
    discovered_idea_ids = [str(item["id"]) for item in all_items(ideas) if isinstance(item, dict) and item.get("id")]
    if discovered_idea_ids and not idea_ids:
        ctx.set_many("idea_id", discovered_idea_ids[:3])
    else:
        ctx.set("idea_id", first_value(ideas, "id") or ctx.get("idea_id"))

    run_id = None
    run_ids: list[str] = []
    if ctx.get("idea_id"):
        history = await try_get(f"/api/cortex/run/history/{ctx.get('idea_id')}")
        run_id = first_value(history, "id")
        run_ids.extend(str(item["id"]) for item in all_items(history) if isinstance(item, dict) and item.get("id") is not None)
    if run_id is None:
        recent = await try_get("/api/cortex/ops/recent")
        run_id = first_value(recent, "run_id") or first_value(recent, "id")
        run_ids.extend(str(item["run_id"] or item["id"]) for item in all_items(recent) if isinstance(item, dict) and (item.get("run_id") is not None or item.get("id") is not None))
    ctx.set("run_id", run_id)
    ctx.set_many("run_id", run_ids[:3])

    candidates = await try_get("/api/agency/candidates")
    ctx.set("candidate_id", first_value(candidates, "id"))

    memories = await try_get("/api/memory/org-memories")
    ctx.set("memory_id", first_value(memories, "id"))
    ctx.set_many("memory_id", [item["id"] for item in all_items(memories)[:3] if isinstance(item, dict) and item.get("id") is not None])

    skills = await try_get("/api/skills/")
    ctx.set("skill_id", first_value(skills, "id"))
    ctx.set("skill_name", first_value(skills, "name"))
    ctx.set_many("skill_id", [item["id"] for item in all_items(skills)[:3] if isinstance(item, dict) and item.get("id") is not None])
    ctx.set_many("skill_name", [item["name"] for item in all_items(skills)[:3] if isinstance(item, dict) and item.get("name")])

    cycles = await try_get("/api/cycles/")
    ctx.set("cycle_id", first_value(cycles, "id"))
    ctx.set_many("cycle_id", [item["id"] for item in all_items(cycles)[:3] if isinstance(item, dict) and item.get("id") is not None])

    domains = await try_get("/api/domains/")
    domain_id = first_value(domains, "id")
    ctx.set("domain_id", domain_id)
    if domain_id is not None:
        records = await try_get(f"/api/domains/{domain_id}/records")
        ctx.set("record_id", first_value(records, "id"))

    apps = await try_get("/api/workspace-apps/")
    ctx.set("app_id", first_value(apps, "id"))
    ctx.set("state_key", "default")

    chat_bootstrap = await try_get("/api/chat/bootstrap")
    if isinstance(chat_bootstrap, dict):
        ctx.set("conversation_id", chat_bootstrap.get("default_conversation_id") or chat_bootstrap.get("room", {}).get("id"))
    if ctx.get("conversation_id"):
        messages = await try_get(f"/api/chat/conversations/{ctx.get('conversation_id')}/messages?limit=50")
        message_id = None
        for message in all_items(messages.get("messages", []) if isinstance(messages, dict) else messages):
            if isinstance(message, dict) and message.get("id") is not None:
                message_id = message["id"]
                break
        ctx.set("message_id", message_id)
        ctx.set("thread_id", message_id)

    traces = await try_get("/api/system/traces/recent")
    ctx.set("trace_id", first_value(traces, "id"))

    return ctx


def resolve_openapi_path(path: str, ctx: DiscoveryContext) -> tuple[str | None, str | None]:
    resolved = path
    while "{" in resolved and "}" in resolved:
        start = resolved.index("{")
        end = resolved.index("}", start)
        name = resolved[start + 1 : end]
        value = ctx.get(name)
        if value is None:
            return None, f"missing sample for {{{name}}}"
        resolved = f"{resolved[:start]}{value}{resolved[end + 1:]}"
    return resolved, None


def resolve_openapi_paths(path: str, ctx: DiscoveryContext) -> tuple[list[str], str | None]:
    names: list[str] = []
    cursor = 0
    while "{" in path[cursor:] and "}" in path[cursor:]:
        start = path.index("{", cursor)
        end = path.index("}", start)
        names.append(path[start + 1 : end])
        cursor = end + 1
    if not names:
        return [path], None

    missing = [name for name in names if not ctx.values_for(name)]
    if missing:
        return [], f"missing sample for {{{missing[0]}}}"

    resolved_paths = [path]
    for name in names:
        next_paths: list[str] = []
        for current in resolved_paths:
            for value in ctx.values_for(name):
                next_paths.append(current.replace(f"{{{name}}}", value))
        resolved_paths = next_paths
    return resolved_paths, None


def openapi_get_specs(spec: dict[str, Any], ctx: DiscoveryContext, *, include_sensitive: bool) -> tuple[list[RequestSpec], list[SkippedRoute]]:
    specs: list[RequestSpec] = []
    skipped: list[SkippedRoute] = []
    for path, operations in sorted(spec.get("paths", {}).items()):
        operation = operations.get("get")
        if not operation:
            continue
        if not include_sensitive and path in SENSITIVE_GET_TEMPLATES:
            skipped.append(SkippedRoute("GET", path, "sensitive or unbounded asset read"))
            continue
        resolved_paths, reason = resolve_openapi_paths(path, ctx)
        if not resolved_paths:
            skipped.append(SkippedRoute("GET", path, reason or "unresolved path params"))
            continue

        query_params: dict[str, Any] = {}
        for param in operation.get("parameters") or []:
            if param.get("in") != "query":
                continue
            required = bool(param.get("required"))
            name = param.get("name")
            if not name or not required:
                continue
            query_params[name] = sample_query_value(name, param.get("schema"))
        operation_id = operation.get("operationId") or path
        for index, resolved in enumerate(resolved_paths, start=1):
            path_with_query = add_query(resolved, query_params)
            name = operation_id if len(resolved_paths) == 1 else f"{operation_id} [{index}]"
            specs.append(RequestSpec(name, "GET", path_with_query, source="openapi"))
    return specs, skipped


async def discover_idea_ids(
    client: httpx.AsyncClient,
    *,
    explicit_idea_ids: list[str],
    limit: int,
) -> list[str]:
    if explicit_idea_ids:
        return explicit_idea_ids[:limit]
    try:
        ideas = await fetch_json(client, "GET", "/api/cortex/ideas")
    except Exception:
        return []
    return [str(idea["id"]) for idea in ideas[:limit] if idea.get("id")]


async def discover_chat_thread_ids(client: httpx.AsyncClient, *, limit: int) -> list[int]:
    try:
        bootstrap = await fetch_json(client, "GET", "/api/chat/bootstrap")
        conversation_id = bootstrap.get("default_conversation_id") or bootstrap.get("room", {}).get("id")
        if not conversation_id:
            return []
        page = await fetch_json(
            client,
            "GET",
            f"/api/chat/conversations/{conversation_id}/messages?limit=50",
        )
        ids = [
            int(message["id"])
            for message in page.get("messages", [])
            if int(message.get("reply_count") or 0) > 0
        ]
        return ids[:limit]
    except Exception:
        return []


def build_specs(
    *,
    idea_ids: list[str],
    chat_thread_ids: list[int],
    include_writes: bool,
) -> list[RequestSpec]:
    specs = [
        RequestSpec("layout: me", "GET", "/api/me"),
        RequestSpec("layout: ws token", "POST", "/api/auth/ws-token", {"tab_id": "api-benchmark"}, writes=True),
        RequestSpec("layout: auth status", "GET", "/api/cortex/auth/status"),
        RequestSpec("chat: bootstrap", "GET", "/api/chat/bootstrap"),
        RequestSpec("notifications: summary", "GET", "/api/notifications/summary"),
        RequestSpec("notifications: unread", "GET", "/api/notifications?status=unread&limit=50"),
        RequestSpec("cortex: ideas", "GET", "/api/cortex/ideas"),
        RequestSpec("cortex: connections", "GET", "/api/cortex/connections"),
        RequestSpec("cortex: team members", "GET", "/api/team/members"),
        RequestSpec("workspace: apps", "GET", "/api/workspace-apps/"),
        RequestSpec("run: status", "GET", "/api/cortex/run/status"),
    ]
    for idea_id in idea_ids:
        short = idea_id[:8]
        specs.extend(
            [
                RequestSpec(f"thread {short}: unified stream", "GET", f"/api/cortex/ideas/{idea_id}/unified-stream"),
                RequestSpec(f"thread {short}: browser session", "GET", f"/api/cortex/ideas/{idea_id}/browser/session"),
                RequestSpec(f"thread {short}: mark read", "POST", f"/api/cortex/ideas/{idea_id}/mark-read", writes=True),
            ]
        )
    for thread_id in chat_thread_ids:
        specs.append(RequestSpec(f"chat thread {thread_id}", "GET", f"/api/chat/messages/{thread_id}/thread?limit=50"))

    if include_writes:
        return specs
    return [spec for spec in specs if not spec.writes]


async def run(args: argparse.Namespace) -> int:
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout, limits=limits) as client:
        if args.inventory:
            spec = await fetch_json(client, "GET", "/api/openapi.json")
            operations = openapi_operation_inventory(spec)
            if args.json:
                print(
                    json.dumps(
                        {
                            "total": len(operations),
                            "by_method": dict(sorted(Counter(row["method"] for row in operations).items())),
                            "by_benchmark_mode": dict(
                                sorted(Counter(row["benchmark_mode"] for row in operations).items())
                            ),
                            "operations": operations,
                        },
                        indent=2,
                    )
                )
            else:
                print_inventory(operations)
            return 0

        idea_ids = await discover_idea_ids(client, explicit_idea_ids=args.idea_id, limit=args.idea_limit)
        skipped: list[SkippedRoute] = []
        if args.all:
            spec = await fetch_json(client, "GET", "/api/openapi.json")
            ctx = await discover_openapi_context(client, idea_ids)
            specs, skipped = openapi_get_specs(spec, ctx, include_sensitive=args.include_sensitive)
        else:
            chat_thread_ids = await discover_chat_thread_ids(client, limit=args.chat_thread_limit)
            specs = build_specs(
                idea_ids=idea_ids,
                chat_thread_ids=chat_thread_ids,
                include_writes=args.include_writes,
            )
        if args.only:
            specs = [spec for spec in specs if args.only.lower() in spec.name.lower() or args.only.lower() in spec.path.lower()]
        if not specs:
            raise SystemExit("No benchmark specs selected.")

        for _ in range(args.warmup):
            await asyncio.gather(*(timed_request(client, spec) for spec in specs[: args.concurrency]))

        samples: list[Sample] = []
        for _ in range(args.runs):
            for start in range(0, len(specs), args.concurrency):
                batch = specs[start : start + args.concurrency]
                samples.extend(await asyncio.gather(*(timed_request(client, spec) for spec in batch)))

    rows = summarize(samples)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_table(rows)
        if args.all:
            print_skipped(skipped)
    return 1 if any(row["fail"] for row in rows) else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--idea-id", action="append", default=[], help="Idea id to benchmark. Repeatable.")
    parser.add_argument("--idea-limit", type=int, default=3)
    parser.add_argument("--chat-thread-limit", type=int, default=2)
    parser.add_argument("--include-writes", action="store_true")
    parser.add_argument("--all", action="store_true", help="Discover and benchmark all safe GET routes from OpenAPI.")
    parser.add_argument("--inventory", action="store_true", help="List every OpenAPI operation and its benchmark mode.")
    parser.add_argument("--include-sensitive", action="store_true", help="Include secret-value and raw asset routes in --all mode.")
    parser.add_argument("--only", default="", help="Substring filter for endpoint name or path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
