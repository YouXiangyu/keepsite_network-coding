"""Movie ticket request and backup field contracts."""
from datetime import datetime
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FIELD_NAMES = ("title", "cinema", "hall", "seat", "showtime")
Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")]
AssetId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Text = Annotated[str, Field(max_length=200)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TicketFields(Model):
    title: Annotated[str, Field(max_length=120)] = ""
    cinema: Text = ""
    hall: Text = ""
    seat: Text = ""
    showtime: str = ""

    @field_validator("showtime")
    @classmethod
    def valid_showtime(cls, value):
        if value and (len(value) != 16 or datetime.strptime(value, "%Y-%m-%dT%H:%M").isoformat(timespec="minutes") != value):
            raise ValueError("放映时间应为 YYYY-MM-DDTHH:mm。")
        return value


class Operation(Model):
    operationId: Identifier


class CreateTicket(Operation):
    id: Identifier
    originalAssetId: AssetId
    fields: TicketFields = Field(default_factory=TicketFields)


class UpdateTicket(Operation):
    fields: TicketFields = Field(default_factory=TicketFields)
    originalAssetId: AssetId | None = None

    @model_validator(mode="after")
    def valid_changes(self):
        if "originalAssetId" in self.model_fields_set and self.originalAssetId is None:
            raise ValueError("原票不能为空。")
        if not self.fields.model_fields_set and self.originalAssetId is None:
            raise ValueError("请提交要修改的字段。")
        return self
