"""yapper: the Yapin' bot library. `import yapper` gives you everything in `yapper.main.__all__`."""
from . import main as _main
from .main import *  # noqa: F401,F403  (the public API is whatever main.py lists in __all__)
from .main import __version__

__all__ = [*_main.__all__, "__version__"]
