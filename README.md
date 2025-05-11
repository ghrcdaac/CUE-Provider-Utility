# CUE Provider Utility (cue-upload)

**Version:** 0.1.0 (Align with your `pyproject.toml`)  
**License:** Apache-2.0

## Overview

The CUE Provider Utility (`cue-upload`) is a Python-based command-line interface (CLI) designed to facilitate the upload of files and entire folder structures to the CUE (Cloud Upload Environment) backend system. It supports single-part and multipart uploads, robust error handling, user configuration, and rich feedback to the user.

This tool is intended for users who need to transfer data to the CUE system efficiently and reliably from their local machines.

## Features

* Upload individual files or entire directories recursively.
* Automatic handling of single-part vs. multipart uploads based on file size.
* Concurrent uploads for multiple files within a folder and for parts of a single large file.
* SHA256 checksum validation for data integrity.
* User-friendly configuration via a `config.toml` file and interactive `configure` command.
* Flexible authentication token management (CLI option, config file, environment variable, `.netrc`).
* Support for different backend environments (prod, uat, sit, local).
* Management of ignored file patterns (system defaults + user-defined).
* Detailed logging to both console (with colors and progress bars via Rich) and a log file.
* Client-side retries with exponential backoff for transient errors.
* Cross-platform compatibility (Windows, macOS, Linux).

## Prerequisites

* **Python:** Version 3.10 or newer.
* **Poetry:** Version 1.2+ (or newer, e.g., 2.x) for dependency management and packaging. Installation instructions for Poetry can be found at [python-poetry.org](https://python-poetry.org/docs/#installation).

## Installation

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/ghrcdaac/CUE-Provider-Utility.git
    cd CUE-Provider-Utility
    ```

2.  **Install Dependencies using Poetry:**
    ```bash
    poetry install
    ```

## Configuration

The utility uses a configuration file located at `~/.cue-upload/config.toml` by default. This file is automatically created with default values the first time you run the utility or the `configure` command.

1.  **Initial Configuration (Recommended):**
    ```bash
    cue-upload configure
    ```

2.  **Setting the API Token:**
    Provide the API token via:
    * Command-line: `cue-upload --token YOUR_TOKEN`
    * Config file via `cue-upload configure`
    * Environment variable: `export CUE_UPLOAD_API_TOKEN="YOUR_TOKEN"`
    * `.netrc` file in your home directory.

3.  **Configuration File Location:**
    Default: `~/.cue-upload/config.toml`  
    Override with `--config /path/to/my_custom_config.toml`

## Basic Usage


### General Help
```bash
cue-upload -h
cue-upload <command> -h
```

### Uploading Files or Folders
```bash
cue-upload upload -P /path/to/your/file.txt -c your_collection_name
cue-upload upload -P /path/to/your/folder/ -c your_collection_name -tp remote/sub_path/
```

### Managing Configuration
```bash
cue-upload configure
cue-upload configure --token
```

### Viewing Logs
```bash
cue-upload logs
cue-upload logs -n 100
cue-upload logs -f
```

### Managing Ignored File Patterns
```bash
cue-upload ignore list
cue-upload ignore add "*.log"
cue-upload ignore add "node_modules/"
cue-upload ignore remove "*.log"
cue-upload ignore reset
```

### Global Options

* `-T, --token YOUR_TOKEN`
* `--env ENV_NAME`
* `-v / -vv`: Increase verbosity
* `-q`: Quiet mode
* `--config /path/to/config.toml`
* `--log-file /path/to/log.txt`

## Development

### Activate Virtual Environment

### Running Tests
```bash
pytest
pytest --cov=cue_provider_utility tests/
```

### Linters and Formatters
```bash
black cue_provider_utility/ tests/
isort cue_provider_utility/ tests/
flake8 cue_provider_utility/ tests/
mypy cue_provider_utility/
```

## License

This project is licensed under the Apache License, Version 2.0. See the LICENSE file for details.