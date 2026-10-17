from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from constructai.extraction.schemas import Dimensions


class BOQStatus(str, Enum):
    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class BOQValidationIssue(BaseModel):
    severity: ValidationSeverity
    field: str
    message: str


class BOQItem(BaseModel):
    """Typed contract for a calculated BOQ line."""

    item_number: int = Field(ge=1)
    description: str
    element_type: str
    quantity: Decimal = Field(ge=0)
    provided_quantity: Decimal | None = Field(default=None, ge=0)
    unit: str
    calculation: str
    source_page: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0, le=1)
    dimensions: Dimensions | None = None
    dimensions_source: str | None = None
    element_id: str | None = None
    host_element_id: str | None = None
    item_code: str | None = None
    source_element_ids: list[str] = Field(default_factory=list)


class BOQ(BaseModel):
    """Validated BOQ generated from one extraction result."""

    source_name: str
    status: BOQStatus = BOQStatus.DRAFT
    items: list[BOQItem]
    validation_issues: list[BOQValidationIssue] = Field(default_factory=list)
    boq_id: str | None = None
    extraction_id: str | None = None
