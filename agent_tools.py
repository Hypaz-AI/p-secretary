"""Read-only tool registry for the agent's bounded retrieval planner.

Every tool is a declared, server-side capability: the model may only NAME a tool
and supply typed arguments; it can never supply code, SQL, paths, URLs, or its
own provenance/authorization. Results are filtered through the existing native
consent gates (owner_dialogue.filtered_knowledge / filter_context_rows) INSIDE
each tool, so a result cannot leave unfiltered even if a caller forgets to.

Design invariants (mirroring the project's core principle):
* No write tools. No email/calendar/notify dispatch. Effects stay behind the
  plan→apply gateway; this registry only widens the READ surface.
* Each call rechecks the job lease (jobs.assert_current) and the per-source
  consent gates; nothing is cached across calls.
* Arguments are validated strictly (exact keys, exact types, bounded sizes);
  unknown tools or malformed arguments fail closed with a typed error the
  planner can report, never an exception that aborts the dialogue.
* Tool results are untrusted DATA for the model, never instructions; the
  planner prompt keeps saying so.

This module imports no connector and performs no network I/O. It only calls
knowledge/db/secretary read helpers that already exist and are already gated.
"""
import json
import logging

import db
import jobs

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 2
MAX_TOOL_RESULT_CHARS = 6000


class ToolError(ValueError):
    """Typed planner-visible error: unknown tool, bad arguments, denied source."""


def _exact(request, keys):
    if type(request) is not dict or set(request) != set(keys):
        raise ToolError('Invalid tool arguments')


def _bounded_text(value, limit):
    if type(value) is not str or not 0 < len(value) <= limit:
        raise ToolError('Invalid tool argument')
    return value


def _positive_id(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ToolError('Invalid tool argument')
    return value


# --------------------------------------------------------------------------
# Tool implementations. Each one: validate args → assert lease → read → filter.
# --------------------------------------------------------------------------

def _tool_search(request):
    """Lexical knowledge search; results are consent-filtered snippets."""
    import knowledge
    import owner_dialogue
    _exact(request, ('tool', 'query'))
    query = _bounded_text(request['query'], 300)
    jobs.assert_current()
    hits = owner_dialogue.filtered_knowledge(knowledge.search(query, limit=4))
    return dict(tool='search', query=query, results=hits)


def _tool_open(request):
    """Open an allowlisted source by native ID; consent-gated per call."""
    import knowledge
    import owner_dialogue
    _exact(request, ('tool', 'source_type', 'source_id'))
    kind = request['source_type']
    if type(kind) is not str or kind not in ('message', 'document', 'entry', 'note', 'practice'):
        raise ToolError('Invalid tool argument')
    identity = _positive_id(request['source_id'])
    jobs.assert_current()
    source = owner_dialogue.filtered_knowledge(knowledge.open_source(kind, identity))
    return dict(tool='open', source_type=kind, source_id=identity, source=source)


def _tool_tasks(request):
    """Open owner tasks (bounded); rows are consent-filtered per call."""
    from datetime import datetime, timezone
    import owner_dialogue
    _exact(request, ('tool',))
    jobs.assert_current()
    until = datetime.now(timezone.utc)
    rows = owner_dialogue.filter_context_rows(db.pending_tasks(until), 'task')
    return dict(tool='tasks', tasks=rows[:40])


def _tool_appointments(request):
    """Upcoming appointments (bounded); rows are consent-filtered per call."""
    import owner_dialogue
    _exact(request, ('tool',))
    jobs.assert_current()
    rows = owner_dialogue.filter_context_rows(db.upcoming_appointments(days=30), 'appointment')
    return dict(tool='appointments', appointments=rows[:40])


def _tool_practices(request):
    """Recent practices/responsibilities snapshot; consent-filtered per call."""
    import owner_dialogue
    import secretary
    _exact(request, ('tool',))
    jobs.assert_current()
    snapshot = owner_dialogue.filtered_practices(secretary.context())
    return dict(tool='practices', **snapshot)


REGISTRY = {
    'search': _tool_search,
    'open': _tool_open,
    'tasks': _tool_tasks,
    'appointments': _tool_appointments,
    'practices': _tool_practices,
}

TOOL_NAMES = tuple(sorted(REGISTRY))


def run(request):
    """Validate and run one declared read-only tool; never executes model code.

    Returns a plain dict result (untrusted data for the model). Raises ToolError
    for unknown tools or invalid arguments; PermissionError/Deferred propagate
    from the consent gates so callers can defer or abort as appropriate.
    """
    if type(request) is not dict or type(request.get('tool')) is not str:
        raise ToolError('Invalid tool request')
    handler = REGISTRY.get(request['tool'])
    if handler is None:
        raise ToolError('Unknown tool')
    result = handler(request)
    bounded = json.dumps(result, ensure_ascii=False, default=str)
    if len(bounded) > MAX_TOOL_RESULT_CHARS * 3:
        logger.warning('Tool result exceeded budget: %s', request['tool'])
        raise ToolError('Tool result too large')
    return result
