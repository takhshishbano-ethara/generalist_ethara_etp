from odoo.addons.aurora.tools.harness.test_result import get_modified_files

_DEFAULT_BASE_CMD = "pytest --no-header -rA --tb=no -p no:cacheprovider"


def _test_files_from_patch(patch: str, only_py: bool = False) -> list[str]:
    files = get_modified_files(patch)
    if only_py:
        files = [f for f in files if f.endswith(".py")]
    return sorted(set(files))


def python_test_command(
    test_patch: str,
    base_test_cmd: str = _DEFAULT_BASE_CMD,
) -> str:
    test_files = _test_files_from_patch(test_patch)
    if test_files:
        return f"{base_test_cmd} {' '.join(test_files)}"
    return base_test_cmd


def python_test_command_only_py(
    test_patch: str,
    base_test_cmd: str = _DEFAULT_BASE_CMD,
) -> str:
    test_files = _test_files_from_patch(test_patch, only_py=True)
    if test_files:
        return f"{base_test_cmd} {' '.join(test_files)}"
    return base_test_cmd
