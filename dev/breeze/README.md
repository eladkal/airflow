<!--
 Licensed to the Apache Software Foundation (ASF) under one
 or more contributor license agreements.  See the NOTICE file
 distributed with this work for additional information
 regarding copyright ownership.  The ASF licenses this file
 to you under the Apache License, Version 2.0 (the
 "License"); you may not use this file except in compliance
 with the License.  You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing,
 software distributed under the License is distributed on an
 "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 KIND, either express or implied.  See the License for the
 specific language governing permissions and limitations
 under the License.
 -->

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Apache Airflow Breeze](#apache-airflow-breeze)
- [Setting up development env for Breeze](#setting-up-development-env-for-breeze)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

Apache Airflow Breeze
------------------------

The project is part of [Apache Airflow](https://airflow.apache.org) - it's a development environment
that is used by Airflow developers to effortlessly setup and maintain consistent development environment
for Airflow Development.

This package should never be installed in "production" mode. The `breeze` entrypoint will actually
fail if you do so. It is supposed to be installed only in [editable/development mode](https://packaging.python.org/en/latest/guides/distributing-packages-using-setuptools/#working-in-development-mode)
directly from Airflow sources using `uv tool` or `pipx` - usually with `--force` flag to account
for re-installation  that might often be needed if dependencies change during development.

```shell
uv tool install -e ./dev/breeze --force
```

or

```shell
pipx install -e ./dev/breeze --force
```

You can read more about Breeze in the [documentation](https://github.com/apache/airflow/blob/main/dev/breeze/doc/README.rst)

This README file contains automatically generated hash of the `pyproject.toml` files that were
available when the package was installed. Since this file becomes part of the installed package, it helps
to detect automatically if any of the files have changed. If they did, the user will be warned to upgrade
their installations.

Setting up development env for Breeze
-------------------------------------

> [!NOTE]
> This section is for developers of Breeze. If you are a user of Breeze, you do not need to read this section.

Breeze is actively developed by Airflow maintainers and contributors, Airflow is an active project
and we are in the process of developing Airflow 3, so breeze requires a lot of adjustments to keep up
the dev environment in sync with Airflow 3 development - this is also why it is part of the same
repository as Airflow - because it needs to be closely synchronized with Airflow development.

As of November 2024 Airflow switchd to using `uv` as the recommended development environment for Airflow
and for Breeze. So the instructions below are for setting up the development environment for Breeze
using `uv`.

However we are using only standard python packaging tools, so you can still use `pip` or
`pipenv` or other build frontends to install Breeze, but we recommend using `uv` as it is the most
convenient way to install, manage python packages and virtual environments.

Unlike in Airflow, where we manage our own constraints, we use `uv` to manage requirements for Breeze
and we use `uv` to lock the dependencies. This way we can ensure that the dependencies are always
up-to-date and that the development environment is always consistent for different people. This is
why Breeze's `uv.lock` is committed to the repository and is used to install the dependencies by
default by Breeze. Here's how to install breeze development environment with `uv`:


1. Install `uv` - see [uv documentation](https://docs.astral.sh/uv/getting-started/installation/)

> [!IMPORTANT]
>
> 1. The version of `uv` should be at least as defined in `pyproject.toml` under `[tool.uv]` section,
>    otherwise some breeze commands might malfunction (but you will get error from `uv` about it).
> 2. All the commands below should be executed while you are in `dev/breeze` directory of the Airflow repository.
>

2. Create a new virtual environment for Breeze development (this step can be skipped, uv sync will create
   venv as needed when running ``uv sync``)

```shell
uv venv
```

3. Synchronize Breeze dependencies with `uv` to the latest dependencies stored in uv.lock file:

```shell
uv sync
```

After syncing, the `.venv` directory in breeze folder will contain the virtual environment with all the dependencies
installed - you can use that environment to develop Breeze - for example with your favourite IDE
or text editor, you can also use `uv run` to run the scripts in the virtual environment.

For example to run all tests in the virtual environment you can use:

```shell
uv run pytest
```

4. Add/remove dependencies with `uv`:

```shell
uv add <package>
uv remove <package>
```

5. Update and lock the dependencies (after adding them or periodically to keep them up-to-date):

```shell
uv lock
```

Note that when you update dependencies/lock them you should commit the changes in `pyproject.toml` and `uv.lock`.

See [uv documentation](https://docs.astral.sh/uv/getting-started/) for more details on using `uv`.


PLEASE DO NOT MODIFY THE HASH BELOW! IT IS AUTOMATICALLY UPDATED BY PRE-COMMIT.

---------------------------------------------------------------------------------------------------------

Package config hash: ca0dde3dc813baf255b7f645c73025b9ce0729d24acf33499f15ba62a6d94cb6e665fa84c575fc88b5a20f3a1c73ded735ff2dcfad353d2b91434b03e11a4467

---------------------------------------------------------------------------------------------------------
