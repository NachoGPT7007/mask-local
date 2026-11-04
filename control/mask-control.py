#!/usr/bin/env python3
"""fragmerge lab 电源控制（仅本机回环）。

为什么存在：home.fragmerge.cn 门面跑在 Windows（SYSTEM），它只能通过 WSL 的
localhostForwarding 访问 WSL 内的回环端口，无法直接执行 wsl.exe 或 systemctl。
所以由本服务常驻在 WSL 里，把「启停 lab 服务」收敛成一个极小的 HTTP 接口，
门面收到按钮请求后转发到这里。

边界：
  * 只监听 127.0.0.1:8790，公网不可达（Cloudflare Tunnel 按 hostname 路由，不映射它）。
  * 只允许白名单里的 unit，且动作只有 start / stop。
  * 必须带 X-Control-Token，token 从共享文件读取（与门面同一份）。

用法：python3 mask-control.py
环境：MASK_CONTROL_PORT（默认 8790）、MASK_CONTROL_TOKEN_FILE
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("MASK_CONTROL_PORT", "8790"))
TOKEN_FILE = Path(
    os.environ.get(
        "MASK_CONTROL_TOKEN_FILE",
        "/mnt/e/DeepSeek Harness Workspace/fragmerge-home/.control-token",
    )
)

# 白名单：服务 id → systemd user unit。只允许这些，别的一律 404。
# 白名单：服务 id → (systemd user unit, 发行版或 None=本机)。
# 目前所有服务都在 Ubuntu 本机；distro 槽位留给将来（跨发行版需经 wsl.exe 转发）。
UNITS: dict[str, tuple[str, str | None]] = {
    "decrypt": ("mask-web.service", None),
    "obc": ("obc.service", None),
}
WSL_EXE = Path("/mnt/c/Windows/System32/wsl.exe")

# 拉起后等待端口就绪的最大秒数（不阻塞太久，超时也返回 ok，让前端继续轮询健康探测）
READY_TIMEOUT = float(os.environ.get("MASK_CONTROL_READY_TIMEOUT", "25"))
READY_PORTS = {"decrypt": 8765, "obc": 8420}

# 一次性令牌来源：服务 id → (发行版, 日志路径)。
# superagent 的 launchToken 每次进程启动都换，只能从它自己的启动日志里取最新的那条。
TOKEN_SOURCES: dict[str, tuple[str, str]] = {
    "superagent": ("FedoraLinux-44", "/home/nachogpt/.dsh/sa-judge.log"),
    # 别名：门面的跳转路由用 /go/secretary，指向同一个实例
    "secretary": ("FedoraLinux-44", "/home/nachogpt/.dsh/sa-judge.log"),
}


def read_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def systemctl_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    # 让 systemctl --user 不必依赖登录会话里的 DBUS 变量
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


def run_systemctl(*args: str, timeout: float = 20.0, distro: str | None = None) -> tuple[int, str]:
    """在本机或指定 WSL 发行版里跑 systemctl --user。

    distro=None 时就是普通的本机 user unit；给了发行版名就走 wsl.exe，
    因为不同发行版的 /run/user/<uid>/bus 是分开的，本机 systemctl 看不见对方。
    """
    if distro:
        if not WSL_EXE.exists():
            return 127, f"找不到 wsl.exe：{WSL_EXE}"
        argv = [str(WSL_EXE), "-d", distro, "--", "systemctl", "--user", *args]
        env = None
    else:
        argv = ["systemctl", "--user", *args]
        env = systemctl_env()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        message = (proc.stdout + proc.stderr).strip()
        return proc.returncode, message
    except subprocess.TimeoutExpired:
        return 124, f"systemctl {' '.join(args)} 超时"
    except OSError as exc:
        return 127, f"无法执行 systemctl：{exc}"


def read_launch_token(name: str, timeout: float = 15.0) -> str | None:
    """从目标发行的日志里取最新的一次性令牌（`?token=...` 那一行的值）。

    只读日志的最后一处匹配，不做任何写入；取不到就返回 None，让调用方决定降级。
    """
    spec = TOKEN_SOURCES.get(name)
    if not spec:
        return None
    distro, log_path = spec
    if not WSL_EXE.exists():
        return None
    script = (
        f"grep 'token=' {log_path} 2>/dev/null | tail -1 | sed 's/.*token=//' | tr -d '[:space:]'"
    )
    try:
        proc = subprocess.run(
            [str(WSL_EXE), "-d", distro, "--", "bash", "-c", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    token = (proc.stdout or "").strip()
    return token or None


def unit_state(unit: str, distro: str | None = None) -> dict:
    code, out = run_systemctl("show", "-p", "ActiveState", "-p", "SubState", "-p", "ExecMainPID", unit, distro=distro)
    data = {"active": "unknown", "sub": "unknown", "pid": None}
    if code != 0:
        return data
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "ActiveState":
            data["active"] = value or "unknown"
        elif key == "SubState":
            data["sub"] = value or "unknown"
        elif key == "ExecMainPID":
            data["pid"] = int(value) if value.isdigit() and value != "0" else None
    return data


def port_open(port: int, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((HOST, port)) == 0


def wait_ready(service_id: str) -> bool:
    port = READY_PORTS.get(service_id)
    if not port:
        return True
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if port_open(port):
            return True
        time.sleep(0.4)
    return port_open(port)


def wait_closed(service_id: str, timeout: float = 6.0) -> bool:
    """停止后等端口真正释放，避免前端拿到「已停止但端口还在」的中间态。"""
    port = READY_PORTS.get(service_id)
    if not port:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open(port):
            return True
        time.sleep(0.2)
    return not port_open(port)


class Handler(BaseHTTPRequestHandler):
    server_version = "mask-control/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 收敛日志噪音
        sys.stderr.write("[mask-control] %s\n" % (fmt % args))

    # ----- 工具 -----
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _authorized(self) -> bool:
        expected = read_token()
        if not expected:
            self._json(503, {"ok": False, "error": "控制令牌未配置，拒绝执行"})
            return False
        supplied = self.headers.get("X-Control-Token", "")
        if not secrets.compare_digest(supplied, expected):
            self._json(403, {"ok": False, "error": "控制令牌不匹配"})
            return False
        return True

    def _route(self) -> tuple[str, str] | None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) == 3 and parts[0] == "services":
            return parts[1], parts[2]
        if len(parts) == 2 and parts[0] == "token":
            return parts[1], "token"
        return None

    # ----- GET -----
    def do_GET(self) -> None:  # noqa: N802 - HTTP 方法名
        if self.path.split("?")[0] == "/healthz":
            self._json(200, {"ok": True, "service": "mask-control"})
            return
        route = self._route()
        if route and route[1] == "token":
            if not self._authorized():
                return
            name = route[0]
            token = read_launch_token(name)
            if token is None:
                self._json(404, {"ok": False, "error": f"取不到 {name} 的令牌（服务未启动或日志里没有）"})
                return
            self._json(200, {"ok": True, "name": name, "token": token})
            return
        if not route or route[1] != "status":
            self._json(404, {"ok": False, "error": "未知路径"})
            return
        if not self._authorized():
            return
        service_id = route[0]
        spec = UNITS.get(service_id)
        if not spec:
            self._json(404, {"ok": False, "error": f"不支持的服务：{service_id}"})
            return
        unit, distro = spec
        state = unit_state(unit, distro)
        port = READY_PORTS.get(service_id)
        self._json(
            200,
            {
                "ok": True,
                "service": service_id,
                "unit": unit,
                **state,
                "listening": port_open(port) if port else None,
            },
        )

    # ----- POST -----
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 丢弃 body，防止 keep-alive 错位
        route = self._route()
        if not route:
            self._json(404, {"ok": False, "error": "未知路径"})
            return
        service_id, action = route
        spec = UNITS.get(service_id)
        if not spec:
            self._json(404, {"ok": False, "error": f"不支持的服务：{service_id}"})
            return
        unit, distro = spec
        if action not in ("start", "stop"):
            self._json(404, {"ok": False, "error": f"不支持的动作：{action}"})
            return
        if not self._authorized():
            return

        code, message = run_systemctl(action, unit, distro=distro)
        if code != 0:
            self._json(
                500,
                {"ok": False, "error": f"systemctl {action} 失败（退出码 {code}）", "detail": message},
            )
            return

        if action == "start":
            wait_ready(service_id)
        else:
            wait_closed(service_id)
        state = unit_state(unit)
        port = READY_PORTS.get(service_id)
        # listening 一律表示「此刻端口是否在听」：start 后应为 True，stop 后应为 False
        listening = port_open(port) if port else None
        self._json(
            200,
            {
                "ok": True,
                "service": service_id,
                "action": action,
                "unit": unit,
                **state,
                "listening": listening,
            },
        )


def main() -> None:
    if not read_token():
        sys.stderr.write(f"[mask-control] 警告：读不到令牌文件 {TOKEN_FILE}\n")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    sys.stderr.write(f"[mask-control] 已监听 http://{HOST}:{PORT}/（白名单：{', '.join(UNITS)}）\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
