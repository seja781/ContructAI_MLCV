from constructai.boq.calculator import QuantityCalculator
from constructai.boq.schemas import BOQ, BOQItem
from constructai.boq.validation import BOQValidator
from constructai.extraction.schemas import ExtractionResult


class BOQGenerator:
    """Convert validated extraction output into validated BOQ items."""

    def __init__(self, calculator: QuantityCalculator | None = None, validator: BOQValidator | None = None) -> None:
        self._calculator = calculator or QuantityCalculator()
        self._validator = validator or BOQValidator()

    def generate(self, extraction: ExtractionResult) -> BOQ:
        items = []
        for item_number, element in enumerate(extraction.elements, start=1):
            quantity, calculation = self._calculator.calculate(element)
            items.append(
                BOQItem(
                    item_number=item_number,
                    description=element.description,
                    element_type=element.element_type,
                    quantity=quantity,
                    provided_quantity=element.quantity,
                    unit=element.unit or "unit",
                    calculation=calculation,
                    source_page=element.source_page,
                    confidence=element.confidence,
                    dimensions=element.dimensions,
                    dimensions_source=element.dimensions_source,
                    element_id=element.element_id,
                    host_element_id=element.host_element_id,
                    item_code=element.element_id,
                    source_element_ids=[element.element_id] if element.element_id else [],
                )
            )
        boq = BOQ(source_name=extraction.source_name, items=items)
        extraction_issues = self._validator.validate_extraction(extraction)
        boq_issues = self._validator.validate_boq(boq)
        return boq.model_copy(update={"validation_issues": extraction_issues + boq_issues})