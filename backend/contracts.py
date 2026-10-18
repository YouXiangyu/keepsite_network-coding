"""Shared HTTP and backup validation contracts for account business data."""
from datetime import date as Date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["movie", "travel", "daily", "exhibition"]

Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")]

Tags = Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=30)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CollectionFields(Model):
    title: Annotated[str, Field(min_length=1, max_length=70)]
    category: Category
    tags: Tags | None = None
    note: Annotated[str, Field(max_length=500)] = ""
    date: str
    art: Annotated[str, Field(min_length=1, max_length=80)]

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        if Date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期应为 YYYY-MM-DD。")
        return value


class CreateCollection(CollectionFields):
    id: Identifier
    assetId: Identifier


class UpdateCollection(Model):
    version: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=70)]
    category: Category
    tags: Tags
    note: Annotated[str, Field(max_length=500)]
    art: Annotated[str, Field(min_length=1, max_length=80)]
    date: str | None = None
    assetId: Identifier | None = None

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        if value is not None and Date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期应为 YYYY-MM-DD。")
        return value


class Version(Model):
    version: Annotated[int, Field(ge=1)]


class Profile(Model):
    name: Annotated[str, Field(min_length=1, max_length=24)]
    bio: Annotated[str, Field(max_length=100)] = ""


class Like(Model):
    liked: bool
