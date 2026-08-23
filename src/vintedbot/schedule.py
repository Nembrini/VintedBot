"""Windows Task Scheduler integration: hand ``run-all`` over to the OS.

The task definition is built here as XML and registered with
``Register-ScheduledTask -Xml``. Two alternatives were weighed and
dropped: the ``schtasks.exe`` flags cannot express half of what we need
(``StartWhenAvailable``, ``RandomDelay``, ``MultipleInstancesPolicy``),
and assembling the task from PowerShell cmdlets ends up doing XML
surgery anyway to put a random delay on a repeating trigger. Writing the
document ourselves buys three things: every setting is expressible,
``--dry-run`` prints the *exact* document that would be registered
instead of a paraphrase of it, and the generation stays a pure function —
testable without touching Windows at all.

Everything that talks to the OS funnels through :func:`_run_powershell`,
the single seam the tests replace.

Two choices deserve their reason in writing:

* **LogonType S4U.** The task must run when nobody is logged on, and
  without a Windows password stored anywhere. S4U ("service for user")
  buys exactly that: the process runs as the user, with network access,
  but with no interactive session. The flip side is that mapped network
  drives are unavailable — irrelevant here, everything lives on a local
  absolute path.
* **No console window.** That is a consequence of S4U, not of the
  launcher: a task without an interactive session runs in session 0,
  where no window can appear — not even when the task is started by hand
  from the GUI. ``<Hidden>`` is a second belt, not the mechanism.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import structlog

if TYPE_CHECKING:
    from datetime import datetime

    from vintedbot.config import Settings

logger = structlog.get_logger(__name__)

#: The scheduler's own kill is the OUTER safety net: it must never fire
#: before our watchdog, which stops cleanly and exits with code 4. Hence
#: the margin over ``max_run_seconds``.
EXECUTION_TIME_LIMIT_MARGIN = 1.5

#: Name of the launcher, relative to the project directory.
LAUNCHER_RELATIVE_PATH = Path("scripts") / "run-all.cmd"


class SchedulerError(Exception):
    """The Task Scheduler refused an operation, or is unreachable."""


def iso_duration(seconds: float) -> str:
    """Format a duration the way the task schema wants it: ``PT10M``, ``PT1H30M``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    body = ""
    if hours:
        body += f"{hours}H"
    if minutes:
        body += f"{minutes}M"
    if secs or not body:
        body += f"{secs}S"
    return f"PT{body}"


def split_task_name(full_name: str) -> tuple[str, str]:
    """Split ``\\VintedBot\\run-all`` into folder ``\\VintedBot\\`` and name ``run-all``.

    A name without a folder lands in the root folder ``\\``.
    """
    normalized = "\\" + full_name.strip().strip("\\")
    folder, _, name = normalized.rpartition("\\")
    if not name:
        raise SchedulerError(f"nome del task non valido: {full_name!r}")
    folder = folder or "\\"
    if not folder.endswith("\\"):
        folder += "\\"
    return folder, name


def current_user_id() -> str:
    """``DOMAIN\\user`` for the task principal, falling back to the bare name."""
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if not user:  # pragma: no cover - Windows always sets USERNAME
        import getpass

        user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


@dataclass(frozen=True)
class TaskSpec:
    """Everything the XML needs, resolved and absolute."""

    task_name: str
    command: Path
    working_directory: Path
    interval_minutes: int
    random_delay_minutes: int
    execution_time_limit_seconds: float
    user_id: str
    start_boundary: str
    description: str

    @classmethod
    def from_settings(
        cls, settings: Settings, *, project_dir: Path, now: datetime, user_id: str | None = None
    ) -> TaskSpec:
        """Build the spec from configuration; every path is made absolute.

        The working directory is the project directory because ``.env``
        and ``searches.toml`` are read relative to it — under the
        scheduler the current directory is ``C:\\Windows\\system32``.
        """
        project = project_dir.expanduser().resolve()
        return cls(
            task_name=settings.scheduler_task_name,
            command=project / LAUNCHER_RELATIVE_PATH,
            working_directory=project,
            interval_minutes=settings.scheduler_interval_minutes,
            random_delay_minutes=settings.scheduler_random_delay_minutes,
            execution_time_limit_seconds=settings.max_run_seconds * EXECUTION_TIME_LIMIT_MARGIN,
            user_id=user_id or current_user_id(),
            # Midnight today: a boundary in the past makes the repetition
            # active immediately instead of waiting for a first slot.
            start_boundary=now.replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            ).isoformat(timespec="seconds"),
            description=(
                "VintedBot: esegue le ricerche salvate e notifica gli affari su Telegram. "
                f"Ripetizione ogni {settings.scheduler_interval_minutes} minuti."
            ),
        )


def build_task_xml(spec: TaskSpec) -> str:
    """Render the task definition. Pure function — the whole point of the design.

    Element order follows the task schema (the same order Task Scheduler
    itself uses when exporting a task): the schema is a sequence, not a
    set, and a reordered document is rejected.
    """
    random_delay = (
        f"\n      <RandomDelay>{iso_duration(spec.random_delay_minutes * 60)}</RandomDelay>"
        if spec.random_delay_minutes > 0
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(spec.description)}</Description>
    <URI>{escape(spec.task_name)}</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>{iso_duration(spec.interval_minutes * 60)}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{escape(spec.start_boundary)}</StartBoundary>
      <Enabled>true</Enabled>{random_delay}
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(spec.user_id)}</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{iso_duration(spec.execution_time_limit_seconds)}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(spec.command))}</Command>
      <WorkingDirectory>{escape(str(spec.working_directory))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


@dataclass(frozen=True)
class TaskStatus:
    """What the Task Scheduler knows about our task."""

    registered: bool
    state: str | None = None
    last_run_time: str | None = None
    last_result: int | None = None
    next_run_time: str | None = None

    @property
    def last_result_hex(self) -> str | None:
        """Windows shows the exit code in hex; ours are 0-4, so 0x0…0x4."""
        return None if self.last_result is None else f"0x{self.last_result & 0xFFFFFFFF:X}"


# --------------------------------------------------------------------- OS seam


def _ps_quote(value: str) -> str:
    """Quote a value for a PowerShell single-quoted literal string."""
    return "'" + value.replace("'", "''") + "'"


def _clean_error(text: str) -> str:
    """Turn PowerShell's multi-line error vomit into one readable line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    message = lines[0]
    return message if len(message) <= 300 else message[:297] + "..."


def _run_powershell(script: str) -> str:
    """Run a PowerShell snippet and return stdout. The only seam to Windows."""
    if sys.platform != "win32":
        raise SchedulerError("la schedulazione automatica è disponibile solo su Windows")
    try:
        completed = subprocess.run(  # noqa: S603 - argomenti costruiti da noi, mai dall'utente
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SchedulerError(f"impossibile eseguire PowerShell: {exc}") from exc

    if completed.returncode != 0:
        detail = _clean_error(completed.stderr) or _clean_error(completed.stdout)
        raise SchedulerError(detail or "comando PowerShell fallito senza dettagli")
    return completed.stdout


def _write_temp_xml(xml: str) -> Path:
    """Persist the XML as UTF-16LE, the encoding its own declaration claims."""
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - chiuso subito, il path serve dopo
        mode="w", suffix=".xml", encoding="utf-16", delete=False
    )
    with handle:
        handle.write(xml)
    return Path(handle.name)


def install_command(spec: TaskSpec, xml_path: Path) -> str:
    """The PowerShell snippet that registers the task — shown by --dry-run."""
    folder, name = split_task_name(spec.task_name)
    return (
        "$ErrorActionPreference = 'Stop'; "
        f"$xml = Get-Content -LiteralPath {_ps_quote(str(xml_path))} -Raw -Encoding Unicode; "
        f"Register-ScheduledTask -TaskName {_ps_quote(name)} -TaskPath {_ps_quote(folder)} "
        f"-Xml $xml -User {_ps_quote(spec.user_id)} -Force | Out-Null"
    )


def install_task(spec: TaskSpec) -> None:
    """Register (or replace) the scheduled task. Requires no admin rights."""
    xml_path = _write_temp_xml(build_task_xml(spec))
    try:
        _run_powershell(install_command(spec, xml_path))
    finally:
        xml_path.unlink(missing_ok=True)
    logger.info("scheduled_task_installed", task=spec.task_name, command=str(spec.command))


def uninstall_task(task_name: str) -> bool:
    """Remove the task (and its now-empty folder). True when something was removed."""
    folder, name = split_task_name(task_name)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(name)} -TaskPath {_ps_quote(folder)} "
        "-ErrorAction SilentlyContinue; "
        "if ($null -eq $t) { 'absent' } else { "
        f"Unregister-ScheduledTask -TaskName {_ps_quote(name)} -TaskPath {_ps_quote(folder)} "
        "-Confirm:$false; "
        # Best effort: an empty folder we created is noise in the GUI, but
        # failing to remove it must never fail the uninstall.
        "try { $s = New-Object -ComObject Schedule.Service; $s.Connect(); "
        f"$s.GetFolder('\\').DeleteFolder({_ps_quote(folder.strip(chr(92)))}, 0) }} catch {{}} "
        "'removed' }"
    )
    removed = "removed" in _run_powershell(script)
    logger.info("scheduled_task_uninstalled", task=task_name, removed=removed)
    return removed


def task_status(task_name: str) -> TaskStatus:
    """Ask Windows about the task; an unregistered task is not an error."""
    folder, name = split_task_name(task_name)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(name)} -TaskPath {_ps_quote(folder)} "
        "-ErrorAction SilentlyContinue; "
        "if ($null -eq $t) { ConvertTo-Json @{ registered = $false } } else { "
        "$i = Get-ScheduledTaskInfo -InputObject $t; "
        "ConvertTo-Json @{ registered = $true; state = [string]$t.State; "
        "lastRunTime = $(if ($i.LastRunTime) "
        "{ $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }); "
        "lastResult = $i.LastTaskResult; "
        "nextRunTime = $(if ($i.NextRunTime) "
        "{ $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }) } }"
    )
    return parse_status(_run_powershell(script))


def parse_status(raw: str) -> TaskStatus:
    """Turn the PowerShell JSON into a :class:`TaskStatus` (pure, hence tested)."""
    try:
        data: Any = json.loads(raw)
    except ValueError as exc:
        raise SchedulerError("risposta non leggibile dal Task Scheduler") from exc
    if not isinstance(data, dict) or not data.get("registered"):
        return TaskStatus(registered=False)
    result = data.get("lastResult")
    return TaskStatus(
        registered=True,
        state=_optional_str(data.get("state")),
        last_run_time=_optional_str(data.get("lastRunTime")),
        last_result=int(result) if isinstance(result, int | float) else None,
        next_run_time=_optional_str(data.get("nextRunTime")),
    )


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def run_task_now(task_name: str) -> None:
    """Start the task immediately, as the scheduler would."""
    folder, name = split_task_name(task_name)
    _run_powershell(
        "$ErrorActionPreference = 'Stop'; "
        f"Start-ScheduledTask -TaskName {_ps_quote(name)} -TaskPath {_ps_quote(folder)}"
    )
    logger.info("scheduled_task_started", task=task_name)
