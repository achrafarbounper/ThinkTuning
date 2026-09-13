"""Utilitaires purs du domaine — zéro I/O, zéro dépendance framework.

- ``encoding``  : réparation conservatrice des doubles-encodages UTF-8 ;
- ``thinking``  : extraction des balises thinking inline du flux LLM ;
- ``json_parser`` : extraction tolérante des blocs JSON des réponses LLM.

Historique : ces modules vivaient dans ``ia/agent/`` ; ils migrent sous le
domaine (fonctions pures testables sans infrastructure) lors de la fusion
du legacy dans le noyau hexagonal.
"""
