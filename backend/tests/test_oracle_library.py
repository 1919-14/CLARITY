"""Uploaded black-box oracle library: adapters, API, and run binding.

Coverage:
- Declared-contract validation (kind, bounds, identifiers).
- In-process execution of a Python callable, a joblib/pickle model and an ONNX
  graph, including failure containment.
- Registration probe, library CRUD, and the refusal to delete a bound artifact.
- A run bound to an uploaded artifact actually queries that artifact and records
  its observations (no silent fallback to the built-in simulator).
"""

import io
import math

import joblib
import pytest

from app.adapters.oracle.uploaded import (
    UploadedOracle,
    build_predictor,
    probe_predictor,
)
from app.domain.contracts import ExperimentInput, ObservationStatus
from app.domain.oracle_contracts import (
    OracleContractError,
    OracleKind,
    OracleSpec,
    probe_points,
    select_kind,
)
from app.services.engine_factory import default_engine_factory

FIXTURES = __import__("pathlib").Path(__file__).resolve().parent / "fixtures"
ONNX_FIXTURE = FIXTURES / "linear_blackbox.onnx"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMPLE_SIMULATION = b"def simulate(x):\n    return 3.0 * x + 1.0\n"


def write_python_artifact(directory, source=SIMPLE_SIMULATION, name="simulation.py"):
    path = directory / name
    path.write_bytes(source)
    return path


class LinearEstimator:
    """Minimal estimator exposing the predict() shape a joblib model would."""

    def predict(self, rows):
        return [2.0 * float(row[0]) + 5.0 for row in rows]


def upload_oracle(client, *, content=SIMPLE_SIMULATION, filename="simulation.py", **fields):
    data = {"file": (io.BytesIO(content), filename)}
    data.update({key: str(value) for key, value in fields.items()})
    return client.post("/api/v1/oracles", data=data, content_type="multipart/form-data")


def make_input(value, variable="x"):
    return ExperimentInput(input_id="inp-test", values={variable: value}, domain="test")


# ---------------------------------------------------------------------------
# Declared contract
# ---------------------------------------------------------------------------


class TestOracleContract:
    def test_extension_selects_the_artifact_kind(self):
        assert select_kind("sim.py") is OracleKind.PYTHON_FUNCTION
        assert select_kind("model.joblib") is OracleKind.JOBLIB_MODEL
        assert select_kind("model.pkl") is OracleKind.JOBLIB_MODEL
        assert select_kind("model.onnx") is OracleKind.ONNX_MODEL
        with pytest.raises(OracleContractError):
            select_kind("model.h5")

    def test_python_artifact_requires_an_entrypoint(self, tmp_path):
        with pytest.raises(OracleContractError, match="entrypoint"):
            OracleSpec(
                kind=OracleKind.PYTHON_FUNCTION, file_path=str(write_python_artifact(tmp_path))
            )

    def test_bounds_must_be_finite_and_ordered(self, tmp_path):
        path = str(write_python_artifact(tmp_path))
        with pytest.raises(OracleContractError, match="strictly smaller"):
            OracleSpec(
                kind=OracleKind.PYTHON_FUNCTION,
                file_path=path,
                entrypoint="simulate",
                min_value=2.0,
                max_value=2.0,
            )
        with pytest.raises(OracleContractError, match="finite"):
            OracleSpec(
                kind=OracleKind.PYTHON_FUNCTION,
                file_path=path,
                entrypoint="simulate",
                min_value=-1.0,
                max_value=math.inf,
            )

    def test_identifiers_must_be_python_names(self, tmp_path):
        path = str(write_python_artifact(tmp_path))
        with pytest.raises(OracleContractError):
            OracleSpec(
                kind=OracleKind.PYTHON_FUNCTION,
                file_path=path,
                entrypoint="not a function",
            )
        with pytest.raises(OracleContractError):
            OracleSpec(
                kind=OracleKind.PYTHON_FUNCTION,
                file_path=path,
                entrypoint="simulate",
                input_name="1x",
            )

    def test_probe_points_cover_the_declared_domain(self):
        assert probe_points(-2.0, 2.0) == (-2.0, 0.0, 2.0)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class TestUploadedAdapters:
    def test_python_function_is_queried_in_process(self, tmp_path):
        spec = OracleSpec(
            kind=OracleKind.PYTHON_FUNCTION,
            file_path=str(write_python_artifact(tmp_path)),
            entrypoint="simulate",
            min_value=-5.0,
            max_value=5.0,
        )
        oracle = UploadedOracle(spec)

        observation = oracle.query(make_input(2.0))

        assert observation.status is ObservationStatus.SUCCESS
        assert observation.output_values == {"y": 7.0}
        assert observation.sequence_number == 1
        assert oracle.query_count == 1

    def test_input_outside_the_declared_bounds_is_rejected_without_running(self, tmp_path):
        spec = OracleSpec(
            kind=OracleKind.PYTHON_FUNCTION,
            file_path=str(write_python_artifact(tmp_path)),
            entrypoint="simulate",
            min_value=-1.0,
            max_value=1.0,
        )
        oracle = UploadedOracle(spec)

        observation = oracle.query(make_input(2.0))

        assert observation.status is ObservationStatus.INVALID_INPUT
        assert observation.error.code == "OUT_OF_BOUNDS"
        assert oracle.query_count == 0

    def test_artifact_exception_becomes_a_contained_oracle_failure(self, tmp_path):
        source = b"def simulate(x):\n    raise ValueError('boom in researcher code')\n"
        spec = OracleSpec(
            kind=OracleKind.PYTHON_FUNCTION,
            file_path=str(write_python_artifact(tmp_path, source=source)),
            entrypoint="simulate",
        )
        oracle = UploadedOracle(spec)

        observation = oracle.query(make_input(1.0))

        assert observation.status is ObservationStatus.ORACLE_FAILURE
        assert "boom in researcher code" in observation.error.message
        assert observation.output_values is None

    def test_missing_entrypoint_is_a_clear_contract_error(self, tmp_path):
        spec = OracleSpec(
            kind=OracleKind.PYTHON_FUNCTION,
            file_path=str(write_python_artifact(tmp_path)),
            entrypoint="does_not_exist",
        )
        with pytest.raises(OracleContractError, match="does_not_exist"):
            build_predictor(spec)

    def test_joblib_model_predicts_a_scalar(self, tmp_path):
        model_path = tmp_path / "linear.joblib"
        joblib.dump(LinearEstimator(), model_path)
        spec = OracleSpec(kind=OracleKind.JOBLIB_MODEL, file_path=str(model_path))

        observation = UploadedOracle(spec).query(make_input(4.0))

        assert observation.status is ObservationStatus.SUCCESS
        assert observation.output_values == {"y": 13.0}

    def test_onnx_model_predicts_a_scalar(self):
        spec = OracleSpec(kind=OracleKind.ONNX_MODEL, file_path=str(ONNX_FIXTURE))

        observation = UploadedOracle(spec).query(make_input(2.0))

        assert observation.status is ObservationStatus.SUCCESS
        assert observation.output_values == {"y": 7.0}

    def test_probe_records_observed_values_at_declared_inputs(self, tmp_path):
        spec = OracleSpec(
            kind=OracleKind.PYTHON_FUNCTION,
            file_path=str(write_python_artifact(tmp_path)),
            entrypoint="simulate",
            min_value=-2.0,
            max_value=2.0,
        )

        samples = probe_predictor(spec)

        assert [round(sample.x, 6) for sample in samples] == [-2.0, 0.0, 2.0]
        assert [sample.y for sample in samples] == [-5.0, 1.0, 7.0]
        assert all(not sample.error for sample in samples)


# ---------------------------------------------------------------------------
# Library API
# ---------------------------------------------------------------------------


class TestOracleLibraryApi:
    @pytest.fixture(autouse=True)
    def storage_dir(self, app, tmp_path):
        app.config["ORACLE_STORAGE_DIR"] = str(tmp_path / "oracles")
        return app.config["ORACLE_STORAGE_DIR"]

    def test_upload_registers_a_probed_artifact(self, client):
        response = upload_oracle(
            client,
            name="Growth simulator",
            entrypoint="simulate",
            min_value=-2,
            max_value=2,
        )

        assert response.status_code == 201, response.data[:400]
        oracle = response.get_json()["oracle"]
        assert oracle["kind"] == "python_function"
        assert oracle["name"] == "Growth simulator"
        assert oracle["entrypoint"] == "simulate"
        assert oracle["min_value"] == -2.0
        assert oracle["max_value"] == 2.0
        assert oracle["probe"]["ok_count"] == 3
        assert len(oracle["sha256"]) == 64
        assert oracle["run_count"] == 0
        # Server paths must never leave the backend.
        assert "storage_path" not in oracle
        assert "file_path" not in oracle

        listing = client.get("/api/v1/oracles").get_json()
        assert listing["count"] == 1
        assert listing["oracles"][0]["oracle_id"] == oracle["oracle_id"]

    def test_upload_rejects_a_broken_artifact_and_writes_nothing(self, app, client, storage_dir):
        source = b"def simulate(x):\n    return 'not a number'\n"

        response = upload_oracle(client, content=source, entrypoint="simulate")

        assert response.status_code == 422
        assert "usable scalar" in response.get_json()["message"]
        assert client.get("/api/v1/oracles").get_json()["count"] == 0
        assert not __import__("os").path.isdir(storage_dir) or not __import__("os").listdir(
            storage_dir
        )

    def test_upload_rejects_missing_function_and_unsupported_type(self, client):
        missing = upload_oracle(client, entrypoint="nope")
        assert missing.status_code == 422
        assert "nope" in missing.get_json()["message"]

        unsupported = upload_oracle(client, filename="model.h5", content=b"\x00\x01")
        assert unsupported.status_code == 422
        assert "Unsupported artifact type" in unsupported.get_json()["message"]

        no_file = client.post("/api/v1/oracles", data={}, content_type="multipart/form-data")
        assert no_file.status_code == 400

    def test_declared_kind_must_match_the_extension(self, client):
        response = upload_oracle(client, kind="onnx_model", entrypoint="simulate")

        assert response.status_code == 422
        assert "extension indicates" in response.get_json()["message"]

    def test_delete_removes_an_unused_artifact(self, client):
        oracle_id = upload_oracle(client, entrypoint="simulate").get_json()["oracle"][
            "oracle_id"
        ]

        deleted = client.delete(f"/api/v1/oracles/{oracle_id}")

        assert deleted.status_code == 200
        assert client.get(f"/api/v1/oracles/{oracle_id}").status_code == 404
        assert client.delete("/api/v1/oracles/oracle-missing").status_code == 404


# ---------------------------------------------------------------------------
# Run binding and execution
# ---------------------------------------------------------------------------


class TestRunOracleBinding:
    @pytest.fixture(autouse=True)
    def wiring(self, app, tmp_path):
        app.config["ORACLE_STORAGE_DIR"] = str(tmp_path / "oracles")
        app.config["RUN_ENGINE_FACTORY"] = default_engine_factory

    def test_run_rejects_an_unknown_oracle(self, client):
        response = client.post(
            "/api/v1/runs", json={"name": "Orphan binding", "oracle_id": "oracle-nope"}
        )

        assert response.status_code == 422
        details = response.get_json()["details"]
        assert details[0]["field"] == "oracle_id"

    def test_bound_run_queries_the_uploaded_artifact(self, client):
        oracle_id = upload_oracle(
            client, entrypoint="simulate", min_value=-2, max_value=2
        ).get_json()["oracle"]["oracle_id"]

        created = client.post(
            "/api/v1/runs",
            json={
                "name": "Distil my simulator",
                "max_queries": 6,
                "oracle_id": oracle_id,
            },
        )
        assert created.status_code == 201, created.data[:400]
        run = created.get_json()["run"]
        assert run["oracle_id"] == oracle_id
        assert run["oracle"]["kind"] == "python_function"
        assert run["oracle"]["name"] == "simulation"

        started = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert started.status_code == 200, started.data[:600]
        assert started.get_json()["execution"]["dispatched"] is True

        detail = client.get(f"/api/v1/runs/{run['run_id']}").get_json()["run"]
        assert detail["status"] != "failed"

        observations = client.get(
            f"/api/v1/runs/{run['run_id']}/observations"
        ).get_json()["observations"]
        successful = [obs for obs in observations if obs["status"] == "success"]
        assert successful, "the uploaded oracle produced no successful observations"
        for observation in successful:
            x = observation["input_values"]["x"]
            y = observation["output_values"]["y"]
            # Every recorded value must be the artifact's own response.
            assert abs(y - (3.0 * x + 1.0)) < 1e-5

    def test_bound_artifact_cannot_be_deleted_while_a_run_uses_it(self, client):
        oracle_id = upload_oracle(client, entrypoint="simulate").get_json()["oracle"][
            "oracle_id"
        ]
        client.post(
            "/api/v1/runs", json={"name": "Bound run", "oracle_id": oracle_id}
        )

        response = client.delete(f"/api/v1/oracles/{oracle_id}")

        assert response.status_code == 409
        assert "bound to 1 run" in response.get_json()["message"]
