# ComfyUI Deadline Submission

A set of custom nodes for submitting ComfyUI workflows to Thinkbox Deadline for distributed rendering.

## Overview

This package provides nodes for:
1. Submitting ComfyUI workflows to Deadline
2. Checking the status of Deadline jobs

## Installation

Place this folder in your ComfyUI `custom_nodes` directory.

## Usage

### Direct Workflow Submission

The `Submit to Deadline` node will automatically capture the workflow that's running in ComfyUI. No separate workflow capture node is needed!

1. Add a `Submit to Deadline` node to your workflow
2. Configure your Deadline settings (pool, group, priority, etc.)
3. Execute your workflow normally
4. The node will automatically capture and submit it to Deadline

## Available Nodes

### Submit to Deadline

Submits the current workflow to Deadline for rendering.

**Inputs:**
- `workflow_file`: Path to a workflow file (can be left empty to use the current workflow)
- `auto_detect_workflow`: Automatically detect and use the current workflow when enabled
- `priority`: Job priority in Deadline (0-100)
- `pool`: Deadline pool to use
- `group`: Deadline group to use
- `job_name`: Name for the Deadline job
- `bypass`: Skip submission when enabled
- `output_directory`: (Optional) Directory to save outputs on the render nodes
- `comment`: (Optional) Comment for the job
- `department`: (Optional) Department name

**Outputs:**
- `job_id`: The ID of the submitted Deadline job


## Troubleshooting

If you experience issues with workflow capture:

1. Make sure you've executed the workflow at least once before submitting
2. Check that your ComfyUI installation is up to date
3. Try placing the "Submit to Deadline" node at the end of your workflow chain
4. If the automatic capture doesn't work, you can export a workflow file manually and specify it in the "workflow_file" input


## License

MIT License 