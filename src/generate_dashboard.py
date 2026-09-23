#!/usr/bin/env python3
"""Generate the HTML status dashboard and update service history from test results."""

from __future__ import annotations

import html as html_escape
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# Retain all history indefinitely (no capping/pruning)
MAX_BAR_SEGMENTS = 90  # Number of recent checks to display in the visual timeline bar


SERVICES = {
    'ALMA Services': {
        'Flux Calibration Service (Primary)': {
            'url': 'https://almascience.org/sc/flux',
            'desc': 'ALMA source catalog and calibrator flux queries (hifa_importdata)',
        },
        'Flux Calibration Service (Backup)': {
            'url': 'https://asa.alma.cl/sc/flux',
            'desc': 'Failover endpoint for ALMA flux calibrator service',
        },
        'Jy/K Factor Service (JAO Primary)': {
            'url': 'https://asa.alma.cl/science/jy-kelvins',
            'desc': 'Antenna & SPW Kelvin-to-Jansky conversion factors (hsd_k2jycal)',
        },
        'Jy/K Factor Service (EA Mirror)': {
            'url': 'https://almascience.nao.ac.jp/science/jy-kelvins',
            'desc': 'East Asia regional mirror for Jy/K factor database',
        },
        'Jy/K Factor Service (NA Mirror)': {
            'url': 'https://almascience.nrao.edu/science/jy-kelvins',
            'desc': 'North America regional mirror for Jy/K factor database',
        },
        'Jy/K Factor Service (EU Mirror)': {
            'url': 'https://almascience.eso.org/science/jy-kelvins',
            'desc': 'European regional mirror for Jy/K factor database',
        },
        'Antenna Position Corrections': {
            'url': 'https://asa.alma.cl/uncertainties-service/uncertainties/versions/last/measurements/casa/',
            'desc': 'ALMA antenna position corrections & baseline adjustments (hifa_antpos)',
        },
    },
    'VLA Services': {
        'VLA Baseline Corrections Service': {
            'url': 'http://www.vla.nrao.edu/cgi-bin/evlais_blines.cgi?Year=2026',
            'desc': 'Antenna position & baseline corrections by observation year (hifv_priorcals)',
        },
        'VLA Baseline Corrections Archive': {
            'url': 'http://www.vla.nrao.edu/astro/archive/baselines/',
            'desc': 'Public documentation and manual archive for EVLA antenna movements',
        },
        'NASA CDDIS GNSS/IONEX Service': {
            'url': 'https://cddis.nasa.gov',
            'desc': 'IGS GPS ionospheric TEC maps for delay & Faraday rotation (hifv_tecmaps / VLASS)',
        },
    },
    'Shared Pipeline Infrastructure': {
        'CASA Rundata Distribution': {
            'url': 'https://go.nrao.edu/casarundata',
            'desc': 'Runtime data, beam models, and ephemeris tables (casaconfig data auto-update)',
        },
        'CASA Measures / IERS Distribution': {
            'url': 'https://go.nrao.edu/iers/',
            'desc': 'Earth orientation parameters, leap seconds, polar motion (casaconfig measures)',
        },
    },
}


def parse_junit_results(junit_file: Path) -> dict:
    """Parse JUnit XML produced by pytest, extracting summary and all test cases."""
    empty_result = {
        'passed': 0, 'failed': 0, 'skipped': 0, 'total': 0,
        'duration': 0.0, 'failures_list': [], 'testcases': []
    }
    if not junit_file.exists():
        return empty_result

    try:
        tree = ET.parse(junit_file)
        root = tree.getroot()
        suite = root if root.tag == 'testsuite' else root.find('testsuite')
        if suite is None:
            return empty_result

        total = int(suite.get('tests', 0))
        failures = int(suite.get('failures', 0))
        errors = int(suite.get('errors', 0))
        skipped = int(suite.get('skipped', 0))
        passed = max(0, total - failures - errors - skipped)
        try:
            total_duration = float(suite.get('time', 0.0))
        except ValueError:
            total_duration = 0.0

        failures_list = []
        testcases = []

        for tc in suite.iter('testcase'):
            raw_classname = tc.get('classname', '')
            classname = raw_classname.split('.')[-1]
            name = tc.get('name', '')
            try:
                duration = float(tc.get('time', 0.0))
            except ValueError:
                duration = 0.0

            fail = tc.find('failure') if tc.find('failure') is not None else tc.find('error')
            skip = tc.find('skipped')

            if fail is not None:
                status = 'failed'
                error_text = (fail.get('message') or '') + '\n' + (fail.text or '')
                error_text = error_text.strip()
                first_line = error_text.splitlines()[0][:140] if error_text else 'Test failed'
                failures_list.append(f"{classname}.{name}: {first_line}")
            elif skip is not None:
                status = 'skipped'
                error_text = (skip.get('message') or skip.text or '').strip()
            else:
                status = 'passed'
                error_text = ''

            testcases.append({
                'classname': classname,
                'name': name,
                'duration': duration,
                'status': status,
                'error': error_text,
            })

        return {
            'passed': passed,
            'failed': failures + errors,
            'skipped': skipped,
            'total': total,
            'duration': total_duration,
            'failures_list': failures_list,
            'testcases': testcases,
        }
    except Exception as e:
        print(f"Warning: Failed to parse JUnit XML {junit_file}: {e}", file=sys.stderr)
        return empty_result


def update_history(history_file: Path, current_results: dict) -> list[dict]:
    """Load existing history, append the current run, and persist to history_file."""
    history: list[dict] = []
    if history_file.exists():
        try:
            content = history_file.read_text(encoding='utf-8').strip()
            if content:
                loaded = json.loads(content)
                if isinstance(loaded, list):
                    history = loaded
        except Exception as e:
            print(f"Warning: Could not read history file {history_file}: {e}", file=sys.stderr)

    now_iso = datetime.now(timezone.utc).isoformat()
    now_display = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    repo = os.environ.get('GITHUB_REPOSITORY', '')

    current_entry = {
        'timestamp': now_iso,
        'date_display': now_display,
        'passed': current_results['passed'],
        'failed': current_results['failed'],
        'skipped': current_results['skipped'],
        'total': current_results['total'],
        'duration': round(current_results.get('duration', 0.0), 2),
        'status': 'failed' if current_results['failed'] > 0 else 'operational',
        'run_id': run_id,
        'run_url': f"https://github.com/{repo}/actions/runs/{run_id}" if run_id and repo else '',
    }

    # Avoid exact duplicate timestamp entries if script is run rapidly
    if history and history[-1].get('date_display') == now_display:
        history[-1] = current_entry
    else:
        history.append(current_entry)

    # Retain all history indefinitely in history.json (no pruning)
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(json.dumps(history, indent=2), encoding='utf-8')
    except Exception as e:
        print(f"Warning: Failed to write history file {history_file}: {e}", file=sys.stderr)

    return history


def generate_html(results: dict, history: list[dict], has_html_report: bool = True) -> str:
    """Render HTML dashboard."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    has_failures = results['failed'] > 0
    status_class = 'status-failed' if has_failures else 'status-operational'
    status_text = 'SERVICE DISRUPTIONS DETECTED' if has_failures else 'ALL SYSTEMS OPERATIONAL'

    # Calculate all-time uptime percentage across entire history
    total_checks = len(history)
    if total_checks > 0:
        operational_runs = sum(1 for h in history if h.get('status') == 'operational')
        all_time_uptime_pct = (operational_runs / total_checks) * 100.0
        all_time_uptime_str = f"{all_time_uptime_pct:.1f}%"
    else:
        all_time_uptime_str = "100.0%"

    report_nav_link = ''
    if has_html_report:
        report_nav_link = '<a href="./report.html" class="nav-link" target="_blank" rel="noopener noreferrer">Pytest Diagnostic Report &rarr;</a>'

    # Failure alert section
    failure_section = ''
    if results['failures_list']:
        items = '\n'.join(f'<li><code>{html_escape.escape(item)}</code></li>' for item in results['failures_list'])
        failure_section = f'''
        <div class="admonition admonition-danger">
            <div class="admonition-title">Service Regressions Detected</div>
            <div class="admonition-content">
                <ul>
                    {items}
                </ul>
            </div>
        </div>
        '''

    # Build Uptime History Bar for the most recent checks (up to MAX_BAR_SEGMENTS)
    recent_history = history[-MAX_BAR_SEGMENTS:] if len(history) > MAX_BAR_SEGMENTS else history
    history_bars_html = ''
    for h in recent_history:
        h_status = h.get('status', 'operational')
        bar_class = 'bar-operational' if h_status == 'operational' else 'bar-failed'
        h_date = h.get('date_display', 'Unknown date')
        h_passed = h.get('passed', 0)
        h_failed = h.get('failed', 0)
        h_dur = h.get('duration', 0.0)
        tooltip = f"{h_date}: {h_passed} Passed, {h_failed} Failed ({h_dur:.1f}s)"

        history_bars_html += f'<div class="history-bar-segment {bar_class}" title="{html_escape.escape(tooltip)}"></div>'

    history_section_html = f'''
    <div class="card">
        <div class="card-header-flex">
            <h2 class="card-title-plain">Service Availability Timeline</h2>
            <div class="history-stat-label">All-Time Uptime: <strong>{all_time_uptime_str}</strong></div>
        </div>
        <div class="history-bar-container">
            {history_bars_html}
        </div>
        <div class="history-bar-legend">
            <span>Recent {len(recent_history)} checks</span>
            <span>Total recorded checks: {total_checks}</span>
            <span>Current</span>
        </div>
    </div>
    '''

    # Monitored services tables
    services_cards = ''
    for category, endpoints in SERVICES.items():
        rows = ''
        for name, info in endpoints.items():
            url = info['url']
            desc = info['desc']
            rows += f'''
            <tr>
                <td style="width: 45%;">
                    <div class="service-name">{name}</div>
                    <div class="service-desc">{desc}</div>
                </td>
                <td><a href="{url}" target="_blank" rel="noopener noreferrer"><code>{url}</code></a></td>
                <td style="width: 110px; text-align: right;"><span class="badge badge-passed">ONLINE</span></td>
            </tr>
            '''
        services_cards += f'''
        <div class="card">
            <h2 class="card-title">{category}</h2>
            <div class="table-container">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Service</th>
                            <th>Endpoint URL</th>
                            <th style="text-align: right;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                    </tbody>
                </table>
            </div>
        </div>
        '''

    # Individual testcases rows
    testcases_html = ''
    for tc in results['testcases']:
        status = tc['status']
        dur_str = f"{tc['duration']:.3f}s" if tc['duration'] > 0 else "<0.001s"

        error_block = ''
        if tc['error']:
            escaped_err = html_escape.escape(tc['error'])
            error_block = f'''
            <details class="test-error-details">
                <summary>View Error Traceback</summary>
                <pre class="traceback-pre"><code>{escaped_err}</code></pre>
            </details>
            '''

        escaped_name = html_escape.escape(tc['name'])
        escaped_class = html_escape.escape(tc['classname'])

        testcases_html += f'''
        <tr class="test-row" data-status="{status}" data-search="{escaped_class.lower()} {escaped_name.lower()}">
            <td style="width: 100px;">
                <span class="badge badge-{status}">{status.upper()}</span>
            </td>
            <td>
                <div class="test-class-label">{escaped_class}</div>
                <div class="test-name-label"><code>{escaped_name}</code></div>
                {error_block}
            </td>
            <td style="width: 100px; text-align: right; font-family: var(--md-code-font); font-size: 0.825rem; color: var(--md-text-muted);">
                {dur_str}
            </td>
        </tr>
        '''

    # Recent runs table (collapsible)
    runs_rows = ''
    for h in reversed(history[-15:]):
        h_status = h.get('status', 'operational')
        h_badge = f'<span class="badge badge-{"passed" if h_status == "operational" else "failed"}">{h_status.upper()}</span>'
        h_run_id = h.get('run_id', '')
        h_run_link = f'<a href="{h["run_url"]}" target="_blank">#{h_run_id}</a>' if h.get('run_url') else (f'#{h_run_id}' if h_run_id else 'local')

        runs_rows += f'''
        <tr>
            <td><code>{h.get('date_display', '')}</code></td>
            <td>{h_badge}</td>
            <td>{h.get('passed', 0)}</td>
            <td>{h.get('failed', 0)}</td>
            <td style="font-family: var(--md-code-font);">{h.get('duration', 0.0):.1f}s</td>
            <td>{h_run_link}</td>
        </tr>
        '''

    html = f'''<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pipeline Online Services Monitor</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">
    <style>
        :root {{
            --md-text-font: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            --md-code-font: "JetBrains Mono", SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --md-primary: #3f51b5;
            --md-primary-dark: #303f9f;
            --md-primary-light: #5c6bc0;
            --md-bg: #f8fafc;
            --md-surface: #ffffff;
            --md-text: #263238;
            --md-text-muted: #607d8b;
            --md-border: #e0e0e0;
            --md-code-bg: #f1f5f9;
            --md-success: #2e7d32;
            --md-success-bg: #e8f5e9;
            --md-danger: #c62828;
            --md-danger-bg: #ffebee;
            --md-warning: #ef6c00;
            --md-warning-bg: #fff3e0;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: var(--md-text-font);
            background-color: var(--md-bg);
            color: var(--md-text);
            line-height: 1.55;
            font-size: 0.9375rem;
            -webkit-font-smoothing: antialiased;
        }}
        /* Header / App Bar */
        .app-bar {{
            background-color: var(--md-primary);
            color: #ffffff;
            padding: 0 24px;
            height: 56px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 1px 4px rgba(0,0,0,0.12);
        }}
        .app-bar-brand {{
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 1.05rem;
            font-weight: 600;
            letter-spacing: 0.2px;
        }}
        .app-bar-brand span.subtitle {{
            font-weight: 400;
            color: #e0e7ff;
            font-size: 0.875rem;
        }}
        .nav-link {{
            color: #ffffff;
            text-decoration: none;
            font-size: 0.875rem;
            font-weight: 500;
            padding: 6px 14px;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.12);
            transition: background 0.15s ease;
        }}
        .nav-link:hover {{
            background: rgba(255, 255, 255, 0.22);
            color: #ffffff;
        }}
        /* Main Layout */
        .container {{
            max-width: 1040px;
            margin: 32px auto;
            padding: 0 20px;
        }}
        .status-hero {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .status-indicator {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-weight: 600;
            font-size: 0.9rem;
            letter-spacing: 0.4px;
            padding: 6px 14px;
            border-radius: 4px;
        }}
        .status-indicator::before {{
            content: "";
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }}
        .status-operational {{
            background: var(--md-success-bg);
            color: var(--md-success);
            border: 1px solid #c8e6c9;
        }}
        .status-operational::before {{
            background: var(--md-success);
        }}
        .status-failed {{
            background: var(--md-danger-bg);
            color: var(--md-danger);
            border: 1px solid #ffcdd2;
        }}
        .status-failed::before {{
            background: var(--md-danger);
        }}
        .timestamp {{
            font-size: 0.8125rem;
            color: var(--md-text-muted);
        }}
        /* Metrics Grid */
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-bottom: 28px;
        }}
        @media (max-width: 768px) {{
            .metrics-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
        .metric-card {{
            background: var(--md-surface);
            border-radius: 6px;
            padding: 18px 20px;
            border: 1px solid var(--md-border);
            text-align: left;
        }}
        .metric-value {{
            font-size: 2rem;
            font-weight: 700;
            line-height: 1.1;
            font-feature-settings: "tnum";
            font-variant-numeric: tabular-nums;
        }}
        .metric-label {{
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--md-text-muted);
            text-transform: uppercase;
            letter-spacing: 0.6px;
            margin-top: 4px;
        }}
        /* Card Containers */
        .card {{
            background: var(--md-surface);
            border-radius: 6px;
            border: 1px solid var(--md-border);
            margin-bottom: 24px;
            overflow: hidden;
        }}
        .card-header-flex {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 20px;
            border-bottom: 1px solid var(--md-border);
            background: #fafafa;
        }}
        .card-title {{
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--md-text);
            padding: 16px 20px;
            border-bottom: 1px solid var(--md-border);
            background: #fafafa;
        }}
        .card-title-plain {{
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--md-text);
        }}
        .history-stat-label {{
            font-size: 0.875rem;
            color: var(--md-text-muted);
        }}
        .history-stat-label strong {{
            color: var(--md-success);
        }}
        /* Uptime History Bar */
        .history-bar-container {{
            display: flex;
            align-items: center;
            gap: 2px;
            padding: 20px 20px 10px 20px;
            height: 60px;
        }}
        .history-bar-segment {{
            flex: 1;
            height: 32px;
            border-radius: 2px;
            cursor: pointer;
            transition: opacity 0.15s ease, transform 0.15s ease;
        }}
        .history-bar-segment:hover {{
            opacity: 0.8;
            transform: scaleY(1.15);
        }}
        .bar-operational {{
            background-color: var(--md-success);
        }}
        .bar-failed {{
            background-color: var(--md-danger);
        }}
        .history-bar-legend {{
            display: flex;
            justify-content: space-between;
            padding: 0 20px 16px 20px;
            font-size: 0.75rem;
            color: var(--md-text-muted);
            border-top: 1px solid #f1f5f9;
            padding-top: 10px;
        }}
        /* Tables */
        .table-container {{
            overflow-x: auto;
        }}
        .data-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.875rem;
        }}
        .data-table th, .data-table td {{
            padding: 12px 20px;
            border-bottom: 1px solid var(--md-border);
            text-align: left;
            vertical-align: middle;
        }}
        .data-table th {{
            background-color: #ffffff;
            color: var(--md-text-muted);
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 2px solid var(--md-border);
        }}
        .data-table tr:last-child td {{
            border-bottom: none;
        }}
        .service-name {{
            font-weight: 600;
            color: var(--md-text);
        }}
        .service-desc {{
            font-size: 0.8rem;
            color: var(--md-text-muted);
            margin-top: 2px;
        }}
        /* Badges */
        .badge {{
            display: inline-block;
            font-size: 0.6875rem;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 3px;
            letter-spacing: 0.4px;
            text-transform: uppercase;
        }}
        .badge-passed {{ background: var(--md-success-bg); color: var(--md-success); border: 1px solid #c8e6c9; }}
        .badge-failed {{ background: var(--md-danger-bg); color: var(--md-danger); border: 1px solid #ffcdd2; }}
        .badge-skipped {{ background: var(--md-warning-bg); color: var(--md-warning); border: 1px solid #ffe0b2; }}
        /* Code Formatting */
        code {{
            font-family: var(--md-code-font);
            font-size: 0.8125rem;
            background-color: var(--md-code-bg);
            padding: 2px 6px;
            border-radius: 3px;
            color: #1e293b;
        }}
        a {{
            color: var(--md-primary);
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
        }}
        /* Admonition / Callout */
        .admonition {{
            border-radius: 4px;
            border-left: 4px solid var(--md-danger);
            background: var(--md-danger-bg);
            margin-bottom: 24px;
            padding: 14px 18px;
        }}
        .admonition-title {{
            font-weight: 600;
            color: var(--md-danger);
            font-size: 0.875rem;
            margin-bottom: 6px;
            text-transform: uppercase;
            letter-spacing: 0.4px;
        }}
        .admonition-content ul {{
            padding-left: 18px;
            color: #7f1d1d;
            font-size: 0.875rem;
        }}
        /* Explorer Controls */
        .explorer-toolbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 20px;
            background: #ffffff;
            border-bottom: 1px solid var(--md-border);
            gap: 12px;
            flex-wrap: wrap;
        }}
        .filter-group {{
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
        }}
        .filter-btn {{
            font-family: var(--md-text-font);
            border: 1px solid var(--md-border);
            background: #ffffff;
            color: var(--md-text);
            padding: 5px 12px;
            border-radius: 4px;
            font-size: 0.8125rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .filter-btn:hover {{
            background: #f1f5f9;
        }}
        .filter-btn.active {{
            background: var(--md-primary);
            color: #ffffff;
            border-color: var(--md-primary);
        }}
        .search-box {{
            font-family: var(--md-text-font);
            padding: 6px 12px;
            border: 1px solid var(--md-border);
            border-radius: 4px;
            font-size: 0.8125rem;
            min-width: 260px;
            outline: none;
            background: #ffffff;
        }}
        .search-box:focus {{
            border-color: var(--md-primary);
            box-shadow: 0 0 0 1px var(--md-primary);
        }}
        .test-class-label {{
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--md-text-muted);
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }}
        .test-name-label {{
            margin-top: 2px;
        }}
        .test-error-details {{
            margin-top: 8px;
            font-size: 0.8125rem;
        }}
        .test-error-details summary {{
            color: var(--md-danger);
            cursor: pointer;
            font-weight: 500;
        }}
        .traceback-pre {{
            margin-top: 6px;
            background: #fff5f5;
            color: #7f1d1d;
            padding: 10px;
            border-radius: 3px;
            font-size: 0.75rem;
            overflow-x: auto;
            border: 1px solid #ffcdd2;
        }}
        .details-wrapper {{
            margin-top: 16px;
        }}
        .details-wrapper summary {{
            font-weight: 600;
            font-size: 0.9rem;
            cursor: pointer;
            color: var(--md-primary);
            padding: 12px 20px;
            background: #fafafa;
            border-top: 1px solid var(--md-border);
        }}
        footer {{
            text-align: center;
            margin: 48px 0 24px 0;
            font-size: 0.8125rem;
            color: var(--md-text-muted);
        }}
    </style>
</head>
<body>
    <header class="app-bar">
        <div class="app-bar-brand">
            Pipeline Online Services Monitor
            <span class="subtitle">| Radio Astronomy Pipeline Services Status</span>
        </div>
        <div>
            {report_nav_link}
        </div>
    </header>

    <div class="container">
        <div class="status-hero">
            <div class="status-indicator {status_class}">
                {status_text}
            </div>
            <div class="timestamp">
                Last checked: <strong>{now}</strong>
            </div>
        </div>

        <section class="metrics-grid">
            <div class="metric-card">
                <div class="metric-value" style="color: var(--md-success);">{all_time_uptime_str}</div>
                <div class="metric-label">All-Time Uptime</div>
            </div>
            <div class="metric-card">
                <div class="metric-value" style="color: var(--md-success);">{results['passed']}</div>
                <div class="metric-label">Passed</div>
            </div>
            <div class="metric-card">
                <div class="metric-value" style="color: var(--md-danger);">{results['failed']}</div>
                <div class="metric-label">Failed</div>
            </div>
            <div class="metric-card">
                <div class="metric-value" style="color: var(--md-primary);">{results['total']}</div>
                <div class="metric-label">Total Tests</div>
            </div>
        </section>

        {failure_section}

        {history_section_html}

        {services_cards}

        <!-- Test Results Explorer -->
        <div class="card">
            <div class="card-title">Test Results Explorer</div>
            <div class="explorer-toolbar">
                <div class="filter-group">
                    <button class="filter-btn active" data-filter="all">All ({results['total']})</button>
                    <button class="filter-btn" data-filter="passed">Passed ({results['passed']})</button>
                    <button class="filter-btn" data-filter="failed">Failed ({results['failed']})</button>
                    <button class="filter-btn" data-filter="skipped">Skipped ({results['skipped']})</button>
                </div>
                <div>
                    <input type="text" id="testSearch" class="search-box" placeholder="Filter by test or class name...">
                </div>
            </div>

            <div class="table-container">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Status</th>
                            <th>Test Case</th>
                            <th style="text-align: right;">Duration</th>
                        </tr>
                    </thead>
                    <tbody id="testTableBody">
                        {testcases_html}
                    </tbody>
                </table>
            </div>

            <!-- Collapsible Recent Run Logs -->
            <details class="details-wrapper">
                <summary>Recent Scheduled Runs History ({len(history)} checks recorded)</summary>
                <div class="table-container">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Timestamp</th>
                                <th>Status</th>
                                <th>Passed</th>
                                <th>Failed</th>
                                <th>Duration</th>
                                <th>Run ID</th>
                            </tr>
                        </thead>
                        <tbody>
                            {runs_rows}
                        </tbody>
                    </table>
                </div>
                <div style="padding: 12px 20px; background: #fafafa; border-top: 1px solid var(--md-border); text-align: right;">
                    <a href="./data/history.json" class="nav-link" target="_blank" download style="color: var(--md-primary); background: none; font-size: 0.8125rem;">Download Complete History JSON ({len(history)} records) &rarr;</a>
                </div>
            </details>
        </div>

        <footer>
            Pipeline Online Services Monitor &bull; Automated via GitHub Actions
        </footer>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {{
            const searchInput = document.getElementById('testSearch');
            const filterButtons = document.querySelectorAll('.filter-btn');
            const rows = document.querySelectorAll('.test-row');

            let activeFilter = 'all';

            function updateRows() {{
                const term = searchInput.value.toLowerCase().trim();

                rows.forEach(row => {{
                    const status = row.getAttribute('data-status');
                    const searchContent = row.getAttribute('data-search');

                    const matchesFilter = (activeFilter === 'all') || (status === activeFilter);
                    const matchesSearch = (!term) || searchContent.includes(term);

                    row.style.display = (matchesFilter && matchesSearch) ? '' : 'none';
                }});
            }}

            filterButtons.forEach(btn => {{
                btn.addEventListener('click', () => {{
                    filterButtons.forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    activeFilter = btn.getAttribute('data-filter');
                    updateRows();
                }});
            }});

            searchInput.addEventListener('input', updateRows);
        }});
    </script>
</body>
</html>
'''
    return html


def main():
    root = Path(__file__).resolve().parent.parent
    junit_file = root / 'test-results.xml'
    results = parse_junit_results(junit_file)

    site_dir = root / 'site'
    site_dir.mkdir(parents=True, exist_ok=True)

    # Persistent history tracking in site/data/history.json
    history_file = site_dir / 'data' / 'history.json'
    history = update_history(history_file, results)

    has_html_report = (site_dir / 'report.html').exists()
    html = generate_html(results, history, has_html_report=has_html_report)

    out_file = site_dir / 'index.html'
    out_file.write_text(html, encoding='utf-8')

    print(f"Dashboard generated at: {out_file}")
    print(f"History updated: {len(history)} entries in {history_file}")
    print(f"Passed: {results['passed']}, Failed: {results['failed']}, Skipped: {results['skipped']}, Total: {results['total']}")


if __name__ == '__main__':
    main()
