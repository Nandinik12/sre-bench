import pathlib

from chaos.failures import (
    FAILURE_MODES,
    GOOD_INVENTORY_CONFIG,
    INVENTORY_CONFIG_PATH,
    POISONED_INVENTORY_CONFIG,
)
from chaos.inject import execute, main


def test_all_four_modes_exist():
    assert set(FAILURE_MODES) == {"dead-dependency", "bad-deploy", "filled-disk", "poisoned-config"}


def test_every_mode_has_break_and_restore():
    for m in FAILURE_MODES.values():
        assert m.break_steps and m.restore_steps
        for step in list(m.break_steps) + list(m.restore_steps):
            assert step[0] in ("run", "write_file")


def test_bad_deploy_toggles_env_and_redeploys():
    m = FAILURE_MODES["bad-deploy"]
    assert ("write_file", ".env", "PAYMENTS_BROKEN=1\n") in m.break_steps
    assert ("write_file", ".env", "PAYMENTS_BROKEN=0\n") in m.restore_steps
    redeploys = [s for s in m.break_steps if s[0] == "run"]
    assert any("payments" in s[1] for s in redeploys)


def test_poisoned_config_restore_writes_good_config():
    m = FAILURE_MODES["poisoned-config"]
    assert m.break_steps == [("write_file", INVENTORY_CONFIG_PATH, POISONED_INVENTORY_CONFIG)]
    assert m.restore_steps == [("write_file", INVENTORY_CONFIG_PATH, GOOD_INVENTORY_CONFIG)]


def test_filled_disk_only_touches_junk_file():
    m = FAILURE_MODES["filled-disk"]
    all_cmds = " ".join(" ".join(s[1]) for s in m.break_steps + m.restore_steps if s[0] == "run")
    assert "core.20260712.dump" in all_cmds
    assert "reservations" not in all_cmds
    # must actually hit ENOSPC on the 32MB tmpfs: dd count must exceed volume
    dd = next(" ".join(s[1]) for s in m.break_steps if s[0] == "run")
    assert "count=64" in dd and "|| true" in dd


def test_execute_dry_run_touches_nothing(tmp_path):
    steps = [("write_file", "x.txt", "hello"), ("run", ["echo", "hi"])]
    log = execute(steps, root=tmp_path, dry_run=True)
    assert log == [{"write_file": "x.txt"}, {"run": ["echo", "hi"]}]
    assert not (tmp_path / "x.txt").exists()


def test_execute_write_file(tmp_path):
    execute([("write_file", "sub/dir/x.txt", "hello")], root=tmp_path)
    assert (tmp_path / "sub/dir/x.txt").read_text() == "hello"


def test_execute_rejects_unknown_step(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        execute([("teleport", "somewhere")], root=tmp_path)


def test_cli_list_and_dry_run_break(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "dead-dependency" in out and "blast radius" in out
    assert main(["--dry-run", "break", "poisoned-config"]) == 0


def test_repo_paths_in_plans_exist():
    root = pathlib.Path(__file__).resolve().parent.parent
    assert (root / INVENTORY_CONFIG_PATH).exists()
