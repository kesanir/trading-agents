"""Prepare at 05:55 Pacific, publish at 06:00. Invoked under the systemd reporting lock."""
import hashlib
import json
import logging
import os
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ROOT = Path('app/reports/gamma')
PT = ZoneInfo('America/Los_Angeles')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def prepare(now=None, root=None):
    from app.services.gamma_report_service import collect_report
    from app.services.gamma_report_pdf import render_report, summary
    now = now or datetime.now(timezone.utc)
    folder = Path(root or ROOT) / now.astimezone(PT).date().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    # Do not replace an already published artifact during a repeated prepare.
    if (folder / 'delivery.json').exists():
        raise RuntimeError('Report delivery already started; prepare refused')
    report = collect_report(now)
    candidate = folder / 'gamma-map.pending.pdf'
    render_report(report, candidate)
    report['pdf_sha256'] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    os.replace(candidate, folder / 'gamma-map.pdf')
    (folder / 'summary.txt').write_text(summary(report), encoding='utf-8')
    # Manifest written last; failed/partial preparations cannot be published.
    atomic_json(folder / 'snapshot.json', report)
    return folder


def publish(send_telegram=False, now=None, root=None):
    from app.services.gamma_report_pdf import summary
    from app.data.yahoo_futures import validate_reference
    from app.services.telegram_service import send_telegram_message, send_telegram_document
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(PT)
    if not time(6) <= local.time() < time(6, 6):
        raise RuntimeError('Gamma publication is allowed only 06:00-06:05 America/Los_Angeles')
    folder = Path(root or ROOT) / local.date().isoformat()
    state_path = folder / 'delivery.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get('summary') and state.get('document'):
        log.info('Gamma report already delivered for this session')
        return
    report = json.loads((folder / 'snapshot.json').read_text(encoding='utf-8'))
    if report.get('demo'):
        raise RuntimeError('Synthetic examples cannot be published')
    if report.get('schema_version') != 3:
        raise RuntimeError('Gamma report schema is outdated; prepare again before publishing')
    prepared = datetime.fromisoformat(report['as_of'])
    if report['date'] != local.date().isoformat() or not timedelta(0) <= now - prepared <= timedelta(minutes=15):
        raise RuntimeError('Gamma report is from the wrong session or preparation is stale')
    for reference in (report['market']['qqq'], report['market']['nq']):
        validate_reference(reference, now)
    pdf = folder / 'gamma-map.pdf'
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != report['pdf_sha256']:
        raise RuntimeError('Gamma PDF does not match the prepared snapshot')
    if not send_telegram:
        print(summary(report))
        return
    # Per-part receipts avoid repeating a successful part after a subsequent failure.
    # Delivery remains at least once if the process dies between send and receipt write.
    if not state.get('summary'):
        responses = send_telegram_message(summary(report))
        state['summary'] = responses
        atomic_json(state_path, state)
    if not state.get('document'):
        state['document'] = send_telegram_document(pdf, f"QQQ Gamma Map | {report['date']} | Yahoo NQ=F")
        atomic_json(state_path, state)


def run(job, send_telegram=False):
    try:
        if job == 'gamma-prepare':
            prepare()
        elif job == 'gamma-publish':
            publish(send_telegram)
        else:
            raise ValueError('Unknown gamma job')
    except Exception:
        # Do not include raw HTTP errors/URLs (which can contain credentials) in alerts.
        log.error('Gamma %s failed; report was not completed', job)
        if send_telegram:
            from app.services.telegram_service import send_telegram_message
            try:
                send_telegram_message(f'Gamma report {job} failed. Check EC2 job logs; no completed report is available.')
            except Exception:
                log.error('Gamma failure notification could not be delivered')
        raise
