#!/usr/bin/env python3
"""本地脱敏 Web 界面。

设计约束
--------
1. 只绑定 127.0.0.1，不监听局域网，杜绝文件外流。
2. 上传文件落到系统临时目录，处理完立即清理中间件；结果另存一份到
   ``output/<时间戳>/``，便于用户用文件管理器直接取用。
3. 不依赖 Flask/FastAPI，纯标准库，避免额外安装面。
4. 前端只做展示，所有脱敏判断都在后端，防止绕过策略。

启动：
    .venv/bin/python web/server.py --port 8765
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WEB_DIR = pathlib.Path(__file__).resolve().parent
OUTPUT_ROOT = ROOT / "output"

#: 上传大小上限：法院案卷偶有百兆扫描件，留足余量但不无限
MAX_UPLOAD = 512 * 1024 * 1024

#: token -> (zip 路径, 创建时间)，进程内保存，重启即失效
_DOWNLOADS: dict[str, tuple[pathlib.Path, datetime]] = {}
_LOCK = threading.Lock()


LEVELS = {
    "full": {},
    "identity_org": {"case_no": "keep"},
    "identity": {"org": "keep", "case_no": "keep"},
}


def _cleanup_old_downloads(max_age_seconds: int = 6 * 3600) -> None:
    now = datetime.now()
    with _LOCK:
        for token, (path, created) in list(_DOWNLOADS.items()):
            if (now - created).total_seconds() > max_age_seconds or not path.exists():
                _DOWNLOADS.pop(token, None)


def parse_multipart(content_type: str, body: bytes) -> tuple[list[tuple[str, str, bytes]], dict[str, str]]:
    """极简 multipart/form-data 解析。用 email 包拆，不引入第三方依赖。"""
    header = (
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n"
        b"MIME-Version: 1.0\r\n\r\n"
    )
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(header + body)
    files: list[tuple[str, str, bytes]] = []
    fields: dict[str, str] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append((name or "files", filename, payload))
        elif name:
            fields[name] = payload.decode("utf-8", "replace")
    return files, fields


def apply_level(settings, level: str):
    """按界面上选择的档位覆盖 policy。"""
    overrides = LEVELS.get(level, {})
    for key, value in overrides.items():
        settings.rules.policy[key] = value
    return settings


#: 老 Office 格式 → 新格式。法院材料里 .doc 很常见（法宝导出的就是这样），
#: 而 tools 只认 OOXML。用 LibreOffice 本地转换，不联网。
LEGACY_MAP = {".doc": "docx", ".xls": "xlsx"}


def convert_legacy(path: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """把 .doc/.xls 转成 .docx/.xlsx。返回 (转换后路径, 说明)。

    转换在临时目录里进行，原上传文件随即删除，避免同一份材料被处理两遍。
    失败时返回 (None, 原因)，让 .doc 原样进流水线并被明确标记为「不支持」。
    """
    target = LEGACY_MAP.get(path.suffix.lower())
    if not target:
        return None, ""

    if shutil.which("libreoffice") is None:
        return None, "未安装 LibreOffice，无法转换"

    try:
        proc = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to",
                target,
                "--outdir",
                str(path.parent),
                str(path),
            ],
            check=False,
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return None, "LibreOffice 转换超时（文件可能过大或已损坏）"
    except OSError as exc:
        return None, f"调用 LibreOffice 失败：{exc}"

    out = path.with_suffix("." + target)
    if not out.exists() or out.stat().st_size == 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace")[:200]
        return None, f"转换未产出文件（{detail or 'LibreOffice 未报告原因'}）"

    try:
        path.unlink()
    except OSError:
        pass
    return out, f"已从 {path.suffix} 转为 {out.suffix}，原件未改动"


#: NER 复核用的中文模型（CLUENER 数据集：人名/公司/政府/地址/职位）
NER_MODEL = "uer/roberta-base-finetuned-cluener2020-chinese"


def _run_ner_review(in_dir: pathlib.Path, out_dir: pathlib.Path,
                    keep: pathlib.Path) -> str:
    """跑 NER 复核，清单写进 keep/vault/。返回给用户看的一句话。"""
    try:
        tools_dir = str(ROOT / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import ner_review  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return f"NER 复核不可用（{type(exc).__name__}）：未集成，跳过"

    supported = {".docx", ".pdf", ".txt"}
    srcs = [p for p in sorted(in_dir.iterdir()) if p.suffix.lower() in supported]
    if not srcs:
        return "NER 复核跳过：找不到可读原件"
    src = srcs[0]

    masked = None
    for cand in sorted(out_dir.rglob(src.stem + ".*")):
        if cand.suffix.lower() in supported:
            masked = cand
            break
    if masked is None:
        return "NER 复核跳过：找不到脱敏件"

    try:
        import os

        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        masked_text = ner_review.read_any_text(masked)
        report = ner_review.run(NER_MODEL, src, masked_text)
        out = keep / "vault" / "NER复核清单.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        ner_review.write_report(report, out, src)
        g = report.by_status()
        return (
            f"NER 复核完成：额外指出 {len(g['missed'])} 处规则可能漏检，"
            f"{len(g['suspect'])} 处疑似误报；清单在 vault 包"
        )
    except Exception as exc:  # noqa: BLE001
        return f"NER 复核失败（{type(exc).__name__}）：{str(exc)[:80]}"


def _safe_case_name(raw: str) -> str:
    """案件名会进路径，必须去掉路径分隔符与控制字符。"""
    name = (raw or "").strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = name.strip(". ") or "未命名案件"
    return name[:60]


def _as_count(value) -> int:
    """pipeline 的 replacements 可能是 int、list 或 None，统一折成计数。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return len(value)
    except TypeError:
        return 0


def build_zip(root: pathlib.Path, zip_path: pathlib.Path,
              exclude_top: set[str] | None = None) -> None:
    """打包 root 下的文件。exclude_top 用于剔除顶层目录（如 vault）。"""
    skip = exclude_top or set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(root.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(root)
            if rel.parts and rel.parts[0] in skip:
                continue
            zf.write(item, rel)


class Handler(BaseHTTPRequestHandler):
    server_version = "MaskLocal/0.1"

    # ---------------------------------------------------------------- 基础

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---------------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = (WEB_DIR / "index.html").read_bytes()
            self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
            return
        if path.startswith("/download/"):
            token = path[len("/download/"):]
            with _LOCK:
                hit = _DOWNLOADS.get(token)
            if not hit or not hit[0].exists():
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "下载已过期，请重新处理"})
                return
            data = hit[0].read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="masked-{token[:8]}.zip"',
            )
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知路径"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/mask":
                self._handle_mask()
            elif path == "/api/restore":
                self._handle_restore()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知接口"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            )

    # ---------------------------------------------------------------- 还原

    def _handle_restore(self) -> None:
        """把编辑过的脱敏件还原成真名。

        输入是两个文件：
          - 编辑后的脱敏件（docx/xlsx/pdf/txt）
          - vault 包（zip，含加密映射表与 .key）

        只替换代号与 XXX 掩码，其余文字原样保留——也就是
        「【熊猫】AAA【金鱼】」被编辑成「【熊猫】BBB【金鱼】」后，
        还原得到「张三BBB李四」。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "空请求"})
            return
        if length > MAX_UPLOAD:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                       {"ok": False, "error": "文件过大（上限 512MB）"})
            return

        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        files, _fields = parse_multipart(ctype, body)

        target: tuple[str, bytes] | None = None
        vault_zip: bytes | None = None
        for _field, filename, payload in files:
            safe = pathlib.Path(filename).name
            if not safe:
                continue
            if safe.lower().endswith(".zip"):
                vault_zip = payload
            elif target is None:
                target = (safe, payload)

        if target is None:
            self._json(HTTPStatus.BAD_REQUEST,
                       {"ok": False, "error": "没有收到待还原的文件"})
            return
        if vault_zip is None:
            self._json(HTTPStatus.BAD_REQUEST,
                       {"ok": False, "error": "没有收到 vault 包（zip）"})
            return

        from masking.restore import RestoreError, restore_file
        from masking.vault import Vault, VaultError

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="mask-restore-"))
        try:
            vault_dir = tmp / "vault"
            vault_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(io.BytesIO(vault_zip)) as zf:
                    # 只解压映射表与密钥，其余忽略，避免压缩包夹带路径穿越
                    for member in zf.namelist():
                        base = pathlib.Path(member).name
                        if not base or base.startswith("."):
                            if base != ".key":
                                continue
                        if not (base.endswith(".json") or base == ".key"):
                            continue
                        (vault_dir / base).write_bytes(zf.read(member))
            except zipfile.BadZipFile:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"ok": False, "error": "vault 包不是有效的 zip"})
                return

            maps = sorted(p for p in vault_dir.glob("*.json"))
            keys = sorted(vault_dir.glob(".key"))
            if not maps:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"ok": False, "error": "vault 包里没有映射表（*.json）"})
                return
            if not keys:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"ok": False, "error": "vault 包里没有密钥（.key），无法解密映射表"})
                return

            try:
                vault = Vault.load(str(maps[0]))
            except VaultError as exc:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"ok": False, "error": f"映射表无法读取：{exc}"})
                return

            src = tmp / target[0]
            src.write_bytes(target[1])
            out_dir = tmp / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{src.stem}_已还原{src.suffix}"

            try:
                restore_file(src, out, vault)
            except RestoreError as exc:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"ok": False, "error": f"还原失败：{exc}"})
                return

            if not out.exists():
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"ok": False, "error": "还原后未找到输出文件"})
                return

            # 留一份到 output/，与脱敏产物一样可用文件管理器取用
            OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            keep = OUTPUT_ROOT / f"{stamp}-已还原"
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, keep / out.name)

            zip_path = OUTPUT_ROOT / f"{stamp}-已还原.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(out, out.name)

            token = uuid.uuid4().hex
            with _LOCK:
                _DOWNLOADS[token] = (zip_path, datetime.now())
            _cleanup_old_downloads()

            wsl_path = str(keep)
            win_path = "\\\\wsl.localhost\\Ubuntu" + wsl_path.replace("/", "\\")
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "file": out.name,
                    "download": f"/download/{token}",
                    "path_hint": f"文件管理器：{win_path}\nWSL 路径：{wsl_path}",
                    "note": "还原件含真实姓名，只留本地，不要上传到联网环境。",
                },
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 主流程

    def _handle_mask(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "空请求"})
            return
        if length > MAX_UPLOAD:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                       {"ok": False, "error": "文件过大（上限 512MB）"})
            return

        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        files, fields = parse_multipart(ctype, body)
        if not files:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "没有收到文件"})
            return

        level = fields.get("level", "full")
        case_name = _safe_case_name(fields.get("case_name", ""))

        from masking.config import Settings  # 延迟导入，便于给出友好报错
        from masking.pipeline import mask_path

        settings = apply_level(Settings.load(), level)

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="mask-web-"))
        try:
            # 输入目录名就是案件名：pipeline 用它作为脱敏作用域，
            # 映射表、报告都会以这个名字落盘，用户下载后一眼能对上。
            in_dir = tmp / case_name
            out_dir = tmp / "out"
            in_dir.mkdir(parents=True, exist_ok=True)

            converted_notes: list[str] = []
            for _field, filename, payload in files:
                safe = pathlib.Path(filename).name  # 去掉客户端可能带来的路径
                if not safe:
                    continue
                dest = in_dir / safe
                dest.write_bytes(payload)
                new_path, note = convert_legacy(dest)
                if new_path is not None and note:
                    converted_notes.append(f"{safe}：{note}")

            run = mask_path(str(in_dir), out_dir=str(out_dir), settings=settings)

            # 结果另存一份到 output/，便于用文件管理器取用
            OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            keep = OUTPUT_ROOT / stamp
            if out_dir.exists():
                shutil.copytree(out_dir, keep, dirs_exist_ok=True)

            # NER 复核（可选）：必须写进 keep/vault 并在打 zip 之前完成，
            # 否则清单不会进下载包。
            ner_note = ""
            if (fields.get("ner") or "").lower() in ("1", "true", "on", "yes"):
                ner_note = _run_ner_review(in_dir, out_dir, keep)

            # 报告是在临时目录里生成的，路径写的是 /tmp/mask-web-xxx；
            # 复制到 output/ 后要把这些路径改写成用户实际能打开的路径，
            # 否则用户照着报告去找目录会扑空。
            for md in (keep / "脱敏报告.md", keep / "vault" / "复核清单.md"):
                if md.exists():
                    try:
                        text = md.read_text(encoding="utf-8")
                        text = text.replace(str(out_dir), str(keep))
                        md.write_text(text, encoding="utf-8")
                    except OSError:
                        pass

            # zip 必须落在 output/ 而不是临时目录——临时目录在 finally 里会被删，
            # 下载链接随即 404。这个坑实测踩过。
            # 两个包分开：可交付包不含 vault（vault 里有真名复核清单与加密映射表），
            # 避免用户把整包一起上传给外部工作台。
            src_tree = keep if keep.exists() else out_dir
            zip_deliver = OUTPUT_ROOT / f"{stamp}-可交付.zip"
            build_zip(src_tree, zip_deliver, exclude_top={"vault"})

            zip_vault: pathlib.Path | None = OUTPUT_ROOT / f"{stamp}-vault.zip"
            if (src_tree / "vault").exists():
                build_zip(src_tree / "vault", zip_vault)
            else:
                zip_vault = None

            token = uuid.uuid4().hex
            token_vault = uuid.uuid4().hex
            with _LOCK:
                _DOWNLOADS[token] = (zip_deliver, datetime.now())
                if zip_vault is not None:
                    _DOWNLOADS[token_vault] = (zip_vault, datetime.now())
            _cleanup_old_downloads()

            processed, review, failed = [], [], []
            review.extend(converted_notes)
            if ner_note:
                review.append(ner_note)
            replacements = 0
            entities = 0
            for f in run.files:
                name = pathlib.Path(f.source).name
                if f.ok:
                    processed.append(f"{name}（{f.entities} 个实体）")
                    entities += f.entities
                    replacements += _as_count(getattr(f, "replacements", None))
                elif f.error:
                    failed.append(f"{name}：{f.error}")
                if getattr(f, "needs_review", None):
                    review.extend(f"{name}：{r}" for r in f.needs_review)

            report = keep / "脱敏报告.md"
            if report.exists():
                review.append(f"报告已生成：{report.name}（零真名，可随脱敏件一起交付）")

            wsl_path = str(keep)
            # 文件管理器要 UNC 形式：\\wsl.localhost\Ubuntu\home\...
            win_path = "\\\\wsl.localhost\\Ubuntu" + wsl_path.replace("/", "\\")

            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "counts": {
                        "files": len(processed),
                        "entities": entities,
                        "replacements": replacements,
                        "review": len(review),
                    },
                    "lists": {"processed": processed, "review": review, "failed": failed},
                    "download": f"/download/{token}",
                    "download_vault": f"/download/{token_vault}" if zip_vault else "",
                    "path_hint": f"文件管理器：{win_path}\nWSL 路径：{wsl_path}",
                },
            )
        finally:
            # 保留 output 中的副本，清理上传与中间文件
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="本地脱敏 Web 界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not (WEB_DIR / "index.html").exists():
        print(f"缺少前端文件: {WEB_DIR / 'index.html'}", file=sys.stderr)
        return 1

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"本地脱敏界面 → http://{args.host}:{args.port}")
    print("仅监听本机回环地址，文件不会离开这台机器。Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
