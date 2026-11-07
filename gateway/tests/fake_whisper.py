"""A stand-in for whisper.cpp's whisper-server: says how much audio it got and
logs the request fields (one JSON line per request) to --fake-log."""

import argparse
import io
import json
import wave

from aiohttp import web


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--fake-log", required=True)
    args, _ = parser.parse_known_args()

    async def index(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def inference(request: web.Request) -> web.Response:
        form = await request.post()
        upload = form["file"]
        with wave.open(io.BytesIO(upload.file.read())) as w:
            seconds = w.getnframes() / w.getframerate()
        fields = {k: v for k, v in form.items() if k != "file"}
        with open(args.fake_log, "a") as log:
            log.write(json.dumps({"seconds": seconds, **fields}) + "\n")
        return web.json_response({"text": f" heard {seconds:.1f}s\n"})

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_post("/inference", inference)
    web.run_app(app, host=args.host, port=args.port, print=None)


main()
