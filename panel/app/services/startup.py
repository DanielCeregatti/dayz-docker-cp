"""Bringing the server up: update what is enabled, then start.

"Update server files on start" and "Update mods on start" are about the DayZ
server's start, not the container's. Tying them to the container would be the
wrong moment twice over: with AUTO_START off nothing starts at boot, so the
updates would run for nobody - and a server started from the dashboard hours
later would come up on whatever was current at boot.

The two switches are independent. Either alone, both, or neither: each one
only decides whether its own step runs, and a step that is switched off is
skipped rather than failed.

A crash restart deliberately does **not** update. The watchdog exists to get
the server back up, and putting a multi-minute download in front of that turns
a short outage into a long one - while a failing download would prevent the
restart entirely.
"""

from __future__ import annotations

import logging
import threading
import time

from . import server_files
from .jobs import JobBusy, JobState
from .server import ServerError

log = logging.getLogger(__name__)

# How long a boot job may take before the sequence gives up waiting. Large,
# because a first install is gigabytes; small enough that a wedged job does
# not keep the server from ever starting.
JOB_TIMEOUT_SECONDS = 3 * 60 * 60

# Pause before the one retry of a failed server files update. Steam's own
# hiccups - an app info request that times out - are usually over by then.
UPDATE_RETRY_SECONDS = 60


class StartupSequence:
    """Runs the enabled pre-start updates and then starts the server."""

    def __init__(self, settings, store, steamcmd, mods, manager) -> None:
        self.settings = settings
        self.store = store
        self.steamcmd = steamcmd
        self.mods = mods
        self.manager = manager
        self._lock = threading.Lock()
        self._running = False
        self._step = ""

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running

    @property
    def step(self) -> str:
        """What the sequence is doing right now, for the status endpoint."""
        with self._lock:
            return self._step

    # --- public API -------------------------------------------------------

    def start_server(self, reason: str = "manual", restart: bool = False) -> str:
        """Start (or restart) the server, updating first if that is enabled.

        Returns a message describing what will happen. Raises ServerError if
        the server cannot be started at all - the caller reports that as it
        would for a plain start.
        """
        steps = self._enabled_steps()

        if not steps and not restart:
            # Nothing to do first: start synchronously, so a failure is
            # reported in the response rather than swallowed by a thread.
            self.manager.start(reason=reason)
            return "Server starting."

        with self._lock:
            if self._running:
                raise ServerError("A start is already in progress.")
            if not restart and self.manager.running:
                raise ServerError("The server is already running.")
            self._running = True
            self._step = "queued"

        threading.Thread(
            target=self._run,
            args=(reason, restart, steps),
            name="server-startup",
            daemon=True,
        ).start()

        if not steps:
            return "Restarting."
        return "Updating before start: " + ", ".join(steps) + "."

    def _enabled_steps(self) -> list[str]:
        stored = self.store.current
        steps = []

        if not self.settings.steam_credentials_set:
            # Without credentials neither step can run. Saying so once beats
            # two identical failures inside the job log.
            if stored.auto_update or stored.auto_mod_update:
                log.warning("Update on start is enabled but Steam credentials are missing")
            return steps

        if stored.auto_update and self.settings.server_installed:
            steps.append("server files")
        if stored.auto_mod_update and self.mods.registry.all():
            steps.append("mods")
        return steps

    # --- the sequence -----------------------------------------------------

    def _run(self, reason: str, restart: bool, steps: list[str]) -> None:
        try:
            if restart:
                self._set_step("stopping")
                # Updating the files under a running server is exactly what the
                # panel refuses to do elsewhere, so the stop comes first.
                try:
                    self.manager.stop(wait=True)
                except ServerError:
                    pass  # already stopped, which is where we want to be

            if "server files" in steps:
                self._set_step("updating server files")
                if not self._update_server_files():
                    return

            if "mods" in steps:
                self._set_step("updating mods")
                if not self._await(self.mods.update_all()):
                    return

            self._set_step("starting")
            self.manager.start(reason=reason)
        except (ServerError, JobBusy) as exc:
            log.warning("Start after update failed: %s", exc)
            self.manager.console_buffer.append(f"[panel] Start aborted: {exc}")
        except Exception:  # noqa: BLE001 - a thread must not die silently
            log.exception("Startup sequence failed")
        finally:
            with self._lock:
                self._running = False
                self._step = ""

    def _update_server_files(self) -> bool:
        """Update the server files. False means the server must not be started.

        A failed update gets one retry. If that fails as well but SteamCMD left
        the app manifest exactly as it was, no update ever began and the files
        on disk are the ones the server last ran on, so it starts on those. A
        Steam timeout before the download kept the server down from the 06:00
        restart until someone noticed at noon (26 Oct 2026); an update that got
        far enough to touch the manifest is still a stop sign.
        """
        manifest = self._read_manifest()
        job = self.steamcmd.update_server()
        if not self._finished(job):
            return False
        if job.state is JobState.SUCCESS:
            return True

        if job.state is JobState.FAILED:
            log.warning("Job %s failed - retrying in %d s", job.id, UPDATE_RETRY_SECONDS)
            self.manager.console_buffer.append(
                f"[panel] {job.title} failed - retrying in {UPDATE_RETRY_SECONDS} s."
            )
            self._set_step("retrying the server files update")
            time.sleep(UPDATE_RETRY_SECONDS)
            job = self.steamcmd.update_server()
            if not self._finished(job):
                return False
            if job.state is JobState.SUCCESS:
                return True

        untouched = manifest is not None and self._read_manifest() == manifest
        if job.state is JobState.FAILED and untouched:
            log.warning("Job %s failed without touching the server files - starting anyway", job.id)
            self.manager.console_buffer.append(
                f"[panel] {job.title} failed twice but changed nothing - "
                "starting on the current files."
            )
            return True

        return self._refuse(job)

    def _read_manifest(self) -> bytes | None:
        path = server_files.manifest_path(self.settings.paths, self.settings.SERVER_APP_ID)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _finished(self, job) -> bool:
        """Wait for a job. False means it ran out of time and the server stays down."""
        if job is None or job.done.wait(JOB_TIMEOUT_SECONDS):
            return True
        log.warning("Job %s did not finish in time - not starting the server", job.id)
        return False

    def _await(self, job) -> bool:
        """Wait for a job. False means the server must not be started."""
        if not self._finished(job):
            return False
        if job is None or job.state is JobState.SUCCESS:
            return True
        return self._refuse(job)

    def _refuse(self, job) -> bool:
        # A failed update before a start is a stop sign. Starting anyway could
        # mean running half-written files, and the operator has to see why.
        log.warning("Job %s ended as %s - not starting the server", job.id, job.state.value)
        self.manager.console_buffer.append(
            f"[panel] {job.title} did not succeed ({job.state.value}) - "
            "the server was not started. See the job output."
        )
        return False

    def _set_step(self, step: str) -> None:
        with self._lock:
            self._step = step
        log.info("Startup sequence: %s", step)

    # --- container boot ---------------------------------------------------

    def install_if_missing(self):
        """AUTO_INSTALL: fetch the server files when there are none.

        Stays tied to the container, not to the server start: it is what makes
        a fresh container usable at all, and there is nothing to start before
        it has run.
        """
        if self.settings.server_installed or not self.settings.auto_install:
            return None
        if not self.settings.steam_credentials_set:
            return None

        try:
            job = self.steamcmd.install_server()
        except JobBusy:
            return None
        log.info("AUTO_INSTALL is enabled - started job %s", job.id)
        return job
