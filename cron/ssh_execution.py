"""Opt-in SSH cron execution; paths are resolved only on the workbench."""

import json
import shlex
import subprocess
import uuid
from pathlib import PurePosixPath

SCRIPT_TIMEOUT = 60
KILL_AFTER = 2


def ssh_environment(**kwargs):
    from tools.environments.ssh import SSHEnvironment

    return SSHEnvironment(**kwargs)


def workspace_path(value, base="/workspace/scripts"):
    """Remote names only: never interpret these paths on the brain filesystem."""
    p = PurePosixPath(value)
    if not p.is_absolute():
        p = PurePosixPath(base) / p
    if ".." in p.parts or "\x00" in str(p) or not p.is_relative_to(base):
        raise ValueError("script must be inside /workspace/scripts")
    return str(p)


def execute_cancellable(env, command, cancel_event):
    """Bound one foreground process group; retain unknown on SSH failure."""
    directory = str(
        PurePosixPath(env.get_temp_dir()) / ("hermes-cron-" + uuid.uuid4().hex)
    )
    q = shlex.quote(directory)
    wrapped = (
        "umask 077; mkdir " + q + " || exit 76; "
        "trap " + shlex.quote("rm -rf -- " + q) + " EXIT; "
        "setsid sh -c " + shlex.quote(command) + " & child=$!; "
        'echo "$child" > ' + q + '/pid; wait "$child"'
    )
    result = []

    def worker():
        try:
            result.append(env._run_ssh(wrapped, timeout=70))
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            result.append(exc)

    from agent.memory_provider import spawn_context_thread

    thread = spawn_context_thread(worker, name="ssh-cron")
    thread.start()
    cancelled = False
    while thread.is_alive():
        thread.join(0.1)
        if cancel_event is not None and cancel_event.is_set() and thread.is_alive():
            cancelled = True
            # If dispatch has not written pid yet, retry until it does or exits.
            r = env._run_ssh(
                "test -f " + q + "/pid || exit 77; "
                "p=$(cat " + q + '/pid); case "$p" in ""|*[!0-9]*) exit 78;; esac; '
                'kill -KILL -- -"$p" 2>/dev/null || ! kill -0 -- -"$p" 2>/dev/null',
                timeout=15,
            )
            if r.returncode not in (0, 77):
                raise RuntimeError(
                    "cancel termination unconfirmed; interrupted/unknown"
                )
    if cancelled:
        raise RuntimeError("cancelled during execution; interrupted; no automatic redo")
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


def script_root():
    from hermes_cli.config import load_config

    root = (
        load_config().get("ssh_adaptation", {}).get("script_root", "/workspace/scripts")
    )
    if not PurePosixPath(root).is_absolute() or ".." in PurePosixPath(root).parts:
        raise ValueError("script_root must be an absolute remote path")
    return root


def run_script(script_path, workdir=None, cancel_event=None, interpreter=None):
    from hermes_cli.config import load_config
    from tools.environments.ssh import SSHEnvironment

    cfg = load_config()["terminal"]
    if cfg.get("backend") != "ssh":
        return False, "adaptation requires SSH; no host fallback"
    try:
        root = script_root()
        path = workspace_path(script_path, root)
        cwd = workspace_path(workdir or "/workspace", "/workspace")
        if cancel_event is not None and cancel_event.is_set():
            return False, "cancelled before dispatch"
        env = ssh_environment(
            host=cfg["ssh_host"],
            user=cfg.get("ssh_user", "root"),
            port=cfg.get("ssh_port", 22),
            key_path=cfg["ssh_key"],
            cwd=cwd,
            timeout=60,
            probe_only=True,
        )
        # Resolve in the workbench, including symlinks; host paths are never executed.
        check = (
            "p=$(realpath -e -- " + shlex.quote(path) + "); "
            'case "$p" in ' + shlex.quote(root) + "/*) ;; *) exit 73;; esac; "
            'test -f "$p" || exit 74; '
            "d=$(realpath -e -- " + shlex.quote(cwd) + "); "
            'case "$d" in /workspace|/workspace/*) ;; *) exit 75;; esac; cd "$d" || exit 75; '
        )
        from cron.ssh_interpreter import selection_command

        # Keep the supervisor alive on TERM so timeout reaches its forced group kill,
        # even if the foreground script exits before its ordinary background children.
        protocol = (
            "import json,subprocess,sys,signal,time\n"
            "def hold_for_kill(signum,frame):\n"
            "    while True: time.sleep(60)\n"
            "signal.signal(signal.SIGTERM,hold_for_kill)\n"
            "r=subprocess.run(sys.argv[1:],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,errors='replace')\n"
            "print(json.dumps({'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}))\n"
        )
        command = (
            check
            + selection_command(interpreter)
            + "exec timeout -k "
            + str(KILL_AFTER)
            + "s "
            + str(SCRIPT_TIMEOUT)
            + "s python3 -c "
            + shlex.quote(protocol)
            + ' "$interpreter" "$p"'
        )
        r = execute_cancellable(env, command, cancel_event)
        from agent.redact import redact_sensitive_text

        if r.returncode:
            return False, "workbench transport/script timeout exit " + str(
                r.returncode
            ) + "\n" + redact_sensitive_text((r.stdout + "\n" + r.stderr).strip())
        result = json.loads(r.stdout)
        if (
            not isinstance(result, dict)
            or type(result.get("returncode")) is not int
            or not isinstance(result.get("stdout"), str)
            or not isinstance(result.get("stderr"), str)
        ):
            raise ValueError("invalid workbench result protocol")
        output = redact_sensitive_text(
            result["stdout"].strip()
            if result["returncode"] == 0
            else (result["stdout"] + "\n" + result["stderr"]).strip()
        )
        return result["returncode"] == 0, output if result[
            "returncode"
        ] == 0 else "workbench script exit " + str(result["returncode"]) + "\n" + output
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        from agent.redact import redact_sensitive_text

        detail = redact_sensitive_text(str(exc))[:1000]
        return False, "workbench dispatch failed (" + type(
            exc
        ).__name__ + "): " + detail + "; outcome may be unknown; explicit redo only"


def read_script(
    script_path, *, execution_entry=True, max_bytes=1048576, metadata=False
):
    """Separate entry validation from classified execution-reference reads on SSH."""
    from hermes_cli.config import load_config
    from cron.ssh_lifecycle_reader import reference_reader_program

    root = script_root()
    cfg = load_config()["terminal"]
    if cfg.get("backend") != "ssh":
        raise ValueError("SSH required")
    prefix = ""
    if execution_entry:
        path = workspace_path(script_path, root)
        prefix = (
            "p=$(realpath -e -- " + shlex.quote(path) + "); "
            'case "$p" in ' + shlex.quote(root) + "/*) ;; *) exit 73;; esac; "
            'test -f "$p" || exit 74; '
        )
    else:
        path = str(PurePosixPath(str(script_path)))
        if not path.startswith(("/", "~")) or "\x00" in path:
            raise ValueError("invalid remote reference")
    env = ssh_environment(
        host=cfg["ssh_host"],
        user=cfg.get("ssh_user", "root"),
        port=cfg.get("ssh_port", 22),
        key_path=cfg["ssh_key"],
        probe_only=True,
    )
    command = (
        prefix
        + "python3 -c "
        + shlex.quote(reference_reader_program())
        + " "
        + shlex.quote(path)
        + " "
        + str(max_bytes)
    )
    r = env._run_ssh(command, timeout=15)
    if r.returncode:
        raise ValueError("workbench script read failed")
    entry = json.loads(r.stdout)
    if entry["unsafe"] or not entry["exists"]:
        raise ValueError("workbench script unreadable or oversized")
    return entry if metadata else entry["text"]


def validate_script(script_path):
    if not script_path or not str(script_path).strip():
        return None
    try:
        read_script(script_path)
        return None
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        return "workbench script validation failed: " + type(exc).__name__


def check_lifecycle(prompt, script, workdir=None):
    from cron.lifecycle_guard import (
        GatewayLifecycleBlocked,
        scan_gateway_lifecycle,
        _lifecycle_command_scan_with_data_exemption,
        _LifecycleScanBudget,
        _budget_exhausted,
    )

    cwd = normalize_workdir(workdir or "/workspace")
    if script:
        try:
            entry = read_script(script, metadata=True)
            text = entry["text"] or ""
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            raise GatewayLifecycleBlocked("Cannot inspect workbench script") from exc
    else:
        text = ""
    combined = (prompt or "") + "\n" + text
    if script and PurePosixPath(entry["resolved_path"]).suffix.lower() not in (
        ".sh",
        ".bash",
    ):
        budget = _LifecycleScanBudget()
        if not budget.charge_text(combined):
            unsafe = _budget_exhausted(budget, "text", 0)
            refusal = budget.refusal
        else:
            unsafe = _lifecycle_command_scan_with_data_exemption(combined)
            refusal = None
        if not unsafe:
            # Python source keeps its native direct scan; prompt is still shell/tool intent.
            unsafe, refusal = scan_gateway_lifecycle(
                prompt or "",
                cwd=cwd,
                read_remote_script=lambda reference, max_bytes=1048576: read_script(
                    reference, execution_entry=False, max_bytes=max_bytes
                ),
                remote_only=True,
                retain_cwd=True,
            )
    else:
        unsafe, refusal = scan_gateway_lifecycle(
            combined,
            cwd=cwd,
            read_remote_script=lambda reference, max_bytes=1048576: read_script(
                reference, execution_entry=False, max_bytes=max_bytes
            ),
            remote_only=True,
            retain_cwd=True,
        )
    if unsafe:
        raise GatewayLifecycleBlocked(refusal or "Gateway lifecycle operation blocked")


def normalize_workdir(workdir):
    if workdir is None or not str(workdir).strip():
        return None
    value = workspace_path(str(workdir), "/workspace")
    from hermes_cli.config import load_config

    c = load_config()["terminal"]
    if c.get("backend") != "ssh":
        raise ValueError("SSH required")
    env = ssh_environment(
        host=c["ssh_host"],
        user=c.get("ssh_user", "root"),
        port=c.get("ssh_port", 22),
        key_path=c["ssh_key"],
        probe_only=True,
    )
    r = env._run_ssh(
        "d=$(realpath -e -- "
        + shlex.quote(value)
        + '); case "$d" in /workspace|/workspace/*) ;; *) exit 75;; esac; test -d "$d" && test -x "$d" && cd "$d"',
        timeout=15,
    )
    if r.returncode:
        raise ValueError("workbench workdir missing or outside workspace")
    return value
