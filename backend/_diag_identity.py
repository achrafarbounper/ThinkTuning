"""Diagnostic temporaire : identité des classes après pop/re-import du module mongodb."""
import sys

from app.infrastructure.persistence import common
from app.infrastructure.persistence.mongodb import MongoRunStore as M1_class

print("M1 class id:", id(M1_class))

# Simule l'état "module jamais chargé"
original = sys.modules.pop("app.infrastructure.persistence.mongodb", None)
common._MONGO_STORE_CLASSES.clear()

cls = common.get_mongo_store_class("run")
print("resolved id:", id(cls), "name:", cls.__name__)

sys.modules["app.infrastructure.persistence.mongodb"] = original
common._MONGO_STORE_CLASSES.clear()
common._MONGO_STORE_CLASSES["run"] = M1_class

from app.infrastructure.persistence import mongodb  # noqa: E402

print("module id:", id(mongodb), "original id:", id(original), "same:", mongodb is original)
print("identity check:", M1_class is mongodb.MongoRunStore)

# Ce que fait sqlite.py au chargement :
import importlib  # noqa: E402

sqlite_mod = importlib.import_module("app.infrastructure.persistence.sqlite")
print("sqlite alias identity:", sqlite_mod.SqliteRunStore is mongodb.MongoRunStore)
