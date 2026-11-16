"""Modal sandbox for re-runs: phase A (install, network on) and phase B (run, network off). (Phase 10)

Planning is local code: dependency files, imports of the paper's code mapped to pip names, Python version, and
which model providers the code calls. Reuses the planning and image choices of vendor/code_test_harness (not
its network policy).

Secrets (CLAUDE.md rule 7): phase B of a paper whose code calls OpenAI, Anthropic or Gemini gets a
`modal.Secret.from_dict` holding only the matching SANDBOX_* keys that are set (exported as OPENAI_API_KEY,
ANTHROPIC_API_KEY, GEMINI_API_KEY and GOOGLE_API_KEY) and an outbound allowlist of those providers' hosts only.
Phase A never gets a secret. The Berget key, the Modal tokens and HF_TOKEN are never passed. A paper that needs a
provider whose key is unset is not run ("needs external model").
"""

from __future__ import annotations

import ast
import io
import re
import shlex
import sys
import tarfile
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from onus.codecheck.common import SKIP_DIRS, mask_secrets

MAX_UPLOAD_FILE = 100 * 1024 * 1024
OUTPUT_KEEP = 300_000  # bytes of each command's output kept (the tail) for the agent and for compare.py
PROVIDERS = {
    "openai": (re.compile(r"\bimport openai\b|\bfrom openai\b|api\.openai\.com|\bOpenAI\(|litellm|langchain_openai"),
               ("OPENAI_API_KEY",), "sandbox_openai_api_key"),
    "anthropic": (re.compile(r"\bimport anthropic\b|\bfrom anthropic\b|api\.anthropic\.com|langchain_anthropic"),
                  ("ANTHROPIC_API_KEY",), "sandbox_anthropic_api_key"),
    "gemini": (re.compile(r"google\.generativeai|google\.genai|from google import genai|generativelanguage\."
                          r"googleapis\.com|langchain_google_genai"),
               ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "sandbox_gemini_api_key"),
}
# Other external services the sandbox cannot reach in phase B.
EXTERNAL = re.compile(r"\bimport (?:boto3|wandb|pymongo|psycopg2?|redis)\b|\bfrom (?:boto3|wandb|pymongo|redis)\b|"
                      r"api\.(?:together|groq|mistral|cohere|deepseek|perplexity)\.|\bHF_TOKEN\b|\bvllm\b")
GPU = re.compile(r"\.cuda\(|device\s*=\s*['\"]cuda['\"]|\bvllm\b|bitsandbytes|flash_attn")
PIP_NAME = {"fitz": "pymupdf", "cv2": "opencv-python-headless", "sklearn": "scikit-learn", "PIL": "pillow",
            "yaml": "pyyaml", "dotenv": "python-dotenv", "bs4": "beautifulsoup4", "skimage": "scikit-image",
            "Bio": "biopython", "docx": "python-docx", "pptx": "python-pptx", "google": "google-genai",
            "dateutil": "python-dateutil", "attr": "attrs", "jose": "python-jose", "Crypto": "pycryptodome",
            "gym": "gymnasium", "faiss": "faiss-cpu", "igraph": "python-igraph", "umap": "umap-learn"}
BASE_PACKAGES = ("numpy", "pandas", "scipy", "matplotlib", "seaborn", "scikit-learn", "statsmodels", "networkx",
                 "sympy", "tqdm", "requests", "pyyaml", "python-dotenv", "jupyter", "nbconvert", "ipykernel",
                 "openai", "anthropic", "google-genai", "google-generativeai", "tiktoken",
                 "torch", "transformers", "datasets", "accelerate", "sentence-transformers", "pytest")
APT_PACKAGES = ("git", "build-essential", "r-base-core", "graphviz", "libgl1", "libglib2.0-0")


@dataclass
class RerunPlan:
    files: list[str]
    python: str
    requirements: list[str]
    projects: list[str]
    extra_pip: list[str]
    providers: list[str]
    external: list[str]
    gpu_hint: bool
    entries: list[str]
    missing_keys: list[str] = field(default_factory=list)


def _files(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*")) if p.is_file() and not (SKIP_DIRS & set(p.relative_to(root).parts))
            and p.stat().st_size <= MAX_UPLOAD_FILE]


def _imports(src: str) -> set[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # paper code may hold invalid escape sequences
            tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w]*)", src, re.MULTILINE))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            out.add(n.module.split(".")[0])
    return out


def plan_rerun(root: Path, entries: list[str], settings, python_default: str = "3.11") -> RerunPlan:
    files = _files(root)
    rel = [p.relative_to(root).as_posix() for p in files]
    py = [p for p in files if p.suffix == ".py"]
    src = "\n".join(p.read_text(errors="ignore")[:300_000] for p in py)
    reqs = [r for r, p in zip(rel, files, strict=True) if re.match(r"requirements.*\.txt$", p.name, re.IGNORECASE)]
    projects = sorted({(Path(r).parent.as_posix() or ".") for r, p in zip(rel, files, strict=True)
                       if p.name in ("pyproject.toml", "setup.py")})
    meta = "\n".join(p.read_text(errors="ignore") for p in files if p.name in ("pyproject.toml", "setup.py",
                                                                                "setup.cfg"))
    python = "3.12" if re.search(r"requires[-_]python\s*=\s*['\"][^'\"]*>=\s*3\.1[2-9]", meta) else python_default
    extra: list[str] = []
    if not reqs and not projects:  # no dependency file: imports that are neither stdlib, local nor preinstalled
        local = {p.stem for p in py} | {d.name for d in root.rglob("*") if d.is_dir()}
        std = set(sys.stdlib_module_names)
        base = {b.replace("-", "_").lower() for b in BASE_PACKAGES} | {"sklearn", "yaml", "dotenv", "google"}
        for mod in sorted(_imports(src)):
            if mod in std or mod in local or mod.lower() in base or mod.startswith("_"):
                continue
            extra.append(PIP_NAME.get(mod, mod.replace("_", "-")))
    providers = [name for name, (pat, _, _) in PROVIDERS.items() if pat.search(src)]
    missing = [name for name in providers if not getattr(settings, PROVIDERS[name][2]).get_secret_value()]
    external = sorted({m.group(0) for m in EXTERNAL.finditer(src)})
    return RerunPlan(files=rel, python=python, requirements=reqs, projects=projects, extra_pip=extra[:30],
                     providers=providers, external=external, gpu_hint=bool(GPU.search(src)), entries=entries,
                     missing_keys=missing)


def provider_secret_values(plan: RerunPlan, settings) -> dict[str, str]:
    """Env name -> key value for the providers this paper's code calls, only for keys that are set. Never printed."""
    out: dict[str, str] = {}
    for name in plan.providers:
        _, envs, attr = PROVIDERS[name]
        val = getattr(settings, attr).get_secret_value()
        if val:
            out |= {e: val for e in envs}
    return out


def code_tarball(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in _files(root):
            tf.add(p, arcname=p.relative_to(root).as_posix())
    return buf.getvalue()


# ---------------------------------------------------------------- sandbox interface


@dataclass
class ExecResult:
    rc: int
    output: str  # stdout + stderr, masked
    seconds: float
    timed_out: bool = False


class Sandbox(Protocol):
    async def exec(self, cmd: str, timeout: int) -> ExecResult: ...
    async def read_text(self, path: str) -> str: ...
    async def write_text(self, path: str, content: str) -> None: ...
    async def terminate(self) -> None: ...


def wrap_command(cmd: str, timeout: int, work: str = "/work", tmp: str = "/tmp") -> str:
    """The bash script that runs one command: in `work`, under `timeout`, with `set -o pipefail` (so `... | tee`
    or `| tail` keeps the program's exit code), stdin closed, the output tail and the exit code written to files."""
    inner = "set -o pipefail\n" + cmd
    return (f"cd {shlex.quote(work)} && timeout {int(timeout)} bash -lc {shlex.quote(inner)} > {tmp}/_out.log 2>&1 "
            f"< /dev/null; echo $? > {tmp}/_rc; tail -c {OUTPUT_KEEP} {tmp}/_out.log > {tmp}/_tail.log")


class ModalSandbox:
    """Thin async wrapper over a running modal.Sandbox (Modal's .aio methods); every output is masked before it is
    returned."""

    def __init__(self, sb, known_secrets: list[str]):
        self.sb = sb
        self.known = known_secrets

    async def exec(self, cmd: str, timeout: int) -> ExecResult:
        t0 = time.monotonic()
        try:
            p = await self.sb.exec.aio("bash", "-c", wrap_command(cmd, timeout), timeout=int(timeout) + 60)
            await p.wait.aio()
            rc = int((await self.sb.filesystem.read_text.aio("/tmp/_rc") or "1").strip() or 1)
            out = await self.sb.filesystem.read_text.aio("/tmp/_tail.log")
        except Exception as e:  # noqa: BLE001 - a sandbox error is reported to the agent, never raised
            return ExecResult(rc=-1, output=mask_secrets(f"sandbox error: {type(e).__name__}: {e}", self.known),
                              seconds=time.monotonic() - t0)
        return ExecResult(rc=rc, output=mask_secrets(out, self.known), seconds=time.monotonic() - t0,
                          timed_out=rc == 124)

    async def read_text(self, path: str) -> str:
        return mask_secrets(await self.sb.filesystem.read_text.aio(path), self.known)

    async def write_text(self, path: str, content: str) -> None:
        await self.sb.filesystem.write_text.aio(content, path)

    async def terminate(self) -> None:
        try:
            await self.sb.terminate.aio()
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup
            pass


def base_image(python: str):
    import modal

    return (modal.Image.debian_slim(python_version=python).apt_install(*APT_PACKAGES).pip_install(*BASE_PACKAGES)
            .pip_install("uv"))


INSTALL_STEP = """step() {{
  t=$1; shift
  timeout "$t" uv pip install --system "$@"; rc=$?
  if [ "$rc" = 124 ]; then echo "INSTALL_TIMEOUT after ${{t}}s: $*"; return 1; fi
  if [ "$rc" != 0 ]; then echo "uv failed (rc $rc), trying pip: $*"; timeout "$t" pip install -q "$@" || return 1; fi
}}"""


def install_script(plan: RerunPlan, budget_s: int = 840) -> str:
    """Phase A script. Dependencies are installed with uv (pip only as a fallback when uv errors): pip's
    backtracking resolver can spend more than ten minutes on a large dependency tree that uv resolves in
    seconds. Each step is capped by the phase-A budget; a timeout is marked INSTALL_TIMEOUT in the log."""
    t = max(60, min(600, int(budget_s)))
    lines = ["mkdir -p /work && tar -xzf /tmp/code.tar.gz -C /work && rm /tmp/code.tar.gz", INSTALL_STEP.format(),
             "set -x", "fail=0"]
    for r in plan.requirements:
        lines.append(f"step {t} -r {shlex.quote('/work/' + r)} || fail=1")
    for d in plan.projects:
        lines.append(f"step {t} -e {shlex.quote('/work/' + d)} || fail=1")
    if plan.extra_pip:
        lines.append(f"step {t} " + " ".join(shlex.quote(x) for x in plan.extra_pip) + " || fail=1")
    lines.append("echo INSTALL_FAIL=$fail")
    return "\n".join(lines) + "\n"


@dataclass
class Prepared:
    sandbox: Sandbox | None
    install_log: str
    install_failed: bool
    minutes_a: float
    error: str = ""


async def prepare_modal(root: Path, plan: RerunPlan, *, app_name: str, total_minutes: float,
                        install_minutes: float, min_run_minutes: float, secret_values: dict[str, str],
                        provider_hosts: list[str], known_secrets: list[str]) -> Prepared:
    """Phase A then phase B (Modal's .aio API). Returns the running phase-B sandbox (caller terminates it). Phase B's
    timeout is what is left of `total_minutes` after phase A (at least `min_run_minutes`). When phase A fails, no
    phase-B sandbox is started (install_failed, sandbox None)."""
    from onus.config import ensure_modal_auth

    if not ensure_modal_auth():
        return Prepared(None, "", False, 0.0, "Modal tokens are not set")
    import modal

    t0 = time.monotonic()
    sb = None
    try:
        app = await modal.App.lookup.aio(app_name, create_if_missing=True)
        sb = await modal.Sandbox.create.aio(app=app, image=base_image(plan.python), timeout=int(install_minutes * 60),
                                            cpu=2.0, memory=8192, workdir="/work")
        await sb.filesystem.write_bytes.aio(code_tarball(root), "/tmp/code.tar.gz")
        await sb.filesystem.write_text.aio(install_script(plan, int(install_minutes * 60) - 60), "/tmp/install.sh")
        p = await sb.exec.aio("bash", "-c", "bash /tmp/install.sh > /tmp/install.log 2>&1",
                              timeout=int(install_minutes * 60))
        await p.wait.aio()
        log = mask_secrets(await sb.filesystem.read_text.aio("/tmp/install.log"), known_secrets)
        failed = "INSTALL_FAIL=0" not in log
        image = None if failed else await sb.snapshot_filesystem.aio()
    except Exception as e:  # noqa: BLE001
        return Prepared(None, "", True, (time.monotonic() - t0) / 60,
                        mask_secrets(f"phase A: {type(e).__name__}: {e}", known_secrets)[:400])
    finally:
        if sb is not None:
            try:
                await sb.terminate.aio()
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass
    minutes_a = (time.monotonic() - t0) / 60
    if failed:
        return Prepared(None, log, True, minutes_a, "install failed")
    run_s = int(max(min_run_minutes * 60, total_minutes * 60 - (time.monotonic() - t0)))
    kw: dict = {"app": app, "image": image, "timeout": run_s, "cpu": 2.0, "memory": 8192, "workdir": "/work"}
    if secret_values:
        kw["secrets"] = [modal.Secret.from_dict(secret_values)]
        kw["outbound_domain_allowlist"] = provider_hosts
    else:
        kw["block_network"] = True
    try:
        sb2 = await modal.Sandbox.create.aio(**kw)
    except Exception as e:  # noqa: BLE001
        return Prepared(None, log, False, minutes_a,
                        mask_secrets(f"phase B: {type(e).__name__}: {e}", known_secrets)[:400])
    return Prepared(ModalSandbox(sb2, known_secrets), log, False, minutes_a)
