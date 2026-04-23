"""
GitHub → GitLab Project Importer
Reads ALL config from a .env file in the same directory.
Copy .env.example → .env, fill in your values, then run:

    python github_to_gitlab.py

Optional CLI overrides:
    python github_to_gitlab.py --github-repo-url <url> --github-token <token> ...
"""

import argparse
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv

# ─── Load .env (mandatory) ────────────────────────────────────────────────────
ENV_PATH = Path(__file__).parent / ".env"

if not ENV_PATH.exists():
    print("❌ ERROR: .env file not found!")
    print(f"   Expected at: {ENV_PATH}")
    print("   Copy .env.example → .env and fill in your values.\n")
    sys.exit(1)

load_dotenv(ENV_PATH)


# ─── Config ───────────────────────────────────────────────────────────────────

def get_config(args: argparse.Namespace) -> dict:
    def val(cli_val, env_key, required=True):
        v = cli_val or os.getenv(env_key, "")
        if required and not v:
            print(f"❌ Missing required config: {env_key}  (set in .env or pass as CLI flag)")
            sys.exit(1)
        return v

    return {
        "github_url":   val(args.github_repo_url,    "GITHUB_REPO_URL"),
        "github_token": val(args.github_token,        "GITHUB_TOKEN"),
        "gitlab_url":   val(args.gitlab_url,          "GITLAB_URL"),
        "gitlab_token": val(args.gitlab_token,        "GITLAB_TOKEN"),
        "subfolder":    val(None, "GITHUB_SUBFOLDER", required=False),
        "namespace_id": val(args.gitlab_namespace_id, "GITLAB_NAMESPACE_ID", required=False) or None,
    }


# ─── GitHub helpers ───────────────────────────────────────────────────────────

def parse_github_repo(url: str) -> tuple:
    url = url.rstrip("/").removesuffix(".git")
    parts = urllib.parse.urlparse(url).path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse GitHub URL: {url}")
    return parts[0], parts[1]


def inject_token(url: str, token: str, prefix: str = "") -> str:
    user = f"{prefix}:{token}@" if prefix else f"{token}@"
    if url.startswith("https://"):
        return url.replace("https://", f"https://{user}")
    if url.startswith("http://"):
        return url.replace("http://", f"http://{user}")
    return url


# ─── GitLab helpers ───────────────────────────────────────────────────────────

def gitlab_api(gitlab_url: str, token: str, method: str, path: str, **kwargs):
    url = f"{gitlab_url.rstrip('/')}/api/v4{path}"
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    resp = requests.request(method, url, headers=headers, **kwargs)
    if not resp.ok:
        # Print full details to help diagnose
        print(f"  ⚠ Request URL  : {url}")
        print(f"  ⚠ Payload      : {kwargs.get('json', {})}")
        raise RuntimeError(f"GitLab API {resp.status_code}: {resp.text}")
    return resp.json() if resp.text else {}


def sanitize_path(name: str) -> str:
    """GitLab path: lowercase, only letters/digits/hyphens, no leading/trailing hyphens."""
    import re
    path = name.lower().replace("_", "-").replace(" ", "-")
    path = re.sub(r"[^a-z0-9-]", "-", path)   # replace any other chars
    path = re.sub(r"-{2,}", "-", path)          # collapse multiple hyphens
    path = path.strip("-")                      # no leading/trailing hyphens
    return path


def get_or_create_gitlab_project(gitlab_url: str, token: str,
                                  name: str, namespace_id) -> dict:
    payload = {
        "name": name,
        "path": sanitize_path(name),
        "visibility": "private",
    }
    if namespace_id:
        payload["namespace_id"] = int(namespace_id)

    # Check if project already exists via direct path lookup (avoids broken search API)
    try:
        who = gitlab_api(gitlab_url, token, "GET", "/user")
        username = who["username"]
        slug = payload["path"]
        import urllib.parse as _up
        encoded = _up.quote(f"{username}/{slug}", safe="")
        project = gitlab_api(gitlab_url, token, "GET", f"/projects/{encoded}")
        print(f"  ℹ Project already exists : {project['path_with_namespace']}")
        return project
    except RuntimeError:
        pass  # does not exist yet, proceed to create

    try:
        project = gitlab_api(gitlab_url, token, "POST", "/projects", json=payload)
        print(f"  ✔ Created GitLab project : {project['path_with_namespace']}")
        return project
    except RuntimeError as exc:
        raise RuntimeError(f"Failed to create project: {exc}") from exc


# ─── Git operations ───────────────────────────────────────────────────────────

def run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git error:\n{result.stderr.strip()}")
    return result.stdout.strip()


def mirror_to_gitlab(cfg: dict, gitlab_project: dict):
    github_auth   = inject_token(cfg["github_url"], cfg["github_token"])
    gitlab_remote = (
        f"{cfg['gitlab_url'].rstrip('/')}/"
        f"{gitlab_project['path_with_namespace']}.git"
    )
    gitlab_auth   = inject_token(gitlab_remote, cfg["gitlab_token"], prefix="oauth2")
    subfolder     = cfg.get("subfolder", "").strip("/")

    with tempfile.TemporaryDirectory() as tmpdir:
        clone_dir = os.path.join(tmpdir, "repo")

        if subfolder:
            print(f"  ⬇  Sparse-cloning subfolder: '{subfolder}' …")
            run(["git", "clone", "--no-checkout", "--filter=blob:none", github_auth, clone_dir])
            run(["git", "sparse-checkout", "init", "--cone"], cwd=clone_dir)
            run(["git", "sparse-checkout", "set", subfolder], cwd=clone_dir)
            run(["git", "checkout"], cwd=clone_dir)
            run(["git", "remote", "set-url", "origin", gitlab_auth], cwd=clone_dir)
            branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone_dir)
            print(f"  ⬆  Pushing branch '{branch}' to GitLab …")
            run(["git", "push", "-u", "origin", branch, "--force"], cwd=clone_dir)
        else:
            print("  ⬇  Mirror-cloning full repository …")
            run(["git", "clone", "--mirror", github_auth, clone_dir])
            print("  ⬆  Pushing all refs to GitLab …")
            run(["git", "push", "--mirror", gitlab_auth], cwd=clone_dir)

    gl_web = f"{cfg['gitlab_url'].rstrip('/')}/{gitlab_project['path_with_namespace']}"
    print(f"  ✔ Available at : {gl_web}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Import a GitHub repo into GitLab — config via .env"
    )
    parser.add_argument("--github-repo-url",    default=None)
    parser.add_argument("--github-token",        default=None)
    parser.add_argument("--gitlab-url",          default=None)
    parser.add_argument("--gitlab-token",        default=None)
    parser.add_argument("--gitlab-namespace-id", default=None)
    args = parser.parse_args()

    print("\n══════════════════════════════════════════")
    print("   GitHub  →  GitLab  Importer")
    print("══════════════════════════════════════════\n")

    cfg = get_config(args)

    _, repo_name = parse_github_repo(cfg["github_url"])
    subfolder    = cfg.get("subfolder", "").strip("/")
    project_name = subfolder.split("/")[-1] if subfolder else repo_name

    print(f"  Source  : {cfg['github_url']}" +
          (f"  →  subfolder: '{subfolder}'" if subfolder else "  (full repo)"))
    print(f"  Target  : {cfg['gitlab_url']}  (project: {project_name})\n")

    print("[1/2] Setting up GitLab project …")
    gitlab_project = get_or_create_gitlab_project(
        cfg["gitlab_url"], cfg["gitlab_token"], project_name, cfg["namespace_id"]
    )

    print("\n[2/2] Cloning from GitHub & pushing to GitLab …")
    mirror_to_gitlab(cfg, gitlab_project)

    print("\n✅ Import complete!\n")


if __name__ == "__main__":
    main()