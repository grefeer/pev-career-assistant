"""CLI for the Windows Executor: pairing, simulation, and resume."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx

from executor.browser import BrowserSession
from executor.checkpoints import CheckpointStore
from executor.client import ExecutorApiClient
from executor.engine import ExecutorEngine
from executor.protocol import ExecutorTaskPayload
from executor.secrets import WindowsCredentialStore


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host in LOOPBACK_HOSTS


def _ensure_loopback(url: str) -> None:
    if not _is_loopback(url):
        print(
            f"Error: simulation requires loopback (127.0.0.1/localhost), "
            f"got: {url}",
            file=sys.stderr,
        )
        sys.exit(1)


def _load_fixture(path: str) -> ExecutorTaskPayload:
    fixture_path = Path(path)
    if not fixture_path.exists():
        print(f"Fixture not found: {fixture_path}", file=sys.stderr)
        sys.exit(1)
    try:
        raw = fixture_path.read_text(encoding="utf-8")
        return ExecutorTaskPayload.model_validate_json(raw)
    except Exception as exc:
        print(f"Failed to parse fixture: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_pair(args: argparse.Namespace) -> None:
    """Pair the executor device with the backend service."""
    pairing_code = getpass.getpass("Enter pairing code: ")

    # Generate RSA-3072 key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=3072
    )
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Store private key in credential store
    store = WindowsCredentialStore()
    store.set("device-private-key", private_key_pem.decode("ascii"))

    # Pair with backend
    response = httpx.post(
        f"{args.base_url.rstrip('/')}/api/devices/pair",
        json={
            "code": pairing_code,
            "name": args.device_name,
            "public_key_pem": public_key_pem.decode("ascii"),
        },
        timeout=10.0,
    )
    if response.status_code != 200:
        print(f"Pairing failed: {response.text}", file=sys.stderr)
        sys.exit(1)

    data = response.json()
    device_token = data["device_token"]
    store.set("device-token", device_token)

    print(f"Device paired successfully: {data['device']['id']}")


def cmd_run_simulation(args: argparse.Namespace) -> None:
    """Run a simulation against a mock site."""
    _ensure_loopback(args.base_url)
    fixture = _load_fixture(args.fixture)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = data_dir / "checkpoints"

    store = WindowsCredentialStore()
    client = ExecutorApiClient(args.base_url, secret_store=store)
    browser = BrowserSession(
        user_data_dir=str(data_dir / "chrome-profile"),
    )
    checkpoints = CheckpointStore(checkpoint_dir)
    engine = ExecutorEngine(client=client, browser=browser, checkpoints=checkpoints)

    try:
        outcome = engine.run(payload=fixture)
        print(f"Simulation finished: {outcome.kind} ({outcome.reason_code})")
    finally:
        browser.close()


def cmd_resume_simulation(args: argparse.Namespace) -> None:
    """Resume a simulation from an existing checkpoint."""
    _ensure_loopback(args.base_url)
    fixture = _load_fixture(args.fixture)

    data_dir = Path(args.data_dir)
    checkpoint_dir = data_dir / "checkpoints"
    store = CheckpointStore(checkpoint_dir)

    # Verify checkpoint exists before proceeding
    cp = store.load(fixture.task_id)
    if cp is None:
        print(
            f"No checkpoint found for task {fixture.task_id}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Resuming from checkpoint at step '{cp.step}'")

    credential_store = WindowsCredentialStore()
    client = ExecutorApiClient(args.base_url, secret_store=credential_store)
    browser = BrowserSession(
        user_data_dir=str(data_dir / "chrome-profile"),
    )
    checkpoints = CheckpointStore(checkpoint_dir)
    engine = ExecutorEngine(client=client, browser=browser, checkpoints=checkpoints)

    try:
        outcome = engine.run(payload=fixture)
        print(f"Simulation finished: {outcome.kind} ({outcome.reason_code})")
    finally:
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="executor",
        description="Career Assistant Windows Executor",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # pair subcommand
    pair_parser = subparsers.add_parser("pair", help="Pair device with backend")
    pair_parser.add_argument("--base-url", required=True)
    pair_parser.add_argument("--device-name", required=True)

    # run-simulation subcommand
    run_parser = subparsers.add_parser(
        "run-simulation", help="Run simulation against mock site"
    )
    run_parser.add_argument("--base-url", required=True)
    run_parser.add_argument("--task-id")
    run_parser.add_argument("--fixture", required=True)
    run_parser.add_argument("--data-dir", required=True)

    # resume-simulation subcommand
    resume_parser = subparsers.add_parser(
        "resume-simulation", help="Resume simulation from checkpoint"
    )
    resume_parser.add_argument("--base-url", required=True)
    resume_parser.add_argument("--task-id")
    resume_parser.add_argument("--fixture", required=True)
    resume_parser.add_argument("--data-dir", required=True)

    args = parser.parse_args()

    if args.command == "pair":
        cmd_pair(args)
    elif args.command == "run-simulation":
        cmd_run_simulation(args)
    elif args.command == "resume-simulation":
        cmd_resume_simulation(args)


if __name__ == "__main__":
    main()
