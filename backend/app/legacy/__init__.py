# -*- coding: utf-8 -*-
"""Couche legacy déplacée (ex-``backend/core``).

Les stores/runners historiques (ex-``core/``) vivent désormais sous
``app.legacy.core`` : ils restent la source de vérité des persistances et des
runners ML tant que la strangulation n'est pas terminée. Les adaptateurs
(``app/infrastructure/legacy_*.py``) et les couches hexagonales les
consomment via les ports du domaine. À absorber progressivement dans
``app/infrastructure/persistence`` et ``app/application``.
"""
