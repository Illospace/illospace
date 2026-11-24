"""Check a route with a private tool challenge and a synthetic image."""

import base64
import json
import secrets
import struct
import zlib
from dataclasses import replace
from types import SimpleNamespace

import httpx

from illo.brain.client import BrainClient, BrainEvent, RunImage
from illo.brain.routes import resolve_route
from illo.brain.run_manager import RunManager
from illo.connectors.chatgpt import ChatGPTClient
from illo.db import create_pool
from illo.gateway import InboundEvent
from illo.gateway.principal import smoke_authority
from illo.gateway.replies import RunFailure
from illo.workspace import RouteSettings, RunKind, load_workspace

COLORS = {"red": b"\xff\0\0", "green": b"\0\xff\0", "blue": b"\0\0\xff"}


def solid_image(rgb: bytes) -> RunImage:
    def chunk(name, data):
        return struct.pack("!I", len(data)) + name + data + struct.pack("!I", zlib.crc32(name + data))

    size = 64
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress((b"\0" + rgb * size) * size)) + chunk(b"IEND", b"")
    return RunImage(mime="image/png", data=base64.b64encode(png).decode())


class SmokeCheck:
    system_prompt = (
        "Call smoke_step with n=1. Wait for its result before calling smoke_step with n=2. "
        "Pass the returned token as token in the next call. Repeat for n=3. "
        "Make exactly one tool call per response. Then answer the user's question if there is one. "
        "When there is no question, reply Ready."
    )
    question = "What color is the square in the image? Reply with one word."

    def __init__(self):
        self.answer = secrets.choice(tuple(COLORS))
        self.image = solid_image(COLORS[self.answer])

    def verify(self, item: BrainEvent, has_image: bool) -> None:
        if getattr(item, "smoke_steps", 0) != 3 or item.usage.get("limited"):
            raise RunFailure("provider", detail="smoke_steps")
        if has_image and (
            not getattr(item, "smoke_image_used", False)
            or item.text.strip().lower().rstrip(".") != self.answer
        ):
            raise RunFailure("provider", detail="smoke_image")


async def report(item: BrainEvent) -> None:
    # Model text, tool arguments, and credentials do not belong in this output.
    shown = {"type": item.type}
    if item.type in {"response", "final", "error"}:
        shown["usage"] = item.usage
    if item.type in {"response", "final"}:
        shown["stop"] = getattr(item, "stop", None)
    if item.type == "response":
        shown["fields"] = getattr(item, "fields", [])
    if item.type == "error":
        shown["category"] = item.category
        shown["http_status"] = getattr(item, "http_status", None)
    print(json.dumps(shown))


async def smoke(config, kind: RunKind, provider: str | None, model: str | None,
                person_key: str | None) -> int:
    try:
        workspace = load_workspace(config.workspace_dir)
        overrides = RouteSettings(provider=provider, model=model)
        route = replace(resolve_route(workspace, kind), **overrides.model_dump(exclude_none=True))
        client = BrainClient(config.brain_url)
        providers = await client.providers()
        if route.provider not in providers:
            print("The brain has no provider with that name.")
            return 1
        if providers[route.provider]["wire"] == "fake":
            print("The smoke check needs a model provider.")
            return 1
        async with create_pool(config.database_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                authority = await smoke_authority(conn, kind, person_key)
            if authority is None:
                print("The person key does not name an active person.")
                return 1
            ctx = SimpleNamespace(
                config=config, workspace=workspace, pool=pool, providers=providers,
                brain_client=client, chatgpt_client=ChatGPTClient(), slack_client=None,
            )
            event = InboundEvent(channel="smoke", kind="smoke", external_id="brain-smoke",
                                 text="Run the tool check.")
            await RunManager(ctx).smoke(event, authority, route, SmokeCheck(), report)
    except RunFailure as error:
        print({
            "no_payer": "No model sign-in is available. Use --as if the route uses a person's grant.",
            "smoke_steps": "The model did not complete three dependent tool rounds.",
            "smoke_image": "The model did not answer the image question correctly.",
        }.get(error.detail, str(error)))
        return 1
    except (ValueError, httpx.HTTPError):
        print("The workspace settings or the brain service need a check.")
        return 1
    except Exception:
        print("The smoke run failed.")
        return 1
    print("The smoke check passed.")
    return 0
