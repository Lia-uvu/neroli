from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol


def clean_model_text(text: str) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)


class ModelRunner(Protocol):
    name: str

    def run(self, prompt: str) -> str:
        ...


class CLIModel:
    """通过任意 headless CLI 调用模型（如 codex exec / claude -p）。

    command 是完整命令行模板，prompt 从 stdin 喂入，回复从 stdout 读取。
    例：--provider cli --model gpt-5.4-mini
    订阅制通道可能限流，失败按 30s/60s/120s 退避重试三次。
    """

    RETRY_DELAYS = (30, 60, 120)

    def __init__(self, command: str, timeout: float = 300.0, cwd: str | None = None,
                 success_artifact: str | None = None) -> None:
        self.name = f"cli:{command}"
        self.argv = shlex.split(command)
        self.timeout = timeout
        self.cwd = cwd  # agentic CLI（codex）以此为工作区根：curator 的工作台目录
        self.success_artifact = (
            (Path(cwd) / success_artifact) if cwd and success_artifact
            else (Path(success_artifact) if success_artifact else None)
        )
        self.last_attempts: list[dict[str, object]] = []

    def _artifact_ready(self) -> bool:
        return bool(self.success_artifact and self.success_artifact.is_file())

    def _artifact_result(self) -> str:
        assert self.success_artifact is not None
        return f"(success artifact: {self.success_artifact.name})"

    def run(self, prompt: str) -> str:
        self.last_attempts = []
        last_error: Exception | None = None
        for attempt, delay in enumerate((0,) + self.RETRY_DELAYS, 1):
            if delay:
                time.sleep(delay)
            try:
                result = subprocess.run(
                    self.argv,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout,
                    cwd=self.cwd,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
                stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
                self.last_attempts.append({
                    "attempt": attempt, "status": "timeout", "returncode": None,
                    "stdout": clean_model_text(stdout), "stderr": clean_model_text(stderr)[:2000],
                })
                # subprocess.run 已终止超时子进程；如果它在被终止前已完成协议提交，
                # submission 才是成功信号，不应再付费重跑。
                if self._artifact_ready():
                    return self._artifact_result()
                last_error = RuntimeError(f"CLI model timed out after {self.timeout}s")
                continue
            self.last_attempts.append({
                "attempt": attempt,
                "status": "exit_0" if result.returncode == 0 else "exit_nonzero",
                "returncode": result.returncode,
                "stdout": clean_model_text(result.stdout),
                "stderr": clean_model_text(result.stderr)[:2000],
            })
            # artifact 已由上层复验；只要本轮新产物存在，就优先于 CLI 的收尾退出码。
            # 这样 submit 成功后 telemetry/cleanup 报错也不会把同一任务再跑四次。
            if self._artifact_ready():
                return self._artifact_result()
            if result.returncode == 0:
                if result.stdout.strip():
                    return clean_model_text(result.stdout)
                # curator 的 agent 通过 ./submit 把已门审结果落成 submission.json 后，
                # 会遵照 prompt 不再输出 final。这个文件由每轮导出工作台时先删除，
                # 因而 rc=0 + 新产物存在就是本轮成功；上层仍会复验内容和 constant_id。
            last_error = RuntimeError(
                f"CLI model exited {result.returncode}: {result.stderr.strip()[:500] or 'empty output'}"
            )
        raise last_error or RuntimeError("CLI model failed")


def model_attempts(model: ModelRunner) -> list[dict[str, object]]:
    """返回最近一次 run 的物理 CLI 尝试；无内部重试信息的 runner 返回空。"""
    attempts = getattr(model, "last_attempts", None)
    return list(attempts) if isinstance(attempts, list) else []


def _codex_bin() -> str:
    """codex 可执行文件：$CLAUDE_MEMORY_CODEX_BIN 显式指定 > PATH > macOS 应用内置路径。"""
    override = os.environ.get("CLAUDE_MEMORY_CODEX_BIN")
    if override:
        return override
    found = shutil.which("codex")
    if found:
        return found
    for app_path in (
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
    ):
        if os.path.exists(app_path):
            return app_path
    return "codex"


def _build_codex_cmd_with_effort(model_name: str, reasoning_effort: str = "low",
                                 sandbox: str = "read-only") -> str:
    """sandbox：card-gen 等只读任务用默认 read-only；curator 要跑 ./submit 落
    submission.json，用 workspace-write（写权限只有 cwd，即工作台目录）。"""
    codex_bin = _codex_bin()
    return " ".join(
        [
            shlex.quote(codex_bin),
            "exec",
            "--model",
            shlex.quote(model_name),
            "-c",
            shlex.quote(f'model_reasoning_effort="{reasoning_effort}"'),
            "-c",
            shlex.quote("tools.web_search=false"),
            "-c",
            shlex.quote("tools.image_gen=false"),
            "--skip-git-repo-check",
            "--sandbox",
            sandbox,
            "--ephemeral",
            "-",
        ]
    )


def default_codex_command(model_name: str) -> str:
    codex_bin = _codex_bin()
    return " ".join(
        [
            shlex.quote(codex_bin),
            "exec",
            "--model",
            shlex.quote(model_name),
            "-c",
            shlex.quote('model_reasoning_effort="none"'),
            "-c",
            shlex.quote("tools.web_search=false"),
            "-c",
            shlex.quote("tools.image_gen=false"),
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "-",
        ]
    )


class OllamaModel:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, prompt: str) -> str:
        result = subprocess.run(
            ["ollama", "run", self.name],
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"ollama exited {result.returncode}")
        return clean_model_text(result.stdout)


class APIChatModel:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def run(self, prompt: str) -> str:
        payload: dict[str, object] = {
            "model": self.name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"API request failed with HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"API request failed: {error.reason}") from error

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError(f"API response did not contain message content: {body}") from error
        return clean_model_text(text or "")


def build_model(
    provider: str,
    name: str,
    *,
    api_base_url: str | None = None,
    api_key_env: str = "CLAUDE_MEMORY_API_KEY",
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float = 120.0,
    model_cmd: str | None = None,
    cwd: str | None = None,
    success_artifact: str | None = None,
) -> ModelRunner:
    if provider == "cli":
        command = model_cmd or os.environ.get("CLAUDE_MEMORY_MODEL_CMD") or default_codex_command(name)
        return CLIModel(command, timeout=max(timeout, 300.0), cwd=cwd,
                        success_artifact=success_artifact)
    if provider == "ollama":
        return OllamaModel(name)
    if provider == "api":
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing API key. Set ${api_key_env} before using --provider api.")
        if not api_base_url:
            raise RuntimeError("Missing API base URL. Pass --api-base-url or set CLAUDE_MEMORY_API_BASE_URL.")
        return APIChatModel(
            name,
            api_base_url,
            api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    raise ValueError(f"Unsupported model provider: {provider}")
