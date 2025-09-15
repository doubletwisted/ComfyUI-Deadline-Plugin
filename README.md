# ComfyUI Deadline Plugin

Submit ComfyUI workflows to Thinkbox Deadline render farm.

Check out a quick demo showing how ComfyUI can play nice with Deadline.
Please check out the video:

<p align="center"> <a href="https://youtu.be/NFmIvEoEPiU"> <img src="https://img.youtube.com/vi/NFmIvEoEPiU/maxresdefault.jpg" alt="ComfyUI x Deadline: Leverage Your GPUs" /> </a> </p>

## Features

- Submit ComfyUI workflows directly to Deadline
- Batch rendering with seed variation
- Real-time progress monitoring via Deadline Monitor
- Configurable pools, groups, and priorities

## Installation

### ComfyUI Manager (Recommended)
1. Open ComfyUI Manager → Install Custom Nodes
2. Search "ComfyUI Deadline Submission" → Install
3. Restart ComfyUI

### Manual Installation
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/YOUR_USERNAME/ComfyUI-Deadline-Plugin.git
```

### Deadline Plugin Setup (Required)
Copy `plugins/ComfyUI/` to your Deadline Repository's `custom/plugins/` directory and restart Deadline services.

## Usage

1. Add "Submit to Deadline" node to your workflow
2. Configure job settings (name, priority, pool, etc.)
3. Execute workflow
4. Monitor progress in Deadline Monitor

### Key Settings

- **batch_count**: Number of tasks (1-100)
- **change_seeds_per_task**: Randomize seeds for different outputs
- **priority**: Job priority (0-100)
- **pool/group**: Deadline worker assignment

## Configuration

### Model Paths (Optional)
For render farms with shared storage, copy `example_extra_model_paths.yaml` to your ComfyUI installation as `extra_model_paths.yaml` and update paths.

## How It Works

1. Captures current ComfyUI workflow
2. Submits to Deadline with proper configuration
3. Workers execute workflow via ComfyUI API
4. Progress reported through Deadline Monitor

## Requirements

- ComfyUI installation on worker machines
- Thinkbox Deadline
- No additional Python dependencies (uses standard library)
