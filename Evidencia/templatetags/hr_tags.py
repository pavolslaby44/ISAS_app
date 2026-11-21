from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """🧠 ISAS ENGINE: Umožňuje ťahať dáta zo slovníka priamo v HTML (nahrádza hranaté zátvorky)."""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None