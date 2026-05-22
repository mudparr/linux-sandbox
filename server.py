import os
import base64
import shutil
import asyncio
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

def _load_token() -> str:
    token_file = os.environ.get("SANDBOX_TOKEN_FILE", "/opt/sandbox/token")
    try:
        with open(token_file) as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    return os.environ.get("SANDBOX_TOKEN", "")


TOKEN = _load_token()
WORKSPACE = Path(os.environ.get("SANDBOX_WORKSPACE", "/workspace"))
DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_EXEC_TIMEOUT", "600"))

WORKSPACE.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Sandbox", version="1.0.0")


def check_auth(token: str | None):
    if not TOKEN:
        raise HTTPException(status_code=503, detail="Token not configured")
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


class ExecRequest(BaseModel):
    command: str
    cwd: str | None = None
    timeout: int | None = None
    env: dict[str, str] | None = None


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


class WriteRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"


class ReadRequest(BaseModel):
    path: str
    encoding: str = "utf-8"


def resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = WORKSPACE / p
    return p


@app.get("/health")
async def health():
    return {"status": "ok", "workspace": str(WORKSPACE)}


@app.post("/exec", response_model=ExecResponse)
async def exec_command(
    req: ExecRequest,
    x_sandbox_token: str | None = Header(default=None),
):
    check_auth(x_sandbox_token)

    workdir = resolve(req.cwd) if req.cwd else WORKSPACE
    workdir.mkdir(parents=True, exist_ok=True)

    timeout = req.timeout or DEFAULT_TIMEOUT
    env = {**os.environ, **(req.env or {})}

    proc = await asyncio.create_subprocess_shell(
        req.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workdir),
        env=env,
    )

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()

    return ExecResponse(
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        timed_out=timed_out,
    )


@app.post("/write")
async def write_file(
    req: WriteRequest,
    x_sandbox_token: str | None = Header(default=None),
):
    check_auth(x_sandbox_token)
    target = resolve(req.path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if req.encoding == "base64":
        target.write_bytes(base64.b64decode(req.content))
    else:
        target.write_text(req.content, encoding="utf-8")

    return {"path": str(target), "bytes": target.stat().st_size}


@app.post("/read")
async def read_file(
    req: ReadRequest,
    x_sandbox_token: str | None = Header(default=None),
):
    check_auth(x_sandbox_token)
    target = resolve(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {target}")

    if req.encoding == "base64":
        content = base64.b64encode(target.read_bytes()).decode("ascii")
    else:
        content = target.read_text(encoding="utf-8", errors="replace")

    return {"path": str(target), "content": content, "encoding": req.encoding}


@app.get("/ls")
async def list_dir(
    path: str = ".",
    x_sandbox_token: str | None = Header(default=None),
):
    check_auth(x_sandbox_token)
    target = resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {target}")

    entries = []
    for child in sorted(target.iterdir()):
        entries.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "bytes": child.stat().st_size if child.is_file() else None,
        })
    return {"path": str(target), "entries": entries}


@app.post("/reset")
async def reset_workspace(
    x_sandbox_token: str | None = Header(default=None),
):
    check_auth(x_sandbox_token)
    for child in WORKSPACE.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    return {"status": "workspace cleared", "workspace": str(WORKSPACE)}
