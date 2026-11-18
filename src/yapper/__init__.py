"""yapper: the Yapin' bot library. `import yapper` gives you everything in `yapper.main.__all__`."""
from . import main as _main
from .main import *  # noqa: F401,F403  (the public API is whatever main.py lists in __all__)
from .main import __version__

__all__ = [*_main.__all__, "__version__"]


def __getattr__(name):
    # `from yapper import telemetry` (or `yapper.telemetry`) loads the opt-in telemetry module the first time it is
    # asked for. `import yapper` never loads it, and it is deliberately not in __all__, so `from yapper import *`
    # does not either: telemetry only exists in a program that went and asked for it.
    if name == "telemetry":
        import importlib
        return importlib.import_module(".telemetry", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted([*globals(), "telemetry"])
