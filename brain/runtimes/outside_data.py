"""Mark outside data without a dependency on the app package."""

import re
import unicodedata

DATA_SOURCE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,80}$")
# "<", the full-width "＜", and the small "﹤", then an optional "/", then "outside data"
# with any space, underscore, or hyphen, in any case.
DATA_MARKER = re.compile(r"[<＜﹤]\s*/?\s*outside[\s_\-]*data", re.IGNORECASE)


def _visible(text: str) -> str:
    """Show format controls (zero-width, bidi) as text, so they cannot hide a marker."""
    return "".join(
        f"[U+{ord(c):04X}]" if unicodedata.category(c) == "Cf" else c for c in text
    )


def data_text(source: str, text) -> str:
    """Mark data for the model. Only this function writes the outside-data markers."""
    if not DATA_SOURCE.fullmatch(source):
        raise ValueError("The data source name is invalid")
    text = str(text or "")
    probe = unicodedata.normalize(
        "NFKC", "".join(c for c in text if unicodedata.category(c) != "Cf")
    )
    if DATA_MARKER.search(probe):
        # Hostile or nested text only: make every look-alike visible, then break each marker.
        text = unicodedata.normalize("NFKC", _visible(text))
        text = DATA_MARKER.sub(lambda m: "‹" + m.group(0)[1:], text)
    return f'<outside_data source="{source}">\n{text}\n</outside_data>'
