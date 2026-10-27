"""Local development binding; all-interface listening requires explicit --host 0.0.0.0."""

import argparse
import ipaddress
import socket
from contextlib import ExitStack
from urllib.parse import urlsplit

import uvicorn

from .config import Config


def private_same_origin(origin, request_url):
    """Explicit development mode only: literal RFC1918 IP and exact scheme/host/port."""
    try:
        source, target = urlsplit(origin or ""), urlsplit(str(request_url))
        if source.scheme not in ("http", "https") or source.scheme != target.scheme:
            return False
        if (
            source.netloc != target.netloc
            or source.path
            or source.query
            or source.fragment
            or source.username
        ):
            return False
        lan_address(source.hostname)
        return True
    except (ValueError, TypeError, argparse.ArgumentTypeError):
        return False


def lan_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise argparse.ArgumentTypeError("IPv4 Wi-Fi 주소를 입력하세요.") from None
    # RFC1918 only. Do not expose to public, link-local, VPN's public, or wildcard interfaces.
    networks = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    if not any(address in ipaddress.IPv4Network(network) for network in networks):
        raise argparse.ArgumentTypeError("개발 LAN은 사설 IPv4 주소만 허용합니다.")
    return str(address)


def main():
    parser = argparse.ArgumentParser(description="localhost + 지정한 Wi-Fi IP에서 개발 API 실행")
    binding = parser.add_mutually_exclusive_group()
    binding.add_argument("--lan-ip", type=lan_address)
    binding.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"])
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("포트는 1~65535입니다.")
    config = Config()
    if args.lan_ip and f"http://{args.lan_ip}:{args.port}" not in config.allowed_origins:
        parser.error("TRADING_ALLOWED_ORIGINS에 지정한 Wi-Fi 접속 주소를 먼저 추가하세요.")
    addresses = [args.host] if args.host else ["127.0.0.1"] + ([args.lan_ip] if args.lan_ip else [])
    application = "nastack_trading.app:create_app"
    if args.host == "0.0.0.0":
        from .app import create_app

        config = config.model_copy(update={"development_lan_same_origin": True})
        application = create_app(config)
        print("Development server: all IPv4 interfaces; private-IP same-origin requests allowed.", flush=True)
    with ExitStack() as stack:
        sockets = []
        for address in addresses:
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((address, args.port))
            except OSError:
                parser.error("IP가 현재 Wi-Fi 주소인지, 포트가 사용 중인지 확인하세요.")
            sock.listen(128)
            sock.setblocking(False)
            sockets.append(sock)
        uvicorn.Server(
            uvicorn.Config(
                application, factory=isinstance(application, str), port=args.port, timeout_graceful_shutdown=5
            )
        ).run(sockets=sockets)


if __name__ == "__main__":
    main()
