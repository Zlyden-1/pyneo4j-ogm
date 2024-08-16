# pylint: disable=unused-argument, unused-import, redefined-outer-name, protected-access, missing-module-docstring, missing-class-docstring
# pyright: reportGeneralTypeIssues=false

import os

import pytest

from pyneo4j_ogm.exceptions import MigrationNotInitializedError
from pyneo4j_ogm.migrations import create
from pyneo4j_ogm.migrations.actions.create import normalize_filename
from pyneo4j_ogm.migrations.utils.defaults import (
    DEFAULT_MIGRATION_DIR,
    MIGRATION_TEMPLATE,
)
from tests.fixtures.migrations import (
    CUSTOM_CONFIG_FILENAME,
    CUSTOM_MIGRATION_DIR,
    initialized_migration,
    initialized_migration_with_custom_path,
    tmp_cwd,
)


def test_create(initialized_migration):
    file_name = "testmigration"
    assert len(os.listdir(os.path.join(initialized_migration, DEFAULT_MIGRATION_DIR))) == 1

    return_value = create(file_name)
    assert "name" in return_value
    assert "path" in return_value
    assert len(os.listdir(os.path.join(initialized_migration, DEFAULT_MIGRATION_DIR))) == 2

    file = [
        file for file in os.listdir(os.path.join(initialized_migration, DEFAULT_MIGRATION_DIR)) if file.endswith(".py")
    ][0]
    assert file_name in file

    with open(os.path.join(initialized_migration, DEFAULT_MIGRATION_DIR, file), "r", encoding="utf-8") as f:
        assert f.read() == MIGRATION_TEMPLATE.format(name=file)


def test_fails_if_not_initialized(tmp_cwd):
    with pytest.raises(MigrationNotInitializedError):
        create("testmigration")


def test_with_custom_path(initialized_migration_with_custom_path):
    file_name = "testmigration"
    assert len(os.listdir(os.path.join(initialized_migration_with_custom_path, CUSTOM_MIGRATION_DIR))) == 1

    return_value = create(file_name, os.path.join(initialized_migration_with_custom_path, CUSTOM_CONFIG_FILENAME))
    assert "name" in return_value
    assert "path" in return_value
    assert len(os.listdir(os.path.join(initialized_migration_with_custom_path, CUSTOM_MIGRATION_DIR))) == 2

    file = [
        file
        for file in os.listdir(os.path.join(initialized_migration_with_custom_path, CUSTOM_MIGRATION_DIR))
        if file.endswith(".py")
    ][0]
    assert file_name in file

    with open(
        os.path.join(initialized_migration_with_custom_path, CUSTOM_MIGRATION_DIR, file), "r", encoding="utf-8"
    ) as f:
        assert f.read() == MIGRATION_TEMPLATE.format(name=file)


def test_normalizes_filename():
    assert normalize_filename("TestMigration") == "test_migration"
    assert normalize_filename("testMigration") == "test_migration"
    assert normalize_filename("test_migration") == "test_migration"
    assert normalize_filename("test-migration") == "test_migration"
