# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import difflib
import itertools
import json
import os
import re
import sys
from collections import defaultdict
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Any, TypeVar

from airflow_breeze.branch_defaults import AIRFLOW_BRANCH, DEFAULT_AIRFLOW_CONSTRAINTS_BRANCH
from airflow_breeze.global_constants import (
    ALL_PYTHON_MAJOR_MINOR_VERSIONS,
    APACHE_AIRFLOW_GITHUB_REPOSITORY,
    COMMITTERS,
    CURRENT_KUBERNETES_VERSIONS,
    CURRENT_MYSQL_VERSIONS,
    CURRENT_POSTGRES_VERSIONS,
    CURRENT_PYTHON_MAJOR_MINOR_VERSIONS,
    DEFAULT_KUBERNETES_VERSION,
    DEFAULT_MYSQL_VERSION,
    DEFAULT_POSTGRES_VERSION,
    DEFAULT_PYTHON_MAJOR_MINOR_VERSION,
    DISABLE_TESTABLE_INTEGRATIONS_FROM_CI,
    HELM_VERSION,
    KIND_VERSION,
    NUMBER_OF_LOW_DEP_SLICES,
    PROVIDERS_COMPATIBILITY_TESTS_MATRIX,
    PUBLIC_AMD_RUNNERS,
    PUBLIC_ARM_RUNNERS,
    TESTABLE_CORE_INTEGRATIONS,
    TESTABLE_PROVIDERS_INTEGRATIONS,
    GithubEvents,
    SelectiveAirflowCtlTestType,
    SelectiveCoreTestType,
    SelectiveProvidersTestType,
    SelectiveTaskSdkTestType,
    all_helm_test_packages,
    all_selective_core_test_types,
    providers_test_type,
)
from airflow_breeze.utils.console import get_console
from airflow_breeze.utils.exclude_from_matrix import excluded_combos
from airflow_breeze.utils.functools_cache import clearable_cache
from airflow_breeze.utils.kubernetes_utils import get_kubernetes_python_combos
from airflow_breeze.utils.packages import get_available_distributions
from airflow_breeze.utils.path_utils import (
    AIRFLOW_DEVEL_COMMON_PATH,
    AIRFLOW_PROVIDERS_ROOT_PATH,
    AIRFLOW_ROOT_PATH,
)
from airflow_breeze.utils.provider_dependencies import DEPENDENCIES, get_related_providers
from airflow_breeze.utils.run_utils import run_command

ALL_VERSIONS_LABEL = "all versions"
CANARY_LABEL = "canary"
DEBUG_CI_RESOURCES_LABEL = "debug ci resources"
DEFAULT_VERSIONS_ONLY_LABEL = "default versions only"
DISABLE_IMAGE_CACHE_LABEL = "disable image cache"
FORCE_PIP_LABEL = "force pip"
FULL_TESTS_NEEDED_LABEL = "full tests needed"
INCLUDE_SUCCESS_OUTPUTS_LABEL = "include success outputs"
LATEST_VERSIONS_ONLY_LABEL = "latest versions only"
LOG_WITHOUT_MOCK_IN_TESTS_EXCEPTION_LABEL = "log exception"
NON_COMMITTER_BUILD_LABEL = "non committer build"
UPGRADE_TO_NEWER_DEPENDENCIES_LABEL = "upgrade to newer dependencies"
USE_PUBLIC_RUNNERS_LABEL = "use public runners"

ALL_CI_SELECTIVE_TEST_TYPES = "API Always CLI Core Other Serialization"

ALL_PROVIDERS_SELECTIVE_TEST_TYPES = (
    "Providers[-amazon,google,standard] Providers[amazon] Providers[google] Providers[standard]"
)


class FileGroupForCi(Enum):
    ENVIRONMENT_FILES = "environment_files"
    PYTHON_PRODUCTION_FILES = "python_scans"
    JAVASCRIPT_PRODUCTION_FILES = "javascript_scans"
    ALWAYS_TESTS_FILES = "always_test_files"
    API_FILES = "api_files"
    GIT_PROVIDER_FILES = "git_provider_files"
    STANDARD_PROVIDER_FILES = "standard_provider_files"
    API_CODEGEN_FILES = "api_codegen_files"
    HELM_FILES = "helm_files"
    DEPENDENCY_FILES = "dependency_files"
    DOC_FILES = "doc_files"
    UI_FILES = "ui_files"
    SYSTEM_TEST_FILES = "system_tests"
    KUBERNETES_FILES = "kubernetes_files"
    TASK_SDK_FILES = "task_sdk_files"
    GO_SDK_FILES = "go_sdk_files"
    AIRFLOW_CTL_FILES = "airflow_ctl_files"
    ALL_PYPROJECT_TOML_FILES = "all_pyproject_toml_files"
    ALL_PYTHON_FILES = "all_python_files"
    ALL_SOURCE_FILES = "all_sources_for_tests"
    ALL_AIRFLOW_PYTHON_FILES = "all_airflow_python_files"
    ALL_AIRFLOW_CTL_PYTHON_FILES = "all_airflow_ctl_python_files"
    ALL_PROVIDERS_PYTHON_FILES = "all_provider_python_files"
    ALL_PROVIDERS_DISTRIBUTION_CONFIG_FILES = "all_provider_distribution_config_files"
    ALL_DEV_PYTHON_FILES = "all_dev_python_files"
    ALL_DEVEL_COMMON_PYTHON_FILES = "all_devel_common_python_files"
    ALL_PROVIDER_YAML_FILES = "all_provider_yaml_files"
    TESTS_UTILS_FILES = "test_utils_files"
    ASSET_FILES = "asset_files"
    UNIT_TEST_FILES = "unit_test_files"
    DEVEL_TOML_FILES = "devel_toml_files"


class AllProvidersSentinel:
    pass


ALL_PROVIDERS_SENTINEL = AllProvidersSentinel()

T = TypeVar("T", FileGroupForCi, SelectiveCoreTestType)


class HashableDict(dict[T, list[str]]):
    def __hash__(self):
        return hash(frozenset(self))


CI_FILE_GROUP_MATCHES = HashableDict(
    {
        FileGroupForCi.ENVIRONMENT_FILES: [
            r"^.github/workflows",
            r"^dev/breeze",
            r"^dev/.*\.py$",
            r"^Dockerfile",
            r"^scripts/ci/docker-compose",
            r"^scripts/ci/kubernetes",
            r"^scripts/docker",
            r"^scripts/in_container",
            r"^generated/provider_dependencies.json$",
        ],
        FileGroupForCi.PYTHON_PRODUCTION_FILES: [
            r"^airflow-core/src/airflow/.*\.py",
            r"^providers/.*\.py",
            r"^pyproject.toml",
            r"^hatch_build.py",
        ],
        FileGroupForCi.JAVASCRIPT_PRODUCTION_FILES: [
            r"^airflow-core/src/airflow/.*\.[jt]sx?",
            r"^airflow-core/src/airflow/.*\.lock",
            r"^airflow-core/src/airflow/ui/.*\.yaml$",
            r"^airflow-core/src/airflow/api_fastapi/auth/managers/simple/ui/.*\.yaml$",
        ],
        FileGroupForCi.API_FILES: [
            r"^airflow-core/src/airflow/api/",
            r"^airflow-core/src/airflow/api_fastapi/",
            r"^airflow-core/tests/unit/api/",
            r"^airflow-core/tests/unit/api_fastapi/",
        ],
        FileGroupForCi.GIT_PROVIDER_FILES: [
            r"^providers/git/src/",
        ],
        FileGroupForCi.STANDARD_PROVIDER_FILES: [
            r"^providers/standard/src/",
        ],
        FileGroupForCi.API_CODEGEN_FILES: [
            r"^airflow-core/src/airflow/api_fastapi/core_api/openapi/.*generated\.yaml",
            r"^clients/gen",
        ],
        FileGroupForCi.HELM_FILES: [
            r"^chart",
            r"^airflow-core/src/airflow/kubernetes",
            r"^airflow-core/tests/unit/kubernetes",
            r"^helm-tests",
        ],
        FileGroupForCi.DOC_FILES: [
            r"^docs",
            r"^devel-common/src/docs",
            r"^\.github/SECURITY\.md",
            r"^airflow-core/src/.*\.py$",
            r"^airflow-core/docs/",
            r"^providers/.*/src/",
            r"^providers/.*/tests/",
            r"^providers/.*/docs/",
            r"^providers-summary-docs",
            r"^docker-stack-docs",
            r"^chart",
            r"^task-sdk/docs/",
            r"^task-sdk/src/",
            r"^airflow-ctl/src/",
            r"^airflow-core/tests/system",
            r"^airflow-ctl/src",
            r"^airflow-ctl/docs",
            r"^CHANGELOG\.txt",
            r"^airflow-core/src/airflow/config_templates/config\.yml",
            r"^chart/RELEASE_NOTES\.rst",
            r"^chart/values\.schema\.json",
            r"^chart/values\.json",
            r"^RELEASE_NOTES\.rst",
        ],
        FileGroupForCi.UI_FILES: [
            r"^airflow-core/src/airflow/ui/",
            r"^airflow-core/src/airflow/api_fastapi/auth/managers/simple/ui/",
        ],
        FileGroupForCi.KUBERNETES_FILES: [
            r"^chart",
            r"^kubernetes-tests",
            r"^providers/cncf/kubernetes/",
        ],
        FileGroupForCi.ALL_PYTHON_FILES: [
            r".*\.py$",
        ],
        FileGroupForCi.ALL_AIRFLOW_PYTHON_FILES: [
            r"^airflow-core/.*\.py$",
        ],
        FileGroupForCi.ALL_AIRFLOW_CTL_PYTHON_FILES: [
            r"^airflow-ctl/.*\.py$",
        ],
        FileGroupForCi.ALL_PROVIDERS_PYTHON_FILES: [
            r"^providers/.*\.py$",
        ],
        FileGroupForCi.ALL_PROVIDERS_DISTRIBUTION_CONFIG_FILES: [
            r"^providers/.*/pyproject\.toml$",
            r"^providers/.*/provider\.yaml$",
        ],
        FileGroupForCi.ALL_DEV_PYTHON_FILES: [
            r"^dev/.*\.py$",
        ],
        FileGroupForCi.ALL_DEVEL_COMMON_PYTHON_FILES: [
            r"^devel-common/.*\.py$",
        ],
        FileGroupForCi.ALL_SOURCE_FILES: [
            r"^.pre-commit-config.yaml$",
            r"^airflow-core/.*",
            r"^airflow-ctl/.*",
            r"^chart/.*",
            r"^providers/.*",
            r"^task-sdk/.*",
            r"^devel-common/.*",
            r"^kubernetes-tests/.*",
            r"^docker-tests/.*",
            r"^dev/.*",
        ],
        FileGroupForCi.SYSTEM_TEST_FILES: [
            r"^airflow-core/tests/system/",
        ],
        FileGroupForCi.ALWAYS_TESTS_FILES: [
            r"^airflow-core/tests/unit/always/",
        ],
        FileGroupForCi.ALL_PROVIDER_YAML_FILES: [
            r".*/provider\.yaml$",
        ],
        FileGroupForCi.ALL_PYPROJECT_TOML_FILES: [
            r".*pyproject\.toml$",
        ],
        FileGroupForCi.TESTS_UTILS_FILES: [
            r"^airflow-core/tests/unit/utils/",
            r"^devel-common/.*\.py$",
        ],
        FileGroupForCi.TASK_SDK_FILES: [
            r"^task-sdk/src/airflow/sdk/.*\.py$",
            r"^task-sdk/tests/.*\.py$",
        ],
        FileGroupForCi.GO_SDK_FILES: [
            r"^go-sdk/.*\.go$",
        ],
        FileGroupForCi.ASSET_FILES: [
            r"^airflow-core/src/airflow/assets/",
            r"^airflow-core/src/airflow/models/assets/",
            r"^airflow-core/src/airflow/datasets/",
            r"^task-sdk/src/airflow/sdk/definitions/asset/",
        ],
        FileGroupForCi.UNIT_TEST_FILES: [
            r"^airflow-core/tests/unit/",
            r"^task-sdk/tests/",
            r"^providers/.*/tests/unit/",
            r"^dev/breeze/tests/",
            r"^airflow-ctl/tests/",
        ],
        FileGroupForCi.AIRFLOW_CTL_FILES: [
            r"^airflow-ctl/src/airflowctl/.*\.py$",
            r"^airflow-ctl/tests/.*\.py$",
        ],
        FileGroupForCi.DEVEL_TOML_FILES: [
            r"^devel-common/pyproject\.toml$",
        ],
    }
)

PYTHON_OPERATOR_FILES = [
    r"^providers/tests/standard/operators/test_python.py",
]

TEST_TYPE_MATCHES = HashableDict(
    {
        SelectiveCoreTestType.API: [
            r"^airflow-core/src/airflow/api/",
            r"^airflow-core/src/airflow/api_fastapi/",
            r"^airflow-core/tests/unit/api/",
            r"^airflow-core/tests/unit/api_fastapi/",
        ],
        SelectiveCoreTestType.CLI: [
            r"^airflow-core/src/airflow/cli/",
            r"^airflow-core/tests/unit/cli/",
        ],
        SelectiveProvidersTestType.PROVIDERS: [
            r"^providers/.*/src/airflow/providers/",
            r"^providers/.*/tests/",
        ],
        SelectiveTaskSdkTestType.TASK_SDK: [
            r"^task-sdk/src/",
            r"^task-sdk/tests/",
        ],
        SelectiveCoreTestType.SERIALIZATION: [
            r"^airflow-core/src/airflow/serialization/",
            r"^airflow-core/tests/unit/serialization/",
        ],
        SelectiveAirflowCtlTestType.AIRFLOW_CTL: [
            r"^airflow-ctl/src/",
            r"^airflow-ctl/tests/",
        ],
    }
)


def find_provider_affected(changed_file: str, include_docs: bool) -> str | None:
    file_path = AIRFLOW_ROOT_PATH / changed_file
    if not include_docs:
        for parent_dir_path in file_path.parents:
            if parent_dir_path.name == "docs" and (parent_dir_path.parent / "provider.yaml").exists():
                # Skip Docs changes if include_docs is not set
                return None
    # Find if the path under src/system tests/tests belongs to provider or is a common code across
    # multiple providers
    for parent_dir_path in file_path.parents:
        if parent_dir_path == AIRFLOW_PROVIDERS_ROOT_PATH:
            # We have not found any provider specific path up to the root of the provider base folder
            break
        if parent_dir_path.is_relative_to(AIRFLOW_PROVIDERS_ROOT_PATH):
            relative_path = parent_dir_path.relative_to(AIRFLOW_PROVIDERS_ROOT_PATH)
            # check if this path belongs to a specific provider
            if (parent_dir_path / "provider.yaml").exists():
                # new providers structure
                return str(relative_path).replace(os.sep, ".")
    if file_path.is_relative_to(AIRFLOW_DEVEL_COMMON_PATH):
        # if devel-common changes, we want to run tests for all providers, as they might start failing
        return "Providers"
    return None


def _match_files_with_regexps(files: tuple[str, ...], matched_files, matching_regexps):
    for file in files:
        if any(re.match(regexp, file) for regexp in matching_regexps):
            matched_files.append(file)


def _exclude_files_with_regexps(files: tuple[str, ...], matched_files, exclude_regexps):
    for file in files:
        if any(re.match(regexp, file) for regexp in exclude_regexps):
            if file in matched_files:
                matched_files.remove(file)


@clearable_cache
def _matching_files(
    files: tuple[str, ...], match_group: FileGroupForCi, match_dict: HashableDict
) -> list[str]:
    matched_files: list[str] = []
    match_regexps = match_dict[match_group]
    _match_files_with_regexps(files, matched_files, match_regexps)
    count = len(matched_files)
    if count > 0:
        get_console().print(f"[warning]{match_group} matched {count} files.[/]")
        get_console().print(matched_files)
    else:
        get_console().print(f"[warning]{match_group} did not match any file.[/]")
    return matched_files


# TODO: In Python 3.12 we will be able to use itertools.batched
def _split_list(input_list, n) -> list[list[str]]:
    """Splits input_list into n sub-lists."""
    it = iter(input_list)
    return [
        list(itertools.islice(it, i))
        for i in [len(input_list) // n + (1 if x < len(input_list) % n else 0) for x in range(n)]
    ]


def _get_test_type_description(provider_test_types: list[str]) -> str:
    if not provider_test_types:
        return ""
    first_provider = provider_test_types[0]
    last_provider = provider_test_types[-1]
    if first_provider.startswith("Providers["):
        first_provider = first_provider.replace("Providers[", "").replace("]", "")
    if last_provider.startswith("Providers["):
        last_provider = last_provider.replace("Providers[", "").replace("]", "")
    return (
        f"{first_provider[:13]}...{last_provider[:13]}"
        if first_provider != last_provider
        else (first_provider[:29])
    )


def _get_test_list_as_json(list_of_list_of_types: list[list[str]]) -> list[dict[str, str]] | None:
    if len(list_of_list_of_types) == 1 and len(list_of_list_of_types[0]) == 0:
        return None
    return [
        {"description": _get_test_type_description(list_of_types), "test_types": " ".join(list_of_types)}
        for list_of_types in list_of_list_of_types
    ]


class SelectiveChecks:
    __HASHABLE_FIELDS = {"_files", "_default_branch", "_commit_ref", "_pr_labels", "_github_event"}

    def __init__(
        self,
        files: tuple[str, ...] = (),
        default_branch=AIRFLOW_BRANCH,
        default_constraints_branch=DEFAULT_AIRFLOW_CONSTRAINTS_BRANCH,
        commit_ref: str | None = None,
        pr_labels: tuple[str, ...] = (),
        github_event: GithubEvents = GithubEvents.PULL_REQUEST,
        github_repository: str = APACHE_AIRFLOW_GITHUB_REPOSITORY,
        github_actor: str = "",
        github_context_dict: dict[str, Any] | None = None,
    ):
        self._files = files
        self._default_branch = default_branch
        self._default_constraints_branch = default_constraints_branch
        self._commit_ref = commit_ref
        self._pr_labels = pr_labels
        self._github_event = github_event
        self._github_repository = github_repository
        self._github_actor = github_actor
        self._github_context_dict = github_context_dict or {}
        self._new_toml: dict[str, Any] = {}
        self._old_toml: dict[str, Any] = {}

    def __important_attributes(self) -> tuple[Any, ...]:
        return tuple(getattr(self, f) for f in self.__HASHABLE_FIELDS)

    def __hash__(self):
        return hash(self.__important_attributes())

    def __eq__(self, other):
        return isinstance(other, SelectiveChecks) and all(
            [getattr(other, f) == getattr(self, f) for f in self.__HASHABLE_FIELDS]
        )

    def __str__(self) -> str:
        from airflow_breeze.utils.github import get_ga_output

        output = []
        for field_name in dir(self):
            if not field_name.startswith("_"):
                value = getattr(self, field_name)
                if value is not None:
                    output.append(get_ga_output(field_name, value))
        return "\n".join(output)

    default_postgres_version = DEFAULT_POSTGRES_VERSION
    default_mysql_version = DEFAULT_MYSQL_VERSION

    default_kubernetes_version = DEFAULT_KUBERNETES_VERSION
    default_kind_version = KIND_VERSION
    default_helm_version = HELM_VERSION

    @cached_property
    def latest_versions_only(self) -> bool:
        return LATEST_VERSIONS_ONLY_LABEL in self._pr_labels

    @cached_property
    def default_python_version(self) -> str:
        return (
            CURRENT_PYTHON_MAJOR_MINOR_VERSIONS[-1]
            if LATEST_VERSIONS_ONLY_LABEL in self._pr_labels
            else DEFAULT_PYTHON_MAJOR_MINOR_VERSION
        )

    @cached_property
    def default_branch(self) -> str:
        return self._default_branch

    @cached_property
    def default_constraints_branch(self) -> str:
        return self._default_constraints_branch

    def _should_run_all_tests_and_versions(self) -> bool:
        if self._github_event in [GithubEvents.PUSH, GithubEvents.SCHEDULE, GithubEvents.WORKFLOW_DISPATCH]:
            get_console().print(f"[warning]Running everything because event is {self._github_event}[/]")
            return True
        if not self._commit_ref:
            get_console().print("[warning]Running everything in all versions as commit is missing[/]")
            return True
        if self.pyproject_toml_changed:
            get_console().print("[warning]Running everything with all versions: changed pyproject.toml[/]")
            return True
        if self.generated_dependencies_changed:
            get_console().print(
                "[warning]Running everything with all versions: provider dependencies changed[/]"
            )
            return True
        return False

    @cached_property
    def all_versions(self) -> bool:
        if DEFAULT_VERSIONS_ONLY_LABEL in self._pr_labels:
            return False
        if LATEST_VERSIONS_ONLY_LABEL in self._pr_labels:
            return False
        if ALL_VERSIONS_LABEL in self._pr_labels:
            return True
        if self._should_run_all_tests_and_versions():
            return True
        return False

    @cached_property
    def full_tests_needed(self) -> bool:
        if self._should_run_all_tests_and_versions():
            return True
        if self._matching_files(
            FileGroupForCi.ENVIRONMENT_FILES,
            CI_FILE_GROUP_MATCHES,
        ):
            get_console().print("[warning]Running full set of tests because env files changed[/]")
            return True
        if self._matching_files(
            FileGroupForCi.API_FILES,
            CI_FILE_GROUP_MATCHES,
        ):
            get_console().print("[warning]Running full set of tests because api files changed[/]")
            return True
        if self._matching_files(
            FileGroupForCi.GIT_PROVIDER_FILES,
            CI_FILE_GROUP_MATCHES,
        ):
            # TODO(potiuk): remove me when we get rid of the dependency
            get_console().print(
                "[warning]Running full set of tests because git provider files changed "
                "and for now we have core tests depending on them.[/]"
            )
            return True
        if self._matching_files(
            FileGroupForCi.STANDARD_PROVIDER_FILES,
            CI_FILE_GROUP_MATCHES,
        ):
            # TODO(potiuk): remove me when we get rid of the dependency
            get_console().print(
                "[warning]Running full set of tests because standard provider files changed "
                "and for now we have core tests depending on them.[/]"
            )
            return True
        if self._matching_files(
            FileGroupForCi.TESTS_UTILS_FILES,
            CI_FILE_GROUP_MATCHES,
        ):
            get_console().print("[warning]Running full set of tests because tests/utils changed[/]")
            return True
        if FULL_TESTS_NEEDED_LABEL in self._pr_labels:
            get_console().print(
                "[warning]Full tests needed because "
                f"label '{FULL_TESTS_NEEDED_LABEL}' is in  {self._pr_labels}[/]"
            )
            return True
        return False

    @cached_property
    def python_versions(self) -> list[str]:
        if self.all_versions:
            return CURRENT_PYTHON_MAJOR_MINOR_VERSIONS
        if self.latest_versions_only:
            return [CURRENT_PYTHON_MAJOR_MINOR_VERSIONS[-1]]
        return [DEFAULT_PYTHON_MAJOR_MINOR_VERSION]

    @cached_property
    def python_versions_list_as_string(self) -> str:
        return " ".join(self.python_versions)

    @cached_property
    def all_python_versions(self) -> list[str]:
        """
        All python versions include all past python versions available in previous branches
        Even if we remove them from the main version. This is needed to make sure we can cherry-pick
        changes from main to the previous branch.
        """
        if self.all_versions:
            return ALL_PYTHON_MAJOR_MINOR_VERSIONS
        if self.latest_versions_only:
            return [CURRENT_PYTHON_MAJOR_MINOR_VERSIONS[-1]]
        return [DEFAULT_PYTHON_MAJOR_MINOR_VERSION]

    @cached_property
    def all_python_versions_list_as_string(self) -> str:
        return " ".join(self.all_python_versions)

    @cached_property
    def postgres_versions(self) -> list[str]:
        if self.all_versions:
            return CURRENT_POSTGRES_VERSIONS
        if self.latest_versions_only:
            return [CURRENT_POSTGRES_VERSIONS[-1]]
        return [DEFAULT_POSTGRES_VERSION]

    @cached_property
    def mysql_versions(self) -> list[str]:
        if self.all_versions:
            return CURRENT_MYSQL_VERSIONS
        if self.latest_versions_only:
            return [CURRENT_MYSQL_VERSIONS[-1]]
        return [DEFAULT_MYSQL_VERSION]

    @cached_property
    def kind_version(self) -> str:
        return KIND_VERSION

    @cached_property
    def helm_version(self) -> str:
        return HELM_VERSION

    @cached_property
    def postgres_exclude(self) -> list[dict[str, str]]:
        if not self.all_versions:
            # Only basic combination so we do not need to exclude anything
            return []
        return [
            # Exclude all combinations that are repeating python/postgres versions
            {"python-version": python_version, "backend-version": postgres_version}
            for python_version, postgres_version in excluded_combos(
                CURRENT_PYTHON_MAJOR_MINOR_VERSIONS, CURRENT_POSTGRES_VERSIONS
            )
        ]

    @cached_property
    def mysql_exclude(self) -> list[dict[str, str]]:
        if not self.all_versions:
            # Only basic combination so we do not need to exclude anything
            return []
        return [
            # Exclude all combinations that are repeating python/mysql versions
            {"python-version": python_version, "backend-version": mysql_version}
            for python_version, mysql_version in excluded_combos(
                CURRENT_PYTHON_MAJOR_MINOR_VERSIONS, CURRENT_MYSQL_VERSIONS
            )
        ]

    @cached_property
    def sqlite_exclude(self) -> list[dict[str, str]]:
        return []

    @cached_property
    def kubernetes_versions(self) -> list[str]:
        if self.all_versions:
            return CURRENT_KUBERNETES_VERSIONS
        if self.latest_versions_only:
            return [CURRENT_KUBERNETES_VERSIONS[-1]]
        return [DEFAULT_KUBERNETES_VERSION]

    @cached_property
    def kubernetes_versions_list_as_string(self) -> str:
        return " ".join(self.kubernetes_versions)

    @cached_property
    def kubernetes_combos(self) -> list[str]:
        python_version_array: list[str] = self.python_versions_list_as_string.split(" ")
        kubernetes_version_array: list[str] = self.kubernetes_versions_list_as_string.split(" ")
        combo_titles, short_combo_titles, combos = get_kubernetes_python_combos(
            kubernetes_version_array, python_version_array
        )
        return short_combo_titles

    @cached_property
    def kubernetes_combos_list_as_string(self) -> str:
        return " ".join(self.kubernetes_combos)

    def _matching_files(self, match_group: FileGroupForCi, match_dict: HashableDict) -> list[str]:
        return _matching_files(self._files, match_group, match_dict)

    def _should_be_run(self, source_area: FileGroupForCi) -> bool:
        if self.full_tests_needed:
            get_console().print(f"[warning]{source_area} enabled because we are running everything[/]")
            return True
        matched_files = self._matching_files(source_area, CI_FILE_GROUP_MATCHES)
        if matched_files:
            get_console().print(
                f"[warning]{source_area} enabled because it matched {len(matched_files)} changed files[/]"
            )
            return True
        get_console().print(f"[warning]{source_area} disabled because it did not match any changed files[/]")
        return False

    @cached_property
    def mypy_checks(self) -> list[str]:
        checks_to_run: list[str] = []
        if (
            self._matching_files(FileGroupForCi.DEVEL_TOML_FILES, CI_FILE_GROUP_MATCHES)
            and self._default_branch == "main"
        ):
            return [
                "mypy-airflow-core",
                "mypy-providers",
                "mypy-dev",
                "mypy-task-sdk",
                "mypy-devel-common",
                "mypy-airflow-ctl",
            ]
        if (
            self._matching_files(FileGroupForCi.ALL_AIRFLOW_PYTHON_FILES, CI_FILE_GROUP_MATCHES)
            or self.full_tests_needed
        ):
            checks_to_run.append("mypy-airflow-core")
        if (
            self._matching_files(FileGroupForCi.ALL_PROVIDERS_PYTHON_FILES, CI_FILE_GROUP_MATCHES)
            or self._matching_files(
                FileGroupForCi.ALL_PROVIDERS_DISTRIBUTION_CONFIG_FILES, CI_FILE_GROUP_MATCHES
            )
            or self._are_all_providers_affected()
        ) and self._default_branch == "main":
            checks_to_run.append("mypy-providers")
        if (
            self._matching_files(FileGroupForCi.ALL_DEV_PYTHON_FILES, CI_FILE_GROUP_MATCHES)
            or self.full_tests_needed
        ):
            checks_to_run.append("mypy-dev")
        if (
            self._matching_files(FileGroupForCi.TASK_SDK_FILES, CI_FILE_GROUP_MATCHES)
            or self.full_tests_needed
        ):
            checks_to_run.append("mypy-task-sdk")
        if (
            self._matching_files(FileGroupForCi.ALL_DEVEL_COMMON_PYTHON_FILES, CI_FILE_GROUP_MATCHES)
            or self.full_tests_needed
        ):
            checks_to_run.append("mypy-devel-common")
        if (
            self._matching_files(FileGroupForCi.ALL_AIRFLOW_CTL_PYTHON_FILES, CI_FILE_GROUP_MATCHES)
            or self.full_tests_needed
        ):
            checks_to_run.append("mypy-airflow-ctl")
        return checks_to_run

    @cached_property
    def needs_mypy(self) -> bool:
        return self.mypy_checks != []

    @cached_property
    def needs_python_scans(self) -> bool:
        return self._should_be_run(FileGroupForCi.PYTHON_PRODUCTION_FILES)

    @cached_property
    def needs_javascript_scans(self) -> bool:
        return self._should_be_run(FileGroupForCi.JAVASCRIPT_PRODUCTION_FILES)

    @cached_property
    def needs_api_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.API_FILES)

    @cached_property
    def needs_ol_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.ASSET_FILES)

    @cached_property
    def needs_api_codegen(self) -> bool:
        return self._should_be_run(FileGroupForCi.API_CODEGEN_FILES)

    @cached_property
    def run_ui_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.UI_FILES)

    @cached_property
    def run_amazon_tests(self) -> bool:
        if self.providers_test_types_list_as_strings_in_json == "[]":
            return False
        return (
            "amazon" in self.providers_test_types_list_as_strings_in_json
            or "Providers" in self.providers_test_types_list_as_strings_in_json.split(" ")
        )

    @cached_property
    def run_task_sdk_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.TASK_SDK_FILES)

    @cached_property
    def run_go_sdk_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.GO_SDK_FILES)

    @cached_property
    def run_airflow_ctl_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.AIRFLOW_CTL_FILES)

    @cached_property
    def run_kubernetes_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.KUBERNETES_FILES)

    @cached_property
    def docs_build(self) -> bool:
        return self._should_be_run(FileGroupForCi.DOC_FILES)

    @cached_property
    def needs_helm_tests(self) -> bool:
        return self._should_be_run(FileGroupForCi.HELM_FILES) and self._default_branch == "main"

    @cached_property
    def run_tests(self) -> bool:
        if self.full_tests_needed:
            return True
        if self._is_canary_run():
            return True
        if self.only_new_ui_files:
            return False
        # we should run all test
        return self._should_be_run(FileGroupForCi.ALL_SOURCE_FILES)

    @cached_property
    def run_system_tests(self) -> bool:
        return self.run_tests

    @cached_property
    def only_pyproject_toml_files_changed(self) -> bool:
        return all(Path(file).name == "pyproject.toml" for file in self._files)

    @cached_property
    def ci_image_build(self) -> bool:
        # in case pyproject.toml changed, CI image should be built - even if no build dependencies
        # changes because some of our tests - those that need CI image might need to be run depending on
        # changed rules for static checks that are part of the pyproject.toml file
        return (
            self.run_tests
            or self.docs_build
            or self.run_kubernetes_tests
            or self.needs_helm_tests
            or self.run_ui_tests
            or self.pyproject_toml_changed
            or self.any_provider_yaml_or_pyproject_toml_changed
        )

    @cached_property
    def prod_image_build(self) -> bool:
        return self.run_kubernetes_tests or self.needs_helm_tests

    def _select_test_type_if_matching(
        self, test_types: set[str], test_type: SelectiveCoreTestType
    ) -> list[str]:
        matched_files = self._matching_files(test_type, TEST_TYPE_MATCHES)
        count = len(matched_files)
        if count > 0:
            test_types.add(test_type.value)
            get_console().print(f"[warning]{test_type} added because it matched {count} files[/]")
        return matched_files

    def _are_all_providers_affected(self) -> bool:
        # if "Providers" test is present in the list of tests, it means that we should run all providers tests
        # prepare all providers packages and build all providers documentation
        return "Providers" in self._get_providers_test_types_to_run()

    def _fail_if_suspended_providers_affected(self) -> bool:
        return "allow suspended provider changes" not in self._pr_labels

    def _get_core_test_types_to_run(self) -> list[str]:
        if self.full_tests_needed:
            return list(all_selective_core_test_types())

        candidate_test_types: set[str] = {"Always"}
        matched_files: set[str] = set()
        for test_type in SelectiveCoreTestType:
            if test_type not in [
                SelectiveCoreTestType.ALWAYS,
                SelectiveCoreTestType.CORE,
                SelectiveCoreTestType.OTHER,
            ]:
                matched_files.update(self._select_test_type_if_matching(candidate_test_types, test_type))

        kubernetes_files = self._matching_files(FileGroupForCi.KUBERNETES_FILES, CI_FILE_GROUP_MATCHES)
        system_test_files = self._matching_files(FileGroupForCi.SYSTEM_TEST_FILES, CI_FILE_GROUP_MATCHES)
        all_source_files = self._matching_files(FileGroupForCi.ALL_SOURCE_FILES, CI_FILE_GROUP_MATCHES)
        all_providers_source_files = self._matching_files(
            FileGroupForCi.ALL_PROVIDERS_PYTHON_FILES, CI_FILE_GROUP_MATCHES
        )
        all_providers_distribution_config_files = self._matching_files(
            FileGroupForCi.ALL_PROVIDERS_DISTRIBUTION_CONFIG_FILES, CI_FILE_GROUP_MATCHES
        )
        test_always_files = self._matching_files(FileGroupForCi.ALWAYS_TESTS_FILES, CI_FILE_GROUP_MATCHES)
        test_ui_files = self._matching_files(FileGroupForCi.UI_FILES, CI_FILE_GROUP_MATCHES)

        remaining_files = (
            set(all_source_files)
            - set(all_providers_source_files)
            - set(all_providers_distribution_config_files)
            - set(matched_files)
            - set(kubernetes_files)
            - set(system_test_files)
            - set(test_always_files)
            - set(test_ui_files)
        )
        get_console().print(f"[warning]Remaining non test/always files: {len(remaining_files)}[/]")
        count_remaining_files = len(remaining_files)

        for file in self._files:
            if file.endswith("bash.py") and Path(file).parent.name == "operators":
                candidate_test_types.add("Serialization")
                candidate_test_types.add("Core")
                break
        if count_remaining_files > 0:
            get_console().print(
                f"[warning]We should run all core tests except providers."
                f"There are {count_remaining_files} changed files that seems to fall "
                f"into Core/Other category[/]"
            )
            get_console().print(remaining_files)
            candidate_test_types.update(all_selective_core_test_types())
        else:
            get_console().print(
                "[warning]There are no core/other files. Only tests relevant to the changed files are run.[/]"
            )
        # sort according to predefined order
        sorted_candidate_test_types = sorted(candidate_test_types)
        get_console().print("[warning]Selected core test type candidates to run:[/]")
        get_console().print(sorted_candidate_test_types)
        return sorted_candidate_test_types

    def _get_providers_test_types_to_run(self, split_to_individual_providers: bool = False) -> list[str]:
        if self._default_branch != "main":
            return []
        if self.upgrade_to_newer_dependencies:
            return ["Providers"]
        if self.full_tests_needed or self.run_task_sdk_tests:
            if split_to_individual_providers:
                return list(providers_test_type())
            return ["Providers"]
        all_providers_source_files = self._matching_files(
            FileGroupForCi.ALL_PROVIDERS_PYTHON_FILES, CI_FILE_GROUP_MATCHES
        )
        all_providers_distribution_config_files = self._matching_files(
            FileGroupForCi.ALL_PROVIDERS_DISTRIBUTION_CONFIG_FILES, CI_FILE_GROUP_MATCHES
        )
        assets_source_files = self._matching_files(FileGroupForCi.ASSET_FILES, CI_FILE_GROUP_MATCHES)
        if (
            len(all_providers_source_files) == 0
            and len(all_providers_distribution_config_files) == 0
            and len(assets_source_files) == 0
            and not self.needs_api_tests
        ):
            # IF API tests are needed, that will trigger extra provider checks
            return []
        affected_providers = self._find_all_providers_affected(
            include_docs=False,
        )
        candidate_test_types: set[str] = set()
        if isinstance(affected_providers, AllProvidersSentinel):
            if split_to_individual_providers:
                for provider in get_available_distributions():
                    candidate_test_types.add(f"Providers[{provider}]")
            else:
                candidate_test_types.add("Providers")
        elif affected_providers:
            if split_to_individual_providers:
                for provider in affected_providers:
                    candidate_test_types.add(f"Providers[{provider}]")
            else:
                candidate_test_types.add(f"Providers[{','.join(sorted(affected_providers))}]")
        sorted_candidate_test_types = sorted(candidate_test_types)
        get_console().print("[warning]Selected providers test type candidates to run:[/]")
        get_console().print(sorted_candidate_test_types)
        return sorted_candidate_test_types

    @staticmethod
    def _extract_long_provider_tests(current_test_types: set[str]):
        """
        In case there are Provider tests in the list of test to run - either in the form of
        Providers or Providers[...] we subtract them from the test type,
        and add them to the list of tests to run individually.

        In case of Providers, we need to replace it with Providers[-<list_of_long_tests>], but
        in case of Providers[list_of_tests] we need to remove the long tests from the list.

        In case of celery tests we want to isolate them from the rest, because they seem to be hanging
        infrequently when running together with other tests

        :param current_test_types: The set of test types to run
        """
        long_tests = ["amazon", "celery", "google", "standard"]
        for original_test_type in tuple(current_test_types):
            if original_test_type == "Providers":
                current_test_types.remove(original_test_type)
                for long_test in long_tests:
                    current_test_types.add(f"Providers[{long_test}]")
                current_test_types.add(f"Providers[-{','.join(long_tests)}]")
            elif original_test_type.startswith("Providers["):
                provider_tests_to_run = (
                    original_test_type.replace("Providers[", "").replace("]", "").split(",")
                )
                if any(long_test in provider_tests_to_run for long_test in long_tests):
                    current_test_types.remove(original_test_type)
                    for long_test in long_tests:
                        if long_test in provider_tests_to_run:
                            current_test_types.add(f"Providers[{long_test}]")
                            provider_tests_to_run.remove(long_test)
                    current_test_types.add(f"Providers[{','.join(provider_tests_to_run)}]")

    @cached_property
    def core_test_types_list_as_strings_in_json(self) -> str | None:
        if not self.run_tests:
            return None
        current_test_types = sorted(set(self._get_core_test_types_to_run()))
        return json.dumps(_get_test_list_as_json([current_test_types]))

    @cached_property
    def providers_test_types_list_as_strings_in_json(self) -> str:
        if not self.run_tests:
            return "[]"
        current_test_types = set(self._get_providers_test_types_to_run())
        if self._default_branch != "main":
            test_types_to_remove: set[str] = set()
            for test_type in current_test_types:
                if test_type.startswith("Providers"):
                    get_console().print(
                        f"[warning]Removing {test_type} because the target branch "
                        f"is {self._default_branch} and not main[/]"
                    )
                    test_types_to_remove.add(test_type)
            current_test_types = current_test_types - test_types_to_remove
        self._extract_long_provider_tests(current_test_types)
        return json.dumps(_get_test_list_as_json([sorted(current_test_types)]))

    def _get_individual_providers_list(self):
        current_test_types = set(self._get_providers_test_types_to_run(split_to_individual_providers=True))
        if "Providers" in current_test_types:
            current_test_types.remove("Providers")
            current_test_types.update(
                {f"Providers[{provider}]" for provider in get_available_distributions(include_not_ready=True)}
            )
        return current_test_types

    @cached_property
    def individual_providers_test_types_list_as_strings_in_json(self) -> str | None:
        """Splits the list of test types into several lists of strings (to run them in parallel)."""
        if not self.run_tests:
            return None
        current_test_types = sorted(self._get_individual_providers_list())
        if not current_test_types:
            return None
        # We are hard-coding the number of lists as reasonable starting point to split the
        # list of test types - and we can modify it in the future
        # TODO: In Python 3.12 we will be able to use itertools.batched
        if len(current_test_types) < NUMBER_OF_LOW_DEP_SLICES:
            return json.dumps(_get_test_list_as_json([current_test_types]))
        list_of_list_of_types = _split_list(current_test_types, NUMBER_OF_LOW_DEP_SLICES)
        return json.dumps(_get_test_list_as_json(list_of_list_of_types))

    @cached_property
    def include_success_outputs(
        self,
    ) -> bool:
        return INCLUDE_SUCCESS_OUTPUTS_LABEL in self._pr_labels

    @cached_property
    def basic_checks_only(self) -> bool:
        return not self.ci_image_build

    @staticmethod
    def _print_diff(old_lines: list[str], new_lines: list[str]):
        diff = "\n".join(line for line in difflib.ndiff(old_lines, new_lines) if line and line[0] in "+-?")
        get_console().print(diff)

    @cached_property
    def generated_dependencies_changed(self) -> bool:
        return "generated/provider_dependencies.json" in self._files

    @cached_property
    def any_provider_yaml_or_pyproject_toml_changed(self) -> bool:
        if not self._commit_ref:
            get_console().print("[warning]Cannot determine changes as commit is missing[/]")
            return False
        for file in self._files:
            path_file = Path(file)
            if path_file.name == "provider.yaml" or path_file.name == "pyproject.toml":
                return True
        return False

    @cached_property
    def pyproject_toml_changed(self) -> bool:
        if not self._commit_ref:
            get_console().print("[warning]Cannot determine pyproject.toml changes as commit is missing[/]")
            return False
        if "pyproject.toml" not in self._files:
            return False
        new_result = run_command(
            ["git", "show", f"{self._commit_ref}:pyproject.toml"],
            capture_output=True,
            text=True,
            cwd=AIRFLOW_ROOT_PATH,
            check=False,
        )
        if new_result.returncode != 0:
            get_console().print(
                f"[warning]Cannot determine pyproject.toml changes. "
                f"Could not get pyproject.toml from {self._commit_ref}[/]"
            )
            return False
        old_result = run_command(
            ["git", "show", f"{self._commit_ref}^:pyproject.toml"],
            capture_output=True,
            text=True,
            cwd=AIRFLOW_ROOT_PATH,
            check=False,
        )
        if old_result.returncode != 0:
            get_console().print(
                f"[warning]Cannot determine pyproject.toml changes. "
                f"Could not get pyproject.toml from {self._commit_ref}^[/]"
            )
            return False
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        self._new_toml = tomllib.loads(new_result.stdout)
        self._old_toml = tomllib.loads(old_result.stdout)
        return True

    @cached_property
    def upgrade_to_newer_dependencies(self) -> bool:
        if len(self._matching_files(FileGroupForCi.ALL_PYPROJECT_TOML_FILES, CI_FILE_GROUP_MATCHES)) > 0:
            get_console().print("[warning]Upgrade to newer dependencies: Dependency files changed[/]")
            return True
        if self._github_event in [GithubEvents.PUSH, GithubEvents.SCHEDULE]:
            get_console().print("[warning]Upgrade to newer dependencies: Push or Schedule event[/]")
            return True
        if UPGRADE_TO_NEWER_DEPENDENCIES_LABEL in self._pr_labels:
            get_console().print(
                f"[warning]Upgrade to newer dependencies: Label '{UPGRADE_TO_NEWER_DEPENDENCIES_LABEL}' "
                f"in {self._pr_labels}[/]"
            )
            return True
        return False

    @cached_property
    def docs_list_as_string(self) -> str | None:
        _ALL_DOCS_LIST = ""
        if not self.docs_build:
            return None
        if self._default_branch != "main":
            return "apache-airflow docker-stack"
        if self.full_tests_needed:
            return _ALL_DOCS_LIST
        providers_affected = self._find_all_providers_affected(
            include_docs=True,
        )
        if (
            isinstance(providers_affected, AllProvidersSentinel)
            or "docs/conf.py" in self._files
            or "docs/build_docs.py" in self._files
            or self._are_all_providers_affected()
        ):
            return _ALL_DOCS_LIST
        packages = []
        if any(file.startswith(("airflow-core/src/airflow/", "airflow-core/docs/")) for file in self._files):
            packages.append("apache-airflow")
        if any(file.startswith("providers-summary-docs/") for file in self._files):
            packages.append("apache-airflow-providers")
        if any(file.startswith("chart/") for file in self._files):
            packages.append("helm-chart")
        if any(file.startswith("docker-stack-docs") for file in self._files):
            packages.append("docker-stack")
        if any(file.startswith("task-sdk/src/") for file in self._files):
            packages.append("task-sdk")
        if providers_affected:
            for provider in providers_affected:
                packages.append(provider.replace("-", "."))
        return " ".join(packages)

    @cached_property
    def skip_pre_commits(self) -> str:
        pre_commits_to_skip = set()
        pre_commits_to_skip.add("identity")
        # Skip all mypy "individual" file checks if we are running mypy checks in CI
        # In the CI we always run mypy for the whole "package" rather than for `--all-files` because
        # The pre-commit will semi-randomly skip such list of files into several groups and we want
        # to make sure that such checks are always run in CI for whole "group" of files - i.e.
        # whole package rather than for individual files. That's why we skip those checks in CI
        # and run them via `mypy-all` command instead and dedicated CI job in matrix
        # This will also speed up static-checks job usually as the jobs will be running in parallel
        pre_commits_to_skip.update(
            {
                "mypy-providers",
                "mypy-airflow-core",
                "mypy-dev",
                "mypy-task-sdk",
                "mypy-airflow-ctl",
                "mypy-devel-common",
            }
        )
        if self._default_branch != "main":
            # Skip those tests on all "release" branches
            pre_commits_to_skip.update(
                (
                    "compile-fab-assets",
                    "generate-openapi-spec-fab",
                    "check-airflow-providers-bug-report-template",
                    "check-airflow-provider-compatibility",
                    "check-extra-packages-references",
                    "check-provider-yaml-valid",
                    "lint-helm-chart",
                    "validate-operators-init",
                )
            )

        if self.full_tests_needed:
            # when full tests are needed, we do not want to skip any checks and we should
            # run all the pre-commits just to be sure everything is ok when some structural changes occurred
            return ",".join(sorted(pre_commits_to_skip))
        if not (
            self._matching_files(FileGroupForCi.UI_FILES, CI_FILE_GROUP_MATCHES)
            or self._matching_files(FileGroupForCi.API_CODEGEN_FILES, CI_FILE_GROUP_MATCHES)
        ):
            pre_commits_to_skip.add("ts-compile-lint-ui")
            pre_commits_to_skip.add("ts-compile-lint-simple-auth-manager-ui")
        if not self._matching_files(FileGroupForCi.ALL_PYTHON_FILES, CI_FILE_GROUP_MATCHES):
            pre_commits_to_skip.add("flynt")
        if not self._matching_files(
            FileGroupForCi.HELM_FILES,
            CI_FILE_GROUP_MATCHES,
        ):
            pre_commits_to_skip.add("lint-helm-chart")
        if not (
            self._matching_files(
                FileGroupForCi.ALL_PROVIDERS_DISTRIBUTION_CONFIG_FILES, CI_FILE_GROUP_MATCHES
            )
            or self._matching_files(FileGroupForCi.ALL_PROVIDERS_PYTHON_FILES, CI_FILE_GROUP_MATCHES)
        ):
            # only skip provider validation if none of the provider.yaml and provider
            # python files changed because validation also walks through all the provider python files
            pre_commits_to_skip.add("check-provider-yaml-valid")
        return ",".join(sorted(pre_commits_to_skip))

    @cached_property
    def skip_providers_tests(self) -> bool:
        if self._default_branch != "main":
            return True
        if self.full_tests_needed:
            return False
        if self._get_providers_test_types_to_run():
            return False
        if not self.run_tests:
            return True
        return True

    @cached_property
    def docker_cache(self) -> str:
        return "disabled" if DISABLE_IMAGE_CACHE_LABEL in self._pr_labels else "registry"

    @cached_property
    def debug_resources(self) -> bool:
        return DEBUG_CI_RESOURCES_LABEL in self._pr_labels

    @cached_property
    def disable_airflow_repo_cache(self) -> bool:
        return self.docker_cache == "disabled"

    @cached_property
    def helm_test_packages(self) -> str:
        return json.dumps(all_helm_test_packages())

    @cached_property
    def selected_providers_list_as_string(self) -> str | None:
        if self._default_branch != "main":
            return None
        if self.full_tests_needed:
            return ""
        if self._are_all_providers_affected():
            return ""
        affected_providers = self._find_all_providers_affected(include_docs=True)
        if not affected_providers:
            return None
        if isinstance(affected_providers, AllProvidersSentinel):
            return ""
        return " ".join(sorted(affected_providers))

    @cached_property
    def amd_runners(self) -> str:
        return PUBLIC_AMD_RUNNERS

    @cached_property
    def arm_runners(self) -> str:
        return PUBLIC_ARM_RUNNERS

    @cached_property
    def has_migrations(self) -> bool:
        return any([file.startswith("airflow-core/src/airflow/migrations/") for file in self._files])

    @cached_property
    def providers_compatibility_tests_matrix(self) -> str:
        """Provider compatibility input matrix for the current run. Filter out python versions not built"""
        return json.dumps(
            [
                check
                for check in PROVIDERS_COMPATIBILITY_TESTS_MATRIX
                if check["python-version"] in self.python_versions
            ]
        )

    @cached_property
    def excluded_providers_as_string(self) -> str:
        providers_to_exclude = defaultdict(list)
        for provider, provider_info in DEPENDENCIES.items():
            if "excluded-python-versions" in provider_info:
                for python_version in provider_info["excluded-python-versions"]:
                    providers_to_exclude[python_version].append(provider)
        sorted_providers_to_exclude = dict(
            sorted(providers_to_exclude.items(), key=lambda item: int(item[0].split(".")[1]))
        )  # ^ sort by Python minor version
        return json.dumps(sorted_providers_to_exclude)

    @cached_property
    def only_new_ui_files(self) -> bool:
        all_source_files = set(self._matching_files(FileGroupForCi.ALL_SOURCE_FILES, CI_FILE_GROUP_MATCHES))
        new_ui_source_files = set(self._matching_files(FileGroupForCi.UI_FILES, CI_FILE_GROUP_MATCHES))
        remaining_files = all_source_files - new_ui_source_files

        if all_source_files and new_ui_source_files and not remaining_files:
            return True
        return False

    @cached_property
    def testable_core_integrations(self) -> list[str]:
        if not self.run_tests:
            return []
        return [
            integration
            for integration in TESTABLE_CORE_INTEGRATIONS
            if integration not in DISABLE_TESTABLE_INTEGRATIONS_FROM_CI
        ]

    @cached_property
    def testable_providers_integrations(self) -> list[str]:
        if not self.run_tests:
            return []
        return [
            integration
            for integration in TESTABLE_PROVIDERS_INTEGRATIONS
            if integration not in DISABLE_TESTABLE_INTEGRATIONS_FROM_CI
        ]

    @cached_property
    def is_committer_build(self):
        if NON_COMMITTER_BUILD_LABEL in self._pr_labels:
            return False
        return self._github_actor in COMMITTERS

    def _find_all_providers_affected(self, include_docs: bool) -> list[str] | AllProvidersSentinel | None:
        affected_providers: set[str] = set()

        all_providers_affected = False
        suspended_providers: set[str] = set()
        for changed_file in self._files:
            provider = find_provider_affected(changed_file, include_docs=include_docs)
            if provider == "Providers":
                all_providers_affected = True
            elif provider is not None:
                if provider not in DEPENDENCIES:
                    suspended_providers.add(provider)
                else:
                    affected_providers.add(provider)
        if self.needs_api_tests:
            affected_providers.add("fab")
        if self.needs_ol_tests:
            affected_providers.add("openlineage")
        if all_providers_affected:
            return ALL_PROVIDERS_SENTINEL
        if suspended_providers:
            # We check for suspended providers only after we have checked if all providers are affected.
            # No matter if we found that we are modifying a suspended provider individually,
            # if all providers are
            # affected, then it means that we are ok to proceed because likely we are running some kind of
            # global refactoring that affects multiple providers including the suspended one. This is a
            # potential escape hatch if someone would like to modify suspended provider,
            # but it can be found at the review time and is anyway harmless as the provider will not be
            # released nor tested nor used in CI anyway.
            get_console().print("[yellow]You are modifying suspended providers.\n")
            get_console().print(
                "[info]Some providers modified by this change have been suspended, "
                "and before attempting such changes you should fix the reason for suspension."
            )
            get_console().print(
                "[info]When fixing it, you should set suspended = false in provider.yaml "
                "to make changes to the provider."
            )
            get_console().print(f"Suspended providers: {suspended_providers}")
            if self._fail_if_suspended_providers_affected():
                get_console().print(
                    "[error]This PR did not have `allow suspended provider changes`"
                    " label set so it will fail."
                )
                sys.exit(1)
            else:
                get_console().print(
                    "[info]This PR had `allow suspended provider changes` label set so it will continue"
                )
        if not affected_providers:
            return None

        for provider in list(affected_providers):
            affected_providers.update(
                get_related_providers(provider, upstream_dependencies=True, downstream_dependencies=True)
            )
        return sorted(affected_providers)

    def _is_canary_run(self):
        return (
            self._github_event in [GithubEvents.SCHEDULE, GithubEvents.PUSH, GithubEvents.WORKFLOW_DISPATCH]
            and self._github_repository == APACHE_AIRFLOW_GITHUB_REPOSITORY
        ) or CANARY_LABEL in self._pr_labels

    @classmethod
    def _find_caplog_in_def(cls, added_lines):
        """
        Find caplog in def

        :param added_lines: lines added in the file
        :return: True if caplog is found in def else False
        """
        line_counter = 0
        while line_counter < len(added_lines):
            if all(keyword in added_lines[line_counter] for keyword in ["def", "caplog", "(", ")"]):
                return True
            if "def" in added_lines[line_counter] and ")" not in added_lines[line_counter]:
                while line_counter < len(added_lines) and ")" not in added_lines[line_counter]:
                    if "caplog" in added_lines[line_counter]:
                        return True
                    line_counter += 1
            line_counter += 1
        return None

    def _caplog_exists_in_added_lines(self) -> bool:
        """
        Check if caplog is used in added lines

        :return: True if caplog is used in added lines else False
        """
        lines = run_command(
            ["git", "diff", f"{self._commit_ref}^"],
            capture_output=True,
            text=True,
            cwd=AIRFLOW_ROOT_PATH,
            check=False,
        )

        if "caplog" not in lines.stdout or lines.stdout == "":
            return False

        added_caplog_lines = [
            line.lstrip().lstrip("+") for line in lines.stdout.split("\n") if line.lstrip().startswith("+")
        ]
        return self._find_caplog_in_def(added_lines=added_caplog_lines)

    @cached_property
    def is_log_mocked_in_the_tests(self) -> bool:
        """
        Check if log is used without mock in the tests
        """
        if self._is_canary_run() or self._github_event not in (
            GithubEvents.PULL_REQUEST,
            GithubEvents.PULL_REQUEST_TARGET,
        ):
            return False
        # Check if changed files are unit tests
        if (
            self._matching_files(FileGroupForCi.UNIT_TEST_FILES, CI_FILE_GROUP_MATCHES)
            and LOG_WITHOUT_MOCK_IN_TESTS_EXCEPTION_LABEL not in self._pr_labels
        ):
            if self._caplog_exists_in_added_lines():
                get_console().print(
                    f"[error]Caplog is used in the test. "
                    f"Please be sure you are mocking the log. "
                    "For example, use `patch.object` to mock `log`."
                    "Using `caplog` directly in your test files can cause side effects "
                    "and not recommended. If you think, `caplog` is the only way to test "
                    "the functionality or there is and exceptional case, "
                    "please ask maintainer to include as an exception using "
                    f"'{LOG_WITHOUT_MOCK_IN_TESTS_EXCEPTION_LABEL}' label. "
                    "It is up to maintainer decision to allow this exception.",
                )
                sys.exit(1)
            else:
                return True
        return True

    @cached_property
    def force_pip(self):
        return FORCE_PIP_LABEL in self._pr_labels
