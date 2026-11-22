"""Brand registry.

AGENT_SPEC section 9: keep creator brands separate. Logos, typography,
color systems, and visual language must never mix without explicit instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from system import paths
from system.core_docs import load_core_docs


class BrandError(Exception):
    pass


@dataclass
class Brand:
    slug: str
    name: str
    brand_md: str = ""
    visual_identity_md: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def root(self) -> Path:
        return paths.BRANDS_DIR / self.slug

    def section(self, doc: str, heading: str) -> str:
        text = self.brand_md if doc == "brand" else self.visual_identity_md
        lines = text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if line.strip().lower() == f"## {heading}".lower():
                start = i + 1
                break
        if start is None:
            return ""
        end = len(lines)
        for j in range(start, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        return "\n".join(lines[start:end]).strip()


DISPLAY_NAMES = {
    "subhra-gaming": "Subhra Gaming",
}


BRAND_SLUGS = frozenset({"subhra-gaming"})


class BrandRegistry:
    def __init__(self) -> None:
        self._brands: dict[str, Brand] = {}
        for slug in BRAND_SLUGS:
            root = paths.BRANDS_DIR / slug
            brand = Brand(
                slug=slug,
                name=DISPLAY_NAMES.get(slug, slug.replace("-", " ").title()),
                brand_md=self._read(root / "BRAND.md"),
                visual_identity_md=self._read(root / "VISUAL_IDENTITY.md"),
                aliases=self._aliases(slug),
            )
            self._brands[slug] = brand

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @staticmethod
    def _aliases(slug: str) -> tuple[str, ...]:
        slug_words = slug.replace("-", " ").lower()
        compact = slug.replace("-", "").lower()

        # Also register readable labels so a request that names the channel by
        # display name still resolves to the correct slug.
        labels: dict[str, str] = {
            "subhra gaming": "subhra-gaming",
            "subhragaming": "subhra-gaming",
        }
        if slug in labels:
            return (slug, slug_words, compact, labels[slug], labels[slug].replace("-", ""))

        return (slug, slug_words, compact)

    def list_brands(self) -> list[Brand]:
        return list(self._brands.values())

    def get(self, slug: str) -> Brand:
        key = slug.strip().lower()
        for brand in self._brands.values():
            if key in brand.aliases or key == brand.slug:
                return brand
        raise BrandError(
            f"Unknown brand '{slug}'. Known brands: {', '.join(self._brands)}"
        )

    def resolve(self, text: str) -> Brand:
        """Resolve a brand from free text (objective, channel name, etc.)."""
        lowered = text.lower()
        for brand in self._brands.values():
            for alias in brand.aliases:
                if alias in lowered:
                    return brand
        raise BrandError(
            "Could not identify the channel/brand from the request. "
            f"Known brands: {', '.join(self._brands)}. "
            "Specify the channel explicitly (behavior rule 3: avoid unnecessary questions only when information is available)."
        )

    def assert_separation(self, text: str, context: dict | None = None) -> None:
        """Raise when a request tries to use a paused or unknown brand.

        Right now there is only one active brand (Subhra Gaming), so this
        rejects any attempt to bring back a paused channel (e.g. GTA V Labs)
        or invent a new one. Separation used to mean two active brands, now
        it means one active brand only.
        """
        text_l = text.lower()
        matched: list[str] = []
        for brand in self._brands.values():
            for alias in brand.aliases:
                if alias in text_l:
                    matched.append((brand.slug, alias))
        matched_slugs = [m[0] for m in matched]
        context_active: set[str] = set()
        context_override: str | None = None
        if context:
            override = context.get("channel_override") or context.get("channel")
            if override:
                context_override = str(override).strip().lower()
                if context_override in BRAND_SLUGS:
                    context_active.add(context_override)
        # If more than one distinct brand shows up in the text, treat that
        # as a mixing attempt regardless of which ones are currently active.
        if len(set(matched_slugs)) > 1:
            raise BrandError(
                "Only Subhra Gaming is active right now. Mixing multiple channels "
                f"({', '.join(sorted(set(matched_slugs)))}) is not allowed."
            )
        if context_override:
            if context_override not in BRAND_SLUGS:
                raise BrandError(
                    "Only Subhra Gaming is active right now. "
                    f"'{context_override}' is not an active channel."
                )
            if matched_slugs and set(matched_slugs) | {context_override} != {context_override}:
                combined = set(matched_slugs) | {context_override}
                raise BrandError(
                    "Only Subhra Gaming is active right now. Mixing multiple channels "
                    f"({', '.join(sorted(combined))}) is not allowed."
                )
            return
        if not context_override:
            if matched_slugs:
                return
            if context and (context.get("channel_override") or context.get("channel")):
                return
            raise BrandError(
                "Could not identify a single brand/channel. Specify Subhra Gaming "
                "explicitly."
            )



def load_brand_identity(brand: Brand) -> dict:
    """Structured identity fields parsed from VISUAL_IDENTITY.md (may be unfilled)."""
    vi = brand.visual_identity_md

    def grab(heading: str, labels: list[str]) -> dict:
        section = brand.section("visual_identity", heading)
        out = {}
        for label in labels:
            match = re.search(rf"{label}:([^\n]*)", section)
            value = match.group(1).strip() if match else ""
            value = re.sub(r"^-\s*", "", value)
            out[label] = value
        return out

    return {
        "colors": grab("Colors", ["Primary", "Secondary", "Accent", "Background"]),
        "typography": grab("Typography", ["Primary", "Secondary", "Thumbnail"]),
        "filled": bool(vi.strip()) and "Fill this document" not in vi,
        "raw": vi,
    }
