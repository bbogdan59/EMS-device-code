from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ems-device")
except PackageNotFoundError:
    __version__ = None  # Source-only checkout: never invent an installed version.
