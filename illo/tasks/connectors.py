"""Register task wake types and changes from outside systems."""

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class WakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = "none"


class WebhookConfig(WakeConfig):
    type: str = "webhook"
    url: HttpUrl
    secret: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def secure_url(cls, value):
        if value.scheme != "https" or value.username or value.password:
            raise ValueError("Use an HTTPS address without credentials")
        return value


@dataclass
class WakeResult:
    ok: bool
    text: str
    links: list = field(default_factory=list)
    retry: bool = True
    guidance: str | None = None


wake_types = {}
handover_checks = {}
link_watchers = {}
signal_handlers = {}
lane_handlers = []


def register_wake(name, model, function, *, handover=None):
    if name in wake_types:
        raise ValueError("The wake type is already present.")
    wake_types[name] = (model, function)
    if handover:
        handover_checks[name] = handover


def validate_handover(workspace, holder, links):
    if not holder or not holder.startswith("agent:"):
        return
    agent = next((a for a in workspace.agents if a.key == holder[6:]), None)
    if agent and (check := handover_checks.get(agent.wake.type)):
        check(agent, links)


def register_links(kind, function):
    link_watchers.setdefault(kind, []).append(function)


def register_signal(name, function):
    signal_handlers[name] = function


def register_lane(function):
    if function not in lane_handlers:
        lane_handlers.append(function)


async def signal(name, ctx, conn, **values):
    if name in signal_handlers:
        await signal_handlers[name](ctx, conn, **values)


async def lane(ctx, conn, row, task_id=None, token=None):
    for function in lane_handlers:
        await function(ctx, conn, row, task_id=task_id, token=token)


async def changed(ctx, conn, kind, **values):
    for function in link_watchers.get(kind, []):
        await function(ctx, conn, **values)


async def passive(ctx, conn, task, agent, reason, event):
    return WakeResult(True, "The holder starts on its own clock." if agent.wake.type == "poll"
                      else "A person starts the holder.")


register_wake("none", WakeConfig, passive)
register_wake("poll", WakeConfig, passive)
# Import the transport only when this type runs.
async def webhook(ctx, conn, task, agent, reason, event):
    from illo.tasks.wake import send_webhook

    return await send_webhook(ctx, conn, task, agent, reason, event)


register_wake("webhook", WebhookConfig, webhook)
