# ComfyUI Deadline Plugin

A comprehensive plugin for integrating ComfyUI workflows with Thinkbox Deadline render farm management.

## 🚀 Features

- **Seamless Integration**: Submit ComfyUI workflows directly to Deadline from within ComfyUI
- **Distributed Rendering**: Leverage your render farm to process ComfyUI workflows at scale
- **Real-time Progress Monitoring**: Track rendering progress through Deadline Monitor
- **Seed Variation Control**: Automatically vary seeds across tasks for batch rendering
- **Flexible Configuration**: Support for pools, groups, priorities, and custom output directories

## 📦 Repository Structure

This repository contains two main components:

```
ComfyUI-Deadline-Plugin/
├── custom_nodes/deadline_submission/    # ComfyUI custom nodes (auto-installed)
│   ├── __init__.py
│   ├── deadline_submit.py
│   └── README.md
├── plugins/ComfyUI/                     # Deadline plugin (manual install required)
│   ├── ComfyUI.py
│   ├── ComfyUI.param
│   └── README.md
├── requirements.txt                     # Python dependencies
├── pyproject.toml                      # Modern Python packaging
└── README.md                           # This file
```

## 🔧 Installation

### Option 1: ComfyUI Manager (Recommended)

1. **Install Custom Nodes** (Automatic):
   - Open ComfyUI Manager
   - Go to "Install Custom Nodes"
   - Search for "ComfyUI Deadline Submission"
   - Click Install
   - Restart ComfyUI

2. **Install Deadline Plugin** (Manual - Required):
   - Copy the `plugins/ComfyUI/` directory to your Deadline Repository's `custom/plugins/` directory
   - Restart Deadline services or Deadline Monitor

### Option 2: Manual Installation

1. **Clone Repository**:
   ```bash
   cd ComfyUI/custom_nodes
   git clone https://github.com/YOUR_USERNAME/ComfyUI-Deadline-Plugin.git
   ```

2. **Install Deadline Plugin**:
   - Copy `ComfyUI-Deadline-Plugin/plugins/ComfyUI/` to `[Deadline Repository]/custom/plugins/ComfyUI/`
   - Restart Deadline services

4. **Restart ComfyUI**

### Worker Machine Setup

No additional Python dependencies required - the plugin uses only standard library modules.

## 🎯 Quick Start

### Using ComfyUI Custom Nodes

1. **Add Submit Node**: In ComfyUI, add a "Submit to Deadline" node to your workflow
2. **Configure Settings**: Set job name, priority, pool, group, etc.
3. **Execute Workflow**: Run your workflow normally
4. **Monitor Progress**: Check job status in Deadline Monitor

### Direct Workflow Submission

1. **Export Workflow**: Save your workflow as JSON from ComfyUI
2. **Submit via Deadline**: Use the ComfyUI plugin in Deadline Monitor
3. **Configure Job**: Set rendering parameters and submit

## ⚙️ Configuration

### Deadline Plugin Configuration

Configure these settings in Deadline Monitor:

- **ComfyUI Instalation Path**: Path toComfyUI_windows_portable folder

### Model Paths Configuration (Optional)

ComfyUI is very slow to read models from network. For render farms with shared model storage, you can configure ComfyUI to use centralized model paths:

1. **Copy the example file**: `ComfyUI\custom_nodes\deadline_submission\example_extra_model_paths.yaml` to your ComfyUI installation
2. **Rename it**: `extra_model_paths.yaml` 
3. **Edit paths**: Update the paths to match your network storage setup

The example configuration shows a local-first, network-fallback setup:
- **Local path**: `C:/AI` (fast access on each worker)
- **Network path**: `X:/AI` (centralized storage, fallback)

This ensures workers use local models when available, falling back to network storage when needed.

## 📋 Usage Guide

### Submit to Deadline Node

**Required Inputs:**
- `workflow_file`: Override workflow file path (leave empty for auto-detection)
- `auto_detect_workflow`: Use current workflow (recommended: ON)
- `batch_count`: Number of tasks to create (1-100)
- `chunk_size`: Frames per task (1-16)
- `change_seeds_per_task`: Vary seeds across tasks for different outputs
- `priority`: Job priority (0-100)
- `pool`: Deadline pool to use
- `group`: Deadline group to use
- `job_name`: Name for the Deadline job
- `bypass`: Skip submission (for testing)
- `skip_local_execution`: Submit only vs. submit and run locally

**Optional Inputs:**
- `output_directory`: Custom output directory for all workers
- `comment`: Job comment
- `department`: Department name

**Outputs:**
- `job_id`: Deadline job ID for tracking


### Seed Variation Feature

Control how seeds are handled across batch tasks:

- **ON**: Each task gets randomized seeds → Different outputs
- **OFF**: All tasks use original seeds → Identical outputs

Compatible with:
- KSampler nodes (seed parameter)
- RandomNoise nodes (noise_seed parameter)
- Any node with seed-like parameters


## 🏗️ Technical Details

### How It Works

1. **Workflow Capture**: Automatically captures current ComfyUI workflow
2. **Deadline Submission**: Creates Deadline job with proper configuration
3. **Worker Execution**: 
   - Starts ComfyUI server on worker
   - Submits workflow via API
   - Randomizes seeds for batch
   - Monitors progress
4. **Progress Reporting**: Progress updates through Deadline Monitor

---

**Note**: This plugin requires both ComfyUI and Thinkbox Deadline to be properly installed and configured. The ComfyUI custom nodes can be installed automatically via ComfyUI Manager, but the Deadline plugin must be manually copied to your Deadline Repository. 