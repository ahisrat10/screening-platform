import docker
import tempfile
import os
import time

LANGUAGE_CONFIGS = {
    "python": {
        "image":    "python:3.11-slim",
        "filename": "solution.py",
        # No stdin redirection needed at all — the input() shim prepended to
        # the code reads /code/stdin.txt directly via a plain file read.
        "cmd":      "python /code/solution.py",
        "timeout":  5,
    },
    "c": {
        "image":    "gcc:latest",
        "filename": "solution.c",
        # Copy both the source AND the stdin file into the container's own
        # /tmp before use. Redirecting "<" directly against the Windows bind
        # mount is what was failing; redirecting against a container-local
        # copy sidesteps that entirely.
        "cmd":      "cp /code/solution.c /tmp/solution.c && cp /code/stdin.txt /tmp/stdin.txt && cd /tmp && gcc solution.c -o solution 2>&1 && ./solution < /tmp/stdin.txt",
        "timeout":  10,
    },
    "cpp": {
        "image":    "gcc:latest",
        "filename": "solution.cpp",
        "cmd":      "cp /code/solution.cpp /tmp/solution.cpp && cp /code/stdin.txt /tmp/stdin.txt && cd /tmp && g++ solution.cpp -o solution 2>&1 && ./solution < /tmp/stdin.txt",
        "timeout":  10,
    },
    "javascript": {
        "image":    "node:18-slim",
        "filename": "solution.js",
        "cmd":      "cp /code/solution.js /tmp/solution.js && cp /code/stdin.txt /tmp/stdin.txt && cd /tmp && node solution.js < /tmp/stdin.txt",
        "timeout":  5,
    },
    "java": {
        "image":    "openjdk:11-slim",
        "filename": "Solution.java",
        "cmd":      "cp /code/Solution.java /tmp/ && cp /code/stdin.txt /tmp/stdin.txt && cd /tmp && javac Solution.java 2>&1 && java Solution < /tmp/stdin.txt",
        "timeout":  15,
    },
}

def build_python_code(code: str, stdin: str) -> str:
    """
    Prepend a fake input() to Python solutions so they never touch stdin at
    all. The test input is embedded directly into the generated source as a
    Python list literal (via repr) — no file I/O, no volume-mount reads, no
    stdin piping. input() just pops the next line from that in-memory list.
    """
    lines = (stdin if stdin else "").split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    shim = f'''\
_input_lines = {lines!r}
_input_index = 0

def input(*_args, **_kwargs):
    global _input_index
    if _input_index >= len(_input_lines):
        raise EOFError("EOF when reading a line")
    _line = _input_lines[_input_index]
    _input_index += 1
    return _line

'''
    return shim + code


def execute_code(code: str, language: str, stdin: str = "") -> dict:
    lang   = language.lower().strip()
    config = LANGUAGE_CONFIGS.get(lang)

    if not config:
        return {
            "success":        False,
            "output":         "",
            "error":          f"Language '{language}' not supported. Use: python, c, cpp, javascript, java",
            "execution_time": 0,
            "language":       language,
        }

    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        return {
            "success":        False,
            "output":         "",
            "error":          "Docker is not running! Please start Docker Desktop.",
            "execution_time": 0,
            "language":       language,
        }

    start_time = time.time()

    with tempfile.TemporaryDirectory() as tmpdir:
        code_to_write = code
        if lang == "python":
            code_to_write = build_python_code(code, stdin)

        code_file = os.path.join(tmpdir, config["filename"])
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(code_to_write)

        # Write stdin to a plain file in the mounted dir instead of piping it
        # through Docker's stdin attachment, which does not work reliably on
        # Windows Docker Desktop. Python reads this file directly (no stdin
        # needed at all); other languages cp it into the container's own
        # /tmp and redirect "<" against that local copy (see LANGUAGE_CONFIGS).
        stdin_file = os.path.join(tmpdir, "stdin.txt")
        with open(stdin_file, "w", encoding="utf-8") as f:
            f.write(stdin if stdin else "")

        # NOTE: client.containers.run() in docker-py 7.x does not accept a
        # `timeout` kwarg — passing one raises TypeError immediately (before
        # the container even starts), and since the word "timeout" appears
        # in that TypeError's message, the old broad except-clause below
        # matched it and misreported it as "Time Limit Exceeded". That
        # masked the real error for every single run, regardless of stdin
        # handling. Fixed by running detached and enforcing the timeout via
        # container.wait(timeout=...), which is the documented way to do it.
        container = None
        try:
            container = client.containers.run(
                image            = config["image"],
                command          = ["/bin/sh", "-c", config["cmd"]],
                volumes          = {tmpdir: {"bind": "/code", "mode": "ro"}},
                # "exec" is required — Docker's default tmpfs mount options
                # include noexec, which silently breaks running compiled
                # C/C++/Java binaries from /tmp (they cp their build there).
                tmpfs            = {"/tmp": "size=64m,exec"},
                mem_limit        = "256m",
                nano_cpus        = 500_000_000,
                network_disabled = True,
                pids_limit       = 50,
                security_opt     = ["no-new-privileges"],
                detach           = True,
            )

            try:
                wait_result = container.wait(timeout=config["timeout"])
            except Exception as wait_err:
                if "timeout" in str(wait_err).lower() or "timed out" in str(wait_err).lower():
                    container.kill()
                    return {
                        "success":        False,
                        "output":         "",
                        "error":          f"Time Limit Exceeded! Code took more than {config['timeout']} seconds.",
                        "execution_time": config["timeout"],
                        "language":       language,
                    }
                raise

            execution_time = round(time.time() - start_time, 3)
            exit_code      = wait_result.get("StatusCode", 0)

            if exit_code != 0:
                # Combined, not stderr-only: compiler errors for c/cpp/java
                # are redirected with "2>&1" inside the shell command (see
                # LANGUAGE_CONFIGS), so they land in Docker's stdout stream,
                # not its stderr stream.
                error_msg = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace").strip()
                return {
                    "success":        False,
                    "output":         "",
                    "error":          error_msg,
                    "execution_time": execution_time,
                    "language":       language,
                }

            result_output = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace").strip()

            return {
                "success":        True,
                "output":         result_output,
                "error":          "",
                "execution_time": execution_time,
                "language":       language,
            }

        except docker.errors.ImageNotFound:
            return {
                "success":        False,
                "output":         "",
                "error":          f"Docker image not found. Run: docker pull {config['image']}",
                "execution_time": 0,
                "language":       language,
            }

        except Exception as e:
            execution_time = round(time.time() - start_time, 3)
            return {
                "success":        False,
                "output":         "",
                "error":          f"Execution error: {str(e)}",
                "execution_time": execution_time,
                "language":       language,
            }

        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass