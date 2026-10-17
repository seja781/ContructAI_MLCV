from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field


class ElementReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Dimensions(BaseModel):
    """Optional dimensions (metres) used by a later quantity calculation step.

    Extractors fill them only from a dimension annotation (converted with exact
    unit factors), from geometry measured with a verified drawing scale, or
    from a human reviewer. They are never defaulted.
    """

    length: Decimal | None = Field(default=None, gt=0)
    width: Decimal | None = Field(default=None, gt=0)
    height: Decimal | None = Field(default=None, gt=0)


class ExtractedElement(BaseModel):
    """Typed contract for a construction element returned by extraction."""

    element_type: str = Field(validation_alias=AliasChoices("element_type", "category"))
    element_id: str | None = None
    host_element_id: str | None = None  # wall hosting this door/window/opening (used for deductions)
    description: str
    location: str | None = None
    dimensions: Dimensions | None = None
    quantity: Decimal | None = Field(default=None, ge=0)
    unit: str | None = None
    specification: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_reference: str | None = None
    confidence: float = Field(ge=0, le=1)
    # Where the dimensions came from: "ocr" (read from an annotation by OCR),
    # "text_layer" (exact PDF/SVG text), "geometry_verified_scale" (measured with
    # a verified drawing scale), "human" (entered by a reviewer) or None.
    dimensions_source: str | None = None
    review_status: ElementReviewStatus = ElementReviewStatus.PENDING
    review_reasons: list[str] = Field(default_factory=list)

    # --- Measurement provenance (optional; older payloads remain valid) ---
    evidence: str | None = None
    room_type: str | None = None
    measurement_source: str | None = Field(
        default=None,
        description="dimension_annotation | geometry_verified_scale | opencv_candidate | text_fixture",
    )
    measurement_status: str | None = Field(default=None, description="extracted | calculated | unverified")
    raw_dimension_text: str | None = None
    original_unit: str | None = None
    annotation_ids: list[str] = Field(default_factory=list)
    association_status: str | None = None

    @property
    def category(self) -> str:
        """Compatibility alias for the original foundation contract."""

        return self.element_type


class MeasurementValue(BaseModel):
    """One value exactly as read, plus its normalised metric value."""

    raw_text: str
    value: Decimal | None = None  # in original unit (decimal feet for ft-in)
    original_unit: str | None = None  # ft-in | ft | in | m | cm | mm | None (unknown)
    unit_source: str | None = None  # annotation | pair_suffix | drawing_note
    value_m: Decimal | None = None  # None when the unit is unknown


class DimensionAnnotation(BaseModel):
    """A dimension/area annotation found in the drawing, associated or not."""

    annotation_id: str
    kind: Literal["pair", "linear", "area", "unreadable"]
    raw_text: str
    values: list[MeasurementValue] = Field(default_factory=list)
    area_value: Decimal | None = None
    area_unit: str | None = None
    area_m2: Decimal | None = None
    context_text: str | None = None
    source: str
    source_page: int | None = None
    bbox: list[float] | None = None
    associated_element_id: str | None = None
    associated_label: str | None = None
    association_status: Literal["associated", "uncertain", "unassociated"] = "unassociated"
    association_reason: str | None = None
    status: Literal["extracted", "unverified"] = "unverified"
    confidence: float = Field(default=0.0, ge=0, le=1)
    readings_agreeing: int = 0
    readings_total: int = 0
    alternative_readings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class UnresolvedItem(BaseModel):
    """Something seen in the drawing that could not become a measured element."""

    text: str
    reason: str
    source_page: int | None = None
    bbox: list[float] | None = None
    source: str | None = None


class ScaleReference(BaseModel):
    """One dimension annotation matched to a measured dimension line."""

    annotation_id: str | None = None
    raw_text: str
    value_m: Decimal
    pixel_length: float
    pixels_per_metre: float
    orientation: Literal["horizontal", "vertical"]
    agrees_with_consensus: bool = False


class ScaleCalibration(BaseModel):
    """How (and whether) drawing pixels can be converted to real-world units.

    ``status`` is only ``verified`` when at least two independent dimension
    references agree. A single reference or disagreeing references never
    establish a scale.
    """

    page: int
    status: Literal["verified", "single_reference", "conflicting", "unavailable"]
    pixels_per_metre: float | None = None
    method: str = "dimension annotation matched to its dimension line"
    tolerance_pct: float | None = None
    spread_pct: float | None = None
    references: list[ScaleReference] = Field(default_factory=list)
    reason: str


class ExtractionResult(BaseModel):
    """Structured output returned for one uploaded drawing."""

    source_name: str
    elements: list[ExtractedElement]
    extraction_id: str | None = None

    # --- Transparency fields (optional; older payloads remain valid) ---
    status: Literal["complete", "partial", "no_measurements"] = "complete"
    extraction_method: str | None = None
    real_world_scale: str | None = None
    scale_calibration: list[ScaleCalibration] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dimension_annotations: list[DimensionAnnotation] = Field(default_factory=list)
    unresolved: list[UnresolvedItem] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
