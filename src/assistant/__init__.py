# Parapharma Assistant - Assistant vocal parapharmacie pour robot Pepper

__version__ = "1.0.0"
__author__ = "Projet AMS - Master 2"

from .config import Config, RunMode, get_config
from .logger import SystemLogger, get_logger

__all__ = [
    "Config",
    "RunMode",
    "get_config",
    "SystemLogger",
    "get_logger",
    "__version__",
]
