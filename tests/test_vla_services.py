"""Regression tests for VLA and shared pipeline online services.

Endpoints:
  - VLA Baseline Corrections: http://www.vla.nrao.edu/cgi-bin/evlais_blines.cgi?Year=<YEAR>
  - NASA CDDIS (TEC maps): https://cddis.nasa.gov
  - CASA rundata / IERS: https://go.nrao.edu/casarundata, https://go.nrao.edu/iers/
"""

from __future__ import annotations

import datetime
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

import certifi
import pytest

# Base URL for VLA baseline corrections (allows overriding via env var)
VLA_BASELINES_BASE_URL = os.environ.get('VLA_BASELINES_URL', 'http://www.vla.nrao.edu/cgi-bin/evlais_blines.cgi?Year=')

VLA_ARCHIVE_URL = 'http://www.vla.nrao.edu/astro/archive/baselines/'

CASA_RUNDATA_URL = os.environ.get('CASA_RUNDATA_URL', 'https://go.nrao.edu/casarundata')
CASA_IERS_URL = os.environ.get('CASA_IERS_URL', 'https://go.nrao.edu/iers/')
CDDIS_URL = 'https://cddis.nasa.gov'

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']


def _get(url: str, timeout: float = 30.0) -> tuple[int, str]:
    """Execute HTTP GET and return (status_code, body_text)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'pipeline-services-monitor/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as response:
            body = response.read().decode('utf-8', errors='replace')
            return response.status, body
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        pytest.skip(f'Service unreachable ({url}): {e}')


def _parse_baseline_rows(html: str, year: int) -> list[dict[str, Any]]:
    """Parse baseline correction table rows from the VLA CGI HTML response.

    Returns a list of dicts with keys:
      year, moved_date, obs_date, put_date, put_time, ant, pad, bx, by, bz
    """
    rows = []
    lines = html.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith('<') or line.startswith(';'):
            continue

        # Look for valid month abbreviation in the line
        if not any(m in line for m in MONTHS):
            continue

        parts = line.split()
        # When 9 columns: [moved_date, obs_date, put_date, put_time, ant, pad, Bx, By, Bz]
        # When 8 columns: [obs_date, put_date, put_time, ant, pad, Bx, By, Bz]
        if len(parts) == 9:
            moved_date, obs_date, put_date, put_time_str, ant, pad, bx, by, bz = parts
        elif len(parts) == 8:
            moved_date = ''
            obs_date, put_date, put_time_str, ant, pad, bx, by, bz = parts
        else:
            continue

        try:
            rows.append({
                'year': year,
                'moved_date': moved_date,
                'obs_date': obs_date,
                'put_date': put_date,
                'put_time': put_time_str,
                'ant': int(ant),
                'pad': pad,
                'bx': float(bx),
                'by': float(by),
                'bz': float(bz),
            })
        except ValueError:
            continue

    return rows


# --- VLA baseline corrections ---


class TestVLABaselineCorrectionsService:
    """Validate the VLA baseline correction endpoint used by hifv_priorcals."""

    def test_archive_info_page_reachable(self) -> None:
        status, body = _get(VLA_ARCHIVE_URL)
        assert status == 200
        assert 'baseline' in body.lower()

    def test_historical_year_2024_liveness(self) -> None:
        status, body = _get(f'{VLA_BASELINES_BASE_URL}2024')
        assert status == 200
        assert 'EVLA baselines for 2024' in body

    def test_current_year_liveness(self) -> None:
        current_year = datetime.datetime.now(datetime.timezone.utc).year
        status, body = _get(f'{VLA_BASELINES_BASE_URL}{current_year}')
        assert status == 200
        assert f'EVLA baselines for {current_year}' in body

    def test_schema_and_column_headers(self) -> None:
        status, body = _get(f'{VLA_BASELINES_BASE_URL}2024')
        assert status == 200
        assert 'ANT' in body
        assert 'PAD' in body
        assert 'Bx' in body and 'By' in body and 'Bz' in body

    def test_parses_valid_rows(self) -> None:
        _, body = _get(f'{VLA_BASELINES_BASE_URL}2024')
        rows = _parse_baseline_rows(body, 2024)
        assert len(rows) > 0, 'Expected baseline correction rows for 2024'
        first = rows[0]
        assert 'ant' in first and isinstance(first['ant'], int)
        assert 'pad' in first and isinstance(first['pad'], str)
        assert 'bx' in first and isinstance(first['bx'], float)
        assert 'by' in first and isinstance(first['by'], float)
        assert 'bz' in first and isinstance(first['bz'], float)

    def test_offset_magnitudes_physical(self) -> None:
        _, body = _get(f'{VLA_BASELINES_BASE_URL}2024')
        rows = _parse_baseline_rows(body, 2024)
        for r in rows:
            assert abs(r['bx']) < 1.0, f'Bx offset unreasonably large: {r}'
            assert abs(r['by']) < 1.0, f'By offset unreasonably large: {r}'
            assert abs(r['bz']) < 1.0, f'Bz offset unreasonably large: {r}'

    def test_known_historical_moves_2024(self) -> None:
        _, body = _get(f'{VLA_BASELINES_BASE_URL}2024')
        rows = _parse_baseline_rows(body, 2024)
        matching = [r for r in rows if r['ant'] == 2 and r['pad'] == 'N36' and 'JAN04' in r['obs_date']]
        assert len(matching) >= 1, 'Expected antenna 2 on pad N36 in Jan 2024'
        rec = matching[0]
        assert pytest.approx(rec['bx'], abs=1e-4) == 0.0000
        assert pytest.approx(rec['by'], abs=1e-4) == -0.0237
        assert pytest.approx(rec['bz'], abs=1e-4) == -0.0115


# --- Shared infrastructure ---


class TestSharedInfrastructureServices:
    """Validate shared runtime data and auxiliary service endpoints."""

    def test_casarundata_redirect_reachable(self) -> None:
        status, _ = _get(CASA_RUNDATA_URL)
        assert status == 200

    def test_casaiers_redirect_reachable(self) -> None:
        status, _ = _get(CASA_IERS_URL)
        assert status == 200

    def test_cddis_server_reachable(self) -> None:
        status, _ = _get(CDDIS_URL)
        assert status == 200
