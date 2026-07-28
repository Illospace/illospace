"""Primary failure captured across a complete run-scoped unit of work."""

from __future__ import annotations


class RunExecutionFailure(RuntimeError):
    """Keep the first run exception stable across rollback and settlement."""

    def __init__(self, run_id: int, original: BaseException):
        self.run_id = int(run_id)
        self.original = original
        detail = str(original).strip() or repr(original)
        super().__init__(
            f"run_execution_failed: {type(original).__name__}: {detail}"
        )

    @classmethod
    def capture(
        cls,
        run_id: int,
        error: BaseException,
    ) -> "RunExecutionFailure":
        if isinstance(error, cls):
            return error
        if isinstance(error.__context__, cls):
            return error.__context__
        return cls(run_id, error)


__all__ = ["RunExecutionFailure"]
