from django import template
import re

register = template.Library()

@register.filter
def extract_youtube_id(url):
    """
    Extracts YouTube video ID from various formats.
    """
    pattern = r'(?:v=|\/shorts\/|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, url)
    return match.group(1) if match else ''