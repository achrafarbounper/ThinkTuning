import os

# Single source of truth for global runtime flags.
#
# Ces flags vivent ici (module "feuille", sans dépendances) plutôt que dans le
# package api afin que les modules bas niveau (src.*, core.*) puissent les lire
# sans importer l'application FastAPI — ce qui provoquait des imports circulaires :
#   predictor -> api -> api.main -> api.routes.predict
#   -> core.predictor_cache -> predictor (module partiellement initialisé)
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"
