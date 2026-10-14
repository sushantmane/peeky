"""Fresh compiled builds and trusted independent checker completion.

Descriptors and manifests are evidence, not runnable retained build artifacts.
The checker is trusted developer code and shares OS permissions with the target.
"""

import hashlib
import json
import uuid
from dataclasses import asdict
from pathlib import Path

from peeky.build import BuildCommandError, SourceEdit


def _hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


class CompiledVerification:
    """Shared integrity lifecycle for fixed, trusted compiled target recipes."""

    language: str

    def __init__(self, target, root: Path):
        self.target = target
        self.root = Path(root).resolve(strict=True)
        self.source_manifest = {
            name: _hash(self.root / name) for name in target.verification_inputs
        }
        self._checks = {}

    def _protect(self, source, check):
        source, check = Path(source).resolve(strict=True), Path(check).resolve(strict=True)
        relative = source.relative_to(self.root).as_posix()
        if relative not in self.target.verification_sources:
            raise ValueError("Verification source is not a declared editable input")
        self._checks.setdefault(check, _hash(check))
        self._assert_originals()
        return source, check, relative

    def _assert_originals(self):
        try:
            unchanged = all(
                _hash(self.root / name) == digest for name, digest in self.source_manifest.items()
            ) and all(_hash(path) == digest for path, digest in self._checks.items())
        except OSError as exc:
            raise RuntimeError(
                "Original compiled source or checker changed during investigation"
            ) from exc
        if not unchanged:
            raise RuntimeError("Original compiled source or checker changed during investigation")

    def validate_launch(self, launch):
        self._assert_originals()
        if launch.get("source_manifest") != self.source_manifest:
            raise ValueError("Worker compiled source manifest differs from verification inputs")
        if launch.get("target_identity") != self.target.target_identity:
            raise ValueError(
                "Worker compiled target configuration differs from verification target"
            )

    def _descriptor(self, runtime, source):
        staged = runtime.source_path(source)
        return {
            "schema": 1,
            "language": self.language,
            "command": list(runtime.verification_command),
            "arguments": list(self.target.arguments),
            "environment": runtime.environment,
            "source": {"path": str(staged), "sha256": _hash(staged)},
            "source_manifest": self.source_manifest,
            "target_identity": self.target.target_identity,
            "artifact_manifest_id": runtime.manifest_id,
            "build_manifest": asdict(runtime.prepared.manifest),
            "artifacts_retained": False,
        }

    def _check(self, runtime, source, check, *, run_baseline=False):
        from peeky.agent import execute, verify

        self._assert_originals()
        runtime.assert_current()
        descriptor = self._descriptor(runtime, source)
        descriptor_path = runtime.prepared.workspace / "peeky-verification.json"
        descriptor_path.write_text(json.dumps(descriptor, sort_keys=True))
        descriptor_hash = _hash(descriptor_path)
        execution = None
        if run_baseline:
            execution = execute(
                [*descriptor["command"], *descriptor["arguments"]],
                runtime.prepared.workspace,
                env=runtime.environment,
            )
            runtime.assert_current()
            self._assert_originals()
            if _hash(descriptor_path) != descriptor_hash:
                raise RuntimeError("Verification descriptor changed during baseline execution")
        result = verify(check, descriptor_path, runtime.prepared.workspace, env=runtime.environment)
        runtime.assert_current()
        self._assert_originals()
        try:
            unchanged = _hash(descriptor_path) == descriptor_hash
        except OSError as exc:
            raise RuntimeError("Verification descriptor changed during checking") from exc
        if not unchanged:
            raise RuntimeError("Verification descriptor changed during checking")
        result.update(
            language=self.language,
            stage="verification",
            descriptor=descriptor,
            descriptor_sha256=descriptor_hash,
            build_manifest=descriptor["build_manifest"],
            artifact_manifest_id=runtime.manifest_id,
            artifacts_retained=False,
            checker_identity={"path": str(check), "sha256": self._checks[check]},
        )
        return execution, result

    def baseline(self, source: Path, check: Path):
        source, check, _ = self._protect(source, check)
        runtime = self.target.prepare(source, self.root)
        try:
            return self._check(runtime, source, check, run_baseline=True)
        finally:
            runtime.close()
            self._assert_originals()

    def candidate(self, source: Path, check: Path, old: str, new: str, destination: Path):
        source, check, relative = self._protect(source, check)
        original = source.read_text()
        if not old or original.count(old) != 1 or old == new:
            raise ValueError("old must match exactly once and new must change it")
        destination = Path(destination).resolve()
        candidate = destination / "candidate" / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(original.replace(old, new, 1))
        candidate_hash = _hash(candidate)
        runtime = None
        try:
            try:
                runtime = self.target.prepare(
                    source, self.root, edit=SourceEdit(relative, old, new)
                )
            except BuildCommandError as exc:
                result = {
                    **exc.result,
                    "passed": False,
                    "stage": "build",
                    "language": self.language,
                    "diagnostic": str(exc),
                    "artifacts_retained": False,
                    "checker_identity": {"path": str(check), "sha256": self._checks[check]},
                }
            else:
                if _hash(runtime.source_path(source)) != candidate_hash:
                    raise RuntimeError("Prepared compiled candidate differs from saved candidate")
                _, result = self._check(runtime, source, check)
            self._assert_originals()
            if _hash(candidate) != candidate_hash:
                raise RuntimeError("Saved compiled candidate changed during verification")
            result.update(candidate=str(candidate), candidate_sha256=candidate_hash)
            evidence = destination / f"{self.language}-build-{uuid.uuid4().hex}.json"
            result["evidence_file"] = str(evidence)
            evidence.write_text(json.dumps(result, indent=2) + "\n")
            return result
        finally:
            if runtime:
                runtime.close()
            self._assert_originals()
