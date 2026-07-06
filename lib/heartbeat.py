"""Compatibility facade for heartbeat implementation modules."""
import inspect

from lib import heartbeat_core as _core
from lib import heartbeat_monitoring as _monitoring
from lib import heartbeat_task_diagnosis as _task_diagnosis
from lib import heartbeat_runtime as _runtime


_MODULES = (_core, _monitoring, _task_diagnosis, _runtime)
_ORIGINALS = {}
_FACADE_DEFAULTS = {}


def _exported_names():
    names = []
    seen = set()
    for module in _MODULES:
        for name in getattr(module, "__all__", []):
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names


def _wrap(module, name):
    def wrapper(*args, **kwargs):
        _sync()
        return getattr(module, name)(*args, **kwargs)

    wrapper.__name__ = name
    wrapper.__doc__ = getattr(getattr(module, name), "__doc__", None)
    return wrapper


def _install_exports():
    for module in _MODULES:
        for name in getattr(module, "__all__", []):
            if name.startswith("__"):
                continue
            value = getattr(module, name)
            _ORIGINALS.setdefault((module, name), value)
            if inspect.isfunction(value):
                globals()[name] = _wrap(module, name)
            else:
                globals()[name] = value

    for name in _exported_names():
        if name in globals():
            _FACADE_DEFAULTS[name] = globals()[name]


def _sync():
    for module in _MODULES:
        for name in getattr(module, "__all__", []):
            if name not in globals():
                continue
            current = globals()[name]
            default = _FACADE_DEFAULTS.get(name, object())
            original = _ORIGINALS.get((module, name))
            if current is default and original is not None:
                setattr(module, name, original)
            else:
                setattr(module, name, current)


_install_exports()

__all__ = _exported_names()
