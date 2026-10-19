"""SSH remote execution environment with ControlMaster connection persistence."""

import contextlib
import hashlib
import logging
import os
import posixpath
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from tools.credential_files import iter_skills_files, ssh_profile_sync_base

from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
from tools.environments.base_output import _popen_bash
from tools.environments.file_sync import (
    FileSyncManager, iter_sync_files, quoted_mkdir_command, quoted_rm_command, unique_parent_dirs)
from tools.environments.remote_common import (
    bash_argv, client_env_with, load_hermes_env_vars, prepend_unset, resolve_passthrough_env, run_capture)

logger = logging.getLogger(__name__)

# Windows OpenSSH has no Unix-socket ControlMaster: ControlPath/ControlMaster options
# fail the connection outright ('getsockname failed: Not a socket'). Skip multiplexing there.
# Skip multiplexing there; each command pays a fresh connection but the backend works. See #73927.
_SSH_MULTIPLEX = os.name != "nt"

# Module-level binding: tests patch ``ssh._load_hermes_env_vars`` to fake the .env file.
_load_hermes_env_vars = load_hermes_env_vars

# Remote roots that receive upload-only skill copies. Reconciliation is scoped to
# these; credentials, cache and the legacy shared skills tree are never touched.
_SKILL_SYNC_SUBDIRS = ("skills", "external_skills", "project_skills")


def _ensure_ssh_available() -> None:
    """Fail fast with a clear error when the SSH client is unavailable."""
    for tool in ("ssh", "scp"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool.upper()} is not installed or not in PATH. "
                               "Install OpenSSH client: apt install openssh-client")


def _sync_error(reason: str, subject: str, what: str = "the SSH connection") -> EnvironmentConnectionError:
    return EnvironmentConnectionError(
        reason, retry_hint=f"{subject} failed — verify {what} is healthy, then retry.")


class SSHEnvironment(BaseEnvironment):
    """Run commands on a remote machine over SSH.

    Spawn-per-call: every execute() spawns a fresh ``ssh ... bash -c`` process.
    Session snapshot preserves exported env vars across calls (unless
    ``persistent=False``); CWD persists via in-band
    stdout markers. Uses SSH ControlMaster for connection reuse.
    """

    # Passthrough values are re-forwarded on every command (see _run_bash), so like docker/local
    # they stay out of the remote snapshot under multiplex.
    _profile_scoped_passthrough = True
    _sudo_nopasswd_probe_supported = True

    def __init__(self, host: str, user: str, cwd: str = "~",
                 timeout: int = 60, port: int = 22, key_path: str = "",
                 probe_only: bool = False, sync_files: bool = True, persistent: bool = True):
        super().__init__(cwd=cwd, timeout=timeout, persist_env=persistent)
        self.host, self.user, self.port, self.key_path = host, user, port, key_path
        self.control_dir = Path(tempfile.gettempdir()) / "hermes-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        # Short, deterministic socket name: the path must stay under macOS's 104-byte sun_path
        # limit (raw user@host:port + SSH's 16-byte suffix under a deep $TMPDIR exceeds it), and
        # stability across reconnects keeps ControlMaster reuse working. A probe gets its own
        # per-instance socket so its cleanup() can never close the agent's shared master.
        socket_key = f"{user}@{host}:{port}"
        if probe_only:
            socket_key = f"{socket_key}:probe:{self._session_id}"
        _socket_id = hashlib.sha256(socket_key.encode()).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"
        _ensure_ssh_available()
        self._establish_connection()
        if probe_only:
            self._sync_manager = None
            return
        self._remote_home_detected = False
        self._remote_home = self._detect_remote_home()
        self._hermes_home = get_hermes_home()
        self._sync_base = ssh_profile_sync_base(self._remote_home, self._hermes_home)
        self._sync_manager = None
        if sync_files:
            self._ensure_remote_dirs()
            # Best effort: a cleanup failure must not make the workbench unusable;
            # the next environment retries against the same remote tree.
            try:
                self._reconcile_remote_skill_copies()
            except Exception:
                logger.exception("SSH: stale skill copy cleanup failed on %s", self.host)
            self._sync_manager = FileSyncManager(
                get_files_fn=self._profile_sync_files,
                upload_fn=self._scp_upload, delete_fn=self._ssh_delete,
                bulk_upload_fn=self._ssh_bulk_upload, bulk_download_fn=self._ssh_bulk_download,
                upload_only_roots=tuple(f"{self._sync_base}/{name}" for name in
                                        ("skills", "external_skills", "project_skills")))
            self._sync_manager.sync(force=True)
        self.init_session()

    def _control_socket_for(self, send_env: tuple[str, ...]) -> Path:
        """One ControlMaster per SendEnv name-set, beside the plain target socket: a mux master only
        relays the env names it was itself started with and silently drops the rest, so a passthrough
        command must ride a master that knows its names. scp/sync/probes keep the plain socket."""
        plain = Path(self.control_socket)
        if not send_env:
            return plain
        # <target-id[:8]><names-hash[:8]>.sock: same length as the plain socket (macOS's 104-byte
        # sun_path cap) and prefix-globbable so cleanup() finds every sibling without extra state.
        digest = hashlib.sha256(" ".join(send_env).encode()).hexdigest()[:8]
        return plain.with_name(f"{plain.stem[:8]}{digest}.sock")

    def _control_sockets(self) -> list[Path]:
        """The plain socket plus every SendEnv-set sibling (shared 8-char target prefix)."""
        plain = Path(self.control_socket)
        siblings = sorted(plain.parent.glob(f"{plain.stem[:8]}*.sock")) if plain.parent.is_dir() else []
        return [plain, *(s for s in siblings if s != plain)]

    def _target_flags(self, port_flag: str) -> list:
        """Port/key flags shared by ssh (``-p``) and scp (``-P``)."""
        flags = [port_flag, str(self.port)] if self.port != 22 else []
        return flags + (["-i", self.key_path] if self.key_path else [])

    def _build_ssh_command(self, extra_args: list | None = None, send_env: Iterable[str] = ()) -> list:
        send_env = tuple(sorted(send_env))
        cmd = ["ssh"]
        if _SSH_MULTIPLEX:
            cmd.extend(["-o", f"ControlPath={self._control_socket_for(send_env)}",
                        "-o", "ControlMaster=auto", "-o", "ControlPersist=300"])
        cmd.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"])
        # Names only; values ride the ssh client's own environment (never the remote command text).
        cmd.extend(arg for name in send_env for arg in ("-o", f"SendEnv={name}"))
        cmd.extend(self._target_flags("-p"))
        cmd.extend(extra_args or [])
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _run_ssh(self, remote_cmd: str, timeout: float) -> subprocess.CompletedProcess:
        """Run one remote shell command over the multiplexed connection, capturing output."""
        return run_capture(self._build_ssh_command() + [remote_cmd], timeout=timeout)

    def _run_ssh_checked(self, remote_cmd: str, timeout: float, reason: str, subject: str) -> None:
        result = self._run_ssh(remote_cmd, timeout=timeout)
        if result.returncode != 0:
            raise _sync_error(f"{reason}: {result.stderr.strip()}", subject)

    def _establish_connection(self):
        try:
            result = self._run_ssh("echo 'SSH connection established'", timeout=15)
        except subprocess.TimeoutExpired:
            raise EnvironmentConnectionError(
                f"SSH connection to {self.user}@{self.host} timed out",
                retry_hint=(f"Check that {self.host} is up and reachable on port {self.port} "
                            "and that sshd is running, then retry."))
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            raise EnvironmentConnectionError(
                f"SSH connection failed: {error_msg}",
                retry_hint=(f"Check that {self.host} is up, sshd is running on port {self.port}, and "
                            f"{self.user} can log in with the configured key, then retry — "
                            "the connection is re-established automatically."))

    def _detect_remote_home(self) -> str:
        """Detect the remote user's home directory."""
        with contextlib.suppress(Exception):
            result = self._run_ssh("echo $HOME", timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                logger.debug("SSH: remote home = %s", result.stdout.strip())
                self._remote_home_detected = True
                return result.stdout.strip()
        return "/root" if self.user == "root" else f"/home/{self.user}"

    def agent_visible_cache_base(self) -> str:
        return self._sync_base

    def _additional_profile_scoped_passthrough_names(self) -> Iterable[str]:
        return ("HERMES_HOME",)

    def _profile_sync_files(self) -> list[tuple[str, str]]:
        # Eviction/cleanup may run after the caller's profile context has ended.
        token = set_hermes_home_override(self._hermes_home)
        try:
            return iter_sync_files(self._sync_base)
        finally:
            reset_hermes_home_override(token)

    def _skill_sync_roots(self) -> tuple[str, ...]:
        return tuple(f"{self._sync_base}/{name}" for name in _SKILL_SYNC_SUBDIRS)

    def _profile_skill_files(self) -> list[dict[str, str]]:
        """Current host skill files mapped into this profile's remote namespace."""
        token = set_hermes_home_override(self._hermes_home)
        try:
            return iter_skills_files(container_base=self._sync_base)
        finally:
            reset_hermes_home_override(token)

    def _list_remote_skill_files(self) -> set[str]:
        """Enumerate existing regular files and symlinks under the skill roots."""
        roots = self._skill_sync_roots()
        # One round trip. Optional roots (external_skills/project_skills) may be
        # absent; a real find error aborts instead of being read as "nothing there".
        # A symlinked base/root is skipped: following it could enumerate (and then
        # delete) another profile's tree through this profile's path string.
        command = (
            f"if [ -L {shlex.quote(self._sync_base)} ]; then exit 1; fi; "
            "for d in " + " ".join(shlex.quote(root) for root in roots) + "; do "
            "if [ -L \"$d\" ]; then continue; fi; "
            "if [ -d \"$d\" ]; then find \"$d\" \\( -type f -o -type l \\) -print0 || exit 1; fi; done"
        )
        result = self._run_ssh(command, timeout=30)
        if result.returncode != 0:
            logger.warning("SSH: could not enumerate remote skill copies on %s: %s",
                           self.host, result.stderr.strip())
            return set()
        found = set()
        for raw in result.stdout.split("\0"):
            if not raw:
                continue
            normalized = posixpath.normpath(raw)
            # Never trust the remote to keep rm scoped to this profile's skill roots.
            if any(normalized == root or normalized.startswith(root + "/") for root in roots):
                found.add(normalized)
        return found

    def _stale_remote_skill_files(
        self, remote_files: set[str], expected_files: set[str],
    ) -> tuple[list[str], set[str]]:
        """Files under remote skill dirs whose ``SKILL.md`` no longer exists on the host.

        A missing SKILL.md means the whole skill was uninstalled (or renamed). Only that
        skill directory is removed: files an agent generated inside a still-installed
        skill (venv, node_modules, ...) are execution state, not stale uploads.

        A SKILL.md directly at a sync root is never treated as a removable skill: that
        would make the root itself "stale" and delete every skill below it. A live
        nested skill inside a stale parent also survives.
        """
        roots = self._skill_sync_roots()
        remote_skill_dirs = {
            posixpath.dirname(path) for path in remote_files
            if posixpath.basename(path) == "SKILL.md"
        }
        candidate_dirs = {directory for directory in remote_skill_dirs
                          if all(directory != root for root in roots)}
        stale_dirs = {directory for directory in candidate_dirs
                      if f"{directory}/SKILL.md" not in expected_files}
        live_dirs = candidate_dirs - stale_dirs
        stale_files = []
        for path in sorted(remote_files):
            if not any(path == directory or path.startswith(directory + "/")
                       for directory in stale_dirs):
                continue
            if any(path == directory or path.startswith(directory + "/")
                   for directory in live_dirs):
                continue
            stale_files.append(path)
        return stale_files, stale_dirs

    def _prune_empty_remote_dirs(self, directories: set[str]) -> None:
        if not directories:
            return
        quoted = " ".join(shlex.quote(directory) for directory in sorted(directories))
        self._run_ssh(f"find {quoted} -depth -type d -empty -delete 2>/dev/null || true", timeout=10)

    def _reconcile_remote_skill_copies(self) -> None:
        """Drop execution copies of skills that no longer exist on the host.

        ``FileSyncManager`` only deletes remote files recorded by this process, so a
        fresh session, gateway restart or an uninstall through the host CLI (ACLI)
        left the previous environment's copy behind and still executable. Rebuilding
        the expected inventory from host state and comparing it with the remote tree
        makes cleanup independent of process-local memory.
        """
        expected = {entry["container_path"] for entry in self._profile_skill_files()}
        stale_files, stale_dirs = self._stale_remote_skill_files(
            self._list_remote_skill_files(), expected)
        if not stale_files:
            return
        logger.info("SSH: removing %d stale skill file(s) from %d uninstalled skill dir(s) on %s",
                    len(stale_files), len(stale_dirs), self.host)
        for start in range(0, len(stale_files), 200):
            self._ssh_delete(stale_files[start:start + 200])
        self._prune_empty_remote_dirs(stale_dirs)

    def _ensure_remote_dirs(self) -> None:
        """Create base ~/.hermes directory tree on remote in one SSH call."""
        base = self._sync_base
        self._run_ssh(quoted_mkdir_command([base, f"{base}/skills", f"{base}/credentials", f"{base}/cache"]),
                      timeout=10)

    def _scp_upload(self, host_path: str, remote_path: str) -> None:
        """Upload a single file via scp over ControlMaster."""
        self._run_ssh(f"mkdir -p {shlex.quote(str(Path(remote_path).parent))}", timeout=10)
        scp_cmd = ["scp"] + (["-o", f"ControlPath={self.control_socket}"] if _SSH_MULTIPLEX else [])
        scp_cmd += self._target_flags("-P") + [host_path, f"{self.user}@{self.host}:{remote_path}"]
        result = run_capture(scp_cmd, timeout=30)
        if result.returncode != 0:
            raise _sync_error(f"scp failed: {result.stderr.strip()}", f"File sync to {self.user}@{self.host}")

    def _ssh_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Upload many files in one tar-over-SSH stream: local ``tar c`` piped through one SSH
        connection to remote ``tar x``, after a single batched ``mkdir -p``."""
        if not files:
            return
        base = self._sync_base
        parents = unique_parent_dirs(files)
        if parents:
            self._run_ssh_checked(quoted_mkdir_command(parents), 30, "remote mkdir failed",
                                  f"Remote directory setup on {self.host}")

        # Symlink staging avoids fragile GNU tar --transform rules. On Windows
        # without Developer Mode symlink creation raises OSError winerror 1314;
        # only that case falls back to a plain copy, other OSErrors re-raise.
        with tempfile.TemporaryDirectory(prefix="hermes-ssh-bulk-") as staging:
            for host_path, remote_path in files:
                try:
                    rel_remote = os.path.relpath(remote_path, base)
                except ValueError as exc:
                    raise RuntimeError(f"remote path {remote_path!r} is not under sync base {base!r}") from exc
                if rel_remote == "." or rel_remote.startswith("../"):
                    raise RuntimeError(f"remote path {remote_path!r} escapes sync base {base!r}")
                staged = os.path.join(staging, rel_remote)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                try:
                    os.symlink(os.path.abspath(host_path), staged)
                except OSError as e:
                    if getattr(e, "winerror", None) != 1314:
                        raise
                    shutil.copy2(host_path, staged)

            # --no-overwrite-dir keeps tar from stamping the staging dir's mode onto
            # existing dirs (e.g. /home/<user>); a umask-002 0775 home breaks sshd StrictModes.
            ssh_cmd = self._build_ssh_command() + [f"tar xf - --no-overwrite-dir -C {shlex.quote(base)}"]
            tar_proc = subprocess.Popen(["tar", "-chf", "-", "-C", staging, "."], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ssh_proc = subprocess.Popen(ssh_cmd, stdin=tar_proc.stdout,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except Exception:
                tar_proc.kill()
                tar_proc.wait()
                raise
            tar_proc.stdout.close()  # let tar_proc receive SIGPIPE if ssh_proc exits early
            try:
                _, ssh_stderr = ssh_proc.communicate(timeout=120)
                # communicate() (not wait()) drains stderr so tar can't deadlock on >PIPE_BUF errors.
                if tar_proc.poll() is None:
                    _, tar_stderr_raw = tar_proc.communicate(timeout=10)
                else:
                    tar_stderr_raw = tar_proc.stderr.read() if tar_proc.stderr else b""
            except subprocess.TimeoutExpired:
                for proc in (tar_proc, ssh_proc):
                    proc.kill()
                for proc in (tar_proc, ssh_proc):
                    proc.wait()  # kill both first, then reap: never wait on one while the other blocks
                raise EnvironmentConnectionError(
                    "SSH bulk upload timed out",
                    retry_hint=f"Bulk file sync to {self.host} timed out — check the connection and retry.")
            if tar_proc.returncode != 0:
                raise RuntimeError(f"tar create failed (rc={tar_proc.returncode}): "
                                   f"{tar_stderr_raw.decode(errors='replace').strip()}")
            if ssh_proc.returncode != 0:
                raise _sync_error(f"tar extract over SSH failed (rc={ssh_proc.returncode}): "
                                  f"{ssh_stderr.decode(errors='replace').strip()}",
                                  f"File sync over SSH to {self.host}", what="the connection")
        logger.debug("SSH: bulk-uploaded %d file(s) via tar pipe", len(files))

    def _ssh_bulk_download(self, dest: Path) -> None:
        """Download remote .hermes/ as a tar archive."""
        # Tar from / with the full path so archive entries keep absolute paths
        # (home/user/.hermes/skills/f.py), matching _pushed_hashes keys.
        rel_base = self._sync_base.lstrip("/")
        # Live sockets inside .hermes (gateway.sock and friends) cannot be archived: tar prints
        # "socket ignored" and some builds exit 2, which failed every sync-back and left a
        # multi-GB temp tar behind on each retry. Exclude them up front.
        ssh_cmd = self._build_ssh_command() + [
            f"tar cf - --exclude='*.sock' -C / {shlex.quote(rel_base)}"]
        with open(dest, "wb") as f:
            result = subprocess.run(ssh_cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.PIPE, timeout=120, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            # A socket not named *.sock is the only rc=2 we knowingly accept, and only when
            # nothing else was reported — anchored to the diagnostic suffix so a filename merely
            # containing "socket ignored" cannot sneak through. Every other status still fails.
            diagnostic_lines = [line for line in stderr.splitlines() if line.strip()]
            tolerated = result.returncode == 2 and bool(diagnostic_lines) and all(
                line.endswith(": socket ignored") for line in diagnostic_lines)
            if not tolerated:
                raise _sync_error(f"SSH bulk download failed: {stderr}",
                                  f"File sync from {self.host}")

    def _ssh_delete(self, remote_paths: list[str]) -> None:
        self._run_ssh_checked(quoted_rm_command(remote_paths), 10, "remote rm failed",
                              f"Remote file cleanup on {self.host}")

    def _before_execute(self) -> None:
        if self._sync_manager is not None:
            self._sync_manager.sync()  # rate-limited internally

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Forward the passthrough allowlist (skill ``required_environment_variables`` +
        ``terminal.env_passthrough``) the way docker does: ``SendEnv`` carries the names, the ssh
        client's env carries the values, so secrets never enter the remote ``bash -c`` argv. The
        remote sshd must ``AcceptEnv`` them (#14091). Profile-scoped names missing from the active
        scope are unset remotely so a shared host cannot serve another profile's value."""
        values, unset_names = resolve_passthrough_env(hermes_env_loader=_load_hermes_env_vars)
        if hasattr(self, "_sync_base"):
            cmd_string = f"export HERMES_HOME={shlex.quote(self._sync_base)}; " + cmd_string
        cmd = self._build_ssh_command(send_env=values) + bash_argv(shlex.quote(prepend_unset(cmd_string, unset_names)), login)
        client_env = client_env_with(values)
        return _popen_bash(cmd, stdin_data, env=client_env) if client_env is not None else _popen_bash(cmd, stdin_data)

    def cleanup(self):
        if self._sync_manager:
            logger.info("SSH: syncing files from sandbox...")
            self._sync_manager.sync_back(hermes_home=self._hermes_home)
        for socket in self._control_sockets():
            if not socket.exists():
                continue
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                cmd = ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", f"{self.user}@{self.host}"]
                subprocess.run(cmd, capture_output=True, timeout=5, stdin=subprocess.DEVNULL, check=False)
            with contextlib.suppress(OSError):
                socket.unlink()
