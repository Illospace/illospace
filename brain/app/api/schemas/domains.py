from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


FieldType = Literal[
    "text",
    "long_text",
    "number",
    "boolean",
    "date",
    "datetime",
    "enum",
    "multi_enum",
    "url",
    "email",
    "phone",
    "money",
    "user_ref",
    "record_ref",
    "json",
]
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
RemovalMode = Literal["archive", "delete"]


class DomainFieldCreate(BaseModel):
    key: str
    name: str | None = None
    field_type: FieldType = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)
    default_value: Any | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    searchable: bool = True
    sortable: bool = True


class DomainObjectCreate(BaseModel):
    key: str
    name: str | None = None
    description: str | None = None
    title_field: str | None = None
    fields: list[DomainFieldCreate] = Field(default_factory=list)


class DomainRelationTypeCreate(BaseModel):
    key: str
    name: str | None = None
    description: str | None = None
    source_object: str
    target_object: str
    cardinality: Cardinality = "many_to_many"


class DomainCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    slug: str | None = Field(default=None, max_length=80)
    description: str | None = None
    objects: list[DomainObjectCreate] = Field(default_factory=list)
    relations: list[DomainRelationTypeCreate] = Field(default_factory=list)


class DomainObjectAdd(BaseModel):
    key: str
    name: str | None = None
    description: str | None = None
    title_field: str | None = None
    fields: list[DomainFieldCreate] = Field(default_factory=list)


class DomainFieldAdd(DomainFieldCreate):
    pass


class DomainRelationTypeAdd(DomainRelationTypeCreate):
    pass


class DomainRecordCreate(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None


class DomainRecordUpdate(BaseModel):
    data_patch: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    expected_version: int | None = None


class DomainRelationCreate(BaseModel):
    relation_key: str
    source_record_id: int
    target_record_id: int
    properties: dict[str, Any] = Field(default_factory=dict)


class DomainRemove(BaseModel):
    mode: RemovalMode = "archive"


class DomainRemoveRecord(BaseModel):
    mode: RemovalMode = "archive"


class DomainSummaryRead(BaseModel):
    id: int
    org_id: str
    slug: str
    name: str
    description: str | None = None
    object_count: int
    has_records: bool
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DomainFieldRead(BaseModel):
    id: int
    domain_id: int
    object_type_id: int
    key: str
    name: str
    field_type: str
    required: bool
    options: list[Any]
    default_value: Any | None = None
    validation: dict[str, Any]
    searchable: bool
    sortable: bool
    created_at: datetime
    updated_at: datetime


class DomainObjectRead(BaseModel):
    id: int
    domain_id: int
    key: str
    name: str
    description: str | None = None
    title_field: str | None = None
    sort_order: int
    fields: list[DomainFieldRead]
    created_at: datetime
    updated_at: datetime


class DomainRelationTypeRead(BaseModel):
    id: int
    domain_id: int
    key: str
    name: str
    description: str | None = None
    source_object: str | None = None
    target_object: str | None = None
    source_object_type_id: int
    target_object_type_id: int
    cardinality: str
    created_at: datetime
    updated_at: datetime


class DomainSchemaRead(DomainSummaryRead):
    objects: list[DomainObjectRead]
    relation_types: list[DomainRelationTypeRead]


class DomainRecordRead(BaseModel):
    id: int
    org_id: str
    domain_id: int
    object_type_id: int
    object_key: str | None = None
    title: str
    data: dict[str, Any]
    version: int
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DomainRelationRead(BaseModel):
    id: int
    org_id: str
    domain_id: int
    relation_type_id: int
    relation_key: str | None = None
    source_record_id: int
    target_record_id: int
    properties: dict[str, Any]
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DomainEventRead(BaseModel):
    id: int
    org_id: str
    domain_id: int
    record_id: int | None = None
    relation_id: int | None = None
    event_type: str
    actor_kind: str
    actor_id: str | None = None
    run_id: int | None = None
    idea_id: str | None = None
    before: dict[str, Any]
    after: dict[str, Any]
    patch: dict[str, Any]
    reason: str | None = None
    created_at: datetime
