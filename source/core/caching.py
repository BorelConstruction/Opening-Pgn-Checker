from __future__ import annotations
import json
import os
import sys
import tempfile
import time
from typing import Callable, Generic, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")

OnCreate = Callable[[K, V], None]
OnSet = Callable[[K, V], None]
OnGet = Callable[[K], None]


class OnGetItemMixin(Generic[K]):
    _on_getitem: Optional[OnGet[K]] = None

    def set_on_getitem(self, cb: Optional[OnGet[K]]) -> None:
        self._on_getitem = cb

    def __getitem__(self, key: K):
        cb = getattr(self, "_on_getitem", None)
        if cb is not None:
            cb(key)
        return super().__getitem__(key)
    

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

   

class CacheDict(OnGetItemMixin[K], KeyDefaultDict[K, V]):
    def __init__(self, factory: Callable[[K], V], auto_save: bool = True, save_path: Optional[str] = None):
        super().__init__(factory)
        self._saving = False
        self.autosave_interval = 180 # seconds
        self._last_save_t = time.monotonic()
        self.default_cache_path = save_path
        if auto_save:
            self.enable_auto_save()

    def enable_auto_save(self, enforced_path: Optional[str] = None) -> None:
        self.default_cache_path = enforced_path or self.default_cache_path
        
        def autosave_on_getitem(key: K, val: V = None) -> None:
            if self._saving: # prevents recursive calls to serialize() when saving the cache, though it won't happen unless dumping is unimaginably slow
                return
            
            now = time.monotonic()
            if now - self._last_save_t < self.autosave_interval:
                return
            self._last_save_t = now
            
            sys.stderr.write(f"\nAuto-saving cache...")
            self.serialize()

        self.set_on_getitem(autosave_on_getitem)

    def serialize(self, enforced_path: Optional[str] = None) -> None:
        self._saving = True
        try:
            sys.stderr.write("\nSaving cache...")
            path = enforced_path or self.default_cache_path
            if path is None:
                raise ValueError("Cache path is not set")
            self.default_cache_path = path
            payload = {
                "version": 1,
                "items": [pc.to_dict() for pc in self.values()],
            }
            dir_ = os.path.dirname(path)
            os.makedirs(dir_, exist_ok=True) if dir_ else None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8",
                                                dir=dir_ if dir_ else None,
                                                delete=False) as tmp:
                    tmp_name = tmp.name
                    json.dump(payload, tmp)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, path)
            except Exception as e:
                print(f"[CACHE SAVE FAILED] {type(e).__name__}: {e}", file=sys.stderr)
                if tmp_name:
                    try: os.remove(tmp_name)
                    except OSError: pass
                raise
        finally:
            self._saving = False

    @classmethod
    def from_dict(cls, path: str, pos_cache_factory: Callable) -> CacheDict[K, V]:
        cache = cls(lambda fen: pos_cache_factory({"fen": fen}))
        cache.default_cache_path = path
        if not os.path.exists(path):
            sys.stderr.write("\nCache file does not exist, starting with an empty cache.")
            return cache
        
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        items = payload.get("items", [])
        for item in items:
            # pc = PosCache.from_dict(self, item)
            pc = pos_cache_factory(item)
            cache[pc.fen] = pc
        return cache
