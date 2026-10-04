"""Common budget disclosure only. Skills, retrieval and caps remain unchanged."""
from rsi_harness.clean_rsi import SOLVE
from rsi_harness.clean_terminal_agent import CleanTerminalAgent


def budget_notice(used, commands):
    return (
        "Execution budget (the same for both experimental conditions): "
        f"{max(0, SOLVE['max_model_calls'] - used)} model calls remaining, "
        "INCLUDING this response; "
        f"{max(0, SOLVE['max_commands'] - commands)} terminal commands remaining. "
        "When the budget is exhausted, grading will inspect the filesystem as it is. "
        "No additional completion turn is guaranteed.\n\n"
    )


class BudgetAwareAgent(CleanTerminalAgent):
    async def _handle_llm_interaction(self, chat, prompt, original_instruction="", session=None):
        notice = budget_notice(self.rsi_calls, self.rsi_commands)
        self.event("budget_disclosure", calls_used=self.rsi_calls,
                   calls_remaining=SOLVE['max_model_calls'] - self.rsi_calls,
                   commands_used=self.rsi_commands, notice=notice)
        return await super()._handle_llm_interaction(
            chat, notice + prompt, original_instruction, session
        )

    async def _run_agent_loop(self, *args, **kwargs):
        await super()._run_agent_loop(*args, **kwargs)
        if self.rsi_stop_reason is None:
            # Upstream returns normally at its max-turn boundary; do not confuse
            # this with the explicit two-turn task-completion handshake.
            marks = [any(c.function_name == "mark_task_complete" for c in (s.tool_calls or []))
                     for s in self._trajectory_steps if s.source == "agent"]
            confirmed = len(marks) >= 2 and all(marks[-2:])
            if confirmed:
                self.rsi_stop_reason = "agent_confirmed_complete"
            elif self.rsi_calls >= SOLVE['max_model_calls'] or self._n_episodes >= self._max_episodes:
                self.rsi_stop_reason = "budget_exhausted"
            else:
                self.rsi_stop_reason = "agent_loop_returned_without_completion"
            self.event("agent_stopped", reason=self.rsi_stop_reason,
                       model_calls=self.rsi_calls, completion_confirmed=confirmed)
