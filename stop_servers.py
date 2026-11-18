"""
Stop servers
============

Stops every recording API server running on this machine, however it was
started and whichever port it listens on.

    python stop_servers.py            stop them all
    python stop_servers.py --list     only show them
    python stop_servers.py --force    do not ask, even if rooms are recording

Recordings are ended through the API first, so each MP3 is finalised and moved
to its place. The launchd agent or scheduled task is stopped as well, not just
its process, which it would start again; the server is back at the next log
in, or when the restart script is run.

A server is recognised by its answer: every Python process listening on a TCP
port is asked for its /status. The listening processes come from tools the
operating system brings: lsof on macOS, netstat and tasklist on Windows.

The exit code is 0 if no server is running afterwards, 1 if one still is or
the question was answered with no, and 2 if they could not be looked for.
"""

import argparse
import csv
import dataclasses
import http.client
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

from config import local_url

# What starts the server unattended, under the names macos/install-agent.sh
# and windows/install-task.ps1 give them.
LAUNCHD_LABEL = os.environ.get("RECORDING_API_LABEL", "local.recording-api")
TASK_NAME = os.environ.get("RECORDING_API_TASK", "RecordingAPI")

# How long a process may take to answer /status. Most Python programs that
# listen are not a recording API and say so at once; this is for one that
# accepts the connection and then says nothing.
ANSWER_TIMEOUT_S = 2.0

# How long a recording gets to end when asked through the API, and a server to
# exit when told to. A server ends its own recordings first, and one whose
# ffmpeg hangs takes ten seconds by itself (STOP_TIMEOUT_S in recording.py).
END_RECORDING_TIMEOUT_S = 30.0
EXIT_TIMEOUT_S = 30.0

# How long a port gets to close once its process is gone.
PORT_CLOSE_TIMEOUT_S = 5.0

# Requests go straight to the address, whatever proxy the system is set up to
# use: a proxy cannot reach this machine's own 127.0.0.1.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclasses.dataclass(frozen=True)
class Server:
    """One recording API server that is running."""

    pid: int
    url: str  # where this machine reaches it, e.g. http://127.0.0.1:8080
    sessions: dict  # "sessions" of its /status: what is recording, by room

    @property
    def rooms(self) -> list[str]:
        return list(self.sessions)


# --- Finding the servers ------------------------------------------------------


def find_servers() -> list[Server]:
    """Every recording API server running on this machine.

    Raises OSError if the listening processes cannot be listed.
    """
    servers = []
    # By address: with --reload, two processes listen on the same one.
    statuses: dict[str, dict | None] = {}
    for pid, url in listening_python_processes():
        if url not in statuses:
            statuses[url] = status_of(url)
        if statuses[url] is not None:
            servers.append(Server(pid, url, statuses[url]["sessions"]))
    return servers


def status_of(url: str) -> dict | None:
    """The /status of the recording API at url; None if that is not one."""
    try:
        status = request(f"{url}/status")
    except OSError:
        return None
    return status if isinstance(status.get("sessions"), dict) else None


def request(
    url: str, body: dict | None = None, timeout: float = ANSWER_TIMEOUT_S
) -> dict:
    """GET url, or POST body to it as JSON; returns the JSON object answered.

    Raises OSError for anything else: no answer, an error status, not JSON.
    """
    data = None if body is None else json.dumps(body).encode()
    message = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    try:
        with DIRECT.open(message, timeout=timeout) as response:
            answer = json.load(response)
    except (http.client.HTTPException, ValueError) as e:
        raise OSError(f"{url} did not answer in JSON over HTTP: {e!r}") from e
    if not isinstance(answer, dict):
        raise OSError(f"{url} did not answer with a JSON object")
    return answer


def listening_python_processes() -> list[tuple[int, str]]:
    """(pid, url) for every TCP port a Python process on this machine listens on."""
    if sys.platform == "win32":
        names = names_from_tasklist(output_of(["tasklist", "/FO", "CSV", "/NH"]))
        listeners = listeners_from_netstat(output_of(["netstat", "-ano"]), names)
        if not names or not listeners:
            # Windows itself always listens somewhere, so this is a list that
            # could not be read, not a machine where nothing listens.
            raise OSError("could not read the output of tasklist and netstat")
    else:
        listeners = listeners_from_lsof(
            output_of(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpctn"])
        )
    return sorted(
        {
            (pid, url_of(address))
            for pid, command, address in listeners
            if command.lower().startswith("python")
        }
    )


def output_of(command: list[str]) -> str:
    # errors="replace": Windows tools answer in the console's code page, and a
    # name that does not decode is not one looked for here.
    result = subprocess.run(
        command, capture_output=True, text=True, errors="replace", check=False
    )
    return result.stdout


def listeners_from_lsof(output: str) -> list[tuple[int, str, str]]:
    """(pid, command, address) of each listening socket lsof -F reported.

    It prints one field per line, named by the first letter: p starts a
    process, c is its command, t a socket's type and n its address, with
    "*:8080" for every address of that type.
    """
    listeners = []
    pid, command, kind = 0, "", ""
    for line in output.splitlines():
        field, value = line[:1], line[1:]
        if field == "p" and value.isdigit():
            pid = int(value)
        elif field == "c":
            command = value
        elif field == "t":
            kind = value
        elif field == "n":
            host, _, port = value.rpartition(":")
            if host == "*":
                host = "[::]" if kind == "IPv6" else "0.0.0.0"
            if port.isdigit():
                listeners.append((pid, command, f"{host}:{port}"))
    return listeners


def listeners_from_netstat(
    output: str, names: dict[int, str]
) -> list[tuple[int, str, str]]:
    """The same from Windows' netstat -ano, which gives no process names.

    A listening socket is told by the port 0 of its remote address: the word
    in the State column is translated ("ABHÖREN" on a German Windows).
    """
    listeners = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != "TCP" or not fields[-1].isdigit():
            continue
        local, remote, pid = fields[1], fields[2], int(fields[-1])
        if remote.endswith(":0") and local.rpartition(":")[2].isdigit():
            listeners.append((pid, names.get(pid, ""), local))
    return listeners


def names_from_tasklist(output: str) -> dict[int, str]:
    """Each process's name by pid, from tasklist /FO CSV /NH."""
    return {
        int(row[1]): row[0]
        for row in csv.reader(output.splitlines())
        if len(row) >= 2 and row[1].isdigit()
    }


def url_of(address: str) -> str:
    """Where this machine reaches what listens on address, e.g. "0.0.0.0:8080"."""
    host, _, port = address.rpartition(":")
    return local_url(host.strip("[]"), int(port))


# --- Stopping them ------------------------------------------------------------


def show(servers: list[Server]) -> None:
    if not servers:
        print("No recording API server is running on this machine.")
    for server in servers:
        rooms = ", ".join(server.rooms)
        doing = f"recording {rooms}" if rooms else "not recording"
        print(f"Recording API at {server.url} (process {server.pid}), {doing}")


def confirmed(question: str) -> bool:
    try:
        return input(question).strip().lower().startswith("y")
    except EOFError:
        return False  # nobody to ask; that is what --force is for


def end_recordings(servers: list[Server]) -> None:
    """Stop what is recording, the way a button press would."""
    # By address: with --reload, two processes are the same server.
    for url, rooms in {server.url: server.rooms for server in servers}.items():
        for room in rooms:
            print(f"Stopping recording in {room} ...")
            try:
                reply = request(
                    f"{url}/recording/stop",
                    {"room_name": room},
                    END_RECORDING_TIMEOUT_S,
                )
            except OSError as e:
                # Worth continuing: the stop is still wanted, and a recording
                # that is cut off stays playable.
                print(f"  warning: could not stop it cleanly: {e}", file=sys.stderr)
                continue
            saved = reply.get("file")
            print(f"  saved {saved}" if saved else f"  {reply.get('message', '')}")


def stop_service() -> None:
    """Stop the agent or task that starts the server, so it stays stopped.

    Ending its server process alone would not do: launchd starts it again.
    """
    if sys.platform == "win32":
        # Fails if there is no such task or it is not running: nothing to end.
        ended = subprocess.run(
            ["schtasks", "/End", "/TN", TASK_NAME], capture_output=True, check=False
        )
        if ended.returncode == 0:
            print(f"Ended scheduled task '{TASK_NAME}'.")
    elif sys.platform == "darwin":
        service = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
        if not launchd_has(service):
            return
        print(f"Unloading launchd agent '{LAUNCHD_LABEL}' ...")
        unloading = subprocess.run(
            ["launchctl", "bootout", service],
            capture_output=True,
            text=True,
            check=False,
        )
        # bootout can return before the server has ended its recordings and
        # exited, and until then the agent cannot be loaded again.
        deadline = time.monotonic() + EXIT_TIMEOUT_S
        while (loaded := launchd_has(service)) and time.monotonic() < deadline:
            time.sleep(0.5)
        if loaded:
            print(
                f"  warning: launchd did not unload it: {unloading.stderr.strip()}",
                file=sys.stderr,
            )


def launchd_has(service: str) -> bool:
    found = subprocess.run(
        ["launchctl", "print", service], capture_output=True, check=False
    )
    return found.returncode == 0


def stop_processes(pids: list[int]) -> None:
    """End the server processes that are still running, and their ffmpeg."""
    if sys.platform == "win32":
        for pid in pids:
            # Windows has no polite request for a program without a window.
            # /T takes the whole tree: an ffmpeg left behind keeps the audio
            # device open, and the next recording then fails to start.
            killing = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
            # 128: there is no such process, because the task took it with it.
            if killing.returncode not in (0, 128):
                print(
                    f"  warning: could not end process {pid}: "
                    f"{(killing.stderr or killing.stdout).strip()}",
                    file=sys.stderr,
                )
        return

    # SIGTERM is the polite request: the server ends its recordings and exits.
    for pid in pids:
        send(pid, signal.SIGTERM)
    deadline = time.monotonic() + EXIT_TIMEOUT_S
    while any(map(is_running, pids)) and time.monotonic() < deadline:
        time.sleep(0.2)

    for pid in filter(is_running, pids):
        print(f"Process {pid} did not exit; killing it.")
        ffmpegs = children_of(pid)
        for process in (pid, *ffmpegs):
            send(process, signal.SIGKILL)


def send(pid: int, signal_number: int) -> None:
    try:
        os.kill(pid, signal_number)
    except ProcessLookupError:
        pass  # already gone
    except PermissionError:
        print(
            f"  warning: process {pid} belongs to another user and was left running",
            file=sys.stderr,
        )


def is_running(pid: int) -> bool:
    # ps, not os.kill(pid, 0), which also finds a process that has exited but
    # has not been collected by its parent yet.
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def children_of(pid: int) -> list[int]:
    found = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
    )
    return [int(child) for child in found.stdout.split()]


def still_running() -> list[Server]:
    """The servers that still answer once the ports have had time to close."""
    deadline = time.monotonic() + PORT_CLOSE_TIMEOUT_S
    while True:
        servers = find_servers()
        if not servers or time.monotonic() >= deadline:
            return servers
        time.sleep(0.5)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stop every recording API server running on this machine."
    )
    parser.add_argument("--list", action="store_true", help="only show them")
    parser.add_argument(
        "--force",
        "-f",
        "-Force",  # as windows\restart.cmd spells it
        action="store_true",
        help="do not ask, even if rooms are recording",
    )
    options = parser.parse_args(arguments)

    try:
        servers = find_servers()
    except OSError as e:
        print(f"error: could not look for running servers: {e}", file=sys.stderr)
        return 2

    show(servers)
    if options.list:
        return 0

    recording = any(server.rooms for server in servers)
    question = "Stop these recordings? [y/N] "
    if recording and not options.force and not confirmed(question):
        print("Nothing changed.")
        return 1

    end_recordings(servers)
    stop_service()
    stop_processes(sorted({server.pid for server in servers}))

    left = still_running()
    for server in left:
        print(
            f"error: still running: {server.url} (process {server.pid})",
            file=sys.stderr,
        )
    if servers and not left:
        print("Stopped.")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
