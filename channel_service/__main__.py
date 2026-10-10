import argparse
from pathlib import Path
import signal
import sys
from waitress import serve
from .app import ROOT, create_app
from .demo import AGENT_ID, DemoMultica
from .multica import Multica


def main():
    parser = argparse.ArgumentParser(description='Channel durable platform-to-agent message service')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--data-dir', type=Path, default=ROOT / '.local')
    parser.add_argument('--public-origin', default='')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--no-auth', action='store_true')
    args = parser.parse_args()
    if args.no_auth and (not args.demo or args.public_origin or args.host not in ('127.0.0.1', '::1', 'localhost')):
        parser.error('--no-auth is available only for a loopback demo without a public origin')
    adapter = DemoMultica() if args.demo else Multica(args.data_dir / 'multica.json')
    app = create_app(args.data_dir, adapter, auth=not args.no_auth, public_origin=args.public_origin, background=True, demo=args.demo)
    store = app.extensions['channel']
    if args.demo and not store.sources():
        store.source('demo', '离线演示来源')
        store.save_route(dict(name='离线分析', agent_id=AGENT_ID, grouping='conversation', batch_seconds=1))
    def stop(*_):
        app.extensions['worker'].stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    serve(app, host=args.host, port=args.port, threads=12, max_request_body_size=9 * 1024 * 1024)

if __name__ == '__main__':
    main()
