"""Drive setup_account.py non-interactively against the fake server."""
import builtins, getpass, sys
ROOT = sys.argv[1]; answers = sys.argv[2:]
sys.path.insert(0, ROOT)
from fastmcp import Client
import connection, setup_account
setup_account.ACCOUNT_URL = "http://127.0.0.1:8790/mcp"
connection.datavera_client = lambda: Client("http://127.0.0.1:8790/mcp")
getpass.getpass = lambda prompt="": "dv_sk_" + "a" * 64
it = iter(answers)
builtins.input = lambda prompt="": (print(prompt + "<answer>"), next(it, ""))[1]
sys.argv = ["setup_account.py"]
setup_account.main()
