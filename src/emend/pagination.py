"""A bounded, browser-only reading surface for reflowable, horizontal LTR EPUBs."""

from functools import lru_cache
from importlib.resources import files

from emend.inspect import NS

MAX_PAGES = 5000


def validate_layout(layout: str, page_margin: int):
    if layout not in {"scroll", "paginated"}:
        raise ValueError("Layout must be scroll or paginated.")
    if type(page_margin) is not int or not 16 <= page_margin <= 120:
        raise ValueError("Page margin must be between 16 and 120 CSS pixels.")


def check_package(package):
    fixed = package.xpath(
        "opf:metadata/opf:meta[@property='rendition:layout' and normalize-space(.)='pre-paginated']"
        " | opf:metadata/opf:meta[@name='fixed-layout' and @content='true']",
        namespaces=NS,
    )
    fixed_spine = any(
        "rendition:layout-pre-paginated" in item.get("properties", "").split()
        for item in package.xpath("opf:spine/opf:itemref", namespaces=NS)
    )
    if fixed or fixed_spine:
        raise ValueError(
            "Paginated preview supports reflowable EPUBs only; use --layout scroll for fixed-layout books."
        )
    if package.xpath("opf:spine[@page-progression-direction='rtl']", namespaces=NS):
        raise ValueError(
            "Right-to-left page progression is not supported by paginated preview; use --layout scroll."
        )


def page_indices(page_count: int) -> list[int]:
    if not 1 <= page_count <= MAX_PAGES:
        raise ValueError(f"Paginated document must contain between 1 and {MAX_PAGES} pages.")
    return sorted({0, page_count // 2, page_count - 1})


@lru_cache(maxsize=1)
def pagination_script() -> str:
    return files("emend").joinpath("assets", "paginate.js").read_text()


def paginate(page, width: int, height: int, margin: int) -> dict:
    animations = page.evaluate("""() => {
        const animations = document.getAnimations();
        for (const animation of animations) { animation.pause(); animation.currentTime = 0; }
        return animations.length;
    }""")
    page.wait_for_function("document.fonts.status === 'loaded'", timeout=5000)
    result = page.evaluate(
        pagination_script(),
        {"width": width, "height": height, "margin": margin, "maxPages": MAX_PAGES},
    )
    if result.get("error"):
        raise ValueError(result["error"])
    # Validate again on the Python side before any offsets are used for capture.
    page_indices(result["page_count"])
    if animations:
        result["observations"].append(
            f"Animations frozen at their initial frame for stable pagination: {animations}."
        )
    return result
