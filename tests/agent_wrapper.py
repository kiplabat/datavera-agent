import sys
ROOT = sys.argv.pop(1)
sys.path.insert(0, ROOT)
from fastmcp import Client
import agent
agent.datavera_client_factory = lambda: Client("http://127.0.0.1:8790/mcp")
sys.argv = ["agent.py"] + sys.argv[1:]
sys.exit(agent.main())
