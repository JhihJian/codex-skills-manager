"""Trusted deterministic checkers and an opt-in bubblewrap runner."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - bubblewrap is unavailable on this platform too
    resource = None  # type: ignore[assignment]


class CheckerRegistrationError(ValueError):
    pass


class CheckerCommandRejected(ValueError):
    pass


def _copy_workspace_secure(
    source: Path,
    destination: Path,
    max_bytes: int,
    max_entries: int,
    deadline: float,
) -> None:
    """Copy regular files through no-follow descriptors with live budgets."""
    destination.mkdir()
    copied_bytes = 0
    copied_entries = 0
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    non_block = getattr(os, "O_NONBLOCK", 0)
    for directory, directory_names, file_names, directory_fd in os.fwalk(source, follow_symlinks=False):
        if time.monotonic() >= deadline:
            raise TimeoutError("workspace copy timed out")
        relative = Path(directory).relative_to(source)
        target_directory = destination / relative
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in directory_names:
            copied_entries += 1
            if copied_entries > max_entries:
                raise CheckerCommandRejected("workspace exceeds the sandbox entry limit")
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise CheckerCommandRejected(f"workspace contains an unsafe directory entry: {relative / name}")
            (target_directory / name).mkdir(exist_ok=True)
        for name in file_names:
            copied_entries += 1
            if copied_entries > max_entries:
                raise CheckerCommandRejected("workspace exceeds the sandbox entry limit")
            label = relative / name
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise CheckerCommandRejected(f"workspace contains an unsupported entry: {label}")
            try:
                source_fd = os.open(name, os.O_RDONLY | no_follow | non_block, dir_fd=directory_fd)
            except OSError as exc:
                raise CheckerCommandRejected(f"workspace entry changed during copy: {label}") from exc
            try:
                opened_metadata = os.fstat(source_fd)
                if not stat.S_ISREG(opened_metadata.st_mode):
                    raise CheckerCommandRejected(f"workspace contains a non-regular file: {label}")
                with os.fdopen(source_fd, "rb", closefd=False) as source_file, (target_directory / name).open("xb") as target_file:
                    while chunk := source_file.read(1024 * 1024):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("workspace copy timed out")
                        copied_bytes += len(chunk)
                        if copied_bytes > max_bytes:
                            raise CheckerCommandRejected("workspace exceeds the sandbox byte limit")
                        target_file.write(chunk)
                (target_directory / name).chmod(stat.S_IMODE(opened_metadata.st_mode))
            finally:
                os.close(source_fd)


def _open_workspace_document(root: Path, relative: Path) -> int:
    """Open a relative regular file without following any path component."""
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise CheckerCommandRejected("artifact path must be a normalized relative path")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    non_block = getattr(os, "O_NONBLOCK", 0)
    directory_fd = os.open(root, os.O_RDONLY | directory_flag | no_follow)
    try:
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | directory_flag | no_follow, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(relative.parts[-1], os.O_RDONLY | no_follow | non_block, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO}:
                raise CheckerCommandRejected("artifact path contains a link or non-regular entry") from exc
            raise
    finally:
        os.close(directory_fd)
    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        raise CheckerCommandRejected("artifact is not a regular file")
    return file_fd


class RegisteredChecker(Protocol):
    checker_id: str
    version: str

    def build_command(self, sandbox_workspace: str, **options: Any) -> Sequence[str]: ...


def _result(
    checker_id: str,
    *,
    outcome: str,
    assertions: Mapping[str, Any] | None = None,
    validity: str = "valid",
    reason: str | None = None,
    checker_version: str = "1.0.0",
    parser_version: int = 1,
    trust_level: str = "trusted",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checker_id": checker_id,
        "checker_version": checker_version,
        "parser_version": parser_version,
        "trust_level": trust_level,
        "lifecycle": "finished",
        "outcome": outcome,
        "validity": validity,
        "assertions": dict(assertions or {}),
        "reason": reason,
    }
    payload.update(extra)
    return payload


class GradleSummaryChecker:
    checker_id = "gradle-summary"
    version = "1.0.0"
    parser_version = 1

    _SUMMARY_RE = re.compile(
        r"(?P<total>\d+)\s+tests?\s+completed(?:,\s*(?P<failed>\d+)\s+failed)?(?:,\s*(?P<skipped>\d+)\s+skipped)?",
        re.IGNORECASE,
    )
    _ALT_SUMMARY_RE = re.compile(
        r"Tests\s+run:\s*(?P<total>\d+)\s*,\s*Failures:\s*(?P<failed>\d+)(?:\s*,\s*(?:Skipped|Ignored):\s*(?P<skipped>\d+))?",
        re.IGNORECASE,
    )
    _ENVIRONMENT_PATTERNS = (
        re.compile(r"JAVA_HOME\s+(?:is not set|is set to an invalid)", re.IGNORECASE),
        re.compile(r"java:\s*(?:command not found|not found)", re.IGNORECASE),
        re.compile(r"could not find java", re.IGNORECASE),
        re.compile(r"unable to (?:start|create).*(?:daemon|process)", re.IGNORECASE),
        re.compile(r"permission denied", re.IGNORECASE),
        re.compile(r"no space left on device", re.IGNORECASE),
        re.compile(r"could not resolve (?:all )?(?:files|dependencies)", re.IGNORECASE),
    )

    def parse(
        self,
        stdout: str,
        stderr: str = "",
        exit_code: int = 0,
        *,
        skipped_policy: str = "declared",
    ) -> dict[str, Any]:
        text = f"{stdout or ''}\n{stderr or ''}"
        raw_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        summaries = list(self._SUMMARY_RE.finditer(text)) or list(self._ALT_SUMMARY_RE.finditer(text))
        if not summaries:
            environment_error = any(pattern.search(text) for pattern in self._ENVIRONMENT_PATTERNS)
            return _result(
                self.checker_id,
                outcome="infrastructure-error" if environment_error else "parse-error",
                validity="environment-mismatch" if environment_error else "valid",
                reason="gradle-environment-error" if environment_error else "test-summary-missing",
                checker_version=self.version,
                parser_version=self.parser_version,
                exit_code=exit_code,
                raw_result_sha256=raw_hash,
            )
        total = sum(int(match.group("total") or 0) for match in summaries)
        failed = sum(int(match.group("failed") or 0) for match in summaries)
        skipped = sum(int(match.group("skipped") or 0) for match in summaries)
        executed = total - skipped
        assertions = {
            "total": total,
            "failed": failed,
            "skipped": skipped,
            "executed": executed,
            "skippedPolicy": skipped_policy,
        }
        if total <= 0 or executed <= 0:
            return _result(
                self.checker_id,
                outcome="parse-error",
                assertions=assertions,
                reason="all-tests-skipped" if total > 0 else "empty-test-suite",
                checker_version=self.version,
                parser_version=self.parser_version,
                exit_code=exit_code,
                raw_result_sha256=raw_hash,
            )
        if failed > 0:
            outcome, reason = "assertion-fail", "tests-failed"
        elif skipped > 0 and skipped_policy == "forbid":
            outcome, reason = "assertion-fail", "tests-skipped"
        elif exit_code != 0:
            # A non-zero process with a clean summary may have failed after the
            # tests (reporting, daemon, or another task), so it is not an
            # assertion failure.
            outcome, reason = "infrastructure-error", "gradle-process-failed"
        elif any(pattern.search(text) for pattern in self._ENVIRONMENT_PATTERNS):
            outcome, reason = "infrastructure-error", "gradle-environment-error"
        else:
            outcome, reason = "assertion-pass", None
        return _result(
            self.checker_id,
            outcome=outcome,
            assertions=assertions,
            reason=reason,
            checker_version=self.version,
            parser_version=self.parser_version,
            exit_code=exit_code,
            raw_result_sha256=raw_hash,
        )

    check = parse

    def build_command(self, sandbox_workspace: str, **options: Any) -> Sequence[str]:
        tasks = options.get("tasks", ["test"])
        if isinstance(tasks, str):
            tasks = [tasks]
        if not isinstance(tasks, (list, tuple)) or not tasks:
            raise CheckerCommandRejected("Gradle tasks must be a non-empty list")
        safe_tasks: list[str] = []
        for task in tasks:
            value = str(task)
            if not re.fullmatch(r"[A-Za-z0-9_:.-]+", value) or value.startswith("-"):
                raise CheckerCommandRejected(f"unsafe Gradle task: {value}")
            safe_tasks.append(value)
        return [f"{sandbox_workspace}/gradlew", "--no-daemon", "--console=plain", *safe_tasks]


class DocumentArtifactChecker:
    checker_id = "document-artifact"
    version = "1.0.0"
    parser_version = 1

    def check(
        self,
        workspace: str | Path,
        path: str | Path,
        *,
        min_bytes: int = 1,
        required_text: Sequence[str] = (),
        allowed_extensions: Sequence[str] = (),
        max_bytes: int = 10_000_000,
    ) -> dict[str, Any]:
        root = Path(workspace).expanduser().resolve()
        relative = Path(path)
        try:
            document_fd = _open_workspace_document(root, relative)
        except FileNotFoundError:
            return _result(
                self.checker_id,
                outcome="assertion-fail",
                assertions={"total": 1, "exists": False},
                reason="document-missing",
                checker_version=self.version,
                parser_version=self.parser_version,
                artifact_path=str(relative),
            )
        except CheckerCommandRejected as exc:
            return _result(
                self.checker_id,
                outcome="blocked",
                validity="untrusted",
                reason="unsafe-artifact-path",
                checker_version=self.version,
                parser_version=self.parser_version,
                error=str(exc),
            )
        except OSError as exc:
            return _result(
                self.checker_id,
                outcome="infrastructure-error",
                validity="environment-mismatch",
                reason="document-unreadable",
                checker_version=self.version,
                parser_version=self.parser_version,
                error=str(exc),
            )
        assertions: dict[str, Any] = {"total": 1, "exists": True}
        try:
            with os.fdopen(document_fd, "rb") as document:
                size = os.fstat(document_fd).st_size
                if size > int(max_bytes):
                    return _result(
                        self.checker_id,
                        outcome="infrastructure-error",
                        assertions=assertions,
                        reason="document-size-limit-exceeded",
                        checker_version=self.version,
                        parser_version=self.parser_version,
                    )
                raw_content = document.read(int(max_bytes) + 1)
                if len(raw_content) > int(max_bytes):
                    return _result(
                        self.checker_id,
                        outcome="infrastructure-error",
                        assertions=assertions,
                        reason="document-size-limit-exceeded",
                        checker_version=self.version,
                        parser_version=self.parser_version,
                    )
            content = raw_content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            return _result(
                self.checker_id,
                outcome="infrastructure-error",
                validity="environment-mismatch",
                assertions=assertions,
                reason="document-unreadable",
                checker_version=self.version,
                parser_version=self.parser_version,
                error=str(exc),
            )
        suffixes = {str(item).lower() if str(item).startswith(".") else f".{str(item).lower()}" for item in allowed_extensions}
        extension_ok = not suffixes or relative.suffix.lower() in suffixes
        text_missing = [text for text in required_text if text not in content]
        assertions.update(
            {
                "size": size,
                "minBytes": int(min_bytes),
                "extensionAllowed": extension_ok,
                "requiredTextCount": len(required_text),
                "requiredTextMissing": len(text_missing),
            }
        )
        if size < int(min_bytes):
            outcome, reason = "assertion-fail", "document-too-small"
        elif not extension_ok:
            outcome, reason = "assertion-fail", "document-extension-not-allowed"
        elif text_missing:
            outcome, reason = "assertion-fail", "required-text-missing"
        else:
            outcome, reason = "assertion-pass", None
        return _result(
            self.checker_id,
            outcome=outcome,
            assertions=assertions,
            reason=reason,
            checker_version=self.version,
            parser_version=self.parser_version,
            artifact_path=str(relative),
            artifact_sha256=hashlib.sha256(raw_content).hexdigest(),
        )

    def build_command(self, sandbox_workspace: str, **options: Any) -> Sequence[str]:
        # This checker normally runs in-process against a captured artifact.
        # There is intentionally no shell escape hatch for arbitrary commands.
        raise CheckerCommandRejected("document-artifact does not define an active rerun command")


class BubblewrapCheckerRunner:
    """Run commands built exclusively by registered checkers in bubblewrap."""

    def __init__(
        self,
        checkers: Sequence[RegisteredChecker] = (),
        *,
        bwrap_path: str | Path | None = None,
        timeout_seconds: float = 120,
        max_output_bytes: int = 1_000_000,
        memory_limit_bytes: int = 1_073_741_824,
        process_limit: int = 64,
        max_workspace_bytes: int = 1_073_741_824,
        max_workspace_files: int = 100_000,
    ):
        detected = str(bwrap_path) if bwrap_path is not None else shutil.which("bwrap")
        self.bwrap_path = detected
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        self.memory_limit_bytes = int(memory_limit_bytes)
        self.process_limit = int(process_limit)
        self.max_workspace_bytes = int(max_workspace_bytes)
        self.max_workspace_files = int(max_workspace_files)
        self._checkers: dict[str, RegisteredChecker] = {}
        for checker in checkers:
            self.register(checker)

    @property
    def available(self) -> bool:
        return bool(
            self.bwrap_path
            and Path(self.bwrap_path).is_file()
            and os.access(self.bwrap_path, os.X_OK)
            and os.name == "posix"
            and resource
        )

    def register(self, checker: RegisteredChecker) -> None:
        checker_id = str(getattr(checker, "checker_id", "")).strip()
        if not checker_id or not callable(getattr(checker, "build_command", None)):
            raise CheckerRegistrationError("checker must define checker_id and build_command")
        if checker_id in self._checkers:
            raise CheckerRegistrationError(f"checker already registered: {checker_id}")
        self._checkers[checker_id] = checker

    def _limits(self) -> None:
        if resource is None:
            return
        resource.setrlimit(resource.RLIMIT_AS, (self.memory_limit_bytes, self.memory_limit_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (self.process_limit, self.process_limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (self.max_output_bytes, self.max_output_bytes))
        os.setsid()

    @staticmethod
    def _base_bwrap_command(bwrap_path: str, workspace: Path) -> list[str]:
        command = [
            bwrap_path,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--unshare-net",
            "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/tmp/home",
            "--setenv", "LANG", "C.UTF-8",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--dir", "/tmp/home",
            "--bind", str(workspace), "/workspace",
            "--chdir", "/workspace",
        ]
        for host_path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(host_path).exists():
                command.extend(("--ro-bind", host_path, host_path))
        return command

    def run(
        self,
        checker_id: str,
        workspace: str | Path,
        *,
        timeout_seconds: float | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        if "command" in options or "cmd" in options or "shell" in options:
            raise CheckerCommandRejected("arbitrary commands are not accepted")
        checker = self._checkers.get(checker_id)
        if checker is None:
            raise CheckerCommandRejected(f"checker is not registered: {checker_id}")
        source = Path(workspace).expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"workspace is not a directory: {source}")
        if not self.available:
            return _result(
                checker_id,
                outcome="blocked",
                validity="environment-mismatch",
                reason="bwrap-unavailable",
                checker_version=str(getattr(checker, "version", "unknown")),
                sandbox="unavailable",
                executed=False,
            )
        run_timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        deadline = time.monotonic() + max(0.0, float(run_timeout))
        with tempfile.TemporaryDirectory(prefix="outcome-check-") as temporary:
            isolated_workspace = Path(temporary) / "workspace"
            try:
                _copy_workspace_secure(
                    source,
                    isolated_workspace,
                    self.max_workspace_bytes,
                    self.max_workspace_files,
                    deadline,
                )
            except TimeoutError:
                return _result(
                    checker_id,
                    outcome="timeout",
                    reason="workspace-copy-timeout",
                    checker_version=str(getattr(checker, "version", "unknown")),
                    sandbox="bwrap",
                    executed=False,
                )
            command = list(checker.build_command("/workspace", **options))
            if not command or not all(isinstance(item, str) and item for item in command):
                raise CheckerCommandRejected("checker produced an invalid command")
            full_command = [*self._base_bwrap_command(str(self.bwrap_path), isolated_workspace), "--", *command]
            started = time.monotonic()
            with tempfile.TemporaryFile() as output_file:
                try:
                    process = subprocess.Popen(
                        full_command,
                        stdin=subprocess.DEVNULL,
                        stdout=output_file,
                        stderr=subprocess.STDOUT,
                        env={},
                        text=False,
                        preexec_fn=self._limits,
                    )
                except OSError as exc:
                    return _result(
                        checker_id,
                        outcome="blocked",
                        validity="environment-mismatch",
                        reason="bwrap-unavailable",
                        checker_version=str(getattr(checker, "version", "unknown")),
                        sandbox="unavailable",
                        executed=False,
                        error=str(exc),
                    )
                timed_out = False
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    process.communicate(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                output_file.seek(0)
                stdout_raw = output_file.read(self.max_output_bytes + 1)
            truncated = len(stdout_raw) > self.max_output_bytes or process.returncode == -signal.SIGXFSZ
            stdout = stdout_raw[: self.max_output_bytes].decode("utf-8", errors="replace")
            stderr = ""
            if timed_out:
                outcome, reason = "timeout", "checker-timeout"
            elif truncated:
                outcome, reason = "infrastructure-error", "output-limit-exceeded"
            elif callable(getattr(checker, "parse", None)):
                try:
                    parser_options = {
                        key: options[key]
                        for key in getattr(checker, "parser_option_names", ("skipped_policy",))
                        if key in options
                    }
                    parsed = checker.parse(stdout, stderr, process.returncode, **parser_options)  # type: ignore[attr-defined]
                except Exception as exc:
                    parsed = _result(
                        checker_id,
                        outcome="parse-error",
                        reason="checker-parser-error",
                        checker_version=str(getattr(checker, "version", "unknown")),
                        parser_error=str(exc),
                    )
                outcome, reason = str(parsed["outcome"]), parsed.get("reason")
            elif process.returncode == 0:
                # The parser must establish assertions. Runner success alone
                # is never deterministic evidence of task success.
                outcome, reason = "parse-error", "checker-output-not-parsed"
            else:
                outcome, reason = "infrastructure-error", "checker-process-failed"
            runner_fields = {
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "output_truncated": truncated,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "sandbox": "bwrap",
                "network": "disabled",
                "workspace_mode": "temporary-writable-copy",
                "executed": True,
            }
            if not timed_out and not truncated and callable(getattr(checker, "parse", None)):
                parsed.update(runner_fields)
                return parsed
            return _result(
                checker_id,
                outcome=outcome,
                reason=reason,
                checker_version=str(getattr(checker, "version", "unknown")),
                **runner_fields,
            )


IsolatedCheckerRunner = BubblewrapCheckerRunner
