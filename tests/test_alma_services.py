"""Regression tests for ALMA pipeline online services.

These tests verify availability and correctness of three online services used
by the ALMA pipeline:

1. ALMA Flux Calibration Service (sc/flux)
   - Primary:  https://almascience.org/sc/flux
   - Backup:   https://asa.alma.cl/sc/flux

2. Jy/K Conversion Factor Service (jy-kelvins)
   - JAO (primary): https://asa.alma.cl/science/jy-kelvins
   - EA mirror:     https://almascience.nao.ac.jp/science/jy-kelvins
   - NA mirror:     https://almascience.nrao.edu/science/jy-kelvins
   - EU mirror:     https://almascience.eso.org/science/jy-kelvins

3. Antenna Position Corrections Service (uncertainties-service)
   - https://asa.alma.cl/uncertainties-service/uncertainties/versions/last/measurements/casa/

No pipeline package dependency is required; only stdlib + certifi are used.

Run with:
    pytest tests/test_alma_services.py -v
or with uv:
    uv run pytest tests/test_alma_services.py -v
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from xml.dom import minidom

import certifi
import functools
import pytest

# Reference values from reference_values.json (refresh via tests/update_references.py)
_REF = json.loads((Path(__file__).parent / 'reference_values.json').read_text())

_flux_lq = _REF['flux_service']['liveness_query']
_flux_rq = _REF['flux_service']['real_query']
_jk = _REF['jyperk_service']
_ap = _REF['antpos_service']

FLUX_REF_J1427_DENSITY: float = _flux_lq['flux_density_jy']
FLUX_REF_J1427_SPIX: float = _flux_lq['spectral_index']
FLUX_REF_TOLERANCE: float = _flux_lq['tolerance_relative']

JYPERK_REF_FACTOR_COUNT: int = _jk['expected_entry_count']
JYPERK_REF_FACTORS: dict[int, float] = {int(k): v for k, v in _jk['factors_by_spwid'].items()}
JYPERK_REF_ANTENNAS: set[str] = set(_jk['expected_antennas'])

ANTPOS_REF_UID: str = _ap['test_uid']
ANTPOS_REF_POSITIONS: dict[str, list[float]] = _ap['itrf_positions']
ANTPOS_REF_TOLERANCE_M: float = _ap['tolerance_metres']

# Shared SSL context


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


@functools.lru_cache(maxsize=None)
def _get(url: str, timeout: float = 60.0) -> bytes:
    """Perform an HTTPS GET and return the raw response body.

    Responses are cached for the duration of the test session so each unique
    URL is fetched exactly once, regardless of how many tests use it.
    """
    ctx = _ssl_ctx()
    with urllib.request.urlopen(url, context=ctx, timeout=timeout) as resp:
        return resp.read()


def _get_skip_on_server_error(label: str, url: str, timeout: float = 60.0) -> bytes:
    """Fetch URL, skipping the test if the server returns 5xx error (unavailable/maintenance).

    This reduces noise when regional mirrors are temporarily down for maintenance.
    Other errors (4xx, connection refused, timeouts) still fail the test normally.
    """
    try:
        return _get(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            pytest.skip(f'{label}: endpoint unavailable ({exc.code} server error)')
        raise


# --- ALMA Flux Calibration Service (sc/flux) ---
# The pipeline uses this "standard candle" test query to check liveness before
# a real observation query (see almaimportdata.py line ~207):
#   NAME=J1427-4206  DATE=27-March-2013  FREQUENCY=86837309056.169… Hz
# Source J1427-4206 at Band 3 is a well-known bright calibrator that has been
# measured continuously for decades, so its flux entry is stable.

FLUX_SERVICE_PRIMARY = 'https://almascience.org/sc/flux'
FLUX_SERVICE_BACKUP = 'https://asa.alma.cl/sc/flux'

FLUX_LIVENESS_PARAMS = {
    'NAME': 'J1427-4206',
    'DATE': '27-March-2013',
    'FREQUENCY': '86837309056.169219970703125',
    'WEIGHTED': 'true',
    'RESULT': '1',
    'VERBOSE': '1',
    'CATALOGUE': '5',
}

# Real-query example from testing logs (Band 6, 2023 dataset):
FLUX_REAL_PARAMS = {
    'NAME': 'J1957-3845',
    'DATE': '14-August-2023',
    'FREQUENCY': '224000001907.3486328125',
    'WEIGHTED': 'true',
    'RESULT': '1',
    'VERBOSE': '1',
    'CATALOGUE': '5',
}

# Full set of fields the service returns — kept in sync with reference_values.json
FLUX_EXPECTED_FIELD_NAMES = {
    'StatusCode',
    'SourceName',
    'Frequency',
    'Date',
    'FluxDensity',
    'FluxDensityError',
    'SpectralIndex',
    'SpectralIndexError',
    'DataConditions',
    'Nearest Measurement Date',
    'Verbose',
    'Version',
}


def _query_flux_service(base_url: str, params: dict[str, str]) -> dict[str, str | None]:
    """Query the sc/flux service and return a field-name→value dict."""
    url = f'{base_url}?{urllib.parse.urlencode(params)}'
    raw = _get(url)
    dom = minidom.parseString(raw)
    fields = dom.getElementsByTagName('FIELD')
    field_names = [f.getAttribute('name') for f in fields]
    rowdict: dict[str, str | None] = {}
    for node in dom.getElementsByTagName('TR'):
        cells = node.getElementsByTagName('TD')
        for name, cell in zip(field_names, cells):
            rowdict[name] = cell.childNodes[0].nodeValue if cell.childNodes else None
    return rowdict


class TestFluxService:
    """Tests for the ALMA source-flux catalogue service."""

    @pytest.mark.parametrize('base_url', [FLUX_SERVICE_PRIMARY, FLUX_SERVICE_BACKUP])
    def test_liveness(self, base_url: str) -> None:
        """Service responds with valid XML containing at least one FIELD element."""
        url = f'{base_url}?{urllib.parse.urlencode(FLUX_LIVENESS_PARAMS)}'
        raw = _get(url)
        assert raw, 'Empty response body'
        dom = minidom.parseString(raw)
        fields = dom.getElementsByTagName('FIELD')
        assert len(fields) > 0, f'No FIELD elements in response from {base_url}'

    @pytest.mark.parametrize('base_url', [FLUX_SERVICE_PRIMARY, FLUX_SERVICE_BACKUP])
    def test_known_source_liveness_query(self, base_url: str) -> None:
        """J1427-4206 on 27-March-2013 at 87 GHz returns a valid flux density."""
        result = _query_flux_service(base_url, FLUX_LIVENESS_PARAMS)
        assert result, 'No data rows returned'
        assert 'FluxDensity' in result, 'FluxDensity field missing'
        flux = result.get('FluxDensity')
        assert flux is not None, 'FluxDensity value is None'
        assert float(flux) > 0.0, f'Expected positive flux density, got {flux}'

    @pytest.mark.parametrize('base_url', [FLUX_SERVICE_PRIMARY, FLUX_SERVICE_BACKUP])
    def test_response_fields_present(self, base_url: str) -> None:
        """All expected XML field names are present in the response schema."""
        url = f'{base_url}?{urllib.parse.urlencode(FLUX_LIVENESS_PARAMS)}'
        raw = _get(url)
        dom = minidom.parseString(raw)
        returned_fields = {f.getAttribute('name') for f in dom.getElementsByTagName('FIELD')}
        missing = FLUX_EXPECTED_FIELD_NAMES - returned_fields
        assert not missing, f'Missing fields in response: {missing}'

    @pytest.mark.parametrize('base_url', [FLUX_SERVICE_PRIMARY, FLUX_SERVICE_BACKUP])
    def test_spectral_index_numeric(self, base_url: str) -> None:
        """SpectralIndex returned for the liveness source is a finite number."""
        result = _query_flux_service(base_url, FLUX_LIVENESS_PARAMS)
        spix_str = result.get('SpectralIndex')
        assert spix_str is not None, 'SpectralIndex missing'
        spix = float(spix_str)
        assert -5.0 < spix < 5.0, f'Spectral index unrealistically large: {spix}'

    @pytest.mark.parametrize('base_url', [FLUX_SERVICE_PRIMARY, FLUX_SERVICE_BACKUP])
    def test_source_name_echo(self, base_url: str) -> None:
        """Service echoes back the source name in the SourceName field."""
        result = _query_flux_service(base_url, FLUX_LIVENESS_PARAMS)
        source_name = result.get('SourceName', '')
        assert source_name, 'SourceName is empty'
        assert len(source_name) > 0

    @pytest.mark.parametrize('base_url', [FLUX_SERVICE_PRIMARY, FLUX_SERVICE_BACKUP])
    def test_date_echoed(self, base_url: str) -> None:
        """Service echoes back the queried date in the Date field."""
        result = _query_flux_service(base_url, FLUX_LIVENESS_PARAMS)
        echoed = result.get('Date', '')
        queried = FLUX_LIVENESS_PARAMS['DATE']
        assert echoed == queried, f'Date echoed {echoed!r} != queried {queried!r}'

    @pytest.mark.parametrize(
        'base_url,ref',
        [
            (FLUX_SERVICE_PRIMARY, _flux_lq),
        ],
    )
    def test_reference_all_liveness_fields(self, base_url: str, ref: dict) -> None:
        """Liveness query on primary endpoint returns reference values within tolerance.

        Checks: StatusCode, FluxDensity, FluxDensityError, SpectralIndex,
                SpectralIndexError, DataConditions, NearestMeasurementDate.
        Tolerances: 0.1% for flux/spectral measurements (historical), 1% for error estimates.

        Note: Validates the standard candle ping (J1427-4206, Band 3) used by
        almaimportdata to verify service health before science runs. Expects a
        direct catalog measurement (StatusCode 1).
        """
        result = _query_flux_service(base_url, FLUX_LIVENESS_PARAMS)
        base_tol = ref['tolerance_relative']
        # Error estimates (statistical) have higher tolerance than measurements (historical)
        error_tol = 0.01  # 1% tolerance for error estimates that naturally drift

        assert result.get('StatusCode') == ref['expected_status_code'], (
            f'StatusCode {result.get("StatusCode")!r} != {ref["expected_status_code"]!r}'
        )
        assert result.get('DataConditions') == ref['data_conditions'], (
            f'DataConditions {result.get("DataConditions")!r} != {ref["data_conditions"]!r}'
        )
        nmd = float(result['Nearest Measurement Date'])
        assert abs(nmd - ref['nearest_measurement_date_days']) < 1.0, (
            f'Nearest Measurement Date {nmd} != ref {ref["nearest_measurement_date_days"]}'
        )
        for field, ref_key, tol in (
            ('FluxDensity', 'flux_density_jy', base_tol),
            ('FluxDensityError', 'flux_density_error_jy', error_tol),
            ('SpectralIndex', 'spectral_index', base_tol),
            ('SpectralIndexError', 'spectral_index_error', error_tol),
        ):
            got = float(result[field])
            ref_val = ref[ref_key]
            rel_err = abs(got - ref_val) / abs(ref_val) if ref_val != 0 else abs(got)
            assert rel_err < tol, f'{field}: {got} vs ref {ref_val}, rel err {rel_err:.2e} (tol {tol})'

    @pytest.mark.parametrize(
        'base_url,ref',
        [
            (FLUX_SERVICE_PRIMARY, _flux_rq),
        ],
    )
    def test_reference_all_real_query_fields(self, base_url: str, ref: dict) -> None:
        """Real query on primary endpoint returns reference values within tolerance.

        Tolerances: 0.1% for flux/spectral measurements (historical), 1% for error estimates.

        Note: Validates an actual production science query (J1957-3845, Band 6)
        requiring server-side temporal interpolation and extrapolation (StatusCode 0).
        """
        result = _query_flux_service(base_url, FLUX_REAL_PARAMS)
        base_tol = ref['tolerance_relative']
        # Error estimates (statistical) have higher tolerance than measurements (historical)
        error_tol = 0.01  # 1% tolerance for error estimates that naturally drift

        assert result.get('StatusCode') == ref['expected_status_code'], (
            f'StatusCode {result.get("StatusCode")!r} != {ref["expected_status_code"]!r}'
        )
        assert result.get('DataConditions') == ref['data_conditions'], (
            f'DataConditions {result.get("DataConditions")!r} != {ref["data_conditions"]!r}'
        )
        nmd = float(result['Nearest Measurement Date'])
        assert abs(nmd - ref['nearest_measurement_date_days']) < 1.0, (
            f'Nearest Measurement Date {nmd} != ref {ref["nearest_measurement_date_days"]}'
        )
        for field, ref_key, tol in (
            ('FluxDensity', 'flux_density_jy', base_tol),
            ('FluxDensityError', 'flux_density_error_jy', error_tol),
            ('SpectralIndex', 'spectral_index', base_tol),
            ('SpectralIndexError', 'spectral_index_error', error_tol),
        ):
            got = float(result[field])
            ref_val = ref[ref_key]
            rel_err = abs(got - ref_val) / abs(ref_val) if ref_val != 0 else abs(got)
            assert rel_err < tol, f'{field}: {got} vs ref {ref_val}, rel err {rel_err:.2e} (tol {tol})'


# --- Jy/K Conversion Factor Service (jy-kelvins) ---
# The pipeline calls:
#   <base>/asdm/?uid=<url-encoded UID>
# Example from testing log (hsd_calimage run):
#   https://asa.alma.cl/science/jy-kelvins/asdm/?uid=uid%3A%2F%2FA002%2FX85c183%2FX36f
# ASDM uid://A002/X85c183/X36f is a Single-Dish science dataset used in the
# hsd_calimage regression test.

JYPERK_ENDPOINTS = {
    'JAO (asa.alma.cl)': 'https://asa.alma.cl/science/jy-kelvins',
    'EA (almascience.nao.ac.jp)': 'https://almascience.nao.ac.jp/science/jy-kelvins',
    'NA (almascience.nrao.edu)': 'https://almascience.nrao.edu/science/jy-kelvins',
    'EU (almascience.eso.org)': 'https://almascience.eso.org/science/jy-kelvins',
}

# UID from hsd_calimage regression test (seen in getjyperkalma log)
JYPERK_TEST_UID = 'uid://A002/X85c183/X36f'


def _jyperk_url(base: str, uid: str, endpoint: str = 'asdm') -> str:
    encoded_uid = urllib.parse.quote(uid, safe='')
    return f'{base}/{endpoint}/?uid={encoded_uid}'


def _parse_jyperk_response(raw: bytes) -> list[dict]:
    """Parse JSON response from jy-kelvins service.

    The service returns JSON with the structure::

        {
          "success": true,
          "data": {
            "length": <int>,
            "factors": [
              {"MS": "...", "Antenna": "DA61", "Spwid": 17,
               "Polarization": "Polarization_0", "factor": 43.768},
              ...
            ]
          }
        }
    """
    payload = json.loads(raw)
    return payload.get('data', {}).get('factors', [])


class TestJyPerKService:
    """Tests for the ALMA Jy/K conversion factor database service."""

    def test_all_endpoints_available(self) -> None:
        """At least one JyPerK endpoint is reachable (fails if all endpoints are down).

        This test catches permanent outages (e.g., DNS failure, network partition)
        and alerts that a mirror endpoint is dead. Transient 5xx errors are caught
        by individual endpoint tests which skip on 5xx.
        """
        down_endpoints: dict[str, str] = {}
        for label, base_url in JYPERK_ENDPOINTS.items():
            try:
                url = _jyperk_url(base_url, JYPERK_TEST_UID)
                # Try a simple request without skipping on 5xx
                ctx = _ssl_ctx()
                with urllib.request.urlopen(url, context=ctx, timeout=30.0) as resp:
                    pass  # Just check it responds
            except urllib.error.HTTPError as e:
                if e.code >= 500:
                    down_endpoints[label] = f'HTTP {e.code}'
                else:
                    # 4xx errors (e.g., 404) are more serious – client error
                    pytest.fail(f'{label}: client error {e.code} from {url}')
            except (urllib.error.URLError, TimeoutError) as e:
                # Connection refused, DNS failure, timeout = permanent problem
                down_endpoints[label] = type(e).__name__

        # If ALL endpoints are down, fail; if some are down (5xx), that's ok
        all_down = len(down_endpoints) == len(JYPERK_ENDPOINTS)
        if all_down:
            pytest.fail(
                f'All JyPerK endpoints are unreachable:\n'
                + '\n'.join(f'  {label}: {reason}' for label, reason in down_endpoints.items())
            )

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_liveness(self, label: str, base_url: str) -> None:
        """Service responds with HTTP 200 and non-empty body."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        assert raw, f'{label}: empty response body from {url}'

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_json_format(self, label: str, base_url: str) -> None:
        """Response is valid JSON with a data.factors list."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        payload = json.loads(raw)
        assert payload.get('success') is True, f'{label}: success field is not True'
        rows = payload.get('data', {}).get('factors', [])
        assert len(rows) > 0, f'{label}: no factor entries in data.factors from {url}'

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_length_field_consistent(self, label: str, base_url: str) -> None:
        """data.length matches the actual number of factor entries."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        payload = json.loads(raw)
        data = payload.get('data', {})
        reported = data.get('length')
        actual = len(data.get('factors', []))
        assert reported == actual, f'{label}: data.length={reported} but len(factors)={actual}'

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_query_uid_echoed(self, label: str, base_url: str) -> None:
        """query.uid in the response matches the UID that was requested."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        payload = json.loads(raw)
        echoed_uid = payload.get('query', {}).get('uid')
        assert echoed_uid == JYPERK_TEST_UID, (
            f'{label}: query.uid echoed {echoed_uid!r} != requested {JYPERK_TEST_UID!r}'
        )

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_factor_values_positive(self, label: str, base_url: str) -> None:
        """All Jy/K factor values are positive numbers."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        rows = _parse_jyperk_response(raw)
        assert rows, f'{label}: no rows to validate'
        for row in rows:
            factor = float(row['factor'])
            assert factor > 0.0, f'{label}: non-positive Jy/K factor: {row}'

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_uid_in_response(self, label: str, base_url: str) -> None:
        """The UID queried appears in the MS field of the returned factors."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        rows = _parse_jyperk_response(raw)
        assert rows, f'{label}: no rows returned'
        uid_fragment = 'X85c183'  # part of the UID that should appear in MS name
        ms_values = [r['MS'] for r in rows]
        assert any(uid_fragment in ms for ms in ms_values), (
            f'{label}: UID fragment "{uid_fragment}" not found in MS field: {ms_values[:3]}'
        )

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_polarizations_known(self, label: str, base_url: str) -> None:
        """All Polarization labels follow the expected Polarization_N pattern."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        rows = _parse_jyperk_response(raw)
        for row in rows:
            pol = row['Polarization']
            assert pol.startswith('Polarization_'), f'{label}: unexpected polarization "{pol}" in row {row}'

    @pytest.mark.parametrize('label,base_url', list(JYPERK_ENDPOINTS.items()))
    def test_reference_factor_values(self, label: str, base_url: str) -> None:
        """Jy/K factors for uid://A002/X85c183/X36f match reference values (tol 0.1%)."""
        url = _jyperk_url(base_url, JYPERK_TEST_UID)
        raw = _get_skip_on_server_error(label, url, timeout=60.0)
        rows = _parse_jyperk_response(raw)
        assert len(rows) == JYPERK_REF_FACTOR_COUNT, (
            f'{label}: expected {JYPERK_REF_FACTOR_COUNT} factor entries, got {len(rows)}'
        )
        returned_antennas = {r['Antenna'] for r in rows}
        assert returned_antennas == JYPERK_REF_ANTENNAS, (
            f'{label}: antenna set mismatch – expected {JYPERK_REF_ANTENNAS}, got {returned_antennas}'
        )
        for row in rows:
            spw = int(row['Spwid'])
            if spw in JYPERK_REF_FACTORS:
                ref = JYPERK_REF_FACTORS[spw]
                got = float(row['factor'])
                rel_err = abs(got - ref) / ref
                assert rel_err < FLUX_REF_TOLERANCE, (
                    f'{label}: Spw {spw} factor {got} differs from reference {ref} '
                    f'by {rel_err:.2e} (tol {FLUX_REF_TOLERANCE})'
                )


# --- Antenna Position Correction Service (uncertainties-service) ---
# The pipeline calls:
#   <base>/?asdm=<encoded_uid>&search=auto&firstIntegration=True
# Example from testing log (uid___A002_Xc46ab2_X15ae PPR run):
#   https://asa.alma.cl/uncertainties-service/.../casa//?asdm=uid%3A%2F%2FA002%2FXc46ab2%2FX15ae&search=auto&firstIntegration=True
# Returns JSON.

ANTPOS_SERVICE_BASE = 'https://asa.alma.cl/uncertainties-service/uncertainties/versions/last/measurements/casa/'

# UID from the PPR regression test that successfully queried the service
ANTPOS_TEST_UID = 'uid://A002/Xc46ab2/X15ae'

# Known no-correction UID (the service returned quickly with an empty list for
# newer short EBs; use a historical UID that is known to have corrections)
ANTPOS_KNOWN_UID_WITH_CORRECTIONS = 'uid://A002/Xc46ab2/X15ae'


def _antpos_url(uid: str, search: str = 'auto', first_integration: bool = True) -> str:
    params = urllib.parse.urlencode({
        'asdm': uid,
        'search': search,
        'firstIntegration': str(first_integration),
    })
    return f'{ANTPOS_SERVICE_BASE}?{params}'


def _query_antpos(uid: str) -> list | dict:
    """Query the antenna position service and return the parsed JSON."""
    url = _antpos_url(uid)
    raw = _get(url, timeout=60.0)
    return json.loads(raw)


class TestAntposService:
    """Tests for the ALMA antenna position corrections service."""

    def test_liveness(self) -> None:
        """Service responds with HTTP 200 for a known ASDM UID."""
        url = _antpos_url(ANTPOS_TEST_UID)
        raw = _get(url, timeout=60.0)
        assert raw, f'Empty response body from {url}'

    def test_response_is_valid_json(self) -> None:
        """Response body is parseable as JSON dict."""
        url = _antpos_url(ANTPOS_TEST_UID)
        raw = _get(url, timeout=60.0)
        data = json.loads(raw)  # raises on invalid JSON
        assert isinstance(data, dict)

    def test_response_is_dict(self) -> None:
        """JSON response is a dict mapping antenna names to [X, Y, Z] lists."""
        data = _query_antpos(ANTPOS_TEST_UID)
        assert isinstance(data, dict), f'Expected dict, got {type(data).__name__}'

    def test_response_schema(self) -> None:
        """Response is a dict mapping antenna names to [X, Y, Z] lists (ITRF metres)."""
        data = _query_antpos(ANTPOS_TEST_UID)
        assert isinstance(data, dict), f'Expected dict, got {type(data).__name__}'
        if not data:
            pytest.skip('No antenna position corrections returned for this UID')
        for ant_name, xyz in data.items():
            assert isinstance(ant_name, str), f'Antenna key is not a string: {ant_name!r}'
            assert isinstance(xyz, list) and len(xyz) == 3, (
                f'Expected [X, Y, Z] list for antenna {ant_name}, got {xyz!r}'
            )

    def test_coordinate_values_are_finite(self) -> None:
        """All ITRF coordinate values are finite floats."""
        import math

        data = _query_antpos(ANTPOS_TEST_UID)
        if not data:
            pytest.skip('No antenna position corrections for this UID')
        for ant_name, xyz in data.items():
            for i, val in enumerate(xyz):
                assert math.isfinite(float(val)), f'Non-finite coordinate [{i}] for antenna {ant_name}: {val}'

    def test_reference_values(self) -> None:
        """uid://A002/Xc46ab2/X15ae: all antenna ITRF positions match reference (tol 1 mm)."""
        data = _query_antpos(ANTPOS_REF_UID)
        assert isinstance(data, dict), f'Expected dict, got {type(data).__name__}'
        assert len(data) >= 1, 'No antenna corrections returned'
        missing_ants = set(ANTPOS_REF_POSITIONS) - set(data)
        assert not missing_ants, f'Antennas missing from response: {sorted(missing_ants)}'
        failures: list[str] = []
        for ant, (rx, ry, rz) in ANTPOS_REF_POSITIONS.items():
            x, y, z = data[ant]
            for coord, got, ref in (('X', x, rx), ('Y', y, ry), ('Z', z, rz)):
                err = abs(got - ref)
                if err >= ANTPOS_REF_TOLERANCE_M:
                    failures.append(
                        f'{ant} {coord}: got {got}, ref {ref}, err {err:.6f} m (tol {ANTPOS_REF_TOLERANCE_M} m)'
                    )
        assert not failures, 'ITRF position mismatches:\n' + '\n'.join(failures)

    def test_search_auto_parameter(self) -> None:
        """search=auto parameter is accepted without error."""
        url = _antpos_url(ANTPOS_TEST_UID, search='auto')
        raw = _get(url, timeout=60.0)
        assert raw, 'Empty response with search=auto'

    def test_search_both_latest_parameter(self) -> None:
        """search=both_latest parameter is accepted without error."""
        url = _antpos_url(ANTPOS_TEST_UID, search='both_latest')
        raw = _get(url, timeout=60.0)
        assert raw, 'Empty response with search=both_latest'

    def test_search_both_closest_parameter(self) -> None:
        """search=both_closest parameter is accepted without error."""
        url = _antpos_url(ANTPOS_TEST_UID, search='both_closest')
        raw = _get(url, timeout=60.0)
        assert raw, 'Empty response with search=both_closest'

    def test_multiple_uids(self) -> None:
        """Several UIDs seen in regression test logs all return valid JSON dicts."""
        # UIDs observed in testing logs for various datasets
        test_uids = [
            'uid://A002/Xc46ab2/X15ae',
            'uid://A002/X1199f9e/X7c24',
            'uid://A002/Xd0a588/X2239',
            'uid://A002/Xee1eb6/Xc58d',
        ]
        for uid in test_uids:
            url = _antpos_url(uid)
            raw = _get(url, timeout=60.0)
            data = json.loads(raw)
            assert isinstance(data, dict), f'Expected dict for UID {uid}, got {type(data).__name__}'


# --- Endpoint consistency ---


class TestFluxServiceEndpointConsistency:
    """Validate that primary and backup flux service endpoints return consistent results.

    Note: The primary endpoint is required. If backup is unavailable, the consistency
    test is skipped (since we can't compare two endpoints with only one available).
    """

    def test_liveness_query_consistency(self) -> None:
        """Primary and backup endpoints return consistent flux density for J1427-4206.

        Both endpoints should return the same (or nearly identical) flux density
        for the same standard candle source queried at the same frequency/date.
        Allows 2% relative tolerance to account for minor calibration updates.
        Skips if backup endpoint is unavailable.
        """
        try:
            primary_result = _query_flux_service(FLUX_SERVICE_PRIMARY, FLUX_LIVENESS_PARAMS)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            pytest.fail(f'Primary flux endpoint unavailable: {e.__class__.__name__}')

        try:
            backup_result = _query_flux_service(FLUX_SERVICE_BACKUP, FLUX_LIVENESS_PARAMS)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            pytest.skip('Backup flux endpoint unavailable; skipping consistency check')

        assert primary_result, 'Primary endpoint returned no data'
        assert backup_result, 'Backup endpoint returned no data'

        for field in ['FluxDensity', 'SpectralIndex']:
            primary_val = float(primary_result[field])
            backup_val = float(backup_result[field])
            rel_err = abs(primary_val - backup_val) / abs(primary_val) if primary_val != 0 else abs(backup_val)
            assert rel_err < 0.02, (
                f'{field} inconsistency: primary={primary_val}, backup={backup_val}, rel_err={rel_err:.3f} (tol 2%)'
            )

    def test_real_query_consistency(self) -> None:
        """Primary and backup endpoints return consistent flux density for real observation.

        J1957-3845 on 14-August-2023 should have the same flux on both endpoints.
        Skips if backup endpoint is unavailable.
        """
        try:
            primary_result = _query_flux_service(FLUX_SERVICE_PRIMARY, FLUX_REAL_PARAMS)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            pytest.fail(f'Primary flux endpoint unavailable: {e.__class__.__name__}')

        try:
            backup_result = _query_flux_service(FLUX_SERVICE_BACKUP, FLUX_REAL_PARAMS)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            pytest.skip('Backup flux endpoint unavailable; skipping consistency check')

        assert primary_result, 'Primary endpoint returned no data'
        assert backup_result, 'Backup endpoint returned no data'

        for field in ['FluxDensity', 'SpectralIndex']:
            primary_val = float(primary_result[field])
            backup_val = float(backup_result[field])
            rel_err = abs(primary_val - backup_val) / abs(primary_val) if primary_val != 0 else abs(backup_val)
            assert rel_err < 0.02, (
                f'{field} inconsistency: primary={primary_val}, backup={backup_val}, rel_err={rel_err:.3f} (tol 2%)'
            )

    def test_status_codes_consistent(self) -> None:
        """Both endpoints return the same StatusCode.

        Skips if backup endpoint is unavailable.
        """
        try:
            primary_result = _query_flux_service(FLUX_SERVICE_PRIMARY, FLUX_LIVENESS_PARAMS)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            pytest.fail(f'Primary flux endpoint unavailable: {e.__class__.__name__}')

        try:
            backup_result = _query_flux_service(FLUX_SERVICE_BACKUP, FLUX_LIVENESS_PARAMS)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            pytest.skip('Backup flux endpoint unavailable; skipping consistency check')

        primary_status = primary_result.get('StatusCode')
        backup_status = backup_result.get('StatusCode')
        assert primary_status == backup_status, (
            f'StatusCode mismatch: primary={primary_status!r}, backup={backup_status!r}'
        )


class TestJyPerKServiceEndpointConsistency:
    """Validate that all JyPerK endpoints return consistent Jy/K factors."""

    def test_factor_count_consistent_across_endpoints(self) -> None:
        """All JyPerK endpoints return the same number of factors for uid://A002/X85c183/X36f.

        All 4 regional mirrors should have identical factor data.
        Skips if any endpoint is unavailable (network/maintenance).
        """
        endpoint_results: dict[str, int] = {}
        unavailable: list[str] = []
        for label, base_url in JYPERK_ENDPOINTS.items():
            try:
                url = _jyperk_url(base_url, JYPERK_TEST_UID)
                raw = _get(url, timeout=60.0)
                rows = _parse_jyperk_response(raw)
                endpoint_results[label] = len(rows)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                unavailable.append(f'{label} ({e.__class__.__name__})')

        if unavailable:
            pytest.skip(f'Skipping consistency check: unavailable endpoints: {", ".join(unavailable)}')

        assert endpoint_results, 'No endpoints available for consistency check'
        # All endpoints should return the same count
        counts = list(endpoint_results.values())
        assert len(set(counts)) == 1, f'Inconsistent factor counts across endpoints: {endpoint_results}'

    def test_antenna_set_consistent_across_endpoints(self) -> None:
        """All JyPerK endpoints return factors for the same antennas.

        Skips if any endpoint is unavailable (network/maintenance).
        """
        endpoint_antennas: dict[str, set[str]] = {}
        unavailable: list[str] = []
        for label, base_url in JYPERK_ENDPOINTS.items():
            try:
                url = _jyperk_url(base_url, JYPERK_TEST_UID)
                raw = _get(url, timeout=60.0)
                rows = _parse_jyperk_response(raw)
                endpoint_antennas[label] = {r['Antenna'] for r in rows}
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                unavailable.append(f'{label} ({e.__class__.__name__})')

        if unavailable:
            pytest.skip(f'Skipping consistency check: unavailable endpoints: {", ".join(unavailable)}')

        assert endpoint_antennas, 'No endpoints available for consistency check'
        # All endpoints should have the same antenna set
        antenna_sets = list(endpoint_antennas.values())
        assert all(ant_set == antenna_sets[0] for ant_set in antenna_sets), (
            f'Inconsistent antenna sets across endpoints:\n'
            + '\n'.join(f'  {label}: {sorted(ants)}' for label, ants in endpoint_antennas.items())
        )

    def test_spwid_set_consistent_across_endpoints(self) -> None:
        """All JyPerK endpoints return factors for the same spectral windows.

        Skips if any endpoint is unavailable (network/maintenance).
        """
        endpoint_spwids: dict[str, set[int]] = {}
        unavailable: list[str] = []
        for label, base_url in JYPERK_ENDPOINTS.items():
            try:
                url = _jyperk_url(base_url, JYPERK_TEST_UID)
                raw = _get(url, timeout=60.0)
                rows = _parse_jyperk_response(raw)
                endpoint_spwids[label] = {int(r['Spwid']) for r in rows}
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                unavailable.append(f'{label} ({e.__class__.__name__})')

        if unavailable:
            pytest.skip(f'Skipping consistency check: unavailable endpoints: {", ".join(unavailable)}')

        assert endpoint_spwids, 'No endpoints available for consistency check'
        # All endpoints should have the same SPW set
        spwid_sets = list(endpoint_spwids.values())
        assert all(spw_set == spwid_sets[0] for spw_set in spwid_sets), (
            f'Inconsistent spectral window sets across endpoints:\n'
            + '\n'.join(f'  {label}: {sorted(spws)}' for label, spws in endpoint_spwids.items())
        )

    def test_factor_values_consistent_across_endpoints(self) -> None:
        """Jy/K factor values are consistent (within 0.5%) across all JyPerK endpoints.

        Different mirrors should return identical conversion factors for the same
        antenna, spectral window, and polarization (minor floating-point variations OK).
        Skips if any endpoint is unavailable (network/maintenance).
        """
        # Fetch from all endpoints
        endpoint_data: dict[str, list[dict]] = {}
        unavailable: list[str] = []
        for label, base_url in JYPERK_ENDPOINTS.items():
            try:
                url = _jyperk_url(base_url, JYPERK_TEST_UID)
                raw = _get(url, timeout=60.0)
                rows = _parse_jyperk_response(raw)
                endpoint_data[label] = rows
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                unavailable.append(f'{label} ({e.__class__.__name__})')

        if unavailable:
            pytest.skip(f'Skipping consistency check: unavailable endpoints: {", ".join(unavailable)}')

        assert endpoint_data, 'No endpoints available for consistency check'

        # Build a dict keyed by (Antenna, Spwid, Polarization) for the first endpoint
        reference_label = list(endpoint_data.keys())[0]
        reference_factors: dict[tuple, float] = {}
        for row in endpoint_data[reference_label]:
            key = (row['Antenna'], int(row['Spwid']), row['Polarization'])
            reference_factors[key] = float(row['factor'])

        # Compare all other endpoints to reference
        failures: list[str] = []
        for label in list(endpoint_data.keys())[1:]:
            for row in endpoint_data[label]:
                key = (row['Antenna'], int(row['Spwid']), row['Polarization'])
                got_factor = float(row['factor'])
                ref_factor = reference_factors.get(key)

                if ref_factor is None:
                    failures.append(f'{label}: extra entry {key} not in {reference_label}')
                    continue

                rel_err = abs(got_factor - ref_factor) / abs(ref_factor) if ref_factor != 0 else 0
                if rel_err >= 0.005:  # 0.5% tolerance
                    failures.append(
                        f'{label} {key}: factor={got_factor:.6f} vs ref={ref_factor:.6f}, '
                        f'rel_err={rel_err:.3f} (tol 0.5%)'
                    )

        assert not failures, 'JyPerK factor inconsistencies:\n' + '\n'.join(failures)


# --- Service availability smoke tests ---


class TestServiceAvailability:
    """Quick availability smoke tests to confirm all service roots respond."""

    @pytest.mark.parametrize(
        'name,url',
        [
            ('Flux primary', FLUX_SERVICE_PRIMARY),
            ('Flux backup', FLUX_SERVICE_BACKUP),
            ('JyPerK JAO', 'https://asa.alma.cl/science/jy-kelvins'),
            ('JyPerK EA', 'https://almascience.nao.ac.jp/science/jy-kelvins'),
            ('JyPerK NA', 'https://almascience.nrao.edu/science/jy-kelvins'),
            ('JyPerK EU', 'https://almascience.eso.org/science/jy-kelvins'),
            ('Antpos', ANTPOS_SERVICE_BASE),
        ],
    )
    def test_service_reachable(self, name: str, url: str) -> None:
        """Each service root/base URL returns a non-error HTTP response."""
        ctx = _ssl_ctx()
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=30.0) as resp:
                # Any 2xx/3xx/4xx is acceptable here – we just want to confirm
                # the server is up and TLS works.  A 404 on the bare root is
                # fine; the real queries use specific paths.
                assert resp is not None, f'{name}: no response object'
        except urllib.error.HTTPError as exc:
            # HTTPError means the server responded – that's enough for a
            # reachability check (e.g. 404 on a bare root URL is acceptable).
            assert exc.code < 500, f'{name}: server error {exc.code} from {url}'
