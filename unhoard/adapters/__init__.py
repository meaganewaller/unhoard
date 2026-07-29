from typing import Callable

from .base import Adapter, WritebackAdapter
from .raindrop import RaindropAdapter
from .chrome import ChromeAdapter
from .safari import SafariAdapter
from .generic_json import GenericJSONAdapter

REGISTRY: dict[str, Callable[..., Adapter]] = {
    "raindrop": RaindropAdapter,
    "chrome": ChromeAdapter,
    "safari": SafariAdapter,
    "json": GenericJSONAdapter,
}

__all__ = [
    "REGISTRY", "Adapter", "WritebackAdapter",
    "RaindropAdapter", "ChromeAdapter", "SafariAdapter", "GenericJSONAdapter",
]

