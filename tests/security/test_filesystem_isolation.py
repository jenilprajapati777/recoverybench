"""Tests for filesystem staging security, sensitive file exclusion, allowlist enforcement, and symlink security."""

import os
from pathlib import Path

import pytest

from recoverybench.errors import ConfigurationError
from recoverybench.sandbox.staging import (
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILE_EXTENSIONS,
    EXCLUDED_FILE_PREFIXES,
    WorkspaceStaging,
)


def test_is_safe_path_filters_sensitive_directories() -> None:
    """Verify that dangerous directory paths are rejected."""
    for dirname in EXCLUDED_DIR_NAMES:
        assert not WorkspaceStaging.is_safe_path(Path(dirname) / "file.py")
        assert not WorkspaceStaging.is_safe_path(Path("subdir") / dirname / "file.py")

    assert WorkspaceStaging.is_safe_path(Path("src") / "agent.py")
    assert WorkspaceStaging.is_safe_path(Path("utils") / "helper.py")


def test_is_safe_path_filters_sensitive_file_prefixes_and_names() -> None:
    """Verify that credentials and env files are rejected."""
    for prefix in EXCLUDED_FILE_PREFIXES:
        assert not WorkspaceStaging.is_safe_path(Path(f"{prefix}.local"))
        assert not WorkspaceStaging.is_safe_path(Path("subdir") / f"{prefix}.prod")

    for ext in EXCLUDED_FILE_EXTENSIONS:
        assert not WorkspaceStaging.is_safe_path(Path(f"my_cert{ext}"))
        assert not WorkspaceStaging.is_safe_path(Path("keys") / f"server{ext}")


def test_is_safe_path_allowlist_excludes_unknown_extensions_by_default() -> None:
    """Verify default-deny allowlist excludes unknown, suspicious, and arbitrary extensions."""
    # Unknown/dangerous extensions rejected by default
    assert not WorkspaceStaging.is_safe_path(Path("database.bak"))
    assert not WorkspaceStaging.is_safe_path(Path("archive.tar"))
    assert not WorkspaceStaging.is_safe_path(Path("program.exe"))
    assert not WorkspaceStaging.is_safe_path(Path("deploy.sh"))
    assert not WorkspaceStaging.is_safe_path(Path("data.dat"))
    assert not WorkspaceStaging.is_safe_path(Path("cache.bin"))

    # Approved extensions accepted
    assert WorkspaceStaging.is_safe_path(Path("main.py"))
    assert WorkspaceStaging.is_safe_path(Path("config.json"))
    assert WorkspaceStaging.is_safe_path(Path("schema.yaml"))
    assert WorkspaceStaging.is_safe_path(Path("pyproject.toml"))
    assert WorkspaceStaging.is_safe_path(Path("requirements.txt"))
    assert WorkspaceStaging.is_safe_path(Path("data.csv"))


def test_is_safe_path_nested_secrets_and_custom_credential_names() -> None:
    """Verify suspicious credential filenames and hidden files are rejected even with valid extensions."""
    # Files containing secret keywords rejected even if extension is .txt/.json/.yaml
    assert not WorkspaceStaging.is_safe_path(Path("nested") / "secrets" / "custom_cred.txt")
    assert not WorkspaceStaging.is_safe_path(Path("config") / "db_password.txt")
    assert not WorkspaceStaging.is_safe_path(Path("auth") / "client_secret.json")
    assert not WorkspaceStaging.is_safe_path(Path("keys") / "api_key.yaml")
    assert not WorkspaceStaging.is_safe_path(Path("auth_token.json"))

    # Special tool credential files rejected
    assert not WorkspaceStaging.is_safe_path(Path(".pypirc"))
    assert not WorkspaceStaging.is_safe_path(Path(".npmrc"))
    assert not WorkspaceStaging.is_safe_path(Path(".kube") / "config")
    assert not WorkspaceStaging.is_safe_path(Path(".docker") / "config.json")


def test_stage_workspace_excludes_sensitive_files(tmp_path: Path) -> None:
    """Verify staging copy omits all sensitive files and copies permitted files."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    # Create safe files
    (source_dir / "agent.py").write_text("print('hello')", encoding="utf-8")
    (source_dir / "utils").mkdir()
    (source_dir / "utils" / "helper.py").write_text("# helper", encoding="utf-8")
    (source_dir / "README.md").write_text("# Docs", encoding="utf-8")

    # Create sensitive files
    (source_dir / ".env").write_text("SECRET=123", encoding="utf-8")
    (source_dir / ".env.production").write_text("API_KEY=live", encoding="utf-8")
    (source_dir / "server.key").write_text("PRIVATE KEY", encoding="utf-8")
    (source_dir / "cert.pem").write_text("CERTIFICATE", encoding="utf-8")

    # Create sensitive directories
    git_dir = source_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("git config", encoding="utf-8")

    ssh_dir = source_dir / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("ssh secret", encoding="utf-8")

    aws_dir = source_dir / ".aws"
    aws_dir.mkdir()
    (aws_dir / "credentials").write_text("aws secret", encoding="utf-8")

    # Stage workspace
    staged = WorkspaceStaging.stage_workspace(source_dir)
    try:
        assert staged.exists()

        # Check safe files exist in staged directory
        assert (staged / "agent.py").exists()
        assert (staged / "agent.py").read_text(encoding="utf-8") == "print('hello')"
        assert (staged / "utils" / "helper.py").exists()
        assert (staged / "README.md").exists()

        # Check sensitive files are completely absent
        assert not (staged / ".env").exists()
        assert not (staged / ".env.production").exists()
        assert not (staged / "server.key").exists()
        assert not (staged / "cert.pem").exists()
        assert not (staged / ".git").exists()
        assert not (staged / ".ssh").exists()
        assert not (staged / ".aws").exists()

    finally:
        WorkspaceStaging.cleanup_workspace(staged)
        assert not staged.exists()


def test_stage_workspace_symlink_escape_rejection(tmp_path: Path) -> None:
    """Verify symlink pointing outside the workspace is detected and raises ConfigurationError."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    outside_file = tmp_path / "secret_outside.txt"
    outside_file.write_text("SUPER_SECRET_HOST_DATA", encoding="utf-8")

    symlink_file = source_dir / "escape_link.txt"
    try:
        os.symlink(outside_file, symlink_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted on this host environment")

    with pytest.raises(ConfigurationError) as exc_info:
        WorkspaceStaging.stage_workspace(source_dir)

    assert "Symlink escape detected" in exc_info.value.message


def test_stage_workspace_symlink_to_excluded_file_rejected(tmp_path: Path) -> None:
    """Verify internal symlink pointing to an excluded sensitive file is rejected."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    secret_file = source_dir / ".env"
    secret_file.write_text("SECRET=123", encoding="utf-8")

    symlink_file = source_dir / "disguised_env.txt"
    try:
        os.symlink(secret_file, symlink_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted on this host environment")

    # Staging must detect that disguised_env.txt points to an excluded file (.env)
    with pytest.raises(ConfigurationError) as exc_info:
        WorkspaceStaging.stage_workspace(source_dir)

    assert "targets an excluded sensitive file" in exc_info.value.message


def test_stage_workspace_internal_symlink_allowed(tmp_path: Path) -> None:
    """Verify internal symlink pointing within the workspace to a safe file is allowed."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    target_file = source_dir / "target.txt"
    target_file.write_text("internal data", encoding="utf-8")

    symlink_file = source_dir / "internal_link.txt"
    try:
        os.symlink(target_file, symlink_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted on this host environment")

    staged = WorkspaceStaging.stage_workspace(source_dir)
    try:
        assert (staged / "internal_link.txt").exists()
        assert (staged / "internal_link.txt").read_text(encoding="utf-8") == "internal data"
    finally:
        WorkspaceStaging.cleanup_workspace(staged)


def test_stage_workspace_normal_agent_project_staged_cleanly(tmp_path: Path) -> None:
    """Verify realistic Python agent project structure stages cleanly with manifests and code."""
    source_dir = tmp_path / "my_agent_project"
    source_dir.mkdir()

    (source_dir / "pyproject.toml").write_text("[project]\nname='agent'", encoding="utf-8")
    (source_dir / "requirements.txt").write_text("pydantic>=2.0\n", encoding="utf-8")
    src = source_dir / "src" / "my_agent"
    src.mkdir(parents=True)
    (src / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")
    (src / "__init__.py").write_text("", encoding="utf-8")
    data = source_dir / "data"
    data.mkdir()
    (data / "sample.json").write_text('{"item": 1}', encoding="utf-8")
    (data / "catalog.csv").write_text("id,name\n1,alpha\n", encoding="utf-8")

    # Staged workspace
    staged = WorkspaceStaging.stage_workspace(source_dir)
    try:
        assert (staged / "pyproject.toml").exists()
        assert (staged / "requirements.txt").exists()
        assert (staged / "src" / "my_agent" / "agent.py").exists()
        assert (staged / "data" / "sample.json").exists()
        assert (staged / "data" / "catalog.csv").exists()
    finally:
        WorkspaceStaging.cleanup_workspace(staged)


def test_stage_workspace_invalid_source_raises() -> None:
    """Verify attempting to stage a non-existent directory raises ConfigurationError."""
    with pytest.raises(ConfigurationError):
        WorkspaceStaging.stage_workspace(Path("non_existent_workspace_dir_xyz123"))


def test_stage_workspace_rejects_settings_json_with_api_key(tmp_path: Path) -> None:
    """Verify settings.json containing plaintext API keys is detected and rejected."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")
    test_key = "sk_" + "live_" + "abcdef1234567890abcdef1234"
    (source_dir / "settings.json").write_text(
        f'{{"api_key": "{test_key}", "env": "prod"}}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as exc_info:
        WorkspaceStaging.stage_workspace(source_dir)
    assert "detected in workspace file" in exc_info.value.message or "detected in configuration file" in exc_info.value.message


def test_stage_workspace_rejects_nested_credentials_in_yaml(tmp_path: Path) -> None:
    """Verify YAML configuration with nested database password is detected and rejected."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")
    yaml_content = (
        "database:\n"
        "  host: 'db.internal'\n"
        "  port: 5432\n"
        "  password: 'super_secret_production_password'\n"
    )
    (source_dir / "config.yaml").write_text(yaml_content, encoding="utf-8")

    with pytest.raises(ConfigurationError) as exc_info:
        WorkspaceStaging.stage_workspace(source_dir)
    assert "detected in configuration file" in exc_info.value.message


def test_stage_workspace_excludes_pypirc_and_npmrc(tmp_path: Path) -> None:
    """Verify .pypirc and .npmrc are strictly excluded from staged workspace."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")
    (source_dir / ".pypirc").write_text("[pypi]\nusername = __token__\npassword = pypi-secret", encoding="utf-8")
    (source_dir / ".npmrc").write_text("//registry.npmjs.org/:_authToken=npm_secret", encoding="utf-8")

    staged = WorkspaceStaging.stage_workspace(source_dir)
    try:
        assert (staged / "agent.py").exists()
        assert not (staged / ".pypirc").exists()
        assert not (staged / ".npmrc").exists()
    finally:
        WorkspaceStaging.cleanup_workspace(staged)


def test_stage_workspace_excludes_kube_config(tmp_path: Path) -> None:
    """Verify .kube/config is strictly excluded from staged workspace."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")
    kube_dir = source_dir / ".kube"
    kube_dir.mkdir()
    (kube_dir / "config").write_text("apiVersion: v1\nclusters: []\n", encoding="utf-8")

    staged = WorkspaceStaging.stage_workspace(source_dir)
    try:
        assert (staged / "agent.py").exists()
        assert not (staged / ".kube").exists()
        assert not (staged / ".kube" / "config").exists()
    finally:
        WorkspaceStaging.cleanup_workspace(staged)


def test_stage_workspace_explicit_agent_spec_files_manifest(tmp_path: Path) -> None:
    """Verify AgentSpec explicit files manifest stages only declared items and ignores the rest."""
    from recoverybench.sandbox.spec import AgentSpec

    source_dir = tmp_path / "source"
    source_dir.mkdir()

    # Create multiple files in workspace
    (source_dir / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")
    (source_dir / "helper.py").write_text("def help(): pass\n", encoding="utf-8")
    (source_dir / "unrelated_large_file.txt").write_text("gigantic unneeded dataset", encoding="utf-8")
    sub = source_dir / "extra"
    sub.mkdir()
    (sub / "extra.py").write_text("print('extra')", encoding="utf-8")

    # Spec explicitly requests only agent.py and helper.py
    spec = AgentSpec.from_file(
        source_dir / "agent.py",
        files=["agent.py", "helper.py"],
    )

    staged = WorkspaceStaging.stage_workspace(spec.workspace_dir, manifest_files=spec.files)
    try:
        # Declared files staged
        assert (staged / "agent.py").exists()
        assert (staged / "helper.py").exists()

        # Undeclared files strictly omitted
        assert not (staged / "unrelated_large_file.txt").exists()
        assert not (staged / "extra").exists()
        assert not (staged / "extra" / "extra.py").exists()
    finally:
        WorkspaceStaging.cleanup_workspace(staged)


def test_stage_workspace_rejects_package_manifest_with_embedded_credentials(tmp_path: Path) -> None:
    """Verify package manifest with embedded HTTP basic auth URL credentials is rejected."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")
    (source_dir / "requirements.txt").write_text(
        "--extra-index-url https://user:super_secret_pass@pypi.company.internal/simple\npydantic>=2.0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as exc_info:
        WorkspaceStaging.stage_workspace(source_dir)
    assert "Embedded URL credentials detected" in exc_info.value.message


def test_stage_workspace_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    """Verify declaring a file with path traversal in AgentSpec or manifest raises ConfigurationError."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "agent.py").write_text("def run(t, c): pass\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as exc_info:
        WorkspaceStaging.stage_workspace(source_dir, manifest_files=["../outside_file.py"])
    assert "escapes workspace root" in exc_info.value.message
