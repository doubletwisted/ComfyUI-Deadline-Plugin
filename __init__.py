"""
ComfyUI Deadline Plugin

A comprehensive plugin for integrating ComfyUI workflows with Thinkbox Deadline render farm management.
"""

# Import the custom nodes
from .deadline_submit import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# Export for ComfyUI Manager
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"] 