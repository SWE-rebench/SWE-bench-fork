import os
import posixpath
import re
import requests
from collections import OrderedDict
from unidiff import PatchSet
from io import StringIO

from swebench.harness.constants import (
    SWEbenchInstance,
    MAP_REPO_TO_ENV_YML_PATHS,
    MAP_REPO_TO_INSTALL,
    MAP_REPO_TO_REQS_PATHS,
    MAP_REPO_VERSION_TO_SPECS,
    NON_TEST_EXTS,
    SWE_BENCH_URL_RAW,
    START_TEST_OUTPUT,
    END_TEST_OUTPUT,
)
from swebench.harness.utils import get_modified_files
from functools import cache

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36"
}


@cache
def get_environment_yml_by_commit(repo: str, commit: str, env_name: str, reqs_paths = None) -> str:
    if reqs_paths is None:
        reqs_paths = MAP_REPO_TO_REQS_PATHS[repo]

    for req_path in reqs_paths:
        reqs_url = posixpath.join(SWE_BENCH_URL_RAW, repo, commit, req_path)
        reqs = requests.get(reqs_url, headers=HEADERS)
        if reqs.status_code == 200:
            break
    else:
        raise ValueError(
            f"Could not find environment.yml at paths {MAP_REPO_TO_ENV_YML_PATHS[repo]} for repo {repo} at commit {commit}"
        )

    lines = reqs.text.split("\n")
    cleaned = []
    for line in lines:
        # Rename environment to given name
        if line.startswith("name:"):
            cleaned.append(f"name: {env_name}")
            continue
        cleaned.append(line)

    return "\n".join(cleaned)


def get_environment_yml(instance: SWEbenchInstance, env_name: str, reqs_paths = None) -> str:
    """
    Get environment.yml for given task instance

    Args:
        instance (dict): SWE Bench Task instance
        env_name (str): Rename retrieved environment.yml to this name
    Returns:
        environment.yml (str): Returns environment.yml as string
    """
    # Attempt to find environment.yml at each path based on task instance's repo
    commit = (
        instance["environment_setup_commit"]
        if "environment_setup_commit" in instance
        else instance["base_commit"]
    )

    return get_environment_yml_by_commit(instance["repo"], commit, env_name, reqs_paths)


@cache
def get_requirements_by_commit(repo: str, commit: str, reqs_paths = None) -> str:
    all_lines = []
    if reqs_paths is None:
        reqs_paths = MAP_REPO_TO_REQS_PATHS.get(repo, [])

    for req_path in reqs_paths:
        reqs_url = posixpath.join(SWE_BENCH_URL_RAW, repo, commit, req_path)
        reqs = requests.get(reqs_url, headers=HEADERS)
        if reqs.status_code == 200:
            all_lines += reqs.text.split('\n')
    # else:
        # raise ValueError(
        #     f"Could not find requirements.txt at paths {MAP_REPO_TO_REQS_PATHS[repo]} for repo {repo} at commit {commit}"
        # )
        # return ""

    if not all_lines:
        return ""

    original_req = []
    additional_reqs = []
    req_dir = "/".join(req_path.split("/")[:-1])
    exclude_line = lambda line: any(
        [line.strip().startswith(x) for x in ["-e .", "#", ".[test", "-c"]]
    ) or line.strip() == "."

    for line in all_lines:
        if line.strip().startswith("-r"):
            # Handle recursive requirements
            file_name = line[len("-r") :].strip()
            reqs_url = os.path.join(
                SWE_BENCH_URL_RAW,
                repo,
                commit,
                req_dir,
                file_name,
            )
            reqs = requests.get(reqs_url, headers=HEADERS)
            if reqs.status_code == 200:
                for line_extra in reqs.text.split("\n"):
                    if not exclude_line(line_extra):
                        additional_reqs.append(line_extra)
        else:
            if not exclude_line(line):
                original_req.append(line)

    # Combine all requirements into single text body
    get_package_name = lambda line: line.split("==")[0].split(">=")[0].split("<=")[0].strip()
    original_req += additional_reqs

    deduped = OrderedDict()
    for line in original_req:
        pkg = get_package_name(line)
        if pkg and pkg not in deduped:
            deduped[pkg] = line
    original_req = list(deduped.values())

    all_reqs = "\n".join(original_req)

    return all_reqs


def get_requirements(instance: SWEbenchInstance) -> str:
    """
    Get requirements.txt for given task instance

    Args:
        instance (dict): task instance
    Returns:
        requirements.txt (str): Returns requirements.txt as string
    """
    # Attempt to find requirements.txt at each path based on task instance's repo
    commit = (
        instance["environment_setup_commit"]
        if "environment_setup_commit" in instance
        else instance["base_commit"]
    )
    reqs_paths = None
    if "install_config" in instance and instance["install_config"].get("reqs_path") is not None:
        reqs_paths = tuple(instance["install_config"].get("reqs_path", []))

    return get_requirements_by_commit(instance["repo"], commit, reqs_paths)

def get_changed_files(patch: str) -> list[str]:
    """Extract changed or added filenames from a git patch, excluding deleted files."""

    files = []
    patch_set: PatchSet = PatchSet(StringIO(patch))

    for patched_file in patch_set:
        # Skip deleted files
        if not patched_file.is_removed_file:
            files.append(patched_file.path)

    return files


def get_default_test_directives(test_patch: str) -> list[str]:
    directives = get_changed_files(test_patch)
    return [d for d in directives if not any(d.endswith(ext) for ext in NON_TEST_EXTS)]


def get_test_directives(instance) -> list[str]:
    """
    Get test directives from the test_patch of a task instance

    Args:
        instance (dict): task instance
    Returns:
        directives (list): List of test directives
    """
    # HumanEvalFix: For seq2seq code repos, testing command is fixed
    if any(
        x == instance["repo"]
        for x in ["swe-bench/humaneval", "swe-bench/humanevalfix-python"]
    ):
        return ["test.py"]
    if any(
        x == instance["repo"]
        for x in ["swe-bench/humanevalfix-go", "swe-bench/humanevalfix-java"]
    ):
        return []
    if instance["repo"] == "swe-bench/humanevalfix-js":
        return ["test.js"]
    if instance["repo"] == "nebius/nebo":
        return [""]

    # Get test directives from test patch and remove non-test files
    test_patch = instance["test_patch"]
    directives = get_default_test_directives(test_patch)

    # For Django tests, remove extension + "tests/" prefix and convert slashes to dots (module referencing)
    if instance["repo"] == "django/django":
        directives_transformed = []
        for d in directives:
            d = d[: -len(".py")] if d.endswith(".py") else d
            d = d[len("tests/") :] if d.startswith("tests/") else d
            d = d.replace("/", ".")
            directives_transformed.append(d)
        directives = directives_transformed

    return directives


def make_repo_script_list_py(
    specs, repo, repo_directory, base_commit, env_name
) -> list:
    """
    Create a list of bash commands to set up the repository for testing.
    This is the setup script for the instance image.
    """
    setup_commands = [
        f"git clone -o origin https://github.com/{repo} {repo_directory}",
        f"chmod -R 777 {repo_directory}",  # So nonroot user can run tests
        f"cd {repo_directory}",
        f"git reset --hard {base_commit}",
        # Remove the remote so the agent won't see newer commits.
        "git remote remove origin",
        # Make sure conda is available for later use
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        'echo "Current environment: $CONDA_DEFAULT_ENV"',
    ]
    if repo in MAP_REPO_TO_INSTALL:
        setup_commands.append(MAP_REPO_TO_INSTALL[repo])

    # Run pre-install set up if provided
    if specs.get("pre_install"):
        for pre_install in specs["pre_install"]:
            setup_commands.append(pre_install)

    if specs.get("install"):
        setup_commands.append(specs["install"])

    # If the setup modifies the repository in any way, it can be 
    # difficult to get a clean diff.  This ensures that `git diff`
    # will only reflect the changes from the user while retaining the
    # original state of the repository plus setup commands.
    clean_diff_commands = [
        "git config --global user.email setup@swebench.config",
        "git config --global user.name SWE-bench",
        "git commit --allow-empty -am SWE-bench",
    ]

    setup_commands += clean_diff_commands

    return setup_commands


def make_env_script_list_py(instance, specs, env_name) -> list:
    """
    Creates the list of commands to set up the conda environment for testing.
    This is the setup script for the environment image.
    """
    HEREDOC_DELIMITER = "EOF_59812759871"
    reqs_commands = [
        "source /opt/miniconda3/bin/activate",
    ]
    # Create conda environment according to install instructinos
    pkgs = specs.get("packages", "")
    if pkgs == "requirements.txt":
        # Create environment
        cmd = f"conda create -n {env_name} python={specs['python']} -y"
        reqs_commands.append(cmd)

        # Install dependencies
        reqs = get_requirements(instance)
        path_to_reqs = "$HOME/requirements.txt"
        reqs_commands.append(
            f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}"
        )
        cmd = f"conda activate {env_name} && python -m pip install -r {path_to_reqs}"
        reqs_commands.append(cmd)
        reqs_commands.append(f"rm {path_to_reqs}")
    elif pkgs == "environment.yml":
        # Create environment from yml
        reqs_paths = None
        if specs.get("env_yml_path"):
            reqs_paths = tuple(specs["env_yml_path"])
        reqs = get_environment_yml(instance, env_name, reqs_paths)
        path_to_reqs = "environment.yml"
        reqs_commands.append(
            f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}"
        )
        if "no_use_env" in specs and specs["no_use_env"]:
            # `conda create` based installation
            cmd = (
                f"conda create -c conda-forge -n {env_name} python={specs['python']} -y"
            )
            reqs_commands.append(cmd)

            # Install dependencies
            cmd = f"conda env update -f {path_to_reqs}"
            reqs_commands.append(cmd)
        else:
            # `conda env create` based installation
            cmd = f"conda env create --file {path_to_reqs}"
            reqs_commands.append(cmd)

            cmd = f"conda activate {env_name} && conda install python={specs['python']} -y"
            reqs_commands.append(cmd)

        # Remove environment.yml
        reqs_commands.append(f"rm {path_to_reqs}")
    else:
        # Create environment + install dependencies
        cmd = f"conda create -n {env_name} python={specs['python']} {pkgs} -y"
        reqs_commands.append(cmd)

    reqs_commands.append(f"conda activate {env_name}")

    # Install additional packages if specified
    if specs.get("pip_packages"):
        pip_packages = " ".join(specs["pip_packages"])
        cmd = f"python -m pip install {pip_packages}"
        reqs_commands.append(cmd)
    return reqs_commands


def make_eval_script_list_py(
    instance, specs, env_name, repo_directory, base_commit, test_patch
) -> list:
    """
    Applies the test patch and runs the tests.
    """
    HEREDOC_DELIMITER = "EOF_114329324912"
    test_files = get_modified_files(test_patch)
    # Reset test files to the state they should be in before the patch.
    reset_tests_command = f"git checkout {base_commit} {' '.join(test_files)}"
    apply_test_patch_command = (
        f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{test_patch}\n{HEREDOC_DELIMITER}"
    )
    if "test_cmd" in specs:
        test_cmd = specs["test_cmd"]
    else:
        test_cmd = MAP_REPO_VERSION_TO_SPECS[instance["repo"]][instance["version"]][
            "test_cmd"
        ]
    test_command = " ".join(
            [
                test_cmd,
                *get_test_directives(instance),
            ]
        )
    eval_commands = [
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f"cd {repo_directory}",
    ]
    if "eval_commands" in specs:
        eval_commands += specs["eval_commands"]
    eval_commands += [
        f"git config --global --add safe.directory {repo_directory}",  # for nonroot user
        f"cd {repo_directory}",
        # This is just informational, so we have a record
        "git status",
        "git show",
        f"git -c core.fileMode=false diff {base_commit}",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
    ]
    if "install" in specs:
        eval_commands.append(specs["install"])
    eval_commands += [
        reset_tests_command,
        apply_test_patch_command,
        f": '{START_TEST_OUTPUT}'",
        test_command,
        f": '{END_TEST_OUTPUT}'",
        reset_tests_command,  # Revert tests after done, leave the repo in the same state as before
    ]
    return eval_commands
