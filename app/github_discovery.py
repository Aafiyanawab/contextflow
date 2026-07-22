import os
from github import Github
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# ── What we look for in a repo ───────────────────────────
DISCOVERY_RULES = {
    "terraform": {
        "files": ["main.tf", "variables.tf", "outputs.tf", "providers.tf"],
        "extensions": [".tf"],
        "folders": ["terraform/", "infra/"]
    },
    "docker": {
        "files": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
        "extensions": [],
        "folders": []
    },
    "kubernetes": {
        "files": ["deployment.yaml", "service.yaml", "ingress.yaml"],
        "extensions": [],
        "folders": ["k8s/", "helm/", "manifests/"]
    },
    "github_actions": {
        "files": [],
        "extensions": [".yml", ".yaml"],
        "folders": [".github/workflows/"]
    },
    "python": {
        "files": ["requirements.txt", "setup.py", "pyproject.toml"],
        "extensions": [".py"],
        "folders": []
    },
    "javascript": {
        "files": ["package.json", "package-lock.json"],
        "extensions": [".js", ".ts"],
        "folders": []
    },
    "java": {
        "files": ["pom.xml", "build.gradle"],
        "extensions": [".java"],
        "folders": []
    }
}

CLOUD_PROVIDER_HINTS = {
    "aws": ["aws_instance", "aws_s3", "aws_vpc", "aws_lambda",
            "amazon", "boto3", "awslocal"],
    "gcp": ["google_compute", "google_storage", "gcloud"],
    "azure": ["azurerm", "azure_", "az login"]
}

FRAMEWORK_HINTS = {
    "flask": ["from flask", "import flask"],
    "fastapi": ["from fastapi", "import fastapi"],
    "django": ["from django", "import django"],
    "express": ["require('express')", "from 'express'"],
    "react": ["from 'react'", "require('react')"],
    "spring": ["@SpringBootApplication", "springframework"]
}


# Package manager, inferred from lockfiles/manifests (first match wins).
PACKAGE_MANAGERS = [
    ("pnpm", ["pnpm-lock.yaml"]),
    ("yarn", ["yarn.lock"]),
    ("npm", ["package-lock.json", "package.json"]),
    ("poetry", ["poetry.lock"]),
    ("pip", ["requirements.txt", "Pipfile", "setup.py", "pyproject.toml"]),
    ("maven", ["pom.xml"]),
    ("gradle", ["build.gradle", "build.gradle.kts"]),
    ("go modules", ["go.mod"]),
    ("cargo", ["Cargo.toml"]),
]


def detect_package_manager(file_paths):
    """Infer the dependency manager from manifest/lockfile basenames."""
    names = {p.rsplit("/", 1)[-1] for p in file_paths}
    for manager, markers in PACKAGE_MANAGERS:
        if any(marker in names for marker in markers):
            return manager
    return None


def get_all_file_paths(repo):
    """Get all file paths in a repository."""
    try:
        contents = repo.get_git_tree("HEAD", recursive=True)
        return [item.path for item in contents.tree
                if item.type == "blob"]
    except Exception as e:
        print(f"Error fetching file tree: {e}")
        return []


def detect_cloud_provider(repo, file_paths):
    """Scan Terraform files to detect cloud provider."""
    tf_files = [f for f in file_paths if f.endswith(".tf")][:5]

    for tf_file in tf_files:
        try:
            content = repo.get_contents(tf_file).decoded_content.decode("utf-8").lower()
            for provider, hints in CLOUD_PROVIDER_HINTS.items():
                if any(hint in content for hint in hints):
                    return provider
        except Exception:
            continue
    return None


def detect_framework(repo, file_paths):
    """Scan Python/JS files to detect framework."""
    code_files = [f for f in file_paths
                  if f.endswith((".py", ".js", ".ts"))][:10]

    for code_file in code_files:
        try:
            content = repo.get_contents(code_file).decoded_content.decode("utf-8")
            for framework, hints in FRAMEWORK_HINTS.items():
                if any(hint in content for hint in hints):
                    return framework
        except Exception:
            continue
    return None


def discover_repo_context(repo_url: str, progress=None) -> dict:
    """
    Main function — scans a GitHub repo and returns discovered context.
    Optional progress(step, detail) callback reports scan stages for live UIs.
    """
    def report(step, detail=""):
        if progress:
            progress(step, detail)

    print(f"\nScanning repository: {repo_url}")

    # Parse repo name from URL
    # e.g. https://github.com/owner/repo → owner/repo
    repo_name = "/".join(repo_url.rstrip("/").split("/")[-2:])

    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(repo_name)

    # Get all file paths
    file_paths = get_all_file_paths(repo)
    print(f"Found {len(file_paths)} files")
    report("tree", f"{len(file_paths)} files")

    context = {
        "repo": repo_url,
        "cloud": None,
        "iac": None,
        "containerization": None,
        "orchestration": None,
        "cicd": None,
        "language": None,
        "framework": None
    }

    # Check each discovery rule
    for tool, rules in DISCOVERY_RULES.items():
        detected = False

        # Check for specific files
        for f in rules["files"]:
            if any(path.endswith(f) or path == f for path in file_paths):
                detected = True
                break

        # Check for folders
        if not detected:
            for folder in rules["folders"]:
                if any(path.startswith(folder) for path in file_paths):
                    detected = True
                    break

        # Check for extensions (only if many files match)
        if not detected and rules["extensions"]:
            matches = sum(1 for path in file_paths
                          if any(path.endswith(ext)
                                 for ext in rules["extensions"]))
            if matches >= 3:
                detected = True

        if tool in ("terraform", "docker", "kubernetes", "github_actions"):
            report(tool, "detected" if detected else "not found")

        if detected:
            if tool == "terraform":
                context["iac"] = "terraform"
            elif tool == "docker":
                context["containerization"] = "docker"
            elif tool == "kubernetes":
                context["orchestration"] = "kubernetes"
            elif tool == "github_actions":
                context["cicd"] = "github_actions"
            elif tool in ["python", "javascript", "java"]:
                if not context["language"]:
                    context["language"] = tool

    report("language", context["language"] or "not found")

    # Detect cloud provider from Terraform files
    context["cloud"] = detect_cloud_provider(repo, file_paths)
    report("cloud", context["cloud"] or "not found")

    # Detect framework from code files
    context["framework"] = detect_framework(repo, file_paths)
    report("framework", context["framework"] or "not found")

    # ── Repository Inventory enrichment ──
    # Extra metadata persisted alongside the detected stack so the admin
    # Repository Inventory (and the AI) read stored facts instead of
    # re-scanning per question. owner/repo_name parsed from the URL;
    # package manager inferred from manifests; size from the GitHub API.
    context["owner"] = repo_name.split("/")[0]
    context["repo_name"] = repo_name.split("/")[-1]
    context["package_manager"] = detect_package_manager(file_paths)
    context["file_count"] = len(file_paths)
    try:
        context["repo_size_kb"] = repo.size  # GitHub reports repo size in KB
    except Exception:
        context["repo_size_kb"] = None

    # Remove None values
    context = {k: v for k, v in context.items() if v is not None}

    return context


# ── Test ─────────────────────────────────────────────────
if __name__ == "__main__":
    # Test with your own GitHub repo
    test_repo = "https://github.com/Aafiyanawab/floci"

    context = discover_repo_context(test_repo)

    print("\nDiscovered Context:")
    for key, value in context.items():
        print(f"  {key}: {value}")