"""Task Scheduler integration: XML generation, parsing and the OS seam.

Nothing here touches Windows: :func:`vintedbot.schedule._run_powershell`
is the single door to the system and every test replaces it. No task is
ever registered, queried or removed for real.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest

import vintedbot.schedule as schedule_module
from vintedbot.config import Settings
from vintedbot.schedule import (
    EXECUTION_TIME_LIMIT_MARGIN,
    LAUNCHER_RELATIVE_PATH,
    SchedulerError,
    TaskSpec,
    TaskStatus,
    build_task_xml,
    install_task,
    iso_duration,
    parse_status,
    split_task_name,
    task_status,
    uninstall_task,
)

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path / "data",
        max_run_seconds=600,
    )


@pytest.fixture()
def spec(settings: Settings) -> TaskSpec:
    return TaskSpec.from_settings(
        settings,
        project_dir=Path("C:/progetti/VintedBot"),
        now=datetime(2026, 8, 23, 15, 4, 5, tzinfo=UTC),
        user_id="PC\\filip",
    )


@pytest.fixture()
def powershell(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every snippet that would have been sent to PowerShell."""
    calls: list[str] = []

    def fake(script: str) -> str:
        calls.append(script)
        return fake.reply  # type: ignore[attr-defined]

    fake.reply = ""  # type: ignore[attr-defined]
    monkeypatch.setattr(schedule_module, "_run_powershell", fake)
    return calls


# ------------------------------------------------------------- primitive pure


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(600, "PT10M"), (900, "PT15M"), (3600, "PT1H"), (5400, "PT1H30M"), (0, "PT0S"), (45, "PT45S")],
)
def test_iso_duration(seconds: int, expected: str) -> None:
    assert iso_duration(seconds) == expected


@pytest.mark.parametrize(
    ("full", "folder", "name"),
    [
        ("\\VintedBot\\run-all", "\\VintedBot\\", "run-all"),
        ("VintedBot\\run-all", "\\VintedBot\\", "run-all"),
        ("run-all", "\\", "run-all"),
        ("\\run-all", "\\", "run-all"),
        ("\\a\\b\\c", "\\a\\b\\", "c"),
    ],
)
def test_split_task_name(full: str, folder: str, name: str) -> None:
    assert split_task_name(full) == (folder, name)


def test_split_task_name_rejects_empty() -> None:
    with pytest.raises(SchedulerError):
        split_task_name("\\\\")


# ------------------------------------------------------------------ TaskSpec


def test_spec_uses_absolute_paths_and_the_launcher(spec: TaskSpec) -> None:
    assert spec.command.is_absolute()
    assert spec.command.name == LAUNCHER_RELATIVE_PATH.name
    assert spec.working_directory.is_absolute()
    # La cwd sotto lo scheduler non e' quella del progetto: deve essere imposta.
    assert spec.command.parent.parent == spec.working_directory


def test_execution_limit_leaves_room_for_the_watchdog(settings: Settings, spec: TaskSpec) -> None:
    """The scheduler's kill is the OUTER net: it must not pre-empt exit code 4."""
    assert spec.execution_time_limit_seconds > settings.max_run_seconds
    assert spec.execution_time_limit_seconds == settings.max_run_seconds * (
        EXECUTION_TIME_LIMIT_MARGIN
    )


def test_start_boundary_is_in_the_past_and_naive(spec: TaskSpec) -> None:
    # Un inizio a mezzanotte rende la ripetizione attiva subito; il campo
    # non ammette offset di fuso.
    assert spec.start_boundary.endswith("T00:00:00")
    assert "+" not in spec.start_boundary


# ------------------------------------------------------------------- the XML


def test_xml_is_well_formed(spec: TaskSpec) -> None:
    ElementTree.fromstring(build_task_xml(spec))  # noqa: S314 - documento nostro


def _text(spec: TaskSpec, path: str) -> str | None:
    root = ElementTree.fromstring(build_task_xml(spec))  # noqa: S314 - documento nostro
    found = root.find(path, NS)
    return None if found is None else found.text


def test_xml_encodes_every_requested_constraint(spec: TaskSpec) -> None:
    checks = {
        # gira senza login e senza password salvata
        ".//t:Principal/t:LogonType": "S4U",
        # nessun privilegio di amministratore
        ".//t:Principal/t:RunLevel": "LeastPrivilege",
        # se il giro precedente e' ancora in corso, il nuovo non parte
        ".//t:Settings/t:MultipleInstancesPolicy": "IgnoreNew",
        # portatile: parte e prosegue anche a batteria
        ".//t:Settings/t:DisallowStartIfOnBatteries": "false",
        ".//t:Settings/t:StopIfGoingOnBatteries": "false",
        # recupera le esecuzioni perse, ma non sveglia il PC
        ".//t:Settings/t:StartWhenAvailable": "true",
        ".//t:Settings/t:WakeToRun": "false",
        # ripetizione a intervallo regolare, con jitter
        ".//t:Triggers/t:TimeTrigger/t:Repetition/t:Interval": "PT10M",
        ".//t:Triggers/t:TimeTrigger/t:RandomDelay": "PT3M",
        # limite coerente con il watchdog (600s * 1.5)
        ".//t:Settings/t:ExecutionTimeLimit": "PT15M",
        # nessuna finestra
        ".//t:Settings/t:Hidden": "true",
    }
    for path, expected in checks.items():
        assert _text(spec, path) == expected, path


def test_xml_repetition_has_no_duration_so_it_never_stops(spec: TaskSpec) -> None:
    root = ElementTree.fromstring(build_task_xml(spec))  # noqa: S314 - documento nostro
    repetition = root.find(".//t:Triggers/t:TimeTrigger/t:Repetition", NS)
    assert repetition is not None
    assert repetition.find("t:Duration", NS) is None


def test_xml_action_points_at_the_launcher(spec: TaskSpec) -> None:
    assert _text(spec, ".//t:Actions/t:Exec/t:Command") == str(spec.command)
    assert _text(spec, ".//t:Actions/t:Exec/t:WorkingDirectory") == str(spec.working_directory)


def test_random_delay_is_omitted_when_zero(settings: Settings, spec: TaskSpec) -> None:
    from dataclasses import replace

    xml = build_task_xml(replace(spec, random_delay_minutes=0))
    assert "RandomDelay" not in xml


def test_xml_escapes_special_characters(spec: TaskSpec) -> None:
    from dataclasses import replace

    hostile = replace(spec, user_id="PC\\a&b", description="ricerche <tutte> & affari")
    xml = build_task_xml(hostile)
    ElementTree.fromstring(xml)  # noqa: S314 - resta ben formato
    assert "a&amp;b" in xml
    assert "<tutte>" not in xml


# ------------------------------------------------------------ status parsing


def test_parse_status_unregistered() -> None:
    assert parse_status('{"registered": false}') == TaskStatus(registered=False)


def test_parse_status_full() -> None:
    status = parse_status(
        '{"registered": true, "state": "Ready", "lastRunTime": "2026-08-23 15:00:00",'
        ' "lastResult": 3, "nextRunTime": "2026-08-23 15:10:00"}'
    )
    assert status.registered
    assert status.state == "Ready"
    assert status.last_run_time == "2026-08-23 15:00:00"
    assert status.last_result == 3
    assert status.last_result_hex == "0x3"


def test_parse_status_tolerates_missing_times() -> None:
    status = parse_status('{"registered": true, "state": "Ready", "lastRunTime": null}')
    assert status.last_run_time is None
    assert status.last_result is None
    assert status.last_result_hex is None


def test_parse_status_rejects_garbage() -> None:
    with pytest.raises(SchedulerError):
        parse_status("non è json")


# ------------------------------------------------------------------ the seam


def test_install_writes_utf16_xml_and_cleans_up(spec: TaskSpec, powershell: list[str]) -> None:
    captured: dict[str, str] = {}

    def fake(script: str) -> str:
        powershell.append(script)
        match = re.search(r"Get-Content -LiteralPath '([^']+)'", script)
        assert match is not None
        path = Path(match.group(1))
        # Il file deve esistere MENTRE PowerShell gira, e sparire dopo.
        captured["path"] = str(path)
        captured["xml"] = path.read_text(encoding="utf-16")
        return ""

    schedule_module._run_powershell = fake  # type: ignore[assignment]  # noqa: SLF001
    install_task(spec)

    assert "-Xml $xml" in powershell[0]
    assert "Register-ScheduledTask" in powershell[0]
    assert "-TaskName 'run-all'" in powershell[0]
    assert "-TaskPath '\\VintedBot\\'" in powershell[0]
    assert captured["xml"] == build_task_xml(spec)
    assert not Path(captured["path"]).exists()  # niente file temporanei orfani


def test_uninstall_is_idempotent(powershell: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_module, "_run_powershell", lambda script: "absent")
    assert uninstall_task("\\VintedBot\\run-all") is False

    monkeypatch.setattr(schedule_module, "_run_powershell", lambda script: "removed")
    assert uninstall_task("\\VintedBot\\run-all") is True


def test_status_queries_the_right_task(powershell: list[str]) -> None:
    powershell_reply = '{"registered": true, "state": "Ready"}'
    schedule_module._run_powershell = lambda script: (  # type: ignore[assignment]  # noqa: SLF001
        powershell.append(script) or powershell_reply
    )
    assert task_status("\\VintedBot\\run-all").state == "Ready"
    assert "Get-ScheduledTask -TaskName 'run-all' -TaskPath '\\VintedBot\\'" in powershell[0]


def test_quoting_survives_an_apostrophe() -> None:
    assert schedule_module._ps_quote("l'affare") == "'l''affare'"  # noqa: SLF001


# ------------------------------------------------------------------ launcher


def test_launcher_exists_where_the_task_will_look_for_it() -> None:
    assert (PROJECT_ROOT / LAUNCHER_RELATIVE_PATH).is_file()


def test_launcher_contract() -> None:
    """The launcher's four promises, guarded against a careless edit."""
    text = (PROJECT_ROOT / LAUNCHER_RELATIVE_PATH).read_text(encoding="ascii")
    # 1. percorsi assoluti derivati dalla posizione dello script, non dalla cwd
    assert "%~dp0" in text
    # 2. usa il python del venv, senza script di attivazione
    assert "\\.venv\\Scripts\\python.exe" in text
    # 3. propaga l'exit code al Task Scheduler
    assert "exit /b %ERRORLEVEL%" in text
    # 4. dichiara la provenienza per i log
    assert "VINTEDBOT_INVOKED_BY=scheduler" in text


def test_launcher_preflight_fails_with_config_code() -> None:
    text = (PROJECT_ROOT / LAUNCHER_RELATIVE_PATH).read_text(encoding="ascii")
    # .venv e .env mancanti => exit 2 (config), mai un crash
    assert text.count("exit /b 2") == 2
    assert "launcher.log" in text
