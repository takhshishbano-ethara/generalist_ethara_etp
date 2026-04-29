"""Dynamic loading/unloading of staging harness files.

Provides scoped overlay on Instance._registry so staging harnesses
temporarily replace or add entries during a single evaluation run,
then restore the original state on cleanup.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time

from .instance import Instance

_logger = logging.getLogger(__name__)


def load_staging_harness(file_path: str, org: str, repo: str) -> dict[str, type | None]:
    """Load a staging harness .py file into Instance._registry.

    Returns a dict mapping each registry key that was affected to its
    *original* value (or ``None`` if the key did not exist before).
    The caller should pass this dict to :func:`unload_staging_harness`
    to restore the registry to its prior state.
    """
    key = f"{org}/{repo}"
    module_name = f"staging_{org}_{repo}_{int(time.time())}"

    original: dict[str, type | None] = {}
    original[key] = Instance._registry.get(key)

    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot create module spec from {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    _logger.info(
        "Loaded staging harness for %s from %s (module=%s)",
        key, file_path, module_name,
    )
    return original


def unload_staging_harness(originals: dict[str, type | None]) -> None:
    """Restore Instance._registry entries that were overwritten by staging.

    *originals* is the dict returned by :func:`load_staging_harness`.
    """
    for key, original_class in originals.items():
        if original_class is not None:
            Instance._registry[key] = original_class
            _logger.debug("Restored production harness for %s", key)
        else:
            Instance._registry.pop(key, None)
            _logger.debug("Removed staging-only harness for %s", key)

    stale = [name for name in sys.modules if name.startswith("staging_")]
    for name in stale:
        del sys.modules[name]
