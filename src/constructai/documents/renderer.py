"""Convert construction drawings into page images for OCR and geometry analysis.

PDF and SVG are rendered with PyMuPDF (already a project dependency), so no
Cairo/GTK runtime is needed on Windows. Raster images are loaded with Pillow,
EXIF orientation is applied and transparency is composited onto white (a plain
``convert("RGB")`` turns transparent pixels black and hides black text).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_DRAWING_EXTENSIONS = {".pdf", ".svg", *SUPPORTED_IMAGE_EXTENSIONS}

# Rendering resolution for vector drawings. An image-processing parameter only:
# it is never used to convert pixels into real-world units.
RENDER_DPI = 300


class DrawingPage:
    def __init__(self, page_number: int, image: Image.Image):
        self.page_number = page_number
        self.image = to_rgb(image)


def to_rgb(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


class DrawingDocument:
    """Render PDF/SVG/image drawings into page images."""

    def __init__(self, source: Path):
        self.source = Path(source)

    def render_pages(self) -> list[DrawingPage]:
        suffix = self.source.suffix.lower()
        if suffix in {".pdf", ".svg"}:
            return self._render_vector()
        if suffix in SUPPORTED_IMAGE_EXTENSIONS:
            try:
                with Image.open(self.source) as image:
                    image.load()
                    return [DrawingPage(1, image)]
            except Exception as error:
                raise ValueError(f"Image could not be opened as a drawing: {error}") from error
        raise ValueError(f"Unsupported drawing format '{suffix}'")

    def _render_vector(self) -> list[DrawingPage]:
        import pymupdf

        pages: list[DrawingPage] = []
        try:
            with pymupdf.open(self.source) as document:
                for index, page in enumerate(document, start=1):
                    pixmap = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
                    image = Image.open(BytesIO(pixmap.tobytes("png")))
                    pages.append(DrawingPage(index, image))
        except Exception as error:
            raise ValueError(f"Drawing could not be rendered: {error}") from error
        return pages
