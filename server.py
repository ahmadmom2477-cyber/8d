"""Local 8D audio processor. Run with: python server.py"""
from __future__ import annotations
import html, json, mimetypes, os, shutil, subprocess, tempfile, urllib.parse, sys
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 100 * 1024 * 1024

def run(command, cwd=None):
    return subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

def safe_float(value, default, low, high):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default

def process_audio(source: Path, output: Path, speed: float, depth: float, echo: float):
    # Tuned against the supplied Aurora reference: wider side energy, lower LR
    # correlation and a slow, audible orbit without destroying the center image.
    amount = 0.30 + (depth / 100.0) * 0.58
    echo_gain = (echo / 100.0) * 0.18
    dry = 1.0 - (echo / 100.0) * 0.22
    filters = (
        "aresample=48000:async=1:first_pts=0," 
        "aformat=sample_fmts=fltp:channel_layouts=stereo," 
        f"apulsator=mode=sine:amount={amount:.4f}:offset_l=0:offset_r=0.5:width=1:hz={speed:.4f},"
        f"aecho=in_gain=1.0:out_gain={echo_gain:.4f}:delays=120|240:decays=0.08|0.04,"
        f"volume={dry:.4f},alimiter=limit=0.96:attack=5:release=50:level=disabled,"
        "loudnorm=I=-14:TP=-1.0:LRA=7:linear=false"
    )
    run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn", "-af", filters,
         "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(output)])

def download_youtube(url: str, template: str):
    executable = shutil.which("yt-dlp")
    command = [executable, "--no-playlist", "-f", "bestaudio/best", "-o", template, url] if executable else [sys.executable, "-m", "yt_dlp", "--no-playlist", "-f", "bestaudio/best", "-o", template, url]
    run(command)

class Handler(BaseHTTPRequestHandler):
    server_version = "Thamanya8D/1.0"
    def log_message(self, fmt, *args):
        print(fmt % args)

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health": return self.send_json(200, {"ok": True, "engine": "ffmpeg"})
        if path == "/" : path = "/index.html"
        file_path = (ROOT / path.lstrip("/")).resolve()
        if ROOT not in file_path.parents and file_path != ROOT: return self.send_error(403)
        if not file_path.is_file(): return self.send_error(404)
        data = file_path.read_bytes(); self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/process": return self.send_error(404)
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD + 10_000_000: return self.send_json(413, {"error":"الملف أكبر من 100MB"})
        temp_dir = Path(tempfile.mkdtemp(prefix="thamanya8d-"))
        try:
            body = self.rfile.read(length)
            message = BytesParser(policy=policy.default).parsebytes((f"Content-Type: {self.headers.get('Content-Type', '')}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            fields, files = {}, {}
            for part in message.iter_parts():
                disposition = part.get("Content-Disposition", "")
                params = dict(part.get_params(header="Content-Disposition", failobj=[]))
                name = params.get("name")
                if not name: continue
                payload = part.get_payload(decode=True) or b""
                if params.get("filename"):
                    files[name] = (params.get("filename"), payload)
                else:
                    fields[name] = payload.decode("utf-8", "ignore")
            speed = safe_float(fields.get("speed"), .22, .05, 1.5)
            depth = safe_float(fields.get("depth"), 85, 0, 100)
            echo = safe_float(fields.get("echo"), 12, 0, 35)
            source = None
            url = (fields.get("url") or "").strip()
            if url:
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme not in ("http", "https") or not ("youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc):
                    return self.send_json(400, {"error":"الرابط يجب أن يكون من YouTube"})
                template = str(temp_dir / "download.%(ext)s")
                download_youtube(url, template)
                candidates = [p for p in temp_dir.iterdir() if p.name.startswith("download.")]
                if not candidates: raise RuntimeError("لم يتم تنزيل الصوت")
                source = candidates[0]
            elif "file" in files:
                filename, payload = files["file"]
                if len(payload) > MAX_UPLOAD: return self.send_json(413, {"error":"الملف أكبر من 100MB"})
                source = temp_dir / Path(filename).name
                source.write_bytes(payload)
            else:
                return self.send_json(400, {"error":"اختر ملفًا أو ضع رابط YouTube"})
            output = temp_dir / "thamanya-8d.wav"
            process_audio(source, output, speed, depth, echo)
            data = output.read_bytes(); name = Path(source).stem + "-8d.wav"
            self.send_response(200); self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Disposition", f'attachment; filename="{name.encode("ascii", "ignore").decode() or "thamanya-8d"}.wav"')
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", "ignore")[-600:]
            self.send_json(500, {"error":"فشلت المعالجة", "detail": detail})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4173"))
    print(f"ثمانية 8D engine listening on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
