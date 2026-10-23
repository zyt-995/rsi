"""Exact paid-session continuation after local context admission; never replay a proposer."""
from pathlib import Path
import time
from swe_chain_evo.common import read, sha


def context_manifest(output):
    path = Path(output) / 'context-proposer-resume.json'
    return read(path) if path.exists() else None


def validate_pending_context_recovery(root):
    root = Path(root)
    saved = context_manifest(root / 'arms/evoskill/steps/000003/update')
    if not saved:
        return False
    state = read(root / 'native-campaign.json')
    item = state['arms']['evoskill']
    return (saved['root'] == str(root) and saved['run_created'] == state['created']
            and saved['configuration_sha256'] == sha(root / 'configuration.json')
            and len(item['rows']) == 4 and item['pending_update'] == 3
            and not item['updates']
            and sha(root / 'first-update-acceptance.json') == saved['old_gate_sha256']
            and sha(Path(saved['old_outcome_archive'])) == saved['old_outcome_sha256'])


def checked_resume(runner, output, target, identity, role):
    saved = context_manifest(output)
    if not saved or role != 'skill_proposer':
        return None
    if (saved['root'] != str(runner.root)
            or saved['configuration_sha256'] != sha(runner.root / 'configuration.json')
            or saved['target'] != str(target.relative_to(runner.root))
            or sha(target / 'identity.json') != saved['identity_sha256']
            or read(target / 'identity.json') != identity
            or sha(target / 'trace.json') != saved['trace_sha256']):
        raise RuntimeError('exact paid proposer continuation identity changed')
    previous = read(target / 'trace.json')
    last = previous['messages'][-1]
    if (not previous['is_error'] or previous['output'] is not None
            or last.get('api_error_status') != 400
            or last.get('terminal_reason') != 'api_error'
            or '262144' not in previous['result'] or '262429' not in previous['result']
            or previous['session_id'] != saved['session_id']):
        raise RuntimeError('not the audited pre-dispatch context admission failure')
    if not Path(saved['session_file']).is_file():
        raise RuntimeError('original paid CLI session missing; no fresh replay allowed')
    return saved


def existing_scope(runner, saved, target):
    matching = [(token, scope) for token, scope in runner.broker.scopes.items()
                if scope['name'] == saved['scope_name']]
    if len(matching) != 1:
        raise RuntimeError('original paid proposer scope not uniquely identified')
    token, scope = matching[0]
    if scope['blocked'] or scope['pending_tokens'] or scope['calls'] != saved['prior_call_ids']:
        raise RuntimeError('paid proposer scope changed; inspect original continuation')
    if sha(Path(saved['session_file'])) != saved['session_file_sha256_before']:
        raise RuntimeError('original CLI session changed before first continuation')
    return token


def configure_context(options, config, saved=None):
    # The server still enforces exactly context262144/output<=65536. This tells
    # the native CLI its real window and triggers its own compaction before the
    # configured output allowance would make an otherwise valid request fail.
    window = config['model']['context_tokens']
    options.env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] = str(window)
    options.env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] = str(window - config['model']['max_output_tokens'])
    if saved:
        remaining_turns = config['arms']['evoskill']['max_meta_turns'] - saved['used_turns']
        remaining_seconds = config['arms']['evoskill']['meta_timeout_seconds'] - saved['used_active_seconds']
        if remaining_turns < 1 or remaining_seconds <= 0:
            raise RuntimeError('original proposer stage budget exhausted')
        options.resume = saved['session_id']
        options.max_turns = remaining_turns
        return remaining_seconds
    return None


def empty_resume_notification(message):
    return (getattr(message, 'num_turns', None) == 0
            and not getattr(message, 'is_error', False)
            and not getattr(message, 'result', None)
            and getattr(message, 'structured_output', None) is None
            and getattr(message, 'terminal_reason', None) is None)


async def execute_resumed_query(options, query):
    import copy
    from dataclasses import is_dataclass, replace
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage
    messages = []
    compact_options = copy.copy(options)
    compact_options.env = dict(options.env)
    # Only the native history-summary control command uses a smaller reply.
    # The original configured65536 output cap is restored for the proposal.
    compact_options.env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = '16384'
    compact_result = None
    boundary = False
    async with ClaudeSDKClient(compact_options) as client:
        await client.query("/compact")
        async for message in client.receive_messages():
            if getattr(message, 'subtype', None) == 'compact_boundary':
                boundary = True
            if isinstance(message, ResultMessage):
                if empty_resume_notification(message) and not boundary:
                    continue
                messages.append(message)
                compact_result = message
                if getattr(message, 'is_error', False) or getattr(message, 'terminal_reason', None) == 'api_error':
                    return messages
                break
            messages.append(message)
    if not boundary or compact_result is None:
        raise RuntimeError('Original SDK session did not confirm native compact boundary; preserve before retry')
    options.max_turns -= compact_result.num_turns
    if options.max_turns < 1:
        raise RuntimeError('Original stage turn budget exhausted after native context summary')
    async with ClaudeSDKClient(options) as client:
        await client.query(query)
        async for message in client.receive_messages():
            if isinstance(message, ResultMessage) and empty_resume_notification(message):
                continue
            if isinstance(message, ResultMessage):
                totals = dict(num_turns=message.num_turns + compact_result.num_turns,
                              duration_ms=message.duration_ms + compact_result.duration_ms)
                if is_dataclass(message):
                    message = replace(message, **totals)
                else:
                    message = copy.copy(message)
                    message.__dict__.update(totals)
                messages.append(message)
                break
            messages.append(message)
    return messages
