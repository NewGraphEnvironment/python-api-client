import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mergin.cli import push
from mergin.common import ClientError, MAX_UPLOAD_CHANGES
from mergin.merginproject import MerginProject

PROJECT_METADATA = {
    "name": "test_project",
    "namespace": "test_workspace",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "version": "v1",
    "files": [],
    "role": "owner",
}


def _create_local_project(directory, files_count, updated_count=0):
    """Create a local project with files_count new local files and updated_count files that
    are on the server but modified locally."""
    MerginProject.write_metadata(str(directory), PROJECT_METADATA)
    for i in range(updated_count):
        (directory / f"server_{i}.txt").write_text(f"server content {i}")
    mp = MerginProject(str(directory))
    server_files = [{"path": f["path"], "checksum": f["checksum"], "size": f["size"]} for f in mp.inspect_files()]
    mp.update_metadata({**PROJECT_METADATA, "files": server_files})
    for i in range(updated_count):
        (directory / f"server_{i}.txt").write_text(f"local content {i}")
    for i in range(files_count):
        (directory / f"file_{i}.txt").write_text(f"content {i}")


def _finalize_first_batch(job):
    """Mock of push_project_finalize: mark only the first MAX_UPLOAD_CHANGES new files as uploaded,
    as push does when there are more than MAX_UPLOAD_CHANGES added files."""
    mp = MerginProject(os.getcwd())
    new_files = sorted((f for f in mp.inspect_files() if f["path"].startswith("file_")), key=lambda f: f["path"])
    uploaded = [{"path": f["path"], "checksum": f["checksum"], "size": f["size"]} for f in new_files]
    mp.update_metadata({**PROJECT_METADATA, "version": "v2", "files": mp.files() + uploaded[:MAX_UPLOAD_CHANGES]})


@pytest.mark.parametrize(
    "files_count, updated_count, expected_remaining",
    [
        (MAX_UPLOAD_CHANGES + 50, 0, 50),
        (MAX_UPLOAD_CHANGES, 0, 0),
        # more than MAX_UPLOAD_CHANGES left, including updated files that push left out of the batch
        (MAX_UPLOAD_CHANGES * 2, 10, MAX_UPLOAD_CHANGES + 10),
    ],
)
def test_push_reports_remaining_changes(tmp_path, monkeypatch, files_count, updated_count, expected_remaining):
    _create_local_project(tmp_path, files_count, updated_count)
    monkeypatch.chdir(tmp_path)
    mc = MagicMock()
    mc.has_editor_support.return_value = False
    job = SimpleNamespace(total_size=0, transferred_size=0)

    with (
        patch("mergin.cli.push_project_async", return_value=job),
        patch("mergin.cli.push_project_is_running", return_value=False),
        patch("mergin.cli.push_project_finalize", side_effect=_finalize_first_batch),
    ):
        result = CliRunner().invoke(push, obj={"client": mc})

    assert result.exit_code == 0
    if expected_remaining:
        assert (
            f"Done, but {expected_remaining} local changes were not uploaded "
            f"(push sends at most {MAX_UPLOAD_CHANGES} changes at a time). "
            "Run 'mergin sync' to upload them all, or push again."
        ) in result.output
    else:
        assert result.output.strip().endswith("Done")
        assert "not uploaded" not in result.output


@pytest.mark.parametrize("error", [KeyboardInterrupt(), FileNotFoundError("gone.txt"), ClientError("count failed")])
def test_push_reports_done_when_remaining_check_fails(tmp_path, monkeypatch, error):
    _create_local_project(tmp_path, 1)
    monkeypatch.chdir(tmp_path)
    job = SimpleNamespace(total_size=0, transferred_size=0)

    with (
        patch("mergin.cli.push_project_async", return_value=job),
        patch("mergin.cli.push_project_is_running", return_value=False),
        patch("mergin.cli.push_project_finalize"),
        patch("mergin.cli.get_push_changes_batch", side_effect=error),
        patch("mergin.cli.push_project_cancel") as cancel,
    ):
        result = CliRunner().invoke(push, obj={"client": MagicMock()})

    assert result.exit_code == 0
    assert result.output.strip().endswith("Done")
    for text in ("Cancelling", "Error", "Unhandled exception"):
        assert text not in result.output
    cancel.assert_not_called()
