from .raindrop import RaindropAdapter
from .chrome import ChromeAdapter
from .safari import SafariAdapter
from .generic_json import GenericJSONAdapter

REGISTRY = {
    "raindrop": RaindropAdapter,
    "chrome": ChromeAdapter,
    "safari": SafariAdapter,
    "json": GenericJSONAdapter,
}

__all__ = ["REGISTRY", "RaindropAdapter", "ChromeAdapter", "SafariAdapter", "GenericJSONAdapter"]

