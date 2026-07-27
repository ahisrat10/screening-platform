import docker
import tempfile
import os
import time
import base64

LANGUAGE_CONFIGS = {
    "python": {
        "image":    "python:3.11-slim",
        "filename": "solution.py",
        "cmd":      "python /code/solution.py",
        "timeout":  5,
    },
    "c": {
        "image":    "gcc:latest",
        "filename": "solution.c",
        "cmd":      "cp /code/solution.c /tmp/solution.c && cd /tmp && gcc solution.c -o solution 2>&1 && ./solution",
        "timeout":  10,
    },
    "cpp": {
        "image":    "gcc:latest",
        "filename": "solution.cpp",
        "cmd":      "cp /code/solution.cpp /tmp/solution.cpp && cd /tmp && g++ solution.cpp -o solution 2>&1 && ./solution",
        "timeout":  10,
    },
    "javascript": {
        "image":    "node:18-slim",
        "filename": "solution.js",
        "cmd":      "node /code/solution.js",
        "timeout":  5,
    },
    "java": {
        "image":    "openjdk:11-slim",
        "filename": "Solution.java",
        "cmd":      "cp /code/Solution.java /tmp/ && cd /tmp && javac Solution.java 2>&1 && java Solution",
        "timeout":  15,
    },
}


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
        code_file = os.path.join(tmpdir, config["filename"])
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(code)

        # ── Encode stdin as base64 and pipe it in directly ──────────
        # This avoids any host-container volume read issues with files
        stdin_content = stdin if stdin else ""
        stdin_b64     = base64.b64encode(stdin_content.encode("utf-8")).decode("ascii")

        cmd_with_stdin = f"echo {stdin_b64} | base64 -d | {config['cmd']}"

        try:
            output = client.containers.run(
                image            = config["image"],
                command          = ["/bin/sh", "-c", cmd_with_stdin],
                volumes          = {tmpdir: {"bind": "/code", "mode": "ro"}},
                tmpfs            = {"/tmp": "size=64m"},
                mem_limit        = "256m",
                nano_cpus        = 500_000_000,
                network_disabled = True,
                remove           = True,
                pids_limit       = 50,
                security_opt     = ["no-new-privileges"],
                stdout           = True,
                stderr           = True,
                timeout          = config["timeout"],
            )

            execution_time = round(time.time() - start_time, 3)
            result_output  = output.decode("utf-8", errors="replace").strip()

            return {
                "success":        True,
                "output":         result_output,
                "error":          "",
                "execution_time": execution_time,
                "language":       language,
            }

        except docker.errors.ContainerError as e:
            execution_time = round(time.time() - start_time, 3)
            error_msg = e.stderr.decode("utf-8", errors="replace").strip() if e.stderr else str(e)
            return {
                "success":        False,
                "output":         "",
                "error":          error_msg,
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
            err_str        = str(e).lower()

            if "timeout" in err_str or "timed out" in err_str:
                return {
                    "success":        False,
                    "output":         "",
                    "error":          f"Time Limit Exceeded! Code took more than {config['timeout']} seconds.",
                    "execution_time": config["timeout"],
                    "language":       language,
                }

            return {
                "success":        False,
                "output":         "",
                "error":          f"Execution error: {str(e)}",
                "execution_time": execution_time,
                "language":       language,
            }