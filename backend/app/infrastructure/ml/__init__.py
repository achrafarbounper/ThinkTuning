# project/app/infrastructure/ml/__init__.py
"""Adaptateurs ML — implémentations des ports de prédiction / dépôt de modèles.

Les adaptateurs délègent au legacy (``core.predictor_cache``,
``core.model_versioning``, ``src.inference.predictor``) par attribut de
MODULE (jamais import « par valeur ») : les monkeypatchs des tests
(``monkeypatch.setattr("core.model_versioning.MODEL_ROOT", ...)``) et les
rechargements runtime restent effectifs.
"""
