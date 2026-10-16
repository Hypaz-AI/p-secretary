"""Conservative output budgets and bounded retries for OpenRouter completions."""
import json
import urllib.request

import config

ANALYSIS_TOKENS = 8192
MEMORY_TOKENS = 4096
DIGEST_TOKENS = 8192
CHAT_TOKENS = 16384
MAX_RETRY_TOKENS = 65536


class TruncatedCompletion(RuntimeError):
    """No partial completion may reach a parser or execute side effects."""


def request_content(payload, timeout=90):
    """Regenerate once from original messages, doubling the output budget.

    Only token exhaustion is retried here; transport failures stay with the
    caller/job queue. Never expose reasoning as a substitute for final content.
    """
    payload = dict(payload)
    for attempt in range(2):
        request = urllib.request.Request(
            'https://openrouter.ai/api/v1/chat/completions',
            data=json.dumps(payload).encode(), headers={
                'Authorization': f'Bearer {config.OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://pi-secretary.local',
                'X-Title': 'Pi Secretary'}, method='POST')
        with urllib.request.urlopen(request, timeout=timeout) as response:
            choice = json.loads(response.read().decode())['choices'][0]
        if (choice.get('finish_reason') in ('length', 'max_tokens')
                or choice.get('native_finish_reason') in ('MAX_TOKENS', 'max_tokens')):
            budget = payload['max_tokens']
            if attempt == 0 and budget < MAX_RETRY_TOKENS:
                payload['max_tokens'] = min(budget * 2, MAX_RETRY_TOKENS)
                continue
            raise TruncatedCompletion('Risposta AI troncata: nessun contenuto parziale accettato')
        content = choice['message'].get('content')
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError('Risposta AI senza testo finale; riprovare')
        return content.strip()