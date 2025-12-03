from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe
import re

register = template.Library()


@register.filter
def intdiv(value, arg):
    try:
        return int(value) // int(arg)
    except (ValueError, ZeroDivisionError):
        return 0


@register.filter
def mul(value, arg):
    """
    Multiplies the value by the argument

    Usage: {{ value|mul:arg }}
    Example: {{ 5|mul:100 }} will return 500
    """
    try:
        return int(float(value)) * int(float(arg))
    except (ValueError, TypeError):
        return 0


@register.filter
def split(value, separator=","):
    """
    Split a string into a list using the given separator.

    Usage: {{ value|split:"," }}
    """
    if value is None:
        return []
    try:
        parts = str(value).split(str(separator))
    except Exception:
        return []
    # Trim whitespace and drop empty parts
    return [p.strip() for p in parts if p.strip()]


@register.filter
def strip(value):
    """
    Strip leading/trailing whitespace from a string.

    Usage: {{ value|strip }}
    """
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


@register.filter(name="markdown_links")
def markdown_links(value):
    """
    Convert simple Markdown-style links [text](https://example.com)
    into HTML anchors, server-side.

    - Escapes the base text to avoid XSS.
    - Adds a visual space before and after the link text.
    - Opens links in a new tab with safe rel attributes.
    """
    if not value:
        return ""

    text = escape(str(value))
    pattern = re.compile(r"\[([^\]]+?)\]\((https?://[^\s)]+)\)")

    def _replace(match: re.Match) -> str:
        label_raw = match.group(1) or ""
        href_raw = match.group(2) or ""

        label = escape(label_raw.strip() or href_raw)
        href = escape(href_raw)

        return (
            " "
            '<a href="{href}" target="_blank" rel="noopener noreferrer" '
            'class="text-blue-600 hover:text-blue-800 underline">{label}</a>'
            " "
        ).format(href=href, label=label)

    result = pattern.sub(_replace, text)
    return mark_safe(result)