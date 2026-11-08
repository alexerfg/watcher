"""Plugin loader. Any module in this folder that defines NAME and fetch() is
picked up automatically, so adding a platform = adding one file."""
import importlib
import logging
import pkgutil

log = logging.getLogger("watcher")


def load_plugins():
    found = {}
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{mod_info.name}")
        except Exception as e:  # a broken plugin must not stop the others
            log.error("Could not load plugin %s: %s", mod_info.name, type(e).__name__)
            continue
        if hasattr(mod, "NAME") and hasattr(mod, "fetch"):
            found[mod.NAME] = mod
    return found
