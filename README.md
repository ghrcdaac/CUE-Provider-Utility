# CUE Provider Utility (cue-upload)
**Version:** 0.1.0  
**License:** Apache-2.0  

## Overview
The CUE Provider Utility (cue-upload) is a Python-based command-line interface (CLI) designed to facilitate the upload of files and entire folder structures to the CUE (Cloud Upload Environment) backend system. It is optimized for large files, provides a rich user experience, and ensures data integrity through robust error handling and checksum validation.

This tool is intended for users who need to transfer data to the CUE system from their local machines.

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
* Cross-platform compatibility (Windows, macOS, Linux).

## Prerequisites
- **Python:** Version 3.10 or newer.  
- **Poetry:** Version 1.2+ is recommended for dependency management. Installation instructions can be found at [python-poetry.org](https://python-poetry.org).

## Installation
**Clone the Repository:**
```bash
git clone https://github.com/ghrcdaac/CUE-Provider-Utility.git
cd CUE-Provider-Utility
```

**Install Dependencies using Poetry:**  
This command creates a virtual environment and installs all necessary packages.
```bash
poetry install
```

## Configuration
The utility is configured via a file located at `~/.cue-upload/config.toml`. This file is created automatically the first time you run the tool.

### First-Time Setup
The recommended way to set up the CLI is to use the interactive configure command. This will guide you through setting up your API key storage.

```bash
cue-upload configure
```

The configuration process will:
- Ask you where you want to store your API key:
  - `.netrc` file (Recommended): A standard, secure, and user-specific file for credentials.
  - `config.toml` file.
- Prompt you to enter your API key (your input will be visible to prevent typos).
- Optionally allow you to configure advanced settings like the default environment or retry attempts.

### API Key Management
The CLI retrieves your API key from the following sources, in this order of priority:
1. **Command-Line Argument:** The `-t` or `--token` option. (e.g., `cue-upload --token YOUR_KEY ...`)
2. **Environment Variable:** The `CUE_UPLOAD_API_TOKEN` environment variable.
3. **.netrc File:** A secure, permissions-controlled file in your home directory.
4. **config.toml File:** The application's configuration file.

The CLI will print which source it is using when you start an upload.

## Basic Usage
### Activate the Virtual Environment
Before running any commands, activate the Poetry virtual environment:
```bash
poetry shell
```

### General Help
```bash
cue-upload -h
cue-upload upload -h
```

### Uploading Files or Folders
```bash
# Upload a single file
cue-upload upload -P /path/to/your/file.txt -c your_collection_name

# Upload a folder and specify a target sub-path in the collection
cue-upload upload -P /path/to/your/folder/ -c your_collection_name -tp remote/sub_path/
```

### Managing Configuration
```bash
# Run the interactive configuration wizard
cue-upload configure
```

### Viewing Logs
```bash
# View the entire log file
cue-upload logs

# View the last 100 lines
cue-upload logs -n 100
```

### Managing Ignored File Patterns
```bash
# List all current ignore patterns
cue-upload ignore list

# Add a pattern to ignore all .log files
cue-upload ignore add "*.log"

# Remove a pattern
cue-upload ignore remove "*.log"

# Clear all user-defined patterns
cue-upload ignore reset
```

## Global Options
- `-t, --token YOUR_TOKEN`: Provide API key directly.  
- `--env ENV_NAME`: Target a specific backend environment (prod, uat, sit, local).  
- `-v / -vv`: Increase console verbosity. `-vv` includes full network logs.  
- `-q`: Quiet mode, showing only critical errors.  
- `--config /path/to/config.toml`: Use a custom configuration file.  

## Development
### Running Tests
Tests are located in the `tests/` directory and can be run with pytest.
```bash
# Run all tests
pytest

# Run tests with coverage report
pytest --cov=cue_provider_utility tests/
```

### Linters and Formatters
This project uses `black`, `isort`, `flake8`, and `mypy` to maintain code quality.
```bash
black cue_provider_utility/ tests/
isort cue_provider_utility/ tests/
flake8 cue_provider_utility/ tests/
mypy cue_provider_utility/
```

## License
This project is licensed under the Apache License, Version 2.0. See the LICENSE file for details.
