from __future__ import annotations
import json
import os
import sys
import tempfile
from typing import Callable, Generic, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")

OnCreate = Callable[[K, V], None]
OnSet = Callable[[K, V], None]


class OnSetItemMixin(Generic[K, V]):
    _on_setitem: Optional[OnSet[K, V]] = None

    def set_on_setitem(self, cb: Optional[OnSet[K, V]]) -> None:
        self._on_setitem = cb

    def __setitem__(self, key: K, val: V) -> None:
        super().__setitem__(key, val)  # expects next class is dict-like
        cb = getattr(self, "_on_setitem", None)
        if cb is not None:
            cb(key, val)
    

class KeyDefaultDict(dict[K, V], Generic[K, V]):
    """
    A default_dict but the default can depend on the key.
    """

    def __init__(self, factory: Callable[[K], V]):
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory
        super().__init__()

    def __missing__(self, key: K) -> V:
        value = self._factory(key)
        self[key] = value
        return value

    # def get_factory(self) -> Callable[[K], V]:
    #     return self._factory

   

class CacheDict(OnSetItemMixin[K, V], KeyDefaultDict[K, V]):
    def __init__(self, factory: Callable[[K], V], auto_save: bool = True):
        super().__init__(factory)
        self.uncached = 0
        # if auto_save:
        #     self.enable_auto_save()

    def enable_auto_save(self) -> None:
        def on_setitem(key: K, val: V) -> None:
            self.uncached += 1
            if key == 'db_stats' or key == 'eval':
                sys.stderr.write(f"\nCached {self.uncached} items")
            if self.uncached % 100 == 0:
                self.serialize(self.default_cache_path)
        self.set_on_setitem(on_setitem)

    def serialize(self, path: str) -> None:
        sys.stderr.write("\nSaving cache...")
        path = path or self._default_cache_path() # TODO
        self.default_cache_path = path
        payload = {
            "version": 1,
            "items": [pc.to_dict() for pc in self.values()],
        }
        dir_ = os.path.dirname(path)
        os.makedirs(dir_, exist_ok=True) if dir_ else None
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=dir_ if dir_ else None,
            delete=False
        ) as tmp:
            json.dump(payload, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)

    @classmethod
    def from_dict(cls, path: str, pos_cache_factory: Callable) -> bool:
        path = path or cls._default_cache_path()
        if not os.path.exists(path):
            sys.stderr.write("PATH DOES NOT EXIST")
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            sys.stderr.write(f"Failed to load cache: {exc}")
            return False

        items = payload.get("items", [])
        cache = cls(lambda fen: pos_cache_factory({"fen": fen}))
        for item in items:
            # pc = PosCache.from_dict(self, item)
            pc = pos_cache_factory(item)
            cache[pc.fen] = pc
        return cache
