"""Validation post-migration : counts Mongo, settings normalisés, ordre messages."""
from app.infrastructure.persistence.common import get_mongo_provider

provider = get_mongo_provider()
db = provider.db
for name in ("agent_sessions", "agent_session_messages", "agent_memory", "agent_settings"):
    print(f"{name}: {db[name].count_documents({})} document(s)")

# Settings : MongoAgentSettingsStore.get_all doit normaliser les chaînes JSON.
from app.infrastructure.persistence.mongodb import MongoAgentSettingsStore

store = MongoAgentSettingsStore(provider)
values = store.get_all()
print("provider:", repr(values.get("provider")))
print("model:", repr(values.get("model")))
print("temperature:", repr(values.get("temperature")))

# Ordre des messages d'une session (les 3 premiers, par _order asc) :
docs = list(
    db["agent_session_messages"]
    .find({"session_id": "1f898577b647"}, {"_id": 0, "role": 1, "content": 1, "_order": 1})
    .sort("_order", 1)
    .limit(4)
)
for d in docs:
    print(d)
