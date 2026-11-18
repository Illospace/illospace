"""Render a small text subset. Escape source text before adding any HTML."""

import re
from html import escape
from urllib.parse import urlsplit

from illo.channels.slack.tasks import _visible


def safe_url(value):
    try:
        parts = urlsplit(value)
        return (parts.scheme in {"http", "https"} and bool(parts.hostname)
                and not parts.username and not parts.password
                and not any(ord(c) < 33 for c in value))
    except ValueError:
        return False


def inline(text):
    # Tokenize once. Source markup cannot change generated tags or nest links.
    pattern = r'(`[^`\n]+`|\[[^\]\n]+\]\(https?://[^\s)]+\)|https?://[^\s<>]+|\*\*[^*\n]+\*\*|\*[^*\n]+\*)'
    result = []
    for part in re.split(pattern, text):
        if part.startswith('`') and part.endswith('`'):
            result.append('<code>' + escape(part[1:-1]) + '</code>')
        elif part.startswith('**') and part.endswith('**'):
            result.append('<strong>' + escape(part[2:-2]) + '</strong>')
        elif part.startswith('*') and part.endswith('*'):
            result.append('<em>' + escape(part[1:-1]) + '</em>')
        elif part.startswith('[') and (match := re.fullmatch(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', part)):
            label, url = match.groups()
            result.append(link(url, label))
        elif part.startswith(('http://', 'https://')):
            url = part.rstrip('.,;!')
            result.append(link(url, url) + escape(part[len(url):]))
        else:
            result.append(escape(part))
    return ''.join(result)


def link(url, label):
    if not safe_url(url):
        return escape(label)
    return '<a rel="noopener noreferrer" href="' + escape(url, quote=True) + '">' + escape(label) + '</a>'


def render(text):
    lines = _visible(text or '').splitlines()
    result, paragraph, code = [], [], None
    list_kind = None

    def flush():
        nonlocal list_kind
        if paragraph:
            result.append('<p>' + '<br>'.join(inline(line) for line in paragraph) + '</p>')
            paragraph.clear()
        if list_kind:
            result.append(f'</{list_kind}>')
            list_kind = None

    for line in lines:
        if line.startswith('```'):
            if code is None:
                flush()
                code = []
            else:
                result.append('<pre><code>' + escape('\n'.join(code)) + '</code></pre>')
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        if not line.strip():
            flush()
        elif match := re.match(r'^(?:- |\d+\. )(.*)', line):
            kind = 'ul' if line.startswith('- ') else 'ol'
            if list_kind != kind:
                flush()
                result.append(f'<{kind}>')
                list_kind = kind
            result.append('<li>' + inline(match[1]) + '</li>')
        elif line.startswith('>'):
            flush()
            result.append('<blockquote>' + inline(line[1:].lstrip()) + '</blockquote>')
        else:
            if list_kind:
                flush()
            paragraph.append(line)
    if code is not None:
        result.append('<pre><code>' + escape('\n'.join(code)) + '</code></pre>')
    flush()
    return ''.join(result)
