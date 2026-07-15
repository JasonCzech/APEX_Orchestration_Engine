"""Resource-limited subprocess entry point for context-document PDF parsing."""

from __future__ import annotations

import sys


def _positive_int(value: str, *, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    if parsed < 1:
        raise ValueError(f"invalid {label}")
    return parsed


def _apply_resource_limits(*, memory_bytes: int, cpu_seconds: int) -> None:
    """Apply hard child-only limits before importing pypdf or reading input."""

    import resource

    # RLIMIT_AS is reliable in the Linux deployment target. macOS accounts
    # shared-library address reservations differently, so decoded pypdf caps and
    # the parent wall deadline remain its deterministic envelope.
    if sys.platform.startswith("linux"):
        _soft_as, hard_as = resource.getrlimit(resource.RLIMIT_AS)
        effective_memory = (
            memory_bytes if hard_as == resource.RLIM_INFINITY else min(memory_bytes, int(hard_as))
        )
        resource.setrlimit(resource.RLIMIT_AS, (effective_memory, effective_memory))
    _soft_cpu, hard_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    effective_hard_cpu = (
        cpu_seconds + 1
        if hard_cpu == resource.RLIM_INFINITY
        else min(cpu_seconds + 1, int(hard_cpu))
    )
    effective_soft_cpu = min(cpu_seconds, effective_hard_cpu)
    resource.setrlimit(resource.RLIMIT_CPU, (effective_soft_cpu, effective_hard_cpu))


def _write_error(detail: str) -> None:
    sys.stdout.buffer.write(b"E" + detail.encode("utf-8", errors="replace")[:1_000])
    sys.stdout.buffer.flush()


def main() -> int:
    if len(sys.argv) != 5:
        _write_error("PDF extraction worker received invalid arguments")
        return 0
    try:
        max_chars = int(sys.argv[1])
        memory_bytes = _positive_int(sys.argv[2], label="memory limit")
        cpu_seconds = _positive_int(sys.argv[3], label="CPU limit")
        input_limit = _positive_int(sys.argv[4], label="input limit")
        _apply_resource_limits(memory_bytes=memory_bytes, cpu_seconds=cpu_seconds)
        data = sys.stdin.buffer.read(input_limit + 1)
        if len(data) > input_limit:
            raise ValueError("PDF extraction input exceeds its hard limit")

        # Import only after the address-space limit is active. This module is a
        # standalone executable so it does not re-import the full API process.
        from apex.services.text_extraction import _extract_pdf_in_worker

        extracted = _extract_pdf_in_worker(data, max_chars=max_chars)
        text = extracted.text.encode("utf-8", errors="replace")
        sys.stdout.buffer.write(b"S" + extracted.char_count.to_bytes(8, "big") + text)
        sys.stdout.buffer.flush()
        return 0
    except Exception as exc:
        _write_error(str(exc)[:1_000] or exc.__class__.__name__)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
