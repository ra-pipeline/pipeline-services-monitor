# pipeline-services-monitor

Scheduled regression tests and status dashboard for online services used by the ALMA/VLA pipeline.

Dashboard: <https://ra-pipeline.github.io/pipeline-services-monitor/>

[![Online Services Status](https://github.com/ra-pipeline/pipeline-services-monitor/actions/workflows/monitor.yml/badge.svg)](https://ra-pipeline.github.io/pipeline-services-monitor/)

The test suite checks availability, response schema, and numerical consistency across ALMA, VLA, and shared NRAO endpoints. It runs on standard Python (stdlib + `certifi`) without requiring CASA or pipeline dependencies.

## Endpoints Tested

### ALMA

- **Flux service**: `https://almascience.org/sc/flux` (primary), `https://asa.alma.cl/sc/flux` (backup)
- **Jy/K database**: `https://asa.alma.cl/science/jy-kelvins` (JAO), plus regional mirrors at EA (`almascience.nao.ac.jp`), NA (`almascience.nrao.edu`), and EU (`almascience.eso.org`)
- **Antenna position uncertainties**: `https://asa.alma.cl/uncertainties-service/uncertainties/versions/last/measurements/casa/`

### VLA

- **Antenna baseline corrections**: `http://www.vla.nrao.edu/cgi-bin/evlais_blines.cgi?Year=YYYY` (used by `hifv_priorcals`)
- **NASA CDDIS (TEC maps)**: `https://cddis.nasa.gov` (used by `hifv_tecmaps`)

### Shared Infrastructure

- **CASA rundata**: `https://go.nrao.edu/casarundata`
- **CASA measures / IERS**: `https://go.nrao.edu/iers/`

## Running Locally

Requires Python >= 3.10.

### Using uv (recommended)

`uv` automatically handles environments and dependencies defined in `pyproject.toml`:

```bash
# Run all tests
uv run pytest tests/ -v

# Run specific service suites
uv run pytest tests/test_alma_services.py -v
uv run pytest tests/test_vla_services.py -v

# Generate local dashboard and pytest-html report
uv run pytest tests/ --junit-xml=test-results.xml --html=site/report.html --self-contained-html
uv run python src/generate_dashboard.py

# Re-query live endpoints to refresh tests/reference_values.json
uv run python tests/update_references.py
```

### Using standard pip / venv

```bash
pip install .

# Run all tests
pytest tests/ -v

# Run specific service suites
pytest tests/test_alma_services.py -v
pytest tests/test_vla_services.py -v

# Generate local dashboard and pytest-html report
pytest tests/ --junit-xml=test-results.xml --html=site/report.html --self-contained-html
python src/generate_dashboard.py
```

Open `site/index.html` in a browser to view the dashboard.

## Automation

A GitHub Actions workflow runs every 6 hours (`0 */6 * * *`):

1. Runs the test suite, producing `test-results.xml` and `site/report.html`.
2. Appends test results to `history.json` and commits them to a dedicated `data` branch for permanent storage.
3. Compiles `site/index.html` and deploys the dashboard directly to GitHub Pages via `actions/deploy-pages`.
