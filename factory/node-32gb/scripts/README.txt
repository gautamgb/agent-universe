Builder execution is implemented in Python: factory/node-32gb/src/builder_agent.py (subprocess.Popen + docker run).
The previous shell wrapper is intentionally not used for the LangGraph builder node.
