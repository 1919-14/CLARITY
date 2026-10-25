import os
from dotenv import load_dotenv
from app import create_app

# Load environment variables from .env if present
load_dotenv()

env_name = os.environ.get("FLASK_ENV", "development")
app = create_app(env_name)

# Default DiscoveryEngine factory for development runs
from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.discovery_contracts import DiscoveryRunConfig
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import NumericalDomainBounds
from app.domain.ranking_contracts import RankingPolicyConfig


def default_engine_factory(plan):
    oracle = PolynomialLocalOracle()
    queries = plan.budget.max_oracle_queries or 10
    config = DiscoveryRunConfig(
        max_query_budget=queries,
        max_iterations=queries,
        domain_bounds=NumericalDomainBounds(
            variable_name="x", min_val=-2.0, max_val=2.0, domain_name="1d_polynomial"
        ),
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=2, candidate_count=4, seed=42
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=2, performance_acceptance_threshold=1e9
        ),
        experiment_pool_size=4,
    )
    return DiscoveryEngine(
        oracle,
        config,
        run_id=plan.run_id,
        budget=plan.budget_manager,
    )


app.config["RUN_ENGINE_FACTORY"] = default_engine_factory

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    debug = app.config.get("DEBUG", False)

    print(f"Starting CLARITY Backend on http://{host}:{port} (env: {env_name}, debug: {debug})")
    app.run(host=host, port=port, debug=debug)
