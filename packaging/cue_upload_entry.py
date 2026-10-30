"""PyInstaller entry point for the standalone ``cue-upload`` binary."""

from cue_provider_utility.main import cli_app

if __name__ == "__main__":
    cli_app(prog_name="cue-upload")
