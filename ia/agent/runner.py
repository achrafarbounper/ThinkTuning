class AgentRunner:
    """Fine enveloppe autour d'AgentCore consommée par core.agent_cache.

    ``ask`` conserve le comportement historique (réponse finale seule, str).
    ``ask_detailed`` expose en plus la trace de réflexion collectée quand le
    mode « Réflexion » est activé (AgentCore(enable_thinking=True)).
    """

    def __init__(self, agent):
        self.agent = agent

    def ask(self, prompt: str):
        return self.agent.run(prompt)

    def ask_detailed(self, prompt: str):
        """Exécute le run complet et renvoie AgentResult(answer, thinking)."""
        return self.agent.run_detailed(prompt)

    def ask_detailed_streaming(self, prompt: str, on_thinking=None):
        """Comme ``ask_detailed``, mais la réflexion est diffusée EN TEMPS RÉEL.

        ``on_thinking`` (optionnel) est invoqué pour chaque fragment de la
        trace de raisonnement dès sa production par le LLM (stream).
        """
        return self.agent.run_detailed(prompt, on_thinking=on_thinking)
