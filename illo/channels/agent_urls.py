"""Validate agent callback addresses without a network call."""

import re
from urllib.parse import urlsplit


def valid_redirect(uri, *, https_only=False):
    if (not isinstance(uri, str) or not uri or len(uri) > 2048
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri)
            or "#" in uri or "\\" in uri):
        return False
    try:
        parsed = urlsplit(uri)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        if https_only:
            return uri.startswith("https://") and parsed.scheme == "https"
        return bool(re.match(r"^http://(?:localhost|127\.0\.0\.1|\[::1\])(?::[1-9][0-9]{0,4})?(?:/|\?|$)", uri))
    except ValueError:
        return False
