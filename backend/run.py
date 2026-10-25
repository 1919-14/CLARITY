import os
from dotenv import load_dotenv
from app import create_app

# Load environment variables from .env if present
load_dotenv()

env_name = os.environ.get("FLASK_ENV", "development")
app = create_app(env_name)

# Default DiscoveryEngine factory for development runs. It builds the built-in
# polynomial simulator unless the run is bound to an uploaded black box, in which
# case the artifact's declared contract drives the oracle and search bounds.
from app.services.engine_factory import default_engine_factory

app.config["RUN_ENGINE_FACTORY"] = default_engine_factory

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    debug = app.config.get("DEBUG", False)

    print(f"Starting CLARITY Backend on http://{host}:{port} (env: {env_name}, debug: {debug})")
    app.run(host=host, port=port, debug=debug)
