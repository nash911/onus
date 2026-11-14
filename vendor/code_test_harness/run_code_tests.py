"""Test whether each paper's submitted code installs and runs, in isolated Modal sandboxes.

Vendored from the code-triage harness that graded the corpus before the build (code only: no
results, reports or grades). Changes from the original: data root and output folder are flags;
papers are found from the data root instead of a label file; sandbox keys come from the
SANDBOX_* settings (CLAUDE.md rule 7); HF_TOKEN is never passed.

Usage (from the repo root):
    poetry run python vendor/code_test_harness/run_code_tests.py                       # dev papers with code
    poetry run python vendor/code_test_harness/run_code_tests.py --ids X Y
    poetry run python vendor/code_test_harness/run_code_tests.py --data-root data/dev --out var/code_tests

Per paper, in a fresh sandbox (no access to this machine; outbound network limited to package
indexes, GitHub, Hugging Face and the OpenAI / Anthropic / Google LLM APIs):
  1. upload the code (without .git, caches and files > 100 MB)
  2. syntax check: python -m compileall
  3. install: every requirements*.txt, pyproject.toml / setup.py (pip install -e), environment.yml pip deps
  4. run up to MAX_CMDS entry points (README commands first, then likely entry files, then notebooks),
     each under a timeout
  5. classify each run from its exit code and log tail
Results: <out>/<forum_id>/{plan.json,setup.log,run_N.log,result.json}
         <out>/code_test_results.csv  (one row per paper; rebuilt from result.json files)
The data root is only read; nothing is written into it.
"""

import argparse
import csv
import io
import json
import os
import re
import shlex
import sys
import tarfile
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]
load_dotenv(REPO / ".env")
import modal  # noqa: E402  (after .env so MODAL_TOKEN_* are picked up)

DATA = REPO / "data" / "dev"        # overridden by --data-root
OUT = REPO / "var" / "code_tests"   # overridden by --out
MAX_FILE = 100 * 1024 * 1024
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", ".ipynb_checkpoints", ".mypy_cache"}
MAX_CMDS = 3
SOURCE_EXT = {".py", ".ipynb", ".r", ".sh", ".js", ".ts", ".jl", ".m", ".cpp", ".c", ".java", ".go", ".rs"}
# sandbox env name -> setting it is read from (CLAUDE.md rule 7: only these, never HF_TOKEN or Onus's own keys)
SANDBOX_KEYS = {"OPENAI_API_KEY": "SANDBOX_OPENAI_API_KEY", "ANTHROPIC_API_KEY": "SANDBOX_ANTHROPIC_API_KEY",
                "GEMINI_API_KEY": "SANDBOX_GEMINI_API_KEY", "GOOGLE_API_KEY": "SANDBOX_GEMINI_API_KEY"}
ALLOWED_DOMAINS = [
    "pypi.org", "files.pythonhosted.org", "download.pytorch.org",
    "github.com", "codeload.github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
    "huggingface.co", "cdn-lfs.huggingface.co", "cdn-lfs-us-1.huggingface.co", "cas-bridge.xethub.hf.co", "cas-server.xethub.hf.co", "transfer.xethub.hf.co", "us.aws.cdn.hf.co", "eu.aws.cdn.hf.co", "cdn-lfs.hf.co",
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    # common public dataset hosts
    "ossci-datasets.s3.amazonaws.com", "storage.googleapis.com", "zenodo.org", "archive.ics.uci.edu",
    "dl.fbaipublicfiles.com", "www.cs.toronto.edu",
]
GPU_RE = re.compile(r"\.cuda\(|device\s*=\s*['\"]cuda|torch\.cuda\.is_available|\bvllm\b|bitsandbytes|flash_attn")

def make_image(pyver):
    return (
    modal.Image.debian_slim(python_version=pyver)
    .apt_install("git", "build-essential", "r-base-core", "graphviz", "libgl1", "libglib2.0-0")
    .pip_install(
        "numpy", "pandas", "scipy", "matplotlib", "seaborn", "scikit-learn", "statsmodels", "networkx",
        "sympy", "tqdm", "requests", "pyyaml", "python-dotenv", "jupyter", "nbconvert", "ipykernel",
        "openai", "anthropic", "google-genai", "google-generativeai", "tiktoken",
        "torch", "transformers", "datasets", "accelerate", "sentence-transformers", "pytest",
    )
    )


IMAGES = {"3.11": make_image("3.11"), "3.12": make_image("3.12")}


# ---------------------------------------------------------------- local planning
def code_files(code_dir):
    for p in code_dir.rglob("*"):
        if p.is_file() and not (SKIP_DIRS & set(p.relative_to(code_dir).parts)) and p.stat().st_size <= MAX_FILE:
            yield p


CMD_RE = re.compile(r"^\s*(?:\$\s*|>\s*)?((?:python3?|bash|sh|Rscript|jupyter\s+nbconvert|streamlit\s+run|make)\b[^\n#]*)", re.M)
ENTRY_NAMES = re.compile(r"^(main|run|app|demo|train|experiment|experiments|pipeline|simulate|simulation|analysis|"
                         r"run_.*|main_.*|experiment_.*|reproduce.*|run_all.*)\.py$", re.I)


def py_cmd(p, code_dir, rel, source):
    """Run a file inside a package as `python -m pkg.mod` from the package's parent (keeps relative imports working)."""
    top = p.parent
    while (top / "__init__.py").exists() and top != code_dir:
        top = top.parent
    if top != p.parent:
        mod = ".".join(p.relative_to(top).with_suffix("").parts)
        return {"cwd": rel(top) if top != code_dir else ".", "cmd": f"python -m {mod}", "source": source}
    return {"cwd": rel(p.parent) if p.parent != code_dir else ".", "cmd": f"python {shlex.quote(p.name)}", "source": source}


def plan(fid):
    code_dir = DATA / "papers" / fid / "code"
    files = list(code_files(code_dir))
    rel = lambda p: p.relative_to(code_dir).as_posix()
    cmds = []
    # 1) README commands whose target file exists relative to the README's folder
    for rd in sorted((p for p in files if p.name.lower().startswith("readme")), key=lambda p: len(p.parts)):
        text = rd.read_text(errors="ignore")
        for m in CMD_RE.finditer(text):
            cmd = m.group(1).strip().rstrip("\\").strip()
            if re.search(r"[<>{}]|\.\.\.|your_|path/to|YOUR", cmd) or "install" in cmd:
                continue
            toks = shlex.split(cmd, posix=True) if cmd.count('"') % 2 == 0 and cmd.count("'") % 2 == 0 else cmd.split()
            target = next((t for t in toks[1:] if not t.startswith("-") and re.search(r"\.(py|sh|R|ipynb)$", t)), None)
            module = toks[2] if len(toks) > 2 and toks[1] == "-m" else None
            base = rd.parent
            ok = (target and (base / target).exists()) or (module and ((base / module.replace(".", "/")).exists()
                                                                        or (base / (module.replace(".", "/") + ".py")).exists()))
            if toks[0] == "make":
                ok = (base / "Makefile").exists()
            if ok:
                cmds.append({"cwd": rel(base) if base != code_dir else ".", "cmd": cmd, "source": "readme:" + rel(rd)})
    # 2) likely entry files, shallowest first
    py = [p for p in files if p.suffix == ".py"]
    named = sorted((p for p in py if ENTRY_NAMES.match(p.name)), key=lambda p: (len(p.parts), p.name))
    guarded = sorted((p for p in py if p not in named and "__main__" in p.read_text(errors="ignore")),
                     key=lambda p: (len(p.parts), -p.stat().st_size))
    for p in named + guarded:
        cmds.append(py_cmd(p, code_dir, rel, "entry-file"))
    # 3) notebooks, then R / shell scripts
    for p in sorted((p for p in files if p.suffix == ".ipynb"), key=lambda p: len(p.parts)):
        cmds.append({"cwd": rel(p.parent) if p.parent != code_dir else ".",
                     "cmd": f"jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=-1 "
                            f"--output /tmp/executed.ipynb {shlex.quote(p.name)}", "source": "notebook"})
    for p in sorted((p for p in files if p.suffix in (".R", ".r")), key=lambda p: len(p.parts)):
        cmds.append({"cwd": rel(p.parent) if p.parent != code_dir else ".", "cmd": f"Rscript {shlex.quote(p.name)}",
                     "source": "r-script"})
    for p in sorted((p for p in files if p.suffix == ".sh" and re.search(r"run|main|repro|all", p.name, re.I)),
                    key=lambda p: len(p.parts)):
        cmds.append({"cwd": rel(p.parent) if p.parent != code_dir else ".", "cmd": f"bash {shlex.quote(p.name)}",
                     "source": "shell"})
    # 4) fallback: any top-level-most .py / .sh (largest first)
    if not cmds:
        for p in sorted((p for p in files if p.suffix in (".py", ".sh")), key=lambda p: (len(p.parts), -p.stat().st_size)):
            if p.suffix == ".py":
                cmds.append(py_cmd(p, code_dir, rel, "fallback"))
            else:
                cmds.append({"cwd": rel(p.parent) if p.parent != code_dir else ".", "cmd": f"bash {shlex.quote(p.name)}",
                             "source": "fallback"})
    seen, uniq = set(), []
    for c in cmds:
        key = (c["cwd"], re.sub(r"\s+", " ", c["cmd"]))
        if key not in seen:
            seen.add(key); uniq.append(c)
    reqs = [rel(p) for p in files if re.match(r"requirements.*\.txt$", p.name, re.I)]
    projects = [rel(p.parent) or "." for p in files if p.name in ("pyproject.toml", "setup.py")]
    conda_pip = []
    for p in files:
        if p.name in ("environment.yml", "environment.yaml"):
            text = p.read_text(errors="ignore")
            for line in text.splitlines():
                m = re.match(r"^\s+-\s+([A-Za-z0-9_.\-\[\]]+)\s*(?:[=<>!~].*)?$", line)
                if m and m.group(1).lower() not in ("python", "pip", "conda", "cudatoolkit", "cudnn"):
                    conda_pip.append(m.group(1))
    src = "\n".join(p.read_text(errors="ignore")[:300000] for p in py)
    meta = "\n".join(p.read_text(errors="ignore") for p in files if p.name in ("pyproject.toml", "setup.py", "setup.cfg"))
    needs312 = bool(re.search(r"requires[-_]python\s*=\s*['\"][^'\"]*>=\s*3\.1[2-9]", meta))
    roots = {"."} | {p.relative_to(code_dir).parts[0] for p in files if len(p.relative_to(code_dir).parts) > 1}
    for p in files:
        if p.name.lower().startswith("readme") or p.name in ("requirements.txt", "pyproject.toml", "setup.py"):
            roots.add(rel(p.parent) or ".")
    roots |= {r + "/src" if r != "." else "src" for r in list(roots) if (code_dir / r / "src").is_dir()}
    roots |= {rel(p.parent.parent) or "." for p in files if p.name == "__init__.py" and p.parent != code_dir}
    roots = {r for r in roots if r}
    local_names = sorted({p.stem for p in py} | {p.parent.name for p in py} | {d.name for d in code_dir.rglob("*") if d.is_dir()})
    n_src = sum(p.suffix.lower() in SOURCE_EXT for p in files)
    return {
        "forum_id": fid, "n_files": len(files), "n_py": len(py), "n_source": n_src,
        "n_ipynb": sum(p.suffix == ".ipynb" for p in files),
        "requirements": reqs, "projects": sorted(set(projects)), "conda_pip": sorted(set(conda_pip)),
        "gpu_hint": bool(GPU_RE.search(src)),
        "llm_hint": bool(re.search(r"openai|anthropic|genai|gemini|litellm|langchain", src, re.I)),
        "commands": uniq[:MAX_CMDS], "n_candidates": len(uniq),
        "pythonpath": sorted(roots), "local_names": local_names, "python": "3.12" if needs312 else "3.11",
    }


def bundle(fid):
    code_dir = DATA / "papers" / fid / "code"
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    with tarfile.open(tmp.name, "w:gz") as tf:
        for p in code_files(code_dir):
            tf.add(p, arcname=p.relative_to(code_dir).as_posix())
    return tmp.name


# ---------------------------------------------------------------- classification
PATTERNS = [
    ("needs-input", r"EOFError: EOF when reading a line"),
    ("needs-account", r"wandb.*(login|API key)|huggingface-cli login|gated repo|Please log ?in|401 Client Error.*huggingface"),
    ("missing-system-tool", r": command not found|No such file or directory: '(pdflatex|latex|gs|java|node|ffmpeg|Rscript)'"),
    ("needs-args-none", r"(args\.\w+|argv).*\n.*\n?.*NoneType|NoneType.*args\."),
    ("version-drift", r"has no attribute '(get_cmap|bool|int|float|complex|object|str|warn|append|iteritems|ix|as_matrix)'|"
                      r"np\.matrix is not supported|cannot import name '\w+' from '(sklearn|scipy|numpy|pandas|matplotlib|"
                      r"transformers|langchain|torch|networkx|openai)[\w.]*'|module '(numpy|scipy|pandas|matplotlib|sklearn|"
                      r"networkx|openai)[\w.]*' has no attribute|You tried to access openai\.\w+, but this is no longer supported"),
    ("needs-display", r"no display name|\$DISPLAY|cannot connect to X server|could not connect to display"),
    ("syntax-error", r"SyntaxError|IndentationError|TabError"),
    ("needs-gpu", r"No CUDA GPUs|CUDA (error|driver)|Torch not compiled with CUDA|nvidia-smi|cuda.*not available|"
                  r"Found no NVIDIA driver"),
    ("api-quota", r"insufficient_quota|RateLimitError|429|quota|exceeded your current"),
    ("api-auth", r"AuthenticationError|\b401\b|Unauthorized|invalid.{0,20}api.?key|api.?key.{0,40}(not set|missing|required|provided)|"
                 r"Missing credentials|PermissionDenied|model_not_found|\bNotFoundError\b.*model|OPENROUTER_API_KEY|"
                 r"(DEEPSEEK|TOGETHER|GROQ|MISTRAL|COHERE|PERPLEXITY|XAI|SERPAPI|TAVILY)_API_KEY"),
    ("network-blocked", r"ConnectionError|Name or service not known|Max retries exceeded|Temporary failure in name|"
                        r"Connection refused|ProxyError|ConnectTimeout|getaddrinfo"),
    ("missing-module", r"ModuleNotFoundError|ImportError|No module named"),
    ("missing-file", r"FileNotFoundError|No such file or directory|can't open file|does not exist|not found:|"
                     r"\bnot found\b|No scenarios found|Can't find model|could not find .{0,40}(file|data|dataset)"),
    ("needs-args", r"error: the following arguments are required|usage: .*\n.*error:"),
    ("oom", r"MemoryError|Killed|out of memory"),
    ("needs-display", r"no display name|\$DISPLAY|cannot connect to X server|could not connect to display"),
]


def classify(rc, log):
    out = _classify(rc, log)
    return {"needs-input": "needs-args", "needs-args-none": "needs-args"}.get(out, out)


def _classify(rc, log):
    if rc == 0:
        return "ok"
    if rc == 124:
        return "timeout-running"
    log = re.sub(r"\x1b\[[0-9;]*m|\[\d+(;\d+)*m", "", log)  # strip ANSI colour codes (nbconvert tracebacks)
    tail = log[-6000:]
    # last traceback's final line decides first, then the whole tail
    last = [l for l in tail.splitlines() if re.match(r"^\s*[\w.]*(Error|Exception|Exit)\b", l)]
    tail25 = "\n".join(tail.splitlines()[-25:])
    for text in ([last[-1]] if last else []) + [tail25]:
        for name, pat in PATTERNS:
            if re.search(pat, text, re.I if name not in ("needs-args",) else 0):
                return name
    if rc == 137:
        return "oom"
    if rc == 2 and "usage:" in tail:
        return "needs-args"
    return "runtime-error"


PLATFORM_MODULES = {"colabfold", "google.colab", "mlx", "mlx_lm", "modal", "win32api", "win32com", "AppKit", "Foundation",
                    "pywintypes", "winreg", "msvcrt"}
PIP_NAME = {"fitz": "pymupdf", "cv2": "opencv-python-headless", "sklearn": "scikit-learn", "PIL": "pillow",
            "yaml": "pyyaml", "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "Bio": "biopython",
            "docx": "python-docx", "autogen": "pyautogen==0.2.35", "openreview": "openreview-py", "skimage": "scikit-image", "attr": "attrs",
            "dateutil": "python-dateutil", "serpapi": "google-search-results", "Crypto": "pycryptodome",
            "pptx": "python-pptx", "jose": "python-jose", "magic": "python-magic", "lifelines": "lifelines",
            "umap": "umap-learn", "faiss": "faiss-cpu", "igraph": "python-igraph", "community": "python-louvain"}


def missing_module(log):
    m = re.findall(r"No module named '([^']+)'", log)
    return m[-1] if m else None


def refine(outcome, log, local_names):
    """Split missing-module into platform / local-package / undeclared-dependency."""
    if outcome != "missing-module":
        return outcome
    mod = missing_module(log)
    if not mod:
        return outcome
    top = mod.split(".")[0]
    if mod in PLATFORM_MODULES or top in PLATFORM_MODULES:
        return "needs-platform"
    if top in local_names:
        return "missing-local-module"
    return "undeclared-dependency"


def _does_something(body):
    import ast
    for n in body:
        if isinstance(n, ast.Expr) and isinstance(n.value, (ast.Call, ast.Await)):
            return True
        if isinstance(n, (ast.For, ast.While, ast.With, ast.AsyncFor, ast.AsyncWith)):
            return True
        if isinstance(n, ast.Try) and _does_something(n.body):
            return True
        if isinstance(n, ast.If) and (_does_something(n.body) or _does_something(n.orelse)):
            return True
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(getattr(n, "value", None), (ast.Call, ast.Await)):
            f = n.value.func if isinstance(n.value, ast.Call) else None
            name = getattr(f, "id", getattr(f, "attr", "")) if f is not None else ""
            if re.search(r"main|run|experiment|train|simulat|evaluat|pipeline|execute|analy[sz]", name, re.I):
                return True
    return False


def script_effect(path):
    """'no-op' if running the file executes nothing beyond definitions; 'stub' if it is a tiny print-only file."""
    import ast
    try:
        src = Path(path).read_text(errors="ignore")
        tree = ast.parse(src)
    except Exception:
        return None
    code_lines = [l for l in src.splitlines() if l.strip() and not l.strip().startswith("#")]
    calls = [n.value.func for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    if len(code_lines) <= 10 and calls and all(getattr(f, "id", "") == "print" for f in calls):
        return "stub"
    return None if _does_something(tree.body) else "no-op"


def effective(fid, run):
    """Downgrade an 'ok' python run to no-op/stub when the script does nothing."""
    if run["outcome"] != "ok":
        return run["outcome"]
    m = re.match(r"python3?\s+(?:-u\s+)?(['\"]?)([^\s'\"]+\.py)\1", run["cmd"])
    mm = re.match(r"python3?\s+-m\s+([\w.]+)", run["cmd"])
    if m:
        path = DATA / "papers" / fid / "code" / run["cwd"] / m.group(2)
    elif mm:
        path = DATA / "papers" / fid / "code" / run["cwd"] / (mm.group(1).replace(".", "/") + ".py")
    else:
        return run["outcome"]
    return script_effect(path) or run["outcome"]


EXTERNAL = {"api-auth", "api-quota", "network-blocked", "needs-gpu", "missing-file", "oom", "needs-display",
            "needs-platform", "needs-account", "missing-system-tool"}


def grade(res):
    outs = [r["outcome"] for r in res["runs"]]
    if not res["commands_planned"]:
        return "no-entry-point"
    fixed = any(r.get("deps_added") for r in res["runs"] if r["outcome"] in ("ok", "timeout-running"))
    if "ok" in outs or "timeout-running" in outs:
        clean_ok = [r for r in res["runs"] if r["outcome"] in ("ok", "timeout-running") and not r.get("deps_added")]
        if clean_ok:
            return "runs" if any(r["outcome"] == "ok" for r in clean_ok) else "runs-long"
        return "runs-after-adding-deps" if fixed else "runs"
    if "needs-args" in outs and all(o in {"needs-args"} | EXTERNAL for o in outs):
        return "needs-args"
    if outs and all(o in ("no-op", "stub") for o in outs):
        return "stub-or-no-op"
    if outs and all(o in EXTERNAL | {"no-op", "stub"} for o in outs):
        return "blocked-external"
    if res["install_failed"] and any(o == "missing-module" for o in outs):
        return "install-failed"
    if any(o in EXTERNAL for o in outs) and not any(o == "syntax-error" for o in outs):
        return "partly-blocked"
    if "version-drift" in outs and all(o in {"version-drift"} | EXTERNAL | {"needs-args", "no-op", "stub"} for o in outs):
        return "broken-version-drift"
    return "broken"


# ---------------------------------------------------------------- sandbox run
def sh(sb, cmd, timeout):
    p = sb.exec("bash", "-lc", cmd, timeout=timeout + 60)
    p.wait()
    return p.returncode


def test_paper(fid, app, secret, run_timeout, force, use_gpu=False):
    out = OUT / fid
    if (out / "result.json").exists() and not force:
        return json.loads((out / "result.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    pl = plan(fid)
    (out / "plan.json").write_text(json.dumps(pl, indent=2))
    if pl["n_source"] == 0:
        res = {"forum_id": fid, "commands_planned": 0, "gpu": False, "runs": [], "install_failed": False,
               "syntax_errors": None, "error": "", "grade": "no-source-code", "seconds": 0}
        (out / "result.json").write_text(json.dumps(res, indent=2))
        return res
    res = {"forum_id": fid, "commands_planned": len(pl["commands"]), "gpu": pl["gpu_hint"], "runs": [],
           "install_failed": False, "syntax_errors": None, "error": ""}
    t0 = time.time()
    sb = None
    try:
        tarpath = bundle(fid)
        sb = modal.Sandbox.create(
            app=app, image=IMAGES[pl.get("python", "3.11")], secrets=[secret] if pl.get("llm_hint") else [], timeout=60 * 75, cpu=2.0, memory=8192,
            gpu="T4" if (pl["gpu_hint"] and use_gpu) else None, outbound_domain_allowlist=ALLOWED_DOMAINS, workdir="/work")
        sb.filesystem.copy_from_local(tarpath, "/tmp/code.tar.gz")
        os.unlink(tarpath)
        setup = ["set -x", "mkdir -p /work && tar -xzf /tmp/code.tar.gz -C /work",
                 "python -m compileall -q /work > /tmp/compile.log 2>&1; "
                 "grep -c -E 'SyntaxError|IndentationError|TabError' /tmp/compile.log > /tmp/syntax_count || true"]
        inst = []
        for r in pl["requirements"]:
            inst.append(f"pip install -q -r {shlex.quote('/work/' + r)}")
        for d in pl["projects"]:
            inst.append(f"pip install -q -e {shlex.quote('/work/' + d)}")
        if pl["conda_pip"]:
            inst.append("pip install -q " + " ".join(shlex.quote(x) for x in pl["conda_pip"]))
        script = "\n".join(setup) + "\nfail=0\n" + "\n".join(f"timeout 900 {c} || fail=1" for c in inst) + \
                 "\necho INSTALL_FAIL=$fail\n"
        sb.filesystem.write_text(script, "/tmp/setup.sh")
        sh(sb, "bash /tmp/setup.sh > /tmp/setup.log 2>&1", 2400)
        setup_log = sb.filesystem.read_text("/tmp/setup.log")
        (out / "setup.log").write_text(setup_log)
        res["install_failed"] = "INSTALL_FAIL=1" in setup_log
        try:
            res["syntax_errors"] = int(sb.filesystem.read_text("/tmp/syntax_count").strip() or 0)
        except Exception:
            res["syntax_errors"] = None
        pypath = ":".join("/work/" + r if r != "." else "/work" for r in pl["pythonpath"])
        local = set(pl["local_names"])
        for i, c in enumerate(pl["commands"], 1):
            cmd = (f"export MPLBACKEND=Agg PYTHONUNBUFFERED=1 HF_HUB_DISABLE_XET=1 PYTHONPATH=.:{pypath}; "
                   f"cd {shlex.quote('/work/' + c['cwd'])} && timeout {run_timeout} {c['cmd']} "
                   f"> /tmp/run_{i}.log 2>&1 < /dev/null; echo $? > /tmp/rc_{i}")
            ts, added, notes = time.time(), [], ""
            for attempt in range(4):  # first run + up to 3 retries after installing an undeclared dependency
                sh(sb, cmd, run_timeout + 30)
                log = sb.filesystem.read_text(f"/tmp/run_{i}.log")
                rc = int(sb.filesystem.read_text(f"/tmp/rc_{i}").strip() or 1)
                outcome = refine(classify(rc, log), log, local)
                if outcome != "undeclared-dependency" or attempt == 3:
                    break
                mod = missing_module(log).split(".")[0]
                pkg = PIP_NAME.get(mod, mod.replace("_", "-"))
                if pkg in added:
                    break
                prc = sh(sb, f"pip install -q {shlex.quote(pkg)} > /tmp/pip_{i}.log 2>&1", 600)
                added.append(pkg)
                notes += f"# retry {attempt + 1}: pip install {pkg} -> rc {prc}\n"
                if prc != 0:
                    break
            (out / f"run_{i}.log").write_text(
                f"# cwd: {c['cwd']}\n# cmd: {c['cmd']}\n# source: {c['source']}\n# rc: {rc}\n"
                f"# undeclared deps added: {added}\n{notes}\n" + log)
            res["runs"].append({"i": i, "cmd": c["cmd"], "cwd": c["cwd"], "source": c["source"], "rc": rc,
                                "outcome": outcome, "deps_added": added, "seconds": round(time.time() - ts)})
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:500]
        (out / "harness_error.log").write_text(traceback.format_exc())
    finally:
        if sb is not None:
            try:
                sb.terminate()
            except Exception:
                pass
    res["grade"] = grade(res) if not res["error"] else "harness-error"
    res["seconds"] = round(time.time() - t0)
    (out / "result.json").write_text(json.dumps(res, indent=2))
    return res


def write_summary():
    rows = []
    for f in sorted(OUT.glob("*/result.json")):
        r = json.loads(f.read_text())
        pl = json.loads((f.parent / "plan.json").read_text())
        for x in r["runs"]:  # re-derive outcomes from saved logs so classifier changes apply retroactively
            lf = f.parent / f"run_{x['i']}.log"
            if lf.exists():
                body = lf.read_text(errors="ignore").split("\n\n", 1)[-1]
                x["outcome"] = refine(classify(x["rc"], body), body, set(pl.get("local_names", [])))
            x["outcome"] = effective(r["forum_id"], x)
        if r["grade"] not in ("no-source-code", "harness-error"):
            r["grade"] = grade(r)
        f.write_text(json.dumps(r, indent=2))
        rows.append({"forum_id": r["forum_id"], "grade": r["grade"],
                     "outcomes": ";".join(x["outcome"] for x in r["runs"]),
                     "deps_added": ";".join(sorted({d for x in r["runs"] for d in x.get("deps_added", [])})),
                     "install_failed": r["install_failed"], "syntax_errors": r["syntax_errors"],
                     "gpu": r["gpu"], "llm_hint": pl["llm_hint"], "n_py": pl["n_py"], "n_ipynb": pl["n_ipynb"],
                     "n_candidates": pl["n_candidates"], "commands": " | ".join(x["cmd"] for x in r["runs"]),
                     "seconds": r["seconds"], "error": r["error"]})
    if rows:
        with open(OUT / "code_test_results.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
    return rows


def main():
    global DATA, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", type=Path, default=DATA, help="split root holding papers/<forum_id>/code")
    ap.add_argument("--out", type=Path, default=OUT, help="results folder (outside the data root)")
    ap.add_argument("--no-keys", action="store_true", help="pass no provider keys into the sandboxes")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--run-timeout", type=int, default=300)
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--gpu", action="store_true", help="use T4 for GPU-hinted papers (needs a Modal payment method)")
    ap.add_argument("--redo-grades", nargs="*", help="re-run papers whose current grade is in this list")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    DATA, OUT = args.data_root.resolve(), args.out.resolve()
    if OUT == DATA or DATA in OUT.parents:
        sys.exit("--out must not be inside --data-root")
    ids = args.ids or sorted(p.parent.name for p in (DATA / "papers").glob("*/code") if p.is_dir())
    if args.redo_grades is not None:
        redo = []
        for fid in ids:
            rf = OUT / fid / "result.json"
            if not rf.exists() or json.loads(rf.read_text()).get("grade") in args.redo_grades:
                redo.append(fid)
        ids, args.force = redo, True
    if args.summary_only:
        from collections import Counter
        print(Counter(r["grade"] for r in write_summary()))
        return
    if args.plan_only:
        for fid in ids:
            print(json.dumps(plan(fid)))
        return
    keys = {} if args.no_keys else {k: os.environ[s] for k, s in SANDBOX_KEYS.items() if os.environ.get(s)}
    secret = modal.Secret.from_dict(keys)  # values never printed or logged
    app = modal.App.lookup("a4s-code-tests", create_if_missing=True)
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"testing {len(ids)} papers with {args.workers} workers; keys passed: {sorted(keys)}", flush=True)
    with modal.enable_output() if os.environ.get("MODAL_VERBOSE") else open(os.devnull) as _:
        with ThreadPoolExecutor(args.workers) as ex:
            futs = {ex.submit(test_paper, fid, app, secret, args.run_timeout, args.force, args.gpu): fid for fid in ids}
            for n, f in enumerate(as_completed(futs), 1):
                fid = futs[f]
                try:
                    r = f.result()
                    print(f"[{n}/{len(ids)}] {fid} {r['grade']} {[x['outcome'] for x in r['runs']]} {r['seconds']}s {r['error'][:120]}", flush=True)
                except Exception as e:
                    print(f"[{n}/{len(ids)}] {fid} HARNESS FAIL {e}", flush=True)
    rows = write_summary()
    from collections import Counter
    print(Counter(r["grade"] for r in rows))


if __name__ == "__main__":
    main()
