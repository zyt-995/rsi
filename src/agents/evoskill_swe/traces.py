"""Render long Harbor task traces using the native proposer's fallback limits."""
from src.harness import AgentTrace
from src.schemas import AgentResponse


class HarborTaskTrace(AgentTrace[AgentResponse]):
    """Keep the full recorded trace; shorten only a reduced proposer view.

    A parsed verifier envelope can describe a failed task. The native trace
    formatter only reduces parse failures, so it otherwise ignores the fallback
    limits for these Harbor trajectories. Level zero remains the native full
    view; the native helper's smaller head/tail limits reduce the rendered view.
    """

    def summarize(self, head_chars: int = 60_000, tail_chars: int = 60_000) -> str:
        if head_chars < 0 or tail_chars < 0:
            raise ValueError("trace summary limits must be nonnegative")
        text = str(self.result) if self.result else ""
        if (self.parse_error or (head_chars, tail_chars) == (60_000, 60_000)
                or len(text) <= head_chars + tail_chars):
            return super().summarize(head_chars=head_chars, tail_chars=tail_chars)

        # Reuse the native metadata/output rendering without changing the real
        # trace, its parse status, or the complete result stored on disk.
        metadata = AgentTrace.summarize(self.model_copy(update={"result": ""}))
        metadata = metadata.removesuffix("\n## Full Result\n")
        omitted = len(text) - head_chars - tail_chars
        tail = text[-tail_chars:] if tail_chars else ""
        return "\n".join([
            metadata,
            f"\n## Result (truncated, {omitted:,} chars omitted)",
            f"### Start:\n{text[:head_chars]}",
            f"\n[... {omitted:,} characters truncated ...]\n",
            f"### End:\n{tail}",
        ])
