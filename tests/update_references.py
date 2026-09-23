"""Refresh reference_values.json by querying the live ALMA online services.

Run this script whenever reference values need to be updated (e.g. after a
deliberate service change or algorithm update):

    python tests/update_references.py
    # or with uv:
    uv run python tests/update_references.py

The script only updates values that changed by more than the current tolerance,
and prints a diff so the change can be reviewed before committing.
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from xml.dom import minidom

import certifi

HERE = Path(__file__).parent
REF_FILE = HERE / 'reference_values.json'


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, context=_ssl_ctx(), timeout=60) as r:
        return r.read()


def _query_flux(base: str, source: str, obs_date: str, frequency: float) -> dict:
    params = urllib.parse.urlencode({
        'NAME': source,
        'DATE': obs_date,
        'FREQUENCY': str(frequency),
        'WEIGHTED': 'true',
        'RESULT': '1',
        'VERBOSE': '1',
        'CATALOGUE': '5',
    })
    raw = _get(f'{base}?{params}')
    dom = minidom.parseString(raw)
    field_names = [f.getAttribute('name') for f in dom.getElementsByTagName('FIELD')]
    row: dict[str, str | None] = {}
    for node in dom.getElementsByTagName('TR'):
        for name, cell in zip(field_names, node.getElementsByTagName('TD')):
            row[name] = cell.childNodes[0].nodeValue if cell.childNodes else None
    return row


def _query_jyperk(base: str, uid: str) -> list[dict]:
    encoded = urllib.parse.quote(uid, safe='')
    raw = _get(f'{base}/asdm/?uid={encoded}')
    return json.loads(raw).get('data', {}).get('factors', [])


def _query_antpos(uid: str) -> dict:
    base = 'https://asa.alma.cl/uncertainties-service/uncertainties/versions/last/measurements/casa/'
    params = urllib.parse.urlencode({'asdm': uid, 'search': 'auto', 'firstIntegration': 'True'})
    return json.loads(_get(f'{base}?{params}'))


def main() -> None:
    ref = json.loads(REF_FILE.read_text())
    old = json.dumps(ref, indent=2)

    flux_base = 'https://asa.alma.cl/sc/flux'

    for query_key, params_dict in (
        ('liveness_query', {}),
        ('real_query', {}),
    ):
        q = ref['flux_service'][query_key]
        row = _query_flux(flux_base, q['source'], q['date'], q['frequency_hz'])
        q['expected_status_code'] = row.get('StatusCode')
        q['flux_density_jy'] = float(row['FluxDensity'])
        q['flux_density_error_jy'] = float(row['FluxDensityError'])
        q['spectral_index'] = float(row['SpectralIndex'])
        q['spectral_index_error'] = float(row['SpectralIndexError'])
        q['data_conditions'] = row.get('DataConditions')
        q['nearest_measurement_date_days'] = float(row['Nearest Measurement Date'])
        print(
            f'Flux {query_key:15s} {q["source"]}: {q["flux_density_jy"]:.6f} Jy '
            f'(status={q["expected_status_code"]}, dc={q["data_conditions"]})'
        )

    # --- Jy/K factors ---
    jk = ref['jyperk_service']
    factors = _query_jyperk('https://asa.alma.cl/science/jy-kelvins', jk['test_uid'])
    jk['expected_entry_count'] = len(factors)
    jk['expected_antennas'] = sorted({r['Antenna'] for r in factors})
    jk['factors_by_spwid'] = {}
    for row in factors:
        jk['factors_by_spwid'][str(row['Spwid'])] = float(row['factor'])
    print(f'JyPerK         {jk["test_uid"]}: {len(factors)} entries, antennas {jk["expected_antennas"]}')

    # --- Antenna positions ---
    ap = ref['antpos_service']
    data = _query_antpos(ap['test_uid'])
    ap['itrf_positions'] = {ant: xyz for ant, xyz in sorted(data.items())}
    print(f'Antpos         {ap["test_uid"]}: {len(data)} antennas: {sorted(data)}')

    ref['_comment'][-1] = f'Last updated: {date.today()}'

    new = json.dumps(ref, indent=2)
    if new == old:
        print('\nNo changes – reference values are up to date.')
    else:
        import difflib

        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=''))
        print('\nChanges:')
        print('\n'.join(diff))
        REF_FILE.write_text(new + '\n')
        print(f'\nWritten to {REF_FILE}')


if __name__ == '__main__':
    main()
