"""Runtime dell'agente: Copilot SDK con OpenRouter come unico LLM.

* Un solo `CopilotClient` (un processo runtime, ~190 MB) in un thread dedicato
  con il proprio event loop; i worker (thread sincroni) lo usano con `run()`.
* `mode="empty"`: nessuno strumento ambiente (shell, file, rete, git). Sono
  abilitati SOLO gli strumenti custom del turno; qualunque altra richiesta di
  permesso è respinta (difesa in profondità: non dovrebbe mai arrivare).
* Il provider è il gateway locale (agent/gateway.py): la chiave OpenRouter non
  entra mai nel runtime e ogni richiesta passa dal ledger.
* Sessioni EFFIMERE: ciascun turno ricostruisce il contesto dal database e a
  fine turno la sessione viene cancellata, così nessun testo di terzi resta nella
  cronologia del runtime, fuori dalla portata di revoca consensi e cancellazione.
"""
import asyncio
import logging
import os
import threading
import uuid
from pathlib import Path

from secretary.core import ai_budget, config, jobs
from secretary.agent import gateway as gateway_module, toolkit

logger = logging.getLogger(__name__)
ENV_PASSTHROUGH = ('PATH', 'LANG', 'LC_ALL', 'TZ', 'COPILOT_CLI_PATH', 'COPILOT_CLI_EXTRACT_DIR',
                   'COPILOT_SKIP_CLI_DOWNLOAD')


class AgentFailed(RuntimeError):
    """Il turno non ha prodotto un risultato utilizzabile (nessun dato nel messaggio)."""


def _home():
    path = Path(config.DATA_DIR).absolute() / 'agent_runtime'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _env(home):
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    env.update(HOME=str(home / 'home'), XDG_CACHE_HOME=os.environ.get('XDG_CACHE_HOME', str(home / 'cache')),
               COPILOT_GITHUB_TELEMETRY_DISABLED='true', NO_PROXY='127.0.0.1,localhost',
               no_proxy='127.0.0.1,localhost')
    (home / 'home').mkdir(exist_ok=True, mode=0o700)
    return env


class Runtime:
    def __init__(self, gateway=None):
        self.gateway = gateway
        self._loop = None
        self._thread = None
        self._client = None
        self._guard = threading.Lock()

    # ------------------------------------------------------------ thread/loop
    def _ensure_loop(self):
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._loop.run_forever, name='agent-runtime', daemon=True)
                self._thread.start()
            return self._loop

    async def _ensure_client(self):
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.ping('health'), 10)
                return self._client
            except Exception:
                logger.warning('Agent runtime unresponsive: restarting')
                try:
                    await self._client.force_stop()
                except Exception:
                    pass
                self._client = None
        from copilot import CopilotClient
        home = _home()
        client = CopilotClient(mode='empty', base_directory=str(home), env=_env(home),
                               session_idle_timeout_seconds=1800)
        await client.start()
        self._client = client
        return client

    def stop(self):
        loop, client = self._loop, self._client
        if loop is None:
            return
        if client is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.stop(), loop).result(15)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        self._loop = self._client = None

    # --------------------------------------------------------------- turno
    def run(self, state, system, prompt, scope, timeout):
        """Esegue un turno completo e ritorna il testo finale. Chiamare da un thread di worker."""
        if self.gateway is None:
            self.gateway = gateway_module.default()
        self.gateway.start()
        loop = self._ensure_loop()
        turn = self.gateway.open_turn(state.context, scope)
        future = asyncio.run_coroutine_threadsafe(
            self._guarded(state, turn, system, prompt, timeout), loop)
        try:
            text = future.result(timeout + 60)
        except BaseException as exc:
            future.cancel()
            self._translate(state, turn, exc)
            raise
        finally:
            self.gateway.close_turn(turn)
        self._translate(state, turn, None)
        return text

    def _translate(self, state, turn, exc):
        """Un lease perso o un budget esaurito hanno la precedenza su qualunque errore dell'SDK."""
        failure = state.failure or turn.failure
        if isinstance(failure, (jobs.LeaseLost, jobs.Deferred)):
            raise failure
        if isinstance(failure, (ai_budget.BudgetExceeded, ai_budget.BudgetUnavailable)):
            raise jobs.Deferred('Budget AI non disponibile') from None
        if exc is not None and not isinstance(exc, AgentFailed):
            raise AgentFailed(f'Agente non disponibile ({type(exc).__name__})') from None

    async def _guarded(self, state, turn, system, prompt, timeout):
        return await asyncio.wait_for(self._converse(state, turn, system, prompt, timeout), timeout + 30)

    async def _converse(self, state, turn, system, prompt, timeout):
        from copilot import ToolSet
        from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
        from copilot.session_events import AssistantMessageData, PermissionRequestCustomTool, SessionErrorData

        denied, replies, errors = [], [], []

        def on_permission(request, invocation):
            if isinstance(request, PermissionRequestCustomTool):
                return PermissionDecisionApproveOnce()
            denied.append(type(request).__name__)
            return PermissionDecisionReject(feedback='Operazione non consentita')

        async def pre_tool(info, invocation):
            if state.failure is not None:
                return {'permissionDecision': 'deny', 'permissionDecisionReason': 'Esecuzione interrotta'}
            return {'permissionDecision': 'allow'}

        def on_event(event):
            payload = event.data
            if isinstance(payload, AssistantMessageData) and (payload.content or '').strip():
                replies.append(payload.content)
            elif isinstance(payload, SessionErrorData):
                errors.append(getattr(payload, 'message', 'errore'))

        client = await self._ensure_client()
        session_id = f'{state.mode}-{state.context.job_id}-{uuid.uuid4().hex[:8]}'
        session = None
        try:
            session = await client.create_session(
                on_permission_request=on_permission, model=config.AGENT_MODEL, session_id=session_id,
                provider=dict(type='openai', base_url=self.gateway.url, api_key=turn.token, wire_api='completions'),
                tools=toolkit.sdk_tools(state), available_tools=ToolSet().add_custom('*'),
                system_message={'mode': 'replace', 'content': system}, streaming=False,
                enable_session_telemetry=False, hooks={'on_pre_tool_use': pre_tool},
                working_directory=str(_home()))
            session.on(on_event)
            reply = await session.send_and_wait(prompt, timeout=timeout)
            text = (reply.data.content if reply and getattr(reply.data, 'content', None) else
                    (replies[-1] if replies else '')).strip()
        except asyncio.TimeoutError:
            if session is not None:
                try:
                    await session.abort()
                except Exception:
                    pass
            raise AgentFailed('Tempo massimo del turno superato') from None
        finally:
            if session is not None:
                for cleanup in (session.disconnect, lambda: client.delete_session(session_id)):
                    try:
                        await cleanup()
                    except Exception:
                        pass
        if denied:
            logger.warning('Agent runtime denied unexpected permission requests: %s', sorted(set(denied)))
        if not text:
            raise AgentFailed('Risposta vuota' + (f' ({errors[-1][:80]})' if errors else ''))
        return text


_default = None
_default_lock = threading.Lock()


def default():
    global _default
    with _default_lock:
        if _default is None:
            _default = Runtime()
        return _default


def stop():
    """Arresto ordinato del runtime condiviso e del suo gateway (idempotente)."""
    global _default
    with _default_lock:
        runtime, _default = _default, None
    if runtime is not None:
        runtime.stop()
        if runtime.gateway is not None:
            runtime.gateway.stop()
