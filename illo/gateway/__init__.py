"""The gateway is the only entry point for channel events."""

from illo.gateway.events import ChannelAdapter, InboundEvent

__all__ = ["Gateway", "InboundEvent", "ChannelAdapter"]


def __getattr__(name):
    # Load the dispatcher on first use. An eager import makes a cycle: the run manager
    # imports illo.gateway.replies, and the dispatcher imports the run manager. A command
    # that imports the run manager first then fails.
    if name == "Gateway":
        from illo.gateway.dispatch import Gateway

        return Gateway
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
