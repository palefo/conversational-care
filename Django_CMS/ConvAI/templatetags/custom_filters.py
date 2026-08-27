from django import template
from django.conf import settings
from django.templatetags.static import static
from ..site_config import (
    brand_name as _cfg_brand_name,
    brand_logo as _cfg_brand_logo,
    brand_logo_uploaded_url as _cfg_brand_logo_uploaded_url,
)
import os


register = template.Library()

@register.filter(name='detector_label')
def detector_label(title):
    """Read a classifier alert's title in the viewer's language.

    The title of an alert the classifier raised is the detector's label, and
    built-in labels are stored in English so the stored string keeps matching
    the config and the dedupe. This translates it for display only; labels an
    admin wrote pass through as their own words.
    """
    from ..utils_conversation_classification import display_label
    return display_label(title)


@register.filter
def get_item(dictionary, key):
    """Fetches a dictionary item by key"""
    return dictionary.get(key)

@register.filter(name='priority_class')
def priority_class(priority_value):
    if priority_value == 'High':
        return 'high-priority'
    elif priority_value == 'Medium':
        return 'medium-priority'
    elif priority_value == 'Low':
        return 'low-priority'
    return ''

@register.filter
def get_dict_value(dictionary, key):
    """Returns the value of a dictionary given a key, or an empty list if the key does not exist."""
    return dictionary.get(key, [])

@register.filter
def basename(value):
    return os.path.basename(value)

@register.filter
def field_by_name(form, name):
    """Return form[name] (a BoundField) so you can render it dynamically."""
    return form[name]

@register.filter
def dict_get(d, key):
    try:
        return d.get(str(key))
    except Exception:
        return None


@register.filter
def get_item(d, key):
    try:
        return d.get(key)
    except Exception:
        return None

@register.simple_tag
def brand_logo_url():
    """Return the brand logo URL: an uploaded file if present, else the static
    path from DB override / settings / ENV."""
    uploaded = _cfg_brand_logo_uploaded_url()
    if uploaded:
        return uploaded
    return static(_cfg_brand_logo())

@register.simple_tag
def brand_name():
    """Return brand display name (DB override, else settings/ENV)."""
    return _cfg_brand_name()

@register.simple_tag
def static_versioned(path):
    """A static URL stamped with the file's modification time.

    The hand-written ``?v=3`` this replaces had to be bumped by hand whenever a
    stylesheet changed. Forgetting meant browsers kept serving the cached copy
    and the new CSS silently never arrived — which is exactly what happened to
    the detail panel. Deriving the stamp from mtime makes that impossible.
    """
    url = static(path)
    try:
        from django.contrib.staticfiles import finders
        full = finders.find(path)
        if full:
            return f"{url}?v={int(os.path.getmtime(full))}"
    except Exception:
        pass
    return url


@register.filter
def first_unit(value):
    """Keep only the leading unit of a Django `timesince` string.

    "4 days, 10 hours" becomes "4 days". At a glance the coarse figure is the
    whole message — the extra precision is noise on a row you are scanning.
    """
    return str(value).split(",")[0].strip()
