# Sample Black Box Oracles for CLARITY

This directory contains pre-built black box models and simulation functions that you can directly upload and test through the CLARITY frontend at [`/oracles`](http://localhost:5173/oracles).

---

### 1. `catalytic_reactor.py` (Python Function)
Simulates an exothermic catalytic conversion yield curve as a function of temperature.

- **File**: `sample_blackboxes/catalytic_reactor.py`
- **Name**: `Catalytic Reactor Simulator`
- **Kind**: Python function (`.py`)
- **Entrypoint**: `simulate` (or `predict_yield`)
- **Input Name**: `temperature` (or `x`)
- **Output Name**: `yield_pct` (or `y`)
- **Min Value**: `0.0`
- **Max Value**: `50.0`

---

### 2. `damped_oscillator.py` (Python Function)
Simulates mechanical resonance amplitude in a driven damped harmonic oscillator.

- **File**: `sample_blackboxes/damped_oscillator.py`
- **Name**: `Harmonic Resonator`
- **Kind**: Python function (`.py`)
- **Entrypoint**: `simulate` (or `evaluate`)
- **Input Name**: `frequency` (or `omega`)
- **Output Name**: `amplitude`
- **Min Value**: `0.0`
- **Max Value**: `10.0`

---

### 3. `materials_stress_model.joblib` (Scikit-Learn Model)
A trained Random Forest regressor modeling material strain response over an input parameter space.

- **File**: `sample_blackboxes/materials_stress_model.joblib`
- **Name**: `Material Stress Predictor`
- **Kind**: Joblib model (`.joblib`)
- **Input Name**: `strain_load` (or `x`)
- **Output Name**: `deformation` (or `y`)
- **Min Value**: `-10.0`
- **Max Value**: `10.0`

---

### 4. `linear_model.onnx` (ONNX Computational Graph)
An ONNX single-input/single-output scalar regression model.

- **File**: `sample_blackboxes/linear_model.onnx`
- **Name**: `Linear Baseline ONNX`
- **Kind**: ONNX graph (`.onnx`)
- **Input Name**: `x`
- **Output Name**: `y`
- **Min Value**: `-5.0`
- **Max Value**: `5.0`

---

### How to Test in the Frontend:
1. Navigate to **Oracles** in the top navigation bar (`/oracles`).
2. In the **Register Artifact** form, click **Choose File** and select one of the files from `c:\Users\saina\Videos\CLARITY\sample_blackboxes\`.
3. Fill in the suggested fields above.
4. Click **Probe & Register Black Box**. You will see the live validation probe points and receipt!
5. Go to **New Discovery Run** (`/runs/new`), select your registered black box under **Oracle Source**, and start the run!
