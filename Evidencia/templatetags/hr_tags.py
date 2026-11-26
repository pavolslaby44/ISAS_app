import base64
import mimetypes
from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """🧠 ISAS ENGINE: Umožňuje ťahať dáta zo slovníka priamo v HTML (nahrádza hranaté zátvorky)."""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None

@register.filter
def media_data_uri(field_file):
    """🛡️ Prevedie lokálny obrázok (podpis/pečiatka) na inline data URI bez verejného /media/ odkazu."""
    if not field_file or not getattr(field_file, 'name', None):
        return ''
    try:
        mime_type, _ = mimetypes.guess_type(field_file.name)
        if not mime_type or not mime_type.startswith('image/') or mime_type == 'image/svg+xml':
            mime_type = 'image/png'
        with field_file.open('rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return f'data:{mime_type};base64,{encoded}'
    except Exception:
        return ''