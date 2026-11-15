"""Filesystem isolation and secure workspace staging for containerized execution.

Implements an explicit staging allowlist strategy:
- Only declared source files, approved data files, and package manifests are staged.
- Unknown credential files and arbitrary file types are excluded by default.
- Strictly strips .env, .ssh, .aws, .azure, .kube, .git, .venv, .npmrc, .pypirc, etc.
- Detects and rejects symlink escapes targeting external paths.
- Detects and rejects symlinks targeting excluded sensitive files.
"""

import os
import re
import shutil
import tempfile
from pathlib import Path

from recoverybench.errors import ConfigurationError

# Patterns for secret content scanning inside staged files
SECRET_CONTENT_PATTERNS = [
    # Live API keys and tokens
    (re.compile(r"sk_live_[0-9a-zA-Z]{16,}"), "Stripe/Live secret key"),
    (re.compile(r"ak_[0-9a-zA-Z]{16,}"), "Access key"),
    (re.compile(r"ghp_[0-9a-zA-Z]{36}"), "GitHub personal access token"),
    (re.compile(r"AIza[0-9A-Za-z-_]{35}"), "Google API key"),
    (re.compile(r"xox[baprs]-[0-9a-zA-Z]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), "Private key block"),
    # Embedded basic auth in URLs
    (re.compile(r"https?://[a-zA-Z0-9_\-\.]+:[^@\s/]+@[a-zA-Z0-9_\-\.]+"), "Embedded URL credentials"),
    # Key-value secret configurations in JSON, YAML, TOML, INI
    (
        re.compile(
            r"""(?i)["']?(?:api[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token|password|passwd|client[_-]?secret|db[_-]?password)["']?\s*[:=]\s*["']([^"'\r\n\t ]{4,})["']"""
        ),
        "Credential key-value pair",
    ),
    # YAML unquoted keys
    (
        re.compile(
            r"""(?i)^\s*(?:api[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token|password|passwd|client[_-]?secret|db[_-]?password)\s*:\s*["']?([^"'\r\n\t #]{4,})["']?""",
            re.MULTILINE,
        ),
        "Nested YAML credential",
    ),
]

SAFE_PLACEHOLDER_VALUES = {
    "null",
    "none",
    "placeholder",
    "dummy",
    "test",
    "fake",
    "true",
    "false",
    "mock",
    "env",
    "environ",
    "xxxx",
    "changeme",
    "example",
    "your_api_key_here",
    "your_key_here",
    "your_secret_here",
}

# Strictly prohibited directory names anywhere in path
EXCLUDED_DIR_NAMES = {
    ".git",
    ".github",
    ".venv",
    "venv",
    ".env",
    "env",
    "node_modules",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".config",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

# Strictly prohibited file prefixes or exact names
EXCLUDED_FILE_PREFIXES = (
    ".env",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)

# Strictly prohibited file extensions (even if otherwise matching allowlist)
EXCLUDED_FILE_EXTENSIONS = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".pkcs12",
    ".kdbx",
    ".ovpn",
    ".secret",
    ".token",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
)

# Suspicious substrings in directory or filename that indicate credentials/secrets
SUSPICIOUS_SECRET_SUBSTRINGS = (
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "private_key",
    "auth_token",
)

# Explicitly permitted file extensions for Python agent projects (Allowlist)
ALLOWED_FILE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".csv",
    ".tsv",
    ".md",
    ".rst",
    ".sql",
    ".ini",
    ".cfg",
}

# Explicitly permitted exact filenames without extensions or special manifests
ALLOWED_EXACT_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "license",
    "readme",
    "readme.md",
    "readme.rst",
    "readme.txt",
}


class WorkspaceStaging:
    """Manages secure staging of agent source code into an isolated directory."""

    @classmethod
    def is_safe_dir(cls, rel_dir: Path) -> bool:
        """Determine whether a directory is safe to traverse and stage."""
        for part in rel_dir.parts:
            lower = part.lower()
            if lower in EXCLUDED_DIR_NAMES or lower.startswith("."):
                return False
            for sub in SUSPICIOUS_SECRET_SUBSTRINGS:
                if sub in lower:
                    return False
        return True

    @classmethod
    def is_safe_path(cls, rel_path: Path, custom_allowlist: set[str] | None = None) -> bool:
        """Determine whether a relative file is safe to stage.

        Uses an explicit allowlist strategy: only permitted extensions/names are accepted.
        All unknown credential and non-whitelisted files are excluded by default.
        """
        # 1. Directory safety
        if not cls.is_safe_dir(rel_path.parent):
            return False

        filename = rel_path.name.lower()

        # 2. Check prohibited directory as target
        if filename in EXCLUDED_DIR_NAMES:
            return False

        # 3. Reject hidden files (starting with dot) unless explicitly whitelisted manifest
        if filename.startswith(".") and filename not in ALLOWED_EXACT_NAMES:
            return False

        # 4. Check strictly prohibited prefixes (e.g. .env, .npmrc, id_rsa)
        for prefix in EXCLUDED_FILE_PREFIXES:
            if filename.startswith(prefix):
                return False

        # 5. Check strictly prohibited extensions (e.g. .pem, .key, .secret)
        for ext in EXCLUDED_FILE_EXTENSIONS:
            if filename.endswith(ext):
                return False

        # 6. Check suspicious secret substrings (e.g. client_secret.json, db_password.txt)
        for sub in SUSPICIOUS_SECRET_SUBSTRINGS:
            if sub in filename:
                return False

        # 7. Custom allowlist check
        if custom_allowlist and (str(rel_path) in custom_allowlist or rel_path.name in custom_allowlist):
            return True

        # 8. Check explicit allowlist (extensions and exact manifest names)
        ext = rel_path.suffix.lower()
        if ext in ALLOWED_FILE_EXTENSIONS:
            return True

        if filename in ALLOWED_EXACT_NAMES:
            return True

        # Default Deny: Unknown files and extensions are excluded
        return False

    @classmethod
    def scan_file_contents_for_secrets(cls, file_path: Path, rel_path: Path) -> None:
        """Scan file contents for embedded API keys, tokens, and credentials."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return  # Non-text or unreadable

        # 1. Check explicit token / private key / URL auth patterns
        for pattern, desc in SECRET_CONTENT_PATTERNS[:7]:
            match = pattern.search(content)
            if match:
                raise ConfigurationError(
                    f"Security violation: {desc} detected in workspace file '{rel_path}'! "
                    "Secrets and private keys must never be staged into execution containers."
                )

        # 2. Check configuration and data files for credential key-value definitions
        ext = rel_path.suffix.lower()
        if ext in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt"):
            for pattern, desc in SECRET_CONTENT_PATTERNS[7:]:
                for match in pattern.finditer(content):
                    val = match.group(1).strip().lower()
                    if val not in SAFE_PLACEHOLDER_VALUES and not val.startswith(("${", "$(", "%")):
                        raise ConfigurationError(
                            f"Security violation: {desc} detected in configuration file '{rel_path}'! "
                            "Configuration files staged into the sandbox must not contain plaintext credentials."
                        )

    @classmethod
    def _copy_safe_file(
        cls,
        source_file: Path,
        dest_file: Path,
        rel_file: Path,
        source_dir: Path,
        custom_allowlist: set[str] | None = None,
    ) -> None:
        """Copy a single file after performing symlink and content scanning checks."""
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        if source_file.is_symlink():
            target_resolved = source_file.resolve()

            # Check 1: Escape detection (points outside source_dir)
            try:
                target_rel = target_resolved.relative_to(source_dir)
            except ValueError:
                raise ConfigurationError(
                    f"Security violation: Symlink escape detected in workspace! "
                    f"Symlink '{rel_file}' points to external location '{target_resolved}'"
                ) from None

            # Check 2: Excluded target detection (points to an excluded sensitive file)
            if not cls.is_safe_path(target_rel, custom_allowlist=custom_allowlist):
                raise ConfigurationError(
                    f"Security violation: Symlink '{rel_file}' targets an excluded sensitive file '{target_rel}'"
                ) from None

            # Check 3: Content scanning on resolved target
            cls.scan_file_contents_for_secrets(target_resolved, rel_file)
            shutil.copy2(source_file, dest_file)
        else:
            cls.scan_file_contents_for_secrets(source_file, rel_file)
            shutil.copy2(source_file, dest_file)

    @classmethod
    def stage_workspace(
        cls,
        source_dir: str | Path,
        worker_script_content: str | None = None,
        custom_allowlist: set[str] | None = None,
        manifest_files: list[str] | None = None,
    ) -> Path:
        """Copy allowed files from source_dir into a secure temporary directory.

        Rejects directory escapes, symlinks targeting files outside source_dir
        or targeting excluded sensitive files, and files containing credentials.
        """
        source = Path(source_dir).resolve()
        if not source.exists() or not source.is_dir():
            raise ConfigurationError(f"Cannot stage invalid workspace directory: {source_dir}")

        target_temp = Path(tempfile.mkdtemp(prefix="rb_sandbox_"))

        try:
            if manifest_files is not None:
                # Explicit manifest mode: only stage declared files/directories
                for item in manifest_files:
                    clean_item = item.strip().replace("\\", "/")
                    if not clean_item:
                        continue
                    item_path = (source / clean_item).resolve()
                    try:
                        rel_item = item_path.relative_to(source)
                    except ValueError:
                        raise ConfigurationError(
                            f"Security violation: Declared manifest path '{item}' escapes workspace root!"
                        ) from None

                    if not item_path.exists():
                        raise ConfigurationError(
                            f"Declared manifest path does not exist in workspace: '{item}'"
                        )

                    if item_path.is_file():
                        if not cls.is_safe_path(rel_item, custom_allowlist=custom_allowlist):
                            raise ConfigurationError(
                                f"Security violation: Declared manifest file '{item}' is in an excluded path or has prohibited extension!"
                            )
                        cls._copy_safe_file(
                            item_path,
                            target_temp / rel_item,
                            rel_item,
                            source,
                            custom_allowlist=custom_allowlist,
                        )
                    elif item_path.is_dir():
                        if not cls.is_safe_dir(rel_item):
                            raise ConfigurationError(
                                f"Security violation: Declared manifest directory '{item}' is excluded or sensitive!"
                            )
                        for root, dirs, files in os.walk(item_path, followlinks=False):
                            current_root = Path(root)
                            rel_root = current_root.relative_to(source)
                            dirs[:] = [d for d in dirs if cls.is_safe_dir(rel_root / d)]
                            for f in files:
                                source_file = current_root / f
                                rel_file = rel_root / f
                                if not cls.is_safe_path(rel_file, custom_allowlist=custom_allowlist):
                                    continue
                                cls._copy_safe_file(
                                    source_file,
                                    target_temp / rel_file,
                                    rel_file,
                                    source,
                                    custom_allowlist=custom_allowlist,
                                )
            else:
                # Automatic allowlist mode: traverse approved files across source
                for root, dirs, files in os.walk(source, followlinks=False):
                    current_root = Path(root)
                    rel_root = current_root.relative_to(source)

                    # Prune excluded directories in-place so os.walk does not descend into them
                    dirs[:] = [d for d in dirs if cls.is_safe_dir(rel_root / d)]

                    for f in files:
                        source_file = current_root / f
                        rel_file = rel_root / f

                        if not cls.is_safe_path(rel_file, custom_allowlist=custom_allowlist):
                            continue

                        cls._copy_safe_file(
                            source_file,
                            target_temp / rel_file,
                            rel_file,
                            source,
                            custom_allowlist=custom_allowlist,
                        )

            # Inject worker script if provided
            if worker_script_content:
                worker_dest = target_temp / "_recoverybench_worker.py"
                worker_dest.write_text(worker_script_content, encoding="utf-8")

            return target_temp

        except Exception:
            shutil.rmtree(target_temp, ignore_errors=True)
            raise

    @classmethod
    def cleanup_workspace(cls, staged_dir: str | Path) -> None:
        """Safely delete the staged temporary workspace."""
        path = Path(staged_dir)
        if path.exists() and "rb_sandbox_" in path.name:
            shutil.rmtree(path, ignore_errors=True)
