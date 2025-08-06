"""
ComfyUI Deadline Plugin

A comprehensive plugin for integrating ComfyUI workflows with Thinkbox Deadline render farm management.
"""

import logging

# Import the custom nodes
from .deadline_submit import NODE_CLASS_MAPPINGS as SUBMIT_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS as SUBMIT_DISPLAY_MAPPINGS

# Use the mappings from deadline_submit
NODE_CLASS_MAPPINGS = SUBMIT_MAPPINGS
NODE_DISPLAY_NAME_MAPPINGS = SUBMIT_DISPLAY_MAPPINGS

# Setup web extensions
WEB_DIRECTORY = "./web"

# Export for ComfyUI Manager
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

# Initialize API when server is available
def init_server(server):
    """Initialize Deadline API with ComfyUI server"""
    try:
        from .deadline_api import integrate_with_comfyui
        integrate_with_comfyui(server)
        logging.info("Deadline API initialized")
    except Exception as e:
        logging.error(f"Failed to initialize Deadline API: {e}")

# ComfyUI will call this if it exists
try:
    import server
    if hasattr(server, "PromptServer") and server.PromptServer.instance:
        init_server(server.PromptServer.instance)
except:
    # Server not available yet, will be initialized later
    pass 