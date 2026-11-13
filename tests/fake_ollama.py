"""Scripted stand-in for Ollama's /api/chat that honours the `format` schema shape. NOT a real model."""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/api/tags": self._send({"models": [{"name": "qwen3.5:4b"}]})
        elif self.path == "/api/ps": self._send({"models": []})
        else: self.send_error(404)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        props = (body.get("format") or {}).get("properties", {})
        user = body["messages"][-1]["content"]
        if "choice" in props and "query" in props:
            out = {"choice": 1, "query": "glacier mass balance data csv"}
        elif "choice" in props:
            out = {"choice": 1}
        elif "measure" in props:
            m = props["measure"]["enum"]; pick = next((x for x in m if "mass" in x.lower()), m[0])
            tm = props["time"]["enum"]; cat = props["category"]["enum"]
            out = {"measure": pick, "time": tm[0], "category": "(none)" if "Country Name" not in cat else "Country Name",
                   "aggregate": "sum" if "Value" in m else "mean",
                   "measure_label": "Cumulative mass balance" if "mass" in pick.lower() else "Population",
                   "unit": "m w.e." if "mass" in pick.lower() else "people", "category_label": "Country", "tag": "Climate"}
            if "time" in props and "Year" not in tm: out["time"] = tm[0]
        elif "title" in props:
            # Deliberately includes an invented number in 'analysis' to exercise the guard.
            out = {"title": "Reference glaciers keep losing mass", "subtitle": "Cumulative balance since 1956",
                   "summary": "Reference glaciers lost mass across the record.",
                   "analysis": "Glaciers lost 99.9% of their ice, according to this text.\n\nLimitations apply."}
        elif "ok" in props:
            out = {"ok": True}
        else:
            out = {}
        LOG.append({"keys": list(props), "out": out})
        n = len(user) // 4
        self._send({"message": {"role": "assistant", "content": json.dumps(out)}, "done": True,
                    "load_duration": 1_000_000, "prompt_eval_count": n, "prompt_eval_duration": n * 30_000_000,
                    "eval_count": 30, "eval_duration": 8_000_000_000})
if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
