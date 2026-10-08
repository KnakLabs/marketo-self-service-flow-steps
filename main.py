from dotenv import load_dotenv
load_dotenv()  # reads .env in the repo root; a no-op when the vars are already set by the host

import importlib  # noqa: E402

from flask import Flask  # noqa: E402

app = Flask(__name__)

# Each service is registered independently, so a service whose dependencies are
# missing is skipped with a warning rather than taking the whole app down.
SERVICES = ["hearSource", "generateTicket"]

for name in SERVICES:
    try:
        module = importlib.import_module(f"services.{name}.routes")
        app.register_blueprint(module.bp)
        print(f"[ok]      /{name}")
    except Exception as exc:
        print(f"[skipped] /{name}  ({type(exc).__name__}: {exc})")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").getenv("PORT", 3000)))
