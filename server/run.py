"""서버 실행 진입점 — IPv4/IPv6 양쪽을 동시에 받는다.

uvicorn 에 `--host` 로 주소를 넘기면 한쪽만 열린다.

    --host 0.0.0.0   IPv4만. IPv6 전용 네트워크(아이폰 핫스팟 등)에서 접속 불가
    --host ::        IPv6만. 일반 공유기(IPv4)에서 접속 불가

커널 기본값은 듀얼 스택이지만, 파이썬 asyncio 의 create_server() 가 IPv6 주소로
바인드할 때 IPV6_V6ONLY 소켓 옵션을 명시적으로 켜기 때문이다.

그래서 여기서는 소켓을 직접 만들어 그 옵션을 끈 뒤 uvicorn 에 넘긴다. 장소를
옮겨 네트워크가 바뀌어도 실행 명령을 바꿀 필요가 없다.

실행:
    .venv/bin/python -m server.run
    PORT=9000 .venv/bin/python -m server.run
"""

import os
import socket
import sys

import uvicorn

from .main import app

DEFAULT_PORT = 8000
BACKLOG = 128


def bind_socket(port):
    """IPv4/IPv6를 함께 받는 리스닝 소켓. 듀얼 스택이 안 되면 IPv4로 물러난다."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.listen(BACKLOG)
        return sock, "0.0.0.0 (IPv4 전용 — IPv6 미지원 시스템)"

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    dual = True
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    except OSError:
        dual = False  # 일부 시스템은 듀얼 스택을 허용하지 않는다
    sock.bind(("::", port))
    sock.listen(BACKLOG)
    return sock, ":: (IPv4+IPv6)" if dual else ":: (IPv6 전용)"


def main():
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    sock, described = bind_socket(port)
    print(f"바인드: {described}  포트 {port}", file=sys.stderr)

    config = uvicorn.Config(app, log_level=os.environ.get("LOG_LEVEL", "info"))
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
