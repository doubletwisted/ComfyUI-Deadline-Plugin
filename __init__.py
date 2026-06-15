"""
ComfyUI Deadline Plugin.

Provides Deadline submission and seed nodes for ComfyUI.
"""

from .deadline_submit import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    register_on_prompt_handler,
)

register_on_prompt_handler()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
