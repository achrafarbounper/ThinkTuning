from agent.llm_client import LLMClient
from agent.agent_core import AgentCore
from agent.runner import AgentRunner

OLLAMA_URL = "http://192.168.1.184:11434/api/chat"
MODEL_NAME = "llama3.1:8b"

if __name__ == "__main__":
    llm = LLMClient(OLLAMA_URL, MODEL_NAME)
    agent = AgentCore(llm)
    runner = AgentRunner(agent)

    prompt = """
    1) Additionne 12 + 30 avec l'outil 'add'.
    2) Écris le résultat dans 'result.txt' avec l'outil 'write_file'.
    """

    print(runner.ask(prompt))
