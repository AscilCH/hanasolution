"""
B2B Phone Finder — aiohttp web server.
Serves the UI, handles uploads, streams progress via SSE, exports results.
"""
import aiohttp
from aiohttp import web
import asyncio
import json
import os
import sys
import io
import time
import webbrowser
import logging

from scraper import PhoneScraper
from xlsx_handler import read_xlsx, write_xlsx

# ── Logging ──────────────────────────────────────────────────
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s  %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('server')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
os.makedirs(DATA_DIR, exist_ok=True)

# ── Shared app state ─────────────────────────────────────────
state = {
    'filepath': None,
    'companies': [],       # [{row, name, category, existing_phone}]
    'results': {},         # row_num -> {type,row,name,status,phones,source}
    'is_running': False,
    'stop_requested': False,
    'stats': {},
}
progress_queues: list[asyncio.Queue] = []   # SSE subscribers

# Known header mappings (Arabic + English + French)
NAME_COLS = ['Account Name', 'Company', 'Company Name', 'Name', 'Nom',
             'Société', 'اسم الشركة', 'الشركة', 'اسم الحساب']
PHONE_COLS = ['Contact Phone', 'Phone', 'Téléphone', 'Tel', 'هاتف',
              'رقم الهاتف', 'Phone Number']
CATEGORY_COLS = ['Category', 'Catégorie', 'التصنيف', 'Sector', 'القطاع']


def _detect_col(row: dict, candidates: list[str]) -> str:
    """Return the value from the first matching column name."""
    for c in candidates:
        if c in row and row[c]:
            return str(row[c]).strip()
    # fuzzy fallback: partial match
    for key in row:
        kl = str(key).lower()
        for c in candidates:
            if c.lower() in kl or kl in c.lower():
                v = row[key]
                if v:
                    return str(v).strip()
    return ''


async def _broadcast(event: dict):
    """Push an SSE event to all connected frontends."""
    for q in progress_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ── Route handlers ───────────────────────────────────────────
async def handle_index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, 'index.html'))


async def handle_upload(request):
    reader = await request.multipart()
    field = await reader.next()
    filepath = os.path.join(DATA_DIR, 'uploaded.xlsx')

    with open(filepath, 'wb') as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)

    state['filepath'] = filepath
    state['results'] = {}
    state['companies'] = []

    try:
        rows = read_xlsx(filepath)
    except Exception as e:
        log.error(f"Failed to parse xlsx: {e}")
        return web.json_response({'error': str(e)}, status=400)

    companies = []
    stats = {'total': len(rows), 'empty_skipped': 0, 'have_phone': 0, 'to_process': 0}

    for row in rows:
        name = _detect_col(row, NAME_COLS)
        phone = _detect_col(row, PHONE_COLS)
        cat = _detect_col(row, CATEGORY_COLS)

        if not name:
            stats['empty_skipped'] += 1
            continue

        comp = {
            'row': row['_row_num'],
            'name': name,
            'category': cat,
            'existing_phone': phone,
        }
        companies.append(comp)
        if phone:
            stats['have_phone'] += 1
        else:
            stats['to_process'] += 1

    state['companies'] = companies
    state['stats'] = stats
    log.info(f"Uploaded: {stats}")
    return web.json_response({'companies': companies, 'stats': stats})


async def handle_start(request):
    if state['is_running']:
        return web.json_response({'status': 'already_running'}, status=409)
    if not state['companies']:
        return web.json_response({'error': 'No file uploaded'}, status=400)

    state['is_running'] = True
    state['stop_requested'] = False
    asyncio.create_task(_run_scraper())
    return web.json_response({'status': 'started'})


async def handle_stop(request):
    state['stop_requested'] = True
    return web.json_response({'status': 'stopping'})


async def handle_progress(request):
    """SSE endpoint — streams real-time updates to the browser."""
    resp = web.StreamResponse(headers={
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    })
    await resp.prepare(request)

    # replay existing results
    for ev in state['results'].values():
        await resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode('utf-8'))

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    progress_queues.append(queue)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                await resp.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode('utf-8'))
                if event.get('type') == 'done':
                    break
            except asyncio.TimeoutError:
                await resp.write(b": heartbeat\n\n")
    except (ConnectionResetError, asyncio.CancelledError, ConnectionError):
        pass
    finally:
        if queue in progress_queues:
            progress_queues.remove(queue)
    return resp


async def handle_results(request):
    return web.json_response(
        {'results': list(state['results'].values())},
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )


async def handle_export(request):
    if not state['filepath']:
        return web.Response(status=400, text='No file uploaded')

    out_path = os.path.join(DATA_DIR, 'results.xlsx')
    results_list = list(state['results'].values())
    write_xlsx(state['filepath'], out_path, results_list)
    return web.FileResponse(
        out_path,
        headers={'Content-Disposition': 'attachment; filename="B2B_Phone_Results.xlsx"'},
    )


# ── Background scraper task ──────────────────────────────────
async def _run_scraper():
    scraper = PhoneScraper()
    await scraper.start()

    to_process = [c for c in state['companies'] if not c['existing_phone']]
    total = len(to_process)
    found = 0
    not_found = 0
    t0 = time.time()

    log.info(f"Starting scrape: {total} companies to process")

    for i, comp in enumerate(to_process):
        if state['stop_requested']:
            log.info("Stop requested")
            break

        row = comp['row']
        name = comp['name']
        cat = comp['category']

        log.info(f"[{i+1}/{total}] Row {row}: {name}")

        try:
            phones = await scraper.find_phones(name, cat)
        except Exception as e:
            log.error(f"  Error: {e}")
            phones = []

        if phones:
            found += 1
            status = 'found'
            log.info(f"  ✅ {', '.join(p['number'] for p in phones[:3])}")
        else:
            not_found += 1
            status = 'not_found'
            log.info(f"  ❌ Not found")

        event = {
            'type': 'update',
            'row': row,
            'name': name,
            'status': status,
            'phones': [{'number': p['number'],
                        'source_url': p['source_url'],
                        'source_name': p['source_name']} for p in phones],
        }
        state['results'][row] = event
        await _broadcast(event)

        # persist every 5
        if (i + 1) % 5 == 0:
            _save_progress()

    await scraper.stop()
    state['is_running'] = False

    elapsed = round(time.time() - t0, 1)
    done_event = {
        'type': 'done',
        'stats': {'found': found, 'not_found': not_found,
                  'total': total, 'duration_s': elapsed},
    }
    await _broadcast(done_event)
    _save_progress()
    log.info(f"Done! Found {found}/{total} in {elapsed}s")


def _save_progress():
    try:
        with open(os.path.join(DATA_DIR, 'results.json'), 'w', encoding='utf-8') as f:
            json.dump(list(state['results'].values()), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Failed to save progress: {e}")


# ── App setup ────────────────────────────────────────────────
app = web.Application(client_max_size=50 * 1024 * 1024)   # 50 MB upload limit
app.router.add_get('/', handle_index)
app.router.add_post('/api/upload', handle_upload)
app.router.add_post('/api/start', handle_start)
app.router.add_post('/api/stop', handle_stop)
app.router.add_get('/api/progress', handle_progress)
app.router.add_get('/api/results', handle_results)
app.router.add_get('/api/export', handle_export)
app.router.add_static('/static/', STATIC_DIR)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = '0.0.0.0'
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║     HanaSolution — Server            ║")
    print(f"  ║     http://localhost:{port:<17s}  ║")
    print("  ╚══════════════════════════════════════╝")
    print()
    # Only open browser when running locally
    if not os.environ.get('RENDER'):
        webbrowser.open(f'http://localhost:{port}')
    web.run_app(app, host=host, port=port, print=None)
