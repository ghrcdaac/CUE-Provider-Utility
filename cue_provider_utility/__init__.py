from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cue-provider-utility")
except PackageNotFoundError:  # running from a source tree or a frozen bundle
    __version__ = "1.4.0.dev0"
