class AgentRunner:
    def __init__(self, agent):
        self.agent = agent

    def ask(self, prompt: str):
        return self.agent.run(prompt)
