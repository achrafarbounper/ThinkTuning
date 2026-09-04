"""Couche de persistance — absorption strangler des stores legacy (roadmap ⚙️#6).

Cible de la Phase 1 de la migration : les stores de l'agent vivent ici sous
leur identité Core v2, en trois implémentations interchangeables derrière les
ports typés (``app/domain/ports``) :

    - ``sqlite``     : sous-classes typées des stores legacy (MÊME schéma
      SQLite, zéro migration de données) + fabrique de coexistence pointant
      sur les singletons legacy (même instance, même base) ;
    - ``memory``     : fakes en mémoire conformes aux ports (tests des
      use-cases sans disque) ;

La migration physique (SQLAlchemy + Alembic, puis Postgres) remplacera
``sqlite`` sans toucher au domaine ni aux use-cases.
"""
