"""Typed provider message/content block adapters.

The agent and provider facades still exchange legacy dict payloads. This
module gives transports a typed validation layer at those boundaries without
rewriting the main agent loop.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class MessageValidationError(ValueError):
    """Raised when a provider message payload cannot be translated safely."""


class _WireEnum(str, Enum):
    """String enum that keeps wire/log rendering as the raw provider value."""

    @classmethod
    def coerce(cls, value: Any, *, field: str) -> "_WireEnum":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        expected = ", ".join(member.value for member in cls)
        raise MessageValidationError(f"Unknown {field}: {value!r}; expected one of: {expected}")

    def __str__(self) -> str:
        return self.value


class Provider(_WireEnum):
    """Supported provider identifiers."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class MessageRole(_WireEnum):
    """Provider message roles shared by Anthropic-style histories."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ContentBlockType(_WireEnum):
    """Typed content block discriminator values used by provider transports."""

    TEXT = "text"
    IMAGE = "image"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


class StopReason(_WireEnum):
    """Known provider stop reasons normalized by transports."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    PAUSE_TURN = "pause_turn"
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"


def normalize_stop_reason(value: Any, *, strict: bool = False) -> StopReason | str:
    """Return a typed stop reason when known, preserving unknown legacy strings."""

    try:
        return StopReason.coerce(value, field="stop reason")  # type: ignore[return-value]
    except MessageValidationError:
        if strict:
            raise
        if value is None:
            return ""
        return str(value)


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _require_str(value: Any, *, field: str, strict: bool) -> str:
    if isinstance(value, str):
        return value
    if not strict and value is not None:
        return str(value)
    raise MessageValidationError(f"{field} must be a string")


def _require_mapping(value: Any, *, field: str, strict: bool) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if not strict:
        return {}
    raise MessageValidationError(f"{field} must be an object")


def _mapping_from_block(block: Any) -> dict[str, Any]:
    if isinstance(block, ContentBlock):
        return block.model_dump(exclude_none=True)
    if isinstance(block, Mapping):
        return dict(block)

    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(exclude_none=True)
        except TypeError:
            dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)

    data: dict[str, Any] = {}
    for field_name in (
        "type",
        "text",
        "thinking",
        "id",
        "name",
        "input",
        "tool_use_id",
        "content",
        "source",
        "is_error",
        "citations",
        "cache_control",
        "signature",
    ):
        value = getattr(block, field_name, None)
        if value is not None:
            data[field_name] = value
    if data:
        return data
    raise MessageValidationError(f"Content block must be a dict/object, got {type(block).__name__}")


@dataclass
class ContentBlock:
    """Compatibility content block with typed discriminator validation."""

    type: ContentBlockType | str
    text: str | None = None
    thinking: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    content: Any | None = None
    source: dict[str, Any] | None = None
    is_error: bool | None = None
    citations: list[Any] | None = None
    cache_control: dict[str, Any] | None = None
    signature: str | None = None

    def __post_init__(self) -> None:
        self.type = ContentBlockType.coerce(self.type, field="content block type")  # type: ignore[assignment]
        if self.text is not None and not isinstance(self.text, str):
            raise MessageValidationError("text block text must be a string")
        if self.thinking is not None and not isinstance(self.thinking, str):
            raise MessageValidationError("thinking block thinking must be a string")
        if self.id is not None and not isinstance(self.id, str):
            raise MessageValidationError("tool_use id must be a string")
        if self.name is not None and not isinstance(self.name, str):
            raise MessageValidationError("tool_use name must be a string")
        if self.input is not None and not isinstance(self.input, dict):
            raise MessageValidationError("tool_use input must be an object")
        if self.tool_use_id is not None and not isinstance(self.tool_use_id, str):
            raise MessageValidationError("tool_result tool_use_id must be a string")
        if self.source is not None and not isinstance(self.source, dict):
            raise MessageValidationError("image source must be an object")
        if self.is_error is not None and not isinstance(self.is_error, bool):
            raise MessageValidationError("tool_result is_error must be a boolean")

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        """Return the legacy provider dict shape."""

        result: dict[str, Any] = {"type": _enum_value(self.type)}
        for key, value in (
            ("text", self.text),
            ("thinking", self.thinking),
            ("id", self.id),
            ("name", self.name),
            ("input", self.input),
            ("tool_use_id", self.tool_use_id),
            ("content", self.content),
            ("source", self.source),
            ("is_error", self.is_error),
            ("citations", self.citations),
            ("cache_control", self.cache_control),
            ("signature", self.signature),
        ):
            if value is None and exclude_none:
                continue
            if value is not None:
                result[key] = value
        return result

    @classmethod
    def from_legacy(cls, block: Any, *, strict: bool = True) -> "ContentBlock":
        """Build a typed wrapper from a legacy dict or SDK content object."""

        if isinstance(block, str):
            return TextContentBlock(block)

        data = _mapping_from_block(block)
        block_type = ContentBlockType.coerce(data.get("type"), field="content block type")
        cache_control = data.get("cache_control")

        if block_type == ContentBlockType.TEXT:
            text = _require_str(data.get("text"), field="text block text", strict=strict)
            citations = data.get("citations")
            if citations is not None and not isinstance(citations, list):
                if strict:
                    raise MessageValidationError("text block citations must be a list")
                citations = None
            return TextContentBlock(text, citations=citations, cache_control=cache_control)

        if block_type == ContentBlockType.IMAGE:
            source = _require_mapping(data.get("source"), field="image source", strict=strict)
            if not source and strict:
                raise MessageValidationError("image source is required")
            return ImageContentBlock(source=source, cache_control=cache_control)

        if block_type == ContentBlockType.THINKING:
            thinking = data.get("thinking", data.get("text"))
            signature = data.get("signature")
            return ThinkingContentBlock(
                _require_str(thinking, field="thinking block thinking", strict=strict),
                signature=signature,
            )

        if block_type == ContentBlockType.TOOL_USE:
            tool_id = _require_str(data.get("id"), field="tool_use id", strict=strict)
            name = _require_str(data.get("name"), field="tool_use name", strict=strict)
            tool_input = _require_mapping(data.get("input"), field="tool_use input", strict=strict)
            return ToolUseContentBlock(
                id=tool_id,
                name=name,
                input=tool_input,
                cache_control=cache_control,
            )

        if block_type == ContentBlockType.TOOL_RESULT:
            tool_use_id = _require_str(
                data.get("tool_use_id"),
                field="tool_result tool_use_id",
                strict=strict,
            )
            return ToolResultContentBlock(
                tool_use_id=tool_use_id,
                content=data.get("content", ""),
                is_error=data.get("is_error"),
                cache_control=cache_control,
            )

        raise MessageValidationError(f"Unsupported content block type: {data.get('type')!r}")


class TextContentBlock(ContentBlock):
    """Typed text content block wrapper."""

    def __init__(
        self,
        text: str,
        *,
        citations: list[Any] | None = None,
        cache_control: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            type=ContentBlockType.TEXT,
            text=text,
            citations=citations,
            cache_control=cache_control,
        )


class ImageContentBlock(ContentBlock):
    """Typed image content block wrapper."""

    def __init__(
        self,
        *,
        source: dict[str, Any],
        cache_control: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            type=ContentBlockType.IMAGE,
            source=source,
            cache_control=cache_control,
        )


class ThinkingContentBlock(ContentBlock):
    """Typed thinking content block wrapper."""

    def __init__(self, thinking: str, *, signature: str | None = None) -> None:
        super().__init__(
            type=ContentBlockType.THINKING,
            thinking=thinking,
            signature=signature,
        )


class ToolUseContentBlock(ContentBlock):
    """Typed tool-use content block wrapper."""

    def __init__(
        self,
        *,
        id: str,
        name: str,
        input: dict[str, Any] | None = None,
        cache_control: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            type=ContentBlockType.TOOL_USE,
            id=id,
            name=name,
            input=input or {},
            cache_control=cache_control,
        )


class ToolResultContentBlock(ContentBlock):
    """Typed tool-result content block wrapper."""

    def __init__(
        self,
        *,
        tool_use_id: str,
        content: Any,
        is_error: bool | None = None,
        cache_control: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            type=ContentBlockType.TOOL_RESULT,
            tool_use_id=tool_use_id,
            content=content,
            is_error=is_error,
            cache_control=cache_control,
        )


@dataclass
class ProviderMessage:
    """Typed wrapper around the legacy `{role, content}` message shape."""

    role: MessageRole | str
    content: str | list[ContentBlock]

    def __post_init__(self) -> None:
        self.role = MessageRole.coerce(self.role, field="message role")  # type: ignore[assignment]
        if isinstance(self.content, list):
            self.content = [
                block if isinstance(block, ContentBlock) else ContentBlock.from_legacy(block)
                for block in self.content
            ]
        elif not isinstance(self.content, str):
            raise MessageValidationError("message content must be a string or content block list")
        _validate_role_content(self)

    @classmethod
    def from_legacy(cls, message: Any, *, strict: bool = True) -> "ProviderMessage":
        if not isinstance(message, Mapping):
            raise MessageValidationError(f"message must be an object, got {type(message).__name__}")
        role = MessageRole.coerce(message.get("role"), field="message role")
        content = message.get("content", "")
        if isinstance(content, str):
            return cls(role=role, content=content)
        if isinstance(content, list):
            blocks = [ContentBlock.from_legacy(block, strict=strict) for block in content]
            return cls(role=role, content=blocks)
        if not strict and content is not None:
            return cls(role=role, content=str(content))
        raise MessageValidationError("message content must be a string or content block list")

    def to_legacy_dict(self) -> dict[str, Any]:
        content: str | list[dict[str, Any]]
        if isinstance(self.content, str):
            content = self.content
        else:
            content = [block.model_dump(exclude_none=True) for block in self.content]
        return {"role": _enum_value(self.role), "content": content}


def _validate_role_content(message: ProviderMessage) -> None:
    if isinstance(message.content, str):
        return

    role = MessageRole.coerce(message.role, field="message role")
    for block in message.content:
        block_type = ContentBlockType.coerce(block.type, field="content block type")
        if role == MessageRole.USER and block_type in {
            ContentBlockType.THINKING,
            ContentBlockType.TOOL_USE,
        }:
            raise MessageValidationError(f"user messages cannot contain {block_type.value} blocks")
        if role == MessageRole.ASSISTANT and block_type == ContentBlockType.TOOL_RESULT:
            raise MessageValidationError("assistant messages cannot contain tool_result blocks")
        if role == MessageRole.SYSTEM and block_type != ContentBlockType.TEXT:
            raise MessageValidationError("system messages can only contain text blocks")


def content_blocks_from_legacy(blocks: Iterable[Any], *, strict: bool = True) -> list[ContentBlock]:
    """Convert legacy block dicts/SDK objects into typed content blocks."""

    return [ContentBlock.from_legacy(block, strict=strict) for block in blocks]


def content_blocks_to_legacy(blocks: Iterable[Any], *, strict: bool = True) -> list[dict[str, Any]]:
    """Convert typed or legacy content blocks back into API-safe dicts."""

    return [
        ContentBlock.from_legacy(block, strict=strict).model_dump(exclude_none=True)
        for block in blocks
    ]


def provider_messages_from_legacy(messages: Iterable[Any], *, strict: bool = True) -> list[ProviderMessage]:
    """Validate and wrap legacy provider messages."""

    return [ProviderMessage.from_legacy(message, strict=strict) for message in messages]


def provider_messages_to_legacy(messages: Iterable[ProviderMessage]) -> list[dict[str, Any]]:
    """Convert typed messages back to legacy dicts."""

    return [message.to_legacy_dict() for message in messages]


def validate_provider_messages(
    messages: Iterable[Any],
    *,
    provider: Provider | str | None = None,
    strict: bool = True,
) -> list[ProviderMessage]:
    """Validate legacy messages for provider translation.

    `provider` is accepted so call sites document the provider boundary; the
    shared Anthropic-style history rules are currently identical.
    """

    if provider is not None:
        Provider.coerce(provider, field="provider")
    return provider_messages_from_legacy(messages, strict=strict)


def validate_system_blocks(system: list[dict[str, Any]] | str | None) -> list[ContentBlock] | str | None:
    """Validate Anthropic-style system blocks while preserving caller payloads."""

    if system is None or isinstance(system, str):
        return system
    if not isinstance(system, list):
        raise MessageValidationError("system must be a string, a list of text blocks, or None")
    blocks = content_blocks_from_legacy(system)
    for block in blocks:
        if block.type != ContentBlockType.TEXT:
            raise MessageValidationError("system blocks must be text blocks")
    return blocks


def validate_tool_definitions(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Validate the minimal shared tool schema used by transport translators."""

    if tools is None:
        return None
    if not isinstance(tools, list):
        raise MessageValidationError("tools must be a list")
    for index, tool in enumerate(tools):
        if not isinstance(tool, Mapping):
            raise MessageValidationError(f"tool {index} must be an object")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise MessageValidationError(f"tool {index} must have a non-empty name")
        input_schema = tool.get("input_schema")
        if input_schema is not None and not isinstance(input_schema, Mapping):
            raise MessageValidationError(f"tool {name!r} input_schema must be an object")
    return tools
