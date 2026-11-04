"""Shared capture/replay pipeline and safe command-line entry."""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Iterable

import yaml

from . import __version__
from .analysis.stats import Stats
from .collectors.udp import receive_packets
from .communication.parser import Parser
from .storage.session import Session, dumps, read_packets


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    required = {"bind_host", "bind_port", "duration_seconds", "drone", "output_root"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError(f"configuration must contain exactly: {sorted(required)}")
    if not isinstance(config["bind_host"], str) or not config["bind_host"]:
        raise ValueError("bind_host must be a nonempty local IPv4 address/hostname")
    if type(config["bind_port"]) is not int or not 1 <= config["bind_port"] <= 65535:
        raise ValueError("bind_port must be 1..65535")
    duration = config["duration_seconds"]
    if type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration_seconds must be finite and positive")
    for name in ("drone", "output_root"):
        if not isinstance(config[name], str) or not config[name]:
            raise ValueError(f"{name} must be a nonempty string")
    return config


def process(packets: Iterable[dict], session: Session, observer=None, stop=None) -> dict:
    """Archive first; an optional local observer never waits on browser clients."""
    parser, stats = Parser(), Stats()
    status, error = "completed", None
    observer_errors = 0
    def notify(method, value):
        nonlocal observer_errors
        if observer is not None:
            try:
                getattr(observer, method)(value)
            except Exception:
                observer_errors += 1
    try:
        for packet in packets:
            session.packet(packet)
            stats.add_packet(packet)
            notify('push_packet', packet)
            for record in parser.feed(packet):
                session.record(record)
                stats.add_record(record)
                notify('push_record', record)
        if stop is not None and stop():
            status = "interrupted"
    except KeyboardInterrupt:
        status = "interrupted"
    except Exception as exc:
        status, error = "failed", str(exc)
        raise
    finally:
        try:
            summary = stats.summary()
            summary.update({"schema_version": 1, "status": status, "error": error,
                            "pending_bytes": parser.pending_bytes(),
                            "session": str(session.path),
                            "diagnosis": "无法判断故障根因；V0.1 仅统计通信观测。"})
            if observer is not None:
                summary['live_observer_errors'] = observer_errors
            session.save_json("reports/summary.json", summary)
        finally:
            session.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="PX4 receive-only capture and offline replay")
    cli.add_argument("--version", action="version", version=__version__)
    commands = cli.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="reparse raw UDP JSONL; never opens a socket")
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--output-root", type=Path, default=Path("data"))
    replay.add_argument("--drone", default="replay")
    capture = commands.add_parser("capture", help="passive UDP listener")
    capture.add_argument("--config", type=Path, required=True)
    analyze = commands.add_parser("analyze", help="offline processed Session to standalone HTML; no socket")
    analyze.add_argument("--session", type=Path, required=True)
    analyze.add_argument("--time-axis", choices=("auto", "receive"), default="auto")
    analyze.add_argument("--gap-seconds", type=float, default=0.5)
    live = commands.add_parser("live", help="loopback-only read-only live Web or timed historical replay")
    source = live.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path)
    source.add_argument("--replay-session", type=Path)
    live.add_argument("--web-port", type=int, default=8765)
    live.add_argument("--speed", type=float, default=1.0, help="historical replay speed multiplier")
    live.add_argument("--start-delay", type=float, default=1.0, help="seconds before producer starts")
    args = cli.parse_args(argv)
    try:
        if args.command == "live":
            from .live.server import run_live
            return run_live(config=load_config(args.config) if args.config else None,
                            replay_session=args.replay_session, port=args.web_port,
                            speed=args.speed, start_delay=args.start_delay)
        if args.command == "analyze":
            from .analysis.report import generate_report
            print(generate_report(args.session, time_axis=args.time_axis, gap_seconds=args.gap_seconds))
            return 0
        if args.command == "replay":
            if not args.input.is_file():
                raise ValueError(f"input file not found: {args.input}")
            session = Session(args.output_root, args.drone,
                              {"mode": "replay", "input": str(args.input.resolve()), "version": __version__})
            packets = read_packets(args.input)
        else:
            config = load_config(args.config)
            session = Session(Path(config["output_root"]), config["drone"],
                              {"mode": "capture", "config": config, "version": __version__})
            packets = receive_packets(config["bind_host"], config["bind_port"], config["duration_seconds"],
                                      ready=lambda address: print(f"Listening (receive-only): {address}", file=sys.stderr, flush=True))
        print(dumps(process(packets, session)))
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
