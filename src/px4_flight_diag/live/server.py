"""Loopback-only read-only WebSocket service and one capture/replay worker."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from plotly.offline import get_plotlyjs
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from .. import __version__
from ..collectors.udp import receive_packets
from ..storage.session import Session, read_packets
from .core import LiveBuffer

STATIC = Path(__file__).parent / 'static'


class LiveRunner:
    """One producer; no WebSocket client owns capture or recording lifecycle."""
    def __init__(self, *, config: dict | None = None, replay_session: Path | None = None,
                 speed: float = 1.0, start_delay: float = 1.0):
        if (config is None) == (replay_session is None):
            raise ValueError('choose exactly one config or replay_session')
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError('speed must be finite and positive')
        if not math.isfinite(start_delay) or start_delay < 0:
            raise ValueError('start_delay must be finite and nonnegative')
        self.config, self.replay_session = config, replay_session
        if replay_session is not None and not (replay_session / 'raw/udp.jsonl').is_file():
            raise ValueError(f'{replay_session}: missing raw/udp.jsonl for timed replay')
        self.speed, self.start_delay = speed, start_delay
        self.stop_event = threading.Event()
        self.buffer = LiveBuffer(session=str(replay_session or ''), mode='replay' if replay_session else 'live')
        self.thread: threading.Thread | None = None
        self.failure: str | None = None

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError('worker already started')
        self.thread = threading.Thread(target=self._run, name='px4-recording', daemon=False)
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()  # receiver timeout <=.25s; replay uses interruptible wait

    def _run(self) -> None:
        try:
            if self.stop_event.wait(self.start_delay):
                self.buffer.finish()
                return
            if self.replay_session is not None:
                self._replay()
            else:
                from ..cli import process
                session = Session(Path(self.config['output_root']), self.config['drone'],
                                  {'mode': 'capture', 'config': self.config, 'version': __version__, 'live': True})
                self.buffer.session = str(session.path)
                packets = receive_packets(self.config['bind_host'], self.config['bind_port'],
                                          self.config['duration_seconds'], stop=self.stop_event.is_set)
                summary = process(packets, session, observer=self.buffer, stop=self.stop_event.is_set)
                if summary.get('live_observer_errors'):
                    raise RuntimeError(f"Display observer failed {summary['live_observer_errors']} times; recording retained")
            self.buffer.finish()
        except Exception as exc:
            self.failure = str(exc)
            self.buffer.finish(error=self.failure)

    def _replay(self) -> None:
        # Same existing parser, no UDP listener, no output Session and no source mutation.
        from ..communication.parser import Parser
        parser = Parser()
        first = None
        start = time.monotonic()
        for packet in read_packets(self.replay_session / 'raw/udp.jsonl'):
            if first is None:
                first = packet['recv_monotonic_ns']
            due = start + (packet['recv_monotonic_ns'] - first) / 1e9 / self.speed
            if self.stop_event.wait(max(0.0, due - time.monotonic())):
                return
            self.buffer.push_packet(packet)
            for record in parser.feed(packet):
                self.buffer.push_record(record)


def create_app(runner: LiveRunner, *, port: int = 8765, max_clients: int = 4) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        runner.start()
        try:
            yield
        finally:
            await asyncio.to_thread(runner.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])
    clients: set[WebSocket] = set()

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-store'})

    @app.get('/app.js')
    async def javascript():
        return FileResponse(STATIC / 'app.js', media_type='text/javascript')

    @app.get('/plotly.min.js')
    async def plotly():
        return Response(get_plotlyjs(), media_type='text/javascript')

    @app.websocket('/ws')
    async def websocket(ws: WebSocket):
        origin = ws.headers.get('origin')
        if origin:
            url = urlsplit(origin)
            if url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1') or (url.port or 80) != port:
                await ws.close(code=1008)
                return
        if len(clients) >= max_clients:
            await ws.close(code=1013)
            return
        await ws.accept()
        clients.add(ws)
        cursor = 0
        try:
            while True:
                snapshot = runner.buffer.snapshot(cursor)
                await asyncio.wait_for(ws.send_json(snapshot), timeout=1.0)
                cursor = snapshot['cursor']
                await asyncio.sleep(0.2)
        except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, OSError):
            pass
        finally:
            clients.discard(ws)
            try:
                await ws.close()
            except (RuntimeError, OSError):
                pass

    return app


def run_live(*, config: dict | None = None, replay_session: Path | None = None,
             port: int = 8765, speed: float = 1.0, start_delay: float = 1.0) -> int:
    if not 1 <= port <= 65535:
        raise ValueError('web port must be 1..65535')
    if config and port == config['bind_port']:
        raise ValueError('web and UDP ports must be independent')
    runner = LiveRunner(config=config, replay_session=replay_session, speed=speed, start_delay=start_delay)
    print(f'Read-only Web: http://127.0.0.1:{port} · Ctrl+C to close; completed curves remain until exit', flush=True)
    uvicorn.run(create_app(runner, port=port), host='127.0.0.1', port=port,
                log_level='info', ws_max_size=4096, ws_max_queue=4,
                timeout_graceful_shutdown=2, limit_concurrency=16)
    return 2 if runner.failure else 0
