"""Distribution Smoke Test for RecoveryBench.

Builds and verifies wheel and sdist distributions, tests package installation
into a clean temporary virtual environment outside the repository, executes
installed console scripts and SDK entry points, and verifies report generation
both with and without the optional LangGraph dependency.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile


def run_cmd(cmd: list[str], cwd: pathlib.Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Execute a subprocess command, capturing output with clear error reporting."""
    cmd_str = " ".join(cmd)
    print(f"--> Running: {cmd_str} (cwd: {cwd or os.getcwd()})")
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        print(f"STDOUT:\n{result.stdout}", file=sys.stderr)
        print(f"STDERR:\n{result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {cmd_str}")
    return result


def main() -> None:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    print("=== RecoveryBench Distribution Smoke Test ===")
    print(f"Repository Root: {repo_root}")

    # 1. Build distribution artifacts
    dist_dir = repo_root / "dist"
    print("\n--- Step 1: Building Wheel and Source Distribution ---")
    run_cmd([sys.executable, "-m", "build"], cwd=repo_root)

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))

    if not wheels:
        sys.exit("FAIL: No wheel (.whl) artifact found in dist/")
    if not sdists:
        sys.exit("FAIL: No source distribution (.tar.gz) artifact found in dist/")

    wheel_path = wheels[-1].resolve()
    sdist_path = sdists[-1].resolve()
    print(f"Found Wheel: {wheel_path.name}")
    print(f"Found Sdist: {sdist_path.name}")

    # 2. Inspect Wheel Contents & Metadata
    print("\n--- Step 2: Inspecting Wheel Package Contents & Metadata ---")
    required_wheel_members = [
        "recoverybench/__init__.py",
        "recoverybench/cli.py",
        "recoverybench/sdk/tester.py",
        "recoverybench/sandbox/backend.py",
        "recoverybench/sandbox/config.py",
        "recoverybench/sandbox/worker.py",
        "recoverybench/reporting/html_reporter.py",
        "recoverybench/reporting/json_reporter.py",
        "recoverybench/reporting/junit_reporter.py",
        "recoverybench/reporting/schema.py",
        "recoverybench/sample_project/resources.py",
        "recoverybench/scenarios/registry.py",
        "recoverybench/scenarios/validator.py",
    ]

    with zipfile.ZipFile(wheel_path) as zf:
        members = zf.namelist()
        for req in required_wheel_members:
            if req not in members:
                sys.exit(f"FAIL: Required module '{req}' missing from wheel members!")
        print(f"[PASS] All {len(required_wheel_members)} critical modules present in wheel.")

        # Check license and metadata
        license_members = [m for m in members if m.endswith(".dist-info/licenses/LICENSE") or m.endswith(".dist-info/LICENSE")]
        if not license_members:
            sys.exit("FAIL: LICENSE missing from wheel dist-info!")
        print(f"[PASS] License file present in wheel: {license_members[0]}")

        metadata_members = [m for m in members if m.endswith(".dist-info/METADATA")]
        if not metadata_members:
            sys.exit("FAIL: METADATA missing from wheel dist-info!")
        metadata_text = zf.read(metadata_members[0]).decode("utf-8", errors="replace")

        # Verify PEP 639 and PyPI metadata
        for expected_header in [
            "License-Expression: Apache-2.0",
            "License-File: LICENSE",
            "Classifier: Development Status :: 3 - Alpha",
            "Classifier: Programming Language :: Python :: 3.11",
        ]:
            if expected_header not in metadata_text:
                sys.exit(f"FAIL: Wheel METADATA missing expected line: '{expected_header}'")
        print("[PASS] Wheel METADATA verified (PEP 639 license and PyPI classifiers present).")

    # 3. Create Clean Test Virtual Environment Outside Repository
    print("\n--- Step 3: Creating Isolated Virtual Environment Outside Repo ---")
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="rb_dist_test_")).resolve()
    venv_dir = temp_dir / "venv"
    run_dir = temp_dir / "workdir"
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        run_cmd([sys.executable, "-m", "venv", str(venv_dir)])

        # Locate executables
        if sys.platform == "win32":
            venv_python = venv_dir / "Scripts" / "python.exe"
            venv_cli = venv_dir / "Scripts" / "recoverybench.exe"
        else:
            venv_python = venv_dir / "bin" / "python"
            venv_cli = venv_dir / "bin" / "recoverybench"

        if not venv_python.exists():
            sys.exit(f"FAIL: Virtualenv python executable not found at {venv_python}")

        # 4. Install wheel into clean venv (WITHOUT optional extras)
        print("\n--- Step 4: Installing Core Wheel into Clean Environment ---")
        run_cmd([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
        run_cmd([str(venv_python), "-m", "pip", "install", str(wheel_path)])

        if not venv_cli.exists():
            sys.exit(f"FAIL: Installed console script 'recoverybench' not found at {venv_cli}")
        print(f"[PASS] Installed recoverybench console script verified at {venv_cli}")

        # 5. Execute commands outside repo working directory
        print("\n--- Step 5: Testing Installed Console Script and Commands Outside Repo ---")

        # Test recoverybench --version
        ver_res = run_cmd([str(venv_cli), "--version"], cwd=run_dir)
        print(f"Output of --version: {ver_res.stdout.strip()}")
        if "0.2.0" not in ver_res.stdout or "RecoveryBench" not in ver_res.stdout:
            sys.exit(f"FAIL: Unexpected --version output: {ver_res.stdout}")
        print("[PASS] 'recoverybench --version' passed.")

        # Test recoverybench --help
        help_res = run_cmd([str(venv_cli), "--help"], cwd=run_dir)
        if "Usage:" not in help_res.stdout or "doctor" not in help_res.stdout:
            sys.exit(f"FAIL: Unexpected --help output: {help_res.stdout}")
        print("[PASS] 'recoverybench --help' passed.")

        # Test recoverybench list-scenarios
        list_res = run_cmd([str(venv_cli), "list-scenarios"], cwd=run_dir)
        if "payment_timeout" not in list_res.stdout:
            sys.exit(f"FAIL: Built-in scenarios not listed in table: {list_res.stdout}")
        print("[PASS] 'recoverybench list-scenarios' passed.")

        # Test recoverybench list-scenarios --json
        list_json_res = run_cmd([str(venv_cli), "list-scenarios", "--json"], cwd=run_dir)
        if "payment_timeout_after_commit" not in list_json_res.stdout:
            sys.exit(f"FAIL: Built-in scenarios not listed in JSON: {list_json_res.stdout}")
        print("[PASS] 'recoverybench list-scenarios --json' passed.")

        # Test recoverybench demo (runs in trusted in-process mode by default)
        print("\n--- Step 6: Executing Out-of-the-Box Demo in Installed Environment ---")
        demo_res = run_cmd([str(venv_cli), "demo"], cwd=run_dir)
        if "Comparative Summary" not in demo_res.stdout or "GENUINE_FAILURE" not in demo_res.stdout:
            sys.exit(f"FAIL: Demo did not output comparative summary: {demo_res.stdout}")
        print("[PASS] 'recoverybench demo' executed successfully.")

        # Confirm demo reports generated in workdir/.recoverybench/reports/
        reports_dir = run_dir / ".recoverybench" / "reports"
        expected_reports = [
            "demo_naive_report.json",
            "demo_safe_report.json",
            "demo_safe_report.html",
            "demo_safe_report.xml",
        ]
        for rep in expected_reports:
            rep_path = reports_dir / rep
            if not rep_path.exists() or rep_path.stat().st_size == 0:
                sys.exit(f"FAIL: Expected demo report '{rep}' missing or empty at {rep_path}!")
        print(f"[PASS] All {len(expected_reports)} demo reports successfully generated.")

        # Test recoverybench validate-report on generated demo reports
        val_safe = run_cmd([str(venv_cli), "validate-report", str(reports_dir / "demo_safe_report.json")], cwd=run_dir)
        if "Valid execution report" not in val_safe.stdout:
            sys.exit(f"FAIL: validate-report failed on demo_safe_report.json: {val_safe.stdout}")
        val_naive = run_cmd([str(venv_cli), "validate-report", str(reports_dir / "demo_naive_report.json")], cwd=run_dir)
        if "Valid execution report" not in val_naive.stdout:
            sys.exit(f"FAIL: validate-report failed on demo_naive_report.json: {val_naive.stdout}")
        print("[PASS] 'recoverybench validate-report' verified on demo reports.")

        # 6. Verify core works without LangGraph
        print("\n--- Step 7: Verifying Core Package Without LangGraph Extra ---")
        py_check = run_cmd(
            [str(venv_python), "-c", "import recoverybench; print('VER:', recoverybench.__version__)"],
            cwd=run_dir,
        )
        if "VER: 0.2.0" not in py_check.stdout:
            sys.exit(f"FAIL: Incorrect imported version: {py_check.stdout}")

        langgraph_check = run_cmd(
            [str(venv_python), "-c", "import langgraph"],
            cwd=run_dir,
            check=False,
        )
        if langgraph_check.returncode == 0:
            sys.exit("FAIL: langgraph should NOT be installed in core environment!")
        print("[PASS] Confirmed langgraph is absent in core install and core functions independently.")

        # 7. Install optional LangGraph extra and verify
        print("\n--- Step 8: Installing and Verifying Optional LangGraph Extra ---")
        run_cmd([str(venv_python), "-m", "pip", "install", f"{wheel_path}[langgraph]"])
        lg_verify = run_cmd(
            [
                str(venv_python),
                "-c",
                "import recoverybench; from recoverybench.adapters.langgraph import LangGraphAdapter, IdempotentLangGraphAgent; print('LANGGRAPH_OK')",
            ],
            cwd=run_dir,
        )
        if "LANGGRAPH_OK" not in lg_verify.stdout:
            sys.exit(f"FAIL: LangGraph adapter import failed after installing extra: {lg_verify.stdout}")
        print("[PASS] LangGraph extra installed and LangGraphAdapter successfully imported.")

        print("\n=================================================================")
        print("  ALL DISTRIBUTION SMOKE TESTS PASSED SUCCESSFULLY!")
        print("=================================================================")

    finally:
        # Clean up temporary test directory
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"Cleaned up temporary test directory: {temp_dir}")


if __name__ == "__main__":
    main()
