"""
ComfyUI Deadline Plugin.

Provides Deadline submission and seed nodes for ComfyUI.
"""

from .deadline_submit import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    register_on_prompt_handler,
)
from .farm_backend import register_backend

register_on_prompt_handler()
FARM_BACKEND = register_backend()

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
