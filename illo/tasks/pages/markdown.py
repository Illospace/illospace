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


def cut(text, limit=1200):
    """Cut a long text at a blank line outside a code block. The reader opens the rest."""
    text = _visible(text or '')
    if len(text) <= limit + 800:
        return text, ''
    size, fence, lines = 0, False, text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith('```'):
            fence = not fence
        if size >= limit and not fence and not line.strip():
            return '\n'.join(lines[:index]), '\n'.join(lines[index + 1:])
        size += len(line) + 1
    return text, ''


def cells(line):
    # Remove one pipe at each end only. An empty cell at the edge is a cell.
    line = line.strip()
    return [cell.strip() for cell in line[1:-1 if line.endswith('|') and len(line) > 1 else None].split('|')]


def render(text):
    lines = _visible(text or '').splitlines()
    result, paragraph, code, table = [], [], None, []
    lists = []

    def flush(depth=0):
        if paragraph:
            result.append('<p>' + '<br>'.join(inline(line) for line in paragraph) + '</p>')
            paragraph.clear()
        if table:
            head = len(table) > 1 and re.fullmatch(r'[\s|:-]+', table[1]) is not None
            result.append('<div class="table"><table>')
            for index, row in enumerate(table):
                if head and index == 1:
                    continue
                tag = 'th' if head and index == 0 else 'td'
                result.append('<tr>' + ''.join(f'<{tag}>' + inline(cell) + f'</{tag}>' for cell in cells(row)) + '</tr>')
            result.append('</table></div>')
            table.clear()
        while len(lists) > depth:
            result.append('</li></' + lists.pop() + '>')

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
        elif match := re.match(r'^( *)(?:[-*+] |(\d+)\. )(.*)', line):
            # Each list level stays open, so an item with more indent goes inside the item before it.
            kind = 'ol' if match[2] else 'ul'
            level = min(len(match[1]) // 2, len(lists), 3)
            flush(level + 1)
            if len(lists) == level + 1 and lists[-1] != kind:
                flush(level)
            if len(lists) == level + 1:
                result.append('</li>')
            else:
                result.append(f'<{kind}>')
                lists.append(kind)
            result.append('<li>' + inline(match[3]))
        elif lists and line.startswith('  ') and not paragraph:
            result.append('<br>' + inline(line.strip()))
        elif match := re.match(r'^(#{1,6}) +(.*)', line):
            flush()
            tag = 'h3' if len(match[1]) < 3 else 'h4'
            # Closing marks count only after a space, so "C#" keeps its mark.
            title = match[2].rstrip()
            bare = title.rstrip('#')
            if not bare or bare.endswith(' '):
                title = bare.rstrip()
            result.append(f'<{tag}>' + inline(title) + f'</{tag}>')
        elif re.fullmatch(r' {0,3}(?:-{3,}|\*{3,}|_{3,}) *', line):
            flush()
            result.append('<hr>')
        elif line.startswith('|') and line.rstrip().endswith('|') and not paragraph:
            if not table:
                flush()
            table.append(line)
        elif line.startswith('>'):
            flush()
            result.append('<blockquote>' + inline(line[1:].lstrip()) + '</blockquote>')
        else:
            if lists or table:
                flush()
            paragraph.append(line)
    if code is not None:
        result.append('<pre><code>' + escape('\n'.join(code)) + '</code></pre>')
    flush()
    return ''.join(result)
